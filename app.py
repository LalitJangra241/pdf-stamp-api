from flask import Flask, request, jsonify
import base64
import io
import tempfile
import os
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image

app = Flask(__name__)

API_KEY = "PDF_Stamp"

POSITIONS = {
    "bottom-right":  lambda pw, ph, sw, sh: (pw - sw - 30, 20),
    "bottom-left":   lambda pw, ph, sw, sh: (30, 20),
    "top-right":     lambda pw, ph, sw, sh: (pw - sw - 30, ph - sh - 20),
    "top-left":      lambda pw, ph, sw, sh: (30, ph - sh - 20),
    "center":        lambda pw, ph, sw, sh: ((pw - sw) / 2, (ph - sh) / 2),
}


def create_stamp_overlay(page_width, page_height, img_bytes, position, stamp_w, stamp_h):
    """Create a single-page PDF overlay containing the signature image."""
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=(page_width, page_height))

    pos_fn = POSITIONS.get(position, POSITIONS["bottom-right"])
    x, y = pos_fn(page_width, page_height, stamp_w, stamp_h)

    # ✅ Fix: Use ImageReader instead of BytesIO directly
    img_reader = ImageReader(io.BytesIO(img_bytes))
    c.drawImage(img_reader, x, y, width=stamp_w, height=stamp_h, mask="auto")
    c.save()

    packet.seek(0)
    return packet.read()


@app.route("/stamp", methods=["POST"])
def stamp_pdf():
    if request.headers.get("x-api-key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    pdf_b64        = data.get("pdf")
    signature_b64  = data.get("signature")
    position       = data.get("position", "bottom-right")
    pages_option   = data.get("pages", "last")
    stamp_width    = int(data.get("stamp_width",  150))
    stamp_height   = int(data.get("stamp_height",  75))

    if not pdf_b64 or not signature_b64:
        return jsonify({"error": "Both 'pdf' and 'signature' (base64) are required"}), 400

    if position not in POSITIONS:
        return jsonify({"error": f"Invalid position. Choose from: {list(POSITIONS.keys())}"}), 400

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
        sig_bytes = base64.b64decode(signature_b64)

        # Validate image
        Image.open(io.BytesIO(sig_bytes)).verify()

        reader = PdfReader(io.BytesIO(pdf_bytes))
        writer = PdfWriter()
        total  = len(reader.pages)

        if pages_option == "all":
            target_pages = set(range(total))
        elif pages_option == "first":
            target_pages = {0}
        else:  # last
            target_pages = {total - 1}

        for i, page in enumerate(reader.pages):
            if i in target_pages:
                pw = float(page.mediabox.width)
                ph = float(page.mediabox.height)

                overlay_bytes = create_stamp_overlay(
                    pw, ph, sig_bytes, position, stamp_width, stamp_height
                )
                overlay_page = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
                page.merge_page(overlay_page)

            writer.add_page(page)

        output = io.BytesIO()
        writer.write(output)
        output.seek(0)

        result_b64 = base64.b64encode(output.read()).decode("utf-8")
        return jsonify({"pdf": result_b64}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
