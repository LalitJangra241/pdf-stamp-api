####################################################
# PDF STAMP API — Python Version
# Word-Find Edition
# Dependencies: flask, pymupdf, pillow
####################################################

import base64
import io
import json
from flask import Flask, request, jsonify
import fitz        # PyMuPDF  ← finds words + stamps
from PIL import Image

app = Flask(__name__)
API_KEY = "PDF_Stamp"


# ── AUTH ──────────────────────────────────────────
@app.before_request
def check_auth():
    if request.path == "/":
        return  # skip auth for health check
    if request.headers.get("x-api-key") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401


# ── HEALTH CHECK ──────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "PDF Stamp API Running ✅"})


# ── STAMP ENDPOINT ────────────────────────────────
@app.route("/stamp", methods=["POST"])
def stamp():
    try:
        data = request.get_json()

        if not data or "pdf" not in data or "signature" not in data:
            return jsonify({"error": "pdf and signature are required"}), 400

        # ── Read inputs ───────────────────────────
        pdf_bytes = base64.b64decode(data["pdf"])
        sig_bytes = base64.b64decode(data["signature"])

        search_word  = data.get("search_word", "")
        occurrence   = data.get("occurrence", "last")     # first | last | all
        position     = data.get("position", "bottom-right")
        stamp_width  = float(data.get("stamp_width", 150))
        stamp_height = float(data.get("stamp_height", 75))
        padding      = float(data.get("padding", 6))

        # ── Convert signature to PNG (PIL) ────────
        sig_image = Image.open(io.BytesIO(sig_bytes)).convert("RGBA")
        sig_png_io = io.BytesIO()
        sig_image.save(sig_png_io, format="PNG")
        sig_png_bytes = sig_png_io.getvalue()

        # ── Open PDF with PyMuPDF ─────────────────
        pdf_doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        stamp_targets = []
        word_found    = False

        # ── Search for word ───────────────────────
        if search_word:
            all_matches = []

            for page_num in range(len(pdf_doc)):
                page = pdf_doc[page_num]
                # search_for returns list of Rect objects for each match
                instances = page.search_for(search_word)

                for rect in instances:
                    all_matches.append({
                        "page":   page_num,
                        "x":      rect.x0,
                        "y":      rect.y0,       # top of word (PyMuPDF: y increases downward)
                        "width":  rect.width,
                        "height": rect.height,
                        "rect":   rect
                    })

            if all_matches:
                word_found = True
                if occurrence == "first":
                    stamp_targets = [all_matches[0]]
                elif occurrence == "last":
                    stamp_targets = [all_matches[-1]]
                else:
                    stamp_targets = all_matches

        # ── Place stamp ───────────────────────────
        if stamp_targets:
            # Word found → stamp ABOVE the word
            for target in stamp_targets:
                page = pdf_doc[target["page"]]

                # Match stamp width to word width (capped at stamp_width)
                sW = min(target["width"] * 1.1, stamp_width)
                sH = stamp_height

                # PyMuPDF: y=0 is TOP, increases downward
                # So "above the word" = word y0 - stamp height - padding
                stamp_x = target["x"]
                stamp_y = target["y"] - sH - padding

                # Clamp so stamp doesn't go off page
                stamp_y = max(stamp_y, 0)

                stamp_rect = fitz.Rect(stamp_x, stamp_y, stamp_x + sW, stamp_y + sH)
                page.insert_image(stamp_rect, stream=sig_png_bytes)

        else:
            # Word not found → fallback fixed position
            for page in pdf_doc:
                pgW = page.rect.width
                pgH = page.rect.height
                sW  = stamp_width
                sH  = stamp_height

                if position == "bottom-right":
                    x, y = pgW - sW - 20, pgH - sH - 20
                elif position == "bottom-left":
                    x, y = 20, pgH - sH - 20
                elif position == "top-right":
                    x, y = pgW - sW - 20, 20
                elif position == "top-left":
                    x, y = 20, 20
                elif position == "center":
                    x, y = (pgW - sW) / 2, (pgH - sH) / 2
                else:
                    x, y = pgW - sW - 20, pgH - sH - 20

                stamp_rect = fitz.Rect(x, y, x + sW, y + sH)
                page.insert_image(stamp_rect, stream=sig_png_bytes)

        # ── Save & return ─────────────────────────
        output_bytes  = pdf_doc.tobytes()
        output_base64 = base64.b64encode(output_bytes).decode("utf-8")

        return jsonify({
            "pdf":        output_base64,
            "word_found": word_found
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
```

---

### Step 4: Check your `render.yaml` or Start Command
In Render → **Settings** → **Start Command**, make sure it says:
```
python app.py
```
*(or whatever your main file is named)*

---

### Step 5: Commit & Push to GitHub
After saving both files on GitHub, Render will **auto-deploy**. Watch the **Logs** tab — it should show:
```
✅ Server running on port ...
```

---

### Step 6: Test the health check
Open in browser:
```
https://pdf-stamp-api-tcau.onrender.com/
