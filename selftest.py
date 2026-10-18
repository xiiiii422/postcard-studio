# -*- coding: utf-8 -*-
"""Postcard Studio 自检：渲染引擎正确性 + 端到端 HTTP 冒烟（无需 AI 密钥，无需先启服务）。

    python selftest.py            # 全部检查（约 30s）
    python selftest.py --quick    # 跳过 600dpi/并发压力项

为什么值得存在：它第一次跑就抓出了两个直接 HTTP 500（glass_b 非数字、layout 缺字段），
这两类问题在页面上只表现为"点生成没反应"，靠肉眼看不出来。
"""
import base64, contextlib, io, json, os, sys, threading, time, uuid, zipfile
import xml.etree.ElementTree as ET
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
QUICK = "--quick" in sys.argv
PORT = int(os.environ.get("POSTCARD_TEST_PORT", "5199"))   # never production 5179; override if busy
BASE = "http://127.0.0.1:%d" % PORT

ok, fail = [], []


def check(name, cond, extra=""):
    (ok if cond else fail).append(name)
    print("%-30s %s  %s" % (name, "PASS" if cond else "FAIL", extra))


# ============================ A. 渲染引擎（离线，不碰网络）============================
from PIL import Image, ImageOps, ImageChops, ImageDraw, ImageFilter
import make_postcard as M

photo = ImageOps.exif_transpose(Image.open(os.path.join(ROOT, "photo-A.img")).convert("RGB"))
print("Pillow", __import__("PIL").__version__, "| Python", sys.version.split()[0], "| photo", photo.size)

t = time.time(); pal = M.sample_palette(photo); dt = time.time() - t
check("sample_palette 三项配色", len(pal) == 3 and all(0 <= v <= 255 for c in pal for v in c), "%.4fs" % dt)
assert dt < 1.0, "palette sampling regressed to a full-resolution pixel scan"

IVORY, DARK = M.PAPER_SKINS["ivory"], M.PAPER_SKINS["midnight"]
ARGS = dict(dpi=210, tfont=M.CJK_SONG, tstyle="normal", number="N° 01", location="测试地点")

front = M.render_image(photo, pal, IVORY, ["mountain", "clouds"], "TEST 测试", "2026-08-01", **ARGS)
back = M.render_mail(photo, pal, IVORY, number="N° 01", title="TEST 测试", dpi=210, location="测试地点")
check("手绘正面/背面尺寸", front.size == (int(round(150*210/25.4)), int(round(100*210/25.4))) == back.size,
      "%s %s" % (front.size, back.size))
check("深色纸张可渲染", M.render_image(photo, pal, DARK, ["mountain", "moon"], "深夜测试", "", **ARGS).size == front.size)
check("手机壁纸 9:16", M.render_wallpaper(photo, pal, "TEST", "", ["moon"], paper=DARK).size == (1080, 1920))

vp = Image.new("RGB", (600, 900), (120, 90, 60))               # a fake portrait AI artwork
tf = M.render_t2i(photo, pal, vp, "TEST", "", paper=DARK, rounded=True, orient="portrait", **ARGS)
tb = M.render_t2i_mail(vp, pal, number="N° 01", title="TEST", photo=photo, dpi=210, paper=DARK)
check("竖版卡片确实更窄", tf.size[0] < tf.size[1], "%s（背面 %s 恒为横版）" % (tf.size, tb.size))

LAY = [{"id": "photo", "x": 9, "y": 9, "w": 46, "h": 30, "rot": 5},
       {"id": "mountain", "x": 84, "y": 28, "w": 45, "h": 28, "rot": 8},
       {"id": "metadata", "x": 10, "y": 80, "w": 34, "h": 14, "rot": 0},
       {"id": "swatches", "x": 118, "y": 76, "w": 18, "h": 3, "rot": 0}]
check("画布 layout 可渲染", M.render_image(photo, pal, IVORY, [], "LAYOUT", "", dpi=210, layout=LAY).size == front.size)

if not QUICK:
    JOBS = [(210, "auto"), (600, "landscape"), (300, "portrait"), (150, "auto"), (600, "portrait"), (300, "landscape")]
    got, errs = {}, []

    def work(i, dpi, orient):
        try:
            cw, ch = M.card_dims(photo.size[1] / photo.size[0], orient)
            hi = M.render_t2i(photo, pal, vp, "并发", "", dpi=dpi, tfont=M.CJK_SONG, orient=orient)
            mb = M.render_t2i_mail(vp, pal, number="", title="并发", photo=photo, dpi=dpi, paper=DARK)
            if hi.size != (int(round(cw*dpi/25.4)), int(round(ch*dpi/25.4))):
                errs.append("job%d %s" % (i, hi.size))
            if mb.size != (int(round(150*dpi/25.4)), int(round(100*dpi/25.4))):
                errs.append("job%d mail %s" % (i, mb.size))
            got[i] = hi.size
        except Exception as e:
            errs.append("job%d raised %r" % (i, e))
    t = time.time()
    ts = [threading.Thread(target=work, args=(i, d, o)) for i, (d, o) in enumerate(JOBS)]
    [x.start() for x in ts]; [x.join() for x in ts]
    check("6 线程混 DPI/朝向", not errs, "%.2fs %s" % (time.time() - t, errs or "sizes exact"))

# scope() must hand the module globals back untouched, or a later render inherits someone else's DPI
check("全局已还原", (M.MM, M.PXW, M.PXH, M.CW, M.CH, round(M.PX_PER_PT, 6)) == (
    300/25.4, int(round(150*300/25.4)), int(round(100*300/25.4)), 150.0, 100.0, round(300/72.0, 6)),
      str((M.CW, M.CH)))

import make_postcard_svg as S
href = S.embed(photo)                                   # takes the PIL image: nothing on disk
check("SVG embed 免临时文件", href.startswith("data:image/jpeg;base64,"))
bad = S.front_svg(href, S.sample_palette(photo), 'A & B <script>', "2026-08-01", ph_ratio=1.5)
check("SVG 标题转义", "<script>" not in bad and "&amp;" in bad and "&lt;" in bad)
check("SVG 不写全局 PH_RATIO", S.PH_RATIO == 0.75)


def image_w(svg):
    for seg in svg.splitlines():
        if "<image " in seg:
            return float(seg.split('width="')[1].split('"')[0])


check("SVG 朝向按参数走", image_w(S.front_svg(href, pal, "P", "", ph_ratio=1.5))
      < image_w(S.front_svg(href, pal, "L", "", ph_ratio=0.6)))

# ============ A1. SVG 画区真缺陷：孔洞 / 极端长宽比 / 背面转义 / 控制字符 / 配色 ============
# 这几条都对着“跑得出、尺寸也对、但画面/文件是错的”缺陷：孔洞轮廓被同一深色实心重填
# 把亮部盖掉、极端长宽比把网格撑到几百万格、背面标题二次转义、标题里的控制字符让整份
# XML 直接解析失败、纯色暗图把最亮色兜底成 (0,0,0)。

# 背面标题只能转义一次（旧实现 back_svg 先 esc 一次、text_el 再 esc 一次）
back_t = S.back_svg(href, pal, "A & B <c>")
check("SVG 背面标题只转义一次",
      back_t.count("&amp;") == 1 and "&amp;amp;" not in back_t
      and "<c>" not in back_t and "&lt;" in back_t and "&gt;" in back_t,
      "&amp; x%d / 无 &amp;amp; / 无裸 <c>" % back_t.count("&amp;"))

# 标题里的 XML 1.0 非法控制字符必须先剔除，否则 ElementTree / 浏览器整份拒解析
ctl = "BAD\x01TITLE\x0b\x0c\x1fEND"
ctl_front = S.front_svg(href, pal, ctl, "", art_mode="motif")   # 不走矢量化，只测文本
ctl_back = S.back_svg(href, pal, ctl)
ctl_ok = True
for _s in (ctl_front, ctl_back):
    try:
        ET.fromstring(_s.encode("utf-8"))
    except Exception as _e:
        ctl_ok = False
        print("  control-char parse error:", _e)
check("SVG 标题控制字符可解析", ctl_ok and "\x01" not in ctl_front + ctl_back
      and "\x0b" not in ctl_front + ctl_back,
      "ElementTree 解析通过、非法字符已剔除")

# 孔洞：暗色结构里包一块亮部 -> 暗层必须同时保留外环+孔洞环，且按层 evenodd 合成
hole_src = Image.new("RGB", (300, 300), (232, 226, 214))
_hd = ImageDraw.Draw(hole_src)
_hd.rectangle([70, 60, 230, 240], fill=(34, 38, 44))
_hd.ellipse([110, 100, 190, 180], fill=(228, 220, 205))
hole_art = S.vectorize_photo(hole_src, grid_w=300, levels=24)
_hole_layers = hole_art[0]
_dark = [l for l in _hole_layers if l[1] != "EDGE" and sum(l[1]) / 3.0 < 90]
hole_svg = S.vector_art_svg(hole_art, 82.0, 8.0, 62.0, 62.0, paper=(246, 241, 231))
check("SVG 孔洞：暗层保留内外环", bool(_dark) and any(len(l[2]) > 1 for l in _dark),
      "暗层多边形数 %s（外环+孔洞环）" % [len(l[2]) for l in _dark])
_dfill = S.hexc(_dark[0][1]) if _dark else "#000000"
_i = hole_svg.find('fill="%s"' % _dfill)
if _i > 0:
    _j = hole_svg.rfind("<path ", 0, _i)
    _dseg = hole_svg[_j:hole_svg.find("/>", _i) + 2]
else:
    _dseg = ""
check("SVG 孔洞：同层 evenodd 合成",
      'fill-rule="evenodd"' in _dseg and _dseg.count("M") >= 2
      and hole_svg.count('fill-rule="evenodd"') > 0,
      "暗层一条 evenodd path 含 %d 个子路径" % _dseg.count("M"))

# 极端长宽比：200x2000 不再无上限放大网格（旧实现 640x6400 = 4.1M cells）
ext_src = Image.new("RGB", (200, 2000), (120, 110, 100))
_ed = ImageDraw.Draw(ext_src)
for _yy in range(0, 2000, 100):
    _ed.rectangle([10, _yy + 10, 190, _yy + 90], fill=(_yy % 255, 80, 40))
ext_art = S.vectorize_photo(ext_src)
_cap = S.GRID_CELLS_NP if S._NP is not None else S.GRID_CELLS_PY
ext_svg = S.svg_open((246, 241, 231)) + S.vector_art_svg(ext_art, 9, 9, 46, 46) + "</svg>"
_ext_xml = True
try:
    ET.fromstring(ext_svg.encode("utf-8"))
except Exception as _e:
    _ext_xml = False
    print("  extreme-aspect XML error:", _e)
check("SVG 极端长宽比网格受限",
      ext_art[1] * ext_art[2] <= _cap and ext_art[2] <= S.GRID_SIDE_MAX and _ext_xml,
      "%dx%d=%d cells ≤ %d / gh %d ≤ %d / XML 合法"
      % (ext_art[1], ext_art[2], ext_art[1] * ext_art[2], _cap,
         ext_art[2], S.GRID_SIDE_MAX))

# 纯色暗图：没有 lum>150 的像素时最亮色不能退回 (0,0,0)
mono = Image.new("RGB", (64, 64), (40, 45, 50))
mono_pal = S.sample_palette(mono)
check("SVG 纯色暗图配色兜底",
      len(mono_pal) == 3 and mono_pal[2] != (0, 0, 0) and max(mono_pal[2]) > 0,
      "brightest=%s（旧实现退回 (0,0,0)）" % (mono_pal[2],))

# ================== A2. 像素级判据：底板过渡 / 元素墨 / 旋转一致性 ==================
# A 段那些检查只看“跑不跑得通、尺寸对不对”，而下面这批缺陷全都跑得出、尺寸也对：
# 天色底板四条边印成硬线、暗角每次都抛 TypeError 被 except 吞掉（等于从来没画过）、
# 日落那团暖光印成一片灰、元素只转 1° 就凭空长出一圈白点、旋转照片的边框离照片 4mm。
# 只有把两张卡逐像素相减才看得见，所以这一节全是像素测量，每条都带“退回旧写法必须变红”的对照。
TH = 12                       # 亮度差 12/255 算“看得见”：纸纹本身的逐像素噪声在 10 以下
LUMF = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
NAMES = [n for n in M.ELEMENT_BOXES if n not in ("metadata", "swatches")]
_ORIG = {k: getattr(M, k) for k in ("draw_layout_element", "draw_sky_grad", "draw_vignette",
                                    "watercolor_ellipse", "SoftTile", "ROT_RESAMPLE",
                                    "framed_photo_sub", "ELEM_PAD")}


@contextlib.contextmanager
def swap(**kw):
    """临时改掉 make_postcard 的全局（负向对照用）。出来必须还原：后面的检查还靠它。"""
    old = {k: getattr(M, k) for k in kw}
    for k, v in kw.items():
        setattr(M, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(M, k, v)


def backdrop_card(dpi, bg=True, els=(), **patch):
    """只画了底板的卡。同一张卡上多画一笔，两图相减就是那一笔本身。
    patch 里的名字是临时换成负向变异的 make_postcard 全局。"""
    with swap(**patch):
        with M.scope(dpi):
            cv = M.paper_canvas(1, IVORY)
            if bg:
                M.draw_composed(cv, photo, pal, [], "T", "", layout=[], paper=IVORY)
            else:
                M.draw_backdrop(cv, pal, list(els), ())
    return cv


BASECARD = backdrop_card(210)                                  # 别叫 BASE：那是 B 段的 HTTP 基址
BASEX = BASECARD.convert("RGB")
MDPI = BASECARD.width / 150.0                     # 210dpi 下每毫米多少像素


def ink_box(img, ref=BASEX, th=TH):
    """看得见的墨的包围盒（mm），无墨返回 None。point 二值化 + getbbox 都是 C 实现，
    比逐像素扫一遍一个数量级还快（一张 210dpi 的卡 1240x826）。"""
    b = ImageChops.difference(img, ref).convert("L").point(lambda v: 255 if v >= th else 0).getbbox()
    if not b:
        return None
    m = img.width / 150.0
    return (b[0] / m, b[1] / m, b[2] / m, b[3] / m)


def el_card(name, rot=0, ref=BASECARD, bg=IVORY):
    b = M.ELEMENT_BOXES[name]
    cv = ref.copy()
    with M.scope(210):
        M.draw_layout_element(cv, pal, "default", name, (b[0], b[1], b[2], b[3]), None,
                              rot=rot, paper=bg)
    return cv.convert("RGB")


def over_mm(name, a):
    """可见墨比默认框多出几毫米（left/top/right/bottom）。"""
    if not a:
        return (9.0, 9.0, 9.0, 9.0)
    b = M.ELEMENT_BOXES[name]
    return (max(0.0, b[0] - a[0]), max(0.0, b[1] - a[1]),
            max(0.0, a[2] - (b[0] + b[2])), max(0.0, a[3] - (b[1] + b[3])))


def rot_box(name):
    """转 1° 后的可见墨包围盒（走“先画透明小图再贴回卡片”那条路径）。"""
    return ink_box(el_card(name, rot=1), BASEX)


def _lost_mm(a, b):
    """b 相对 a 少掉的最大边距（mm）：小图把看得见的柔边裁掉了。"""
    if not a or not b:
        return 99.0
    return max(max(0.0, b[0] - a[0]), max(0.0, b[1] - a[1]),
               max(0.0, a[2] - b[2]), max(0.0, a[3] - b[3]))


def _grown_mm(a, b):
    """b 相对 a 多出的最大边距（mm）：旋转凭空长出来的东西（灰雾、插值过冲的白点）。"""
    if not a or not b:
        return 99.0
    return max(max(0.0, a[0] - b[0]), max(0.0, a[1] - b[1]),
               max(0.0, b[2] - a[2]), max(0.0, b[3] - a[3]))


rot_lost = lambda name, a: _lost_mm(a, rot_box(name))
rot_grown = lambda name, a: _grown_mm(a, rot_box(name))


def dark_grown(name, delta=20):
    """转 1° 后「直接画法没压暗纸、旋转后却把纸压暗了 delta 级以上」的像素数。
    paste 的逐通道线性混合会把 RGB 再乘一遍覆盖率，特征就是这种凭空多出来的暗；
    单比“直接 vs 旋转”不行，那会把两种重采样对半透明边的合理差异也算成灰雾。"""
    bl = BASEX.convert("L")
    dl = el_card(name, 0).convert("L")
    rl = el_card(name, 1).convert("L")
    haze = ImageChops.subtract(bl, rl).point(lambda v: 255 if v >= delta else 0)
    clean = ImageChops.subtract(bl, dl).point(lambda v: 0 if v else 255)
    return sum(ImageChops.multiply(haze, clean).histogram()[1:])   # multiply 是 8bit 定的，得用 0/255


_t0 = time.time()
_INK = {}
for n in NAMES:
    _img = el_card(n)
    _INK[n] = (ImageChops.difference(_img, BASEX).convert("L").getextrema()[1], ink_box(_img))
_ROTK = {n: rot_box(n) for n in NAMES}
_faint = min(NAMES, key=lambda n: _INK[n][0])
check("%d 项手绘元素都真有墨" % len(NAMES), all(v[0] >= 20 for v in _INK.values()) and len(_INK) >= 23,
      "最淡的是 %s，差别只有 %d/255" % (_faint, _INK[_faint][0]))
_out = {n: max(over_mm(n, _INK[n][1])) for n in NAMES}
_worst = max(_out, key=lambda n: _out[n])
check("手绘元素的墨不出默认框", max(_out.values()) <= 5.0,
      "最出框 %s %.1fmm（框是画布上唯一的操作手柄，墨跑出去就没法躲）" % (_worst, _out[_worst]))

_lost = {n: _lost_mm(_INK[n][1], _ROTK[n]) for n in NAMES}
_grown = {n: _grown_mm(_INK[n][1], _ROTK[n]) for n in NAMES}
_wlost = max(_lost, key=lambda n: _lost[n])
_wgrown = max(_grown, key=lambda n: _grown[n])
check("转 1° 不裁掉看得见的柔边", max(_lost.values()) <= 0.6,
      "最多 %s %.1fmm；柔边被切成直线时这里会上跳" % (_wlost, _lost[_wlost]))
check("转 1° 不凭空长出墨", max(_grown.values()) <= 2.5,
      "最多 %s %.1fmm（1° 的几何量本就 ~1mm，超出的那部分是插值过冲/灰雾）" % (_wgrown, _grown[_wgrown]))
# 负向对照：把旋转的重采样退回 BICUBIC，再把小图的合成退回 paste 线性混合。
with swap(ROT_RESAMPLE=Image.BICUBIC):
    _g_bicubic = rot_grown("boat", _INK["boat"][1])
with swap(SoftTile=Image.Image):
    _g_naive, _d_naive = rot_grown("sun", _INK["sun"][1]), dark_grown("sun")
_haze = {n: dark_grown(n) for n in ("sun", "water", "mountain", "clouds")}
_hz = max(_haze, key=_haze.get)
check("转 1° 不出灰雾", max(_haze.values()) <= 1500,
      "最多 %s 有 %d 个像素被往黑里插；去掉 SoftTile 后同一指标是 %d" % (_hz, _haze[_hz], _d_naive))
check("负向对照：BICUBIC 过冲会被抓到", _g_bicubic >= 2.0 and _g_bicubic > _grown["boat"],
      "船的水线只有 3px 宽，负瓣冲上 255 就转出一串比纸还亮的白点：%.1fmm vs 双线性 %.1fmm"
      % (_g_bicubic, _grown["boat"]))
check("负向对照：paste 灰雾会被抓到", _g_naive >= 5.0 and _d_naive > 3000,
      "去掉 SoftTile 后太阳转 1° 可见范围多 %.1fmm、%d 个像素被往黑里插" % (_g_naive, _d_naive))

# 夜空底下旋转元素还得看得见：小图是画在透明纸上的，那里看不到真实背景，
# 只能从卡上把落点那块的平均色取下来交给它，否则星点会按纸色误判成“亮底用暗墨”。
_night = Image.new("RGB", BASECARD.size, (34, 42, 70)).convert("RGBA")
NIGHT_REF = _night.convert("RGB")
_peak_night = {n: ImageChops.difference(el_card(n, rot=1, ref=_night), NIGHT_REF)
               .convert("L").getextrema()[1] for n in ("stars", "moon", "fireworks")}
_real_dle = M.draw_layout_element


def dle_no_bg_sample(canvas, palette, style, name, rect, photo_=None, rot=0, paper=None):
    """负向变异：旋转的小图看不到真实背景，只能拿纸色判断（修之前的写法）。"""
    if not rot:
        return _real_dle(canvas, palette, style, name, rect, photo_, rot, paper)
    dpi = max(150, int(round(M.MM * 25.4)))
    sub = M.element_sub_img(palette, style, name, rect[2], rect[3], dpi, photo_, M.IVORY) \
        .rotate(rot, expand=True, resample=M.ROT_RESAMPLE)
    canvas.paste(sub, (int(rect[0] * M.MM + rect[2] * M.MM / 2 - sub.width / 2),
                       int(rect[1] * M.MM + rect[3] * M.MM / 2 - sub.height / 2)), sub)


with swap(draw_layout_element=dle_no_bg_sample):
    _pk_old = {n: ImageChops.difference(el_card(n, rot=1, ref=_night, bg=DARK), NIGHT_REF)
               .convert("L").getextrema()[1] for n in ("stars", "moon")}
check("夜空上旋转的元素仍看得见", min(_peak_night.values()) >= 90,
      "最暗 %s %d/255；不取真实背景时只剩 %d（等于隐形）"
      % (min(_peak_night, key=_peak_night.get), min(_peak_night.values()), min(_pk_old.values())))
check("负向对照：不取真实背景会被抓到", min(_pk_old.values()) < 60,
      "退回纸色判断后 stars/moon 的峰值差只剩 %s" % _pk_old)


# ---- 天色底板：软过渡必须在打印分辨率下同样软 ----
SKY_LINES = (("bottom", 100.0, 2.0, 100.0, 56.0), ("top", 100.0, 0.5, 100.0, 16.0),
             ("left", 42.0, 25.0, 72.0, 25.0), ("right", 132.0, 25.0, 150.0, 25.0))


def profile(img, dpi, x0, y0, x1, y1, step=0.25):
    """沿一条线按 mm 取样亮度，返回 [(沿程 mm, 亮度)]。"""
    m = dpi / 25.4
    ln = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    n = max(2, int(round(ln / step)))
    out = []
    for i in range(n + 1):
        t = i / n
        px = min(max(int(round((x0 + (x1 - x0) * t) * m)), 0), img.width - 1)
        py = min(max(int(round((y0 + (y1 - y0) * t) * m)), 0), img.height - 1)
        out.append((t * ln, LUMF(img.getpixel((px, py)))))
    return out


def step_per_mm(pr, win=1.0):
    """不重叠 win mm 窗口的均值之差取最大，单位「级/mm」。
    纸纹是逐像素噪声，直接比单个像素会把噪声当硬边；按 1mm 窗口平均后才量得出真正的台阶。"""
    means, i = [], 0
    while i < len(pr):
        j = i
        while j < len(pr) and pr[j][0] - pr[i][0] < win:
            j += 1
        seg = pr[i:j]
        if len(seg) >= 2:
            means.append(sum(v for _, v in seg) / len(seg))
        i = j
    return max((abs(b - a) for a, b in zip(means, means[1:])), default=0.0)


def sky_steps(dpi, **patch):
    """四条剖面里最大的台阶（级/mm）。取最大而不是平均：硬边只要有一条边上存在就印得出来。"""
    cv = backdrop_card(dpi, bg=False, els=("sun",), **patch).convert("RGB")
    return max(step_per_mm(profile(cv, dpi, *seg[1:])) for seg in SKY_LINES)


_sky210 = sky_steps(210)
_sky600 = _sky210 if QUICK else sky_steps(600)        # --quick 守约定：600dpi 那档不跑
check("天色底板四边无硬边", _sky210 <= 4.0 and _sky600 <= 4.0 and _sky600 <= _sky210 + 1.2,
      "210dpi 最大 %.1f 级/mm，600dpi %s（打印档不许比预览更硬）"
      % (_sky210, "%.1f 级/mm" % _sky600 if not QUICK else "未跑（--quick）"))


def bad_sky_px(canvas, palette, has_sun=False, has_night=False):
    """负向变异：羽化半径写成像素数（旧写法）。6px 在 210dpi 是 0.72mm，在 600dpi 只剩 0.25mm。"""
    lay = Image.new("L", (M.PXW, M.PXH), 0)
    ImageDraw.Draw(lay).rectangle([58 * canvas.width / 150.0, 8 * canvas.height / 100.0,
                                   148 * canvas.width / 150.0, 42 * canvas.height / 100.0], fill=200)
    canvas.paste(Image.new("RGB", canvas.size, (255, 208, 148)), (0, 0),
                 lay.filter(ImageFilter.GaussianBlur(6)))


def bad_sky_poly(canvas, palette, has_sun=False, has_night=False):
    """负向变异：直角多边形平涂，四条边都是硬切口。"""
    M.fill_poly(canvas, [(58, 8), (148, 8), (148, 42), (58, 42)], (255, 208, 148), 200)


# 两个变异都拿 600dpi 量：那正是旧写法露馅的地方（210dpi 预览里它们肉眼看不出来）。
# --quick 不跑 600dpi，就退而在 210dpi 下对照（旧写法在 210dpi 也已超 4.0 级/mm）。
_mpx = sky_steps(210 if QUICK else 600, draw_sky_grad=bad_sky_px)
check("负向对照：按像素写羽化会被抓到", _mpx > 4.0,
      "%.0fdpi 下台阶 %.1f 级/mm（同一写法在 210dpi 只有 %.1f，这就是它当年骗过肉眼的原因）"
      % (210 if QUICK else 600, _mpx, sky_steps(210, draw_sky_grad=bad_sky_px)))
check("负向对照：直角平涂会被抓到", sky_steps(210, draw_sky_grad=bad_sky_poly) > 4.0,
      "%.1f 级/mm" % sky_steps(210, draw_sky_grad=bad_sky_poly))


# ---- 暗角：当年每次渲染都抛 TypeError 被 except 吞掉，这层从来没画出来过 ----
def vig_delta(dpi, vig=None):
    with M.scope(dpi):
        cv = M.paper_canvas(1, IVORY)
    if vig is None:
        with M.scope(dpi):
            M.draw_vignette(cv)
    m = dpi / 25.4
    sq = lambda b: LUMF(cv.crop(tuple(int(v * m) for v in b)).resize((1, 1), Image.BOX).getpixel((0, 0)))
    return sq((70, 46, 80, 54)) - sq((2, 2, 12, 10))


_vig210 = vig_delta(210)
_vig600 = _vig210 if QUICK else vig_delta(600)     # --quick 守约定：600dpi 那档不跑
_vig_off = vig_delta(210, vig=False)
check("暗角真的画了出来", _vig210 >= 8 and abs(_vig600 - _vig210) <= 3.0,
      "中心比四角亮 %.1f 级，600dpi %s（按画布尺寸归一，不随 DPI 变陡）"
      % (_vig210, "%.1f 级" % _vig600 if not QUICK else "未跑（--quick）"))
check("负向对照：暗角没画会被抓到", _vig_off < 1.0, "不画这层时差值 %.1f 级" % _vig_off)


# ---- 日落暖光：修掉 RGBA 黑底光晕后，这团光真的得是暖的、而且不许把纸印灰 ----
def warm_spots(cv, dpi=210):
    m = dpi / 25.4
    sq = lambda b: cv.crop(tuple(int(v * m) for v in b)).resize((1, 1), Image.BOX).getpixel((0, 0))
    return sq((98, 29, 110, 37)), sq((70, 28, 76, 38))      # 光斑中心 / 外侧一圈纸


def bad_wc(canvas, cx, cy, rx, ry, color, alpha, blur_mm=8, seed=2):
    """负向变异：在 RGBA 层里画完再整层高斯模糊（旧写法）。没画到的地方是透明的黑，一起糊进颜色里。"""
    pad = int(blur_mm * 1.8 * M.MM)
    box = (int(cx - rx) - pad, int(cy - ry) - pad, int(cx + rx) + pad, int(cy + ry) + pad)
    lay = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
    ImageDraw.Draw(lay).ellipse([pad, pad, lay.width - pad, lay.height - pad], fill=color + (alpha,))
    lay = lay.filter(ImageFilter.GaussianBlur(int(blur_mm * M.MM / 2)))
    tile = canvas.convert("RGB").crop(box)
    tile.paste(Image.alpha_composite(tile.convert("RGBA"), lay).convert("RGB"), (0, 0))
    canvas.paste(tile, (box[0], box[1]))


_no_wc = lambda *a, **k: None
_warm_core, _warm_ring = warm_spots(backdrop_card(210, bg=False, els=("sun",)).convert("RGB"))
_nw_core, _nw_ring = warm_spots(backdrop_card(210, bg=False, els=("sun",), watercolor_ellipse=_no_wc)
                                .convert("RGB"))
_mw_core, _mw_ring = warm_spots(backdrop_card(210, bg=False, els=("sun",), watercolor_ellipse=bad_wc)
                                .convert("RGB"))
check("日落暖光是暖的、且没把纸印灰",
      _warm_core[0] - _warm_core[2] >= _nw_core[0] - _nw_core[2] + 6
      and LUMF(_warm_core) >= LUMF(_nw_core) - 8
      and LUMF(_warm_ring) >= LUMF(_nw_ring) - 3,
      "光斑中心 R-B %.0f vs 不画暖光 %.0f，亮度 %.1f vs %.1f（暖色本来就暗一点，只许暗 8 级）"
      % (_warm_core[0] - _warm_core[2], _nw_core[0] - _nw_core[2], LUMF(_warm_core), LUMF(_nw_core)))
check("负向对照：RGBA 黑底光晕会被抓到",
      LUMF(_mw_core) < LUMF(_nw_core) - 8 or LUMF(_mw_ring) < LUMF(_nw_ring) - 3,
      "整层高斯模糊后光斑中心暗了 %.1f 级、外面那圈纸暗了 %.1f 级（就是那片灰）"
      % (LUMF(_nw_core) - LUMF(_mw_core), LUMF(_nw_ring) - LUMF(_mw_ring)))


# ---- 旋转照片：边框应当贴着照片，而不是套在旋转后的外接方框上 ----
PH_RECT = (9.0, 9.0, 46.0, 30.0)
_ph_box = (0, 0, int((PH_RECT[0] + PH_RECT[2] + 14.0) * MDPI),
           int((PH_RECT[1] + PH_RECT[3] + 14.0) * MDPI))
_ph_ref = BASECARD.crop(_ph_box).convert("RGB")


def _lm(img):
    return ImageChops.difference(img, _ph_ref).convert("L").point(lambda v: 255 if v >= TH else 0)


def photo_card(rot, framed, renderer=None):
    lay = [{"id": "photo", "x": PH_RECT[0], "y": PH_RECT[1], "w": PH_RECT[2], "h": PH_RECT[3],
            "rot": rot}]
    cv = BASECARD.copy()
    with M.scope(210):
        if renderer:
            renderer(cv, rot, framed)
            return cv.crop(_ph_box).convert("RGB")
        orig = M.framed_photo_sub
        if not framed:
            M.framed_photo_sub = lambda img, gap, border, ink: img.convert("RGBA")
        try:
            M.draw_composed(cv, photo, pal, [], "T", "", layout=lay, paper=IVORY)
        finally:
            M.framed_photo_sub = orig
    return cv.crop(_ph_box).convert("RGB")


def old_photo(cv, rot, framed):
    """负向变异：先转照片，再在它旋转后的外接方框外面画一个正放的框（旧画法）。"""
    ox, oy, s = M.place(PH_RECT, PH_RECT[2], PH_RECT[3])
    cw, chh = PH_RECT[2] * s, PH_RECT[3] * s
    img = photo.resize((int(cw * M.MM), int(chh * M.MM)), Image.LANCZOS).convert("RGBA")
    if rot:
        img = img.rotate(rot, expand=True, resample=Image.BICUBIC)
    px = int(ox * M.MM + cw * M.MM / 2 - img.width / 2)
    py = int(oy * M.MM + chh * M.MM / 2 - img.height / 2)
    cv.paste(img, (px, py), img)
    if framed:
        ImageDraw.Draw(cv).rectangle([px - 2 * M.MM, py - 2 * M.MM,
                                      px + img.width + 2 * M.MM, py + img.height + 2 * M.MM],
                                     outline=M.ink_for(IVORY), width=int(round(0.5 * M.MM)))
    return cv


def frame_gap(rot, renderer=None):
    """边框那一笔的墨离照片的墨有多远（mm，中位）。相减把边框单独隔离出来，
    再把照片的墨一圈圈膨胀（每圈 1px），数到第几圈盖住一半的边框墨就是那个距离。"""
    full = photo_card(rot, True, renderer)
    bare = photo_card(rot, False, renderer)
    pm = _lm(bare)
    fm = ImageChops.subtract(_lm(full), pm.filter(ImageFilter.MaxFilter(9)))
    n_total = sum(fm.histogram()[1:])
    if not n_total:
        return 99.0
    cur = pm
    for i in range(1, 40):
        cur = cur.filter(ImageFilter.MaxFilter(3))
        if sum(ImageChops.multiply(cur, fm).histogram()[1:]) >= 0.5 * n_total:
            return i / MDPI
    return 40 / MDPI


_fg = {r: frame_gap(r) for r in (30, 45)}
check("旋转照片的边框贴着照片", max(_fg.values()) <= 2.5,
      "转 %s° 时边框离照片中位 %.1f / %.1fmm（设计间隙 2mm）"
      % ("、".join(str(r) for r in _fg), _fg[30], _fg[45]))
_fgo = {r: frame_gap(r, old_photo) for r in (30, 45)}
check("负向对照：框画在外接方框上会被抓到", max(_fgo.values()) > 2.5,
      "旧画法转 45° 后中位 %.1fmm：角上几乎贴住照片，边中间空出一截，还会探出元素框压到别的图元"
      % _fgo[45])

check("A2 之后全局已还原", all(getattr(M, k) is v for k, v in _ORIG.items())
      and (M.MM, M.PXW, M.PXH, M.CW, M.CH) == (
          300 / 25.4, int(round(150 * 300 / 25.4)), int(round(100 * 300 / 25.4)), 150.0, 100.0)
      and isinstance(BASE, str) and PORT != 5179 and 1 <= PORT <= 65535 and callable(check),
      "%.1fs（连 B 段要用的 BASE/PORT/check 一起验：A2 里拿同名变量存过一张卡，HTTP 部分就整段静默跳过过）"
      % (time.time() - _t0))

import app
for label, blob in (("PDF 出血+裁剪线", app.build_print_pdf(front)),
                    ("PDF CMYK 多页", app.build_print_pdf_set([front, back], cmyk=True)),
                    ("A4 拼版", app.build_sheet_pdf(front))):
    check(label, blob[:4] == b"%PDF" and len(blob) > 10000, "%dKB" % (len(blob) // 1024))


# ============================ B. HTTP 端到端（进程内起服务）============================
FIELDS = {"title": "烟雾 & 测试", "number": "N° 01", "use_ai": "0", "want_back": "1",
          "style": "watercolor", "paper_skin": "ivory", "tfont": "cjk_song", "tstyle": "normal",
          "filt": "none", "rounded": "1", "show_date": "1", "swatches": "1", "enhance": "1",
          "pattern": "handdrawn", "orient": "auto", "elements": '["mountain","clouds","sun"]',
          "extra": "[]", "tags": "", "glass_a": "60", "glass_b": "3", "desc": ""}
PHOTO_BYTES = open(os.path.join(ROOT, "photo-A.img"), "rb").read()


def multipart(fields, filebytes):
    b = "----selftest" + uuid.uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode("utf-8"))
    buf.write(("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo.img\"\r\n"
               "Content-Type: application/octet-stream\r\n\r\n" % b).encode("utf-8"))
    buf.write(filebytes)
    buf.write(("\r\n--%s--\r\n" % b).encode("utf-8"))
    return buf.getvalue(), "multipart/form-data; boundary=" + b


def call(path, fields=None, method="GET"):
    hdr = {}
    data = None
    if fields is not None:
        data, ct = multipart(fields, PHOTO_BYTES)
        hdr["Content-Type"] = ct
    req = urllib.request.Request(BASE + path, data=data, headers=hdr, method="POST" if fields is not None else method)
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read()) if "json" in r.headers.get("Content-Type", "") else r.read()


from waitress import create_server
srv = create_server(app.app, host="127.0.0.1", port=PORT, threads=6)
_boot_err = None
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(60):
    try:
        call("/status"); break
    except Exception as e:
        _boot_err = e
        time.sleep(0.5)
else:
    print("服务未能启动，跳过 HTTP 部分：%r" % (_boot_err,)); srv.close(); sys.exit(1)

try:
    st = call("/status")
    check("/status 结构", set(st) == {"ai_key", "blocked", "models"}, "ai_key=%s（未配密钥时预期 False）" % st["ai_key"])
    check("/ 返回前端", b"styleNote" in call("/"))
    a = call("/analyze", {})
    check("/analyze 无密钥不报错", a.get("error") is None and a.get("note"), (a.get("note") or "")[:34])

    g = call("/generate", FIELDS)
    check("/generate 正+背", len(g["front"]) > 4000 and len(g["back"]) > 4000, "%.1fKB/%.1fKB" % (len(g["front"]) / 1024, len(g["back"]) / 1024))

    # Every AI pattern must degrade HONESTLY when no key is configured. `aibg` + 关键词 used to POST
    # with an empty "Bearer ", take a 401, and then tell the user “密钥被服务方拒绝，请在 .env 里
    # 填入有效 key” - while /status still said ai_key=False. Both the wording and the trip-wire were lies.
    for _pat, _bg in (("aibg", "keywords"), ("aibg", "photo"), ("t2i", ""), ("zine", "")):
        f = dict(FIELDS); f["pattern"] = _pat
        if _bg: f["bg_mode"] = _bg
        j = call("/generate", f)
        note = j.get("ai_note") or ""
        check("无密钥降级诚实 %s/%s" % (_pat, _bg or "-"), len(j["front"]) > 4000
              and "未配置" in note and "拒绝" not in note, note[:36])
    check("空密钥不应误发 401 熔断", not call("/status")["blocked"], "blocked 必须为空")

    for kind, ext, n in (("pngset", ".png", 3), ("svg", ".svg", 2), ("bundle", None, None),
                         ("print-cmyk", ".pdf", None), ("wallpaper", None, None), ("front-pdf", ".pdf", None)):
        f = dict(FIELDS); f["kind"] = kind
        blob = call("/bundle" if kind == "bundle" else "/export", f)
        if blob[:2] == b"PK":
            names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
            check("/" + kind, all(x.endswith(ext) for x in names) if ext else True, str(names))
            if kind == "bundle":
                note = zipfile.ZipFile(io.BytesIO(blob)).read("使用说明.txt").decode("utf-8")
                check("说明里的规格真实", "150×100mm" in note, [l for l in note.splitlines() if "规格" in l])
        else:
            magic = b"%PDF" if ext == ".pdf" else b"\x89PNG"
            check("/export " + kind, blob[:4] == magic, "%dKB" % (len(blob) // 1024))

    # a slider value is a free-form string from the browser: nothing here may answer 500
    dirty = dict(FIELDS)
    dirty.update({"glass_a": "999", "glass_b": "0.6", "crop": "not-json", "elements": "{[",
                  "extra": "null", "layout": "[{}]", "number": "", "orient": "sideways",
                  "bg_mode": "", "paper_skin": "nope", "tfont": "?"})
    check("脏参数不报错", len(call("/generate", dirty)["front"]) > 4000)
    # ...and the sanitizer must only throw away the bad entry, not the whole layout - a validator
    # that bails out on any dirt would silently reset the user's canvas arrangement.
    from app import _clean_layout, _json_list, _num
    check("_clean_layout 只丢坏项", _clean_layout('[{"id":"a","x":1,"y":2,"w":3,"h":4},{},'
                                                  '{"id":"b","x":0,"y":0,"w":0,"h":9}]') ==
          [{"id": "a", "x": 1.0, "y": 2.0, "w": 3.0, "h": 4.0, "rot": 0}], "坏项 2 / 保留 1")
    check("_num / _json_list 兜底", (_num("0.6", 3, 1, 10), _num("x", 3, 1, 10), _num("999", 3, 1, 10),
                                     _json_list("null"), _json_list('{['), _json_list("[1]")) ==
          (1, 3, 10, [], [], [1]))
    # The AI memo caches full RGB rasters (~4.7MB each) while waitress serves 12 threads, so it has
    # to stay capped - and the cap must be LRU, otherwise a busy run evicts the image the matching
    # export is about to reuse and the user pays for a second generation. Probe with a small cap so
    # the test is deterministic and never depends on the production value.
    import app as _a
    _saved, _saved_max = dict(_a._T2I_CACHE), _a._T2I_MAX
    try:
        _a._T2I_MAX = 3
        _a._T2I_CACHE.clear()
        for i in range(3):
            _a._memo("probe%d" % i, i)                    # probe0 is now the least recently used
        _a._memo_get("probe0")                            # touching it makes it the most recent
        _a._memo("probeX", -1)                            # ...so THIS must evict probe1
        check("AI 图像备忘按 LRU 淘汰", len(_a._T2I_CACHE) == 3 and "probe0" in _a._T2I_CACHE
              and "probe1" not in _a._T2I_CACHE and _a._memo_get("probeX") == -1
              and _a._memo_get(None) is None and _a._memo(None, "x") == "x",
              "3 键上限，读过 / 无键路径均正确")
        check("备忘上限足够小", 0 < _saved_max <= 16, "生产上限 %d 个 AI 图像（约 %dMB）"
              % (_saved_max, _saved_max * 5))
    finally:
        _a._T2I_MAX = _saved_max
        _a._T2I_CACHE.clear()
        _a._T2I_CACHE.update(_saved)
    good = dict(FIELDS); good["layout"] = json.dumps(LAY, ensure_ascii=False)
    px = base64.b64decode(call("/generate", good)["front"].split(",")[1])
    plain = base64.b64decode(g["front"].split(",")[1])
    check("合法 layout 仍生效", px != plain, "%dKB vs %dKB" % (len(px) // 1024, len(plain) // 1024))

    if not QUICK:
        res, err = [], []

        def hit():
            try:
                res.append(len(call("/generate", FIELDS)["front"]))
            except Exception as e:
                err.append(repr(e)[:70])
        t = time.time()
        ts = [threading.Thread(target=hit) for _ in range(4)]
        [x.start() for x in ts]; [x.join() for x in ts]
        check("4 路并发 /generate", len(res) == 4 and not err, "%.2fs %s" % (time.time() - t, err or ""))
finally:
    srv.close()


# ============================ C. 前端静态引用（单文件页面最容易烂的地方）============================
# index.html has no build step, so three classes of typo only show up as a silent JS error in the
# browser: an id that was never added (this is exactly how #styleNote broke "识别完成"), an
# unbalanced tag, and a script that does not even parse. All three are checked here instead.
import re
html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def tag_problem(src):
    """None when every non-void element closes in order; otherwise a description of where it breaks."""
    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>|<!--[\s\S]*?-->", "", src)
    stack = []
    for m in re.finditer(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^>]*/?)>", body):
        close, tag, rest = m.group(1), m.group(2).lower(), m.group(3)
        if tag in VOID or rest.endswith("/"):
            continue
        if close:
            if not stack or stack[-1] != tag:
                return "</%s> 处栈顶是 %s" % (tag, stack[-1] if stack else "(空)")
            stack.pop()
        else:
            stack.append(tag)
    return ("未闭合: " + ",".join(stack[:5])) if stack else None


def missing_ids(src):
    present = set(re.findall(r"\bid=\"([^\"']+)\"", src))
    used = set(re.findall(r"\$\('#([A-Za-z][\w-]*)'\)", src)) | \
           set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", src)) | \
           set(re.findall(r"querySelector\(['\"]#([A-Za-z][\w-]*)[\"']", src))
    return sorted(used - present), len(used)


def js_balance(src):
    """Parenthesis counts after stripping comments and string literals: a cheap proxy for 'this
    script was left half-written'. Parsing the JS with a Python compiler would be wrong - different
    language, and the goal is only to catch an unbalanced brace before it silently kills the page.

    The lookbehind is not decoration: a regex literal can end in an escaped slash (`/a` + backslash
    + two slashes), and reading that as a line comment deletes the rest of the line - parens and
    all - so the checker reported a broken page that node parsed happily. That false alarm costs
    more than no check."""
    clean = re.sub(r"(?<!\\)//[^\n]*|/\*[\s\S]*?\*/|(['\"`])(?:\\.|(?!\1)[^\n])*?\1", "", src)
    return {c: clean.count(c) - clean.count(p) for c, p in (("{", "}"), ("(", ")"), ("[", "]"))}

def lib_chips(src):
    """The hand-drawn library markup: (labels, labels carrying data-v, data-v sitting on an input)."""
    blk = re.search(r"id=\"libChips\"([\s\S]*?)</div>\s*</div>", src)
    inner = blk.group(1) if blk else ""
    labels = re.findall(r"<label[^>]*class=\"[^\"]*chip[^\"]*\"[^>]*>", inner)
    valued = [l for l in labels if "data-v=" in l]
    return labels, valued, re.findall(r"<input[^>]*data-v", inner)


def chip_css_problems(src):
    """The chips ARE the control surface of this page, and they are driven by a wrapped input. Two
    things silently undo keyboard support: hiding that input with `display:none` (which drops it out
    of the tab order entirely) and having no visible focus ring to show where the keyboard is.
    The ring has to be on `input:focus-visible`, not merely somewhere in the CSS: the template row is
    a <button> and its rule would otherwise count as covering the option chips that actually wrap an
    input - which is exactly the false pass this check has to refuse."""
    css = re.search(r"<style[\s\S]*?</style>", src)
    css = css.group(0) if css else ""
    bad = []
    for m in re.finditer(r"\.chip\s+input\s*\{([^}]*)\}", css):
        if re.search(r"display\s*:\s*none", m.group(1)):
            bad.append(".chip input 用了 display:none（键盘选不到任何选项）")
    if "input:focus-visible" not in css:
        bad.append("没有包着 input 的 :focus-visible 规则（选项焦点不可见）")
    if re.search(r"<button[^>]*class=\"[^\"]*\bchip\b", src) and "button.chip:focus-visible" not in css:
        bad.append("button 式 chip 没有焦点规则")
    return bad


def chip_group_problems(src):
    """syncChip() repaints a radio group through closest('.opt'); a group whose wrapper is not `.opt`
    would leave two options looking selected at once."""
    return [m.group(2) + " 不是 .opt 容器"
            for m in re.finditer(r"<div([^>]*\bid=\"(\w+(?:Chips|Opts))\"[^>]*)>", src)
            if "opt" not in m.group(1)]


def _js_of(src):
    """The inline script of this single-file page, as one searchable string."""
    return "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", src))


def _css_of(src):
    """The page's one <style> block, as one searchable string."""
    m = re.search(r"<style[\s\S]*?</style>", src)
    return m.group(0) if m else ""


def chip_state_owners(src):
    """Who is allowed to paint the `.on` highlight? Only the delegated `change` handler and the
    programmatic setters. A per-label CLICK handler that flips `checked` itself is a silent trap: the
    browser then finds the radio already selected when it runs the label's activation behaviour and
    fires no `change` at all, so every other consequence of the choice (the AI sub-switches, the crop
    ratio, autosave) stops running - while the highlight keeps on proving nothing is wrong. This exact
    leftover handler cost the mouse path of every radio group on the page."""
    js = _js_of(src)
    return [ln.strip()[:90] for ln in js.splitlines()
            if re.search(r"onclick|['\"]click['\"]", ln)
            and re.search(r"\.checked\s*=", ln)
            and re.search(r"classList\.(add|toggle)\(\s*['\"]on['\"]", ln)]


def chip_silent_setters(src):
    """Mirror image of chip_state_owners: that one catches a click that announces NOTHING by making
    the browser skip `change`; this catches code that mutates a chip's `checked` and then never fires
    `change` either. Presets were lost by exactly that - the template painted `.on` itself, so the
    chips looked perfect while the AI sub-switches kept their old visibility and localStorage kept the
    old style. `change` is the only hook that carries those consequences, so a setter must announce."""
    bad = []
    for ln in _js_of(src).splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("*"):
            continue                            # prose about the old bug is not an instance of it
        if re.search(r"\.checked\s*=(?!=)", s) and not re.search(r"announce\(|dispatchEvent\(|\.click\(", s):
            bad.append(s[:90])
    return bad


TEXT_FIELDS = ("title", "location", "desc", "tags", "glassA", "glassB", "cropZoom")


def silent_text_writes(src):
    """The same trap one door further along: assigning to `input.value` from code fires no `input`
    event, so a title the page picked for the user - the AI read, a template, the canvas rename - sat
    in the DOM unsaved and was gone after a reload unless something else happened to change at the
    same moment. setTxt() writes through the same `input` event that typing fires, so every
    consequence (readout label, autosave) runs. A line that assigns `.value` of one of the persisted
    fields without going through it is a regression of that bug."""
    bad = []
    for ln in _js_of(src).splitlines():
        s = ln.strip()
        if s.startswith("//") or s.startswith("*"):
            continue
        m = re.search(r"\$\('#(\w+)'\)\.value\s*=(?!=)", s)
        if m and m.group(1) in TEXT_FIELDS and "setTxt(" not in s:
            bad.append(s[:90])
    return bad


def canvas_fit_problems(src):
    """Every canvas drawn for the user has to size its backing store from its own box, or the browser
    resamples it twice and the whole surface goes soft (that is how the editor stayed blurry at 900px
    while the box it sat in was wider and the screen was 1.5x). fitCanvas() is the one helper that
    does it, so the count of drawing surfaces must not exceed the count of calls."""
    surfaces = len(re.findall(r"<canvas[\s>]", src))
    fitted = len(re.findall(r"fitCanvas\(", _js_of(src))) - 1        # -1: the definition itself
    return [] if fitted >= surfaces else ["%d 个画布 / 只有 %d 处定位 backing store" % (surfaces, fitted)]


def dialog_door_problems(src):
    """The overlays on this page used to be opened and closed by toggling a class from five different
    places, which broke every keyboard expectation at once - and a browser never warns you about any
    of it. Measured in a real browser before the fix: six controls inside two *closed* dialogs were in
    the tab chain (`opacity:0` still lays the subtree out and still focuses), Tab walked out of the
    open framing dialog into the page behind it, Escape did nothing at all, and closing dropped focus
    wherever it pleased. The rules below are each one of those four failures; a `visibility:hidden`
    dialog that nothing opens through openModal() is how they silently come back."""
    js = _js_of(src)
    bad = []
    by_hand = re.findall(r"\$\('#\w+'\)\.classList\.(?:add|remove)\('show'\)", js)
    if by_hand:
        bad.append("%d 处直接开关弹窗（绕过 openModal/closeModal）" % len(by_hand))
    if not re.search(r"e\.key==='Escape'", js) or "closeModal(" not in js:
        bad.append("没有 Esc 关闭")
    if "OPENER[id]=document.activeElement" not in js or "op.focus()" not in js:
        bad.append("关闭后不归还焦点")
    if "dialogFocusables(" not in js or "last.focus()" not in js:
        bad.append("Tab 不出弹窗（无焦点圈定）")
    if "setAttribute('aria-modal','true')" not in js or "aria-labelledby" not in js:
        bad.append("弹窗没有 dialog/aria-modal 语义")
    closed = re.search(r"\.modal\{([^}]*)\}", src)
    shown = re.search(r"\.modal\.show\{([^}]*)\}", src)
    if not closed or "visibility:hidden" not in closed.group(1):
        bad.append("隐藏的弹窗仍在 Tab 序列里（.modal 缺 visibility:hidden）")
    if not shown or "visibility:visible" not in shown.group(1):
        bad.append(".modal.show 未恢复 visibility")
    # only the markup, and only within one tag: `[^>]+` would happily span newlines and match a `<`
    # in the script below, so a JS `.onclick=` assignment would look like an inline attribute
    inline = re.findall(r"<[^>\n]*onclick=", src.split("<script")[0])
    if inline:
        bad.append("%d 处内联 onclick（属性处理器够不到 closeModal）" % len(inline))
    return bad


def dead_storage_keys(src):
    """A key that is only ever written is a promise nobody keeps: 「下次不再提示」wrote ps_welcomed,
    but the guide had already stopped auto-opening, so the checkbox changed a number no code ever read
    - a control that looks like a setting and does nothing. Every localStorage key the page touches
    must be read somewhere or it is leftover UI."""
    js = _js_of(src)
    written = set(re.findall(r"localStorage\.(?:setItem|removeItem)\(\s*['\"](\w+)['\"]", js))
    read = set(re.findall(r"localStorage\.getItem\(\s*['\"](\w+)['\"]", js))
    return sorted(written - read)


# Every surface on this page that is either styled away from its native look or made focusable by
# hand has to say out loud where the keyboard is. A generic `:focus` rule would not do: the browser
# only paints the ring on :focus-visible for the controls it decided are keyboard targets.
RING_SELECTORS = ("input[type=text]:focus-visible", "textarea:focus-visible",
                  ".drop:focus-visible", ".preview img:focus-visible")


def focus_ring_problems(src):
    """The base rule for the text fields says `outline:none` - lifted straight from a design mock,
    where nothing is ever tabbed to. The previous round put rings back on chips and buttons and left
    these four fields, the two keyboard-openable previews and the upload zone with nothing: focus did
    move, and showed nothing. Measured by tabbing to #title in a real browser and reading the outline
    the browser painted."""
    css = _css_of(src)
    return [s + " 没有 :focus-visible 规则" for s in RING_SELECTORS if s not in css]


def touch_target_problems(src):
    """Two controls whose hit box was decided by incidental CSS instead of stated as a rule, both
    found by measuring them on a 390px phone: a native range input is 16px tall, so the pixels 8px
    above and below its centre line belonged to the surrounding div and the framing slider was a grab
    you miss more often than you hit; and the header's guide button had an inline style halving
    .btn's padding, which put it at exactly the 24px floor. Enlarging the element box costs the
    native rendering nothing - verified against magnified screenshots before and after."""
    css = _css_of(src)
    bad = []
    m = re.search(r"input\[type=range\]\s*\{([^}]*)\}", css)
    h = re.search(r"height\s*:\s*(\d+)px", m.group(1)) if m else None
    if not h or int(h.group(1)) < 24:
        bad.append("滑杆命中高度 %s（<24px）" % (h.group(1) + "px" if h else "未声明"))
    for tag in re.finditer(r"<button[^>]*>", src):
        if "btn" in tag.group(0) and re.search(r'style="[^"]*padding', tag.group(0)):
            bad.append("按钮用内联样式压 padding（命中高度掉到 .btn 规则之外）")
            break
    return bad


def drop_problems(src):
    """The upload zone reads 「点击或拖入一张照片」 and delivered neither half of that to a keyboard,
    and nothing at all to a drag: an unhandled file drop makes the browser navigate the tab to the
    dropped file, so performing the advertised gesture threw away the photo, the parameters and the
    draft with it. The window has to swallow the default action wherever the drop lands; the zone
    alone is not enough, and the copy is a promise the code has to keep."""
    js = _js_of(src)
    bad = []
    if "window.addEventListener('dragover'" not in js or "window.addEventListener('drop'" not in js:
        bad.append("没有窗口级 dragover/drop 兜底（丢在上传区外仍会翻页）")
    if "addEventListener('drop'" not in js:
        bad.append("没有接收文件的 drop 处理")
    if not re.search(r"<div class=\"drop\"[^>]*tabindex=\"0\"", src):
        bad.append("上传区不可 Tab 聚焦")
    if not re.search(r"addEventListener\('keydown'[\s\S]{0,140}\$\('#photo'\)\.click\(\)", js):
        bad.append("上传区没有 Enter/Space 打开文件选择器")
    return bad


def floating_overlay_problems(src):
    """A position:fixed pill is painted above everything, and on a phone the content is exactly as
    wide as the screen. Measured at 390px: the countdown pill had two option chips sitting under it
    and took their taps - the chip looked fine and never received anything, because the element the
    browser found under the finger was the pill. Either the overlay yields (pointer-events:none) or
    the content gets out of the way (bottom padding); this page does both, and the rule below is the
    only thing that keeps the choice from being undone by the next cosmetic edit."""
    css = _css_of(src)
    # one media query per line, which is how this stylesheet is written; `.*` never crosses a newline
    lines = [b for b in re.findall(r"@media[^{]*\{(.*)", css) if ".peak-hint" in b]
    if not lines:
        return ["窄屏下没有任何针对浮层的规则"]
    block = " ".join(lines)
    bad = []
    if "pointer-events:none" not in block:
        bad.append("浮层仍会接管点击")
    if "padding-bottom" not in block:
        bad.append("页面底部没给浮层留位")
    return bad


def drag_capture_problems(src):
    """A surface that binds pointerdown + pointermove is a drag surface, and a drag that never
    captures its pointer is not finished when the user lets go - it is finished whenever the cursor
    happens to cross the border. pointer events are retargeted to whatever is underneath, so the
    pointerup can land on an element that has no handler for it, the page keeps its drag flag set,
    and the next bare hover moves the object again. Measured here by driving a real mouse: after
    releasing outside the canvas the photo element still answered to a hover with no button pressed.
    Both canvases are covered; the framing dialog at least had a window-level pointerup, the editor
    had nothing."""
    js = _js_of(src)
    bound = {}                                # target -> {event: line}
    for raw in js.splitlines():
        events = re.findall(r"\.addEventListener\('(pointer\w+)'", raw)
        if not events:
            continue
        target = re.split(r"\.addEventListener", raw.strip(), maxsplit=1)[0]
        for ev in events:
            bound.setdefault(target, {})[ev] = raw
    surfaces = {t: h for t, h in bound.items()
                if "pointerdown" in h and "pointermove" in h}
    if not surfaces:
        # the page is supposed to have two draggable canvases; none matching means the naming moved,
        # not that the bug was fixed - and a check that quietly stops looking is worse than none
        return ["没识别到任何拖拽面（pointerdown+pointermove），检查器在看空处"]
    bad = []
    for target, handlers in sorted(surfaces.items()):
        if "setPointerCapture" not in handlers["pointerdown"]:
            bad.append("%s 的拖拽没有捕获指针（滑出边界即失控）" % target)
        if "pointercancel" not in handlers and "window.addEventListener('pointerup'" not in js:
            bad.append("%s 的手势没有兜底收尾" % target)
    return bad


def nav_goto_problems(src):
    """setMode() rewrites the `data-goto` of the 下一步 / 上一步 buttons so 图生图 can hop over the
    recognition step. Finding those buttons BY that attribute is self-defeating: the first call changes
    it to "3", so the next call can no longer locate the button to change it back, and the stepper is
    left jumping 1 -> 3 for the rest of the session - visible only after someone has used one mode
    once, which is how it hid for so long.

    Only the descendant form is the trap. `.step[data-step="1"] [data-goto="2"]` reaches into a .nav
    row to pick a button out of it by a value the code then overwrites, while `.s-step[data-goto="2"]`
    names a stepper pill, whose `data-goto` IS its meaning (the generic click handler navigates by it)
    and nothing ever rewrites. So: flag `data-goto` used as a descendant filter."""
    js = _js_of(src)
    if not re.search(r"dataset\.goto\s*=", js):
        return []
    return [m.group(1) for m in re.finditer(r"""querySelector(?:All)?\(\s*(['"])(.*?)\1""", js)
            if ' [data-goto=' in m.group(2)]


def unreachable_chips(src):
    """Every .chip on this page is a control. One written as a <label> with no input behind it has
    nothing to focus, no Enter/Space activation, and shows up in the accessibility tree as plain text
    - that is how the 模板 row stayed mouse-only long after the option chips next to it were fixed.
    Assumes the one-line-per-chip markup this page actually uses (true for every chip today)."""
    return [ln.strip()[:80] for ln in src.splitlines()
            if re.search(r"<label[^>]*class=\"[^\"]*\bchip\b", ln) and "<input" not in ln]


def boxes_mismatch(src, table):
    """前端 const BOXES={…} 与后端 ELEMENT_BOXES 必须逐项相等。
    框是画布上唯一的操作手柄：前端少一项，那个元素在编辑器里选了也拖不动（buildEditor 里
    的 if(BOXES[v]) 会直接把它丢出画布）；数值不一致，用户拖出来的位置就和后端真正画出来的墨错位。"""
    m = re.search(r"const BOXES=\{([^}]*)\}", src)
    if not m:
        return ["前端没有 BOXES 表"]
    got = {k: [float(v) for v in vs.split(",")] for k, vs in re.findall(r"(\w+):\[([^\]]*)\]", m.group(1))}
    bad = []
    if set(table) - set(got):
        bad.append("前端缺 %s" % ",".join(sorted(set(table) - set(got))))
    if set(got) - set(table):
        bad.append("前端多 %s" % ",".join(sorted(set(got) - set(table))))
    bad += ["%s 前端 %s vs 后端 %s" % (k, got[k], list(table[k]))
            for k in sorted(got) if k in table and got[k] != [float(v) for v in table[k]]]
    return bad


prob = tag_problem(html)
check("HTML 标签配平", prob is None, prob or "")
miss, used_n = missing_ids(html)
check("JS 引用的 id 都存在", not miss, "引用 %d 个 / 缺失 %s" % (used_n, miss or "无"))
labels, valued, on_input = lib_chips(html)
check("手绘元素库带 data-v", bool(labels) and len(labels) == len(valued) and not on_input,
      "%d/%d 个 chip，input 上误挂 %d 个" % (len(valued), len(labels), len(on_input)))
js = "\n".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", html))
balance = js_balance(js)
check("内联 JS 括号配平", all(v == 0 for v in balance.values()), balance or "")
present = set(re.findall(r"\bid=\"([^\"']+)\"", html))
check("关键元素齐全", all(k in present for k in
      ("styleNote", "genHint", "aiBanner", "specPill", "bgModeChips", "bgModelChips",
       "cropBtn", "cropTitle", "orientChips", "status")))
_chipcss = chip_css_problems(html)
check("chip 选项键盘可达", not _chipcss, "; ".join(_chipcss) or "隐藏不脱离 tab 顺序 + 有焦点框")
_groupn = len(re.findall(r"id=\"\w+(?:Chips|Opts)\"", html))
_groupbad = chip_group_problems(html)
check("单选组都在 .opt 容器内", not _groupbad and _groupn >= 10, "%d 组 / 例外 %s" % (_groupn, _groupbad or "无"))
_owner = chip_state_owners(html)
check("chip 高亮只有一个拥有者", not _owner, "click 里又设 checked 又 paint .on 的行：%d" % len(_owner))
_silent = chip_silent_setters(html)
check("程序化改选项都广播 change", not _silent, "; ".join(_silent) or "setter 均走 announce()")
_goto = nav_goto_problems(html)
check("导航按钮不用可改写属性定位", not _goto, "用 [data-goto=…] 当后代过滤的选择器：%s" % (_goto or "无"))
_unreach = unreachable_chips(html)
check("chip 都是可聚焦控件", not _unreach, "没有 input 的 label 式 chip：%s" % (_unreach or "无"))
_txt = silent_text_writes(html)
check("程序化改文本/滑杆都派发 input", not _txt, "; ".join(_txt) or "写入均走 setTxt()")
_fit = canvas_fit_problems(html)
check("画布按自身尺寸定位缓冲区", not _fit, "; ".join(_fit) or "每个 <canvas> 都有 fitCanvas()")
_modal = dialog_door_problems(html)
check("弹窗开关走同一扇门", not _modal, "; ".join(_modal) or "焦点进入/圈定/归还 + Esc + aria 齐备")
_dead = dead_storage_keys(html)
check("localStorage 键都有人读", not _dead, "只写不读：%s" % (", ".join(_dead) or "无"))
_ring = focus_ring_problems(html)
check("每个自定义焦点面都有焦点框", not _ring, "; ".join(_ring) or "文本框/上传区/预览图均有 :focus-visible")
_touch = touch_target_problems(html)
check("命中区域写成规则而非副作用", not _touch, "; ".join(_touch) or "滑杆 ≥24px、按钮不靠内联 padding")
_drop = drop_problems(html)
check("上传区能键盘开、拖得进", not _drop, "; ".join(_drop) or "窗口级兜底 + 区域收图 + Enter/Space")
_float = floating_overlay_problems(html)
check("窄屏浮层不抢点击", not _float, "; ".join(_float) or "胶囊让位 + 底部留高")
_dragcap = drag_capture_problems(html)
check("拖拽面捕获指针并为手势兜底", not _dragcap,
      "; ".join(_dragcap) or "两个画布都 setPointerCapture + 窗口级收尾")
_boxbad = boxes_mismatch(js, M.ELEMENT_BOXES)
check("前后端元素框表逐项一致", not _boxbad,
      "%d 项：%s" % (len(M.ELEMENT_BOXES), "; ".join(_boxbad) or "逐项相等"))

# A check that can never fail is worse than no check, so the static checks above are each handed
# their own known-bad sample. The mutations guard themselves: if the anchor text ever moves, the
# .replace() becomes a no-op, the file stays valid and these controls turn red.
check("检查器自检：标签配平", tag_problem(html.replace("</button>", "</span>", 1)) is not None
      and tag_problem(html + "</p>") is not None)
_bad_boxes = js.replace("stars:[58,10,90,30]", "stars:[58,10,90,20]").replace(",birds:[96,24,28,10]", "")
check("检查器自检：框表一致", _boxbad == [] and len(boxes_mismatch(_bad_boxes, M.ELEMENT_BOXES)) == 2,
      "干净文件 %s / 改掉一项高度再删一项后：%s" % (_boxbad or "无差异",
                                                  boxes_mismatch(_bad_boxes, M.ELEMENT_BOXES)))
check("检查器自检：id 引用", missing_ids(re.sub(r'id="styleNote"', 'id="zzStyleNote"', html))[0] == ["styleNote"])
check("检查器自检：JS 括号",
      not all(v == 0 for v in js_balance(js.replace("{", "", 1)).values())
      and js_balance("const re=/^image\\//;if(x.test(f.type))go(f);") == {"{": 0, "(": 0, "[": 0},
      "去掉一个左花括号仍报不平衡 / 转义斜杠结尾的正则字面量不误判")
_bad_lib = html.replace('<label class="chip" data-v="sun"><input type="checkbox">太阳</label>',
                        '<label class="chip"><input type="checkbox" data-v="sun">太阳</label>')
_l, _v, _i = lib_chips(_bad_lib)
check("检查器自检：元素库 data-v", len(_l) != len(_v) and len(_i) == 1, "%d 个chip / %d 带值 / %d 误挂 input" % (len(_l), len(_v), len(_i)))
check("检查器自检：chip 键盘", chip_css_problems(
      html.replace(".chip input{position:absolute", ".chip input{display:none;position:absolute")) != []
      and chip_css_problems(html.replace("input:focus-visible", "input.zzfocus")) != []
      and chip_css_problems(html.replace("button.chip:focus-visible", "button.chip.zzfocus")) != [])
check("检查器自检：单选组容器", chip_group_problems(
      html.replace('class="opt" id="styleChips"', 'id="styleChips"')) == ["styleChips 不是 .opt 容器"])
_bad_owner = html + "\n<script>x.onclick=()=>{i.checked=true;ch.classList.add('on');};</script>"
check("检查器自检：高亮拥有者", _owner == [] and len(chip_state_owners(_bad_owner)) == 1,
      "干净文件 0 行 / 补一行非法处理器后抓到 %d 行" % len(chip_state_owners(_bad_owner)))
_bad_silent = html + "\n<script>function zz(){q.checked=true;zzpaint(q);}</script>"
check("检查器自检：静默改选项", _silent == [] and len(chip_silent_setters(_bad_silent)) == 1,
      "干净文件 0 行 / 补一行不广播的 setter 后抓到 %d 行" % len(chip_silent_setters(_bad_silent)))
_bad_goto = html.replace("""querySelector('[data-nav="1-next"]')""",
                        """querySelector('.step[data-step="1"] [data-goto="2"]')""" 
                  ).replace("""querySelector('[data-nav="3-prev"]')""",
                        """querySelector('.step[data-step="3"] [data-goto="2"]')""")
check("检查器自检：导航定位", _goto == [] and len(nav_goto_problems(_bad_goto)) == 2,
      "干净文件 0 个 / 退回旧写法后抓到 %d 个" % len(nav_goto_problems(_bad_goto)))
_bad_reach = html.replace('<button type="button" class="chip" data-p="city">城市街景</button>',
                          '<label class="chip" data-p="city">城市街景</label>')
check("检查器自检：chip 可聚焦", _unreach == [] and unreachable_chips(_bad_reach) != [],
      "干净文件 0 个 / 模板改回 label 后抓到 %d 个" % len(unreachable_chips(_bad_reach)))
_bad_txt = html.replace("if(p.title)setTxt('#title',p.title);",
                        "if(p.title)$('#title').value=p.title;")
check("检查器自检：静默改文本", _txt == [] and len(silent_text_writes(_bad_txt)) == 1,
      "干净文件 0 行 / 退回直接赋值后抓到 %d 行" % len(silent_text_writes(_bad_txt)))
_bad_fit = html.replace("const fitted=fitCanvas(cv,0.62);", "const fitted=1;")
check("检查器自检：画布定位", _fit == [] and canvas_fit_problems(_bad_fit) != [],
      "干净文件无告警 / 去掉取景画布的定位后：%s" % (canvas_fit_problems(_bad_fit) or "未抓到"))
_bad_door = html.replace("openModal('cropModal');drawCrop();",
                         "$('#cropModal').classList.add('show');drawCrop();")
check("检查器自检：弹窗绕过开关", _modal == [] and len(dialog_door_problems(_bad_door)) == 1,
      "干净文件 0 项 / 退回直接 add('show') 后抓到 %d 项" % len(dialog_door_problems(_bad_door)))
_bad_esc = html.replace("if(e.key==='Escape'){e.preventDefault();dismiss(m);return;}", "")
_bad_vis = html.replace("opacity:0;pointer-events:none;visibility:hidden",
                        "opacity:0;pointer-events:none")
_bad_inline = html.replace('<div class="modal" id="viewModal">',
                           '<div class="modal" id="viewModal" onclick="closeModal(1)">')
check("检查器自检：Esc/隐藏/内联 onclick",
      len(dialog_door_problems(_bad_esc)) == 1 and len(dialog_door_problems(_bad_vis)) == 1
      and len(dialog_door_problems(_bad_inline)) == 1,
      "去掉 Esc 后 %d 项 / 去掉 visibility 后 %d 项 / 加回内联 onclick 后 %d 项"
      % (len(dialog_door_problems(_bad_esc)), len(dialog_door_problems(_bad_vis)),
         len(dialog_door_problems(_bad_inline))))
_bad_dead = html.replace("$('#closeHelp').onclick=hideHelp;",
                         "$('#closeHelp').onclick=()=>{localStorage.setItem('ps_welcomed','1');hideHelp();};")
check("检查器自检：只写不读的键", _dead == [] and dead_storage_keys(_bad_dead) == ["ps_welcomed"],
      "干净文件 %s / 补回 ps_welcomed 后抓到 %s" % (_dead or "无", dead_storage_keys(_bad_dead)))
_bad_ring = html.replace("input[type=text]:focus-visible,textarea:focus-visible",
                         "textarea:focus-visible")
check("检查器自检：焦点框", _ring == [] and focus_ring_problems(_bad_ring) != [],
      "干净文件 0 项 / 拿掉文本框的环后抓到 %d 项" % len(focus_ring_problems(_bad_ring)))
_bad_range = html.replace("input[type=range]{height:28px}", "input[type=range]{height:16px}")
_bad_pad = html.replace('<button class="btn btn-ai btn-sm" id="helpBtn" style="margin-left:10px">',
                        '<button class="btn btn-ai" id="helpBtn" style="margin-left:10px;padding:4px 10px">')
check("检查器自检：命中区域", _touch == [] and len(touch_target_problems(_bad_range)) == 1
      and len(touch_target_problems(_bad_pad)) == 1,
      "滑杆改回 16px 后 %d 项 / 按钮改回内联 padding 后 %d 项"
      % (len(touch_target_problems(_bad_range)), len(touch_target_problems(_bad_pad))))
_bad_drop = html.replace("window.addEventListener('dragover',e=>e.preventDefault());\n", "")
_bad_drop = _bad_drop.replace('<div class="drop" id="drop" tabindex="0"', '<div class="drop" id="drop"')
check("检查器自检：上传区", _drop == [] and len(drop_problems(_bad_drop)) == 2,
      "干净文件 0 项 / 去掉窗口兜底与 tabindex 后抓到 %d 项" % len(drop_problems(_bad_drop)))
_bad_float = html.replace(".peak-hint{pointer-events:none;right:10px",
                          ".peak-hint{right:10px")
check("检查器自检：浮层抢点击", _float == [] and floating_overlay_problems(_bad_float) == ["浮层仍会接管点击"],
      "干净文件 0 项 / 拿掉 pointer-events 后抓到 %s" % (floating_overlay_problems(_bad_float) or "无"))
_bad_cap = html.replace("try{edCv.setPointerCapture(e.pointerId);}catch(_){}", "")
_bad_end = (html.replace("edCv.addEventListener('pointercancel',edEndDrag);\n", "")
            .replace("window.addEventListener('pointerup',edEndDrag);\n", "")
            .replace("$('#cropCv').addEventListener('pointercancel',()=>{cropDown=false;});\n", "")
            .replace("window.addEventListener('pointerup',()=>{cropDown=false;});\n", ""))
check("检查器自检：拖拽手势", _dragcap == []
      and len(drag_capture_problems(_bad_cap)) == 1 and "edCv" in drag_capture_problems(_bad_cap)[0]
      and len(drag_capture_problems(_bad_end)) == 2,
      "拿掉捕获后 %s / 拆掉收尾后 %d 个面 / 无拖拽面时 %s"
      % (drag_capture_problems(_bad_cap), len(drag_capture_problems(_bad_end)),
         drag_capture_problems("<script>x.addEventListener('click',f);</script>")))

print("\n%d passed, %d failed" % (len(ok), len(fail)))
if fail:
    print("FAILED:", fail)
    sys.exit(1)
print("SELFTEST OK")
