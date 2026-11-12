# Bangla web fonts

Served by the tool itself (never from Google Fonts or another CDN), and only the Bengali-script
part of each font: CSS `unicode-range` makes a browser download a file only when a page actually
shows Bangla text, and only the weights in use. English text keeps the system font.

| File(s) | Font | Used for | Licence |
|---|---|---|---|
| `hind-siliguri-bengali-{400,500,600,700}.woff2` | Hind Siliguri (Indian Type Foundry), Bengali subset from `@fontsource/hind-siliguri@5.3.0` | all Bangla text | SIL OFL 1.1 — `OFL-HindSiliguri.txt` |
| `noto-sans-bengali-digits-{400,500,600,700}.woff2` | Noto Sans Bengali (Noto Project), **only the digits ০–৯**, subset from `@fontsource/noto-sans-bengali@5.3.0` with `pyftsubset --unicodes=U+09E6-09EF` | Bangla digits | SIL OFL 1.1 — `OFL-NotoSansBengali.txt` |
| `tiro-bangla-bengali-400.woff2` | Tiro Bangla (Tiro Typeworks), Bengali subset from `@fontsource/tiro-bangla@5.3.0` | Bangla in serif headings and big numbers | SIL OFL 1.1 — `OFL-TiroBangla.txt` |

Why the digits come from another font: Hind Siliguri draws **১** (one) at letter height, so
"১১" or "২০১৯" look broken next to the other digits. Noto Sans Bengali's digits are the standard
full-height forms and sit well with Hind Siliguri's letters.

To rebuild the digit files:
```bash
pip install fonttools brotli
for w in 400 500 600 700; do
  pyftsubset noto-sans-bengali-bengali-$w-normal.woff2 --unicodes="U+09E6-09EF" \
    --layout-features='*' --flavor=woff2 --output-file=noto-sans-bengali-digits-$w.woff2
done
```
