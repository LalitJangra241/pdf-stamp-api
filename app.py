"""
PDF STAMP API — Flask Server
============================
- Stamp SIZE as percent of page dimensions:
    stamp_width_percent  = % of page WIDTH
    stamp_height_percent = % of page HEIGHT
- Stamp POSITION as percent of page dimensions:
    x_percent = % of page WIDTH  (left edge of stamp)
    y_percent = % of page HEIGHT (top edge of stamp)
- API key authentication via x-api-key header
- Page selection: "all", "1", "1-3", "1,3,5"
- Occurrence selection: "all", "first", "last"
- Optional date/timestamp text
- flip_x / flip_y axis correction
- Request size guard (50 MB limit)
- Returns both "pdf" and "stamped_pdf" keys for compatibility
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

API_KEY        = os.environ.get("PDF_STAMP_API_KEY", "pdf-stamp-api")
MAX_BODY_BYTES = 50 * 1024 * 1024


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
def _parse_pages_cached(page_str: str, total_pages: int):
    if not page_str or page_str.strip().lower() == "all":
        return tuple(range(total_pages))
    indices = set()
    for part in page_str.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            for p in range(int(start), int(end) + 1):
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
        else:
            p = int(part)
            if 1 <= p <= total_pages:
                indices.add(p - 1)
    return tuple(sorted(indices))


def parse_pages(page_str, total_pages) -> list:
    return list(_parse_pages_cached(str(page_str).strip(), total_pages))


def filter_occurrence(page_indices: list, occurrence: str) -> list:
    if not page_indices:
        return page_indices
    occ = str(occurrence).strip().lower()
    if occ == "first":
        return [page_indices[0]]
    if occ == "last":
        return [page_indices[-1]]
    return page_indices


def _resolve_stamp_size(page_w, page_h,
                        stamp_width_percent, stamp_height_percent,
                        stamp_width_px, stamp_height_px):
    """
    stamp_width_percent  → % of page WIDTH
    stamp_height_percent → % of page HEIGHT
    These are passed PER ROW from the sheet so must differ per call.
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent  / 100.0) * page_w
        sh = (stamp_height_percent / 100.0) * page_h
        logger.info(
            "✅ Stamp size (percent) → "
            "w_pct=%.4f page_w=%.2fpt → sw=%.4fpt | "
            "h_pct=%.4f page_h=%.2fpt → sh=%.4fpt",
            stamp_width_percent,  page_w, sw,
            stamp_height_percent, page_h, sh,
        )
        if sw <= 0 or sh <= 0:
            raise ValueError(
                "Stamp size zero/negative: sw={} sh={} "
                "(w%={} h%={})".format(sw, sh, stamp_width_percent, stamp_height_percent)
            )
        return sw, sh

    if stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("✅ Stamp size (fixed px) → sw=%.2fpt sh=%.2fpt", sw, sh)
        return sw, sh

    # Fallback default
    sw = 0.15 * page_w
    sh = 0.10 * page_h
    logger.info("⚠️  Stamp size (default 15%%x10%%) → sw=%.2fpt sh=%.2fpt", sw, sh)
    return sw, sh


def build_stamp_overlay(
    page_width_pt, page_height_pt, stamp_img_bytes,
    x_percent, y_percent,
    stamp_width_pt, stamp_height_pt,
    date_text=None, date_x_percent=None, date_y_percent=None,
    date_font_size=6, flip_x=False, flip_y=False,
) -> bytes:

    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    # ── Stamp position ─────────────────────────────────────
    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    stamp_x = (page_width_pt - raw_x - stamp_width_pt) if flip_x else raw_x
    stamp_y = raw_y if flip_y else (page_height_pt - raw_y - stamp_height_pt)

    logger.info(
        "📍 Stamp draw → "
        "page=(%.2f x %.2f)pt | size=(%.2f x %.2f)pt | "
        "input(x=%.2f%%, y=%.2f%%) → raw(%.2f, %.2f)pt → rl(%.2f, %.2f)pt",
        page_width_pt, page_height_pt,
        stamp_width_pt, stamp_height_pt,
        x_percent, y_percent,
        raw_x, raw_y,
        stamp_x, stamp_y,
    )

    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_rgba   = img.convert("RGBA")
            img_reader = ImageReader(img_rgba)
            c.drawImage(
                img_reader,
                stamp_x, stamp_y,
                width=stamp_width_pt,
                height=stamp_height_pt,
                mask="auto",
            )
    except Exception as exc:
        logger.error("❌ Failed to draw stamp image: %s", exc)
        raise

    # ── Date / timestamp ───────────────────────────────────
    if date_text:
        dx_pct = date_x_percent if date_x_percent is not None else x_percent
        dy_pct = date_y_percent if date_y_percent is not None else y_percent

        raw_dx = (dx_pct / 100.0) * page_width_pt
        raw_dy = (dy_pct / 100.0) * page_height_pt

        date_x = (page_width_pt - raw_dx)       if flip_x else raw_dx
        date_y = (raw_dy + stamp_height_pt + 2) if flip_y else (page_height_pt - raw_dy + 2)

        logger.info(
            "📅 Date draw → text='%s' font=%.1fpt | "
            "input(dx=%.2f%%, dy=%.2f%%) → rl(%.2f, %.2f)pt",
            date_text, date_font_size,
            dx_pct, dy_pct,
            date_x, date_y,
        )

        c.setFont("Helvetica", date_font_size)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(date_x, date_y, str(date_text))

    c.save()
    return packet.getvalue()


def stamp_pdf(
    pdf_bytes, stamp_bytes, x_percent, y_percent,
    stamp_width_percent=None, stamp_height_percent=None,
    stamp_width_px=None, stamp_height_px=None,
    date_text=None, date_x_percent=None, date_y_percent=None,
    date_font_size=6, pages="all", occurrence="all",
    flip_x=False, flip_y=False,
) -> bytes:

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    total  = len(reader.pages)

    page_indices = parse_pages(pages, total)
    page_indices = filter_occurrence(page_indices, occurrence)
    stamp_set    = set(page_indices)

    for idx in range(total):
        page   = reader.pages[idx]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        if idx in stamp_set:
            sw, sh = _resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent, stamp_height_percent,
                stamp_width_px, stamp_height_px,
            )
            overlay_bytes = build_stamp_overlay(
                page_w, page_h, stamp_bytes,
                x_percent, y_percent, sw, sh,
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


# ── ROUTES ────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():

    # ── Auth ───────────────────────────────────────────────
    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    # ── Size guard ─────────────────────────────────────────
    content_length = request.content_length
    if content_length and content_length > MAX_BODY_BYTES:
        return jsonify({"error": "Request too large (max 50 MB)"}), 413

    # ── Parse JSON ─────────────────────────────────────────
    try:
        data = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"error": "Invalid JSON body"}), 400

    if not data:
        return jsonify({"error": "Empty request body"}), 400

    # ── Required fields ────────────────────────────────────
    pdf_b64   = data.get("pdf")
    stamp_b64 = data.get("stamp")
    x_pct     = _to_float(data.get("x_percent"))
    y_pct     = _to_float(data.get("y_percent"))

    if not pdf_b64:
        return jsonify({"error": "Missing: pdf (base64)"}), 400
    if not stamp_b64:
        return jsonify({"error": "Missing: stamp (base64)"}), 400
    if x_pct is None:
        return jsonify({"error": "Missing: x_percent"}), 400
    if y_pct is None:
        return jsonify({"error": "Missing: y_percent"}), 400

    # ── Optional fields ────────────────────────────────────
    stamp_width_percent  = _to_float(data.get("stamp_width_percent"))
    stamp_height_percent = _to_float(data.get("stamp_height_percent"))
    stamp_width_px       = _to_float(data.get("stamp_width_px"))
    stamp_height_px      = _to_float(data.get("stamp_height_px"))
    date_text            = data.get("date_text")
    date_x_percent       = _to_float(data.get("date_x_percent"))
    date_y_percent       = _to_float(data.get("date_y_percent"))
    date_font_size       = _to_float(data.get("date_font_size"), default=6)
    pages                = data.get("pages", "all")
    occurrence           = data.get("occurrence", "all")
    flip_x               = bool(data.get("flip_x", False))
    flip_y               = bool(data.get("flip_y", False))

    # ── LOG EVERY INCOMING REQUEST FULLY ──────────────────
    logger.info("=" * 60)
    logger.info("📥 INCOMING REQUEST")
    logger.info("  x_percent            = %s", x_pct)
    logger.info("  y_percent            = %s", y_pct)
    logger.info("  stamp_width_percent  = %s", stamp_width_percent)   # <-- WATCH THIS
    logger.info("  stamp_height_percent = %s", stamp_height_percent)  # <-- WATCH THIS
    logger.info("  stamp_width_px       = %s", stamp_width_px)
    logger.info("  stamp_height_px      = %s", stamp_height_px)
    logger.info("  date_text            = %s", date_text)
    logger.info("  date_x_percent       = %s", date_x_percent)
    logger.info("  date_y_percent       = %s", date_y_percent)
    logger.info("  date_font_size       = %s", date_font_size)
    logger.info("  pages                = %s", pages)
    logger.info("  occurrence           = %s", occurrence)
    logger.info("  flip_x               = %s", flip_x)
    logger.info("  flip_y               = %s", flip_y)
    logger.info("=" * 60)

    # ── Decode base64 ──────────────────────────────────────
    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception as e:
        return jsonify({"error": "Base64 decode failed: " + str(e)}), 400

    # ── Process ────────────────────────────────────────────
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
        logger.exception("❌ stamp_pdf failed")
        return jsonify({"error": str(e)}), 500

    # ── Encode and return ──────────────────────────────────
    result_b64 = base64.b64encode(result_bytes).decode("utf-8")
    logger.info("✅ Done → output b64 len: %d", len(result_b64))

    return jsonify({
        "pdf":         result_b64,
        "stamped_pdf": result_b64,
    }), 200


# ── ENTRY POINT ───────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
