"""
PDF STAMP API — Flask Server
============================
Features:
- Stamp placement using X/Y percent coordinates
- Stamp SIZE as percent of page dimensions:
    M6 = stamp_width_percent  (e.g. 40 means 40% of page width)
    N6 = stamp_height_percent (e.g. 50 means 50% of page height)
- Backwards compatible: still accepts fixed px via stamp_width / stamp_height
- API key authentication via x-api-key header
- Page selection: "all", "1", "1-3", "1,3,5"
- Occurrence selection: "all", "first", "last"
- Optional date text above the stamp
- flip_x / flip_y axis correction support
"""

import os
import io
import base64
import logging
from flask import Flask, request, jsonify
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Set PDF_STAMP_API_KEY env var on Render, or leave default "PDF_Stamp"
API_KEY = os.environ.get("PDF_STAMP_API_KEY", "PDF_Stamp")


# ─── Auth ─────────────────────────────────────────────────────────────────────

def check_auth(req):
    return req.headers.get("x-api-key") == API_KEY


# ─── Page parsing ──────────────────────────────────────────────────────────────

def parse_pages(page_str, total_pages):
    """
    Parse page string into 0-based list of page indices.
    Accepts: "all", "1", "1-3", "1,3,5"
    """
    if not page_str or str(page_str).strip().lower() == "all":
        return list(range(total_pages))

    page_str = str(page_str).strip()
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

    return sorted(indices)


def filter_occurrence(page_indices, occurrence):
    """Filter page list by occurrence: all / first / last"""
    if not page_indices:
        return page_indices
    occ = str(occurrence).strip().lower()
    if occ == "first":
        return [page_indices[0]]
    if occ == "last":
        return [page_indices[-1]]
    return page_indices  # "all"


# ─── Stamp overlay builder ─────────────────────────────────────────────────────

def build_stamp_overlay(
    page_width_pt,
    page_height_pt,
    stamp_img_bytes,
    x_percent,
    y_percent,
    stamp_width_pt,
    stamp_height_pt,
    date_text=None,
    date_x_percent=None,
    date_y_percent=None,
    date_font_size=6,
    flip_x=False,
    flip_y=False,
):
    """
    Build a transparent PDF overlay with the stamp (and optional date text).
    ReportLab origin = bottom-left, Y increases upward.
    Default (flip_y=False): 0% Y = top of page (screen convention).
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    # X position
    if flip_x:
        stamp_x = page_width_pt - raw_x - stamp_width_pt
    else:
        stamp_x = raw_x

    # Y position — default: 0% = top, flip for ReportLab's bottom-left origin
    if flip_y:
        stamp_y = raw_y
    else:
        stamp_y = page_height_pt - raw_y - stamp_height_pt

    # Draw stamp image
    try:
        img = Image.open(io.BytesIO(stamp_img_bytes)).convert("RGBA")
        img_reader = ImageReader(img)
        c.drawImage(
            img_reader,
            stamp_x,
            stamp_y,
            width=stamp_width_pt,
            height=stamp_height_pt,
            mask="auto",
        )
    except Exception as e:
        logger.error(f"Failed to draw stamp image: {e}")
        raise

    # Draw optional date text just above the stamp
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
            date_y = raw_dy + stamp_height_pt + 2
        else:
            date_y = page_height_pt - raw_dy + 2

        c.setFont("Helvetica", date_font_size)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(date_x, date_y, date_text)

    c.save()
    packet.seek(0)
    return packet.read()


# ─── Main stamping logic ───────────────────────────────────────────────────────

def stamp_pdf(
    pdf_bytes,
    stamp_bytes,
    x_percent,
    y_percent,
    stamp_width_percent=None,   # M6 value (e.g. 40 = 40% of page width)
    stamp_height_percent=None,  # N6 value (e.g. 50 = 50% of page height)
    stamp_width_px=None,        # Legacy: fixed points/px
    stamp_height_px=None,       # Legacy: fixed points/px
    date_text=None,
    date_x_percent=None,
    date_y_percent=None,
    date_font_size=6,
    flip_x=False,
    flip_y=False,
    pages="all",
    occurrence="all",
):
    """
    Stamp a PDF and return the stamped PDF bytes.

    Stamp sizing priority:
      1. stamp_width_percent / stamp_height_percent  ← from M6 / N6 (recommended)
      2. stamp_width_px / stamp_height_px            ← legacy fixed px fallback
      3. Default: 15% width × 10% height
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    total_pages = len(reader.pages)

    target_indices = parse_pages(pages, total_pages)
    target_indices = filter_occurrence(target_indices, occurrence)
    target_set = set(target_indices)

    logger.info(f"Total pages: {total_pages} | Stamping pages (0-based): {target_indices}")

    for i, page in enumerate(reader.pages):
        if i in target_set:
            # Get page dimensions in PDF points (1pt = 1/72 inch)
            page_w = float(page.mediabox.width)
            page_h = float(page.mediabox.height)

            # ── Resolve stamp size ──────────────────────────────────────────
            if stamp_width_percent is not None and stamp_height_percent is not None:
                # M6 / N6 percent-based (auto-scales to page)
                sw = (stamp_width_percent / 100.0) * page_w
                sh = (stamp_height_percent / 100.0) * page_h
                logger.info(
                    f"Page {i+1} ({page_w:.0f}x{page_h:.0f}pt): "
                    f"Stamp {stamp_width_percent}%w x {stamp_height_percent}%h "
                    f"→ {sw:.1f} x {sh:.1f} pt"
                )
            elif stamp_width_px is not None and stamp_height_px is not None:
                # Legacy fixed pixel/point size
                sw = float(stamp_width_px)
                sh = float(stamp_height_px)
                logger.info(
                    f"Page {i+1}: Stamp fixed {sw}x{sh} pt"
                )
            else:
                # Default fallback: 15% width, 10% height
                sw = 0.15 * page_w
                sh = 0.10 * page_h
                logger.info(
                    f"Page {i+1}: Stamp default 15%x10% → {sw:.1f}x{sh:.1f} pt"
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

            overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
            overlay_page = overlay_reader.pages[0]
            page.merge_page(overlay_page)

        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read()


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "PDF Stamp API"})


@app.route("/stamp", methods=["POST"])
def stamp_endpoint():
    if not check_auth(request):
        return jsonify({"error": "Unauthorized. Send header: x-api-key: PDF_Stamp"}), 401

    data = request.get_json(force=True)
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    # ── Required ────────────────────────────────────────────────────────────
    pdf_b64   = data.get("pdf")
    stamp_b64 = data.get("stamp")
    if not pdf_b64 or not stamp_b64:
        return jsonify({"error": "'pdf' and 'stamp' (base64) are required"}), 400

    try:
        pdf_bytes   = base64.b64decode(pdf_b64)
        stamp_bytes = base64.b64decode(stamp_b64)
    except Exception:
        return jsonify({"error": "Invalid base64 in 'pdf' or 'stamp'"}), 400

    # ── Position ────────────────────────────────────────────────────────────
    x_percent = float(data.get("x_percent", 50))
    y_percent = float(data.get("y_percent", 50))

    # ── Stamp size — percent (M6/N6) preferred; px as legacy fallback ───────
    stamp_width_percent  = data.get("stamp_width_percent")   # from M6
    stamp_height_percent = data.get("stamp_height_percent")  # from N6
    stamp_width_px       = data.get("stamp_width")           # legacy px
    stamp_height_px      = data.get("stamp_height")          # legacy px

    if stamp_width_percent  is not None: stamp_width_percent  = float(stamp_width_percent)
    if stamp_height_percent is not None: stamp_height_percent = float(stamp_height_percent)
    if stamp_width_px       is not None: stamp_width_px       = float(stamp_width_px)
    if stamp_height_px      is not None: stamp_height_px      = float(stamp_height_px)

    # ── Date text ───────────────────────────────────────────────────────────
    date_text      = data.get("date_text")
    date_x_percent = data.get("date_x_percent")
    date_y_percent = data.get("date_y_percent")
    date_font_size = float(data.get("date_font_size", 6))

    if date_x_percent is not None: date_x_percent = float(date_x_percent)
    if date_y_percent is not None: date_y_percent = float(date_y_percent)

    # ── Axis + page options ─────────────────────────────────────────────────
    flip_x     = bool(data.get("flip_x", False))
    flip_y     = bool(data.get("flip_y", False))
    pages      = data.get("pages", "all")
    occurrence = data.get("occurrence", "all")

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
    except Exception as e:
        logger.error(f"Stamping failed: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "status":      "success",
        "stamped_pdf": base64.b64encode(result_bytes).decode("utf-8"),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
