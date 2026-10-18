# CHANGELOG

## 1.1.0 — 2026-10-18 · UI 与 SVG 图生图重构

按用户反馈做的第二轮重构。

- **删除"识别元素"功能**：移除 `/analyze` 路由、`analyze_photo` / `_vl_call` / GLM-4V / DeepSeek 视觉链路、
  `use_ai` 参数与前端识别 UI。手绘元素库保留，移入"高级选项 → 手绘元素"。
- **UI 重写为 3 区块单页**：上传照片 → 选择风格/图案 → 预览并下载；桌面左侧控制 + 右侧 sticky 预览，
  移动端单列 + 底部固定生成按钮。删除 5 步向导、mode 概念、`data-goto` 导航、高峰倒计时浮层与 tips 轮播。
- **SVG 跟随当前图案**：AI 图案（`photo` / `t2i` / `aibg` / `zine`）导出 SVG 时先取图生图结果再矢量化画区，
  本地水彩仍矢量化原图；AI 不可用时回退原图并在 ZIP 中附 `ai_note.txt`。
- 新增标题/地点/日期/编号完整输入；EXIF 日期自动预填且可修改。
- 画布编辑改为"高级微调"弹窗，仅矢量手绘可用；AI 图案下禁用并说明。
- 下载分组：PNG 套件 / 打印 PDF / A4 拼版 / 全套 ZIP 为主，单面 PNG、背面 PDF、壁纸、SVG、CMYK 折叠在"更多格式"。
- `localStorage` 升级到 `_v:2`，旧 `ps_state` 的 mode/elements 字段自动迁移/忽略，旧草稿兼容。
- 自检同步：删除识别与浮层/导航专项检查，新增"SVG 画区使用图生图结果"和"无 AI 时附 ai_note.txt"。
  自检数量：`--quick` 100 项、完整 102 项（全部通过）。
- 截图更新：`docs/screenshot-ui.png` 为新 UI。

### 说明

- 无 SiliconFlow key，AI 图生图链路未做实网调用；已用 mock 验证 SVG 会使用 AI artwork，
  以及无 key 时回退原图并附 `ai_note.txt`。
- 识别功能删除后，GLM/DeepSeek key 不再需要；本地 `.env` 只保留 SiliconFlow 占位说明。

---

## 1.0.0 — 2026-10-18 · 首个开源版本

在交接报告（第 36 轮基线）基础上做了一轮以"可交付、可开源、无明显缺陷"为目标的修复。**没有改任何水彩参数**（levels / 阈值 / 滤波 / 色阶 / 排序均保持不变），唯一视觉变化来自孔洞修复。

### 修复（按优先级）

**运行与安全**

- `start-service.ps1` 重写守护循环：原实现每约 60 秒无条件杀掉服务重启并删除日志；现在只在子进程真正退出后重启，退避 5s→60s，健康运行不被打断。
- 日志不再删除：追加写入并在 >5MB 时轮转为 `*.log.1`；Python 以 `-u` 启动；修复"日志永远是 0 字节、崩溃现场丢失"。
- 启动脚本固定 `OPENBLAS_NUM_THREADS=1` / `OMP_NUM_THREADS=1`，避免 numpy/OpenBLAS 在 12 线程 waitress 下偶发内存分配失败。
- `启动明信片.bat` 的 Cloudflare 隧道改为显式 opt-in（`--tunnel` 或 `POSTCARD_TUNNEL=1`），默认只本机；不再内置个人隧道/域名。
- CORS 不再返回 `Access-Control-Allow-Origin: *`：只允许同源与 `file://`（`Origin: null`）；带其他 Origin 的 POST/OPTIONS 返回 403。
- 上传限制 50MP 像素（解码前检查，`DecompressionBomb` 转为友好 413），避免几十 MB 图片解出数百 MP。
- `/export` 增加 kind 白名单，未知类型返回 400；SVG 导出仅在需要背面时生成 `back_svg`。
- 服务端异常不再把 `str(e)` 原文回给客户端（traceback 仍写日志）。
- 启动时清理 `cache/`：14 天 TTL + 300MB 上限。
- 移除 `app.py` 中硬编码的 `D:\deepseek-harness` 路径；`cred_candidates()` 改用 `~` 下的默认位置，可在任意机器运行。
- `make_postcard.py` 增加系统字体探测（Windows / Linux Noto CJK / macOS PingFang，可用
  `POSTCARD_FONT_DIR` 覆盖），非 Windows 环境不再只能依赖缺字形的 Pillow 默认字体。

**SVG 画区（核心缺陷）**

- 修复孔洞被实心重填：`_trace_mask` 会产生外环和孔洞环，旧代码把每个环都当实心多边形，导致深色层内部的亮部（天空、窗口、拱门、浪花等）被同色盖掉。现在每个 tone 层合并为一条 `<path>` 并设置 `fill-rule="evenodd"`，孔洞正确透出下层浅色。合成图验证：修复前圆内 121/121 像素为深色，修复后 0/121。
- 极端长宽比保护：新增 `GRID_CELLS_NP=1_000_000`、`GRID_CELLS_PY=350_000`、`GRID_SIDE_MAX=2000`；1:4 / 1:8 全景或长截图不再产生数百万单元、GB 级内存。
- 背面标题双重转义修复（`A & B` 不再显示为 `A &amp;amp; B`），字号按原始长度计算。
- `esc()` 过滤 XML 1.0 非法控制字符，标题含 `\x01` 等不会再产生非法 SVG。
- `sample_palette` 对纯色/极暗图回退到最亮像素，不再返回 `(0,0,0)`。
- 矢量化失败不再静默：打印 traceback 后再回退手绘剪贴画。
- 清理未使用变量；细条过滤注释改为真实的约 1.1–1.6mm（阈值行为未变）。

**前端**

- 拍摄日期：在 canvas 压缩前读取原始 JPEG 的 EXIF `DateTimeOriginal`/`DateTime`，作为 `date` 字段传给后端（此前 canvas 重编码丢失 EXIF，日期永远显示占位）。
- "矢量SVG"下载名改为 `*.svg.zip`，不再把 ZIP 伪装成 `.svg`。
- 导出按钮在渲染期间全部禁用，避免重复点击产生并发 600dpi/AI 渲染。
- 服务端 JSON `error` 会显示到状态栏，网络异常不再只显示"导出失败"。

**测试与工程**

- `selftest.py` 新增 6 条断言：背面单次转义、控制字符 XML 合法、孔洞保留与 evenodd、极端长宽比网格受限、暗图配色兜底。
- 自检数量：`--quick` 103 项、完整 105 项，全部通过。
- `selftest.py` 不再依赖作者的示例照片：全新克隆没有 `photo-A.img` 时会自动生成一张合成样片。
- 自检端口可用 `POSTCARD_TEST_PORT` 覆盖，避免并行跑测试时 5199 端口争用。
- `find-python.ps1` 探测 5 个依赖（含 numpy）并尝试 `import app`；`.python-path` 用 UTF-8；解释器按版本号降序。
- **AI 识别 provider 扩展**：没有 SiliconFlow 时可用 GLM-4V（`GLM_API_KEY`）识别照片，
  DeepSeek 视觉作为最后兜底；AI 生图仍需要 SiliconFlow。`/status` 增加
  `models.generation` / `vision_provider`，前端会区分"识别可用"和"生图可用"。
- `POSTCARD_NO_AI=1` 可让自检/CI 忽略本地 `.env` 密钥，保证测试可复现。
- 修复：矢量手绘模式不再误报 "未配置 SILICONFLOW_API_KEY / 已回退"；只有真正选 AI 图案时才提示。
- 增加 `.gitignore`、`.gitattributes`、MIT LICENSE；回归脚本与历史日志归档进 `docs/` / `scripts/`。

### 未改 / 已知限制

- 未改水彩参数与算法（除孔洞填充语义）；R1 类繁忙街景质量仍是架构上限。
- 竖版仅支持 AI 图案方式；矢量 SVG 固定横版 150×100mm。
- 矢量 SVG 导出为 ZIP（正/背面两个文件）。
- EXIF 日期只解析 JPEG；HEIC/PNG 无 EXIF 时回退到后端读取或占位。
- Windows 启动脚本（`.ps1` / `.bat`）为 Windows 专用；核心应用可跨平台手动运行。
- AI 能力需要 `SILICONFLOW_API_KEY`；无密钥时自动降级到矢量引擎。
