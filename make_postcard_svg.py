# -*- coding: utf-8 -*-
"""
Vector SVG export that faithfully transcribes the bitmap engine (make_postcard.py):
front = draw_picture (photo box 9,9 w46 / ridge_motif origin 84,28 s1.25 / pines
82..141 y70 h11 / metadata 10,80 / swatches 118,76), back = draw_mail (YZ/T 0190).
Same geometry, same pt sizes (1pt = 25.4/72 mm), same letter-spacing-by-spaces
trick, same fade/darken colours - so the SVG matches the PNG the app renders.
"""
import base64, io, random, math, traceback
from PIL import Image
try:
    import numpy as _NP
except Exception:
    _NP = None

CW, CH = 150.0, 100.0
IVORY = (246, 241, 231)
INK = (56, 58, 62)
POSTAL_RED = (179, 58, 58)
PH_RATIO = 0.75                  # kept for API compatibility (never mutated)

PT = 25.4 / 72.0                 # 1pt in mm (SVG user units are mm)

# Grid budget. gh used to be unbounded: a 1:8 panorama at the portrait grid became
# 800x6400 and _trace_mask then keeps a set of every filled cell per tone layer -
# millions of cells and gigabytes of memory. The budget only bites on panoramas /
# long strips: normal 3:2 and 2:3 photos stay at 640 / 800 unchanged (the widest
# normal case, 800x1206 = 964.8k cells, still fits the 1M ceiling - a tight 900k
# limit would have clipped benchmark card R1 for no safety gain).
GRID_CELLS_NP = 1_000_000        # numpy path: cell ceiling for the whole grid
GRID_CELLS_PY = 350_000          # pure-Python fallback: far slower per cell
GRID_SIDE_MAX = 2000             # neither grid dimension may exceed this


def hexc(c): return "#%02x%02x%02x" % tuple(int(max(0, min(255, v))) for v in c)
def darken(c, amt): return tuple(max(0, v - amt) for v in c)
def _mix(c, t, f): return tuple(int((1 - f) * v + f * tt) for v, tt in zip(c, t))
def fade(ink, paper, f): return _mix(ink, paper or IVORY, max(0.0, min(1.0, f)))
def lum(c): return 0.299*c[0] + 0.587*c[1] + 0.114*c[2]
def ink_for(paper): return INK if paper is None or lum(paper) >= 108 else (240, 242, 240)


# characters XML 1.0 forbids even escaped: one stray control byte (pasted title,
# AI caption) would make the whole file unparseable
_XML_ILLEGAL = {c: None for c in
                list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20))
                + [0xFFFE, 0xFFFF]}


def _xml_clean(s):
    return str(s).translate(_XML_ILLEGAL)


def esc(s):
    if s is None:
        return ""
    return (_xml_clean(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def embed(photo, q=87, max_px=1300):
    """Base64 the photo into the SVG (capped resolution - the artwork stays vector)."""
    im = photo if isinstance(photo, Image.Image) else Image.open(photo)
    im = im.convert("RGB")
    if max(im.size) > max_px:
        im = im.copy()
        im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=q)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def sample_palette(photo):
    """Same as make_postcard.sample_palette: sky / dark structure / brightest accent."""
    thumb = photo.convert("RGB")
    thumb.thumbnail((192, 192), Image.BILINEAR)
    w, h = thumb.size
    sky = thumb.crop((0, 0, w, int(h*0.35))).resize((32, 32))
    body = thumb.crop((0, int(h*0.4), w, h)).resize((32, 32))
    def dom(im):
        q = im.convert("RGB").quantize(colors=5); pal = q.getpalette(); c = sorted(q.getcolors(), reverse=True)[0][1]
        return tuple(pal[c*3:c*3+3])
    def brightest(im):
        # brightest accent that is genuinely bright; pure-colour / very dark
        # photos have no lum>150 pixel at all, and returning (0,0,0) painted a
        # black swatch and skewed cloud_tone. Fall back to the brightest pixel.
        best = (0, 0, 0); bv = -1.0
        fallback = (0, 0, 0); fv = -1.0
        for c in im.convert("RGB").getdata():
            l = lum(c)
            if l > fv:
                fv, fallback = l, c
            if l > bv and l > 150:
                bv, best = l, c
        return best if bv >= 0 else fallback
    def darkest(im):
        best=(255,255,255); bv=999
        for c in im.convert("RGB").getdata():
            l=lum(c)
            if l<bv: bv=l; best=c
        return best
    return dom(sky), darkest(body), brightest(thumb)


def cloud_tone(palette, paper=None):
    """Cloud/mist colour, pressed toward the dark structure colour (see bitmap engine)."""
    ref = paper or IVORY
    if lum(ref) < 108:
        return _mix(palette[2], (252, 252, 250), 0.55)
    f = 0.42
    t = _mix(palette[2], palette[1], f)
    if lum(ref) - lum(t) < 55:
        t = _mix(palette[2], palette[1], min(0.78, f + (55 - (lum(ref) - lum(t))) / 160.0))
    return t


SKNOBS = {
    "default":    dict(body=235, line_w=0.55, line_x=10, cl=205, draw_forest=True, draw_shadow=90),
    "ink":        dict(body=120, line_w=0.8,  line_x=-4, cl=140, draw_forest=True, draw_shadow=120),
    "watercolor": dict(body=115, line_w=0.45, line_x=22, cl=220, draw_forest=True, draw_shadow=60),
    "minimal":    dict(body=70,  line_w=0.28, line_x=48, cl=175, draw_forest=False, draw_shadow=45),
    "pastel":     dict(body=165, line_w=0.42, line_x=34, cl=212, draw_forest=True, draw_shadow=60),
    "oil":        dict(body=255, line_w=0.38, line_x=28, cl=188, draw_forest=True, draw_shadow=120),
    "vintage":    dict(body=205, line_w=0.5,  line_x=20, cl=200, draw_forest=True, draw_shadow=85),
    "film":       dict(body=212, line_w=0.5,  line_x=16, cl=200, draw_forest=True, draw_shadow=90),
}


def font_family(tfont):
    """The bitmap engine's fonts map to system font stacks (SVG can't ship a TTF cheaply)."""
    s = str(tfont or "").lower()
    if "misans" in s:   return "MiSans, 'Microsoft YaHei', sans-serif"
    if "simhei" in s or s == "hei":   return "SimHei, 'Microsoft YaHei', sans-serif"
    if "simkai" in s or "kai" in s: return "KaiTi, STKaiti, serif"
    if "simfang" in s or "fang" in s: return "FangSong, STFangsong, serif"
    if "simsun" in s or "song" in s: return "SimSun, NSimSun, serif"
    return "Arial, 'Microsoft YaHei', sans-serif"


def letterspace(s, spacing):
    """The bitmap engine separates glyphs with int(round(spacing)) real spaces."""
    if not s:
        return s
    sep = " " * max(1, int(round(spacing)))
    return sep.join(list(s))


def text_el(x, y_top, s, color, size_pt, spacing=0.0, tfont=None, weight=None,
            anchor="start", opacity=None):
    """Transcribe text(): anchor 'la' = left/ascender-top, so SVG y = y_top + ~0.9em."""
    fam = font_family(tfont)
    fs = size_pt * PT
    ls = " " * max(1, int(round(spacing))) if spacing > 0 else ""
    content = esc(letterspace(s, spacing)) if ls else esc(s)
    pre = ' xml:space="preserve"' if ls else ""
    extra = pre
    if weight == "bold":
        extra += ' font-weight="600"'
    if opacity is not None:
        extra += f' opacity="{opacity}"'
    if anchor != "start":
        extra += f' text-anchor="{anchor}"'
    return (f'<text x="{x:.2f}" y="{y_top + fs*0.9:.2f}" font-family="{fam}" '
            f'font-size="{fs:.2f}" fill="{hexc(color)}"{extra}>{content}</text>')


def svg_open(paper, w=CW, h=CH):
    """Paper texture mirrors paper_canvas(): a soft vertical light band toward
    (252,248,240) peaking mid-height, plus fine monochrome grain."""
    band = (252, 248, 240)
    blur = (f'<filter id="cloudblur" x="-80%" y="-80%" width="260%" height="260%">'
            f'<feGaussianBlur stdDeviation="2.1"/></filter>'
            f'<linearGradient id="paperband" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{hexc(band)}" stop-opacity="0"/>'
            f'<stop offset="0.5" stop-color="{hexc(band)}" stop-opacity="0.024"/>'
            f'<stop offset="1" stop-color="{hexc(band)}" stop-opacity="0"/></linearGradient>'
            f'<filter id="grain" x="0" y="0" width="100%" height="100%">'
            f'<feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch"/>'
            f'<feColorMatrix type="saturate" values="0"/></filter>'
            f'<filter id="wcBleed" x="-5%" y="-5%" width="110%" height="110%">'
            f'<feGaussianBlur stdDeviation="0.14"/></filter>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" '
            f'width="{w:g}mm" height="{h:g}mm" viewBox="0 0 {w:g} {h:g}" version="1.1">'
            f'<defs>{blur}</defs>')


def paper_texture(w=CW, h=CH):
    """Two overlays reproducing paper_canvas(): the mid-height light band + grain."""
    return (f'<rect x="0" y="0" width="{w:g}" height="{h:g}" fill="url(#paperband)"/>'
            f'<rect x="0" y="0" width="{w:g}" height="{h:g}" filter="url(#grain)" '
            f'opacity="0.05" style="mix-blend-mode:multiply"/>')


def photo_el(href, x, y, w, h, clip_id):
    return (f'<clipPath id="{clip_id}"><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}"/></clipPath>'
            f'<image xlink:href="{href}" href="{href}" x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
            f'preserveAspectRatio="none" clip-path="url(#{clip_id})"/>')


# ---------- photo vectorizer: the artwork adapts to ANY subject ----------
def _trace_mask(mask, w, h):
    """Binary-mask boundary tracing: unit edges between filled/empty cells chained
    into closed loops. numpy fast path iterates only filled cells."""
    segs = {}
    def add(a, b): segs.setdefault(a, []).append(b)
    if _NP is not None and hasattr(mask, "dtype"):
        ys, xs = _NP.nonzero(mask)
        filled = set(zip(xs.tolist(), ys.tolist()))
        for x, y in zip(xs.tolist(), ys.tolist()):
            if y == 0 or (x, y-1) not in filled:   add((x, y), (x+1, y))
            if y == h-1 or (x, y+1) not in filled: add((x+1, y+1), (x, y+1))
            if x == 0 or (x-1, y) not in filled:   add((x, y+1), (x, y))
            if x == w-1 or (x+1, y) not in filled: add((x+1, y), (x+1, y+1))
    else:
        for y in range(h):
            row = mask[y]
            up = mask[y-1] if y else None
            dn = mask[y+1] if y < h-1 else None
            for x in range(w):
                if not row[x]:
                    continue
                if y == 0 or not up[x]:        add((x, y), (x+1, y))
                if dn is None or not dn[x]:    add((x+1, y+1), (x, y+1))
                if x == 0 or not row[x-1]:     add((x, y+1), (x, y))
                if x == w-1 or not row[x+1]:   add((x+1, y), (x+1, y+1))
    loops = []
    while segs:
        start = next(iter(segs))
        loop = [start]; cur = start
        while True:
            ends = segs.get(cur)
            if not ends:
                break
            nxt = ends.pop()
            if not ends:
                del segs[cur]
            loop.append(nxt); cur = nxt
            if cur == start:
                break
        loops.append(loop)
    return loops


def _simplify(pts, eps):
    """Douglas-Peucker (closed aware): keep the shape, drop redundant vertices."""
    if len(pts) < 5:
        return pts
    closed = pts[0] == pts[-1]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        ax, ay = pts[i]; bx, by = pts[j]
        dx, dy = bx - ax, by - ay
        L2 = dx*dx + dy*dy or 1e-9
        dmax, idx = -1.0, -1
        for k in range(i+1, j):
            px, py = pts[k]
            t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / L2))
            vx, vy = ax + t*dx - px, ay + t*dy - py
            d = vx*vx + vy*vy
            if d > dmax:
                dmax, idx = d, k
        if dmax > eps*eps:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    out = [p for p, k in zip(pts, keep) if k]
    if closed and out and out[0] != out[-1]:
        out.append(out[0])
    return out


def _smooth_closed_path(poly):
    """Closed polygon as a smooth quadratic path (relative commands, compact)."""
    n = len(poly)
    x0, y0 = (poly[0][0]+poly[1][0])/2, (poly[0][1]+poly[1][1])/2
    d = [f"M{x0:.1f} {y0:.1f}"]
    x, y = x0, y0
    for i in range(1, n+1):
        cx, cy = poly[i % n]
        nx, ny = poly[(i+1) % n]
        mx, my = (cx+nx)/2, (cy+ny)/2
        d.append(f"q{cx-x:.1f} {cy-y:.1f} {mx-x:.1f} {my-y:.1f}")
        x, y = mx, my
    return " ".join(d) + "z"


def vectorize_photo(photo, levels=44, grid_w=None, eps=0.28, min_area=3, edges=True):
    """Posterize the actual photo into stacked vector layers - whatever the photo
    contains (city, people, food, flowers) becomes real editable geometry.
    Watercolor mode: many fine tonal layers glazed at partial opacity, with soft
    edges - detail stays visible instead of flattening into screen-print blocks.
    A final edge layer adds light ink lines along strong contours.
    Grid resolution is adaptive: portraits/people get a finer grid (faces need
    the resolution), landscapes stay at the proven-coherent density."""
    from PIL import ImageFilter, ImageOps
    im = photo.convert("RGB")
    w0, h0 = im.size
    if grid_w is None:
        # probe skin coverage at low res to pick the grid
        pw = 120
        ph = max(2, round(pw * h0 / w0))
        probe = im.resize((pw, ph), Image.LANCZOS)
        pd = list(probe.getdata())
        ns = 0
        for r, g, b in pd:
            mx, mn = max(r, g, b), min(r, g, b)
            y = 0.299*r + 0.587*g + 0.114*b
            cr = 128 + (r - y) * 0.713
            cb = 128 + (b - y) * 0.564
            if r > 95 and g > 40 and b > 20 and mx - mn > 15 and abs(r-g) > 12 \
                    and r > g and r > b and 133 <= cr <= 177 and 77 <= cb <= 127:
                ns += 1
        grid_w = 800 if ns / float(len(pd)) > 0.02 else 640
    gh = max(2, round(grid_w * h0 / w0))
    # extreme aspect ratios must not explode the cell grid (see GRID_CELLS_NP):
    # shrink grid_w proportionally to the offending factor, recompute gh from the
    # photo's aspect, then step down until both the cell budget and the long-side
    # cap hold. Normal 3:2 / 2:3 photos do not enter this branch.
    cap_cells = GRID_CELLS_NP if _NP is not None else GRID_CELLS_PY
    if gh > GRID_SIDE_MAX or grid_w * gh > cap_cells:
        f = min(1.0,
                GRID_SIDE_MAX * w0 / float(h0 * grid_w),
                math.sqrt(cap_cells * w0 / float(h0 * grid_w * grid_w)))
        grid_w = max(2, int(grid_w * f))
        gh = max(2, round(grid_w * h0 / w0))
        while grid_w > 2 and (gh > GRID_SIDE_MAX or grid_w * gh > cap_cells):
            grid_w -= 1
            gh = max(2, round(grid_w * h0 / w0))
        if gh > GRID_SIDE_MAX:
            # pathological strip (> ~1000:1) where no grid_w satisfies both caps:
            # clamp the long side; such an input cannot read as a postcard anyway
            gh = GRID_SIDE_MAX
    small = im.resize((grid_w, gh), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.3))
    # add colour depth so layers stay visible on paper (poster inks are richer
    # than camera JPEGs): saturate + slight contrast before quantizing
    from PIL import ImageEnhance
    small = ImageEnhance.Color(small).enhance(1.45)
    small = ImageEnhance.Contrast(small).enhance(1.15)

    def _poster_index(small, grid_w, gh, levels, skin=None):
        """Quantize into MANY fine tonal layers (watercolor glazing), light
        majority-filter only, then optional skin consolidation for faces."""
        q = small.quantize(colors=levels)
        pal = q.getpalette()
        qc = {ci: cnt for cnt, ci in q.getcolors()}
        # watercolor: keep neighbouring tones separate for gradient depth -
        # only fold near-duplicates (quantization noise) together
        reps = []            # (representative index, rgb)
        lut = list(range(256))
        for ci in sorted(qc, key=lambda c: -qc[c]):
            rgb = tuple(pal[ci*3:ci*3+3])
            for rj, rrgb in reps:
                if sum((rgb[i]-rrgb[i])**2 for i in range(3)) ** 0.5 < 18:
                    lut[ci] = rj
                    break
            else:
                reps.append((ci, rgb))
        idx = Image.frombytes("L", (grid_w, gh), q.tobytes()).point(lut)
        # filter radius scales with grid density so physical smoothing stays constant
        fs = grid_w / 480.0
        m1 = max(3, round(9 * fs)) | 1
        m2 = max(3, round(7 * fs)) | 1
        m3 = max(3, round(5 * fs)) | 1
        idx = idx.filter(ImageFilter.MedianFilter(m1)).filter(ImageFilter.MedianFilter(m2)).filter(ImageFilter.MedianFilter(m3))
        if skin is not None:
            # faces get a gentler consolidation than poster mode (keep some form)
            sm = skin.filter(ImageFilter.GaussianBlur(2 * fs)).filter(ImageFilter.GaussianBlur(2 * fs))
            idx = Image.composite(idx.filter(ImageFilter.MedianFilter(max(3, round(9 * fs)) | 1)), idx, sm)
            # re-quantize tones inside the skin mask into <=6 flat-ish inks
            if _NP is not None:
                sa = _NP.asarray(sm)
                ia = _NP.asarray(idx)
                used = {int(k): int(v) for k, v in
                        zip(*_NP.unique(ia[sa > 128], return_counts=True))}
            else:
                msk = sm.load(); pxp = idx.load()
                used = {}
                for y in range(gh):
                    for x in range(grid_w):
                        if msk[x, y] > 128:
                            v = pxp[x, y]
                            used[v] = used.get(v, 0) + 1
            if used:
                sreps = []       # (rep index, rgb)
                slut = list(range(256))
                for ci in sorted(used, key=lambda c: -used[c]):
                    rgb = tuple(pal[ci*3:ci*3+3])
                    for rj, rrgb in sreps:
                        if sum((rgb[i]-rrgb[i])**2 for i in range(3)) ** 0.5 < 55:
                            slut[ci] = rj
                            break
                    else:
                        if len(sreps) < 6:
                            sreps.append((ci, rgb))
                        else:
                            # 7th+ tone: fold into nearest existing rep
                            best = min(sreps, key=lambda t: sum((rgb[i]-t[1][i])**2 for i in range(3)))
                            slut[ci] = best[0]
                idx = Image.composite(idx.point(slut), idx, sm)
        return idx, pal

    # skin mask (classic RGB rule + YCrCb range) at grid resolution
    def _skin_mask(im, grid_w, gh):
        s = im.resize((grid_w, gh), Image.LANCZOS)
        if _NP is not None:
            a = _NP.asarray(s).astype(_NP.int16)
            r, g, b = a[..., 0], a[..., 1], a[..., 2]
            mx = a.max(axis=2); mn = a.min(axis=2)
            y = 0.299*r + 0.587*g + 0.114*b
            cr = 128 + (r - y) * 0.713
            cb = 128 + (b - y) * 0.564
            m = ((r > 95) & (g > 40) & (b > 20) & (mx - mn > 15) & (_NP.abs(r-g) > 12)
                 & (r > g) & (r > b) & (cr >= 133) & (cr <= 177) & (cb >= 77) & (cb <= 127))
            return Image.fromarray((m * 255).astype(_NP.uint8))
        data = list(s.getdata())
        mask = Image.new("L", (grid_w, gh), 0)
        md = mask.load()
        for i, (r, g, b) in enumerate(data):
            mx, mn = max(r, g, b), min(r, g, b)
            y = 0.299*r + 0.587*g + 0.114*b
            cr = 128 + (r - y) * 0.713
            cb = 128 + (b - y) * 0.564
            if r > 95 and g > 40 and b > 20 and mx - mn > 15 and abs(r-g) > 12 \
                    and r > g and r > b and 133 <= cr <= 177 and 77 <= cb <= 127:
                md[i % grid_w, i // grid_w] = 255
        return mask

    skin = _skin_mask(im, grid_w, gh)
    idx, pal = _poster_index(small, grid_w, gh, levels, skin)
    # busy scenes (street markets, crowds) stay fragmented even after filtering;
    # measure neighbour-transition rate and re-quantize with fewer levels if so
    for _ in range(3):
        if _NP is not None:
            a0 = _NP.asarray(idx)
            trans = int(_NP.count_nonzero(a0[:, 1:] != a0[:, :-1])
                        + _NP.count_nonzero(a0[1:, :] != a0[:-1, :]))
        else:
            pxp = idx.load()
            trans = 0
            for y in range(gh):
                row_prev = -1
                for x in range(grid_w):
                    v = pxp[x, y]
                    if v != row_prev:
                        trans += 1
                        row_prev = v
                    if y and v != pxp[x, y-1]:
                        trans += 1
        frag = trans / float(2 * grid_w * gh)
        if frag <= 0.30 or levels <= 12:
            break
        levels = max(12, levels // 2)
        idx, pal = _poster_index(small, grid_w, gh, levels, skin)
    # recount palette usage on the cleaned map
    if _NP is not None:
        arr = _NP.asarray(idx)
        cnts = _NP.bincount(arr.ravel(), minlength=256)
        order = [int(ci) for ci in _NP.argsort(-cnts) if cnts[ci] >= min_area]
    else:
        px = idx.load()
        counts = {}
        for y in range(gh):
            for x in range(grid_w):
                counts[px[x, y]] = counts.get(px[x, y], 0) + 1
        order = [ci for ci, cnt in sorted(counts.items(), key=lambda kv: -kv[1]) if cnt >= min_area]
    min_poly_a = 2 * (grid_w / 170.0) ** 2    # drop leftover speckle polygons
    eps_eff = eps                                # constant in cells = finer physical detail at high grid

    def _build_layers(order, pal):
        built = []
        for ci in order:
            cnt = int(cnts[ci]) if _NP is not None else counts[ci]
            color = tuple(pal[ci*3:ci*3+3])
            if _NP is not None:
                mask = (arr == ci)
            else:
                mask = [[1 if px[x, y] == ci else 0 for x in range(grid_w)] for y in range(gh)]
            polys = []
            for lp in _trace_mask(mask, grid_w, gh):
                if len(lp) < 8:
                    continue
                if len(lp) > 600:
                    # pre-decimate huge contours: Douglas-Peucker is near-quadratic
                    # on 10k-point outlines once closing merged regions
                    lp = lp[::max(1, len(lp) // 600)]
                s = _simplify(lp, eps_eff)
                if len(s) < 3:
                    continue
                a = abs(sum(s[i][0]*s[(i+1) % len(s)][1] - s[(i+1) % len(s)][0]*s[i][1]
                            for i in range(len(s)))) / 2.0
                if a >= min_poly_a:
                    polys.append(s)
            if polys:
                built.append((cnt, color, polys))
        return built

    # polygon-count adaptation: a busy scene (market, crowd) shatters into
    # 1000+ fragments that read as visual noise even when the transition-rate
    # metric stays low. Re-quantize with fewer tones until the panel is
    # composed of a sane number of washes (or we hit the floor).
    layers = _build_layers(order, pal)
    for _ in range(3):
        npoly = sum(len(l[2]) for l in layers)
        if npoly <= 450 or levels <= 10:
            break
        levels = max(10, int(levels * 0.6))
        idx, pal = _poster_index(small, grid_w, gh, levels, skin)
        if _NP is not None:
            arr = _NP.asarray(idx)
            cnts = _NP.bincount(arr.ravel(), minlength=256)
            order = [int(ci) for ci in _NP.argsort(-cnts) if cnts[ci] >= min_area]
        else:
            px = idx.load()
            counts = {}
            for y in range(gh):
                for x in range(grid_w):
                    counts[px[x, y]] = counts.get(px[x, y], 0) + 1
            order = [ci for ci, cnt in sorted(counts.items(), key=lambda kv: -kv[1]) if cnt >= min_area]
        layers = _build_layers(order, pal)
    if edges:
        # ink contours: strong luminance edges, THIN LINES ONLY - big filled blobs
        # (which soft/foggy photos produce) are discarded so they can't muddy the poster.
        # Median + blur BEFORE edge detection keeps contours coherent instead of
        # fragmenting into speckle noise at fine grids.
        gray = im.convert("L").resize((grid_w, gh), Image.LANCZOS)
        gray = gray.filter(ImageFilter.MedianFilter(3)).filter(ImageFilter.GaussianBlur(0.6))
        e = gray.filter(ImageFilter.FIND_EDGES).filter(ImageFilter.GaussianBlur(0.4))
        thr = 52
        if _NP is not None:
            ea = _NP.asarray(e)
            mask = ea > thr
        else:
            mask = [[1 if e.getpixel((x, y)) > thr else 0 for x in range(grid_w)] for y in range(gh)]
        # area cutoff scales with grid density (cells² -> physical size shrinks as grid grows)
        max_a = 140 * (grid_w / 170.0) ** 2 * 1.5
        polys = []
        for lp in _trace_mask(mask, grid_w, gh):
            if len(lp) < 80 * (grid_w / 480.0):   # drop short speckle fragments
                continue
            s = _simplify(lp, eps * 0.7)
            if len(s) < 3:
                continue
            a = abs(sum(s[i][0]*s[(i+1) % len(s)][1] - s[(i+1) % len(s)][0]*s[i][1]
                        for i in range(len(s)))) / 2.0
            if a <= max_a:                     # thin strokes only (cells²)
                # region boundaries (huge cloud/sky rims) are long AND span most
                # of the panel - they are not detail lines, skip them
                xs0 = [p[0] for p in s]; ys0 = [p[1] for p in s]
                bw = max(xs0) - min(xs0); bh = max(ys0) - min(ys0)
                if bw * bh > 0.09 * grid_w * gh:
                    continue
                polys.append((len(s), s))
        # busy scenes produce hundreds of fragments - keep only the LONGEST
        # ~30 contours so the keylines read as drawn lines, not scribble
        polys.sort(key=lambda t: -t[0])
        kept = []
        sm = skin.load() if skin is not None else None
        for _, s in polys[:30]:
            if sm is not None:
                # never draw keylines across faces/skin - they scramble features
                cx = sum(p[0] for p in s) / len(s)
                cy = sum(p[1] for p in s) / len(s)
                if 0 <= int(cx) < grid_w and 0 <= int(cy) < gh and sm[int(cx), int(cy)] > 128:
                    continue
            kept.append(s)
        if kept:
            layers.append((0, "EDGE", kept))
    return layers, grid_w, gh, idx, pal


def _wc_tone(color, paper, k=0.10, chroma=0.95):
    """Watercolor pigment on white paper is translucent AND weak: every tone
    lifts toward the paper and loses some chroma toward its own luminance.
    Without this the washes print like opaque poster ink."""
    yl = 0.299*color[0] + 0.587*color[1] + 0.114*color[2]
    return tuple(int(round((yl + (color[i] - yl) * chroma) * (1 - k) + paper[i] * k))
                 for i in range(3))


def vector_art_svg(art, x0, y0, bw, bh, paper=None):
    """Paint the vectorized layers into a target box (cover-fit, centre crop).
    Colours close to the paper tone are left UNPAINTED - screen-print style:
    the paper itself provides the highlights, so negative space reads whole.

    One path per tone layer with fill-rule="evenodd": _trace_mask returns outer
    rings AND hole rings, so filling each ring separately plugged every hole with
    the same dark tone and erased the bright detail the lighter layer beneath had
    already painted. evenodd makes hole rings subtract, letting the wash show."""
    if not art:
        return ""
    layers, gw, gh, idx, pal = art
    # screen-print order: lightest ink first, darkest last - dark silhouettes
    # land on top and read clean; EDGE keyline always last
    def _lum(c):
        return (c[0]*3 + c[1]*6 + c[2]) / 10.0 if c != "EDGE" else -1.0
    layers = sorted(layers, key=lambda l: -_lum(l[1]))
    scale = max(bw / gw, bh / gh)
    ox = x0 + (bw - gw * scale) / 2
    oy = y0 + (bh - gh * scale) / 2
    out = []
    for cnt, color, polys in layers:
        if color == "EDGE":
            # watercolor: soft light ink lines, not heavy screen-print keylines
            fill, op = hexc(darken(INK, 12)), 0.5
        else:
            fill, op = hexc(color), 1.0
            if paper is not None:
                d = sum((color[i]-paper[i])**2 for i in range(3)) ** 0.5
                if d < 40:
                    # near-paper tone (fog decks, bright cloud): paint it as a
                    # barely-there wash instead of leaving pure paper. Pure
                    # negative space cuts a hard white fault line through the
                    # subject; a 88%-paper tint keeps the airy feel without the
                    # discontinuity.
                    fill = hexc(tuple(int(round(color[i]*0.12 + paper[i]*0.88)) for i in range(3)))
        subpaths = []
        for poly in polys:
            if paper is not None and color != "EDGE":
                # drop true specks and THIN SLIVERS of any tone. The sliver
                # threshold is 3*(gw/170) grid cells; cover-fitted into the ~62mm
                # art panel that works out to roughly 1.1-1.6mm depending on the
                # photo's aspect (not the "sub-0.3mm" this comment used to claim);
                # the threshold itself is unchanged on purpose.
                a = abs(sum(poly[i][0]*poly[(i+1) % len(poly)][1] -
                            poly[(i+1) % len(poly)][0]*poly[i][1]
                            for i in range(len(poly)))) / 2.0
                xs0 = [q[0] for q in poly]; ys0 = [q[1] for q in poly]
                thin = min(max(xs0)-min(xs0), max(ys0)-min(ys0)) < 3.0 * (gw / 170.0)
                if a < 1.5 * (gw / 170.0) ** 2 or thin:
                    continue
            pts = [(ox+pxx*scale, oy+pyy*scale) for pxx, pyy in poly]
            subpaths.append(_smooth_closed_path(pts))
        if not subpaths:
            continue
        if color == "EDGE":
            # keylines are individual strokes: keep drawing ring by ring
            for dpath in subpaths:
                out.append(f'<path d="{dpath}" fill="{fill}" opacity="{op}"/>')
        else:
            # outer rings + hole rings in one path; evenodd XORs the holes out
            dall = " ".join(subpaths)
            out.append(f'<path d="{dall}" fill="{fill}" fill-rule="evenodd" opacity="{op}"/>')
            # (edge-darkening rims removed: on pale washes they accumulated into
            # grimy outlines; tonal contrast between washes carries the form)
    return "\n".join(out)


def wc_stroke_svg(art, x0, y0, bw, bh, seed=11, paper=None):
    """Generative watercolor strokes: thousands of tiny translucent round-cap
    dabs painted ALONG the local gradient direction, in the panel's own palette.
    Layered over the flat tonal regions they give the picture its hand-painted
    tooth - flat fills alone always read as a child's colouring book."""
    layers, gw, gh, idx, pal = art
    scale = max(bw / gw, bh / gh)
    ox = x0 + (bw - gw * scale) / 2
    oy = y0 + (bh - gh * scale) / 2
    # per-cell luminance from the palette
    if _NP is not None:
        ia = _NP.asarray(idx)
        palr = _NP.asarray(pal, dtype=_NP.float64).reshape(-1, 3)[:len(pal)//3]
        lum = (palr[ia][..., 0]*3 + palr[ia][..., 1]*6 + palr[ia][..., 2]) / 10.0
    else:
        px = idx.load()
        lum = [[(pal[px[x, y]*3]*3 + pal[px[x, y]*3+1]*6 + pal[px[x, y]*3+2]) / 10.0
                for x in range(gw)] for y in range(gh)]
    rnd = random.Random(seed)
    out = []
    fs = gw / 480.0
    for step, alpha in ((4.0 * fs, 0.20), (7.0 * fs, 0.15)):
        y = step * 0.5
        row = 0
        while y < gh - 1:
            x = step * 0.5 + (rnd.random() * step * 0.5 if row % 2 else 0.0)
            while x < gw - 1:
                xi, yi = int(x), int(y)
                lx = lum[yi][min(gw-1, xi+1)] - lum[yi][max(0, xi-1)]
                ly = lum[min(gh-1, yi+1)][xi] - lum[max(0, yi-1)][xi]
                g = (lx*lx + ly*ly) ** 0.5
                lc = lum[yi][xi] if not _NP else float(lum[yi, xi])
                if g > 60 or lc < 70:
                    # detail area or dark wash: the tonal layers already carry
                    # the structure - strokes there only add murk
                    x += step * (0.85 + rnd.random() * 0.3)
                    continue
                ang = math.atan2(ly, lx) + math.pi / 2.0   # run along the edge
                ln = step * (0.9 + rnd.random() * 0.9)
                dx, dy = math.cos(ang) * ln * 0.5, math.sin(ang) * ln * 0.5
                cx = x + math.cos(ang + 0.7) * ln * 0.28
                cy = y + math.sin(ang + 0.7) * ln * 0.28
                ci = int(ia[yi, xi]) if _NP is not None else px[xi, yi]
                col = hexc(_wc_tone(tuple(pal[ci*3:ci*3+3]), paper)) if paper is not None else hexc(tuple(pal[ci*3:ci*3+3]))
                # flat areas (low gradient) get denser, longer strokes - that is
                # where a wash needs its broken-pigment tooth
                dens = 1.0 + 0.9 * (1.0 - min(1.0, g / 45.0))
                ln *= 1.0 + 0.5 * (dens - 1.0) / 1.4
                out.append(
                    f'<path d="M{ox+(x-dx)*scale:.2f} {oy+(y-dy)*scale:.2f} '
                    f'Q{ox+cx*scale:.2f} {oy+cy*scale:.2f} '
                    f'{ox+(x+dx)*scale:.2f} {oy+(y+dy)*scale:.2f}" '
                    f'stroke="{col}" stroke-width="{(0.9+rnd.random()*0.7)*step*0.56*scale:.2f}" '
                    f'fill="none" stroke-linecap="round" '
                    f'opacity="{alpha*(0.75+rnd.random()*0.5):.2f}"/>')
                x += step * (0.85 + rnd.random() * 0.3) / dens
            y += step * (0.85 + rnd.random() * 0.3)
            row += 1
    return "\n".join(out)


# ---------- motif (faithful ridge_motif transcription) ----------
RIDGE_SIL = [(0.0,19.5),(2.5,15.0),(5.5,16.5),(8.0,12.0),(10.5,14.0),(13.0,7.5),
             (15.5,10.5),(17.0,12.5),(19.8,3.5),(22.0,8.5),(24.5,10.5),(27.0,9.0),
             (29.5,12.0),(31.5,11.0),(34.0,13.5),(36.0,19.5)]


def ridge_motif_svg(palette, origin, scale, style, paper):
    ox, oy = origin
    kn = SKNOBS.get(style or "default", SKNOBS["default"])
    dark = palette[1]
    clt = cloud_tone(palette, paper)
    body = tuple(int(min(255, int(dark[i]*0.35 + 246*0.65) + 18)) for i in range(3))
    sil = [(ox + x*scale, oy + y*scale) for x, y in RIDGE_SIL]
    base_y = oy + 22.5*scale
    pts = lambda pp: " ".join(f"{x:.2f},{y:.2f}" for x, y in pp)
    out = []
    out.append(f'<polygon points="{pts(sil + [(x, base_y) for x, _ in reversed(sil)])}" '
               f'fill="{hexc(body)}" opacity="{kn["body"]/255:.3f}"/>')
    shadow = [(ox+19.8*scale,oy+3.5*scale),(ox+22.0*scale,oy+8.5*scale),(ox+24.5*scale,oy+10.5*scale),
              (ox+36*scale,oy+19.5*scale),(ox+36*scale,oy+22.5*scale),(ox+19.8*scale,oy+22.5*scale)]
    out.append(f'<polygon points="{pts(shadow)}" fill="{hexc(dark)}" opacity="{kn["draw_shadow"]/255:.3f}"/>')
    snow = [(ox+15.5*scale,oy+10.5*scale),(ox+17.0*scale,oy+12.5*scale),(ox+19.8*scale,oy+3.5*scale),(ox+19.8*scale,oy+13.0*scale)]
    snow_a = 215/255 if kn["body"] > 150 else 150/255
    out.append(f'<polygon points="{pts(snow)}" fill="{hexc(palette[2])}" opacity="{snow_a:.3f}"/>')
    # ridge contour + inner facet lines
    lc = darken(dark, kn["line_x"])
    out.append(f'<polyline points="{pts(sil)}" fill="none" stroke="{hexc(lc)}" '
               f'stroke-width="{kn["line_w"]:.2f}" stroke-linejoin="round"/>')
    for a, b in [((ox+8.0*scale,oy+12.0*scale),(ox+13.0*scale,oy+7.5*scale)),
                 ((ox+13.0*scale,oy+7.5*scale),(ox+19.8*scale,oy+3.5*scale)),
                 ((ox+22.0*scale,oy+8.5*scale),(ox+27.0*scale,oy+9.0*scale))]:
        out.append(f'<line x1="{a[0]:.2f}" y1="{a[1]:.2f}" x2="{b[0]:.2f}" y2="{b[1]:.2f}" '
                   f'stroke="{hexc(darken(dark, kn["line_x"]+10))}" stroke-width="{kn["line_w"]*0.58:.2f}"/>')
    # watercolour clouds (soft blurred ellipses)
    for (ccx, ccy, crx, cry, al) in [(ox+6.0*scale, oy+1.0*scale, 4.6*scale, 1.9*scale, kn["cl"]),
                                     (ox+17.5*scale, oy+0.2*scale, 5.0*scale, 2.1*scale, kn["cl"]+4),
                                     (ox+28.0*scale, oy+1.4*scale, 4.2*scale, 1.8*scale, kn["cl"]-2)]:
        out.append(f'<ellipse cx="{ccx:.2f}" cy="{ccy:.2f}" rx="{crx:.2f}" ry="{cry:.2f}" '
                   f'fill="{hexc(clt)}" opacity="{max(0,min(1,al/255)):.2f}" filter="url(#cloudblur)"/>')
    out.append(f'<ellipse cx="{ox+31.5*scale:.2f}" cy="{oy+16.5*scale:.2f}" rx="{2.6*scale:.2f}" ry="{1.1*scale:.2f}" '
               f'fill="{hexc(clt)}" opacity="{max(0,min(1,(kn["cl"]-40)/255)):.2f}" filter="url(#cloudblur)"/>')
    return "\n".join(out)


def pines_svg(x0, x1, y_base, height, color, seed=22, count=0):
    """Exact draw_pines transcription (same rng, same inset math)."""
    r = random.Random(seed)
    n = count or max(4, int((x1-x0)/4.4))
    inset = min(height * 1.15 * 0.62, (x1-x0) * 0.45)
    step = ((x1-x0) - 2*inset) / n
    out = []
    for i in range(n):
        cx = x0 + inset + step*(i+0.5)
        h = height * r.uniform(0.7, 1.15)
        w = h*0.62
        out.append(f'<rect x="{cx-0.4:.2f}" y="{y_base-1.3:.2f}" width="0.8" height="1.3" fill="{hexc(color)}"/>')
        for k, frac in enumerate([1.0, 0.72, 0.5]):
            tw = w*frac; th = h*0.5; basey = y_base - k*(h*0.3); topy = basey - th
            out.append(f'<polygon points="{cx-tw:.2f},{basey:.2f} {cx:.2f},{topy:.2f} {cx+tw:.2f},{basey:.2f}" fill="{hexc(color)}"/>')
    return "\n".join(out)


def birds_svg(pts, size=2.4, color=None):
    color = color or darken(INK, 20)
    out = []
    for (x, y) in pts:
        s = size
        out.append(f'<polyline points="{x-s:.2f},{y:.2f} {x:.2f},{y-s*0.6:.2f} {x+s:.2f},{y:.2f}" '
                   f'fill="none" stroke="{hexc(color)}" stroke-width="0.4" stroke-linejoin="round"/>')
    return "\n".join(out)


# ---------- front: draw_picture layout, artwork = vectorized photo (any subject) ----------
def front_svg(embed_href, palette, title, date_str="", ph_ratio=None, paper=None,
              tfont=None, location="", style="default", swatches=True, number="N° 01",
              tstyle="normal", photo=None, art_mode="vector", **_):
    ph_ratio = PH_RATIO if ph_ratio is None else ph_ratio
    paper = paper or IVORY
    ink = ink_for(paper)
    # vectorize the real photo once (falls back to the hand-drawn motif on failure)
    art = None
    if art_mode == "vector":
        try:
            src = photo
            if src is None and embed_href.startswith("data:image"):
                src = Image.open(io.BytesIO(base64.b64decode(embed_href.split(",", 1)[1])))
            if src is not None:
                art = vectorize_photo(src)
        except Exception:
            # never silently hand the user clipart: keep the fallback, but make
            # the failure visible in the server log
            traceback.print_exc()
            art = None
    r = [svg_open(paper)]
    r.append(f'<rect x="0" y="0" width="{CW}" height="{CH}" fill="{hexc(paper)}"/>')
    r.append(paper_texture())
    r.append('<g id="photo" inkscape:label="照片">')
    # photo box: left 9, top 9, width 46, max height 62, frame gap 2mm, 0.5mm stroke
    pw, phh = 46.0, 46.0*ph_ratio
    if phh > 62.0:
        phh = 62.0; pw = phh/ph_ratio
    r.append(photo_el(embed_href, 9.0, 9.0, pw, phh, "pcutF"))
    r.append(f'<rect x="7" y="7" width="{pw+4:.2f}" height="{phh+4:.2f}" fill="none" '
             f'stroke="{hexc(ink)}" stroke-width="0.5"/>')
    r.append('</g>')
    # artwork panel: the posterized photo itself (not fixed clipart)
    r.append('<g id="motif" inkscape:label="画区元素">')
    if art:
        ax0, ay0, aw0, ah0 = 82.0, 8.0, 62.0, 62.0
        r.append(f'<clipPath id="artF"><rect x="{ax0}" y="{ay0}" width="{aw0}" height="{ah0}" rx="1.2"/></clipPath>')
        # watercolor bleed: a hair of blur softens every layer edge
        r.append(f'<g clip-path="url(#artF)" filter="url(#wcBleed)">{vector_art_svg(art, ax0, ay0, aw0, ah0, paper=paper)}\n</g>')
        # same 2mm paper gap + ink frame as the photo box, so both panels read as one family
        r.append(f'<rect x="{ax0-2}" y="{ay0-2}" width="{aw0+4}" height="{ah0+4}" rx="1.2" fill="none" '
                 f'stroke="{hexc(ink)}" stroke-width="0.5"/>')
    else:
        r.append(birds_svg([(99, 30), (110, 26), (121, 32)], size=2.4, color=darken(ink, 20)))
        r.append(ridge_motif_svg(palette, (84, 28), 1.25, style, paper))
        if SKNOBS.get(style or "default", SKNOBS["default"])["draw_forest"]:
            r.append(pines_svg(82, 141, y_base=70, height=11, color=darken(palette[1], 8), seed=22))
    r.append('</g>')
    # metadata (10,80): title 11pt sp1.2 / subtitle 5pt / DATE 4.8pt / N° 5.5pt
    mx, my = 10.0, 80.0
    r.append('<g id="metadata" inkscape:label="文字信息">')
    if title:
        r.append(text_el(mx, my, title, ink, 11, spacing=1.2, tfont=tfont,
                         weight="bold" if "bold" in (tstyle or "") else None))
        r.append(text_el(mx, my+4.8, "- ABOVE THE CLOUDS -", darken(ink, 45), 5.0, spacing=0.9))
    dd = ("DATE · " + date_str) if date_str else "LOCATION · DATE"
    r.append(text_el(mx, my+9.6, dd, darken(ink, 40), 4.8, spacing=0.7, tfont=tfont))
    r.append(text_el(mx, my+14.5, number or "N° 01", darken(ink, 55), 5.5, spacing=0.9))
    r.append('</g>')
    # swatches (118,76) d2.6 step5.4, outlined
    if swatches:
        r.append('<g id="swatches" inkscape:label="配色样本">')
        for i, col in enumerate(palette):
            c = 118.0 + i*5.4
            r.append(f'<circle cx="{c+1.3:.2f}" cy="{76+1.3:.2f}" r="1.3" fill="{hexc(col)}" '
                     f'stroke="{hexc(darken(col,40))}" stroke-width="0.09"/>')
        r.append('</g>')
    r.append('</svg>')
    return "\n".join(r)


# ---------- back: draw_mail transcription ----------
def back_svg(embed_href, palette, title, idx="N° 01", ph_ratio=None, paper=None,
             tfont=None, location="", swatches=True, **_):
    ph_ratio = PH_RATIO if ph_ratio is None else ph_ratio
    paper = paper or IVORY
    ink = ink_for(paper)
    # NOTE: no pre-escaping here - text_el() escapes exactly once. Escaping here
    # too used to print "&amp;" literally on the back and shorten the size step.
    r = [svg_open(paper)]
    r.append(f'<rect x="0" y="0" width="{CW}" height="{CH}" fill="{hexc(paper)}"/>')
    r.append(paper_texture())
    b = 6.0
    r.append(f'<rect x="{b}" y="{b}" width="{CW-2*b}" height="{CH-2*b}" fill="none" '
             f'stroke="{hexc(fade(ink, paper, .50))}" stroke-width="0.4"/>')
    # artwork panel (left): photo 26x32 at (11,40), thin frame
    pw, phh = 26.0, 32.0
    ax, ay = 11.0, 40.0
    r.append('<g id="photo" inkscape:label="照片">')
    r.append(f'<clipPath id="pcutB"><rect x="{ax}" y="{ay}" width="{pw}" height="{phh}"/></clipPath>'
             f'<image xlink:href="{embed_href}" href="{embed_href}" x="{ax}" y="{ay}" width="{pw}" height="{phh}" '
             f'preserveAspectRatio="xMidYMid slice" clip-path="url(#pcutB)"/>')
    r.append(f'<rect x="{ax}" y="{ay}" width="{pw}" height="{phh}" fill="none" '
             f'stroke="{hexc(fade(ink, paper, .55))}" stroke-width="0.5"/>')
    r.append('</g>')
    r.append('<g id="metadata" inkscape:label="文字信息">')
    ty = ay + phh + 3.0
    n = len(title or "")
    sz = 9.0 if n <= 10 else (7.0 if n <= 18 else (5.5 if n <= 26 else 4.6))
    sp = 1.0 if n <= 10 else 0.5
    if title:
        r.append(text_el(ax, ty, title, ink, sz, spacing=sp, tfont=tfont))
    yy = ty + 4.4
    if idx:
        r.append(text_el(ax, yy, idx, fade(ink, paper, .22), 5, spacing=0.9)); yy += 4.6
    if location:
        r.append(text_el(ax, yy, location, fade(ink, paper, .40), 4.4, spacing=0.5, tfont=tfont)); yy += 4.6
    for i, col in enumerate(palette):
        c = ax + i*4.8
        r.append(f'<circle cx="{c+1.2:.2f}" cy="{yy+1.2:.2f}" r="1.2" fill="{hexc(col)}" '
                 f'stroke="{hexc(darken(col,40))}" stroke-width="0.09"/>')
    r.append('</g>')
    r.append('<g id="mail" inkscape:label="邮寄面元素">')
    # 6 red square post-code boxes (top-left)
    bx, by, sq, gap = 10.0, 10.5, 9.0, 1.6
    for i in range(6):
        x0 = bx + i*(sq+gap)
        r.append(f'<rect x="{x0:.1f}" y="{by}" width="{sq}" height="{sq}" fill="none" '
                 f'stroke="{hexc(POSTAL_RED)}" stroke-width="0.6"/>')
    # stamp box (top-right 20x20)
    sx, sy, sw, sh = 124.0, 6.0, 20.0, 20.0
    r.append(f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" fill="none" '
             f'stroke="{hexc(fade(ink, paper, .45))}" stroke-width="0.5"/>')
    r.append(text_el(sx+2.5, sy+8.5, "贴邮票处", fade(ink, paper, .30), 7, tfont="kai"))
    r.append(text_el(sx+13.5, sy+1.0, "80", POSTAL_RED, 5, spacing=0.4))
    # address writing lines (right, 10mm spacing)
    for i in range(4):
        yy2 = 40 + i*10
        r.append(f'<line x1="68" y1="{yy2}" x2="144" y2="{yy2}" '
                 f'stroke="{hexc(fade(ink, paper, .68))}" stroke-width="0.09"/>')
    # barcode area + postal-code label
    r.append(f'<rect x="48" y="76" width="40" height="18" fill="none" '
             f'stroke="{hexc(fade(ink, paper, .62))}" stroke-width="0.09"/>')
    r.append(text_el(50, 82.0, "条码区", fade(ink, paper, .45), 4.5))
    r.append(text_el(108, 84, "邮政编码：", fade(ink, paper, .30), 6.5, tfont="song"))
    r.append('</g>')
    r.append('</svg>')
    return "\n".join(r)


def read_date(photo):
    ex = photo.getexif()
    raw = ex.get(36867) or ex.get(306)
    if not raw:
        return ""
    return str(raw).strip().split(" ")[0].replace(":", "-")


def emit(photo_path, title, out_base, **kw):
    ph = Image.open(photo_path).convert("RGB")
    palette = sample_palette(ph)
    date_str = read_date(ph)
    href = embed(photo_path)
    ratio = ph.size[1] / ph.size[0]
    number = kw.pop("number", "N° 01")
    for side, fn in (("front", front_svg), ("back", back_svg)):
        path = f"{out_base}-{side}.svg"
        if side == "front":
            open(path, "w", encoding="utf-8").write(fn(href, palette, title, date_str, ph_ratio=ratio, number=number, **kw))
        else:
            open(path, "w", encoding="utf-8").write(fn(href, palette, title, idx=number, ph_ratio=ratio, **kw))
        print("wrote", path)


if __name__ == "__main__":
    import os, sys
    args = sys.argv[1:]
    if len(args) < 2:
        print('usage: python make_postcard_svg.py <photo> <title> [out-base="postcard"]')
        raise SystemExit(2)
    out = args[2] if len(args) > 2 else os.path.join(os.path.dirname(os.path.abspath(args[0])), "postcard")
    emit(args[0], args[1], out)
