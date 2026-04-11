"""
PDF STAMP API — Flask Server
============================
Features:
- Stamp placement using X/Y percent coordinates
- Stamp SIZE as percent of page dimensions:
    stamp_width_percent  (e.g. 40 means 40% of page width)
    stamp_height_percent (e.g. 50 means 50% of page height)
- Backwards compatible: still accepts fixed px via stamp_width / stamp_height
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

API_KEY        = os.environ.get("PDF_STAMP_API_KEY", "PDF_Stamp")
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


def _resolve_stamp_size(page_w, page_h, stamp_width_percent, stamp_height_percent, stamp_width_px, stamp_height_px):
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent / 100.0) * page_w
        sh = (stamp_height_percent / 100.0) * page_h
        logger.info("Stamp size (percent): %.0f%%w x %.0f%%h → %.1f x %.1f pt", stamp_width_percent, stamp_height_percent, sw, sh)
    elif stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("Stamp size (fixed px): %.1f x %.1f pt", sw, sh)
    else:
        sw = 0.15 * page_w
        sh = 0.10 * page_h
        logger.info("Stamp size (default 15%%x10%%): %.1f x %.1f pt", sw, sh)
    return sw, sh


def build_stamp_overlay(
    page_width_pt, page_height_pt, stamp_img_bytes,
    x_percent, y_percent, stamp_width_pt, stamp_height_pt,
    date_text=None, date_x_percent=None, date_y_percent=None,
    date_font_size=6, flip_x=False, flip_y=False,
) -> bytes:
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    stamp_x = (page_width_pt - raw_x - stamp_width_pt) if flip_x else raw_x
    stamp_y = raw_y if flip_y else (page_height_pt - raw_y - stamp_height_pt)

    try:
        with Image.open(io.BytesIO(stamp_img_bytes)) as img:
            img_rgba   = img.convert("RGBA")
            img_reader = ImageReader(img_rgba)
            c.drawImage(img_reader, stamp_x, stamp_y,
                        width=stamp_width_pt, height=stamp_height_pt, mask="auto")
    except Exception as exc:
        logger.error("Failed to draw stamp image: %s", exc)
        raise

    if date_text:
        dx_pct = date_x_percent if date_x_percent is not None else x_percent
        dy_pct = date_y_percent if date_y_percent is not None else y_percent

        raw_dx = (dx_pct / 100.0) * page_width_pt
        raw_dy = (dy_pct / 100.0) * page_height_pt

        date_x = (page_width_pt - raw_dx) if flip_x else raw_dx
        date_y = (raw_dy + stamp_height_pt + 2) if flip_y else (page_height_pt - raw_dy + 2)

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

    logger.info("Total pages: %d | Stamping (0-based): %s", total_pages, target_indices)

    for i, page in enumerate(reader.pages):
        if i in target_set:
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)

            sw, sh = _resolve_stamp_size(
                page_w, page_h,
                stamp_width_percent, stamp_height_percent,
                stamp_width_px, stamp_height_px,
            )

            overlay_bytes = build_stamp_overlay(
                page_width_pt=page_w, page_height_pt=page_h,
                stamp_img_bytes=stamp_bytes,
                x_percent=x_percent, y_percent=y_percent,
                stamp_width_pt=sw, stamp_height_pt=sh,
                date_text=date_text,
                date_x_percent=date_x_percent,
                date_y_percent=date_y_percent,
                date_font_size=date_font_size,
                flip_x=flip_x, flip_y=flip_y,
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
        return jsonify({"error": "Unauthorized. Send header: x-api-key: PDF_Stamp"}), 401

    # ── Size guard ────────────────────────────────────────
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return jsonify({"error": f"Request body too large (limit {MAX_BODY_BYTES // (1024*1024)} MB)"}), 413

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "Valid JSON body required"}), 400

    # ── Required ──────────────────────────────────────────
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

    # ── Stamp size ────────────────────────────────────────
    stamp_width_percent  = _to_float(data.get("stamp_width_percent"))
    stamp_height_percent = _to_float(data.get("stamp_height_percent"))
    stamp_width_px       = _to_float(data.get("stamp_width"))
    stamp_height_px      = _to_float(data.get("stamp_height"))

    # ── Date ──────────────────────────────────────────────
    date_text      = data.get("date_text")
    date_x_percent = _to_float(data.get("date_x_percent"))
    date_y_percent = _to_float(data.get("date_y_percent"))
    date_font_size = _to_float(data.get("date_font_size"), 6.0)

    # ── Options ───────────────────────────────────────────
    flip_x     = bool(data.get("flip_x", False))
    flip_y     = bool(data.get("flip_y", False))
    pages      = data.get("pages", "all")
    occurrence = data.get("occurrence", "all")

    try:
        result_bytes = stamp_pdf(
            pdf_bytes=pdf_bytes, stamp_bytes=stamp_bytes,
            x_percent=x_percent, y_percent=y_percent,
            stamp_width_percent=stamp_width_percent,
            stamp_height_percent=stamp_height_percent,
            stamp_width_px=stamp_width_px,
            stamp_height_px=stamp_height_px,
            date_text=date_text,
            date_x_percent=date_x_percent,
            date_y_percent=date_y_percent,
            date_font_size=date_font_size,
            flip_x=flip_x, flip_y=flip_y,
            pages=pages, occurrence=occurrence,
        )
    except Exception as exc:
        logger.error("Stamping failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

    return jsonify({
        "status":      "success",
        "stamped_pdf": base64.b64encode(result_bytes).decode("utf-8"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
