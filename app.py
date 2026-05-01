"""
PDF Stamp API
=============
POST /stamp     — stamps a PDF with an image + timestamp text
                — supports BATCH mode: { pdf: "...", stamps: [...] }
POST /resize    — resizes a stamp image to exact pt dimensions
POST /compress  — compresses a PDF using Ghostscript (★ NEW v2.8.0)
GET  /health    — health check, returns status + features

Required packages:
  pip install fastapi uvicorn pypdf pillow reportlab python-multipart

System requirement (for /compress):
  ghostscript must be installed on the server
  Render: add  apt-get install -y ghostscript  in your build command or Dockerfile

Run:
  uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import io
import os
import subprocess
import tempfile
import math
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# PDF read / write
from pypdf import PdfReader, PdfWriter

# Image + drawing
from PIL import Image
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.utils import ImageReader

# ──────────────────────────────────────────────────────────
# App setup
# ──────────────────────────────────────────────────────────

app = FastAPI(title="PDF Stamp API", version="2.8.0")

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

class StampDescriptor(BaseModel):
    """A single stamp to apply — used in both single and batch modes."""
    stamp:                str   = Field(..., description="Base64-encoded stamp image (PNG/JPG) — pre-resized")
    x_percent:            float = Field(..., ge=0, le=100)
    y_percent:            float = Field(..., ge=0, le=100)
    stamp_width_percent:  float = Field(..., gt=0, le=100)
    stamp_height_percent: float = Field(..., gt=0, le=100)
    date_text:            str   = Field(default="")
    date_x_percent:       float = Field(default=0, ge=0, le=100)
    date_y_percent:       float = Field(default=0, ge=0, le=100)
    date_font_size:       float = Field(default=1, gt=0)


class StampRequest(BaseModel):
    pdf:    str = Field(..., description="Base64-encoded input PDF")

    # ── Single-stamp fields (legacy) ─────────────────────────
    stamp:                Optional[str]   = Field(default=None)
    x_percent:            Optional[float] = Field(default=None, ge=0, le=100)
    y_percent:            Optional[float] = Field(default=None, ge=0, le=100)
    stamp_width_percent:  Optional[float] = Field(default=None, gt=0, le=100)
    stamp_height_percent: Optional[float] = Field(default=None, gt=0, le=100)
    date_text:            Optional[str]   = Field(default=None)
    date_x_percent:       Optional[float] = Field(default=None, ge=0, le=100)
    date_y_percent:       Optional[float] = Field(default=None, ge=0, le=100)
    date_font_size:       Optional[float] = Field(default=None, gt=0)

    # ── Batch-stamp field ────────────────────────────────────
    stamps: Optional[List[StampDescriptor]] = Field(default=None)


class StampResponse(BaseModel):
    pdf:          str
    page_w_pt:    float
    page_h_pt:    float
    page_w_mm:    float
    page_h_mm:    float
    page_w_in:    float
    page_h_in:    float
    page_label:   str
    total_pages:  int


class ResizeRequest(BaseModel):
    stamp:      str   = Field(..., description="Base64-encoded stamp image")
    width_pt:   float = Field(..., gt=0)
    height_pt:  float = Field(..., gt=0)
    dpi:        float = Field(default=150.0, gt=0)


class ResizeResponse(BaseModel):
    stamp:       str
    width_px:    int
    height_px:   int
    width_pt:    float
    height_pt:   float
    dpi:         float


# ★ NEW v2.8.0 ─────────────────────────────────────────────
class CompressRequest(BaseModel):
    pdf:     str   = Field(..., description="Base64-encoded PDF to compress")
    quality: str   = Field(
        default="ebook",
        description=(
            "Ghostscript PDFSETTINGS preset: "
            "'screen'  = 72 dpi  (smallest file, screen only), "
            "'ebook'   = 150 dpi (good balance — RECOMMENDED), "
            "'printer' = 300 dpi (near-lossless, still smaller than input)"
        )
    )


class CompressResponse(BaseModel):
    pdf:              str    # Base64-encoded compressed PDF
    original_size_kb: float  # Input size in KB
    compressed_size_kb: float  # Output size in KB
    reduction_pct:    float  # Size reduction as a percentage
# ──────────────────────────────────────────────────────────


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
# Core: build a single overlay PDF (stamp image + date text)
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
    sw_pt  = (sw_pct  / 100.0) * page_w_pt
    sh_pt  = (sh_pct  / 100.0) * page_h_pt
    x_pt   = (x_pct   / 100.0) * page_w_pt
    y_pt   = page_h_pt - (y_pct / 100.0) * page_h_pt - sh_pt

    dx_pt  = (dx_pct  / 100.0) * page_w_pt
    A4_H   = 841.89
    scaled_font = font_size * (page_h_pt / A4_H)
    dy_pt  = page_h_pt - (dy_pct / 100.0) * page_h_pt - scaled_font

    stamp_bytes = base64.b64decode(stamp_b64)
    stamp_img   = Image.open(io.BytesIO(stamp_bytes)).convert("RGBA")

    buf = io.BytesIO()
    c   = rl_canvas.Canvas(buf, pagesize=(page_w_pt, page_h_pt))

    img_reader = ImageReader(stamp_img)
    c.drawImage(
        img_reader,
        x_pt, y_pt,
        width=sw_pt, height=sh_pt,
        mask="auto",
    )

    if date_text and date_text.strip():
        c.setFont("Helvetica-Bold", scaled_font)
        c.setFillColorRGB(0, 0, 0)
        c.drawString(dx_pt, dy_pt, date_text)

    c.save()
    buf.seek(0)
    return buf.read()


# ──────────────────────────────────────────────────────────
# Core: apply ONE stamp descriptor to raw PDF bytes
# ──────────────────────────────────────────────────────────

def _apply_stamp_to_bytes(
    pdf_bytes:  bytes,
    descriptor: StampDescriptor,
) -> bytes:
    reader    = PdfReader(io.BytesIO(pdf_bytes))
    page_0    = reader.pages[0]
    page_w_pt = float(page_0.mediabox.width)
    page_h_pt = float(page_0.mediabox.height)

    overlay_bytes = _build_overlay(
        page_w_pt  = page_w_pt,
        page_h_pt  = page_h_pt,
        stamp_b64  = descriptor.stamp,
        x_pct      = descriptor.x_percent,
        y_pct      = descriptor.y_percent,
        sw_pct     = descriptor.stamp_width_percent,
        sh_pct     = descriptor.stamp_height_percent,
        date_text  = descriptor.date_text or "",
        dx_pct     = descriptor.date_x_percent,
        dy_pct     = descriptor.date_y_percent,
        font_size  = descriptor.date_font_size,
    )

    overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
    overlay_page   = overlay_reader.pages[0]

    writer = PdfWriter()
    for page in reader.pages:
        page.merge_page(overlay_page)
        writer.add_page(page)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return out_buf.read()


# ──────────────────────────────────────────────────────────
# ★ NEW v2.8.0 — Ghostscript compress helper
# ──────────────────────────────────────────────────────────

VALID_QUALITY_PRESETS = {"screen", "ebook", "printer", "prepress"}

def _compress_pdf_ghostscript(pdf_bytes: bytes, quality: str = "ebook") -> bytes:
    """
    Compress PDF bytes using Ghostscript.

    quality presets map to -dPDFSETTINGS:
      screen  →  72 dpi  — smallest file size, screen-only
      ebook   → 150 dpi  — RECOMMENDED for approval workflows
      printer → 300 dpi  — near-lossless, still smaller than raw

    Returns compressed PDF bytes.
    Raises RuntimeError if Ghostscript is not installed or fails.
    """
    if quality not in VALID_QUALITY_PRESETS:
        quality = "ebook"

    # Write input to a temp file
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_in:
        tmp_in.write(pdf_bytes)
        input_path = tmp_in.name

    output_path = input_path.replace(".pdf", "_compressed.pdf")

    try:
        result = subprocess.run(
            [
                "gs",
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS=/{quality}",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={output_path}",
                input_path,
            ],
            capture_output=True,
            timeout=120,  # 2-minute max
        )

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace")
            raise RuntimeError(f"Ghostscript failed (code {result.returncode}): {stderr[:400]}")

        with open(output_path, "rb") as f:
            compressed_bytes = f.read()

        # Safety: if Ghostscript somehow made it bigger, return original
        if len(compressed_bytes) >= len(pdf_bytes):
            return pdf_bytes

        return compressed_bytes

    finally:
        # Always clean up temp files
        for path in (input_path, output_path):
            try:
                os.unlink(path)
            except OSError:
                pass


# ──────────────────────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    # Check if Ghostscript is available
    gs_available = subprocess.run(
        ["gs", "--version"], capture_output=True
    ).returncode == 0

    features = [
        "stamp",
        "batch_stamp",
        "resize",
        "page_size_detection",
        "multi_page_pdf",
        "font_auto_scale",
        "cross_check_mediabox",
    ]
    if gs_available:
        features.append("compress_ghostscript")

    return HealthResponse(
        status="ok",
        version="2.8.0",
        features=features,
    )


# ★ NEW v2.8.0 ─────────────────────────────────────────────
@app.post("/compress", response_model=CompressResponse)
def compress_pdf(
    body:      CompressRequest,
    x_api_key: Optional[str] = Header(None),
) -> CompressResponse:
    """
    Compress a PDF using Ghostscript before stamping.

    Typical results:
      33 MB PDF  →  ebook quality  →  ~4–6 MB  (85% reduction)

    Requires Ghostscript installed on the server.
    On Render: add  apt-get install -y ghostscript  to build command.
    """

    # ── Auth ─────────────────────────────────────────────────
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    # ── Decode PDF ───────────────────────────────────────────
    try:
        pdf_bytes = base64.b64decode(body.pdf)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 PDF: {exc}")

    original_size_kb = len(pdf_bytes) / 1024.0

    # ── Validate it's a PDF ──────────────────────────────────
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Provided bytes do not appear to be a valid PDF.")

    # ── Compress ─────────────────────────────────────────────
    try:
        compressed_bytes = _compress_pdf_ghostscript(pdf_bytes, body.quality)
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=(
                "Ghostscript (gs) is not installed on this server. "
                "Add 'apt-get install -y ghostscript' to your Render build command."
            )
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Compress failed: {exc}")

    compressed_size_kb = len(compressed_bytes) / 1024.0
    reduction_pct = round(
        (1.0 - compressed_size_kb / original_size_kb) * 100.0, 1
    ) if original_size_kb > 0 else 0.0

    return CompressResponse(
        pdf                = base64.b64encode(compressed_bytes).decode(),
        original_size_kb   = round(original_size_kb, 2),
        compressed_size_kb = round(compressed_size_kb, 2),
        reduction_pct      = reduction_pct,
    )
# ──────────────────────────────────────────────────────────


@app.post("/resize", response_model=ResizeResponse)
def resize_stamp(
    body:      ResizeRequest,
    x_api_key: Optional[str] = Header(None),
) -> ResizeResponse:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    try:
        img_bytes = base64.b64decode(body.stamp)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 stamp: {exc}")

    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot open stamp image: {exc}")

    target_w_px = max(1, round(body.width_pt  / 72.0 * body.dpi))
    target_h_px = max(1, round(body.height_pt / 72.0 * body.dpi))

    try:
        resized = img.resize((target_w_px, target_h_px), Image.LANCZOS)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Resize failed: {exc}")

    out_buf = io.BytesIO()
    resized.save(out_buf, format="PNG", optimize=False)
    out_buf.seek(0)
    resized_b64 = base64.b64encode(out_buf.read()).decode()

    return ResizeResponse(
        stamp      = resized_b64,
        width_px   = target_w_px,
        height_px  = target_h_px,
        width_pt   = body.width_pt,
        height_pt  = body.height_pt,
        dpi        = body.dpi,
    )


@app.post("/stamp", response_model=StampResponse)
def stamp_pdf(
    body:      StampRequest,
    x_api_key: Optional[str] = Header(None),
) -> StampResponse:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    try:
        pdf_bytes = base64.b64decode(body.pdf)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 PDF: {exc}")

    try:
        _reader_check = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cannot parse PDF: {exc}")

    if len(_reader_check.pages) == 0:
        raise HTTPException(status_code=400, detail="PDF has no pages")

    page_0      = _reader_check.pages[0]
    page_w_pt   = float(page_0.mediabox.width)
    page_h_pt   = float(page_0.mediabox.height)
    total_pages = len(_reader_check.pages)

    page_w_mm  = round(page_w_pt / 2.83465, 3)
    page_h_mm  = round(page_h_pt / 2.83465, 3)
    page_w_in  = round(page_w_pt / 72.0, 4)
    page_h_in  = round(page_h_pt / 72.0, 4)
    page_label = guess_page_size(page_w_pt, page_h_pt)

    # ── BATCH MODE ───────────────────────────────────────────
    if body.stamps is not None:
        if len(body.stamps) == 0:
            raise HTTPException(status_code=400, detail="stamps array is empty.")

        current_pdf_bytes = pdf_bytes
        try:
            for idx, descriptor in enumerate(body.stamps):
                current_pdf_bytes = _apply_stamp_to_bytes(current_pdf_bytes, descriptor)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Batch stamp failed at stamp index {idx}: {exc}"
            )

        return StampResponse(
            pdf         = base64.b64encode(current_pdf_bytes).decode(),
            page_w_pt   = round(page_w_pt, 4),
            page_h_pt   = round(page_h_pt, 4),
            page_w_mm   = page_w_mm,
            page_h_mm   = page_h_mm,
            page_w_in   = page_w_in,
            page_h_in   = page_h_in,
            page_label  = page_label,
            total_pages = total_pages,
        )

    # ── SINGLE-STAMP MODE ────────────────────────────────────
    required = ["stamp", "x_percent", "y_percent",
                "stamp_width_percent", "stamp_height_percent",
                "date_text", "date_x_percent", "date_y_percent", "date_font_size"]
    missing = [f for f in required if getattr(body, f) is None]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Single-stamp mode requires fields: {missing}. "
                   "Or use batch mode by passing a `stamps` array."
        )

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

    try:
        overlay_reader = PdfReader(io.BytesIO(overlay_bytes))
        overlay_page   = overlay_reader.pages[0]

        writer = PdfWriter()
        for page in _reader_check.pages:
            page.merge_page(overlay_page)
            writer.add_page(page)

        out_buf = io.BytesIO()
        writer.write(out_buf)
        out_buf.seek(0)
        stamped_bytes = out_buf.read()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"PDF merge failed: {exc}")

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
