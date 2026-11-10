---
name: print-files
description: Generate and verify the CMYK print logos (PDF/X-4 + EPS) from the screen SVGs in this repo. Use when SVG logos were added or changed, when asked to regenerate, check or update print files / CMYK / EPS / PDF, or when colors.json changes.
---

# Print files from SVGs

The SVGs in `<brand>/logo/screen/` are the source of truth. `print/build.py` turns each
one into `<brand>/logo/print/<same subfolders>/<name>-cmyk.pdf` and `-cmyk.eps`, using the
exact CMYK values in `print/colors.json`. Never edit print files by hand, and never edit
SVGs or `colors.json` unless the user asks.

## 1. Set up (once per machine)

```bash
brew install librsvg ghostscript poppler
python3 -m venv print/.venv
print/.venv/bin/pip install -r print/requirements.txt
```

Tested with librsvg 2.63.4, cairo 1.18.6, poppler 26.11.0, Ghostscript 10.08.0,
pikepdf 10.18.0. Tell the user if the installed versions differ (the PDF `Producer`
records the versions used), because output can change between versions.

The ICC profile (`ISO Coated v2 300% (ECI)`) is not committed. `build.py` extracts it
from an existing print PDF and checks its MD5, so no download is needed.

## 2. Check, build, verify

```bash
print/.venv/bin/python print/build.py --check     # validate only, writes nothing
print/.venv/bin/python print/build.py             # all SVGs (or pass specific SVG paths)
print/.venv/bin/python print/verify.py            # compare against HEAD
```

- `build.py` fails a file when the SVG uses a colour that is not in `colors.json`, or
  anything other than flat vector shapes (gradients, text, images, masks, filters,
  opacity). **Do not add colours or guess CMYK values.** Report the file and colour,
  and ask the user for the official CMYK value. They come from the marketing machine
  colour guidelines. Other files still build.
- `verify.py` prints a Markdown table. It exits non-zero if any generated file fails a
  check: non-table colours, RGB, fonts, images, a missing PDF/X-4 output intent, or an
  EPS that doesn't rasterise identically to its PDF.

## 3. Report to the user

Summarise from the verify table:
- files whose CMYK values changed vs HEAD (marked **(changed)**), with old → new values
- shape diff above 0% or "size differs": the SVG artwork differs from what the old print
  file was made from. Render both (`pdftoppm -png -r 40`) and look before calling it fine.
- new print files (new brand or variant) and files that failed to build

Then show `git status --short -- '*/logo/print/'`. Only commit when the user asks, on a
branch rather than main, and include the verify table in the commit/PR description.

## Notes

- PNGs in `screen/` are not regenerated. If an SVG changed, its PNG may be stale.
- `open-home-foundation/badge/` has no print versions and is not built.
- EPS is plain PostScript from `pdftops` with a TIFF preview in a DOS EPS header, not an
  Illustrator-native EPS. Illustrator can open it, but not as editable "Illustrator 10" art.
