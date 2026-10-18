# -*- coding: utf-8 -*-
"""
photo-zine-postcard-v3.3 renderer — LANDSCAPE 3:2 (150x100mm) edition.

Two faces per card:
  * picture side  (背面): photo + hand-drawn motif + 3 swatches + metadata (zine style)
  * mail side     (正面): Chinese national-standard postcard layout (GB standard)
Layout rules from design-image-spec.md + Chinese postcard standard (YZ/T 0190).
"""
import math, random, os, threading, contextlib
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageEnhance, ImageOps, ImageChops

ROOT = os.path.dirname(os.path.abspath(__file__))     # every resource is resolved next to this file
WIN_FONTS = "C:/Windows/Fonts" if os.name == "nt" else ""


def _font_dirs():
    """Places to look for system fonts, so the renderer also runs off Windows.
    Override with POSTCARD_FONT_DIR if your CJK font lives elsewhere."""
    dirs = []
    env = os.environ.get("POSTCARD_FONT_DIR")
    if env:
        dirs.append(env)
    if os.name == "nt":
        dirs.append("C:/Windows/Fonts")
    dirs += [os.path.join(ROOT, "fonts"),
             "/usr/share/fonts/opentype/noto", "/usr/share/fonts/truetype/noto",
             "/usr/share/fonts/truetype", "/usr/share/fonts",
             os.path.expanduser("~/.fonts"),
             "/System/Library/Fonts", "/Library/Fonts"]
    return dirs


def _pick_font(*names):
    for d in _font_dirs():
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p.replace(os.sep, "/")
    return None


CJK_SONG = (_pick_font("simsun.ttc", "NotoSansCJK-Regular.ttc", "NotoSansSC-Regular.otf",
                       "PingFang.ttc", "MiSans-Regular.ttf")
            or (WIN_FONTS + "/simsun.ttc"))
CJK_HEI = _pick_font("simhei.ttf", "NotoSansCJK-Bold.ttc", "NotoSansSC-Bold.otf",
                     "PingFang.ttc") or (WIN_FONTS + "/simhei.ttf")
CJK_KAI = _pick_font("simkai.ttf", "NotoSerifCJK-Regular.ttc", "Songti.ttc") or (WIN_FONTS + "/simkai.ttf")
SANS = (_pick_font("arial.ttf", "DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc")
        or (WIN_FONTS + "/arial.ttf"))

DPI = 300
MM = DPI / 25.4
PX_PER_PT = DPI / 72.0        # typographic pt -> px at this DPI
CW, CH = 150.0, 100.0          # 3:2 landscape, mm
PXW, PXH = int(round(CW*MM)), int(round(CH*MM))

IVORY = (246, 241, 231)
INK   = (56, 58, 62)
POSTAL_RED = (179, 58, 58)
PAPER_SKINS = {
    'ivory': (246, 241, 231),  'white': (252, 252, 251), 'kraft': (230, 220, 204),
    'warm': (244, 236, 222),   'black': (32, 32, 34),     'blue': (238, 242, 247),
    'rose': (248, 238, 236),   'mint': (238, 244, 238),  'midnight': (20, 24, 40),
}


def ink_for(paper):
    """Pick a readable ink colour for the given paper (light ink on dark paper)."""
    if paper is None:
        return INK
    lum = 0.299*paper[0] + 0.587*paper[1] + 0.114*paper[2]
    return INK if lum >= 108 else (240, 242, 240)


def fade(ink, paper, f):
    """A faint rule/label colour derived from `ink`, blended `f` toward the paper tone.
    Works on light paper (ink fades to ivory) and on dark paper (ink fades to midnight)."""
    return _mix(ink, paper or IVORY, max(0.0, min(1.0, f)))


TFONTS = {'song': CJK_SONG, 'hei': CJK_HEI, 'kai': CJK_KAI, 'fang': WIN_FONTS + '/simfang.ttf',
          'mi': os.path.join(ROOT, 'fonts', 'MiSans-Regular.ttf').replace(os.sep, '/')}


def apply_filter(img, filt, dpi=300):
    """Whole-card photographic filters."""
    img = img.convert("RGB")
    if not filt or filt == "none":
        return img
    if filt == "bw":
        return ImageOps.grayscale(img).convert("RGB")
    if filt in ("warm", "cool", "sepia"):
        g = ImageOps.grayscale(img)
        if filt == "sepia":
            return Image.merge("RGB", (g.point(lambda v: min(255, int(v*1.07+15))),
                                       g.point(lambda v: min(255, int(v*0.90))),
                                       g.point(lambda v: min(255, int(v*0.55)))))
        img2 = ImageEnhance.Color(Image.merge("RGB", (g, g, g))).enhance(1.05)
        warm = Image.merge("RGB", (g.point(lambda v: min(255, int(v*1.10+12))),
                                   g.point(lambda v: min(255, int(v*0.96+6))),
                                   g.point(lambda v: min(255, int(v*0.86)))))
        cool = Image.merge("RGB", (g.point(lambda v: min(255, int(v*0.90))),
                                   g.point(lambda v: min(255, int(v*0.98+6))),
                                   g.point(lambda v: min(255, int(v*1.12+12)))))
        base = warm if filt == "warm" else cool
        return Image.blend(img2, base, 0.6)
    if filt == "grain":
        img2 = ImageEnhance.Contrast(img).enhance(1.08)
        noise = Image.effect_noise(img.size, 22).convert("L")
        return Image.blend(img2, Image.merge("RGB", (noise, noise, noise)).point(lambda v: int(120+(v-128)*0.12)), 0.35)
    return img


def cover_crop(img, w_px, h_px):
    """Scale to COVER a box and center-crop (no distortion), adaptive to source."""
    sw, sh = img.size
    scale = max(w_px/sw, h_px/sh)
    nw, nh = int(round(sw*scale)), int(round(sh*scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - w_px)//2); top = max(0, (nh - h_px)//2)
    return img.crop((left, top, left+w_px, top+h_px))

def crop_photo(img, crop):
    """Crop to a normalized region {x,y,w,h} (0-1). Pass None to keep full image."""
    if not crop or len(crop) != 4:
        return img
    x, y, w, h = [float(v) for v in crop]
    W, H = img.size
    left = int(x*W); top = int(y*H); right = int((x+w)*W); bot = int((y+h)*H)
    left = max(0, min(W-1, left)); top = max(0, min(H-1, top)); right = max(left+1, min(W, right)); bot = max(top+1, min(H, bot))
    if right-left < 8 or bot-top < 8:
        return img
    return img.crop((left, top, right, bot))

def enhance_photo(img):
    """Auto-improve the embedded photo: contrast/brightness/colour pop."""
    e = ImageEnhance.Contrast(img); img = e.enhance(1.12)
    e = ImageEnhance.Brightness(img); img = e.enhance(1.05)
    e = ImageEnhance.Color(img); img = e.enhance(1.08)
    return img

def apply_round(img, radius_mm, dpi, bg=IVORY):
    """Round the card corners, filled with the paper colour (clean everywhere, incl. JPEG/PDF)."""
    r = int(round(radius_mm * dpi / 25.4))
    mask = Image.new("L", img.size, 255)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0]-1, img.size[1]-1], radius=r, fill=255)
    rgba = Image.merge("RGBA", (*img.convert("RGB").split(), mask))
    out = Image.new("RGB", img.size, bg)
    out.paste(rgba, mask=rgba.split()[3])
    return out

def mm(v): return v * MM

def rnd(seed): return random.Random(seed)


# The renderers below swap the module-level mm/pt scale (MM, PXW, PXH, CW, CH ...).
# waitress serves several requests per thread, so every render runs inside this
# re-entrant lock + an explicit scope() to keep concurrent cards from corrupting each other.
RENDER_LOCK = threading.RLock()


@contextlib.contextmanager
def scope(dpi=300, cw=None, ch=None):
    """Temporarily set the global paper scale for one render, then restore it."""
    global MM, PX_PER_PT, CW, CH, PXW, PXH
    with RENDER_LOCK:
        old = (MM, PXW, PXH, PX_PER_PT, CW, CH)
        if cw and ch:
            CW, CH = float(cw), float(ch)
        MM = dpi / 25.4
        PXW, PXH = int(round(CW*MM)), int(round(CH*MM))
        PX_PER_PT = dpi / 72.0
        try:
            yield
        finally:
            MM, PXW, PXH, PX_PER_PT, CW, CH = old


def _load_font(path, px):
    """Load a truetype font, degrading gracefully when a bundled CJK font is absent."""
    for p in (path, CJK_SONG, CJK_HEI, SANS):
        if not p:
            continue
        try:
            return ImageFont.truetype(p, int(px))
        except Exception:
            continue
    return ImageFont.load_default()

def font(size_pt):
    px = size_pt * PX_PER_PT
    for p in (SANS,):
        try: return ImageFont.truetype(p, px)
        except Exception: pass
    return ImageFont.load_default()

def cfont(size_pt, path=CJK_SONG):
    return _load_font(path, size_pt * PX_PER_PT)

def letterspace(text, spacing):
    if not text: return ""
    sep = " " * max(1, int(round(spacing)))
    return sep.join(list(text))

def text(canvas, xy, s, fill, size_pt, spacing=0.0, anchor="la", path=None):
    d = ImageDraw.Draw(canvas)
    f = cfont(size_pt, path) if path else font(size_pt)
    d.text((xy[0]*MM, xy[1]*MM), letterspace(s, spacing), fill=fill, font=f, anchor=anchor)

def ctext(canvas, xy, s, fill, size_pt, path=CJK_SONG, anchor="la"):
    ImageDraw.Draw(canvas).text((xy[0]*MM, xy[1]*MM), s, fill=fill, font=cfont(size_pt, path), anchor=anchor)

def draw_meta(canvas, xy, s, fill, size_pt, tfont=None, tstyle="normal"):
    """Text with optional bold (stroke) and italic (shear)."""
    px = size_pt * PX_PER_PT
    f = cfont(size_pt, tfont) if tfont else font(size_pt)
    bold = "bold" in tstyle; ital = "italic" in tstyle
    sw = max(1, int(round(px*0.05))) if bold else 0
    x, y = xy[0]*MM, xy[1]*MM
    if not ital:
        ImageDraw.Draw(canvas).text((x, y), s, fill=fill, font=f, anchor="la", stroke_width=sw, stroke_fill=fill)
        return
    bbox = f.getbbox(s); pad = 12
    w = bbox[2]-bbox[0] + sw*4 + pad; h = bbox[3]-bbox[1] + sw*4 + pad
    tmp = Image.new("RGBA", (int(w + h*0.28 + 4), int(h+2)), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((pad/2 - bbox[0], pad/2 - bbox[1]), s, font=f, fill=fill, stroke_width=sw, stroke_fill=fill)
    tmp = tmp.transform((int(tmp.width + tmp.height*0.28), tmp.height), Image.AFFINE, (1, 0.28, 0, 0, 1, 0), resample=Image.BICUBIC)
    canvas.paste(tmp, (int(x), int(y)), tmp)


def paper_canvas(seed=1, paper=IVORY):
    img = Image.new("RGB", (PXW, PXH), paper)
    grad = Image.new("L", (PXW, PXH), 0)
    g = ImageDraw.Draw(grad)
    for y in range(0, PXH, 4):
        v = int(6 * math.sin(y / PXH * math.pi))
        g.line([(0, y), (PXW, y)], fill=v)
    grad = grad.filter(ImageFilter.GaussianBlur(mm(6)))
    img = Image.composite(img, Image.new("RGB", (PXW, PXH), (252, 248, 240)), grad.point(lambda a: 255 - a))
    noise = Image.effect_noise((PXW, PXH), 18).convert("L")
    noise = ImageEnhance.Contrast(noise).enhance(0.4)
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)).point(lambda v: int(240 + (v - 128) * 0.06)), 0.35)
    return img


def photo_box(canvas, photo, left, top, width_mm, max_h_mm, frame_gap_mm=2.0, frame_w_mm=0.5, frame=INK):
    d = ImageDraw.Draw(canvas)
    w = width_mm*MM
    ph = photo.size[1]/photo.size[0]
    h = w*ph
    if h > max_h_mm*MM:
        h = max_h_mm*MM; w = h/ph
    x, y = left*MM, top*MM
    canvas.paste(photo.resize((int(w), int(h)), Image.LANCZOS), (int(x), int(y)))
    gap, fw = frame_gap_mm*MM, frame_w_mm*MM
    d.rectangle([x-gap, y-gap, x+w+gap, y+h+gap], outline=frame, width=int(round(fw)))
    return (x, y, w, h)


def watercolor_ellipse(canvas, cx, cy, rx, ry, color, alpha, blur_mm=8, seed=2):
    """一团边缘化开的水彩色（云、日晕、黄昏的光晕）。cx/cy/rx/ry 是像素。

    两个坑：
    1) 形状必须画在 L 蒙版上、颜色另铺一层。原来是在 RGBA 层里画完再整层模糊，
       而 RGBA 层没画到的地方颜色是「透明的黑」，模糊把黑拽进颜色里，化开的那一圈
       就成了灰晕——太阳和云周围那圈脏灰色就是这么来的（与天空底板同一个根因）。
    2) 蒙版固定按 6px/mm 算好再放大，而不是跟着导出 DPI。否则 blur_mm=16 在 600dpi 下
       是半径 189 的整幅高斯（3543x2362），一次要一秒以上；而这是一团低频的晕，
       6px/mm 足够平滑，放大后看不出差别，反而保证预览和打印文件长得一样。
    """
    q = 6.0                                            # 蒙版分辨率：px / mm
    cxm, cym, rxm, rym = cx / MM, cy / MM, rx / MM, ry / MM
    pad = blur_mm * 1.8 + 2.0                          # 高斯核尾巴要落在框内，否则边缘会被切
    bx0 = max(0.0, cxm - rxm - pad); bx1 = min(PXW / MM, cxm + rxm + pad)
    by0 = max(0.0, cym - rym - pad); by1 = min(PXH / MM, cym + rym + pad)
    W = max(2, int(round((bx1 - bx0) * q))); H = max(2, int(round((by1 - by0) * q)))
    m = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(m)
    r = rnd(seed)
    for _ in range(6):
        jx = r.uniform(-rxm*0.06, rxm*0.06); jy = r.uniform(-rym*0.06, rym*0.06)
        md.ellipse([(cxm-rxm+jx-bx0)*q, (cym-rym+jy-by0)*q,
                    (cxm+rxm+jx-bx0)*q, (cym+rym+jy-by0)*q], fill=int(max(0, min(255, alpha))))
    m = m.filter(ImageFilter.GaussianBlur(max(1, int(blur_mm * q / 2))))
    out = (max(2, int((bx1 - bx0) * MM)), max(2, int((by1 - by0) * MM)))
    if m.size != out:
        m = m.resize(out, Image.BICUBIC)
    canvas.paste(Image.new("RGB", out, color), (int(bx0 * MM), int(by0 * MM)), m)

def contour_line(canvas, points, color, width_mm=0.55, seed=4, jitter_mm=0.06):
    d = ImageDraw.Draw(canvas); r = rnd(seed); dm = jitter_mm*MM
    pts = [(x*MM+r.uniform(-dm, dm), y*MM+r.uniform(-dm, dm)) for x, y in points]
    d.line(pts, fill=color, width=int(round(width_mm*MM)), joint="curve")

def fill_poly(canvas, points, color, alpha=255):
    lay = Image.new("RGBA", (PXW, PXH), (0, 0, 0, 0))
    ImageDraw.Draw(lay).polygon([(x*MM, y*MM) for x, y in points], fill=color+(alpha,))
    canvas.paste(lay, (0, 0), lay)


def band_fill(canvas, x0, x1, y0, y1, color, alpha=255, side_mm=6.0, bottom_mm=None):
    """平涂色带（海、地面）用羽化蒙版画，而不是直角多边形。

    fill_poly 的边界若落在卡片中间（海面色带 x 58..142 / y 48..76，四条边没有一条贴着裁切线），
    纸上就留下一张贴上去的纸片：左右两端与底边各是一道灰度台阶。这里把这三边化进纸里，
    顶边保持利落——地平线正是这条色带唯一该有的硬线。
    蒙版在「1px = 1mm」的小图上算好再放大，所以羽化宽度与导出 DPI 无关。
    """
    global PXW, PXH
    Wm, Hm = max(8, int(round(PXW / MM))), max(8, int(round(PXH / MM)))
    bot = max(1.0, (y1 - y0) * 0.28) if bottom_mm is None else max(1.0, bottom_mm)
    side = max(1.0, side_mm)
    data = []
    for y in range(Hm):
        for x in range(Wm):
            v = 0
            if x0 <= x <= x1 and y0 <= y <= y1:
                v = min(255, int(255 * min(x - x0, x1 - x) / side), int(255 * (y1 - y) / bot))
            data.append(max(0, v))
    lay = Image.new("L", (Wm, Hm), 0)
    lay.putdata(data)
    lay = lay.filter(ImageFilter.GaussianBlur(1.0)).resize((PXW, PXH), Image.BILINEAR)   # 小图上 1px=1mm
    if alpha < 255:
        lay = lay.point([int(v * alpha / 255) for v in range(256)])
    canvas.paste(Image.new("RGB", (PXW, PXH), color), (0, 0), lay)


def taper_line(canvas, pts, color, width_mm=0.4, alpha=255, taper_mm=6.0):
    """把一条近水平的折线画进蒙版，再按 x 在两端线性淡出。

    d.line 的端头是方头：一根 0.3mm 的细线在纸上就是两道硬切口，看着像尺子画的而不是笔。
    形状只占自己的外接矩形（不是整幅），所以高分辨率下也不贵；浪这类东西正好是水平的，
    用 x 当弧长的近似足够。
    """
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    pad = max(1.0, width_mm)
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad
    W, H = max(2, int((x1 - x0) * MM)), max(2, int((y1 - y0) * MM))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).line([((px - x0) * MM, (py - y0) * MM) for px, py in pts],
                           fill=alpha, width=max(1, int(round(width_mm * MM))), joint="curve")
    if taper_mm > 0:
        span = max(1e-6, x1 - x0)
        ramp = [int(255 * min(1.0, min(i * span / max(1, W - 1), span - i * span / max(1, W - 1)) / taper_mm))
                for i in range(W)]
        grad = Image.new("L", (W, H))
        grad.putdata([v for _ in range(H) for v in ramp])
        m = ImageChops.multiply(m, grad)
    canvas.paste(Image.new("RGB", (W, H), color), (int(x0 * MM), int(y0 * MM)), m)


def local_lum(canvas, cx, cy, r_px=0, paper=None):
    """某点附近的平均亮度（5 点采样），用来决定这块墨该往亮走还是往暗走。

    同一个图元不一定落在纸上：月亮可能被拖到夜蓝的天色上，星点可能压在近白的象牙纸上。
    写死一个「浅色」就必然在其中一种背景上完全消失（星空就是这么变成隐形元素的）。
    带旋转的元素会先渲到一张透明小图上，那里取不到真实背景，退回按纸色判断。
    """
    if canvas.mode == "RGBA":
        ref = paper or IVORY
        return 0.299 * ref[0] + 0.587 * ref[1] + 0.114 * ref[2]
    rgb = canvas.convert("RGB")
    m = max(1, int(r_px))
    vals = []
    for px, py in ((cx, cy), (cx - m, cy), (cx + m, cy), (cx, cy - m), (cx, cy + m)):
        px = min(max(px, 0), rgb.width - 1); py = min(max(py, 0), rgb.height - 1)
        rr, gg, bb = rgb.getpixel((px, py))
        vals.append(0.299 * rr + 0.587 * gg + 0.114 * bb)
    return sum(vals) / len(vals)


GROUND_CAP = 2.6        # 纵向最多放大到原画法的几倍（框拖得再高也不把一辆车拉成卡车）


def ground_line(rect, above, below):
    """把「以地平线为基准作画」的图元缩放进它自己的元素框，返回 (x0, x1, y_base, k)。

    路、车、招牌、火车、码头、集市这一批画法都拿一条地平线当锚，而且都有一部分墨是画在
    地平线**下面**的（路面 4.6mm、招牌 1.7mm、桥墩 4.5mm、车轮 0.45mm）。原先一律把地平线
    钉在框底边 y+h 上，于是这部分墨整块掉到框外：实测「街道」框内只有 0.4% 的点有变化、
    「招牌」框内 0.0%——框里是空的。用户在画布编辑器里拖动它只能带走半条马路，另外半条
    留在原地，甚至能拖出卡片外。现在按框高一起缩放纵向偏移，并贴住框底摆放。
    above / below 是该图元在原尺寸下画到地平线上方 / 下方的毫米数。
    """
    x, y, w, h = rect
    k = min(GROUND_CAP, h / max(1e-6, above + below))
    return x, x + w, y + h - below * k, k


def star_tone(canvas, rect, palette, paper=None):
    """星点/月亮的颜色：看它所在那块背景的明暗，暗背景给暖白，亮背景给深蓝墨。"""
    cx = int((rect[0] + rect[2] / 2) * MM); cy = int((rect[1] + rect[3] / 2) * MM)
    lum = local_lum(canvas, cx, cy, int(max(2.0, min(rect[2], rect[3]) * MM / 3)), paper)
    return (255, 250, 244) if lum < 150 else _mix(palette[1], (34, 40, 64), 0.4)


def darken(c, amt): return tuple(max(0, v-amt) for v in c)


def cloud_tone(palette, paper=None):
    """云/雾的颜色。不能直接用 palette[2]（照片最亮色）——雪山照里那几乎就是纸色，
    画上去等于没画。往暗部结构色上压一段固定距离，既看得见又仍是从这张照片里取的颜色。
    以前能“看见”是因为水彩色带那圈灰晕；晕一修掉，云就跟着没了。
    """
    def lum(c): return 0.299*c[0] + 0.587*c[1] + 0.114*c[2]
    ref = paper or IVORY
    if lum(ref) < 108:                                 # 深色纸：云要往亮里走
        return _mix(palette[2], (252, 252, 250), 0.55)
    f = 0.42
    t = _mix(palette[2], palette[1], f)
    gap = lum(ref) - lum(t)
    if gap < 55:                                       # 云是比天暗一团，30 级在纸上看不出来
        t = _mix(palette[2], palette[1], min(0.78, f + (55 - gap) / 160.0))
    return t


def sample_palette(photo):
    """Sample 3 source colours: dominant sky / dark structure / brightest accent.
    Scans a small thumbnail instead of the full-resolution pixels (~40x faster,
    and immune to single-pixel specular noise)."""
    thumb = photo.convert("RGB")
    thumb.thumbnail((192, 192), Image.BILINEAR)
    w, h = thumb.size
    sky = thumb.crop((0, 0, w, int(h*0.35))).resize((32, 32))
    body = thumb.crop((0, int(h*0.4), w, h)).resize((32, 32))
    def dom(im):
        q = im.convert("RGB").quantize(colors=5); pal = q.getpalette(); c = sorted(q.getcolors(), reverse=True)[0][1]
        return tuple(pal[c*3:c*3+3])
    def brightest(im):
        best=(0,0,0); bv=-1
        for c in im.convert("RGB").getdata():
            lum=0.299*c[0]+0.587*c[1]+0.114*c[2]
            if lum>bv and lum>150: bv=lum; best=c
        return best
    def darkest(im):
        best=(255,255,255); bv=999
        for c in im.convert("RGB").getdata():
            lum=0.299*c[0]+0.587*c[1]+0.114*c[2]
            if lum<bv: bv=lum; best=c
        return best
    return dom(sky), darkest(body), brightest(thumb)


def _lum(c): return 0.299*c[0]+0.587*c[1]+0.114*c[2]

def _mix(c, t, f): return tuple(int((1-f)*v + f*tt) for v, tt in zip(c, t))

def _desat(c, k):
    L = _lum(c); return tuple(int((1-k)*v + k*L) for v in c)

def _sat(c, f):
    L = _lum(c); return tuple(max(0, min(255, int(L + (v-L)*f))) for v in c)

def apply_style(palette, style):
    """Return (graded_palette, paper_rgb) for a selected art style."""
    pal = list(palette)
    paper = IVORY
    if style == "ink":            # 水墨
        pal = [_desat(c, 0.35) for c in pal]
        pal = [_mix(c, (95, 112, 138), 0.22) for c in pal]
        paper = (252, 252, 250)
    elif style == "vintage":      # 复古
        pal = [_mix(c, (178, 141, 95), 0.32) for c in pal]
        pal = [_desat(c, 0.15) for c in pal]
        paper = (237, 227, 209)
    elif style == "pastel":       # 治愈 / 淡彩
        pal = [_mix(c, (255, 255, 255), 0.45) for c in pal]
        paper = (250, 246, 243)
    elif style == "film":         # 胶片
        pal = [_sat(_mix(c, (255, 220, 180), 0.12), 1.12) for c in pal]
        pal = [_mix(c, (190, 210, 220), 0.10) for c in pal]
        paper = (246, 244, 240)
    elif style == "oil":          # 油画
        pal = [_sat(c, 1.25) for c in pal]
        paper = (246, 241, 231)
    elif style == "minimal":      # 极简
        pal = [_desat(c, 0.55) for c in pal]
        paper = (248, 246, 242)
    else:                         # default / hand-drawn
        paper = IVORY
    return pal, paper


def sknobs(style):
    """Style knobs that change HOW elements are drawn (element drawing method)."""
    s = style or "default"
    d = dict(body=235, line_w=0.55, line_x=10, cl=205, wa=70, draw_forest=True, draw_shadow=90)
    if s == "ink":          d = dict(body=120, line_w=0.8, line_x=-4, cl=140, wa=40, draw_forest=True, draw_shadow=120)
    elif s == "watercolor": d = dict(body=115, line_w=0.45, line_x=22, cl=220, wa=50, draw_forest=True, draw_shadow=60)
    elif s == "minimal":    d = dict(body=70,  line_w=0.28, line_x=48, cl=175, wa=40, draw_forest=False, draw_shadow=45)
    elif s == "pastel":     d = dict(body=165, line_w=0.42, line_x=34, cl=212, wa=52, draw_forest=True, draw_shadow=60)
    elif s == "oil":        d = dict(body=255, line_w=0.38, line_x=28, cl=188, wa=85, draw_forest=True, draw_shadow=120)
    elif s == "vintage":    d = dict(body=205, line_w=0.5,  line_x=20, cl=200, wa=66, draw_forest=True, draw_shadow=85)
    elif s == "film":       d = dict(body=212, line_w=0.5,  line_x=16, cl=200, wa=72, draw_forest=True, draw_shadow=90)
    return d

def ridge_motif(canvas, palette, origin=(96.0, 60.0), scale=1.0, style="default"):
    kn = sknobs(style)
    ox, oy = origin
    dark = palette[1]
    clt = cloud_tone(palette)
    body = tuple(int(dark[i]*0.35 + 246*0.65) for i in range(3))
    body = tuple(int(min(255, v+18)) for v in body)
    sil = [(0.0,19.5),(2.5,15.0),(5.5,16.5),(8.0,12.0),(10.5,14.0),(13.0,7.5),
           (15.5,10.5),(17.0,12.5),(19.8,3.5),(22.0,8.5),(24.5,10.5),(27.0,9.0),
           (29.5,12.0),(31.5,11.0),(34.0,13.5),(36.0,19.5)]
    sil = [(ox+x*scale, oy+y*scale) for x, y in sil]
    base = [(x, oy+22.5*scale) for x, _ in sil]
    fill_poly(canvas, sil+base[::-1], body, kn["body"])
    shadow = [(ox+19.8*scale,oy+3.5*scale),(ox+22.0*scale,oy+8.5*scale),(ox+24.5*scale,oy+10.5*scale),
              (ox+36*scale,oy+19.5*scale),(ox+36*scale,oy+22.5*scale),(ox+19.8*scale,oy+22.5*scale)]
    fill_poly(canvas, shadow, dark, kn["draw_shadow"])
    snow = [(ox+15.5*scale,oy+10.5*scale),(ox+17.0*scale,oy+12.5*scale),(ox+19.8*scale,oy+3.5*scale),(ox+19.8*scale,oy+13.0*scale)]
    fill_poly(canvas, snow, palette[2], 215 if kn["body"] > 150 else 150)
    contour_line(canvas, sil, darken(dark, kn["line_x"]), width_mm=kn["line_w"], seed=7, jitter_mm=0.08)
    for a, b in [((ox+8.0*scale,oy+12.0*scale),(ox+13.0*scale,oy+7.5*scale)),
                 ((ox+13.0*scale,oy+7.5*scale),(ox+19.8*scale,oy+3.5*scale)),
                 ((ox+22.0*scale,oy+8.5*scale),(ox+27.0*scale,oy+9.0*scale))]:
        contour_line(canvas, [a, b], darken(dark, kn["line_x"]+10), width_mm=kn["line_w"]*0.58, seed=8, jitter_mm=0.05)
    for (ccx,ccy,crx,cry,al) in [(ox+6.0*scale,oy+1.0*scale,4.6*scale,1.9*scale,kn["cl"]),
                                 (ox+17.5*scale,oy+0.2*scale,5.0*scale,2.1*scale,kn["cl"]+4),
                                 (ox+28.0*scale,oy+1.4*scale,4.2*scale,1.8*scale,kn["cl"]-2)]:
        watercolor_ellipse(canvas, ccx*MM, ccy*MM, crx*MM, cry*MM, clt, al, blur_mm=4.5, seed=int(ccx))
    watercolor_ellipse(canvas, (ox+31.5*scale)*MM, (oy+16.5*scale)*MM, 2.6*scale*MM, 1.1*scale*MM, clt, kn["cl"]-40, blur_mm=3, seed=9)


def read_date(photo):
    """Read captured date from EXIF (DateTimeOriginal then DateTime)."""
    ex = photo.getexif()
    raw = ex.get(36867) or ex.get(306)
    if not raw:
        return ""
    s = str(raw).strip()
    d = s.split(" ")[0].replace(":", "-")
    return d

def draw_pines(canvas, x0, x1, y_base, height, color, seed=22, count=0):
    """x0/x1 是这片林的边界，一排松树都收在这个区间里。

    树冠半宽是 h*0.62，比树间距（≈ 4.4mm）宽得多，排头尾的两棵必然会探出去：实测「树林」
    在框 (82..141) 里的墨从 76.5 一路画到 145.2，左右各出框 4~6mm——画布编辑器里拖动这个框，
    两头的树会留在原地。现在按最高的那棵树反推内缩量，整排林收进框里。
    """
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    n = count or max(4, int((x1-x0)/4.4))
    inset = min(height * 1.15 * 0.62, (x1-x0) * 0.45)      # 最高的那棵的树冠半宽；框很窄时别让内缩吃掉全部空地
    step = ((x1-x0) - 2*inset) / n
    for i in range(n):
        cx = x0 + inset + step*(i+0.5)
        h = height * r.uniform(0.7, 1.15)
        w = h*0.62
        d.rectangle([cx*MM-mm(0.4), (y_base-1.3)*MM, cx*MM+mm(0.4), y_base*MM], fill=color)
        for k, frac in enumerate([1.0, 0.72, 0.5]):
            tw = w*frac; th = h*0.5; basey = y_base - k*(h*0.3); topy = basey - th
            d.polygon([(cx*MM-tw*MM, basey*MM), (cx*MM, topy*MM), (cx*MM+tw*MM, basey*MM)], fill=color)

def draw_birds(canvas, pts, size=2.5, color=INK, seed=5):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    for (x, y) in pts:
        s = size*MM
        d.line([(x*MM-s, y*MM), (x*MM, y*MM-s*0.6), (x*MM+s, y*MM)], fill=color, width=int(round(0.4*MM)))

def draw_clouds(canvas, cx, cy, scale, color, seed=3):
    for (dx, dy, rx, ry, al) in [(-9, 0, 5.0, 2.1, 0.8), (0, -1.5, 6.0, 2.6, 0.82), (9, 0, 5.0, 2.1, 0.78)]:
        watercolor_ellipse(canvas, (cx+dx)*MM, (cy+dy)*MM, rx*scale*MM, ry*scale*MM, color, int(al*255), blur_mm=4.0, seed=seed+dx)

def draw_water(canvas, x0, x1, y, palette, seed=7):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    # water body
    fill_poly(canvas, [(x0, y), (x1, y), (x1, y+5.5), (x0, y+5.5)], palette[0], 85)
    # sun reflection column (warm glint) in the middle
    rx = x0 + (x1-x0)*0.52
    fill_poly(canvas, [(rx-0.9, y+0.2), (rx+0.9, y+0.2), (rx+0.4, y+5.2), (rx-0.4, y+5.2)], (255, 222, 165), 130)
    # ripples
    for k in range(3):
        pts = []
        for i in range(9):
            X = x0 + (x1-x0)*i/8
            Y = y + 1.2 + k*1.5 + r.uniform(-0.3, 0.3)
            pts.append((X, Y))
        contour_line(canvas, pts, darken(palette[0], 25), width_mm=0.3, seed=seed+k, jitter_mm=0.02)

def draw_sun(canvas, cx, cy, r, color):
    # soft halo + warm glow (sunset feel)
    watercolor_ellipse(canvas, cx*MM, cy*MM, r*2.2*MM, r*2.2*MM, (255, 224, 150), 80, blur_mm=9, seed=10)
    watercolor_ellipse(canvas, cx*MM, cy*MM, r*1.4*MM, r*1.4*MM, (255, 238, 200), 150, blur_mm=4, seed=11)
    d = ImageDraw.Draw(canvas)
    d.ellipse([cx*MM - r*0.85*MM, cy*MM - r*0.85*MM, cx*MM + r*0.85*MM, cy*MM + r*0.85*MM], fill=(255, 246, 222), outline=(235, 188, 118), width=int(round(0.4*MM)))

def draw_moon(canvas, cx, cy, r, color, paper=None):
    """月牙：一个圆盘，再用一个偏心圆从**蒙版**里挖掉一块。

    以前是用写死的纸色 (250,246,242) 再画一个圆盖上去：涂在象牙纸上没问题，可月亮一旦被
    拖到夜蓝的天色上、或导出的是深色/彩色纸卡，那块“咬口”就是一块颜色不对的补丁；
    元素带旋转时更糟——它是先画到一张透明小图上再贴回卡片的，那块补丁会直接挡住背后的天色。
    paper 参数保留给旧调用，已不再使用。
    """
    pad = max(0.5, r * 0.3)
    bx0 = max(0, int((cx - r - pad) * MM)); bx1 = min(PXW, int((cx + r + pad) * MM))
    by0 = max(0, int((cy - r - pad) * MM)); by1 = min(PXH, int((cy + r + pad) * MM))
    if bx1 - bx0 < 2 or by1 - by0 < 2:
        return
    m = Image.new("L", (bx1 - bx0, by1 - by0), 0)
    md = ImageDraw.Draw(m)
    md.ellipse([(cx - r) * MM - bx0, (cy - r) * MM - by0, (cx + r) * MM - bx0, (cy + r) * MM - by0], fill=255)
    md.ellipse([(cx - r * 0.55) * MM - bx0, (cy - r * 0.75) * MM - by0,
                (cx + r * 1.55) * MM - bx0, (cy + r * 0.85) * MM - by0], fill=0)
    canvas.paste(Image.new("RGB", (bx1 - bx0, by1 - by0), color), (bx0, by0), m)


def draw_stars(canvas, rect, palette, paper=None, count=0, seed=61):
    """把星点撒在自己的元素框里，颜色按框内那块背景的明暗来定。

    星空以前是两处写死的近白色点：自动布局分支里坐标硬编码在 x70..147 / y10..36（与元素框
    无关，拖动「星空」框什么也不会动），颜色 (250,246,240) 画在象牙纸上实测差值 2/255，
    等于没画；而画布编辑器那条路径根本没有 stars 分支，选了「星空」什么都不出现。
    """
    x, y, w, h = rect
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    col = star_tone(canvas, rect, palette, paper)
    lw = max(1, int(round(0.16 * MM)))
    n = count or max(10, int(w * h / 6.0))
    for _ in range(n):
        sx = x + r.uniform(0.03, 0.97) * w
        sy = y + r.uniform(0.06, 0.94) * h
        sz = 0.22 + r.random() * 0.5                       # mm
        d.ellipse([(sx - sz) * MM, (sy - sz) * MM, (sx + sz) * MM, (sy + sz) * MM], fill=col)
    for _ in range(max(2, n // 7)):                        # 几颗带十字光芒的亮星
        sz = 0.7 + r.random() * 0.7
        fx = min(0.4, sz * 2.6 / max(w, 1.0)); fy = min(0.4, sz * 2.6 / max(h, 1.0))
        sx = x + r.uniform(fx, 1 - fx) * w
        sy = y + r.uniform(fy, 1 - fy) * h
        d.line([(sx - sz * 2.4) * MM, sy * MM, (sx + sz * 2.4) * MM, sy * MM], fill=col, width=lw)
        d.line([sx * MM, (sy - sz * 2.4) * MM, sx * MM, (sy + sz * 2.4) * MM], fill=col, width=lw)

def draw_boat(canvas, cx, cy, scale, palette, paper):
    d = ImageDraw.Draw(canvas); s = scale
    # hull (dark)
    d.polygon([((cx-5*s)*MM, cy*MM), ((cx+5*s)*MM, cy*MM), ((cx+3*s)*MM, (cy+2.2*s)*MM), ((cx-3*s)*MM, (cy+2.2*s)*MM)], fill=darken(palette[1], -6))
    # mast
    d.line([(cx*MM, cy*MM), (cx*MM, (cy-7*s)*MM)], fill=darken(palette[1], 0), width=int(round(0.5*MM)))
    # sail (pale)
    d.polygon([(cx*MM, (cy-7*s)*MM), ((cx+4.5*s)*MM, (cy-0.5*s)*MM), (cx*MM, (cy-0.5*s)*MM)], fill=palette[2], outline=darken(palette[1],20))
    # water line
    contour_line(canvas, [(cx-8*s, cy+3.2*s), (cx+8*s, cy+3.2*s)], palette[0], width_mm=0.4, seed=13)

def draw_flowers(canvas, cx, cy, scale, palette):
    d = ImageDraw.Draw(canvas); s = scale; r = rnd(81)
    # a fuller bouquet: leaves + stems + petal flower heads
    # leaves at the base
    for dx in (-3.5, 0.0, 3.5):
        lx = (cx+dx*s)*MM; ly = (cy+2.6*s)*MM
        d.ellipse([lx-1.3*s*MM, ly-0.6*s*MM, lx+1.3*s*MM, ly+0.6*s*MM], fill=darken(palette[1], 20))
    # flowers
    heads = [(-3.2, 1.4, palette[0]), (0.0, 2.6, palette[1]), (3.2, 1.2, palette[2]), (-1.6, 0.6, palette[2])]
    for dx, lift, col in heads:
        cxx, cyy = (cx+dx*s), (cy - lift*s)
        d.line([cxx*MM, (cyy+3.2*s)*MM, cxx*MM, (cyy)*MM], fill=darken(palette[1], 35), width=int(round(0.3*MM)))
        # petals
        for a in range(6):
            ang = a*60 + (r.random()*20-10)
            import math as _m
            px = cxx + _m.cos(_m.radians(ang))*1.9*s
            py = cyy + _m.sin(_m.radians(ang))*1.9*s
            d.ellipse([(px-1.0*s)*MM, (py-1.0*s)*MM, (px+1.0*s)*MM, (py+1.0*s)*MM], fill=col)
        d.ellipse([cxx*MM-1.0*s*MM, cyy*MM-1.0*s*MM, cxx*MM+1.0*s*MM, cyy*MM+1.0*s*MM], fill=palette[2])

def draw_skyline(canvas, x0, x1, y_base, color, height=14.0, seed=30):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    # lighter backdrop silhouette (depth)
    x = x0
    while x < x1 - 1.5:
        w = min(6.0 + r.uniform(0, 4), x1 - x)            # 最后一栋不能探出右边界
        h = height*r.uniform(0.35, 0.85)
        d.rectangle([x*MM, (y_base-h)*MM, (x+w)*MM, y_base*MM], fill=_mix(color, (250, 246, 242), 0.55))
        x += w - 1
    # main buildings
    x = x0
    while x < x1 - 1.5:
        w = min(4.5 + r.uniform(0, 3.6), x1 - x)          # 同上：栋宽不能超元素框
        h = height * r.uniform(0.5, 1.15)
        d.rectangle([x*MM, (y_base-h)*MM, (x+w)*MM, y_base*MM], fill=color)
        # window grid
        wy = y_base - h*0.88
        while wy < y_base - h*0.1:
            wx = x + w*0.18
            while wx < x + w - 0.8:
                if r.random() > 0.42:
                    d.rectangle([wx*MM, wy*MM, (wx+0.9)*MM, (wy+1.0)*MM], fill=(236, 227, 209))
                wx += 1.5
            wy += 1.8
        # rooftop antenna / detail
        if r.random() > 0.62:
            d.line([(x+w/2)*MM, (y_base-h)*MM, (x+w/2)*MM, (y_base-h-1.3)*MM], fill=color, width=1)
        x += w + 0.8

def draw_people(canvas, x0, x1, y_base, color, count=0, seed=14):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    n = count or max(3, int((x1-x0)/7))
    step = (x1-x0)/n
    for i in range(n):
        cx = x0 + step*(i+0.4) + r.uniform(-0.6, 0.6)
        hh = 3.6 + r.uniform(0.0, 1.4)
        ww = hh*0.28
        d.ellipse([cx*MM-ww*MM, (y_base-hh)*MM, cx*MM+ww*MM, (y_base-hh+ww*2)*MM], fill=color)
        d.rectangle([cx*MM-ww*MM, (y_base-hh+ww*2)*MM, cx*MM+ww*MM, y_base*MM], fill=color)

STREET_TOP, STREET_BOT = 3.8, 4.6        # 灯头到路面底，相对地平线（路缘）的 mm


def draw_street(canvas, x0, x1, y_base, palette, k=1.0):
    """k：纵向缩放系数，由 ground_line() 按元素框高度算出来（1.0 就是原来的画法）。
    纵向所有偏移都乘 k，否则路面、斑马线、路灯底座全部画到 y+h 以下，掉到框外。"""
    d = ImageDraw.Draw(canvas)

    def ym(v): return y_base + v * k          # mm

    # sidewalk (light, above road)
    fill_poly(canvas, [(x0, ym(-1.2)), (x1, ym(-1.2)), (x1, ym(0.1)), (x0, ym(0.1))], palette[2], 130)
    # road body
    fill_poly(canvas, [(x0, ym(0)), (x1, ym(0)), (x1, ym(4.6)), (x0, ym(4.6))], darken(palette[1], 10), 165)
    # lane dashes
    for ix in range(int(x0), int(x1), 4):
        d.rectangle([ix*MM, ym(1.5)*MM, (ix+1.4)*MM, ym(2.0)*MM], fill=palette[2])
    # crosswalk stripes near centre
    cxm = (x0+x1)/2 - 4
    for c in range(5):
        d.rectangle([(cxm+c*2.0)*MM, ym(0)*MM, (cxm+c*2+1.1)*MM, ym(3.0)*MM], fill=palette[2])
    # streetlights
    pole = max(1, int(round(0.22 * k * MM)))
    for lx in (x0+2.5, x1-3):
        d.line([lx*MM, ym(-3.4)*MM, lx*MM, ym(0)*MM], fill=darken(palette[1],20), width=pole)
        d.line([lx*MM, ym(-3.4)*MM, (lx+1.5)*MM, ym(-3.4)*MM], fill=darken(palette[1],20), width=pole)
        d.ellipse([(lx+1.1)*MM, ym(-3.8)*MM, (lx+2.1)*MM, ym(-2.8)*MM], fill=(255, 222, 140))

CAR_TOP, CAR_BOT = 2.2, 0.45             # 车顶到轮底，相对地平线的 mm


def draw_car(canvas, x0, x1, y_base, color, seed=40, k=1.0):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    n = max(2, int((x1-x0)/11)); step = (x1-x0)/n
    rad = max(1, int(round(0.4 * k * MM)))

    def ym(v): return y_base + v * k          # mm

    for i in range(n):
        cx = x0 + step*(i+0.5) + r.uniform(-1, 1)
        cxx = cx*MM; yb = ym(0)*MM
        body = _mix(color, (235, 230, 221), 0.2)
        # wheels + hubcaps
        d.ellipse([cxx-2.15*MM, ym(-0.75)*MM, cxx-0.95*MM, ym(0.45)*MM], fill=darken(color, 30))
        d.ellipse([cxx+0.95*MM, ym(-0.75)*MM, cxx+2.15*MM, ym(0.45)*MM], fill=darken(color, 30))
        d.ellipse([cxx-1.8*MM, ym(-0.5)*MM, cxx-1.3*MM, ym(0.1)*MM], fill=(225, 219, 209))
        d.ellipse([cxx+1.3*MM, ym(-0.5)*MM, cxx+1.8*MM, ym(0.1)*MM], fill=(225, 219, 209))
        # lower body (hood/trunk)
        d.rounded_rectangle([cxx-2.3*MM, ym(-1.5)*MM, cxx+2.3*MM, ym(0)*MM], radius=rad, fill=body, outline=darken(body, 25))
        # cabin with roof
        d.rounded_rectangle([cxx-1.35*MM, ym(-2.2)*MM, cxx+1.6*MM, ym(-1.1)*MM], radius=rad, fill=_mix(color, (228, 224, 215), 0.12), outline=darken(color, 25))
        # glass windshield
        d.polygon([(cxx-1.1*MM, ym(-1.15)*MM), (cxx-0.45*MM, ym(-2.0)*MM), (cxx+0.75*MM, ym(-2.0)*MM), (cxx+1.3*MM, ym(-1.15)*MM)], fill=(176, 190, 205))


SIGN_TOP, SIGN_BOT = 0.0, 1.7


def draw_sign(canvas, x0, x1, y_base, color, seed=51, k=1.0):
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    x = x0

    def ym(v): return y_base + v * k          # mm

    while x < x1:
        w = min(6.0 + r.uniform(0, 3), x1 - x)          # 最后一块招牌不能探出框的右边界
        if w < 1.5:
            break
        d.rectangle([x*MM, ym(0)*MM, (x+w)*MM, ym(1.7)*MM], fill=color)
        d.rectangle([(x+0.5)*MM, ym(0.4)*MM, (x+w-0.5)*MM, ym(1.3)*MM], fill=_mix(color, (246, 240, 232), 0.45))
        x += w + 1.0

def draw_aurora(canvas, x0, x1, y0, y1, seed=70):
    r = rnd(seed)
    for band in range(3):
        pts = []
        for i in range(13):
            X = x0 + (x1-x0)*i/12
            Y = y0 + (y1-y0)*(0.25 + 0.22*band) + r.uniform(-1.5, 1.5) + 1.8*math.sin(i*0.9 + band*1.1)
            pts.append((X, Y))
        col = (86, 202, 176) if band % 2 == 0 else (108, 168, 224)
        fill_poly(canvas, pts + [(x1, y1), (x0, y1)], col, 55)
        contour_line(canvas, pts, darken(col, 18), width_mm=0.4, seed=seed+band, jitter_mm=0.02)

def firework_tone(canvas, rect, palette, paper=None):
    """烟花的墨色：夜空上给暖白，浅色纸上给饱和的暖红。

    三束烟花以前把颜色写死成 (250,235,205)/(255,214,150)：落在象牙纸上实测整片只有 28 级
    反差，看着就是一片脏纸，根本看不出是烟花——和「星空」在亮纸上隐形是同一个毛病。
    """
    cx = int((rect[0] + rect[2] / 2) * MM); cy = int((rect[1] + rect[3] / 2) * MM)
    lum = local_lum(canvas, cx, cy, int(max(2.0, min(rect[2], rect[3]) * MM / 3)), paper)
    if lum < 150:
        return (252, 236, 200)
    return _mix(palette[0], (198, 62, 48), 0.55)


def draw_fireworks(canvas, cx, cy, r, palette, seed=80, spread=None, color=None):
    """spread：三束爆点在框内散布的半径 (横, 纵) mm；color：按背景明暗选的墨色。

    以前散布是固定 ± 7mm / ± 4mm，与元素框大小无关：拖动或缩放「烟花」框只能改变爆点的
    半径，三束的位置钉在卡片中央不动。现在散布和半径都从框尺寸反推，且保证 散布+半径 ≤ 框半径。
    """
    d = ImageDraw.Draw(canvas); rr = rnd(seed)
    sx, sy = spread or (7.0, 4.0)
    for burst in range(3):
        bx = cx + rr.uniform(-sx, sx); by = cy + rr.uniform(-sy, sy)
        br = r*rr.uniform(0.8, 1.3)
        col = color or ((250, 235, 205) if burst % 2 == 0 else (255, 214, 150))
        for a in range(14):
            ang = rr.random()*6.28
            ex = bx + math.cos(ang)*br; ey = by + math.sin(ang)*br
            d.line([bx*MM, by*MM, ex*MM, ey*MM], fill=col, width=max(1, int(round(0.3*MM))))
        d.ellipse([bx*MM-0.5*MM, by*MM-0.5*MM, bx*MM+0.5*MM, by*MM+0.5*MM], fill=col)


TRAIN_TOP, TRAIN_BOT = 2.3, 2.0          # 车顶到钢轨线底；钢轨画在 ym(1.5) 上还有一笔宽，多留 0.5


def draw_train(canvas, x0, x1, y_base, color, seed=90, k=1.0):
    d = ImageDraw.Draw(canvas)

    def ym(v): return y_base + v * k          # mm

    d.line([x0*MM, ym(1.5)*MM, x1*MM, ym(1.5)*MM], fill=darken(color, 12), width=int(round(0.4*k*MM)))
    cx = x0 + 2
    for kk in range(3):
        w = 9.0 if kk == 0 else 7.0
        d.rounded_rectangle([cx*MM, ym(-2.3)*MM, (cx+w)*MM, ym(0)*MM], radius=int(round(0.6*k*MM)), fill=color, outline=darken(color, 25))
        for wx in range(int(cx+1), int(cx+w-1), 2):
            d.rectangle([wx*MM, ym(-1.6)*MM, (wx+1.0)*MM, ym(-0.9)*MM], fill=(236, 230, 220))
        if kk == 0:
            d.ellipse([(cx+w)*MM-1.0*MM, ym(-1.9)*MM, (cx+w)*MM, ym(-0.9)*MM], fill=(255, 230, 150))
        cx += w + 0.7
    for wx in range(int(x0+2), int(x1-1), 4):
        d.ellipse([wx*MM, ym(0)*MM, (wx+1.2)*MM, ym(1.0)*MM], fill=darken(color, 30))


def draw_field(canvas, x0, x1, y, palette, seed=60, depth=9.0):
    """depth：草地带的高度。以前写死 9mm，而「草地」在编辑器里的框高 30mm：
    拖大框只有顶上 9mm 有东西，缩小框（或把框拖到靠近卡底）则一路出框。现在按框高铺满。"""
    d = ImageDraw.Draw(canvas); r = rnd(seed)
    s = max(2.0, depth) / 9.0
    bot = y + 9.0 * s
    grass = (94, 150, 88)
    fill_poly(canvas, [(x0, y), (x1, y), (x1, bot), (x0, bot)], grass, 92)
    fill_poly(canvas, [(x0, y + 4.5*s), (x1, y + 4.5*s), (x1, bot), (x0, bot)], darken(grass, 18), 105)
    for i in range(7):
        ix = x0 + (x1-x0)*i/6
        contour_line(canvas, [(ix, y), (ix, bot)], darken(grass, 30), width_mm=0.16, seed=seed+i)
    for _ in range(14):
        tx = r.uniform(x0, x1); ty = y + r.uniform(1, 8) * s
        d.line([tx*MM, ty*MM, (tx+0.4)*MM, (ty+0.5*s)*MM], fill=darken(grass, 40), width=1)

def draw_goal(canvas, cx, cy, scale, palette, seed=70):
    d = ImageDraw.Draw(canvas); s = scale
    w, h = 8*s, 4*s
    for dx in (-w, w):
        d.line([(cx+dx)*MM, cy*MM, (cx+dx)*MM, (cy-h)*MM], fill=(248, 248, 248), width=int(round(0.5*MM)))
    d.line([(cx-w)*MM, (cy-h)*MM, (cx+w)*MM, (cy-h)*MM], fill=(248, 248, 248), width=int(round(0.5*MM)))
    for k in range(6):
        xx = (cx-w) + 2.7*k*s
        d.line([xx*MM, (cy-h)*MM, (xx+0.05)*MM, cy*MM], fill=(205, 205, 205), width=1)

def draw_tent(canvas, cx, cy, scale, palette, seed=100):
    d = ImageDraw.Draw(canvas); s = scale
    w, h = 6*s, 4*s
    d.polygon([(cx*MM, (cy-h)*MM), ((cx-w)*MM, cy*MM), ((cx+w)*MM, cy*MM)], fill=darken(palette[1], -6), outline=darken(palette[1], 20))
    d.polygon([(cx*MM, (cy-h)*MM), ((cx-w*0.15)*MM, cy*MM), ((cx+w*0.2)*MM, cy*MM)], fill=palette[2])
    d.line([(cx-w)*MM, cy*MM, (cx-w-1.4)*MM, cy*MM], fill=darken(palette[1], 30), width=1)
    d.line([(cx+w)*MM, cy*MM, (cx+w+1.4)*MM, cy*MM], fill=darken(palette[1], 30), width=1)

MARKET_TOP, MARKET_BOT = 3.4, 0.0


def draw_market(canvas, x0, x1, y_base, palette, seed=110, k=1.0):
    d = ImageDraw.Draw(canvas); r = rnd(seed)

    def ym(v): return y_base + v * k          # mm

    x = x0
    while x < x1:
        w = min(7.0, x1 - x)                  # 最后一个摊位的雨棚不能探出框（实测挤出右边界 4.9mm）
        if w < 2.5:
            break
        d.rectangle([x*MM, ym(-3.4)*MM, (x+w)*MM, ym(-2.7)*MM], fill=(228, 122, 110))
        d.rectangle([x*MM, ym(-2.7)*MM, (x+w)*MM, ym(0)*MM], fill=palette[2])
        for gx in range(int(x+0.7), int(x+w-0.6), 1):
            d.rectangle([gx*MM, ym(-2.3)*MM, (gx+0.7)*MM, ym(-0.7)*MM], fill=palette[0])
        x += w + 1.2


PIER_TOP, PIER_BOT = 0.0, 4.5


def draw_pier(canvas, x0, x1, y_base, palette, seed=130, k=1.0):
    d = ImageDraw.Draw(canvas)

    def ym(v): return y_base + v * k          # mm

    d.rectangle([x0*MM, ym(0)*MM, x1*MM, ym(1.6)*MM], fill=darken(palette[1], 10))
    for px in range(int(x0), int(x1), 3):
        d.rectangle([px*MM, ym(1.6)*MM, (px+0.5)*MM, ym(4.5)*MM], fill=darken(palette[1], 25))

def _mix3(a, b, t):
    return tuple(int(a[i]+(b[i]-a[i])*t) for i in range(3))

SUN_WORDS = ("sun", "sunset", "sunrise", "moon", "日", "日落", "dusk", "dawn", "黄昏", "暮")
NIGHT_WORDS = ("night", "evening", "dusk", "夜", "aurora", "stars", "star", "星")


def sky_mood(elements, extra=()):
    """底板氛围只看元素名。两条构图路径（有序布局 / 自动布局）必须用同一个判断，
    否则同一张卡“有没有进过画布编辑器”会渲染出两种天色。"""
    els = {str(e).strip().lower() for e in list(elements) + list(extra) if e}
    return (any(w in e for e in els for w in SUN_WORDS),
            any(w in e for e in els for w in NIGHT_WORDS))


def draw_sky_grad(canvas, palette, has_sun=False, has_night=False):
    """Soft atmospheric sky gradient across the motif area (design-forward).

    形状与羽化都在「1px = 1mm」的小图上算好再放大。以前直接在高斯模糊里写死 6 像素，
    而像素数随 DPI 变：210dpi 预览里 6px = 0.72mm 看着还算软，600dpi 打印文件里同样 6px
    只剩 0.25mm，矩形四条边就直接印到纸上（实测台阶从 55 级/mm 涨到 130 级/mm）。
    颜色也要铺满整幅：蒙版之外若颜色是透明的黑，羽化出来就是一圈发灰的脏边，正是原来的样子。
    """
    global PXW, PXH
    if has_night:
        top, hor = (22, 30, 58), (110, 134, 172)
    elif has_sun:
        top, hor = darken(palette[0], 20), (255, 208, 148)
    else:
        top, hor = darken(palette[0], 6), palette[2]
    Wm, Hm = max(8, int(round(PXW / MM))), max(8, int(round(PXH / MM)))     # 画布毫米尺寸
    # 底板盖住 motif 区：150x100 卡上就是 x 58..148 / y 8..42mm
    x0, x1 = Wm * 0.387, Wm * 0.987
    y0, y1 = Hm * 0.08, Hm * 0.42
    lay = Image.new("RGBA", (Wm, Hm), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    span = max(1e-6, y1 - y0)
    for y in range(Hm):
        t = min(1.0, max(0.0, (y - y0) / span))
        ld.line([(0, y), (Wm, y)], fill=_mix3(top, hor, t) + (int(210 * (1 - t) + 25),))
    feather = max(3.0, Wm * 0.045)                                          # mm；在小图上 1px=1mm
    mask = Image.new("L", (Wm, Hm), 0)
    ImageDraw.Draw(mask).ellipse([x0 - feather, y0 - feather, x1 + feather, y1 + feather], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(feather * 0.55))
    rr, gg, bb, aa = lay.split()
    lay = Image.merge("RGBA", (rr, gg, bb, ImageChops.multiply(aa, mask))).resize((PXW, PXH), Image.BICUBIC)
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), lay).convert("RGB"), (0, 0))


def draw_vignette(canvas):
    """四角极轻压暗。以前写成 Image.radial_gradient("L", max(PXW, PXH))——这个函数只收一个参数，
    于是每次渲染都抛 TypeError，又被紧跟着的 except Exception: pass 吞掉：这层暗角从来没有画出来过。
    现在按画布比例拉伸那张 256x256 渐变，并且不再静默吞异常。"""
    global PXW, PXH
    r = Image.radial_gradient("L").resize((PXW, PXH), Image.BILINEAR)
    lay = r.point(lambda v: int((v/255.0)**2 * 34))
    dark = Image.new("RGBA", (PXW, PXH), (0, 0, 0, 0)); dark.putalpha(lay)
    canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), dark).convert("RGB"), (0, 0))


def draw_backdrop(canvas, palette, elements, extra=()):
    """天色 + 日落暖光 + 暗角：三条都是“底板”，两条构图路径必须共用同一个入口。

    以前天色与暗角只在自动布局分支里画，日落那团暖光也是，于是用户一点开「画布编辑」
    （前端从此下发有序布局），整块背景就没了，同输入两种输出。
    暖光的位置用固定坐标而不是 rect('sunset', …)：'sunset' 不是一个可拖动的图元 id，
    有序布局里永远取不到它，写在那儿只会造成两条路径分叉。
    """
    has_sun, has_night = sky_mood(elements, extra)
    draw_sky_grad(canvas, palette, has_sun, has_night)
    if has_sun and not has_night:
        # 旧版用 palette[2]（照片最亮色）画“暖光”，那在雪山照里就是一团与纸同色的东西；
        # 能看到是因为 RGBA 黑底光晕把它印成了灰色。晕一修掉就得真的给一个暖色。
        watercolor_ellipse(canvas, 104.0*MM, 33.0*MM, 21.84*MM, 15.0*MM,
                           _mix(palette[2], (252, 178, 104), 0.62), 70, blur_mm=16, seed=5)
        watercolor_ellipse(canvas, 104.0*MM, 45.0*MM, 26.0*MM, 8.4*MM,
                           palette[0], 40, blur_mm=12, seed=8)
    draw_vignette(canvas)

def render_image(photo, palette, paper, elements, title, date_str="", style="default", swatches=True, dpi=300, extra=(), layout=None, tfont=None, rounded=False, radius_mm=6.0, filt="none", number="N° 01", location="", tstyle="normal", enhance=False, crop=None):
    """Render the pattern side at a given DPI (true high-res, not stretched)."""
    with scope(dpi):
        canvas = paper_canvas(1, paper)
        ph = crop_photo(enhance_photo(photo) if enhance else photo, crop) if crop else (enhance_photo(photo) if enhance else photo)
        img = draw_composed(canvas, ph, palette, elements, title, date_str, swatches=swatches, style=style, extra=extra, layout=layout, tfont=tfont, number=number, location=location, tstyle=tstyle, paper=paper)
        img = apply_filter(finalize(img), filt, dpi)
        return apply_round(img, radius_mm, dpi, paper) if rounded else img

def render_mail(photo, palette, paper, number="N° 01", title="", dpi=300, rounded=False, radius_mm=6.0, filt="none", location="", enhance=False, crop=None):
    with scope(dpi):
        canvas = paper_canvas(2, paper)
        ph = crop_photo(enhance_photo(photo) if enhance else photo, crop) if crop else (enhance_photo(photo) if enhance else photo)
        img = draw_mail(canvas, palette, idx=number, title=title, photo=ph, location=location, paper=paper)
        img = apply_filter(finalize(img), filt, dpi)
        return apply_round(img, radius_mm, dpi, paper) if rounded else img


def render_wallpaper(photo, palette, title="", date_str="", elements=(), extra=(), tfont=None, enhance=False, crop=None, number="", location="", paper=None):
    """9:16 portrait phone wallpaper: full-bleed photo fading into an ivory base,
    with a subtle hand-drawn motif accent (stars/moon/sun/aurora) and title metadata."""
    W, H = 1080, 1920
    ph = crop_photo(photo, crop) if crop else photo
    if enhance:
        ph = enhance_photo(ph)
    base = cover_crop(ph, W, H).convert("RGB")
    base = ImageEnhance.Brightness(base).enhance(1.03)
    paper = paper or IVORY
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, min(1.0, (y/H - 0.42) / 0.5))
        grad.putpixel((0, y), int(t*235))
    grad = grad.resize((W, H))
    base = Image.composite(Image.new("RGB", (W, H), paper), base, grad)
    d = ImageDraw.Draw(base)
    # ---- hand-drawn motif accent (top) from recognised elements ----
    els = set(elements) | set(extra)
    if ("stars" in els or "night" in els):
        r = rnd(201)
        for _ in range(26):
            sx = r.uniform(0.04, 0.96)*W; sy = r.uniform(0.03, 0.28)*H; sz = r.uniform(2, 5)
            d.ellipse([sx, sy, sx+sz, sy+sz], fill=(255, 250, 244))
    if ("moon" in els or "night" in els):
        mx, my, mr = W*0.82, H*0.13, 66
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=(246, 245, 239))
        d.ellipse([mx-mr*1.4, my-mr*0.5, mx+mr*0.9, my+mr*1.4], fill=(128, 132, 142))
    elif ("sun" in els or "sunset" in els or "sunrise" in els):
        mx, my, mr = W*0.82, H*0.13, 60
        d.ellipse([mx-mr*2, my-mr*2, mx+mr*2, my+mr*2], fill=(255, 226, 152))
        d.ellipse([mx-mr, my-mr, mx+mr, my+mr], fill=(255, 247, 223))
    if ("aurora" in els):
        for band in range(3):
            pts = []
            for i in range(15):
                X = W*0.05 + (W*0.9)*i/14
                Y = H*(0.05 + 0.06*band) + math.sin(i*0.8 + band)*30 + (-12, 8, 18)[band]
                pts.append((X, Y))
            d.line(pts, fill=(86, 202, 176) if band % 2 == 0 else (108, 168, 224), width=12)
    font_title = _load_font(tfont or CJK_SONG, 78)
    font_sub = _load_font(SANS, 30)
    font_small = _load_font(tfont or CJK_SONG, 30)
    ink = ink_for(paper)
    x = 92
    if title:
        d.text((x, H-392), title, font=font_title, fill=ink, anchor="ls")
    d.text((x, H-252), "- PHOTO · POSTCARD -", font=font_sub, fill=fade(ink, paper, .35), anchor="ls")
    info = " · ".join([s for s in [date_str, number, location] if s])
    if info:
        d.text((x, H-188), info, font=font_small, fill=fade(ink, paper, .45), anchor="ls")
    for i, col in enumerate(palette[:3]):
        cx = x + i*54
        d.ellipse([cx, H-120, cx+40, H-80], fill=col, outline=darken(col, 30), width=2)
    return base


def _glass_panel(canvas, x_mm, y_mm, w_mm, h_mm, radius_mm=2.6, alpha=130, blur=0.6):
    """Liquid-glass (glassmorphism) panel: blur the backdrop behind it + translucent white
    fill + top highlight + hairline border. alpha=white opacity(0-255), blur=px stdDev."""
    px0, py0 = int(x_mm*MM), int(y_mm*MM)
    pw, ph = int(w_mm*MM), int(h_mm*MM)
    region = canvas.crop((px0, py0, px0+pw, py0+ph)).convert("RGBA")
    blurred = region.filter(ImageFilter.GaussianBlur(blur)) if blur > 0.05 else region
    lay = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lay)
    rad = int(radius_mm*MM)
    ld.rounded_rectangle([0, 0, pw-1, ph-1], radius=rad, fill=(255, 255, 255, alpha))
    for yy in range(ph):
        a = int(58 * max(0.0, 1 - yy/(ph*0.5)))
        ld.line([(0, yy), (pw, yy)], fill=(255, 255, 255, max(0, a - int(alpha*0.2))))
    ld.rounded_rectangle([0, 0, pw-1, ph-1], radius=rad, outline=(255, 255, 255, 210), width=1)
    canvas.paste(Image.alpha_composite(blurred, lay).convert("RGB"), (px0, py0))


def draw_t2i_pattern(canvas, t2i_img, photo, palette, title, date_str="", tfont=None, tstyle="normal", number="", location="", glass_a=130, glass_b=0.6, paper=None):
    """Compose the AI (img2img) card: the stylized photo is the full backdrop + metadata."""
    d = ImageDraw.Draw(canvas)
    paper = paper or IVORY
    bg = cover_crop(t2i_img.convert("RGB"), canvas.size[0], canvas.size[1])
    canvas.paste(bg, (0, 0))
    W, H = canvas.size
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        grad.putpixel((0, y), int(max(0.0, min(1.0, (y/H - 0.44)/0.42))*250))
    canvas.paste(Image.composite(Image.new("RGB", (W, H), paper), canvas, grad.resize((W, H))), (0, 0))
    ink = ink_for(paper); mx = 10.0
    Hmm = canvas.size[1]/MM   # canvas height in mm (portrait 150 / landscape 100)
    my = Hmm - 20.0
    # liquid-glass title panel, width adaptive to the content length
    t = title if title.strip() else "Untitled"
    n = len(t)
    sz = 13.0 if n <= 10 else (11.0 if n <= 20 else (9.0 if n <= 30 else 7.5))
    def _w(s, size_pt, path=None):
        f = (cfont(size_pt, path) if path else font(size_pt))
        return f.getlength(s) / MM
    title_w = _w(t, sz, tfont)
    date_w = _w("DATE · " + date_str, 5.4, CJK_SONG) if date_str else 0
    loc_w = _w(location, 5.0, CJK_SONG) if location else 0
    sw_w = 3*5.8 + 3.0
    panel_w = max(46.0, min(88.0, max(title_w, date_w, loc_w, sw_w) + 7))
    _glass_panel(canvas, 6.0, Hmm - 27.0, panel_w, 20.0, radius_mm=3.0, alpha=glass_a, blur=glass_b)
    draw_meta(canvas, (mx, my), t, ink, sz, tfont, tstyle)
    yb = my + sz*0.52 + 0.5
    if date_str:
        text(canvas, (mx, yb), "DATE · " + date_str, fade(ink, paper, .45), 5.4, spacing=0.8); yb += 5.6
    if location:
        text(canvas, (mx, yb), location, fade(ink, paper, .25), 5.0, spacing=0.6, path=CJK_SONG); yb += 5.6
    sy = yb + 1.4
    for i, col in enumerate(palette):
        c = (mx + i*5.8)*MM
        d.ellipse([c, sy*MM, c+mm(3.0), (sy+3.0)*MM], fill=col, outline=darken(col, 40), width=1)
    # a slim original-photo badge (top-left) as a subtle reference
    pw, phh = 30.0, 30.0*photo.size[1]/photo.size[0]
    if phh > 40.0: phh = 40.0; pw = phh*photo.size[0]/photo.size[1]
    ax, ay = 9.0, 9.0
    canvas.paste(cover_crop(photo, int(pw*MM), int(phh*MM)), (int(ax*MM), int(ay*MM)))
    d.rectangle([(ax-1.5)*MM, (ay-1.5)*MM, (ax+pw+1.5)*MM, (ay+phh+1.5)*MM], outline=ink, width=int(round(0.4*MM)))
    return canvas


def card_dims(photo_ar, orient="auto"):
    """Front canvas dims (mm). photo_ar = height/width (portrait > 1). Portrait follows a portrait
    photo; 'portrait' forces vertical (100x150), 'landscape' forces horizontal (150x100)."""
    if orient == "portrait" or (orient == "auto" and photo_ar > 1.0):
        return 100.0, 150.0   # vertical 2:3
    return 150.0, 100.0


def render_t2i(photo, palette, t2i_img, title, date_str="", dpi=300, tfont=None, tstyle="normal", number="", location="", rounded=False, radius_mm=6.0, filt="none", paper=(246, 241, 231), glass_a=130, glass_b=0.6, orient="auto"):
    cw, ch = card_dims(photo.size[1]/photo.size[0], orient)
    with scope(dpi, cw, ch):
        canvas = Image.new("RGB", (PXW, PXH), paper)
        img = draw_t2i_pattern(canvas, t2i_img, photo, palette, title, date_str, tfont, tstyle, number, location, glass_a, glass_b, paper)
        img = apply_filter(finalize(img), filt, dpi)
        return apply_round(img, radius_mm, dpi, paper) if rounded else img


def draw_t2i_mail(canvas, t2i_img, palette, idx="", title="", photo=None, location="", tfont=None, paper=None):
    """Writing side with a low-opacity AI backdrop (never hurts address readability)."""
    d = ImageDraw.Draw(canvas)
    paper = paper or IVORY
    W, H = canvas.size
    bg = cover_crop(t2i_img.convert("RGB"), W, H)
    canvas.paste(Image.blend(Image.new("RGB", (W, H), paper), bg, 0.20), (0, 0))
    ink = ink_for(paper)
    b = 6.0
    d.rectangle([b*MM, b*MM, (CW-b)*MM, (CH-b)*MM], outline=fade(ink, paper, .50), width=int(round(0.4*MM)))
    # photo panel (left) + title BELOW it (so the title isn't too high)
    if photo is not None:
        # preserve the photo's own aspect ratio (no forced 26x32 crop)
        pw = 26.0
        phh = pw*photo.size[1]/photo.size[0]
        if phh > 34.0: phh = 34.0; pw = phh*photo.size[0]/photo.size[1]
        ax, ay = 11.0, 40.0
        canvas.paste(cover_crop(photo, int(pw*MM), int(phh*MM)), (int(ax*MM), int(ay*MM)))
        d.rectangle([ax*MM, ay*MM, (ax+pw)*MM, (ay+phh)*MM], outline=fade(ink, paper, .55), width=int(round(0.5*MM)))
        ty = ay + phh + 3.0
        n = len(title)
        sz = 8.0 if n <= 10 else (6.5 if n <= 18 else 5.2)
        sp = 1.0 if n <= 10 else 0.5
        if title:
            # use the selected title font (or CJK) so Chinese renders without garbling
            text(canvas, (ax, ty), title, ink, sz, spacing=sp, path=(tfont or CJK_SONG))
        yy = ty + 4.6
        if location:
            text(canvas, (ax, yy), location, fade(ink, paper, .35), 4.4, spacing=0.5, path=CJK_SONG); yy += 4.6
        for i, col in enumerate(palette):
            c = (ax + i*4.8)*MM
            d.ellipse([c, yy*MM, c+mm(2.4), (yy+2.4)*MM], fill=col, outline=darken(col, 40), width=1)
    bx, by, sq, gap = 10.0, 10.5, 9.0, 1.6
    for i in range(6):
        x0 = (bx + i*(sq+gap))*MM
        d.rectangle([x0, by*MM, x0+sq*MM, (by+sq)*MM], outline=POSTAL_RED, width=int(round(0.6*MM)))
    sx, sy, sw, sh = 124.0, 6.0, 20.0, 20.0
    d.rectangle([sx*MM, sy*MM, (sx+sw)*MM, (sy+sh)*MM], outline=fade(ink, paper, .45), width=int(round(0.5*MM)))
    ctext(canvas, (sx+2.5, sy+8.5), "贴邮票处", fade(ink, paper, .30), 7, path=CJK_KAI)
    text(canvas, (sx+13.5, sy+1.0), "80", POSTAL_RED, 5, spacing=0.4)
    for i in range(4):
        yy = (40+i*10)*MM
        d.line([(68*MM, yy), (144*MM, yy)], fill=fade(ink, paper, .68), width=1)
    d.rectangle([48*MM, 76*MM, 88*MM, 94*MM], outline=fade(ink, paper, .62), width=1)
    ctext(canvas, (50, 82.0), "条码区", fade(ink, paper, .45), 4.5)
    ctext(canvas, (108, 84), "邮政编码：", fade(ink, paper, .30), 6.5, path=CJK_SONG)
    return canvas


def render_t2i_mail(t2i_img, palette, number="", title="", photo=None, dpi=300, rounded=False, radius_mm=6.0, filt="none", location="", paper=(246, 241, 231), tfont=None):
    with scope(dpi):
        canvas = Image.new("RGB", (PXW, PXH), paper)
        img = draw_t2i_mail(canvas, t2i_img, palette, number, title, photo, location, tfont, paper)
        img = apply_filter(finalize(img), filt, dpi)
        return apply_round(img, radius_mm, dpi, paper) if rounded else img


def place(rect, bw, bh):
    """Fit a base (bw, bh) into a rect (x,y,w,h) centered -> (x, y, scale)."""
    x, y, w, h = rect
    s = min(w / bw, h / bh) if (bw and bh) else 1.0
    ox = x + (w - bw * s) / 2
    oy = y + (h - bh * s) / 2
    return ox, oy, s

ELEMENT_BOXES = {
    'mountain': (84, 28, 45, 28),
    'clouds':    (92, 26, 26, 9),
    'forest':    (82, 57, 59, 13),
    'water':     (58, 48, 84, 28),
    'birds':     (96, 24, 28, 10),
    'sun':       (91, 16, 12, 12),
    'moon':      (108, 11, 12, 12),
    'stars':     (58, 10, 90, 30),
    'boat':      (110, 64, 16, 12),
    'flowers':   (56, 38, 22, 24),
    'skyline':   (78, 52, 63, 14),
    'building':  (78, 52, 63, 14),
    'people':    (76, 64, 44, 18),
    'street':    (58, 74, 84, 8),
    'car':       (58, 76.5, 84, 4),
    'sign':      (58, 60, 84, 5),
    'aurora':    (58, 12, 90, 32),
    'fireworks': (86, 14, 40, 32),
    'train':     (58, 66, 84, 10),
    'field':     (58, 50, 84, 30),
    'goal':      (114, 46, 20, 18),
    'tent':      (96, 40, 26, 30),
    'market':    (58, 62, 84, 12),
    'port':      (58, 62, 84, 12),
    'metadata':  (10, 80, 34, 14),
    'swatches':  (118, 76, 18, 3),
}
# 上面这张表是元素默认位置的唯一来源：前端 index.html 的 BOXES 必须与它逐项一致，
# 否则 buildEditor() 里的 if(BOXES[v]) 会把只在后端存在的元素直接丢出画布编辑器（选了也拖不动）。
# 以前后端只有 12 项，其余 13 项只能写在 draw_composed 自动分支的每一行里就地拼字面量。


ELEM_PAD = 6.0        # 旋转元素小图四周的保底空地（mm）
# 带光晕的图元需要更多：光晕的半径是按自己的尺寸算的（太阳的外晕是 2.2r + 9mm 高斯），
# 跟框大小成正比，固定留 6mm 到旋转时仍然会把柔边切成一条直线。
GLOW_PAD = {"sun": 1.8, "moon": 0.2, "aurora": 0.2, "water": 0.15, "fireworks": 0.15,
            "mountain": 0.35, "clouds": 0.35, "flowers": 0.2}


def elem_pad(name, w, h):
    """旋转用的小图要比元素框大一圈，留多少取决于这个图元自己的柔边能跑出框多远。"""
    return max(ELEM_PAD, GLOW_PAD.get(name, 0.0) * max(w, h))


# 转小图只用双线性。BICUBIC 的负瓣会过冲：0.4mm 的水线只有 3px 宽，两边都是全透明（在 RGBA 里
# 就是 (0,0,0,0)），插值冲上去就撞在 255 被夹住，转完线上凭空出现一串比纸还亮的白点——
# 实测转 1° 的船，可见墨范围比直接画法多出 3.4mm（双线性只剩 0.5mm，那点多出来的就是几何量），
# 海面 1.9→0.5、花束 1.9→0.2，二十三个元素里没有一项因此变差。双线性权重非负，单调不过冲。
# （试过“按 alpha 预乘再转、转完除回来”：柔色大面（日晕）上 8bit 的预乘/除法会把淡区的颜色
# 粗量化掉，太阳的可见范围反而从 0.2mm 涨到 11.1mm，水色 1.9→9.0，所以不要预乘。）
ROT_RESAMPLE = Image.BILINEAR


class SoftTile(Image.Image):
    """旋转元素用的那张透明小图：往它身上贴东西得按正确的合成来，不能用 paste 的线性混合。

    PIL 的 paste(带蒙版) 是逐通道线性混合。目标是卡片（不透明的纸）时这正好等于「墨压在纸上」，
    所以直接画法一直是对的；可目标是一张全透明的 RGBA 小图时，RGB 被蒙版乘了一次（成了发黑的
    预乘色），alpha 又记了一遍覆盖率，等小图贴回卡片时还要再乘一次。实测 100/255 覆盖率的日晕
    落在象牙纸上变成 (178,174,165)，本应是 (239,227,198)：转 1° 的太阳四周因此多出一圈又暗又脏
    的灰雾，可见范围比直接画法大一倍——画蒙版的那些图元（水彩色团、羽化色带、月牙）全中招。
    alpha_composite 才是直 alpha 的正确合成，两层都半透明时也能叠对。

    只重写带蒙版的 paste；蒙版与源各自的 alpha 不同时（项目里没有这种写法）以蒙版为准。
    """

    def paste(self, source, box=(0, 0), mask=None):
        if mask is None or self.mode != "RGBA":
            return Image.Image.paste(self, source, box, mask)
        cover = mask.split()[3] if mask.mode == "RGBA" else mask.convert("L")
        layer = Image.new("RGBA", self.size, (0, 0, 0, 0))
        layer.paste(Image.merge("RGBA", list(source.convert("RGB").split()) + [cover]),
                    (box[0], box[1]))
        Image.Image.paste(self, Image.alpha_composite(self, layer), (0, 0))


def element_sub_img(palette, style, name, w, h, dpi, photo=None, paper=None):
    """Render one element into a transparent RGBA image sized w x h (mm), local coords.

    小图不是刚好 w×h，而是四周各留一块空地（elem_pad）：太阳的光晕、云、山脊上的水汽都比元素框
    大一圈（实测山的柔晕高出框顶 4mm），按框裁边再把元素一转，柔边就被切成一条笔直的直线——
    和天空底板当年那个硬边是同一类毛病。留了空地，超出框的那部分也能完整跟着旋转。
    """
    global MM, PXW, PXH, PX_PER_PT
    m = dpi / 25.4
    pad = elem_pad(name, w, h)
    # also safe standalone: callers normally already hold RENDER_LOCK through scope()
    with RENDER_LOCK:
        old = (MM, PXW, PXH, PX_PER_PT)
        MM, PXW, PXH = m, int(round((w + 2 * pad) * m)), int(round((h + 2 * pad) * m))
        img = Image.new("RGBA", (PXW, PXH), (0, 0, 0, 0))
        img.__class__ = SoftTile          # 小图上的蒙版 paste 要按直 alpha 合成，见 SoftTile
        try:
            draw_layout_element(img, palette, style, name, (pad, pad, w, h), photo, paper=paper)
        finally:
            MM, PXW, PXH, PX_PER_PT = old
    return img


def framed_photo_sub(img, gap_mm, border_mm, frame):
    """照片连着它外面那圈边框，画进一张透明小图（边框外沿离照片 gap_mm，线宽 border_mm）。

    带旋转的照片得先拼成这么一张小图再转，边框才能跟着照片一起转；否则只能在那个旋转后的
    外接方框上画一个正放的框，角度越大框和照片离得越远。
    小图四周留的空地刚好能装下这根边框，免得旋转时又被当成柔边切成直线。
    """
    pad = int(gap_mm*MM + border_mm*MM) + 1            # 照片每边到小图边界的像素
    g = int(gap_mm*MM)
    tile = Image.new("RGBA", (img.width + 2*pad, img.height + 2*pad), (0, 0, 0, 0))
    tile.paste(img.convert("RGBA"), (pad, pad))
    ImageDraw.Draw(tile).rectangle([pad - g, pad - g, pad + img.width + g, pad + img.height + g],
                                   outline=frame, width=int(round(border_mm*MM)))
    return tile


def draw_layout_element(canvas, palette, style, name, rect, photo=None, rot=0, paper=None):
    """Draw one element fitted into a rect (x, y, w, h) in mm. rot = degrees.
    paper 是给旋转元素准备的回退值：那些图元要按“落在什么颜色上”决定用亮墨还是暗墨，
    旋转时它们先被画到一张透明小图上，那里看不到真实背景，只能按纸色判断。"""
    x, y, w, h = rect
    d = ImageDraw.Draw(canvas)
    if rot:
        dpi = max(150, int(round(MM * 25.4)))
        # 元素到底落在哪块背景上，直接从卡上取这块的平均色再交给小图。小图是画在透明纸上的，
        # 那里看不到真实背景：星点、月牙、烟花这类“暗底给亮墨、亮底给暗墨”的图元一转动就会拿
        # 纸色误判，在夜空上反过来用暗墨，等于把刚修好的“隐形元素”又请回来一遍。
        gx0 = max(0, int((x - 2 * ELEM_PAD) * MM)); gy0 = max(0, int((y - 2 * ELEM_PAD) * MM))
        gx1 = min(canvas.width, int((x + w + 2 * ELEM_PAD) * MM))
        gy1 = min(canvas.height, int((y + h + 2 * ELEM_PAD) * MM))
        bg = canvas.convert("RGB").crop((gx0, gy0, max(gx0, gx1), max(gy0, gy1))) \
            .resize((1, 1), Image.BOX).getpixel((0, 0)) if gx1 > gx0 and gy1 > gy0 else (paper or IVORY)
        sub = element_sub_img(palette, style, name, w, h, dpi, photo, bg) \
            .rotate(rot, expand=True, resample=ROT_RESAMPLE)
        px = int(x*MM + w*MM/2 - sub.width/2); py = int(y*MM + h*MM/2 - sub.height/2)
        canvas.paste(sub, (px, py), sub)
        return
    if name == "mountain":
        ox, oy, s = place(rect, 36.0, 22.5)
        ridge_motif(canvas, palette, origin=(ox, oy), scale=s, style=style)
    elif name == "clouds":
        ox, oy, s = place(rect, 26.0, 6.0)
        draw_clouds(canvas, ox + 13.0*s, oy + 3.0*s, s, cloud_tone(palette), seed=3)
    elif name == "forest":
        # 最高的那棵树顶到 y_base - 1.1*1.15*height，反推回来框高只容得下 0.79 倍；以前取 0.85，树尖会探出框顶
        draw_pines(canvas, x, x + w, y_base=y + h, height=h * 0.79, color=darken(palette[1], 8), seed=22)
    elif name == "water":
        # fuller sea: horizon + gradient water + waves + glint
        hx0 = x; hx1 = x+w; hy = y + h*0.12
        side, botf = max(3.0, w*0.07), max(3.0, h*0.28)
        band_fill(canvas, hx0, hx1, hy, y+h, palette[0], 110, side, botf)
        # deeper band near bottom（与主色带用同一套羽化窗口，否则两层各自缩一圈边，会看出叠影）
        band_fill(canvas, hx0, hx1, y+h*0.62, y+h, darken(palette[0], 18), 120, side, botf)
        # 日照水面的反光：一列由宽到窄的暖色短横（旧版 draw_water 就是这个画法）。
        # 以前是一个淡色椭圆 + 4mm 模糊：椭圆比模糊半径大不了多少，化出来像一个灰色的
        # 圆环浮在海面正中，看不出是反光；改成沿水平方向排开，既能看出是一列，也不会打圈。
        # 形状只画到 L 蒙版上、颜色另铺一层：若在 RGBA 层里画完再整层模糊，层上没画到的地方
        # 颜色是「透明的黑」，模糊把黑拽进彩色里，边缘就剩一圈灰晕——和天空底板当年同一个毛病。
        # 顺带只在反光自己的外接矩形里建层：600dpi 整幅是 3543x2362，一次全幅高斯要一秒多。
        glint = (255, 222, 165)
        gx = x + w*0.55
        ry = max(0.35, h * 0.035)
        rows = [(hy + (y + h - hy) * (0.14 + i * 0.18), max(0.6, w * (0.060 - i * 0.008)))
                for i in range(5)]
        pad, gw = 3.0, max(r[1] for r in rows)
        bx0 = max(0.0, gx - gw - pad); bx1 = min(PXW / MM, gx + gw + pad)
        by0 = max(0.0, min(r[0] for r in rows) - ry - pad)
        by1 = min(PXH / MM, max(r[0] for r in rows) + ry + pad)
        msk = Image.new("L", (max(1, int((bx1 - bx0) * MM)), max(1, int((by1 - by0) * MM))), 0)
        md = ImageDraw.Draw(msk)
        gr = rnd(31)
        for i, (yy, half) in enumerate(rows):
            for _ in range(3):                       # 同一张蒙版里画完再一次模糊，
                jx = gr.uniform(-half*0.08, half*0.08)          # 避免每个椭圆都建一层再高斯
                jy = gr.uniform(-ry*0.3, ry*0.3)
                md.ellipse([(gx-half+jx-bx0)*MM, (yy-ry+jy-by0)*MM,
                            (gx+half+jx-bx0)*MM, (yy+ry+jy-by0)*MM], fill=150 - i*18)
        msk = msk.filter(ImageFilter.GaussianBlur(max(1, int(1.6*MM/2))))
        canvas.paste(Image.new("RGB", msk.size, glint), (int(bx0*MM), int(by0*MM)), msk)
        # waves：真的起浪，并且两端收在色带里、渐隐而不是截断。
        # 旧版是 9 段、幅度 0.12mm 的折线，放大看就是一条直尺线，还在 x=58/x=142 处硬截断；
        # 颜色也用 darken(palette[0], 30)——照片天空偏白时那只有 40 级反差，纸上基本印不出来。
        wave_c = _mix(palette[0], (30, 34, 40), 0.62)
        for k in range(3):
            rr = rnd(20 + k)
            yy = y + h*(0.35 + k*0.22)
            amp = max(0.45, h*0.05)
            inset = (hx1-hx0) * (0.08 + 0.06*rr.random())
            wx0, wx1 = hx0 + inset, hx1 - inset
            cycles, phase = 2.2 + 0.6*k, rr.uniform(0, math.pi)
            n = 96
            pts = [(wx0 + (wx1-wx0)*i/n,
                    yy + math.sin(i/n*math.pi*2*cycles + phase) * amp * (0.6 + 0.4*math.sin(i/n*math.pi)))
                   for i in range(n+1)]
            taper_line(canvas, pts, wave_c, width_mm=0.32, alpha=175, taper_mm=(wx1-wx0)*0.16)
    elif name == "birds":
        draw_birds(canvas, [(x+w*0.2, y+h*0.5), (x+w*0.5, y+h*0.2), (x+w*0.8, y+h*0.6)],
                   size=max(1.6, h*0.34), color=darken(INK, 20))
    elif name == "sun":
        draw_sun(canvas, x+w/2, y+h/2, min(w, h)*0.5, palette[2])
    elif name == "moon":
        # 本体以前用 palette[2]（照片最亮色）+ 写死纸色的咬口：在近白纸上整枚月亮只有 30 级反差，
        # 看上去就是一块淡得辨不出的白。现在跟星点同一段逻辑：暗背景给暖白，亮背景给蓝墨。
        draw_moon(canvas, x+w/2, y+h/2, min(w, h)*0.5, star_tone(canvas, rect, palette, paper), paper)
    elif name in ("stars", "star", "night"):
        draw_stars(canvas, rect, palette, paper)
    elif name == "boat":
        draw_boat(canvas, x+w/2, y+h - h*0.35, min(w/16.0, h/12.0), palette, palette[2])
    elif name == "flowers":
        draw_flowers(canvas, x+w/2, y+h*0.6, min(w/14.0, h/8.0), palette)
    elif name in ("skyline", "building", "city", "architecture", "urban", "tower"):
        # 最高的那栋楼是 height*1.15，顶上还可能有一根 1.3mm 的天线；以前直接把框高当 height 传进去，
        # 于是最高的几栋连同天线戳出框顶 3mm 以上。
        draw_skyline(canvas, x, x+w, y+h, darken(palette[1], 4), height=max(2.0, (h - 1.3) / 1.15))
    elif name == "people":
        draw_people(canvas, x, x+w, y+h, darken(palette[1], 10), seed=14)
    elif name in ("street","road"):
        sx0, sx1, syb, sk = ground_line(rect, STREET_TOP, STREET_BOT)
        draw_street(canvas, sx0, sx1, syb, palette, k=sk)
    elif name in ("car","vehicle","taxi","bus"):
        ax0, ax1, ayb, ak = ground_line(rect, CAR_TOP, CAR_BOT)
        draw_car(canvas, ax0, ax1, ayb, darken(palette[1], -6), seed=40, k=ak)
    elif name in ("sign","signboard","shop","store"):
        gx0, gx1, gyb, gk = ground_line(rect, SIGN_TOP, SIGN_BOT)
        draw_sign(canvas, gx0, gx1, gyb, darken(palette[1], 0), seed=51, k=gk)
    elif name in ("aurora","northern","polar"):
        draw_aurora(canvas, x, x+w, y, y+h, seed=70)
    elif name in ("fireworks","celebration"):
        # 散布半径与爆点半径都从框尺寸反推：两者之和不超过框的半宽/半高，墨就不会出框
        draw_fireworks(canvas, x+w/2, y+h*0.5, max(1.0, min(w, h)*0.5*0.55/1.3), palette, seed=80,
                       spread=(w*0.5*0.42, h*0.5*0.42), color=firework_tone(canvas, rect, palette, paper))
    elif name in ("train","tram","rail"):
        tx0, tx1, tyb, tk = ground_line(rect, TRAIN_TOP, TRAIN_BOT)
        draw_train(canvas, tx0, tx1, tyb, darken(palette[1], -6), seed=90, k=tk)
    elif name in ("field","grass","meadow","pitch","lawn","court"):
        draw_field(canvas, x, x+w, y, palette, seed=60, depth=h)
    elif name in ("goal","goalpost","soccer","football","stadium","net"):
        # 球门宽 16*尺度、高 4*尺度；以前的尺度取 min(框宽,框高)/12，跟框的真实宽高对不上，
        # 于是 20x18 的框里门要画到 24mm 宽（实测左右各出框 2mm 以上）。现在按门宽反推尺度。
        draw_goal(canvas, x+w/2, y+h*0.85, max(0.4, min(w/17.0, h*0.85/4.2)), palette, seed=70)
    elif name in ("tent","camp","camping","campsite"):
        # 帐篷底宽 12*尺度，两侧各还一根 1.4mm 的地钉线；同理按框宽反推。
        draw_tent(canvas, x+w/2, y+h*0.8, max(0.4, min((w/2-1.5)/6.0, h*0.8/4.0)), palette, seed=100)
    elif name in ("market","bazaar","stall","vendor"):
        mx0, mx1, myb, mk = ground_line(rect, MARKET_TOP, MARKET_BOT)
        draw_market(canvas, mx0, mx1, myb, palette, seed=110, k=mk)
    elif name in ("port","pier","dock","harbor","jetty","wharf"):
        px0, px1, pyb, pk = ground_line(rect, PIER_TOP, PIER_BOT)
        draw_pier(canvas, px0, px1, pyb, palette, seed=130, k=pk)
    elif name == "metadata":
        pass
    elif name == "swatches":
        pass


def draw_composed(canvas, photo, palette, elements, title, date_str="", swatches=True, style="default", extra=(), layout=None, tfont=None, number="N° 01", location="", tstyle="normal", paper=None):
    """Compose the pattern side from a set of *recognized* image elements.
    layout: optional dict {id: (x, y, w, h) mm} overriding element placement."""
    d = ImageDraw.Draw(canvas)
    paper = paper or IVORY
    ink = ink_for(paper)
    box = ELEMENT_BOXES

    # ---- ordered layout (manual canvas editor): z-order + rotation --------
    if isinstance(layout, list):
        # 底板必须在两条路径上都画：以前只有下面的自动布局分支画，于是用户一点开「画布编辑」
        # （前端从此下发有序布局）整张卡的天色和暗角就没了，同输入两种输出。
        draw_backdrop(canvas, palette, elements, extra)
        for item in layout:
            id_ = item.get("id")
            rect = (item["x"], item["y"], item["w"], item["h"])
            rot = int(item.get("rot", 0) or 0)
            if id_ == "photo":
                pw, phh = rect[2], rect[3]
                ox, oy, s = place(rect, pw, phh)
                cw, chh = pw*s, phh*s
                img = photo.resize((int(cw*MM), int(chh*MM)), Image.LANCZOS)
                gap = 2.0
                if rot:
                    # 照片和它外面那圈边框要一起转。以前是先转照片、再在转完的外接方框外面画一个
                    # 正放的框：那个框既不像这张照片的框，又会探出元素框压到别的图元上。
                    sub = framed_photo_sub(img, gap, 0.5, ink) \
                        .rotate(rot, expand=True, resample=ROT_RESAMPLE)
                else:
                    sub = img.convert("RGBA")
                px = int(ox*MM + cw*MM/2 - sub.width/2); py = int(oy*MM + chh*MM/2 - sub.height/2)
                canvas.paste(sub, (px, py), sub)
                if not rot:
                    d.rectangle([px-gap*MM, py-gap*MM, px+sub.width+gap*MM, py+sub.height+gap*MM], outline=ink, width=int(round(0.5*MM)))
            elif id_ == "metadata":
                mx, my = rect[0], rect[1]
                if title:
                    draw_meta(canvas, (mx, my), title, ink, 11, tfont, tstyle)
                    text(canvas, (mx, my+4.8), "- PHOTO · POSTCARD -", fade(ink, paper, .22), 5.0, spacing=0.9)
                dd = ("DATE · " + date_str) if date_str else "LOCATION · DATE"
                text(canvas, (mx, my+9.6), dd, fade(ink, paper, .30), 4.8, spacing=0.7)
                yb = my+14.5
                if number:
                    text(canvas, (mx, yb), number, fade(ink, paper, .22), 5.5, spacing=0.9); yb += 5.0
                if location:
                    text(canvas, (mx, yb), location, fade(ink, paper, .40), 4.4, spacing=0.5, path=CJK_SONG)
            elif id_ == "swatches":
                sx, sy = rect[0], rect[1]
                for i, col in enumerate(palette):
                    c = (sx + i*5.4)*MM
                    d.ellipse([c, sy*MM, c+mm(2.6), (sy+2.6)*MM], fill=col, outline=darken(col,40), width=1)
            else:
                draw_layout_element(canvas, palette, style, id_, rect, rot=rot, paper=paper)
        return canvas

    def rect(name, default):
        return tuple(layout[name]) if layout and name in layout else default
    # photo (movable)
    pr = rect('photo', None)
    ph_ratio = photo.size[1]/photo.size[0]
    pw, phh = 46.0, 46.0*ph_ratio
    if phh > 62.0: phh = 62.0; pw = phh/ph_ratio
    if pr is None:
        photo_box(canvas, photo, left=9.0, top=9.0, width_mm=pw, max_h_mm=62.0, frame=ink)
    else:
        # fit photo into rect (keep aspect, centered)
        ox, oy, s = place(pr, pw, phh)
        canvas.paste(photo.resize((int(pw*s*MM), int(phh*s*MM)), Image.LANCZOS), (int(ox*MM), int(oy*MM)))
        gap = 2.0
        d.rectangle([ox*MM-gap*MM, oy*MM-gap*MM, (ox+pw*s)*MM+gap*MM, (oy+phh*s)*MM+gap*MM], outline=ink, width=int(round(0.5*MM)))
    kn = sknobs(style)
    els = set(elements) | set(extra)
    # normalise aliases so recognition variants map to one drawing
    GNORM = {'sea':'water','ocean':'water','river':'water','lake':'water','pond':'water','harbor':'water',
             'speedboat':'boat','cargo ship':'boat','ship':'boat','sailboat':'boat','boat':'boat','yacht':'boat',
             'skyscraper':'building','tower':'building','architecture':'building','city':'building','mall':'building',
             'shopping':'building','store':'building','shop':'building','crowd':'people','pedestrian':'people',
             'avenue':'street','lane':'street','crosswalk':'street','road':'street',
             'car':'car','vehicle':'car','taxi':'car','bus':'car','truck':'car','auto':'car',
             'sign':'sign','signboard':'sign','banner':'sign','billboard':'sign',
             'aurora':'aurora','northern lights':'aurora','polar':'aurora',
             'firework':'fireworks','fireworks':'fireworks','celebration':'fireworks',
             'train':'train','tram':'train','rail':'train','locomotive':'train',
             'grass':'field','field':'field','meadow':'field','pitch':'field','lawn':'field','pasture':'field','playground':'field','garden':'field',
             'goal':'goal','goalie':'goal','goalpost':'goal','net':'goal','soccer':'goal','football':'goal','stadium':'goal','stand':'goal','court':'field','football field':'field',
             'tent':'tent','camp':'tent','campsite':'tent','camping':'tent','tarp':'tent','帐篷':'tent','露营':'tent',
             'market':'market','marketplace':'market','bazaar':'market','stall':'market','vendor':'market','集市':'market','市集':'market',
             'port':'port','pier':'port','dock':'port','harbor':'port','jetty':'port','wharf':'port','码头':'port','港口':'port'}
    for e in list(els):
        if e in GNORM: els.add(GNORM[e])
    def has(*names): return any(n in els for n in names)

    has_mtn = has("mountain", "peak", "rock", "cliff", "ridge", "summit", "石", "山", "峰")
    has_cloud = has("cloud", "clouds", "雾", "云", "mist", "fog")
    has_forest = has("forest", "tree", "trees", "pine", "pines", "plants", "林", "树", "森")
    has_water = has("water", "lake", "river", "sea", "ocean", "shore", "水", "湖", "海", "江")
    has_birds = has("bird", "birds", "鸟")
    has_sun = has("sun", "sunset", "sunrise", "moon", "日", "日落")
    has_snow = has("snow", "雪")
    has_sky = has("sky", "天")
    has_bldg = has("building", "architecture", "house", "city", "town", "tower", "urban", "apartment", "建筑", "房", "城", "楼", "厦", "店", "商")
    has_people = has("person", "people", "crowd", "pedestrian", "human", "人", "行人", "人群")
    has_street = has("street", "road", "avenue", "lane", "路", "街", "道")
    has_car = has("car", "vehicle", "taxi", "bus", "truck", "车")
    has_sign = has("sign", "signboard", "shop", "store", "billboard", "招牌", "店")
    has_aurora = has("aurora", "northern", "polar", "极光")
    has_fireworks = has("firework", "fireworks", "celebration", "烟花")
    has_train = has("train", "tram", "rail", "locomotive", "火车")
    has_field = has("field", "grass", "meadow", "pitch", "lawn", "pasture", "court", "球场", "草地", "草坪", "场")
    has_goal = has("goal", "goalie", "goalpost", "net", "soccer", "football", "stadium", "stand", "球门", "足球")
    has_tent = has("tent", "camp", "camping", "campsite", "帐篷", "露营")
    has_market = has("market", "marketplace", "bazaar", "stall", "vendor", "集市", "市集")
    has_port = has("port", "pier", "dock", "harbor", "jetty", "wharf", "码头", "港口")
    has_night = has("night", "evening", "dusk", "夜") or "night" in els or "evening" in els
    # guarantee a ground + sky scene so the motif area is never empty
    if not (has_water or has_mtn or has_forest or has_bldg or has_street):
        has_water = True; has_sun = True; els.add("water"); els.add("sun")
    if not (has_mtn or has_cloud or has_forest or has_water or has_sun or has_bldg or has_people or has_street):
        has_mtn = True; has_cloud = has_forest = kn["draw_forest"]
    has_street = has("street", "road", "avenue", "lane", "crosswalk", "路", "街", "道")
    if not (has_mtn or has_cloud or has_forest or has_water or has_sun or has_bldg or has_people or has_street):
        has_mtn = True; has_cloud = has_forest = kn["draw_forest"]

    # --- atmospheric backdrop: sky wash + sunset glow + vignette（两条构图路径共用）---
    draw_backdrop(canvas, palette, elements, extra)

    if has_cloud or has_sky or "clouds" in els:
        draw_layout_element(canvas, palette, style, "clouds", rect('clouds', box['clouds']))
    if has_sun or "sun" in els or any(k in els for k in ("sunset","sunrise","dusk","dawn","黄昏","日落","日出","暮")):
        draw_layout_element(canvas, palette, style, "sun", rect('sun', box['sun']))
    if "moon" in els:
        draw_layout_element(canvas, palette, style, "moon", rect('moon', box['moon']))
    if "night" in els or "stars" in els:
        draw_layout_element(canvas, palette, style, "stars", rect('stars', box['stars']))
    if has_mtn:
        draw_layout_element(canvas, palette, style, "mountain", rect('mountain', box['mountain']))
    if has_forest and kn["draw_forest"] and "skyline" not in els:
        draw_layout_element(canvas, palette, style, "forest", rect('forest', box['forest']))
    if has_bldg or "skyline" in els or "building" in els or "city" in els:
        draw_layout_element(canvas, palette, style, "skyline", rect('skyline', box['skyline']))
    if has_people:
        draw_layout_element(canvas, palette, style, "people", rect('people', box['people']))
    if has_street:
        draw_layout_element(canvas, palette, style, "street", rect('street', box['street']))
    if has_car or "car" in els:
        draw_layout_element(canvas, palette, style, "car", rect('car', box['car']))
    if has_sign or "sign" in els:
        draw_layout_element(canvas, palette, style, "sign", rect('sign', box['sign']))
    if has_aurora or "aurora" in els:
        draw_layout_element(canvas, palette, style, "aurora", rect('aurora', box['aurora']))
    if has_fireworks or "fireworks" in els:
        draw_layout_element(canvas, palette, style, "fireworks", rect('fireworks', box['fireworks']))
    if has_train or "train" in els:
        draw_layout_element(canvas, palette, style, "train", rect('train', box['train']))
    if has_field or "field" in els:
        draw_layout_element(canvas, palette, style, "field", rect('field', box['field']))
    if has_goal or "goal" in els:
        draw_layout_element(canvas, palette, style, "goal", rect('goal', box['goal']))
    if has_tent or "tent" in els:
        draw_layout_element(canvas, palette, style, "tent", rect('tent', box['tent']))
    if has_market or "market" in els:
        draw_layout_element(canvas, palette, style, "market", rect('market', box['market']))
    if has_port or "port" in els:
        draw_layout_element(canvas, palette, style, "port", rect('port', box['port']))
    if has_water or "water" in els:
        draw_layout_element(canvas, palette, style, "water", rect('water', box['water']))
    if has_birds or "birds" in els:
        draw_layout_element(canvas, palette, style, "birds", rect('birds', box['birds']))
    if "boat" in els:
        draw_layout_element(canvas, palette, style, "boat", rect('boat', box['boat']))
    if "flowers" in els:
        draw_layout_element(canvas, palette, style, "flowers", rect('flowers', box['flowers']))

    # metadata
    mx, my = (layout and layout.get('metadata', (10, 80, 34, 14))[:2]) or (10.0, 80.0)
    if title:
        draw_meta(canvas, (mx, my), title, ink, 11, tfont, tstyle)
        text(canvas, (mx, my+4.8), "- PHOTO · POSTCARD -", fade(ink, paper, .22), 5.0, spacing=0.9)
    dd = ("DATE · " + date_str) if date_str else "LOCATION · DATE"
    text(canvas, (mx, my+9.6), dd, fade(ink, paper, .30), 4.8, spacing=0.7)
    yb = my+14.5
    if number:
        text(canvas, (mx, yb), number, fade(ink, paper, .22), 5.5, spacing=0.9); yb += 5.0
    if location:
        text(canvas, (mx, yb), location, fade(ink, paper, .40), 4.4, spacing=0.5, path=CJK_SONG)
    if swatches:
        sx, sy = (layout and layout.get('swatches', (118, 76, 18, 3))[:2]) or (118.0, 76.0)
        for i, col in enumerate(palette):
            c = (sx + i*5.4)*MM
            d.ellipse([c, sy*MM, c+mm(2.6), (sy+2.6)*MM], fill=col, outline=darken(col,40), width=1)
    return canvas


def draw_picture(canvas, photo, palette, title, date_str="", motif=True, forest=True, birds=True, swatches=True):
    d = ImageDraw.Draw(canvas)
    # photo top-left, kept aspect, thin frame + paper gap
    photo_box(canvas, photo, left=9.0, top=9.0, width_mm=46.0, max_h_mm=62.0)
    if birds:
        draw_birds(canvas, [(99, 30), (110, 26), (121, 32)], size=2.4, color=darken(INK, 20))
    if motif:
        ridge_motif(canvas, palette, origin=(84, 28), scale=1.25)
    if forest:
        draw_pines(canvas, 82, 141, y_base=70, height=11, color=darken(palette[1], 8), seed=22)
    # metadata lower-left (restrained), with EXIF date
    mx, my = 10.0, 80.0
    if title:
        text(canvas, (mx, my), title, INK, 11, spacing=1.2)
        text(canvas, (mx, my+4.8), "- ABOVE THE CLOUDS -", darken(INK,45), 5.0, spacing=0.9)
    dd = ("DATE · " + date_str) if date_str else "LOCATION · DATE"
    text(canvas, (mx, my+9.6), dd, darken(INK,40), 4.8, spacing=0.7)
    text(canvas, (mx, my+14.5), "N° 01", darken(INK,55), 5.5, spacing=0.9)
    # 3 source-derived swatches (bottom-right, below the forest)
    if swatches:
        swy = 76.0
        for i, col in enumerate(palette):
            c = (118.0 + i*5.4)*MM
            d.ellipse([c, swy*MM, c+mm(2.6), (swy+2.6)*MM], fill=col, outline=darken(col,40), width=1)
    return canvas


def draw_mail(canvas, palette, idx="N° 01", title="", photo=None, location="", paper=None):
    """YZ/T 0190 art-postcard writing side (正面) — per user refinements:
    square post-code boxes, 邮政编码 label only (no red boxes), no address
    label, no 印制单位, photo panel without an overlaid motif."""
    d = ImageDraw.Draw(canvas)
    paper = paper or IVORY
    ink = ink_for(paper)
    b = 6.0
    d.rectangle([b*MM, b*MM, (CW-b)*MM, (CH-b)*MM], outline=fade(ink, paper, .50), width=int(round(0.4*MM)))

    # -- artwork panel (left): the photo, clean (no motif overlay) -----------
    if photo is not None:
        pw, phh = 26.0, 32.0
        ax, ay = 11.0, 40.0
        canvas.paste(cover_crop(photo, int(pw*MM), int(phh*MM)), (int(ax*MM), int(ay*MM)))
        d.rectangle([ax*MM, ay*MM, (ax+pw)*MM, (ay+phh)*MM], outline=fade(ink, paper, .55), width=int(round(0.5*MM)))
        ty = ay + phh + 3.0
        # adaptive title size so it never runs into the 条码区 (left of x=52mm)
        n = len(title)
        sz = 9.0 if n <= 10 else (7.0 if n <= 18 else (5.5 if n <= 26 else 4.6))
        sp = 1.0 if n <= 10 else 0.5
        if title:
            text(canvas, (ax, ty), title, ink, sz, spacing=sp)
        yy = ty + 4.4
        if idx:
            text(canvas, (ax, yy), idx, fade(ink, paper, .22), 5, spacing=0.9); yy += 4.6
        if location:
            text(canvas, (ax, yy), location, fade(ink, paper, .40), 4.4, spacing=0.5, path=CJK_SONG); yy += 4.6
        for i, col in enumerate(palette):
            c = (ax + i*4.8)*MM
            d.ellipse([c, (yy)*MM, c+mm(2.4), (yy+2.4)*MM], fill=col, outline=darken(col,40), width=1)

    # -- square post-code boxes (top-left, smaller) --------------------------
    bx, by, sq, gap = 10.0, 10.5, 9.0, 1.6
    for i in range(6):
        x0 = (bx + i*(sq+gap))*MM
        d.rectangle([x0, by*MM, x0+sq*MM, (by+sq)*MM], outline=POSTAL_RED, width=int(round(0.6*MM)))

    # -- stamp box 贴邮票处 (top-right, 20x20, 5mm from top) -------------------
    sx, sy, sw, sh = 124.0, 6.0, 20.0, 20.0
    d.rectangle([sx*MM, sy*MM, (sx+sw)*MM, (sy+sh)*MM], outline=fade(ink, paper, .45), width=int(round(0.5*MM)))
    ctext(canvas, (sx+2.5, sy+8.5), "贴邮票处", fade(ink, paper, .30), 7, path=CJK_KAI)
    text(canvas, (sx+13.5, sy+1.0), "80", POSTAL_RED, 5, spacing=0.4)

    # -- address writing lines (right, 10mm spacing, no label) ---------------
    for i in range(4):
        yy = (40+i*10)*MM
        d.line([(68*MM, yy), (144*MM, yy)], fill=fade(ink, paper, .68), width=1)

    # -- bottom: 条码区 (shifted left, clear of neighbours) + 邮政编码 label --
    d.rectangle([48*MM, 76*MM, 88*MM, 94*MM], outline=fade(ink, paper, .62), width=1)
    ctext(canvas, (50, 82.0), "条码区", fade(ink, paper, .45), 4.5)
    ctext(canvas, (108, 84), "邮政编码：", fade(ink, paper, .30), 6.5, path=CJK_SONG)
    return canvas


def finalize(canvas):
    canvas = canvas.convert("RGB")
    return ImageEnhance.Contrast(canvas).enhance(1.03)


def build(photo_path, title, out_base):
    photo = Image.open(photo_path).convert("RGB")
    palette = sample_palette(photo)
    date_str = read_date(photo)   # EXIF capture date, "" if absent
    side_img = finalize(draw_picture(paper_canvas(1), photo, palette, title, date_str))
    side_mail = finalize(draw_mail(paper_canvas(2), palette, idx="N° 01", title=title, photo=photo))
    side_img.save(out_base + "-front.png")
    side_mail.save(out_base + "-back.png")

    from reportlab.pdfgen import canvas as rlcanvas
    from reportlab.lib.units import mm as rlmm
    out = out_base + ".pdf"
    c = rlcanvas.Canvas(out, pagesize=(150*rlmm, 100*rlmm))
    w_pt, h_pt = 150*rlmm, 100*rlmm
    c.drawImage(out_base + "-front.png", 0, 0, width=w_pt, height=h_pt); c.showPage()
    c.drawImage(out_base + "-back.png", 0, 0, width=w_pt, height=h_pt); c.showPage()
    c.save()
    print("wrote", out); print("palette", palette)


if __name__ == "__main__":
    build(os.path.join(ROOT, "photo-B.img"), "ALPINE RIDGE", os.path.join(ROOT, "postcard-B"))
    build(os.path.join(ROOT, "photo-A.img"), "CLOUD PEAK", os.path.join(ROOT, "postcard-A"))
