"""
PDF STAMP API  — Flask
======================
POST /stamp
  Stamps a PDF with an image + optional date text.
  All positions and sizes are PERCENTAGE-based (0-100) relative to page dimensions.
  Supports: single page, all pages, first/last occurrence.

POST /health
  Returns {"status": "ok"}

Request JSON fields
-------------------
  pdf                  (str, required)  Base64-encoded PDF
  signature            (str, required)  Base64-encoded stamp/signature image
  x_percent            (float, required) 0-100 from LEFT edge
  y_percent            (float, required) 0-100 from TOP edge

  stamp_width          (float) fixed px width  (used if stamp_width_percent not given)
  stamp_height         (float) fixed px height (used if stamp_height_percent not given)
  stamp_width_percent  (float) % of page width  -- TAKES PRIORITY over stamp_width
  stamp_height_percent (float) % of page height -- TAKES PRIORITY over stamp_height

  date_text            (str)   Date string to print above stamp
  date_x_percent       (float) X center of date text (defaults to x_percent)
  date_y_percent       (float) Y of date text from top (defaults to y_percent - 4)
  date_font_size       (float) Font size in pts (default 8)

  occurrence           (str)   "all" | "first" | "last"  (default "all")
  pages                (str)   "all" | "1" | "1,3,5" | "1-3" (default "all")
  padding              (float) Extra offset in pts (default 0)

  flip_x               (bool)  Mirror X axis before placing stamp (default false)
  flip_y               (bool)  Mirror Y axis before placing stamp (default false)

Response JSON
-------------
  {"pdf": "<base64>"}   on success
  {"error": "<msg>"}    on failure
"""

from flask import Flask, request, jsonify
import base64
import io
import traceback

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib.colors import HexColor
import PIL.Image

app = Flask(__name__)

API_KEY = "PDF_Stamp"


# ── helpers ────────────────────────────────────────────────────────────────────

def check_api_key():
    if request.headers.get("x-api-key", "") != API_KEY:
        return jsonify({"error": "Unauthorized: invalid or missing x-api-key"}), 401
    return None


def b64_decode(b64: str) -> bytes:
    if "," in b64:
        b64 = b64.split(",", 1)[1]
    b64 += "=" * (-len(b64) % 4)
    return base64.b64decode(b64)


def parse_pages(pages_str: str, total: int) -> list:
    s = str(pages_str).strip().lower()
    if s == "all":
        return list(range(total))
    indices = set()
    for part in s.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            indices.update(range(int(a) - 1, int(b)))
        else:
            indices.add(int(part) - 1)
    return sorted(i for i in indices if 0 <= i < total)


def resolve_occurrence(indices: list, occurrence: str) -> list:
    occ = str(occurrence).strip().lower()
    if occ == "first":
        return indices[:1]
    if occ == "last":
        return indices[-1:]
    return indices


def make_overlay(pw, ph, stamp_bytes,
                 x_pt, y_pt, sw_pt, sh_pt,
                 date_text, date_x_pt, date_y_pt, font_sz) -> bytes:
    """
    Build a transparent PDF page overlay (ReportLab origin = bottom-left).
    All y values are already converted from top-origin before this call.
    """
    buf = io.BytesIO()
    c   = canvas.Canvas(buf, pagesize=(pw, ph))

    if date_text:
        c.setFont("Helvetica-Bold", font_sz)
        c.setFillColor(HexColor("#000000"))
        c.drawCentredString(date_x_pt, date_y_pt, date_text)

    img        = PIL.Image.open(io.BytesIO(stamp_bytes)).convert("RGBA")
    img_reader = ImageReader(img)
    c.drawImage(img_reader, x_pt, y_pt,
                width=sw_pt, height=sh_pt,
                mask="auto", preserveAspectRatio=False)

    c.save()
    buf.seek(0)
    return buf.read()


# ── routes ─────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/stamp", methods=["POST"])
def stamp():
    err = check_api_key()
    if err:
        return err

    try:
        body = request.get_json(force=True)

        # required
        for f in ["pdf", "signature", "x_percent", "y_percent"]:
            if f not in body:
                return jsonify({"error": f"Missing required field: '{f}'"}), 400

        pdf_bytes   = b64_decode(body["pdf"])
        stamp_bytes = b64_decode(body["signature"])

        x_pct = float(body["x_percent"])
        y_pct = float(body["y_percent"])

        # optional axis flips
        if body.get("flip_x", False):
            x_pct = 100 - x_pct
        if body.get("flip_y", False):
            y_pct = 100 - y_pct

        # stamp size — percent keys take priority over fixed-px keys
        sw_pct = float(body["stamp_width_percent"])  if "stamp_width_percent"  in body else None
        sh_pct = float(body["stamp_height_percent"]) if "stamp_height_percent" in body else None
        sw_px  = float(body.get("stamp_width",  70)) if sw_pct is None else None
        sh_px  = float(body.get("stamp_height", 30)) if sh_pct is None else None

        # date / text options
        date_text  = str(body.get("date_text",    ""))
        date_x_pct = float(body.get("date_x_percent", x_pct))
        date_y_pct = float(body.get("date_y_percent", max(y_pct - 4, 0)))
        font_size  = float(body.get("date_font_size", 8))
        padding    = float(body.get("padding", 0))

        # page selection
        occurrence  = str(body.get("occurrence", "all"))
        pages_param = str(body.get("pages",      "all"))

        # validate
        for name, val in [("x_percent", x_pct), ("y_percent", y_pct)]:
            if not (0 <= val <= 100):
                return jsonify({"error": f"{name} must be 0-100, got {val}"}), 400
        if sw_pct is not None and not (0 < sw_pct <= 100):
            return jsonify({"error": f"stamp_width_percent must be 0-100, got {sw_pct}"}), 400
        if sh_pct is not None and not (0 < sh_pct <= 100):
            return jsonify({"error": f"stamp_height_percent must be 0-100, got {sh_pct}"}), 400

        # process PDF
        reader      = PdfReader(io.BytesIO(pdf_bytes))
        writer      = PdfWriter()
        total_pages = len(reader.pages)

        stamp_set = set(resolve_occurrence(
            parse_pages(pages_param, total_pages), occurrence
        ))

        for idx, page in enumerate(reader.pages):
            if idx in stamp_set:
                box = page.mediabox
                pw  = float(box.width)
                ph  = float(box.height)

                # resolve stamp size in pts
                sw_pt = (pw * sw_pct / 100) if sw_pct is not None else sw_px
                sh_pt = (ph * sh_pct / 100) if sh_pct is not None else sh_px

                # convert % positions → pts
                # x: left-origin (same as ReportLab)
                # y: top-origin input → flip to ReportLab bottom-origin
                x_pt = pw * x_pct / 100 + padding
                y_pt = ph * (1 - y_pct / 100) - sh_pt - padding

                date_x_pt = pw * date_x_pct / 100
                date_y_pt = ph * (1 - date_y_pct / 100)

                overlay = make_overlay(
                    pw, ph, stamp_bytes,
                    x_pt, y_pt, sw_pt, sh_pt,
                    date_text, date_x_pt, date_y_pt, font_size,
                )
                page.merge_page(PdfReader(io.BytesIO(overlay)).pages[0])

            writer.add_page(page)

        out = io.BytesIO()
        writer.write(out)
        out.seek(0)

        return jsonify({"pdf": base64.b64encode(out.read()).decode()}), 200

    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


# ── entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
