"""
PDF STAMP API — Flask Server
============================

Stamp sizing (percent of real page dimensions):
  stamp_width_percent  → final stamp width  = (value/100) × page_width_pt
  stamp_height_percent → final stamp height = (value/100) × page_height_pt

  Example: A4 page = 595 × 842 pt, stamp_width_percent=2, stamp_height_percent=2
    stamp_width  = 0.02 × 595 = 11.90 pt
    stamp_height = 0.02 × 842 = 16.84 pt

Stamp position (percent of page, top-left origin):
  x_percent → left edge of stamp = (value/100) × page_width_pt
  y_percent → top  edge of stamp = (value/100) × page_height_pt
  (ReportLab uses bottom-left origin; conversion is applied internally)

Optional timestamp text:
  date_text       → string to render (e.g. "10/04/2026")
  date_x_percent  → horizontal position, % of page width
  date_y_percent  → vertical position,   % of page height
  date_font_size  → font size in pt

Other options:
  pages      → "all" | "1" | "1-3" | "1,3,5"   (default: "all")
  occurrence → "all" | "first" | "last"          (default: "all")
  flip_x     → mirror stamp horizontally          (default: false)
  flip_y     → mirror stamp vertically            (default: false)

Auth: x-api-key header
Max request size: 50 MB
Returns: { "pdf": "<base64>", "stamped_pdf": "<base64>" }
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
    """Safely convert a value to float; return default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=256)
def _parse_pages_cached(page_str: str, total_pages: int) -> tuple:
    """
    Convert a page specification string to a tuple of 0-based page indices.

    Formats supported:
      "all"     → every page
      "1"       → page 1 only
      "1-3"     → pages 1, 2, 3
      "1,3,5"   → pages 1, 3, 5
      "1-3,5"   → pages 1, 2, 3, 5
    """
    if not page_str or page_str.strip().lower() == "all":
        return tuple(range(total_pages))

    indices = set()
    for part in page_str.split(","):
        part = part.strip()
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s), int(end_s)
            for p in range(start, end + 1):
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                indices.add(p - 1)

    return tuple(sorted(indices))


def parse_pages(page_str: str, total_pages: int) -> list:
    return list(_parse_pages_cached(str(page_str).strip(), total_pages))


def filter_occurrence(page_indices: list, occurrence: str) -> list:
    """Return a subset of page_indices based on occurrence string."""
    if not page_indices:
        return page_indices
    occ = str(occurrence).strip().lower()
    if occ == "first":
        return [page_indices[0]]
    if occ == "last":
        return [page_indices[-1]]
    return page_indices  # "all" or unrecognised → keep all


# ─────────────────────────────────────────────────────────
# STAMP SIZE RESOLVER
# ─────────────────────────────────────────────────────────

def resolve_stamp_size(
    page_w: float,
    page_h: float,
    stamp_width_percent,
    stamp_height_percent,
    stamp_width_px,
    stamp_height_px,
) -> tuple:
    """
    Determine the final stamp dimensions in PDF points.

    Priority:
      1. stamp_width_percent / stamp_height_percent
         The sheet sends these from cols I/J (e.g. 2 = 2% of page dimension).
         stamp_width_pt  = (stamp_width_percent  / 100) × page_width_pt
         stamp_height_pt = (stamp_height_percent / 100) × page_height_pt

      2. stamp_width_px / stamp_height_px   — fixed point values, no scaling
      3. Default fallback                   — 15% × 10% of page
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent  / 100.0) * page_w
        sh = (stamp_height_percent / 100.0) * page_h
        logger.info(
            "Stamp size (%%): w_pct=%.4f × page_w=%.2fpt = %.4fpt | "
            "h_pct=%.4f × page_h=%.2fpt = %.4fpt",
            stamp_width_percent, page_w, sw,
            stamp_height_percent, page_h, sh,
        )
        if sw <= 0 or sh <= 0:
            raise ValueError(
                f"Stamp size resolved to zero/negative: sw={sw:.4f}pt sh={sh:.4f}pt "
                f"(w_pct={stamp_width_percent} h_pct={stamp_height_percent})"
            )
        return sw, sh

    if stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("Stamp size (fixed): sw=%.2fpt sh=%.2fpt", sw, sh)
        return sw, sh

    sw = 0.15 * page_w
    sh = 0.10 * page_h
    logger.warning(
        "Stamp size not provided — using default 15%%×10%% of page: "
        "sw=%.2fpt sh=%.2fpt", sw, sh
    )
    return sw, sh


# ─────────────────────────────────────────────────────────
# OVERLAY BUILDER
# ─────────────────────────────────────────────────────────

def build_stamp_overlay(
    page_width_pt: float,
    page_height_pt: float,
    stamp_img_bytes: bytes,
    x_percent: float,
    y_percent: float,
    stamp_width_pt: float,
    stamp_height_pt: float,
    date_text=None,
    date_x_percent=None,
    date_y_percent=None,
    date_font_size: float = 6.0,
    flip_x: bool = False,
    flip_y: bool = False,
) -> bytes:
    """
    Render stamp image (and optional timestamp) onto a transparent PDF overlay.

    Coordinate conversion:
      Input  : x_percent / y_percent measure from the TOP-LEFT corner
               (same convention as CSS / the sheet values).
      ReportLab uses a BOTTOM-LEFT origin, so we convert:
        rl_y = page_height - (y_percent/100 × page_height) - stamp_height
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    # ── Convert top-left % coords to ReportLab bottom-left pts ──
    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    stamp_x = (page_width_pt  - raw_x - stamp_width_pt)  if flip_x else raw_x
    stamp_y = raw_y                                        if flip_y else (page_height_pt - raw_y - stamp_height_pt)

    logger.info(
        "Stamp draw | page=(%.2f × %.2f)pt | size=(%.2f × %.2f)pt | "
        "in=(%.2f%%, %.2f%%) raw=(%.2f, %.2f)pt → rl=(%.2f, %.2f)pt",
        page_width_pt, page_height_pt,
        stamp_width_pt, stamp_height_pt,
        x_percent, y_percent,
        raw_x, raw_y,
        stamp_x, stamp_y,
    )

    # ── Draw stamp image ─────────────────────────────────────────
    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_rgba   = img.convert("RGBA")
            img_reader = ImageReader(img_rgba)
            c.drawImage(
                img_reader,
                stamp_x, stamp_y,
                width=stamp_width_pt,
                height=stamp_height_pt,
                mask="auto",          # honour PNG transparency
            )
    except Exception as exc:
        logger.error("Failed to draw stamp image: %s", exc)
        raise

    # ── Draw optional timestamp text ─────────────────────────────
    if date_text:
        dx_pct = date_x_percent if date_x_percent is not None else x_percent
        dy_pct = date_y_percent if date_y_percent is not None else y_percent

        raw_dx = (dx_pct / 100.0) * page_width_pt
        raw_dy = (dy_pct / 100.0) * page_height_pt

        # Place text just above the stamp's top edge (+2 pt padding)
        date_x = (page_width_pt - raw_dx)        if flip_x else raw_dx
        date_y = (raw_dy + stamp_height_pt + 2)  if flip_y else (page_height_pt - raw_dy + 2)

        logger.info(
            "Date draw | text='%s' font=%.1fpt | "
            "in=(%.2f%%, %.2f%%) → rl=(%.2f, %.2f)pt",
            date_text, date_font_size,
            dx_pct, dy_pct,
            date_x, date_y,
        )

        c.setFont("Helvetica", date_font_size)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(date_x, date_y, str(date_text))

    c.save()
    return packet.getvalue()


# ─────────────────────────────────────────────────────────
# CORE STAMP FUNCTION
# ─────────────────────────────────────────────────────────

def stamp_pdf(
    pdf_bytes: bytes,
    stamp_bytes: bytes,
    x_percent: float,
    y_percent: float,
    stamp_width_percent=None,
    stamp_height_percent=None,
    stamp_width_px=None,
    stamp_height_px=None,
    date_text=None,
    date_x_percent=None,
    date_y_percent=None,
    date_font_size: float = 6.0,
    pages: str = "all",
    occurrence: str = "all",
    flip_x: bool = False,
    flip_y: bool = False,
) -> bytes:
    """
    Apply the stamp to the selected pages of the PDF.
    The stamp is sized using the REAL page dimensions read from the PDF itself.
    Returns the fully stamped PDF as bytes.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    total  = len(reader.pages)

    page_indices = parse_pages(pages, total)
    page_indices = filter_occurrence(page_indices, occurrence)
    stamp_set    = set(page_indices)

    logger.info(
        "PDF: %d pages | stamping pages: %s",
        total,
        [p + 1 for p in page_indices],
    )

    for idx in range(total):
        page   = reader.pages[idx]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        if idx in stamp_set:
            logger.info("Processing page %d | %.2fpt × %.2fpt", idx + 1, page_w, page_h)

            # Resolve stamp size using REAL page dimensions from the PDF
            sw, sh = resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent, stamp_height_percent,
                stamp_width_px, stamp_height_px,
            )

            overlay_bytes = build_stamp_overlay(
                page_w, page_h, stamp_bytes,
                x_percent, y_percent,
                sw, sh,
                date_text=date_text,
                date_x_percent=date_x_percent,
                date_y_percent=date_y_percent,
                date_font_size=date_font_size,
                flip_x=flip_x,
                flip_y=flip_y,
            )

            overlay_page = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
            page.merge_page(overlay_page)

        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ─────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check — called by Apps Script to wake the server."""
    return jsonify({"status": "ok"}), 200


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():

    # ── Auth ──────────────────────────────────────────────
    if not check_auth(request):
        logger.warning("Unauthorized request from %s", request.remote_addr)
        return jsonify({"error": "Unauthorized"}), 401

    # ── Request size guard ────────────────────────────────
    cl = request.content_length
    if cl and cl > MAX_BODY_BYTES:
        logger.warning("Request too large: %d bytes", cl)
        return jsonify({"error": f"Request too large (max {MAX_BODY_BYTES // 1024 // 1024} MB)"}), 413

    # ── Parse JSON body ───────────────────────────────────
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

    if not pdf_b64:
        return jsonify({"error": "Missing required field: pdf (base64 string)"}), 400
    if not stamp_b64:
        return jsonify({"error": "Missing required field: stamp (base64 string)"}), 400
    if x_pct is None:
        return jsonify({"error": "Missing required field: x_percent"}), 400
    if y_pct is None:
        return jsonify({"error": "Missing required field: y_percent"}), 400

    # ── Validate coordinate range ─────────────────────────
    if not (0 <= x_pct <= 100):
        return jsonify({"error": f"x_percent must be 0–100, got {x_pct}"}), 400
    if not (0 <= y_pct <= 100):
        return jsonify({"error": f"y_percent must be 0–100, got {y_pct}"}), 400

    # ── Optional fields ───────────────────────────────────
    stamp_width_percent  = _to_float(data.get("stamp_width_percent"))
    stamp_height_percent = _to_float(data.get("stamp_height_percent"))
    stamp_width_px       = _to_float(data.get("stamp_width_px"))
    stamp_height_px      = _to_float(data.get("stamp_height_px"))
    date_text            = data.get("date_text")          # str or None
    date_x_percent       = _to_float(data.get("date_x_percent"))
    date_y_percent       = _to_float(data.get("date_y_percent"))
    date_font_size       = _to_float(data.get("date_font_size"), default=6.0)
    pages                = data.get("pages", "all")
    occurrence           = data.get("occurrence", "all")
    flip_x               = bool(data.get("flip_x", False))
    flip_y               = bool(data.get("flip_y", False))

    # ── Validate stamp size % ─────────────────────────────
    if stamp_width_percent is not None:
        if stamp_width_percent <= 0:
            return jsonify({"error": f"stamp_width_percent must be > 0, got {stamp_width_percent}"}), 400
        if stamp_width_percent > 100:
            return jsonify({"error": f"stamp_width_percent must be ≤ 100, got {stamp_width_percent}"}), 400

    if stamp_height_percent is not None:
        if stamp_height_percent <= 0:
            return jsonify({"error": f"stamp_height_percent must be > 0, got {stamp_height_percent}"}), 400
        if stamp_height_percent > 100:
            return jsonify({"error": f"stamp_height_percent must be ≤ 100, got {stamp_height_percent}"}), 400

    # ── Validate font size when timestamp present ─────────
    if date_text and (date_font_size is None or date_font_size <= 0):
        return jsonify({
            "error": f"date_font_size must be > 0 when date_text is provided, got {date_font_size}"
        }), 400

    # ── Full request log ──────────────────────────────────
    logger.info("=" * 60)
    logger.info("INCOMING /stamp REQUEST")
    logger.info("  x_percent            = %s",  x_pct)
    logger.info("  y_percent            = %s",  y_pct)
    logger.info("  stamp_width_percent  = %s  (col I from sheet)", stamp_width_percent)
    logger.info("  stamp_height_percent = %s  (col J from sheet)", stamp_height_percent)
    logger.info("  stamp_width_px       = %s",  stamp_width_px)
    logger.info("  stamp_height_px      = %s",  stamp_height_px)
    logger.info("  date_text            = %s",  date_text)
    logger.info("  date_x_percent       = %s",  date_x_percent)
    logger.info("  date_y_percent       = %s",  date_y_percent)
    logger.info("  date_font_size       = %s",  date_font_size)
    logger.info("  pages                = %s",  pages)
    logger.info("  occurrence           = %s",  occurrence)
    logger.info("  flip_x               = %s",  flip_x)
    logger.info("  flip_y               = %s",  flip_y)
    logger.info("=" * 60)

    # ── Decode base64 inputs ──────────────────────────────
    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception as e:
        return jsonify({"error": f"Base64 decode failed: {e}"}), 400

    logger.info(
        "Decoded | PDF: %d bytes | Stamp image: %d bytes",
        len(pdf_bytes), len(stamp_bytes)
    )

    # ── Apply stamp ───────────────────────────────────────
    try:
        result_bytes = stamp_pdf(
            pdf_bytes, stamp_bytes,
            x_pct, y_pct,
            stamp_width_percent  = stamp_width_percent,
            stamp_height_percent = stamp_height_percent,
            stamp_width_px       = stamp_width_px,
            stamp_height_px      = stamp_height_px,
            date_text            = date_text,
            date_x_percent       = date_x_percent,
            date_y_percent       = date_y_percent,
            date_font_size       = date_font_size,
            pages                = pages,
            occurrence           = occurrence,
            flip_x               = flip_x,
            flip_y               = flip_y,
        )
    except Exception as e:
        logger.exception("stamp_pdf failed")
        return jsonify({"error": str(e)}), 500

    # ── Encode and return ─────────────────────────────────
    result_b64 = base64.b64encode(result_bytes).decode("utf-8")
    logger.info("Done | output b64 length: %d", len(result_b64))

    # Both keys returned for backward compatibility with the Apps Script
    return jsonify({
        "pdf":         result_b64,
        "stamped_pdf": result_b64,
    }), 200


# ─────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
