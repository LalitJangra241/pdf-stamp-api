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
- Optional date text above the stamp
- flip_x / flip_y axis correction support
- Request size guard (50 MB limit)
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


def _resolve_stamp_size(page_w, page_h, stamp_width_percent, stamp_height_percent,
                         stamp_width_px, stamp_height_px):
    """
    stamp_width_percent  → % of page WIDTH
    stamp_height_percent → % of page HEIGHT
    Example: page=1000x800, width%=40, height%=50
             → stamp = 400 x 400 pt
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent  / 100.0) * page_w   # 40% of page width
        sh = (stamp_height_percent / 100.0) * page_h   # 50% of page height
        logger.info(
            "Stamp size (percent): width=%.1f%% of page_w(%.1f) → %.2f pt | "
            "height=%.1f%% of page_h(%.1f) → %.2f pt",
            stamp_width_percent, page_w, sw,
            stamp_height_percent, page_h, sh
        )
    elif stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("Stamp size (fixed px): %.2f x %.2f pt", sw, sh)
    else:
        sw = 0.15 * page_w
        sh = 0.10 * page_h
        logger.info("Stamp size (default 15%%x10%%): %.2f x %.2f pt", sw, sh)

    return sw, sh


def build_stamp_overlay(
    page_width_pt, page_height_pt, stamp_img_bytes,
    x_percent, y_percent,
    stamp_width_pt, stamp_height_pt,
    date_text=None, date_x_percent=None, date_y_percent=None,
    date_font_size=6, flip_x=False, flip_y=False,
) -> bytes:
    """
    Coordinate system:
      - x_percent / y_percent = top-left corner of stamp as % of page
      - ReportLab origin is BOTTOM-LEFT, so we convert:
          reportlab_x = x_percent% of page_width
          reportlab_y = page_height - (y_percent% of page_height) - stamp_height
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    # ── Stamp position ────────────────────────────────────────────────────────
    # x_percent / y_percent = top-left of stamp, measured from top-left of page
    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    if flip_x:
        stamp_x = page_width_pt - raw_x - stamp_width_pt
    else:
        stamp_x = raw_x

    if flip_y:
        # flip_y: y=0 is bottom, keep raw_y as-is from bottom
        stamp_y = raw_y
    else:
        # Normal: y_percent measured from TOP → convert to ReportLab bottom-left
        stamp_y = page_height_pt - raw_y - stamp_height_pt

    logger.info(
        "Stamp draw: page=(%.1f x %.1f) | size=(%.2f x %.2f) | "
        "raw_x=%.2f raw_y=%.2f | rl_x=%.2f rl_y=%.2f",
        page_width_pt, page_height_pt,
        stamp_width_pt, stamp_height_pt,
        raw_x, raw_y, stamp_x, stamp_y
    )

    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_rgba   = img.convert("RGBA")
            img_reader = ImageReader(img_rgba)
            c.drawImage(
                img_reader,
                stamp_x, stamp_y,
                width=stamp_width_pt, height=stamp_height_pt,
                mask="auto"
            )
    except Exception as exc:
        logger.error("Failed to draw stamp image: %s", exc)
        raise

    # ── Date / timestamp ──────────────────────────────────────────────────────
    if date_text:
        dx_pct = date_x_percent if date_x_percent is not None else x_percent
        dy_pct = date_y_percent if date_y_percent is not None else y_percent

        raw_dx = (dx_pct / 100.0) * page_width_pt
        raw_dy = (dy_pct / 100.0) * page_height_pt

        if flip_x:
            date_x = page_width_pt - raw_dx
        else:
            date_x = raw_dx

        if flip_y:
            # Place date just above the stamp (stamp_y is already from bottom)
            date_y = raw_dy + stamp_height_pt + 2
        else:
            # Date sits just above the stamp top edge
            # stamp top in RL coords = stamp_y + stamp_height_pt
            # We want text just above that → stamp_y + stamp_height_pt + 2
            # But date_y_percent is its own cell so use it directly:
            date_y = page_height_pt - raw_dy + 2

        logger.info(
            "Date draw: text='%s' font=%.1f | raw=(%.2f,%.2f) | rl=(%.2f,%.2f)",
            date_text, date_font_size, raw_dx, raw_dy, date_x, date_y
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
    date_font_size=6, flip_x=False, flip_y=False,
    pages="all", occurrence="all",
) -> bytes:
    reader      = PdfReader(io.BytesIO(pdf_bytes))
    writer      = PdfWriter()
    total_pages = len(reader.pages)

    target_indices = parse_pages(pages, total_pages)
    target_indices = filter_occurrence(target_indices, occurrence)
    target_set     = set(target_indices)

    logger.info("Total pages: %d | Stamping pages (0-based): %s", total_pages, target_indices)

    for i, page in enumerate(reader.pages):
        if i in target_set:
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)

            logger.info("Page %d size: %.2f x %.2f pt", i, page_w, page_h)

            sw, sh = _resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent, stamp_height_percent,
                stamp_width_px, stamp_height_px,
            )

            overlay_bytes = build_stamp_overlay(
                page_width_pt=page_w,
                page_height_pt=page_h,
                stamp_img_bytes=stamp_bytes,
                x_percent=x_percent,
                y_percent=y_percent,
                stamp_width_pt=sw,
                stamp_height_pt=sh,
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


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "PDF Stamp API"})


@app.route("/ready", methods=["GET"])
def ready():
    return jsonify({"status": "ready"})


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():
    # ── Auth ──────────────────────────────────────────────
    if not check_auth(request):
        return jsonify({"error": "Unauthorized. Send header: x-api-key: " + API_KEY}), 401

    # ── Size guard ────────────────────────────────────────
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return jsonify({"error": f"Request body too large (limit {MAX_BODY_BYTES // (1024*1024)} MB)"}), 413

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Valid JSON body required"}), 400

    # ── Required fields ───────────────────────────────────
    pdf_b64   = data.get("pdf")
    stamp_b64 = data.get("stamp")
    if not pdf_b64 or not stamp_b64:
        return jsonify({"error": "'pdf' and 'stamp' (base64) are required"}), 400

    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception:
        return jsonify({"error": "Invalid base64 in 'pdf' or 'stamp'"}), 400

    # ── Position ──────────────────────────────────────────
    x_percent = _to_float(data.get("x_percent"), 50.0)
    y_percent = _to_float(data.get("y_percent"), 50.0)

    # ── Stamp size (percent of page dimensions) ───────────
    stamp_width_percent  = _to_float(data.get("stamp_width_percent"))   # % of page width
    stamp_height_percent = _to_float(data.get("stamp_height_percent"))  # % of page height
    stamp_width_px       = _to_float(data.get("stamp_width"))           # fixed px fallback
    stamp_height_px      = _to_float(data.get("stamp_height"))          # fixed px fallback

    # ── Date / timestamp ──────────────────────────────────
    date_text      = data.get("date_text")
    date_x_percent = _to_float(data.get("date_x_percent"))
    date_y_percent = _to_float(data.get("date_y_percent"))
    date_font_size = _to_float(data.get("date_font_size"), 6.0)

    # ── Options ───────────────────────────────────────────
    flip_x     = bool(data.get("flip_x", False))
    flip_y     = bool(data.get("flip_y", False))
    pages      = data.get("pages", "all")
    occurrence = data.get("occurrence", "all")

    logger.info(
        "Request → x=%.1f%% y=%.1f%% | stamp_w=%.1f%% stamp_h=%.1f%% | "
        "date='%s' dx=%.1f dy=%.1f fs=%.1f | pages=%s occ=%s",
        x_percent, y_percent,
        stamp_width_percent or 0, stamp_height_percent or 0,
        date_text or "", date_x_percent or 0, date_y_percent or 0, date_font_size,
        pages, occurrence
    )

    try:
        result_bytes = stamp_pdf(
            pdf_bytes=pdf_bytes,
            stamp_bytes=stamp_bytes,
            x_percent=x_percent,
            y_percent=y_percent,
            stamp_width_percent=stamp_width_percent,
            stamp_height_percent=stamp_height_percent,
            stamp_width_px=stamp_width_px,
            stamp_height_px=stamp_height_px,
            date_text=date_text,
            date_x_percent=date_x_percent,
            date_y_percent=date_y_percent,
            date_font_size=date_font_size,
            flip_x=flip_x,
            flip_y=flip_y,
            pages=pages,
            occurrence=occurrence,
        )
    except Exception as exc:
        logger.error("Stamping failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

    encoded = base64.b64encode(result_bytes).decode("utf-8")

    return jsonify({
        "status":      "success",
        "pdf":         encoded,   # primary key
        "stamped_pdf": encoded,   # backwards compat alias
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
