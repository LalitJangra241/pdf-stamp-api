"""
PDF Stamp API
=============
POST /stamp   — stamps a PDF with an image + timestamp text
GET  /health  — health check, returns status + features

Required packages:
  pip install fastapi uvicorn pypdf pillow reportlab python-multipart

Run:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import io
import math
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# PDF read / write
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, FloatObject, NameObject, RectangleObject
)

# Image + drawing
from PIL import Image
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader

# ──────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────

app = FastAPI(title="PDF Stamp API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API_KEY = "pdf-stamp-api"

# ──────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────

class StampRequest(BaseModel):
    pdf:                  str   = Field(..., description="Base64-encoded input PDF")
    stamp:                str   = Field(..., description="Base64-encoded stamp image (PNG/JPG)")
    x_percent:            float = Field(..., ge=0, le=100, description="Stamp left edge as % of page width")
    y_percent:            float = Field(..., ge=0, le=100, description="Stamp top edge as % of page height")
    stamp_width_percent:  float = Field(..., gt=0, le=100, description="Stamp width as % of page width")
    stamp_height_percent: float = Field(..., gt=0, le=100, description="Stamp height as % of page height")
    date_text:            str   = Field(..., description="Timestamp text to render (e.g. 14/04/2026)")
    date_x_percent:       float = Field(..., ge=0, le=100, description="Timestamp left edge as % of page width")
    date_y_percent:       float = Field(..., ge=0, le=100, description="Timestamp top edge as % of page height")
    date_font_size:       float = Field(..., gt=0, description="Font size in pt (calibrated for A4; auto-scaled by API)")


class StampResponse(BaseModel):
    pdf:          str            # Base64-encoded stamped PDF
    page_w_pt:    float          # Page width  in points
    page_h_pt:    float          # Page height in points
    page_w_mm:    float          # Page width  in mm
    page_h_mm:    float          # Page height in mm
    page_w_in:    float          # Page width  in inches
    page_h_in:    float          # Page height in inches
    page_label:   str            # e.g. "A4 Portrait", "Letter Landscape"
    total_pages:  int            # Number of pages in the PDF


class HealthResponse(BaseModel):
    status:   str
    version:  str
    features: list[str]


# ──────────────────────────────────────────────────────────
# Helper: guess page name from dimensions
# ──────────────────────────────────────────────────────────

_PAGE_SIZES = [
    ("A3",      841.89,  1190.55),
    ("A4",      595.28,   841.89),
    ("A5",      419.53,   595.28),
    ("Letter",  612.00,   792.00),
    ("Legal",   612.00,  1008.00),
    ("Tabloid", 792.00,  1224.00),
]

def guess_page_size(w_pt: float, h_pt: float) -> str:
    s, l   = min(w_pt, h_pt), max(w_pt, h_pt)
    orient = "Portrait" if w_pt <= h_pt else "Landscape"
    for name, sw, sl in _PAGE_SIZES:
        if abs(s - sw) < 6 and abs(l - sl) < 6:
            return f"{name} {orient}"
    return f"Custom {orient} ({w_pt:.1f} × {h_pt:.1f} pt)"


# ──────────────────────────────────────────────────────────
# Helper: render overlay (stamp image + date text) as PDF
# ──────────────────────────────────────────────────────────

def _build_overlay(
    page_w_pt:  float,
    page_h_pt:  float,
    stamp_b64:  str,
    x_pct:      float,
    y_pct:      float,
    sw_pct:     float,
    sh_pct:     float,
    date_text:  str,
    dx_pct:     float,
    dy_pct:     float,
    font_size:  float,
) -> bytes:
    """
    Build a single-page transparent PDF overlay containing:
      • the stamp image
      • the date text

    All coordinates use PDF convention (origin = bottom-left).
    The y_percent / date_y_percent values come from the sheet as
    "% from top", so we convert: pdf_y = page_h - top_pt - element_h
    """

    # Convert percentages → points
    sw_pt  = (sw_pct  / 100.0) * page_w_pt
    sh_pt  = (sh_pct  / 100.0) * page_h_pt
    x_pt   = (x_pct   / 100.0) * page_w_pt
    # y_pct is "from top" → convert to PDF bottom-left origin
    y_pt   = page_h_pt - (y_pct / 100.0) * page_h_pt - sh_pt

    dx_pt  = (dx_pct  / 100.0) * page_w_pt
    # Scale font from A4 reference
    A4_H   = 841.89
    scaled_font = font_size * (page_h_pt / A4_H)
    # Date text y: from top → PDF origin; subtract one line height (≈ font size)
    dy_pt  = page_h_pt - (dy_pct / 100.0) * page_h_pt - scaled_font

    # Decode stamp image
    stamp_bytes = base64.b64decode(stamp_b64)
    stamp_img   = Image.open(io.BytesIO(stamp_bytes)).convert("RGBA")

    # Build overlay PDF in memory with ReportLab
    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(page_w_pt, page_h_pt))

    # Draw stamp image
    img_reader = ImageReader(stamp_img)
    c.drawImage(
        img_reader,
        x_pt, y_pt,
        width=sw_pt, height=sh_pt,
        mask="auto",          # preserve transparency
    )

    # Draw date text
    c.setFont("Helvetica-Bold", scaled_font)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(dx_pt, dy_pt, date_text)

    c.save()
    buf.seek(0)
    return buf.read()


# ──────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version="2.1.0",
        features=[
            "stamp",
            "page_size_detection",
            "multi_page_pdf",
            "font_auto_scale",
            "cross_check_mediabox",
        ],
    )


@app.post("/stamp", response_model=StampResponse)
def stamp_pdf(
    body:      StampRequest,
    x_api_key: Optional[str] = Header(None),
) -> StampResponse:

    # ── Auth ────────────────────────────────────────────────
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    # ── Decode PDF ─────────────────────────────────────────
    try:
        pdf_bytes = base64.b64decode(body.pdf)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 PDF: {exc}")

    # ── Read PDF + extract page size ────────────────────────
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot parse PDF: {exc}")

    if len(reader.pages) == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages")

    page_0   = reader.pages[0]
    page_w_pt = float(page_0.mediabox.width)
    page_h_pt = float(page_0.mediabox.height)
    total_pages = len(reader.pages)

    # Derived size fields
    page_w_mm = round(page_w_pt / 2.83465, 3)
    page_h_mm = round(page_h_pt / 2.83465, 3)
    page_w_in = round(page_w_pt / 72.0, 4)
    page_h_in = round(page_h_pt / 72.0, 4)
    page_label = guess_page_size(page_w_pt, page_h_pt)

    # ── Build overlay ───────────────────────────────────────
    try:
        overlay_bytes = _build_overlay(
            page_w_pt  = page_w_pt,
            page_h_pt  = page_h_pt,
            stamp_b64  = body.stamp,
            x_pct      = body.x_percent,
            y_pct      = body.y_percent,
            sw_pct     = body.stamp_width_percent,
            sh_pct     = body.stamp_height_percent,
            date_text  = body.date_text,
            dx_pct     = body.date_x_percent,
            dy_pct     = body.date_y_percent,
            font_size  = body.date_font_size,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Overlay build failed: {exc}")

    # ── Merge overlay onto every page ───────────────────────
    try:
        overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
        overlay_page   = overlay_reader.pages[0]

        writer = PdfWriter()
        for page in reader.pages:
            page.merge_page(overlay_page)
            writer.add_page(page)

        out_buf = io.BytesIO()
        writer.write(out_buf)
        out_buf.seek(0)
        stamped_bytes = out_buf.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF merge failed: {exc}")

    # ── Return ──────────────────────────────────────────────
    return StampResponse(
        pdf         = base64.b64encode(stamped_bytes).decode(),
        page_w_pt   = round(page_w_pt, 4),
        page_h_pt   = round(page_h_pt, 4),
        page_w_mm   = page_w_mm,
        page_h_mm   = page_h_mm,
        page_w_in   = page_w_in,
        page_h_in   = page_h_in,
        page_label  = page_label,
        total_pages = total_pages,
    )
