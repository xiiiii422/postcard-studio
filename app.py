# -*- coding: utf-8 -*-
"""
Postcard Studio — web app.
Upload a photo, choose a style/pattern, generate the postcard.
A deterministic Pillow renderer produces PNG/PDF; the vector renderer produces
editable SVG. AI is used only for image generation (image-to-image restyle and
text-to-image backgrounds), not for element recognition.
"""
import base64, io, json, os, sys, hashlib, time, threading, urllib.request, warnings
from collections import OrderedDict
from urllib.parse import urlsplit
from flask import Flask, request, jsonify, send_file, has_request_context
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))     # every resource sits next to app.py

# The launchers redirect our output to log files, and a redirected stream inherits the OEM code
# page - which cannot encode the Chinese in these messages or the degree sign in "N deg 01".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


MAX_UPLOAD_PIXELS = 50_000_000    # 50 MP: far above any phone/camera photo, small enough to decode safely
# Pillow only hard-stops at 2x this value; the exact budget is enforced by load_photo() itself.
Image.MAX_IMAGE_PIXELS = MAX_UPLOAD_PIXELS * 2


class PhotoTooLarge(Exception):
    """An upload above the pixel budget - turned into a friendly 413 by the error handler below."""


def load_photo(f):
    """Open an uploaded image and correct EXIF orientation (phone photos).
    EXIF is carried across convert() on purpose: read_date() reads DateTimeOriginal from it, and a
    decompression bomb is refused from the header, before a single pixel is decoded."""
    from PIL import ImageOps
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(f)
            w, h = img.size
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise PhotoTooLarge("图片尺寸异常（可能是损坏或构造的超大图），已拒绝处理。") from e
    if w * h > MAX_UPLOAD_PIXELS:
        raise PhotoTooLarge("图片像素过多（%d×%d，上限 5000 万像素），请先缩小后再上传。" % (w, h))
    exif = img.info.get("exif")      # convert() drops info["exif"]; keep it so M.read_date still works
    img = ImageOps.exif_transpose(img.convert("RGB"))
    if exif and not img.info.get("exif"):
        img.info["exif"] = exif
    return img

import make_postcard as M

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024   # refuse absurd uploads instead of exhausting RAM

T2I_URL = "https://api.siliconflow.cn/v1/images/generations"
T2I_MODEL = "Tongyi-MAI/Z-Image-Turbo"                  # T2I (text-to-image) for generic scene
IMGEDIT_MODEL = "Qwen/Qwen-Image-Edit-2509"              # image-to-image: preserves the photo, only restyles (reliable)
FREE_IMG_MODEL = "Kwai-Kolors/Kolors"                    # media-tools skill: free background generation (文生图)
QUALITY_IMG_MODEL = "Tongyi-MAI/Z-Image-Turbo"           # higher-quality text-to-image (commercial, cost-effective)


def cred_candidates():
    """Where a dsh credentials file may live: $DSH_CRED wins, then $DSH_HOME (which may point at
    either the harness root or the folder that directly holds the file), then the default install
    under the user's home directory. No absolute path is baked in, so the app runs on any OS/user."""
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), "deepseek-harness")
    rel = ".credentials.yaml"
    seen, out = set(), []
    for p in (os.environ.get("DSH_CRED"), os.path.join(home, rel),
              os.path.join(home, "dsh-home", rel)):
        if p and p not in seen and os.path.exists(p):
            seen.add(p); out.append(p)
    return out


def read_cred(name, path=None):
    """Pull a `refs:` entry out of the dsh credentials file (never hard-code secrets here)."""
    for p in ([path] if path else cred_candidates()):
        in_refs = False
        try:
            for ln in open(p, encoding="utf-8"):
                s = ln.rstrip()
                if s.startswith("refs:"):
                    in_refs = True; continue
                if in_refs and s and not s[0].isspace():
                    in_refs = False
                if in_refs and s.lstrip().startswith(name + ":"):
                    v = s.split(":", 1)[1].strip()
                    if v:
                        return v
        except Exception:
            pass
    return None


def _dotenv(path):
    """Minimal KEY=VALUE reader so the API key can live in app.py's folder, not in app.py."""
    out = {}
    try:
        for ln in open(path, encoding="utf-8"):
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


ENV = _dotenv(os.path.join(ROOT, ".env"))
# Self-tests and CI must be deterministic even when a developer has a .env with real keys:
# POSTCARD_NO_AI=1 makes every secret lookup return "", while host/port/threads still work.
_NO_AI = os.environ.get("POSTCARD_NO_AI") == "1"


def _cfg(name, default=""):
    """Same precedence as a secret, minus the credentials file: process env > .env > built-in."""
    return (os.environ.get(name) or ENV.get(name) or default).strip()


# ---- same-origin guard ---------------------------------------------------------------
# The page is served by this app, so its calls are same-origin; a page opened from disk sends
# "Origin: null". Every other Origin is a cross-site caller and must not reach a POST endpoint:
# a multipart POST is a CORS-"simple" request, so without this any web page could drive renders
# (and paid AI calls) on this machine.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
SITE_METHODS = ("POST", "OPTIONS")


def _split_origin(origin):
    try:
        u = urlsplit(origin)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return u.hostname.lower(), (u.port or (443 if u.scheme == "https" else 80))


def origin_allowed(origin):
    """True for same-origin requests and file:// pages (Origin: null)."""
    if not origin or origin == "null":
        return True
    got = _split_origin(origin)
    if not got:
        return False
    host, port = got
    try:
        this_port = str(int(_cfg("POSTCARD_PORT", "5179") or "5179"))
    except ValueError:
        this_port = "5179"
    if host in LOCAL_HOSTS and str(port) == this_port:
        return True
    # A LAN/custom bind (POSTCARD_HOST=0.0.0.0) serves the page from its own address, which only
    # the request's Host header knows - accept that same origin without opening up the whole web.
    if not has_request_context():
        return False
    try:
        served = urlsplit("//" + (request.host or ""))
    except ValueError:
        return False
    if host != (served.hostname or "").lower():
        return False
    if served.port is None:
        # Behind a tunnel/proxy the Host header may omit the port; the browser's Origin still
        # carries 443/80. Accept the same host there, or the configured local port.
        return port in (80, 443) or str(port) == this_port
    return port == served.port


@app.before_request
def guard_cross_site():
    if request.method not in SITE_METHODS:
        return None
    origin = request.headers.get("Origin")
    if origin and not origin_allowed(origin):
        return jsonify({"error": "跨站请求已被拒绝。"}), 403
    if request.method == "OPTIONS":
        return "", 204
    return None


@app.after_request
def cors(resp):
    origin = request.headers.get("Origin")
    if origin and origin_allowed(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin     # never "*": only same-origin/file://
        resp.headers.add("Vary", "Origin")
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _secret(name, *fallbacks):
    """Resolve a key: process env > .env beside app.py > dsh credentials file."""
    if _NO_AI:
        return ""
    v = (os.environ.get(name) or ENV.get(name) or "").strip()
    if v:
        return v
    for cand in (fallbacks or (name,)):
        v = read_cred(cand)
        if v:
            return v
    return ""


# AI is used for image generation only (image-to-image restyle / text-to-image backgrounds).
T2I_KEY = _secret("SILICONFLOW_API_KEY", "SILICONFLOW_KEY", "DSH_SILICONFLOW_KEY")
if not T2I_KEY:
    print("[明信片工坊] 未找到 SILICONFLOW_API_KEY：AI 图生图/背景生成不可用，"
          "会回退为本地矢量渲染；配置方法见 .env.example。")


_T2I_LAST = [0.0]
_T2I_GAP = 4.0
# genKey -> image, so the export/download of a card reuses the SAME AI image (no extra API cost).
# These are full RGB rasters at ~1536px, i.e. ~4.7MB each, and waitress serves 12 threads at once:
# the map is therefore LRU-capped. A miss is cheap and safe - it only re-reads the on-disk cache.
_T2I_CACHE = OrderedDict()
_T2I_MAX = 8
_AI_NOTE = threading.local()   # why AI was skipped/failed, surfaced to the browser instead of failing
                               # silently. Per-thread on purpose: with a shared list, one request could
                               # clear (or overwrite) another request's note between the write and the
                               # read, and the user got a hand-drawn card with no explanation at all.
_AI_BLOCKED = [None]       # set once the provider rejects the key, so we stop paying 4s of retries per click
IMG_CACHE_DIR = os.path.join(ROOT, "cache", "img2img")   # persistent cache (same photo -> no re-generation)
_CACHE_LOCK = threading.Lock()                            # waitress runs 12 request threads
_THROTTLE_LOCK = threading.Lock()


def _throttle():
    """Serialise paid generation calls so concurrent requests respect the provider rate gap."""
    with _THROTTLE_LOCK:
        wait = _T2I_GAP - (time.time() - _T2I_LAST[0])
        if wait > 0:
            time.sleep(wait)
        _T2I_LAST[0] = time.time()


def _memo_get(genKey):
    """The memoised image for THIS genKey, or None. Keys are derived from the photo plus the
    options, so hitting another card's entry is impossible by construction - and returning one
    anyway would print a stranger's artwork on this user's postcard."""
    if not genKey:
        return None
    with _CACHE_LOCK:
        img = _T2I_CACHE.get(genKey)
        if img is not None:
            _T2I_CACHE.move_to_end(genKey)
    return img


def _memo(genKey, img):
    """Publish a generated image to the LRU memo and return it."""
    if genKey:
        with _CACHE_LOCK:
            _T2I_CACHE[genKey] = img
            _T2I_CACHE.move_to_end(genKey)
            while len(_T2I_CACHE) > _T2I_MAX:
                _T2I_CACHE.popitem(last=False)
    return img


def _cache_get(key):
    p = os.path.join(IMG_CACHE_DIR, key + ".png")
    if os.path.exists(p):
        try:
            img = Image.open(p).convert("RGB")
            if img.size[0] >= 512:
                return img
        except Exception:
            pass
    return None


def _cache_put(key, img):
    try:
        os.makedirs(IMG_CACHE_DIR, exist_ok=True)
        img.save(os.path.join(IMG_CACHE_DIR, key + ".png"), "PNG")
        _cache_trim(200)
    except Exception:
        pass


def _cache_trim(max_files=200):
    try:
        files = [(os.path.getmtime(os.path.join(IMG_CACHE_DIR, f)), f)
                 for f in os.listdir(IMG_CACHE_DIR) if f.endswith(".png")]
        files.sort(reverse=True)
        for _, f in files[max_files:]:
            try: os.remove(os.path.join(IMG_CACHE_DIR, f))
            except Exception: pass
    except Exception:
        pass


CACHE_DIR = os.path.join(ROOT, "cache")
CACHE_TTL_DAYS = 14
CACHE_MAX_BYTES = 300 * 1024 * 1024


def clean_cache(max_age_days=CACHE_TTL_DAYS, max_bytes=CACHE_MAX_BYTES):
    """Keep cache/ bounded at start-up: delete files untouched for 14 days, then the oldest ones
    until the folder fits 300MB. Only cache copies live here - every miss just re-renders."""
    now = time.time()
    kept = []
    for base, _dirs, names in os.walk(CACHE_DIR):
        for name in names:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if now - st.st_mtime > max_age_days * 86400:
                try: os.remove(p)
                except OSError: pass
            else:
                kept.append((st.st_mtime, st.st_size, p))
    total = sum(size for _mtime, size, _p in kept)
    if total > max_bytes:
        kept.sort()
        for _mtime, size, p in kept:
            if total <= max_bytes:
                break
            try:
                os.remove(p)
                total -= size
            except OSError:
                pass
    return total


def t2i_image(prompt, genKey=None, size="1536x1024", fresh=False):
    """Generate an image via the T2I model. Reuses the memo, then the on-disk cache, before paying
    for a call - the other two generators both did; this one re-billed the SAME prompt after every
    service restart, and ignored `fresh`, so 「换一张」 could never return a different scene."""
    hit = _memo_get(genKey)
    if hit is not None:
        return hit
    key = hashlib.sha256(("t2i:" + T2I_MODEL + ":" + size + ":" + prompt).encode()).hexdigest()
    if not fresh:
        cached = _cache_get(key)
        if cached is not None:
            return _memo(genKey, cached)
    _throttle()
    payload = {"model": T2I_MODEL, "prompt": prompt, "image_size": size, "batch_size": 1}
    req = urllib.request.Request(T2I_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + T2I_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=150) as r:
        data = json.loads(r.read())
    url = data["images"][0]["url"]
    with urllib.request.urlopen(url, timeout=150) as r:
        img = Image.open(io.BytesIO(r.read())).convert("RGB")
    _cache_put(key, img)
    return _memo(genKey, img)


STYLE_HINT = {"watercolor": "gentle watercolor", "ink": "ink wash", "minimal": "minimalist flat",
              "vintage": "vintage retro", "pastel": "soft pastel", "film": "cinematic warm",
              "oil": "oil painting", "handdrawn": "hand-drawn"}

EL_SCENE = {
    "bamboo": "lush bamboo forest", "forest": "pine forest", "tree": "forest", "pines": "pine forest", "woods": "forest",
    "mountain": "majestic mountain range", "peak": "majestic mountain range", "cliff": "mountain cliffs", "hill": "rolling hills",
    "water": "serene lake", "sea": "calm sea", "ocean": "calm sea", "river": "river through landscape", "lake": "serene lake",
    "beach": "sandy beach", "coast": "seaside coastline", "harbor": "harbour", "pier": "wooden pier",
    "road": "serpentine road", "street": "old town street", "path": "winding path", "lane": "country lane", "bridge": "bridge over water",
    "sunset": "golden sunset", "sunrise": "sunrise", "dusk": "twilight", "night": "starry night", "dawn": "dawn glow",
    "sky": "expansive sky", "cloud": "cloudy sky", "stars": "starry night sky", "moon": "moonlit night", "aurora": "aurora borealis", "rainbow": "rainbow",
    "building": "quaint village architecture", "city": "cityscape", "tower": "old tower", "house": "cottage", "castle": "castle",
    "boat": "boat on its water", "sailboat": "sailboat at sea", "ship": "ship at sea", "train": "train through a landscape",
    "field": "green meadow", "grass": "meadow", "flowers": "flower meadow", "garden": "blooming garden", "tent": "camping tent",
    "snow": "snowy landscape", "ice": "glacier", "rain": "rainy scene", "storm": "moody storm", "fog": "misty scene", "mist": "misty scene",
    "balloon": "hot air balloon over the landscape", "waterfall": "waterfall", "goat": "pasture", "sunny": "sunny day", "light": "golden light",
}


def scene_phrase(elements):
    seen = []
    for e in (elements or []):
        w = EL_SCENE.get(str(e).strip().lower())
        if w and w not in seen:
            seen.append(w)
    return ", ".join(seen[:6])


def t2i_prompt(r, desc=""):
    # img2img: change ONLY the rendering style, keep the photo's scene/objects identical.
    # Keep the prompt minimal (no elements/theme) so the model trusts the input image.
    hint = STYLE_HINT.get((r.get("style") or "watercolor").lower(), "soft watercolor")
    d = desc.strip()
    core = (f"Repaint this exact photo as a {hint} artwork. Keep the identical scene, objects "
            f"and composition; only change the rendering style. Do not add or remove any object. No text, no watermark.")
    if d:
        core = (f"Repaint this exact photo as a {hint} artwork with this feel: {d}. Keep the identical scene, "
                f"objects and composition; only change the rendering style. Do not add or remove any object. No text, no watermark.")
    return core


def img2img_edit(photo, prompt, genKey=None, size="1536x1024", fresh=False, orient="auto"):
    """Image-to-image (img2img) with Qwen-Image-Edit: repaint the uploaded photo in a new
    style, keeping the scene/objects identical. Cached locally (by photo+prompt) so the SAME
    photo is never re-generated; `fresh=True` forces a new render (for the 换一张 button)."""
    hit = _memo_get(genKey)
    if hit is not None:
        return hit
    # pre-crop to the FRONT card aspect (portrait photo -> 2:3, landscape -> 3:2) so nothing is over-cropped
    ph_ar = photo.size[1]/photo.size[0]
    _cw, _ch = M.card_dims(ph_ar, orient)
    tar = _cw/_ch
    im = photo.copy(); im.thumbnail((1536, 1536), Image.LANCZOS)
    w, h = im.size
    if tar >= 1: cw, ch = min(1536, w), round(min(1536, w)/tar)
    else: ch, cw = min(1536, h), round(min(1536, h)*tar)
    im = M.cover_crop(im, cw, ch)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=92)
    img_bytes = buf.getvalue()
    key = hashlib.sha256(img_bytes + prompt.encode()).hexdigest()
    if not fresh:
        cached = _cache_get(key)
        if cached is not None:
            return _memo(genKey, cached)
    _throttle()
    duri = "data:image/jpeg;base64," + base64.b64encode(img_bytes).decode()
    payload = {"model": IMGEDIT_MODEL, "prompt": prompt, "image_size": size, "batch_size": 1, "image": duri}
    req = urllib.request.Request(T2I_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + T2I_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=200) as r:
        data = json.loads(r.read())
    url = data["images"][0]["url"]
    with urllib.request.urlopen(url, timeout=150) as r:
        img = Image.open(io.BytesIO(r.read())).convert("RGB")
    _cache_put(key, img)
    return _memo(genKey, img)


def free_gen(prompt, size="1024x1024", genKey=None, fresh=False, model=None):
    """Text-to-image background (media-tools skill). model: None -> FREE_IMG_MODEL(Kolors, 免费),
    or QUALITY_IMG_MODEL (Z-Image, 高品质商用). Cached locally by prompt."""
    model = model or FREE_IMG_MODEL
    key = hashlib.sha256(("img:" + model + ":" + prompt).encode()).hexdigest()
    hit = _memo_get(genKey)
    if hit is not None:
        return hit
    if not fresh:
        cached = _cache_get(key)
        if cached is not None:
            return _memo(genKey, cached)
    _throttle()
    payload = {"model": model, "prompt": prompt, "image_size": size, "batch_size": 1}
    img = None
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(T2I_URL, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + T2I_KEY}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            url = data["images"][0]["url"]
            with urllib.request.urlopen(url, timeout=60) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
            break
        except Exception as e:
            if _maybe_block(e):
                raise RuntimeError(str(e)[:120])      # a rejected key will not improve on retry
            print("bg gen attempt", attempt, "failed:", str(e)[:100])
            last, img = e, None
    if img is None:
        raise RuntimeError("bg generation failed: %s" % str(last)[:120])
    _cache_put(key, img)
    return _memo(genKey, img)


def bg_prompt(r, desc=""):
    """Build a keyword-driven background prompt (workflow 1: keyword text-to-image)."""
    base = "clean scenic background, no text, no watermark, no logo, no signature, no stamp, no border"
    if desc.strip():
        return (desc.strip() + ", " + base)
    hint = STYLE_HINT.get((r.get("style") or "watercolor").lower(), "gentle watercolor")
    scene = scene_phrase(r.get("elements") or [])
    parts = [f"a beautiful {hint} illustration"]
    if scene:
        parts.append("of " + scene)
    return ", ".join(parts) + ", rich color, soft light, elegant travel postcard background, " + base


def get_ai_image(r, photo, desc=""):
    """The ONE place that turns a pattern mode into the AI artwork shared by both card faces.
    handdrawn -> None (pure vector engine).
    t2i       -> img2img restyle of the uploaded photo (Qwen-Image-Edit).
    zine      -> img2img torn-paper watercolor collage.
    aibg      -> bg_mode 'keywords': text-to-image scene (Kolors 免费 / Z-Image 高品质, see bg_model);
                 bg_mode 'photo'  : same img2img restyle as t2i."""
    pattern = r.get("pattern") or "handdrawn"
    note_ai(None)
    if pattern == "handdrawn":
        return None                     # pure vector engine: no AI call and no "AI unavailable" note
    # Guard here, not only inside safe_t2i: the aibg/keywords branch below calls free_gen() directly,
    # which used to POST with an empty "Bearer " and come back 401. That burned the throttle on a call
    # that cannot succeed and then recorded "密钥被服务方拒绝，请在 .env 填入有效 key" - a lie, since the
    # real reason was that no key is configured at all.
    if _AI_BLOCKED[0]:
        return note_ai(_AI_BLOCKED[0])
    if not T2I_KEY:
        return note_ai("未配置 SILICONFLOW_API_KEY：AI 生图/风格化不可用，已改用矢量手绘引擎。")
    genKey, fresh, orient = r.get("genkey"), r.get("fresh"), (r.get("orient") or "auto")
    if pattern == "zine":
        return safe_t2i(zine_prompt(r, desc), genKey, photo, fresh, orient)
    if pattern == "aibg":
        if (r.get("bg_mode") or "keywords") == "photo":
            return safe_t2i(t2i_prompt(r, desc), genKey, photo, fresh, orient)
        model = FREE_IMG_MODEL if r.get("bg_model") == "free" else QUALITY_IMG_MODEL
        try:
            return free_gen(bg_prompt(r, desc), size="1536x1024", genKey=genKey, fresh=fresh, model=model)
        except Exception as e:
            if _AI_BLOCKED[0]:
                return note_ai(_AI_BLOCKED[0])
            print("aibg text-to-image failed -> img2img of the photo:", str(e)[:100])
            return safe_t2i(t2i_prompt(r, desc), genKey, photo, fresh, orient)
    if pattern in ("t2i", "photo"):
        return safe_t2i(t2i_prompt(r, desc), genKey, photo, fresh, orient)
    return None


def zine_prompt(r, desc=""):
    """Gathered Scenes Zine v1.3 (实景拼贴): RICH watercolor of the photo (kept clear & vivid) mounted
    as a hand-torn paper-collage - torn edge + paper texture + small high-chroma echo + quiet negative space."""
    d = desc.strip()
    core = ("Repaint this exact photo as a RICH, saturated watercolor painting, clearly preserving the whole "
            "scene and subject, vividly colored - do NOT fade, wash out or hide the subject. Mount this "
            "watercolor as a hand-torn paper collage on warm cream paper: a visible torn fibrous paper edge "
            "around the watercolor, watercolor paper texture and grain, a little quiet paper negative space, "
            "and ONE small accent of a high-chroma hue that echoes the scene. Keep the watercolor scene bold "
            "and central; do NOT cover the subject with a large flat colour field or abstract shapes. "
            "NO text, NO lettering, NO watermark, NO logo, NO border.")
    if d:
        return core + " Watercolor feel: " + d + "."
    return core


def _maybe_block(e):
    """A key the provider rejects will not fix itself mid-session: remember it so the next click
    answers at once instead of waiting out the throttle again."""
    code = getattr(e, "code", None)
    if code in (401, 403) or "Token is invalid" in str(e):
        _AI_BLOCKED[0] = ("AI 密钥被服务方拒绝（HTTP %s），请在 .env 里填入有效的 SILICONFLOW_API_KEY 并重启服务；"
                          "在此之前一律改用矢量手绘引擎。" % (code or 401))
        return True
    return False


def note_ai(msg):
    """Record (and echo) why the AI artwork is unavailable, so the UI can say so out loud."""
    _AI_NOTE.v = msg
    if msg:
        print("[AI]", msg)
    return None


def ai_note():
    """The note recorded by this request thread, if any."""
    return getattr(_AI_NOTE, "v", None) or ""


def safe_t2i(prompt, genKey=None, photo=None, fresh=False, orient="auto"):
    """AI pattern: img2img style-transfer of the uploaded photo (default), else T2I scene.
    `fresh=True` bypasses the local cache (换一张). On failure return None to fall back to hand-drawn."""
    if _AI_BLOCKED[0]:
        note_ai(_AI_BLOCKED[0])
        return None
    if not T2I_KEY:
        return note_ai("未配置 AI 密钥（环境变量或 .env 里的 SILICONFLOW_API_KEY），已改用矢量手绘引擎。")
    try:
        if photo is not None:
            try:
                return img2img_edit(photo, prompt, genKey=genKey, fresh=fresh, orient=orient)
            except Exception as e:
                if _maybe_block(e):
                    return note_ai(_AI_BLOCKED[0])
                # Only ever pick up the image generated for THIS card: the genKey hashes the photo
                # and the options, so borrowing another visitor's artwork would print a stranger's
                # photo on this one.
                hit = _memo_get(genKey)
                if hit is not None:
                    print("img2img failed -> reuse this card's cached AI image:", str(e)[:100])
                    return hit
                raise
        return t2i_image(prompt, genKey=genKey, fresh=fresh)
    except Exception as e:
        print("t2i failed -> fallback hand-drawn:", str(e)[:120])
        return note_ai("AI 生成未成功（%s），已改用矢量手绘引擎。" % str(e)[:80])


STYLE_KEYWORDS = [
    ("watercolor", "watercolor"), ("水彩", "watercolor"),
    ("ink", "ink"), ("水墨", "ink"), ("墨", "ink"),
    ("oil", "oil"), ("油画", "oil"), ("厚涂", "oil"),
    ("minimal", "minimal"), ("极简", "minimal"), ("简约", "minimal"),
    ("vintage", "vintage"), ("复古", "vintage"), (".胶片", "film"), ("film", "film"), ("胶片", "film"),
    ("pastel", "pastel"), ("治愈", "pastel"), ("淡彩", "pastel"),
    ("handdrawn", "handdrawn"), ("手绘", "handdrawn"), ("插画", "handdrawn"),
]

def infer_style(desc):
    d = (desc or "").lower()
    for kw, s in STYLE_KEYWORDS:
        if kw in d:
            return s
    return "default"


def _num(value, default, lo, hi):
    """Clamp a form field into an integer range. Every slider here arrives as a string a browser (or
    a hand-built POST) can fill with anything; an unguarded int() turned that into a bare HTTP 500."""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _json_list(value):
    """Parse a form field that has to be a JSON array. Anything else becomes []: `extra="null"`
    used to reach the renderer as None and blow up on the first `for` over it."""
    try:
        raw = json.loads(value) if value else []
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


def _clean_layout(value):
    """The canvas editor posts [{id,x,y,w,h,rot}]. draw_composed indexes item["x"] directly, so a
    single partial object aborted the whole render; keep only complete, positively-sized items."""
    out = []
    for item in _json_list(value):
        if not isinstance(item, dict):
            continue
        try:
            x, y, w, h = (float(item["x"]), float(item["y"]), float(item["w"]), float(item["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not (w > 0 and h > 0):
            continue
        out.append({"id": str(item.get("id") or ""), "x": x, "y": y, "w": w, "h": h,
                    "rot": _num(item.get("rot"), 0, -360, 360)})
    return out or None


def resolve(photo, options):
    """Compute palette/paper/style/date/title/elements/extra driven by options."""
    palette = M.sample_palette(photo)
    user_date = (options.get("date") or "").strip()      # a date the UI/API supplied wins over EXIF
    date_str = user_date or M.read_date(photo)
    title = (options.get("title") or "").strip() or "UNTITLED"
    number = (options.get("number") or "").strip()
    elements = _json_list(options.get("elements"))
    extra = _json_list(options.get("extra"))
    layout = _clean_layout(options.get("layout"))

    # merge user custom tags so the design isn't limited to the built-in themes
    raw_tags = (options.get("tags") or "").strip()
    custom = [t.strip().lower() for t in raw_tags.split(",") if t.strip()]
    if custom:
        elements = list(elements) + [t for t in custom if t not in elements]

    style = (options.get("style") or "default").strip() or "default"
    desc = options.get("desc", "").strip()
    from_desc = infer_style(desc)
    if style == "default" and from_desc != "default":
        style = from_desc
    elif from_desc != "default":
        style = from_desc
    palette, paper = M.apply_style(palette, style)
    skin = (options.get("paper_skin") or "").strip()
    if skin and skin in M.PAPER_SKINS:
        paper = M.PAPER_SKINS[skin]
    tfont = M.TFONTS.get((options.get("tfont") or "").strip())
    rounded = options.get("rounded") == "1"
    filt = (options.get("filt") or "none").strip() or "none"
    location = (options.get("location") or "").strip()
    tstyle = (options.get("tstyle") or "normal").strip() or "normal"
    enhance = options.get("enhance") == "1"
    pattern = (options.get("pattern") or "handdrawn").strip() or "handdrawn"
    genkey = (options.get("genkey") or "").strip()
    glass_a = _num(options.get("glass_a"), 50, 0, 100)     # white opacity
    glass_b = _num(options.get("glass_b"), 3, 1, 10)       # blur strength
    fresh = (options.get("fresh") == "1")
    bg_mode = (options.get("bg_mode") or "keywords").strip() or "keywords"
    bg_model = (options.get("bg_model") or "quality").strip() or "quality"
    orient = (options.get("orient") or "auto").strip() or "auto"
    crop = None
    if options.get("crop"):
        raw = _json_list(options["crop"])
        try:
            vals = [float(v) for v in raw]
        except (TypeError, ValueError):
            vals = []                                   # crop_photo() float()s each entry straight away
        if len(vals) == 4 and all(0.0 <= v <= 1.0 for v in vals) and vals[2] > 0 and vals[3] > 0:
            crop = vals

    show_date = options.get("show_date", "1") == "1"
    want_back = options.get("want_back", "1") == "1"
    swatches = options.get("swatches", "1") == "1"
    return {"palette": palette, "paper": paper, "style": style, "date_str": date_str,
            "title": title, "number": number, "elements": elements, "extra": extra, "layout": layout,
            "tfont": tfont, "rounded": rounded, "filt": filt, "location": location, "tstyle": tstyle, "enhance": enhance, "crop": crop, "pattern": pattern, "genkey": genkey, "glass_a": int(glass_a/100*255), "glass_b": glass_b/5.0, "fresh": fresh, "bg_mode": bg_mode, "bg_model": bg_model, "orient": orient,
            "show_date": show_date, "want_back": want_back, "swatches": swatches}


def img_to_b64_jpg(im, q=0.85):
    buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=int(q*100))
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _face(r, photo):
    """Apply the user's crop + enhance exactly once, so preview and export share identical pixels."""
    ph = M.crop_photo(photo, r["crop"]) if r["crop"] else photo
    return M.enhance_photo(ph) if r["enhance"] else ph


def render_faces(r, photo, dpi, desc="", faces=("front", "back")):
    """Render the requested faces from ONE AI artwork: the two sides of a card must always match,
    and one generation (not two) is billed. Falls back to the vector engine when AI fails."""
    ph = _face(r, photo)
    dstr = r["date_str"] if r["show_date"] else ""
    t2i = get_ai_image(r, ph, desc)
    out = {}
    if "front" in faces:
        if t2i is not None:
            out["front"] = M.render_t2i(photo, r["palette"], t2i, r["title"], dstr, dpi=dpi, tfont=r["tfont"],
                                        tstyle=r["tstyle"], number=r["number"], location=r["location"],
                                        rounded=r["rounded"], filt=r["filt"], paper=r["paper"],
                                        glass_a=r["glass_a"], glass_b=r["glass_b"], orient=r.get("orient") or "auto")
        else:
            out["front"] = M.render_image(ph, r["palette"], r["paper"], r["elements"], r["title"], dstr,
                                          style=r["style"], swatches=r["swatches"], dpi=dpi, extra=r["extra"],
                                          layout=r["layout"], tfont=r["tfont"], rounded=r["rounded"], filt=r["filt"],
                                          number=r["number"], location=r["location"], tstyle=r["tstyle"])
    if "back" in faces:
        if t2i is not None:
            out["back"] = M.render_t2i_mail(t2i, r["palette"], r["number"], r["title"], photo=ph, dpi=dpi,
                                            rounded=r["rounded"], filt=r["filt"], location=r["location"],
                                            paper=r["paper"], tfont=r["tfont"])
        else:
            out["back"] = M.render_mail(ph, r["palette"], r["paper"], r["number"], r["title"], dpi=dpi,
                                        rounded=r["rounded"], filt=r["filt"], location=r["location"])
    return out


def render_front_for(r, photo, dpi, desc=""):
    """Pattern side only (used by the single-face export endpoints)."""
    return render_faces(r, photo, dpi, desc, ("front",))["front"]


def render_back_for(r, photo, dpi, desc=""):
    """Writing side only."""
    return render_faces(r, photo, dpi, desc, ("back",))["back"]


PREVIEW_DPI = 210     # small render + JPEG keeps the preview payload light (mobile-friendly)


def render(png, options):
    """Preview: identical code path to the export, only at a lighter DPI.
    `ai_note` tells the page why a hand-drawn card arrived when an AI pattern was asked for."""
    photo = load_photo(png)
    r = resolve(photo, options)
    desc = options.get("desc", "")
    faces = ("front", "back") if r["want_back"] else ("front",)
    faces = render_faces(r, photo, PREVIEW_DPI, desc, faces)
    out = {k: img_to_b64_jpg(v) for k, v in faces.items()}
    out["ai_note"] = ai_note()
    return out


def card_trim(img_rgb, landscape=(150.0, 100.0)):
    """Print trim for one render: 150x100mm landscape, or 100x150mm when the card is portrait.
    Without this, a portrait card was squeezed into a 150x100 page (distorted artwork)."""
    w, h = img_rgb.size
    return landscape if w >= h else (landscape[1], landscape[0])


def _place_image(c, img_rgb, x, y, w_pt, h_pt, cmyk=False):
    """Draw an in-memory PIL image straight into the PDF - no temporary PNG on disk."""
    from reportlab.lib.utils import ImageReader
    c.drawImage(ImageReader(img_rgb.convert("CMYK") if cmyk else img_rgb.convert("RGB")),
                x, y, width=w_pt, height=h_pt)


def _trim_marks(c, tx0, ty0, tx1, ty1, arm_pt, width_mm=0.25):
    """Crop marks lying inside the bleed band, aligned to the trim line, so they are never
    clipped by the page edge (the old 5mm cross hung off a trim+3mm page and vanished)."""
    from reportlab.lib.units import mm as rlmm
    c.saveState()
    c.setLineWidth(width_mm*rlmm); c.setStrokeColorRGB(0, 0, 0)
    for cx, cy, dx, dy in ((tx0, ty0, -1, -1), (tx1, ty0, 1, -1), (tx0, ty1, -1, 1), (tx1, ty1, 1, 1)):
        c.line(cx, cy, cx + dx*arm_pt, cy)
        c.line(cx, cy, cx, cy + dy*arm_pt)
    c.restoreState()


def _outer_marks(c, x0, y0, x1, y1, gap_mm=2.0, len_mm=5.0, width_mm=0.25):
    """Standard external crop marks (gap + arm outside the trim) for sheets that have waste room."""
    from reportlab.lib.units import mm as rlmm
    g, L = gap_mm*rlmm, len_mm*rlmm
    c.saveState(); c.setLineWidth(width_mm*rlmm); c.setStrokeColorRGB(0, 0, 0)
    for cx, dx in ((x0, -1), (x1, 1)):
        for cy, dy in ((y0, -1), (y1, 1)):
            c.line(cx + dx*g, cy, cx + dx*(g+L), cy)
            c.line(cx, cy + dy*g, cx, cy + dy*(g+L))
    c.restoreState()


def build_print_pdf(img_rgb, trim_w=None, trim_h=None, bleed=3.0, cmyk=False):
    """Wrap one card render into a print-ready PDF: 3mm bleed + trim-aligned crop marks.
    The page follows the card's own shape unless an explicit trim is given."""
    from reportlab.pdfgen import canvas as rlcanvas
    from reportlab.lib.units import mm as rlmm
    if not (trim_w and trim_h):
        trim_w, trim_h = card_trim(img_rgb)
    pw, ph = (trim_w + 2*bleed)*rlmm, (trim_h + 2*bleed)*rlmm
    buf = io.BytesIO()
    c = rlcanvas.Canvas(buf, pagesize=(pw, ph))
    _place_image(c, img_rgb, bleed*rlmm, bleed*rlmm, trim_w*rlmm, trim_h*rlmm, cmyk)
    _trim_marks(c, bleed*rlmm, bleed*rlmm, (bleed+trim_w)*rlmm, (bleed+trim_h)*rlmm, bleed*0.72*rlmm)
    c.save(); buf.seek(0)
    return buf.read()


def build_print_pdf_set(img_list, cmyk=False, trim_w=None, trim_h=None, bleed=3.0):
    """Multi-page print PDF (front, back ...) with bleed + crop marks; one page per card face."""
    from reportlab.pdfgen import canvas as rlcanvas
    from reportlab.lib.units import mm as rlmm
    import io as _io
    buf = _io.BytesIO()
    sizes = [(card_trim(im) if not (trim_w and trim_h) else (trim_w, trim_h)) for im in img_list]
    first = sizes[0] if sizes else (150.0, 100.0)
    c = rlcanvas.Canvas(buf, pagesize=((first[0] + 2*bleed)*rlmm, (first[1] + 2*bleed)*rlmm))
    for im, (tw, th) in zip(img_list, sizes):
        c.setPageSize(((tw + 2*bleed)*rlmm, (th + 2*bleed)*rlmm))
        _place_image(c, im, bleed*rlmm, bleed*rlmm, tw*rlmm, th*rlmm, cmyk)
        _trim_marks(c, bleed*rlmm, bleed*rlmm, (bleed+tw)*rlmm, (bleed+th)*rlmm, bleed*0.72*rlmm)
        c.showPage()
    c.save(); buf.seek(0)
    return buf.read()


def build_sheet_pdf(img_rgb, cols=2, rows=1, landscape=True, card_w=None, card_h=None):
    """Tile a card onto an A4 sheet for printing multiples, with external crop marks.
    Two 150mm cards cannot fit side by side on 297mm, so the sheet is scaled; the note on
    the page says by how much, because a 1:1 claim here would print the wrong size."""
    from reportlab.pdfgen import canvas as rlcanvas
    from reportlab.lib.units import mm as rlmm
    from reportlab.lib.colors import HexColor
    import io as _io
    if not (card_w and card_h):
        card_w, card_h = card_trim(img_rgb)
    sw, sh = (297.0, 210.0) if landscape else (210.0, 297.0)
    gap = 12.0                       # room for the crop marks around each card
    buf = _io.BytesIO()
    c = rlcanvas.Canvas(buf, pagesize=(sw*rlmm, sh*rlmm))
    cell_w = (sw - gap*(cols+1)) / cols
    cell_h = (sh - gap*(rows+1)) / rows
    cw = min(cell_w, cell_h*card_w/card_h); ch = cw*card_h/card_w
    for r in range(rows):
        top = gap + r*(cell_h+gap)
        for col in range(cols):
            left = gap + col*(cell_w+gap)
            cx = left + (cell_w-cw)/2; cy = top + (cell_h-ch)/2
            ry = sh - (cy + ch)
            _place_image(c, img_rgb, cx*rlmm, ry*rlmm, cw*rlmm, ch*rlmm)
            _outer_marks(c, cx*rlmm, ry*rlmm, (cx+cw)*rlmm, (ry+ch)*rlmm)
    scale = cw/card_w*100.0
    c.setFont("Helvetica", 7); c.setFillColor(HexColor("#666666"))
    c.drawString(gap*rlmm, 6*rlmm,
                 "Postcard Studio: %dx  %.1fx%.1fmm card at %.0f%% of true size - print the single-card "
                 "PDF (not this sheet) for a 1:1 result." % (cols*rows, card_w, card_h, scale))
    c.save(); buf.seek(0)
    return buf.read()


@app.route("/")
def index():
    return send_file(os.path.join(ROOT, "index.html"))


@app.route("/status")
def status():
    """Tell the page whether AI image generation is available before the user clicks."""
    return jsonify({"ai_key": bool(T2I_KEY), "blocked": _AI_BLOCKED[0] or "",
                    "models": {"generation": bool(T2I_KEY), "edit": IMGEDIT_MODEL,
                               "free_bg": FREE_IMG_MODEL, "quality_bg": QUALITY_IMG_MODEL}})


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "图片过大（上限 64MB），请先缩小或降低分辨率后再上传。"}), 413


@app.errorhandler(PhotoTooLarge)
def photo_too_large(_e):
    return jsonify({"error": "图片像素过多（上限 5000 万像素），请先缩小后再上传。"}), 413


OPTION_KEYS = ("title", "number", "elements", "extra", "style", "desc", "swatches", "show_date",
               "date", "want_back", "layout", "paper_skin", "tfont", "rounded", "filt", "location", "tstyle",
               "enhance", "crop", "tags", "pattern", "genkey", "glass_a", "glass_b", "fresh",
               "bg_mode", "bg_model", "orient")

# /export answers with a file, so an unknown kind used to fall through to "back-print.pdf" and hand
# the user a silently wrong download. Only these values are served.
EXPORT_KINDS = ("front-pdf", "front-hi", "back-pdf", "back-hi", "sheet-pdf", "sheet-pdf-back",
                "print-cmyk", "pngset", "svg", "wallpaper")


def pick_options(src):
    """Whitelist the form fields both /generate, /export and /bundle accept."""
    return {k: v for k in OPTION_KEYS if (v := src.get(k)) is not None}


@app.route("/generate", methods=["POST"])
def generate():
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "no photo"}), 400
    options = pick_options(request.form)
    try:
        return jsonify(render(f, options))
    except PhotoTooLarge:
        raise
    except Exception:
        import traceback; traceback.print_exc()
        return jsonify({"error": "生成失败，请稍后重试；若持续失败请换一张照片。"}), 500


def safe_name(s):
    """Turn a postcard title into a filesystem-safe filename stem."""
    import re
    s = re.sub(r'[^\w\-\s\u4e00-\u9fff]+', '', (s or '').strip()).strip() or 'postcard'
    s = re.sub(r'\s+', '-', s)
    return s[:40]


@app.route("/export", methods=["POST"])
def export():
    """Download a hi-res file: front-hi / front-pdf / back-hi / back-pdf."""
    import io as _io
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "no photo"}), 400
    options = pick_options(request.form)
    kind = (request.form.get("kind") or "front-pdf").strip().lower()
    if kind not in EXPORT_KINDS:
        return jsonify({"error": "未知的导出类型：%s" % kind[:40]}), 400
    try:
        photo = load_photo(f)
        r = resolve(photo, options)
        stem = safe_name(r.get("title", ""))
        if kind == "print-cmyk":
            faces = render_faces(r, photo, 300, options.get("desc", ""),
                                 ("front", "back") if r["want_back"] else ("front",))
            pdf = build_print_pdf_set(list(faces.values()), cmyk=True)
            return send_file(io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=stem + "-print-CMYK.pdf")
        if kind == "pngset":
            import zipfile as _z
            faces = render_faces(r, photo, 600, options.get("desc", ""),
                                 ("front", "back") if r["want_back"] else ("front",))
            zbuf = _io.BytesIO()
            with _z.ZipFile(zbuf, "w", _z.ZIP_DEFLATED) as z:
                for name, im in faces.items():
                    buf = _io.BytesIO(); im.save(buf, "PNG"); z.writestr(stem + "-%s-600dpi.png" % name, buf.getvalue())
                wpx = M.render_wallpaper(photo, r["palette"], r["title"], r["date_str"] if r["show_date"] else "",
                                         elements=r["elements"], extra=r["extra"], tfont=r["tfont"], enhance=r["enhance"], crop=r["crop"], number=r["number"], location=r["location"], paper=r["paper"])
                buf = _io.BytesIO(); wpx.save(buf, "PNG"); z.writestr(stem + "-wallpaper-1080x1920.png", buf.getvalue())
            zbuf.seek(0)
            return send_file(zbuf, mimetype="application/zip", as_attachment=True, download_name=stem + "-PNG套件.zip")
        if kind == "svg":
            import make_postcard_svg as SVG
            import zipfile as _zf
            ph = _face(r, photo)                       # crop + enhance, same pixels as preview
            ratio = ph.size[1] / ph.size[0]
            # The art panel must use the SAME artwork as the PNG/PDF previews. For AI patterns
            # that means vectorising the image-to-image result, not the untouched photo.
            art_src = ph
            note = ""
            if (r.get("pattern") or "handdrawn") != "handdrawn":
                ai = get_ai_image(r, ph, options.get("desc", ""))
                if ai is not None:
                    art_src = ai.convert("RGB")
                note = ai_note()
            pal = SVG.sample_palette(art_src)
            href = SVG.embed(ph)                       # photo box keeps the real photo
            dstr = r["date_str"] if r["show_date"] else ""
            kwargs = dict(paper=r.get("paper"), tfont=r.get("tfont"), location=r.get("location", ""),
                          style=r.get("style", "default"), swatches=r.get("swatches", True),
                          tstyle=r.get("tstyle", "normal"))
            orient = (r.get("orient") or "auto")
            front = SVG.front_svg(href, pal, r["title"], dstr, ph_ratio=ratio, orient=orient, photo=art_src, **kwargs)
            # Only build the writing side when the ZIP will contain it: a back_svg failure must not
            # break a front-only download.
            back = None
            if r["want_back"]:
                back = SVG.back_svg(href, pal, r["title"], idx=r.get("number") or "N° 01", ph_ratio=ratio,
                                    location=r.get("location", ""), paper=r.get("paper"),
                                    tfont=r.get("tfont"), swatches=r.get("swatches", True))
            buf = _io.BytesIO()
            with _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as z:
                z.writestr("postcard-front.svg", front)
                if back is not None:
                    z.writestr("postcard-back.svg", back)
                if note:
                    z.writestr("ai_note.txt", note)    # why AI art was unavailable / what happened
            buf.seek(0)
            return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=stem + "-vector.svg.zip")
        if kind == "wallpaper":
            wpx = M.render_wallpaper(photo, r["palette"], r["title"], r["date_str"] if r["show_date"] else "",
                                     elements=r["elements"], extra=r["extra"], tfont=r["tfont"], enhance=r["enhance"],
                                     crop=r["crop"], number=r["number"], location=r["location"], paper=r["paper"])
            buf = _io.BytesIO(); wpx.save(buf, "PNG"); buf.seek(0)
            return send_file(buf, mimetype="image/png", as_attachment=True, download_name=stem + "-wallpaper-1080x1920.png")
        if kind in ("sheet-pdf", "sheet-pdf-back"):
            is_back = kind == "sheet-pdf-back"
            img = (render_front_for(r, photo, 240, options.get("desc","")) if not is_back
                   else render_back_for(r, photo, 240, options.get("desc","")))
            pdf = build_sheet_pdf(img, cols=2, rows=1, landscape=True)
            return send_file(_io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=stem + "-sheet-A4.pdf")
        if kind.startswith("front"):
            if kind == "front-hi":
                img = render_front_for(r, photo, 600, options.get("desc",""))
                buf = _io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
                return send_file(buf, mimetype="image/png", as_attachment=True, download_name=stem + "-front-600dpi.png")
            img = render_front_for(r, photo, 600, options.get("desc",""))
            pdf = build_print_pdf(img)
            return send_file(_io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=stem + "-front-print.pdf")
        else:
            if kind == "back-hi":
                img = render_back_for(r, photo, 600, options.get("desc",""))
                buf = _io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)
                return send_file(buf, mimetype="image/png", as_attachment=True, download_name=stem + "-back-600dpi.png")
            img = render_back_for(r, photo, 600, options.get("desc",""))
            pdf = build_print_pdf(img)
            return send_file(_io.BytesIO(pdf), mimetype="application/pdf", as_attachment=True, download_name=stem + "-back-print.pdf")
    except PhotoTooLarge:
        raise
    except Exception:
        import traceback; traceback.print_exc()
        return jsonify({"error": "导出失败，请稍后重试。"}), 500


@app.route("/bundle", methods=["POST"])
def bundle():
    """Package everything (hi-res PNGs, print PDFs, A4 sheet) into one ZIP."""
    import io as _io, zipfile
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "no photo"}), 400
    options = pick_options(request.form)
    try:
        photo = load_photo(f)
        r = resolve(photo, options)
        faces = render_faces(r, photo, 600, options.get("desc", ""),
                             ("front", "back") if r["want_back"] else ("front",))
        front = faces["front"]
        back = faces.get("back")
        def png_b(img):
            b = _io.BytesIO(); img.save(b, "PNG"); return b.getvalue()
        # Measure the card we actually drew instead of re-deriving it from card_dims(): the AI
        # pattern honours `orient`, the vector one is landscape by design, and the notes must not
        # claim 100x150mm for a file that is 150x100mm.
        _mm = 25.4 / 600.0
        _cw, _ch = round(front.size[0] * _mm), round(front.size[1] * _mm)
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            usage = (f"Postcard Studio · 明信片 使用说明\n"
                     f"================================\n"
                     f"本卡片：{r['title'] or '（未命名）'}\n"
                     f"编号：{r['number']}　|　{('日期：'+r['date_str']) if (r['show_date'] and r['date_str']) else ''}\n"
                     f"位置：{r['location'] or '—'}\n\n"
                     f"• 规格：{_cw:g}×{_ch:g}mm（含 3mm 出血 + 裁剪线）\n"
                     f"• 本包文件：\n"
                     f"   - front-600dpi.png  图案面(正面) 高清\n"
                     f"   - back-600dpi.png   写法面(背面) 高清\n"
                     f"   - front-print.pdf   打印版正面(出血+裁剪线)\n"
                     f"   - back-print.pdf    打印版背面(出血+裁剪线)\n"
                     f"   - front-sheet-A4.pdf A4 一张印2张拼版\n"
                     f"• 打印建议：300dpi 以上；专业胶印请转 CMYK；推荐 200g 以上铜版/哑粉纸。\n")
            z.writestr("使用说明.txt", usage)
            z.writestr("front-600dpi.png", png_b(front))
            if back is not None:
                z.writestr("back-600dpi.png", png_b(back))
                z.writestr("back-print.pdf", build_print_pdf(back))
            z.writestr("front-print.pdf", build_print_pdf(front))
            z.writestr("front-sheet-A4.pdf", build_sheet_pdf(front))
        buf.seek(0)
        return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=safe_name(r.get("title","")) + "-全套.zip")
    except PhotoTooLarge:
        raise
    except Exception:
        import traceback; traceback.print_exc()
        return jsonify({"error": "打包失败，请稍后重试。"}), 500


if __name__ == "__main__":
    # Production WSGI server (threaded), replaces the Flask dev server.
    # Bind loopback by default: the page accepts uploads and every render burns a core for seconds,
    # and cloudflared connects over 127.0.0.1 anyway. Set POSTCARD_HOST=0.0.0.0 to share it on the
    # local network (phone uploads), which knowingly exposes the service to every device on that LAN.
    from waitress import serve
    try:
        _left = clean_cache() // 1024
        print("cache/ 清理完成：保留 %dKB（14 天 TTL，总量上限 300MB）" % _left)
    except Exception as _e:
        print("cache cleanup skipped:", _e)
    host = _cfg("POSTCARD_HOST", "127.0.0.1")
    port = int(_cfg("POSTCARD_PORT", "5179") or 5179)
    threads = int(_cfg("POSTCARD_THREADS", "12") or 12)
    print("Serving on http://%s:%s via waitress (%d threads) ..." % (host, port, threads))
    serve(app, host=host, port=port, threads=threads)
