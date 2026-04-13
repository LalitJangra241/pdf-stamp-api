"""
PDF STAMP API — Flask Server
============================
- Stamp SIZE as percent of page dimensions:
    stamp_width_percent  = % of page WIDTH
    stamp_height_percent = % of page HEIGHT
- Stamp POSITION as percent of page dimensions:
    x_percent = % of page WIDTH  (left edge of stamp)
    y_percent = % of page HEIGHT (top edge of stamp, converts to ReportLab bottom-left)
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

    Example: page = 841 x 595 pt (A4 landscape)
             width%=40, height%=10
             → stamp = 336.4 x 59.5 pt
    """
    if stamp_width_percent is not None and stamp_height_percent is not None:
        sw = (stamp_width_percent  / 100.0) * page_w
        sh = (stamp_height_percent / 100.0) * page_h
        logger.info(
            "Stamp size (percent): "
            "width=%.2f%% of page_w(%.2fpt) → %.2fpt | "
            "height=%.2f%% of page_h(%.2fpt) → %.2fpt",
            stamp_width_percent, page_w, sw,
            stamp_height_percent, page_h, sh,
        )
    elif stamp_width_px is not None and stamp_height_px is not None:
        sw, sh = float(stamp_width_px), float(stamp_height_px)
        logger.info("Stamp size (fixed px): %.2f x %.2f pt", sw, sh)
    else:
        # Default: 15% wide, 10% tall
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
      x_percent / y_percent = TOP-LEFT corner of stamp as % of page.
      ReportLab origin = BOTTOM-LEFT.
      Conversion: rl_x = raw_x
                  rl_y = page_height - raw_y - stamp_height
    """
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width_pt, page_height_pt))

    # ── Stamp position ────────────────────────────────────────
    raw_x = (x_percent / 100.0) * page_width_pt
    raw_y = (y_percent / 100.0) * page_height_pt

    if flip_x:
        stamp_x = page_width_pt - raw_x - stamp_width_pt
    else:
        stamp_x = raw_x

    if flip_y:
        stamp_y = raw_y
    else:
        # Convert from "top-left origin" to ReportLab "bottom-left origin"
        stamp_y = page_height_pt - raw_y - stamp_height_pt

    logger.info(
        "Stamp draw → page=(%.2f x %.2f)pt | size=(%.2f x %.2f)pt | "
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
        logger.error("Failed to draw stamp image: %s", exc)
        raise

    # ── Date / timestamp ──────────────────────────────────────
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
            # Date sits just ABOVE the stamp top edge in the PDF
            # stamp top (RL) = stamp_y + stamp_height_pt
            # date is placed at its own y_percent coordinate
            date_y = page_height_pt - raw_dy + 2

        logger.info(
            "Date draw → text='%s' font=%.1fpt | "
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
    d
