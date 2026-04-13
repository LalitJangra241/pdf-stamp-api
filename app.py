"""
PDF STAMP API — Flask Server
============================

All position + size values are PERCENTAGES (%) of page dimensions.

Sheet column mapping (row 12):
  A = Name
  B = Role / dept
  C = Doer Sign Stamp URL       → stamp image
  D = Doer Sign Stamp X %       → x_percent
  E = Doer Sign Stamp Y %       → y_percent
  F = Timestamp text            → date_text
  G = Timestamp X %             → date_x_percent
  H = Timestamp Y %             → date_y_percent
  I = STAMP_WIDTH %             → stamp_width_percent  (2 = 2% of page width)
  J = STAMP_HEIGHT %            → stamp_height_percent (3 = 3% of page height)
  K = Timestamp_FONT_SIZE (pt)  → date_font_size (A4 base, auto-scales)

Fixed cells:
  C2  = Form submission timestamp
  C10 = PDF Drive link

Endpoints:
  GET  /health  → health check / server wake
  POST /stamp   → stamp PDF, returns base64 PDF

Auth     : x-api-key header
Max size : 50 MB
Response : { "pdf": "<base64>", "stamped_pdf": "<base64>" }
"""

import os
import io
import base64
import logging
from functools import lru_cache

from flask import Flask, request, jsonify
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image

# ─────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

API_KEY          = os.environ.get("PDF_STAMP_API_KEY", "pdf-stamp-api")
MAX_BODY_BYTES   = 50 * 1024 * 1024
A4_HEIGHT_PT     = 841.89
DEFAULT_SIZE_PCT = 3.0


# ─────────────────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────────────────

def check_auth(req) -> bool:
    return req.headers.get("x-api-key") == API_KEY


def _to_float(value, default=None):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=256)
def _parse_pages_cached(page_str: str, total_pages: int) -> tuple:
    if not page_str or page_str.strip().lower() == "all":
        return tuple(range(total_pages))
    indices = set()
    for part in page_str.split(","):
        part = part.strip()
        if "-" in part:
            s, e = part.split("-", 1)
            for p in range(int(s), int(e) + 1):
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                indices.add(p - 1)
    return tuple(sorted(indices))


def parse_pages(page_str: str, total_pages: int) -> list:
    return list(_parse_pages_cached(str(page_str).strip(), total_pages))


def filter_occurrence(indices: list, occurrence: str) -> list:
    if not indices:
        return indices
    occ = str(occurrence).strip().lower()
    if occ == "first": return [indices[0]]
    if occ == "last":  return [indices[-1]]
    return indices


# ─────────────────────────────────────────────────────────
# STAMP SIZE
# ─────────────────────────────────────────────────────────

def resolve_stamp_size(page_w, page_h, stamp_width_percent, stamp_height_percent):
    """
    stamp_width_percent  = % of page width  → I12 = 2 means 2% of page width
    stamp_height_percent = % of page height → J12 = 3 means 3% of page height

    Scales automatically with page size:
      A4  (595pt wide)  + 2% → 11.9pt wide  (~4.2mm)
      A4  (842pt tall)  + 3% → 25.3pt tall  (~8.9mm)
      A3  (842pt wide)  + 2% → 16.8pt wide  (~5.9mm)
      A3  (1191pt tall) + 3% → 35.7pt tall  (~12.6mm)
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = float(stamp_width_percent)
        sh = float(stamp_height_percent)

        if sw <= 0 or sh <= 0:
            raise ValueError(
                f"stamp_width_percent and stamp_height_percent must be > 0. "
                f"Got: W={sw}% H={sh}%"
            )

        sw_pt = (sw / 100.0) * page_w
        sh_pt = (sh / 100.0) * page_h
        logger.info(
            "Stamp size | page=%.1f×%.1fpt | "
            "W=%.1f%%→%.2fpt(%.1fmm) | H=%.1f%%→%.2fpt(%.1fmm)",
            page_w, page_h,
            sw, sw_pt, sw_pt / 2.835,
            sh, sh_pt, sh_pt / 2.835,
        )
        return sw, sh

    logger.warning(
        "stamp_width/height_percent missing — using fallback %.1f%%×%.1f%%",
        DEFAULT_SIZE_PCT, DEFAULT_SIZE_PCT,
    )
    return DEFAULT_SIZE_PCT, DEFAULT_SIZE_PCT


# ─────────────────────────────────────────────────────────
# OVERLAY BUILDER
# ─────────────────────────────────────────────────────────

def build_stamp_overlay(page_w, page_h, stamp_img_bytes,
                        x_pct, y_pct,
                        sw_pct, sh_pct,
                        date_text=None,
                        date_x_pct=None, date_y_pct=None,
                        date_font_size=6.0,
                        flip_x=False, flip_y=False) -> bytes:
    """
    Render stamp image + optional timestamp onto a transparent PDF layer.
    All inputs use TOP-LEFT origin (same as Google Sheet).
    ReportLab uses BOTTOM-LEFT origin — converted internally.
    Font size auto-scales proportionally to page height vs A4.
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_w, page_h))

    # Convert % → absolute pt
    sw    = (sw_pct / 100.0) * page_w
    sh    = (sh_pct / 100.0) * page_h
    raw_x = (x_pct  / 100.0) * page_w
    raw_y = (y_pct  / 100.0) * page_h

    # Top-left → ReportLab bottom-left origin
    stamp_x = (page_w - raw_x - sw) if flip_x else raw_x
    stamp_y = raw_y                  if flip_y else (page_h - raw_y - sh)

    logger.info(
        "Stamp | page=%.1f×%.1fpt | size=%.1f×%.1fpt | "
        "pos=(%.2f%%,%.2f%%) → rl=(%.1f,%.1f)pt",
        page_w, page_h, sw, sh,
        x_pct, y_pct, stamp_x, stamp_y,
    )

    # Draw stamp (PNG transparency supported)
    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_reader = ImageReader(img.convert("RGBA"))
            c.drawImage(img_reader, stamp_x, stamp_y,
                        width=sw, height=sh, mask="auto")
    except Exception as exc:
        logger.error("Failed to draw stamp: %s", exc)
        raise

    # Draw optional timestamp
    if date_text:
        dx_pct = date_x_pct if date_x_pct is not None else x_pct
        dy_pct = date_y_pct if date_y_pct is not None else y_pct

        raw_dx = (dx_pct / 100.0) * page_w
        raw_dy = (dy_pct / 100.0) * page_h

        # Scale font proportionally to page height (base = A4)
        scaled_font = date_font_size * (page_h / A4_HEIGHT_PT)

        date_x = (page_w - raw_dx) if flip_x else raw_dx
        # Baseline sits 2pt above stamp top edge
        date_y = (raw_dy + sh + 2) if flip_y else (page_h - raw_dy + 2)

        logger.info(
            "Date | text='%s' font=%.1fpt (scaled from %.1fpt for A4) | "
            "pos=(%.2f%%,%.2f%%) → rl=(%.1f,%.1f)pt",
            date_text, scaled_font, date_font_size,
            dx_pct, dy_pct, date_x, date_y,
        )

        c.setFont("Helvetica", scaled_font)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(date_x, date_y, str(date_text))

    c.save()
    return packet.getvalue()


# ─────────────────────────────────────────────────────────
# CORE STAMP FUNCTION
# ─────────────────────────────────────────────────────────

def stamp_pdf(pdf_bytes, stamp_bytes,
              x_pct, y_pct,
              stamp_width_percent, stamp_height_percent,
              date_text=None, date_x_pct=None, date_y_pct=None,
              date_font_size=6.0,
              pages="all", occurrence="all",
              flip_x=False, flip_y=False) -> bytes:
    """
    Apply stamp to selected pages.
    Page size is read per-page from the PDF, so mixed-size PDFs work correctly.
    """
    reader       = PdfReader(io.BytesIO(pdf_bytes))
    writer       = PdfWriter()
    total        = len(reader.pages)
    page_indices = filter_occurrence(parse_pages(pages, total), occurrence)
    stamp_set    = set(page_indices)

    logger.info("PDF: %d page(s) | stamping pages: %s", total, [p+1 for p in page_indices])

    for idx in range(total):
        page   = reader.pages[idx]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        if idx in stamp_set:
            logger.info("Page %d | %.1f × %.1f pt", idx + 1, page_w, page_h)

            sw_pct, sh_pct = resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent,
                stamp_height_percent,
            )

            overlay_bytes = build_stamp_overlay(
                page_w, page_h, stamp_bytes,
                x_pct, y_pct,
                sw_pct, sh_pct,
                date_text      = date_text,
                date_x_pct     = date_x_pct,
                date_y_pct     = date_y_pct,
                date_font_size = date_font_size,
                flip_x         = flip_x,
                flip_y         = flip_y,
            )
            page.merge_page(PdfReader(io.BytesIO(overlay_bytes)).pages[0])

        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():
    """
    Required JSON fields:
      pdf                  : base64 PDF
      stamp                : base64 stamp image (PNG recommended)
      x_percent            : stamp left edge  — % of page width   (col D)
      y_percent            : stamp top  edge  — % of page height  (col E)
      stamp_width_percent  : stamp width      — % of page width   (col I, e.g. 2)
      stamp_height_percent : stamp height     — % of page height  (col J, e.g. 3)

    Optional timestamp:
      date_text            : e.g. "10/04/2026"                    (col F)
      date_x_percent       : timestamp left   — % of page width   (col G)
      date_y_percent       : timestamp top    — % of page height  (col H)
      date_font_size       : pt size for A4, auto-scales           (col K)

    Other options:
      pages      : "all" | "1" | "1-3" | "1,3,5"   (default "all")
      occurrence : "all" | "first" | "last"          (default "all")
      flip_x     : boolean                           (default false)
      flip_y     : boolean                           (default false)
    """

    if not check_auth(request):
        logger.warning("Unauthorized request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    cl = request.content_length
    if cl and cl > MAX_BODY_BYTES:
        return jsonify({"error": f"Request too large (max {MAX_BODY_BYTES//1024//1024} MB)"}), 413

    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400
    if not data:
        return jsonify({"error": "Empty request body"}), 400

    # Required fields
    pdf_b64     = data.get("pdf")
    stamp_b64   = data.get("stamp")
    x_pct       = _to_float(data.get("x_percent"))
    y_pct       = _to_float(data.get("y_percent"))
    stamp_w_pct = _to_float(data.get("stamp_width_percent"))
    stamp_h_pct = _to_float(data.get("stamp_height_percent"))

    if not pdf_b64:         return jsonify({"error": "Missing: pdf"}), 400
    if not stamp_b64:       return jsonify({"error": "Missing: stamp"}), 400
    if x_pct       is None: return jsonify({"error": "Missing: x_percent (col D)"}), 400
    if y_pct       is None: return jsonify({"error": "Missing: y_percent (col E)"}), 400
    if stamp_w_pct is None: return jsonify({"error": "Missing: stamp_width_percent (col I)"}), 400
    if stamp_h_pct is None: return jsonify({"error": "Missing: stamp_height_percent (col J)"}), 400

    if not (0 <= x_pct       <= 100): return jsonify({"error": f"x_percent out of range: {x_pct}"}), 400
    if not (0 <= y_pct       <= 100): return jsonify({"error": f"y_percent out of range: {y_pct}"}), 400
    if not (0 <  stamp_w_pct <= 100): return jsonify({"error": f"stamp_width_percent out of range: {stamp_w_pct}"}), 400
    if not (0 <  stamp_h_pct <= 100): return jsonify({"error": f"stamp_height_percent out of range: {stamp_h_pct}"}), 400

    # Optional timestamp
    date_text  = data.get("date_text")
    date_x_pct = _to_float(data.get("date_x_percent"))
    date_y_pct = _to_float(data.get("date_y_percent"))
    font_size  = _to_float(data.get("date_font_size"), 6.0)

    if date_text and (font_size is None or font_size <= 0):
        return jsonify({"error": f"date_font_size must be > 0 when date_text is provided. Got: {font_size}"}), 400

    # Other options
    pages      = data.get("pages", "all")
    occurrence = data.get("occurrence", "all")
    flip_x     = bool(data.get("flip_x", False))
    flip_y     = bool(data.get("flip_y", False))

    logger.info("=" * 60)
    logger.info("INCOMING /stamp")
    logger.info("  x_percent            = %s  (col D)", x_pct)
    logger.info("  y_percent            = %s  (col E)", y_pct)
    logger.info("  stamp_width_percent  = %s  (col I — 2 = 2%% of page width)", stamp_w_pct)
    logger.info("  stamp_height_percent = %s  (col J — 3 = 3%% of page height)", stamp_h_pct)
    logger.info("  date_text            = %s  (col F)", date_text)
    logger.info("  date_x_percent       = %s  (col G)", date_x_pct)
    logger.info("  date_y_percent       = %s  (col H)", date_y_pct)
    logger.info("  date_font_size       = %s  (col K, A4 base)", font_size)
    logger.info("  pages                = %s", pages)
    logger.info("  occurrence           = %s", occurrence)
    logger.info("=" * 60)

    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception as e:
        return jsonify({"error": f"Base64 decode failed: {e}"}), 400

    logger.info("PDF: %d bytes | Stamp: %d bytes", len(pdf_bytes), len(stamp_bytes))

    try:
        result = stamp_pdf(
            pdf_bytes, stamp_bytes,
            x_pct, y_pct,
            stamp_width_percent  = stamp_w_pct,
            stamp_height_percent = stamp_h_pct,
            date_text            = date_text,
            date_x_pct           = date_x_pct,
            date_y_pct           = date_y_pct,
            date_font_size       = font_size,
            pages                = pages,
            occurrence           = occurrence,
            flip_x               = flip_x,
            flip_y               = flip_y,
        )
    except Exception as e:
        logger.exception("stamp_pdf failed")
        return jsonify({"error": str(e)}), 500

    out_b64 = base64.b64encode(result).decode("utf-8")
    logger.info("Done | output b64 length: %d", len(out_b64))
    return jsonify({"pdf": out_b64, "stamped_pdf": out_b64}), 200


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
