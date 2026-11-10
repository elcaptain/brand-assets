# Brands

Source brand assets (logos, wordmarks, lockups) for the Open Home Foundation and its projects. For usage guidelines, see the [brand guidelines site](https://brands.openhomefoundation.io).

## Copyright notice
This logo is trademarked and the property of the Open Home Foundation. This means it is not available for commercial use without express written permission from the foundation. We regard commercial use as anything designed to market or promote a product, software or service that is for sale. Please contact partner@openhomefoundation.org for further information.

## Brands

| Brand | Directory |
|---|---|
| Open Home Foundation | `open-home-foundation/` |
| Home Assistant | `home-assistant/` |
| Music Assistant | `music-assistant/` |
| ESPHome | `esphome/` |
| Sendspin | `sendspin/` |

## Structure

Each brand directory groups assets by type (currently `logo/`, with room for more in future). Logos are provided for print (eps, pdf) and screens (png, svg):

```
{brand}/
└── logo
    ├── print
    │   ├── lockup
    │   │   ├── inline
    │   │   ├── main
    │   │   └── stacked
    │   └── logomark
    └── screen
        ├── lockup
        │   ├── inline
        │   ├── main
        │   └── stacked
        └── logomark
```

## Naming Convention

All files follow: `{brand}-{type}-{variant}-{theme}-{background}.{ext}`

- **Type**: `logomark`, `wordmark`, `lockup` (logomark + wordmark)
- **Variant**: `inline`, `main`, `stacked`
- **Themes**: `color` (full color), `monochrome` (one color)
- **Background**: `on-dark` (for dark backgrounds), `on-light` (for light backgrounds), `on-red` (Sendspin only, for its red brand background)

Examples:
- `open-home-foundation/logo/print/logomark/OHF-mark-color-on-light-cmyk.eps`
- `home-assistant/logo/screen/lockup/HA-lockup-main-color-on-dark.png`

## Print files

Print files are generated from the screen SVGs, never made by hand. `print/build.py` turns `{brand}/logo/screen/…/NAME.svg` into `{brand}/logo/print/…/NAME-cmyk.pdf` (PDF/X-4, ISO Coated v2 300% (ECI)) and `NAME-cmyk.eps`. Colours are mapped exactly using `print/colors.json`, and the build fails on any colour that isn't listed there.

```bash
brew install librsvg ghostscript poppler
python3 -m venv print/.venv && print/.venv/bin/pip install -r print/requirements.txt
print/.venv/bin/python print/build.py     # build all print files
print/.venv/bin/python print/verify.py    # check them and compare with HEAD
```

In Claude Code, the `print-files` skill (`.claude/skills/print-files/`) runs these steps and reports what changed.

## Adding a New Brand

1. Create a directory using the brand's kebab-case name (e.g., `my-project/`).
2. Add the needed folder structure.
3. Name files as `{brand}-{type}-{variant}-{theme}-{background}.{ext}`.
4. Update the brands table in this README.
