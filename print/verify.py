#!/usr/bin/env python3
"""Verify generated print files and compare them with an earlier git revision.

Checks for every <brand>/logo/print/**/NAME-cmyk.pdf that has an SVG source:
  pdf      only k/K colour operators, only colors.json values, no DeviceRGB/ICC/
           Separation, no fonts, no images, PDF/X-4 output intent present
  eps      DOS EPS header + TIFF preview; Ghostscript renders it to exactly the same
           CMYK raster as the PDF (colours and shapes)
  inkcov   Ghostscript ink coverage; only the expected channels carry ink
  old      CMYK values in the file at --against (default HEAD) vs the new file
  shape    old vs new rendered as black silhouettes (ignores colour), % pixels differing

Usage:  print/.venv/bin/python print/verify.py [--against REV] [--markdown]
"""
import argparse
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pikepdf
from pikepdf import Operator

PRINT_DIR = Path(__file__).resolve().parent
REPO = PRINT_DIR.parent
COLOUR_OPS = {"k", "K", "rg", "RG", "g", "G", "cs", "CS", "sc", "SC", "scn", "SCN"}
RES = "72"


def table_values():
    cfg = json.loads((PRINT_DIR / "colors.json").read_text())
    return {tuple(e["cmyk"]): h for h, e in cfg["colors"].items()}


def walk_streams(pdf):
    """Yield (operands, op) for the page and every form XObject it uses."""
    seen = set()

    def visit(obj):
        for operands, op in pikepdf.parse_content_stream(obj):
            yield operands, op
            if str(op) == "Do":
                xo = obj.Resources.XObject[operands[0]]
                if xo.objgen in seen:
                    continue
                seen.add(xo.objgen)
                if xo.Subtype == "/Form":
                    yield from visit(xo)
                else:
                    yield [], Operator("IMAGE")
    yield from visit(pdf.pages[0])


def colours_in(pdf):
    """Set of (op, values) for colour operators; values as rounded percent."""
    found = set()
    for operands, op in walk_streams(pdf):
        o = str(op)
        if o in ("k", "K"):
            found.add(("cmyk", tuple(round(float(v) * 100) for v in operands)))
        elif o in COLOUR_OPS - {"k", "K"} or o in ("IMAGE", "sh", "INLINE IMAGE", "BT"):
            vals = []
            for v in operands:
                try:
                    vals.append(round(float(v), 4))
                except (TypeError, ValueError):
                    vals.append(str(v))
            found.add((o, tuple(vals)))
    return found


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw).stdout


def check_pdf(path, allowed):
    problems = []
    raw = path.read_bytes()
    with pikepdf.open(path) as pdf:
        cols = colours_in(pdf)
        for kind, vals in cols:
            if kind != "cmyk":
                problems.append(f"operator {kind} {vals}")
            elif vals not in allowed:
                problems.append(f"CMYK {vals} not in colors.json")
        oi = pdf.Root.get("/OutputIntents")
        if not oi or oi[0].get("/S") != "/GTS_PDFX":
            problems.append("no GTS_PDFX output intent")
        with pdf.open_metadata() as m:
            if m.get("{http://www.npes.org/pdfx/ns/id/}GTS_PDFXVersion") != "PDF/X-4":
                problems.append("XMP GTS_PDFXVersion != PDF/X-4")
        if pdf.pdf_version != "1.6":
            problems.append(f"PDF version {pdf.pdf_version}")
    for cs in (b"/DeviceRGB", b"/CalRGB", b"/Separation", b"/DeviceN", b"/Pattern", b"/Shading"):
        if cs in raw:
            problems.append(f"{cs.decode()} present")
    fonts = run(["pdffonts", str(path)]).splitlines()[2:]
    if fonts:
        problems.append(f"{len(fonts)} font(s)")
    images = run(["pdfimages", "-list", str(path)]).splitlines()[2:]
    if images:
        problems.append(f"{len(images)} image(s)")
    return cols, problems


def silhouette(src, dst):
    """Copy of a PDF with every colour forced to 100% K, so only shapes are compared."""
    with pikepdf.open(src) as pdf:
        seen = set()

        def fix(obj):
            ops = []
            for operands, op in pikepdf.parse_content_stream(obj):
                o = str(op)
                if o in ("k", "rg", "g", "sc", "scn"):
                    ops.append(([0, 0, 0, 1], Operator("k")))
                elif o in ("K", "RG", "G", "SC", "SCN"):
                    ops.append(([0, 0, 0, 1], Operator("K")))
                elif o in ("cs", "CS"):
                    continue
                else:
                    ops.append((operands, op))
                    if o == "Do":
                        xo = obj.Resources.XObject[operands[0]]
                        if xo.Subtype == "/Form" and xo.objgen not in seen:
                            seen.add(xo.objgen)
                            fix(xo)
            data = pikepdf.unparse_content_stream(ops)
            if isinstance(obj, pikepdf.Page):
                obj.obj.Contents = pdf.make_stream(data)
            else:
                obj.write(data)
        fix(pdf.pages[0])
        pdf.save(dst)


def render_cmyk(path, out, eps=False):
    """Ghostscript raw 8-bit CMYK raster (no anti-aliasing). Returns (w, h, bytes)."""
    cmd = ["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=bitcmyk", "-dGrayValues=256",
           f"-r{RES}", "-o", str(out)]
    if eps:
        cmd.append("-dEPSCrop")
    run(cmd + [str(path)])
    w, h = page_size(path, eps)
    return w, h, out.read_bytes()


def page_size(path, eps):
    if eps:
        for line in path.read_bytes()[:4096].decode("latin1").splitlines():
            if line.startswith("%%BoundingBox:"):
                x0, y0, x1, y1 = map(int, line.split()[1:])
                return round((x1 - x0) * int(RES) / 72), round((y1 - y0) * int(RES) / 72)
    with pikepdf.open(path) as p:
        b = [float(v) for v in p.pages[0].MediaBox]
    return round((b[2] - b[0]) * int(RES) / 72), round((b[3] - b[1]) * int(RES) / 72)


def eps_to_ps(eps, out):
    d = eps.read_bytes()
    magic, po, pl, _, _, to, tl, _ = struct.unpack("<4sIIIIIIH", d[:30])
    if magic != b"\xC5\xD0\xD3\xC6":
        raise ValueError("no DOS EPS header")
    tiff = d[to:to + tl]
    out.write_bytes(d[po:po + pl])
    return tiff[:4] in (b"II*\x00", b"MM\x00*"), len(tiff)


def raster_diff(a, b):
    wa, ha, da = a
    wb, hb, db = b
    if (wa, ha) != (wb, hb) or len(da) != len(db):
        return None
    diff = sum(1 for i in range(0, len(da), 4) if da[i:i + 4] != db[i:i + 4])
    return 100.0 * diff / (wa * ha)


def gray_diff(a, b):
    """Percent of pixels whose ink state differs (K>127 vs not); None if sizes differ."""
    wa, ha, da = a
    wb, hb, db = b
    if (wa, ha) != (wb, hb) or len(da) != len(db):
        return None
    diff = sum(1 for i in range(3, len(da), 4) if (da[i] > 127) != (db[i] > 127))
    return 100.0 * diff / (wa * ha)


def inkcov(path):
    out = run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=inkcov", "-o", "-", str(path)])
    return [float(x) for x in out.split()[:4]]


def fmt_cols(cols):
    return " ".join("/".join(map(str, v)) if k == "cmyk" else f"{k}{list(v)}" for k, v in sorted(cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--against", default="HEAD", help="git revision of the old print files (default HEAD)")
    a = ap.parse_args()

    allowed = table_values()
    rows, failures = [], 0
    pdfs = []
    for svg in sorted(REPO.glob("*/logo/screen/**/*.svg")):
        rel = list(svg.relative_to(REPO).parts)
        rel[2] = "print"
        p = REPO.joinpath(*rel)
        p = p.with_name(p.stem + "-cmyk.pdf")
        if p.exists():
            pdfs.append(p)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for pdf in pdfs:
            rel = pdf.relative_to(REPO).as_posix()
            eps = pdf.with_suffix(".eps")
            notes = []
            new_cols, problems = check_pdf(pdf, allowed)

            # EPS vs PDF: identical CMYK raster
            ps = tmp / "eps.ps"
            try:
                has_tiff, tiff_len = eps_to_ps(eps, ps)
                if not has_tiff:
                    problems.append("EPS preview is not a TIFF")
                r_pdf = render_cmyk(pdf, tmp / "pdf.raw")
                r_eps = render_cmyk(ps, tmp / "eps.raw", eps=True)
                d = raster_diff(r_pdf, r_eps)
                if d is None:
                    problems.append(f"EPS size {r_eps[:2]} != PDF size {r_pdf[:2]}")
                elif d > 0:
                    problems.append(f"EPS raster differs from PDF on {d:.3f}% of pixels")
                eps_note = "identical" if d == 0 else f"{d}"
            except Exception as e:  # noqa: BLE001
                problems.append(f"EPS: {e}")
                eps_note = "error"

            # ink coverage: channels with ink must match colours used
            cov = inkcov(pdf)
            expect = [any(v[i] > 0 for k, v in new_cols if k == "cmyk") for i in range(4)]
            for i, ch in enumerate("CMYK"):
                if cov[i] > 0 and not expect[i]:
                    problems.append(f"unexpected {ch} ink {cov[i]:.3f}")

            # old file from git
            old_note, shape_note = "", ""
            try:
                old_bytes = subprocess.run(["git", "show", f"{a.against}:{rel}"], cwd=REPO,
                                           capture_output=True, check=True).stdout
                old = tmp / "old.pdf"
                old.write_bytes(old_bytes)
                with pikepdf.open(old) as op:
                    old_cols = colours_in(op)
                old_note = fmt_cols(old_cols)
                s_old, s_new = tmp / "s_old.pdf", tmp / "s_new.pdf"
                silhouette(old, s_old)
                silhouette(pdf, s_new)
                d = gray_diff(render_cmyk(s_old, tmp / "o.raw"), render_cmyk(s_new, tmp / "n.raw"))
                shape_note = "size differs" if d is None else f"{d:.2f}%"
                if new_cols != old_cols:
                    notes.append("colours changed")
            except subprocess.CalledProcessError:
                old_note = "(new file)"

            if problems:
                failures += 1
            rows.append((rel, fmt_cols(new_cols), old_note, shape_note, eps_note,
                         " ".join(f"{c:.2f}" for c in cov), "; ".join(problems) or "ok", notes))

    print(f"| file | new CMYK | old CMYK ({a.against}) | shape diff | EPS = PDF | inkcov C M Y K (area fraction) | checks |")
    print("|---|---|---|---|---|---|---|")
    for rel, new, old, shape, epsn, cov, status, notes in rows:
        mark = " **(changed)**" if notes else ""
        print(f"| {Path(rel).name} | {new}{mark} | {old} | {shape} | {epsn} | {cov} | {status} |")
    print(f"\n{len(rows) - failures}/{len(rows)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
