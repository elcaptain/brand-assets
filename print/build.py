#!/usr/bin/env python3
"""Generate CMYK print files (PDF/X-4 + EPS) from the logo SVGs.

    <brand>/logo/screen/<...>/NAME.svg  ->  <brand>/logo/print/<...>/NAME-cmyk.pdf
                                            <brand>/logo/print/<...>/NAME-cmyk.eps

Pipeline per file:
  1. Check the SVG: only simple vector elements, every paint colour is in colors.json.
  2. rsvg-convert renders it to an RGB PDF (1 px = 1 pt).
  3. pikepdf rewrites every colour operator to DeviceCMYK using colors.json (exact
     hex match) and fails on anything it can't map (patterns, images, text, ...).
  4. pikepdf adds the PDF/X-4 output intent, TrimBox and XMP. Ghostscript is NOT
     used for the PDF, so nothing can re-convert the CMYK values.
  5. pdftops -eps writes the EPS; a TIFF preview from pdftocairo is wrapped around
     it as a DOS EPS binary header, like Illustrator's "TIFF (8-bit colour)" preview.

Usage:  print/.venv/bin/python print/build.py [--check] [SVG ...]
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import struct
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

import pikepdf
from pikepdf import Name, Operator

PRINT_DIR = Path(__file__).resolve().parent
REPO = PRINT_DIR.parent
CONFIG = PRINT_DIR / "colors.json"

NAMED_COLOURS = {"white": "#FFFFFF", "black": "#000000"}
PAINT_ATTRS = ("fill", "stroke", "stop-color", "flood-color", "lighting-color", "color")
OPACITY_ATTRS = ("opacity", "fill-opacity", "stroke-opacity", "stop-opacity")
ALLOWED_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
                "polygon", "defs", "clipPath", "title", "desc", "metadata"}
FORBIDDEN_ATTRS = ("mask", "filter", "mix-blend-mode", "isolation")
# Operators that rsvg emits for flat vector art and that are colour-neutral.
PASSTHROUGH_OPS = {"q", "Q", "cm", "m", "l", "c", "v", "y", "h", "re", "f", "f*", "F",
                   "S", "s", "B", "B*", "b", "b*", "n", "W", "W*", "w", "J", "j", "M", "d", "i"}
NS_PDFXID = "http://www.npes.org/pdfx/ns/id/"


class BuildError(Exception):
    pass


# ── config ────────────────────────────────────────────────────────────────

def load_config():
    cfg = json.loads(CONFIG.read_text())
    table = {}
    for hexv, entry in cfg["colors"].items():
        key = normalise_hex(hexv)
        if key is None:
            raise BuildError(f"colors.json: invalid hex {hexv!r}")
        cmyk = entry["cmyk"]
        if len(cmyk) != 4 or not all(isinstance(v, int) and 0 <= v <= 100 for v in cmyk):
            raise BuildError(f"colors.json: {hexv} needs four integer percentages")
        table[key] = cmyk
    return table, cfg["output_intent"]


def normalise_hex(value):
    v = value.strip()
    if v.lower() in NAMED_COLOURS:
        return NAMED_COLOURS[v.lower()]
    m = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", v)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.upper()


def load_profile(oi):
    """The ICC profile is not committed (licence unclear). If it's missing, take it
    from any existing print PDF in the repo that embeds exactly this profile."""
    path = PRINT_DIR / oi["profile"]
    if not path.exists():
        for pdf in sorted(REPO.glob("*/logo/print/**/*-cmyk.pdf")):
            try:
                with pikepdf.open(pdf) as p:
                    for intent in p.Root.get("/OutputIntents", []):
                        data = intent.DestOutputProfile.read_bytes()
                        if hashlib.md5(data).hexdigest() == oi["profile_md5"]:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_bytes(data)
                            break
            except Exception:
                continue
            if path.exists():
                break
    if not path.exists():
        raise BuildError(f"ICC profile {path} missing and not found in any print PDF. "
                         f"Download '{oi['info']}' from eci.org and save it there.")
    data = path.read_bytes()
    if hashlib.md5(data).hexdigest() != oi["profile_md5"]:
        raise BuildError(f"{path}: MD5 does not match colors.json profile_md5")
    return data


# ── step 1: SVG check ─────────────────────────────────────────────────────

def check_svg(svg, table):
    errors = []
    root = ET.parse(svg).getroot()

    def walk(el, in_clip):
        tag = el.tag.split("}")[-1]
        if tag not in ALLOWED_TAGS:
            errors.append(f"unsupported element <{tag}>")
        attrs = dict(el.attrib)
        for decl in attrs.pop("style", "").split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                attrs[k.strip()] = v.strip()
        for a in FORBIDDEN_ATTRS:
            if a in attrs:
                errors.append(f"unsupported attribute {a}={attrs[a]!r} on <{tag}>")
        for a in OPACITY_ATTRS:
            if a in attrs and float(attrs[a]) != 1:
                errors.append(f"{a}={attrs[a]} on <{tag}> (transparency not supported)")
        # clipPath children are geometry only; their fill is never painted.
        if not in_clip:
            for a in PAINT_ATTRS:
                if a not in attrs or attrs[a] == "none":
                    continue
                hexv = normalise_hex(attrs[a])
                if hexv is None:
                    errors.append(f"{a}={attrs[a]!r} on <{tag}> is not a plain colour")
                elif hexv not in table:
                    errors.append(f"colour {attrs[a]} ({a} on <{tag}>) is not in colors.json")
        for child in el:
            walk(child, in_clip or tag == "clipPath")

    walk(root, False)
    if errors:
        raise BuildError("; ".join(sorted(set(errors))))


# ── step 3: colour rewrite ────────────────────────────────────────────────

def rgb_to_hex(values):
    out = []
    for v in values:
        x = float(v) * 255
        r = round(x)
        if abs(x - r) > 0.02 or not 0 <= r <= 255:
            raise BuildError(f"colour component {v} is not an 8-bit value")
        out.append(r)
    return "#%02X%02X%02X" % tuple(out)


def cmyk_operands(cmyk):
    return [Decimal(v) / Decimal(100) for v in cmyk]


def trivial_extgstate(gs):
    for k, v in gs.items():
        if k == "/Type":
            continue
        if k in ("/CA", "/ca") and float(v) == 1:
            continue
        raise BuildError(f"graphics state {k}={v} not supported")
    return True


def rewrite_colours(pdf, table):
    used = set()
    if len(pdf.pages) != 1:
        raise BuildError(f"expected 1 page, got {len(pdf.pages)}")
    page = pdf.pages[0]
    res = page.obj.get("/Resources", pikepdf.Dictionary())
    for key in res.keys():
        if key not in ("/ExtGState", "/ProcSet"):
            raise BuildError(f"page resource {key} not supported (only flat vector fills)")
    for gs in res.get("/ExtGState", {}).values():
        trivial_extgstate(gs)

    new_ops = []
    for operands, op in pikepdf.parse_content_stream(page):
        o = str(op)
        if o in ("rg", "RG"):
            hexv = rgb_to_hex(operands)
        elif o in ("g", "G"):
            hexv = rgb_to_hex([operands[0]] * 3)
        elif o == "gs":
            continue  # only trivial (opacity 1) states, checked above; drop them
        elif o in PASSTHROUGH_OPS:
            new_ops.append((operands, op))
            continue
        else:
            raise BuildError(f"PDF operator {o!r} not supported")
        if hexv not in table:
            raise BuildError(f"colour {hexv} is not in colors.json")
        used.add(hexv)
        new_ops.append((cmyk_operands(table[hexv]), Operator("k" if o in ("rg", "g") else "K")))

    page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new_ops))
    if "/ExtGState" in res:
        del res["/ExtGState"]
    res.ProcSet = pikepdf.Array([Name.PDF])
    return used


# ── step 4: PDF/X-4 ───────────────────────────────────────────────────────

def source_date(svg):
    """Last commit date of the SVG, so rebuilding an unchanged file is byte-identical."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--", str(svg)],
                             cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
        if out:
            return dt.datetime.fromisoformat(out)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return dt.datetime.fromtimestamp(svg.stat().st_mtime, dt.timezone.utc).replace(microsecond=0)


def pdf_date(d):
    off = d.utcoffset() or dt.timedelta(0)
    sign = "+" if off >= dt.timedelta(0) else "-"
    mins = abs(int(off.total_seconds())) // 60
    return d.strftime("D:%Y%m%d%H%M%S") + f"{sign}{mins // 60:02d}'{mins % 60:02d}'"


def make_pdfx(pdf, svg, title, icc, oi, tools):
    page = pdf.pages[0]
    page.obj.TrimBox = page.obj.MediaBox
    for key in ("/Group",):  # cairo may add a transparency group; X-4 allows it but we have no transparency
        if key in page.obj:
            del page.obj[key]

    profile = pdf.make_stream(icc)
    profile.N = 4
    pdf.Root.OutputIntents = pikepdf.Array([pikepdf.Dictionary(
        Type=Name.OutputIntent, S=Name.GTS_PDFX,
        OutputConditionIdentifier=oi["output_condition_identifier"],
        OutputCondition=oi["output_condition"],
        RegistryName=oi["registry_name"],
        Info=oi["info"],
        DestOutputProfile=profile,
    )])

    when = source_date(svg)
    producer = f"brand-assets print/build.py ({tools})"
    rel = svg.relative_to(REPO).as_posix()
    doc_id = uuid.uuid5(uuid.NAMESPACE_URL, "brand-assets:" + rel)
    inst_id = uuid.uuid5(uuid.NAMESPACE_URL, "brand-assets:" + rel + ":" +
                         hashlib.sha256(svg.read_bytes()).hexdigest())

    pdf.docinfo = pdf.make_indirect(pikepdf.Dictionary(
        Title=title, Creator="brand-assets print/build.py", Producer=producer,
        CreationDate=pdf_date(when), ModDate=pdf_date(when), Trapped=Name("/False"),
        GTS_PDFXVersion="PDF/X-4"))
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
    iso = when.isoformat()
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
        m["dc:title"] = title
        m["dc:format"] = "application/pdf"
        m["xmp:CreateDate"] = iso
        m["xmp:ModifyDate"] = iso
        m["xmp:MetadataDate"] = iso
        m["xmp:CreatorTool"] = "brand-assets print/build.py"
        m["pdf:Producer"] = producer
        m["pdf:Trapped"] = "False"
        m["xmpMM:DocumentID"] = f"uuid:{doc_id}"
        m["xmpMM:InstanceID"] = f"uuid:{inst_id}"
        m["xmpMM:VersionID"] = "1"
        m["xmpMM:RenditionClass"] = "default"
        m[f"{{{NS_PDFXID}}}GTS_PDFXVersion"] = "PDF/X-4"


# ── step 5: EPS ───────────────────────────────────────────────────────────

def make_eps(pdf_path, eps_path, tmp):
    ps = tmp / "out.eps"
    subprocess.run(["pdftops", "-eps", "-level3", str(pdf_path), str(ps)], check=True)
    ps_bytes = ps.read_bytes()
    # pdftops writes no date, but strip any to keep builds reproducible
    ps_bytes = re.sub(rb"%%CreationDate:[^\n]*\n", b"", ps_bytes)

    tif_base = tmp / "preview"
    subprocess.run(["pdftocairo", "-tiff", "-tiffcompression", "lzw", "-transp", "-r", "72", "-singlefile",
                    str(pdf_path), str(tif_base)], check=True)
    tiff = (tmp / "preview.tif").read_bytes()

    header_len = 30
    header = struct.pack("<4sIIIIIIH", b"\xC5\xD0\xD3\xC6",
                         header_len, len(ps_bytes), 0, 0,
                         header_len + len(ps_bytes), len(tiff), 0xFFFF)
    eps_path.write_bytes(header + ps_bytes + tiff)


# ── main ──────────────────────────────────────────────────────────────────

def tool_versions():
    out = subprocess.run(["rsvg-convert", "--version"], capture_output=True, text=True).stdout
    rsvg = re.search(r"rsvg-convert version (\S+)", out).group(1)
    cairo = re.search(r"cairo (\S+)", out)
    poppler = re.search(r"version (\S+)", subprocess.run(["pdftops", "-v"], capture_output=True,
                                                         text=True).stderr).group(1)
    return (f"librsvg {rsvg}, cairo {cairo.group(1) if cairo else '?'}, "
            f"pikepdf {pikepdf.__version__}, poppler {poppler}")


def output_paths(svg):
    rel = svg.relative_to(REPO)
    parts = list(rel.parts)
    if len(parts) < 4 or parts[1] != "logo" or parts[2] != "screen":
        raise BuildError("not under <brand>/logo/screen/")
    parts[2] = "print"
    base = REPO.joinpath(*parts).with_suffix("")
    return base.with_name(base.name + "-cmyk.pdf"), base.with_name(base.name + "-cmyk.eps")


def build_one(svg, table, oi, icc, tools, check_only):
    check_svg(svg, table)
    pdf_out, eps_out = output_paths(svg)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rgb = tmp / "rgb.pdf"
        subprocess.run(["rsvg-convert", "--format=pdf", "--dpi-x=72", "--dpi-y=72",
                        "-o", str(rgb), str(svg)], check=True)
        with pikepdf.open(rgb) as pdf:
            used = rewrite_colours(pdf, table)
            if check_only:
                return used
            make_pdfx(pdf, svg, pdf_out.stem, icc, oi, tools)
            pdf_out.parent.mkdir(parents=True, exist_ok=True)
            pdf.save(pdf_out, force_version="1.6", deterministic_id=True,
                     object_stream_mode=pikepdf.ObjectStreamMode.disable)
        make_eps(pdf_out, eps_out, tmp)
    return used


def collect(args):
    if args:
        return [Path(a).resolve() for a in args]
    return sorted(REPO.glob("*/logo/screen/**/*.svg"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="validate SVGs and colour mapping only, write nothing")
    ap.add_argument("svgs", nargs="*", help="SVG files (default: all <brand>/logo/screen/**/*.svg)")
    a = ap.parse_args()

    try:
        table, oi = load_config()
        icc = None if a.check else load_profile(oi)
    except BuildError as e:
        sys.exit(f"error: {e}")
    tools = tool_versions()

    failures = []
    svgs = collect(a.svgs)
    for svg in svgs:
        rel = svg.relative_to(REPO)
        try:
            used = build_one(svg, table, oi, icc, tools, a.check)
            print(f"ok    {rel}  [{' '.join(sorted(used))}]")
        except (BuildError, subprocess.CalledProcessError, ET.ParseError) as e:
            failures.append(f"{rel}: {e}")
            print(f"FAIL  {rel}: {e}", file=sys.stderr)

    print(f"\n{len(svgs) - len(failures)}/{len(svgs)} built" + (" (check only)" if a.check else ""))
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
