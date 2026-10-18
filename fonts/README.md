# Fonts are not bundled

This project can use **MiSans** for the postcard titles, but the font files are
**not included in this repository** (they are large and are distributed under
Xiaomi's own font license, not this project's MIT license).

Without them the app still works: it falls back to the CJK fonts installed on
your system (`SimSun` / `SimHei` / `Microsoft YaHei` on Windows, system sans on
other platforms), and the web UI falls back to the browser's sans-serif.

## Enabling MiSans

1. Download MiSans from Xiaomi's official page and review its license:
   <https://hyperos.mi.com/font/en/download/>
2. Place the files as:
   - `fonts/MiSans-Regular.ttf` — used by the bitmap renderer (title font "mi")
   - `static/fonts/MiSans-Regular.subset.ttf` — used by the web UI (`@font-face`)
3. Restart the service.

If you only have the full font, you can generate the subset with `fonttools`:

```bash
pyftsubset fonts/MiSans-Regular.ttf \
  --output-file=static/fonts/MiSans-Regular.subset.ttf \
  --text-file=index.html --layout-features='*'
```
