####################################################
# PDF STAMP API — FINAL (CASE-INSENSITIVE + STABLE)
####################################################

import base64
import io
from flask import Flask, request, jsonify
import fitz  # PyMuPDF
from PIL import Image

app = Flask(__name__)
API_KEY = "PDF_Stamp"


# ── AUTH ──────────────────────────────────────────
@app.before_request
def check_auth():
    if request.path == "/":
        return
    if request.headers.get("x-api-key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401


# ── HEALTH CHECK ──────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "PDF Stamp API Running ✅"})


# ── STAMP API ─────────────────────────────────────
@app.route("/stamp", methods=["POST"])
def stamp():
    try:
        data = request.get_json()

        if not data or "pdf" not in data or "signature" not in data:
            return jsonify({"error": "pdf and signature are required"}), 400

        pdf_bytes = base64.b64decode(data["pdf"])
        sig_bytes = base64.b64decode(data["signature"])

        search_word  = data.get("search_word", "")
        occurrence   = data.get("occurrence", "last")
        position     = data.get("position", "bottom-right")
        stamp_width  = float(data.get("stamp_width", 150))
        stamp_height = float(data.get("stamp_height", 75))
        padding      = float(data.get("padding", 6))

        # ── Prepare signature ──────────────────────
        sig_image = Image.open(io.BytesIO(sig_bytes)).convert("RGBA")
        sig_io = io.BytesIO()
        sig_image.save(sig_io, format="PNG")
        sig_png = sig_io.getvalue()

        # ── Open PDF ───────────────────────────────
        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        stamp_targets = []
        word_found = False

        # ── SEARCH WORD (CASE-INSENSITIVE) ─────────
        if search_word:
            all_matches = []

            for page_num in range(len(pdf_doc)):
                page = pdf_doc[page_num]

                # Try multiple variations (robust search)
                instances = []
                instances += page.search_for(search_word)
                instances += page.search_for(search_word.upper())
                instances += page.search_for(search_word.lower())

                for rect in instances:
                    all_matches.append({
                        "page": page_num,
                        "x": rect.x0,
                        "y": rect.y0,
                        "width": rect.width,
                        "height": rect.height
                    })

            if all_matches:
                word_found = True

                if occurrence == "first":
                    stamp_targets = [all_matches[0]]
                elif occurrence == "last":
                    stamp_targets = [all_matches[-1]]
                else:
                    stamp_targets = all_matches

        # ── STAMP LOGIC ────────────────────────────
        if stamp_targets:
            for target in stamp_targets:
                page = pdf_doc[target["page"]]

                sW = min(target["width"] * 1.1, stamp_width)
                sH = stamp_height

                # ABOVE word (PyMuPDF origin = top-left)
                stamp_x = target["x"]
                stamp_y = target["y"] - sH - padding

                stamp_y = max(stamp_y, 0)

                rect = fitz.Rect(stamp_x, stamp_y, stamp_x + sW, stamp_y + sH)
                page.insert_image(rect, stream=sig_png)

        else:
            # ── FALLBACK ──────────────────────────
            for page in pdf_doc:
                pgW = page.rect.width
                pgH = page.rect.height

                sW = stamp_width
                sH = stamp_height

                if position == "bottom-right":
                    x, y = pgW - sW - 20, pgH - sH - 20
                elif position == "top-left":
                    x, y = 20, 20
                else:
                    x, y = pgW - sW - 20, pgH - sH - 20

                rect = fitz.Rect(x, y, x + sW, y + sH)
                page.insert_image(rect, stream=sig_png)

        # ── RETURN ────────────────────────────────
        output = pdf_doc.tobytes()
        output_base64 = base64.b64encode(output).decode("utf-8")

        return jsonify({
            "pdf": output_base64,
            "word_found": word_found
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── RUN ───────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
