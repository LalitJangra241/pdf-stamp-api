from flask import Flask, request, jsonify
import base64, io
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image

app = Flask(__name__)

API_KEY = "pdf-stamp-api"


def check_auth(req):
    return req.headers.get("x-api-key") == API_KEY


def create_overlay(page_w, page_h, stamp_bytes,
                   x_pct, y_pct, w_pct, h_pct,
                   date_text=None, dx_pct=None, dy_pct=None, font=6):

    stamp_w = (w_pct / 100) * page_w
    stamp_h = (h_pct / 100) * page_h

    x = (x_pct / 100) * page_w
    y = page_h - ((y_pct / 100) * page_h) - stamp_h

    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_w, page_h))

    img = Image.open(io.BytesIO(stamp_bytes)).convert("RGBA")
    img = img.resize((int(stamp_w), int(stamp_h)))

    c.drawImage(ImageReader(img), x, y, width=stamp_w, height=stamp_h, mask='auto')

    if date_text:
        dx = (dx_pct / 100) * page_w
        dy = page_h - ((dy_pct / 100) * page_h)

        c.setFont("Helvetica", font)
        c.drawString(dx, dy, date_text)

    c.save()
    return packet.getvalue()


def stamp_pdf(pdf_bytes, stamp_bytes, payload):

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    for page in reader.pages:

        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        overlay = create_overlay(
            page_w, page_h, stamp_bytes,
            payload["x_percent"],
            payload["y_percent"],
            payload["stamp_width_percent"],
            payload["stamp_height_percent"],
            payload.get("date_text"),
            payload.get("date_x_percent"),
            payload.get("date_y_percent"),
            payload.get("date_font_size", 6)
        )

        page.merge_page(PdfReader(io.BytesIO(overlay)).pages[0])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@app.route("/stamp", methods=["POST"])
def stamp():

    if not check_auth(request):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json

    try:
        pdf = base64.b64decode(data["pdf"])
        stamp = base64.b64decode(data["stamp"])
    except:
        return jsonify({"error": "Invalid base64"}), 400

    result = stamp_pdf(pdf, stamp, data)

    return jsonify({
        "pdf": base64.b64encode(result).decode()
    })


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    app.run()
