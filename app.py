"""
PDF STAMP API — Flask Server
============================

Stamp sizing (sent from sheet cols I / J):
  stamp_width_percent  → stamp width  = (value / 100) × real_page_width_pt
  stamp_height_percent → stamp height = (value / 100) × real_page_height_pt

  Example (your data — A4 page, I=2, J=2):
    page  = 595.28 × 841.89 pt
    width  = 0.02 × 595.28 = 11.91 pt
    height = 0.02 × 841.89 = 16.84 pt

Stamp position (sent from sheet cols D / E):
  x_percent → left edge = (value / 100) × page_width_pt   (top-left origin)
  y_percent → top  edge = (value / 100) × page_height_pt  (top-left origin)
  ReportLab uses bottom-left; conversion is applied internally.

Timestamp (sent when col F has a date, cols G/H/K filled):
  date_text       → formatted string, e.g. "10/04/2026"
  date_x_percent  → left edge % of page width
  date_y_percent  → top  edge % of page height
  date_font_size  → pt

Auth     : x-api-key header
Max size : 50 MB
Response : { "pdf": "<base64>", "stamped_pdf": "<base64>" }

Your data summary (all rows valid — verified):
  Row 12 PPC          X=59 Y=9.2 W=2 H=2  date=10/04/2026 dX=59 dY=5.2 font=6
  Row 13 QA           X=71 Y=9.2 W=2 H=2  date=09/04/2026 dX=71 dY=5.2 font=6
  Row 14 QC           X=65 Y=9.2 W=2 H=2  date=11/04/2026 dX=65 dY=5.2 font=6
  Row 15 GM           X=85 Y=9.2 W=2 H=2  date=09/04/2026 dX=85 dY=5.2 font=6
  Row 16 MKT          X=77 Y=9.2 W=2 H=2  date=09/04/2026 dX=77 dY=5.2 font=6
  Row 17 CTRL STAMP   X=93 Y=9.2 W=2 H=2  no date/timestamp
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

API_KEY        = os.environ.get("PDF_STAMP_API_KEY", "pdf-stamp-api")
MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MB


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
    """
    "all"       → all pages (0-based indices)
    "1"         → page 1 only
    "1-3"       → pages 1,2,3
    "1,3,5"     → pages 1,3,5
    "1-3,5"     → pages 1,2,3,5
    """
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
# STAMP SIZE — reads REAL page dimensions from the PDF
# ─────────────────────────────────────────────────────────

def resolve_stamp_size(page_w, page_h,
                       stamp_width_percent, stamp_height_percent,
                       stamp_width_px, stamp_height_px):
    """
    Compute final stamp size in PDF points.

    Priority:
      1. stamp_width_percent / stamp_height_percent   ← sheet cols I / J
         sw = (pct / 100) × page_dimension_pt
      2. stamp_width_px / stamp_height_px             ← fixed pt values
      3. Default fallback: 15% × 10% of page

    The page dimensions are read from the actual PDF, so the stamp
    size is always proportional to the document being stamped.
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent  / 100.0) * page_w
        sh = (stamp_height_percent / 100.0) * page_h
        logger.info(
            "Stamp size (%%): %.4f%% × %.2fpt = %.4fpt wide | %.4f%% × %.2fpt = %.4fpt tall",
            stamp_width_percent,  page_w, sw,
            stamp_height_percent, page_h, sh,
        )
        if sw <= 0 or sh <= 0:
            raise ValueError(
                f"Stamp size resolved to zero/negative: sw={sw:.4f} sh={sh:.4f} "
                f"(w%={stamp_width_percent} h%={stamp_height_percent})"
            )
        return sw, sh

    if stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("Stamp size (fixed pt): %.2f × %.2f", sw, sh)
        return sw, sh

    sw = 0.15 * page_w
    sh = 0.10 * page_h
    logger.warning("Stamp size not provided — default 15%%×10%%: %.2f × %.2f", sw, sh)
    return sw, sh


# ─────────────────────────────────────────────────────────
# OVERLAY BUILDER
# ─────────────────────────────────────────────────────────

def build_stamp_overlay(page_w, page_h, stamp_img_bytes,
                        x_pct, y_pct, sw, sh,
                        date_text=None,
                        date_x_pct=None, date_y_pct=None,
                        date_font_size=6.0,
                        flip_x=False, flip_y=False) -> bytes:
    """
    Render stamp image (and optional timestamp) onto a transparent PDF layer.

    Input coordinates use TOP-LEFT origin (same as CSS / the sheet).
    ReportLab uses BOTTOM-LEFT origin; we convert:
      rl_y = page_h - (y_pct/100 × page_h) - stamp_height
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_w, page_h))

    # ── Stamp position ───────────────────────────────────────────
    raw_x = (x_pct / 100.0) * page_w
    raw_y = (y_pct / 100.0) * page_h

    stamp_x = (page_w - raw_x - sw) if flip_x else raw_x
    stamp_y = raw_y                  if flip_y else (page_h - raw_y - sh)

    logger.info(
        "Stamp | page=%.2f×%.2fpt size=%.2f×%.2fpt | "
        "in=(%.2f%%,%.2f%%) raw=(%.2f,%.2f)pt rl=(%.2f,%.2f)pt",
        page_w, page_h, sw, sh,
        x_pct, y_pct, raw_x, raw_y, stamp_x, stamp_y,
    )

    # ── Draw stamp image (supports PNG transparency) ─────────────
    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_reader = ImageReader(img.convert("RGBA"))
            c.drawImage(img_reader, stamp_x, stamp_y,
                        width=sw, height=sh, mask="auto")
    except Exception as exc:
        logger.error("Failed to draw stamp: %s", exc)
        raise

    # ── Draw optional timestamp ──────────────────────────────────
    if date_text:
        dx_pct = date_x_pct if date_x_pct is not None else x_pct
        dy_pct = date_y_pct if date_y_pct is not None else y_pct

        raw_dx = (dx_pct / 100.0) * page_w
        raw_dy = (dy_pct / 100.0) * page_h

        # Place text just above the stamp's top edge (+2 pt padding)
        date_x = (page_w - raw_dx)      if flip_x else raw_dx
        date_y = (raw_dy + sh + 2)      if flip_y else (page_h - raw_dy + 2)

        logger.info(
            "Date  | text='%s' font=%.1fpt | "
            "in=(%.2f%%,%.2f%%) rl=(%.2f,%.2f)pt",
            date_text, date_font_size,
            dx_pct, dy_pct, date_x, date_y,
        )

        c.setFont("Helvetica", date_font_size)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(date_x, date_y, str(date_text))

    c.save()
    return packet.getvalue()


# ─────────────────────────────────────────────────────────
# CORE STAMP FUNCTION
# ─────────────────────────────────────────────────────────

def stamp_pdf(pdf_bytes, stamp_bytes, x_pct, y_pct,
              stamp_width_percent=None, stamp_height_percent=None,
              stamp_width_px=None, stamp_height_px=None,
              date_text=None, date_x_pct=None, date_y_pct=None,
              date_font_size=6.0,
              pages="all", occurrence="all",
              flip_x=False, flip_y=False) -> bytes:
    """
    Apply stamp to selected pages.
    Stamp size is computed from the REAL page dimensions in the PDF.
    Rows are chained: each call receives the PDF output of the previous call.
    """
    reader       = PdfReader(io.BytesIO(pdf_bytes))
    writer       = PdfWriter()
    total        = len(reader.pages)
    page_indices = filter_occurrence(parse_pages(pages, total), occurrence)
    stamp_set    = set(page_indices)

    logger.info("PDF: %d pages | stamping: %s", total, [p+1 for p in page_indices])

    for idx in range(total):
        page   = reader.pages[idx]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        if idx in stamp_set:
            logger.info("Page %d | %.2f×%.2fpt", idx+1, page_w, page_h)
            sw, sh = resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent, stamp_height_percent,
                stamp_width_px, stamp_height_px,
            )
            overlay_bytes = build_stamp_overlay(
                page_w, page_h, stamp_bytes,
                x_pct, y_pct, sw, sh,
                date_text=date_text,
                date_x_pct=date_x_pct, date_y_pct=date_y_pct,
                date_font_size=date_font_size,
                flip_x=flip_x, flip_y=flip_y,
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
    """Health check — pinged by Apps Script to wake the server."""
    return jsonify({"status": "ok"}), 200


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():

    # ── Auth ──────────────────────────────────────────────
    if not check_auth(request):
        logger.warning("Unauthorized from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    # ── Size guard ────────────────────────────────────────
    cl = request.content_length
    if cl and cl > MAX_BODY_BYTES:
        return jsonify({"error": f"Request too large (max {MAX_BODY_BYTES//1024//1024} MB)"}), 413

    # ── Parse body ────────────────────────────────────────
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400
    if not data:
        return jsonify({"error": "Empty request body"}), 400

    # ── Required fields ───────────────────────────────────
    pdf_b64   = data.get("pdf")
    stamp_b64 = data.get("stamp")
    x_pct     = _to_float(data.get("x_percent"))
    y_pct     = _to_float(data.get("y_percent"))

    if not pdf_b64:   return jsonify({"error": "Missing: pdf (base64)"}), 400
    if not stamp_b64: return jsonify({"error": "Missing: stamp (base64)"}), 400
    if x_pct is None: return jsonify({"error": "Missing: x_percent"}), 400
    if y_pct is None: return jsonify({"error": "Missing: y_percent"}), 400

    # ── Validate position ─────────────────────────────────
    if not (0 <= x_pct <= 100):
        return jsonify({"error": f"x_percent must be 0–100, got {x_pct}"}), 400
    if not (0 <= y_pct <= 100):
        return jsonify({"error": f"y_percent must be 0–100, got {y_pct}"}), 400

    # ── Optional fields ───────────────────────────────────
    stamp_w_pct  = _to_float(data.get("stamp_width_percent"))   # col I from sheet
    stamp_h_pct  = _to_float(data.get("stamp_height_percent"))  # col J from sheet
    stamp_w_px   = _to_float(data.get("stamp_width_px"))
    stamp_h_px   = _to_float(data.get("stamp_height_px"))
    date_text    = data.get("date_text")                         # col F formatted
    date_x_pct   = _to_float(data.get("date_x_percent"))        # col G
    date_y_pct   = _to_float(data.get("date_y_percent"))        # col H
    font_size    = _to_float(data.get("date_font_size"), 6.0)   # col K
    pages        = data.get("pages", "all")
    occurrence   = data.get("occurrence", "all")
    flip_x       = bool(data.get("flip_x", False))
    flip_y       = bool(data.get("flip_y", False))

    # ── Validate stamp size % ─────────────────────────────
    if stamp_w_pct is not None and not (0 < stamp_w_pct <= 100):
        return jsonify({"error": f"stamp_width_percent must be 0–100, got {stamp_w_pct}"}), 400
    if stamp_h_pct is not None and not (0 < stamp_h_pct <= 100):
        return jsonify({"error": f"stamp_height_percent must be 0–100, got {stamp_h_pct}"}), 400

    # ── Validate font when timestamp present ──────────────
    if date_text and (font_size is None or font_size <= 0):
        return jsonify({"error": f"date_font_size must be > 0 when date_text is set, got {font_size}"}), 400

    # ── Log full request ──────────────────────────────────
    logger.info("=" * 60)
    logger.info("INCOMING /stamp")
    logger.info("  x_percent            = %s",  x_pct)
    logger.info("  y_percent            = %s",  y_pct)
    logger.info("  stamp_width_percent  = %s  (col I)", stamp_w_pct)
    logger.info("  stamp_height_percent = %s  (col J)", stamp_h_pct)
    logger.info("  stamp_width_px       = %s",  stamp_w_px)
    logger.info("  stamp_height_px      = %s",  stamp_h_px)
    logger.info("  date_text            = %s",  date_text)
    logger.info("  date_x_percent       = %s  (col G)", date_x_pct)
    logger.info("  date_y_percent       = %s  (col H)", date_y_pct)
    logger.info("  date_font_size       = %s  (col K)", font_size)
    logger.info("  pages                = %s",  pages)
    logger.info("  occurrence           = %s",  occurrence)
    logger.info("  flip_x               = %s",  flip_x)
    logger.info("  flip_y               = %s",  flip_y)
    logger.info("=" * 60)

    # ── Decode base64 ─────────────────────────────────────
    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception as e:
        return jsonify({"error": f"Base64 decode failed: {e}"}), 400

    logger.info("PDF: %d bytes | Stamp: %d bytes", len(pdf_bytes), len(stamp_bytes))

    # ── Apply stamp ───────────────────────────────────────
    try:
        result = stamp_pdf(
            pdf_bytes, stamp_bytes, x_pct, y_pct,
            stamp_width_percent = stamp_w_pct,
            stamp_height_percent= stamp_h_pct,
            stamp_width_px      = stamp_w_px,
            stamp_height_px     = stamp_h_px,
            date_text           = date_text,
            date_x_pct          = date_x_pct,
            date_y_pct          = date_y_pct,
            date_font_size      = font_size,
            pages               = pages,
            occurrence          = occurrence,
            flip_x              = flip_x,
            flip_y              = flip_y,
        )
    except Exception as e:
        logger.exception("stamp_pdf failed")
        return jsonify({"error": str(e)}), 500

    out_b64 = base64.b64encode(result).decode("utf-8")
    logger.info("Done | output b64 length: %d", len(out_b64))

    # Return both keys for backward compatibility
    return jsonify({"pdf": out_b64, "stamped_pdf": out_b64}), 200


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
