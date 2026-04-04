"""
PDF STAMP AUTOMATION — Python Client
=====================================
  - Single PDF stamping with X/Y percent coordinates
  - Y-axis flip fix (API origin = top-left, Y increases downward)
  - Debug mode: tests all 4 axis combinations
  - Batch processing from CSV
  - Optional Google Drive upload

Usage:
  python app.py --pdf input.pdf --stamp stamp.png --x 53.4 --y 9.5
  python app.py --pdf input.pdf --stamp stamp.png --x 53.4 --y 9.5 --debug
  python app.py --batch jobs.csv
"""

import argparse
import base64
import csv
import json
import os
import requests

# ── CONFIG ────────────────────────────────────────────────

CONFIG = {
    "API_URL":      "https://pdf-stamp-api-tcau.onrender.com/stamp",
    "API_KEY":      "PDF_Stamp",
    "STAMP_WIDTH":  70,
    "STAMP_HEIGHT": 30,
    "OCCURRENCE":   "last",
    "PAGES":        "all",
    "PADDING":      6,
    "FLIP_Y":       True,   # API origin = top-left, Y increases downward
    "FLIP_X":       False,
}

# ── HELPERS ───────────────────────────────────────────────

def read_file_base64(file_path: str) -> str:
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")

def save_pdf(base64_pdf: str, output_path: str):
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(base64_pdf))
    print(f"  ✅ Saved → {output_path}")

def apply_axis_corrections(x: float, y: float, flip_x=None, flip_y=None):
    fx = CONFIG["FLIP_X"] if flip_x is None else flip_x
    fy = CONFIG["FLIP_Y"] if flip_y is None else flip_y
    api_x = round((100 - x) if fx else x, 2)
    api_y = round((100 - y) if fy else y, 2)
    return api_x, api_y

def call_stamp_api(payload: dict) -> dict:
    try:
        response = requests.post(
            CONFIG["API_URL"],
            headers={"Content-Type": "application/json", "x-api-key": CONFIG["API_KEY"]},
            data=json.dumps(payload),
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error: {e}")

    if response.status_code != 200:
        raise RuntimeError(f"API HTTP {response.status_code}: {response.text[:300]}")

    result = response.json()
    if "error" in result:
        raise RuntimeError(f"API Error: {result['error']}")
    return result

# ── CORE STAMP ────────────────────────────────────────────

def stamp_pdf(
    pdf_path:    str,
    stamp_path:  str,
    x_percent:   float,
    y_percent:   float,
    output_path: str = "Stamped_output.pdf",
    flip_x:      bool = None,
    flip_y:      bool = None,
    verbose:     bool = True,
) -> str:
    if not (0 <= x_percent <= 100 and 0 <= y_percent <= 100):
        raise ValueError(f"Coordinates must be 0–100. Got x={x_percent}, y={y_percent}")

    if verbose:
        print(f"\n📄 PDF     : {pdf_path}")
        print(f"🖼  Stamp   : {stamp_path}")
        print(f"📍 Position: X={x_percent}%  Y={y_percent}%  (user coords)")

    pdf_base64   = read_file_base64(pdf_path)
    stamp_base64 = read_file_base64(stamp_path)

    api_x, api_y = apply_axis_corrections(x_percent, y_percent, flip_x, flip_y)

    if verbose:
        print(f"🔧 API call: X={api_x}%  Y={api_y}%  "
              f"(flip_x={CONFIG['FLIP_X'] if flip_x is None else flip_x}, "
              f"flip_y={CONFIG['FLIP_Y'] if flip_y is None else flip_y})")

    result = call_stamp_api({
        "pdf":          pdf_base64,
        "signature":    stamp_base64,
        "x_percent":    api_x,
        "y_percent":    api_y,
        "stamp_width":  CONFIG["STAMP_WIDTH"],
        "stamp_height": CONFIG["STAMP_HEIGHT"],
        "occurrence":   CONFIG["OCCURRENCE"],
        "pages":        CONFIG["PAGES"],
        "padding":      CONFIG["PADDING"],
    })

    save_pdf(result["pdf"], output_path)
    return output_path

# ── DEBUG MODE ────────────────────────────────────────────

def debug_stamp(pdf_path: str, stamp_path: str, x: float, y: float):
    combos = [
        (False, False, "debug_noflip.pdf",  "No flips      (FLIP_X=False, FLIP_Y=False)"),
        (False, True,  "debug_flipY.pdf",   "Flip Y only   (FLIP_X=False, FLIP_Y=True)  ← most likely fix"),
        (True,  False, "debug_flipX.pdf",   "Flip X only   (FLIP_X=True,  FLIP_Y=False)"),
        (True,  True,  "debug_flipXY.pdf",  "Flip X and Y  (FLIP_X=True,  FLIP_Y=True)"),
    ]

    print(f"\n🔍 DEBUG MODE — X={x}%, Y={y}%\n")
    pdf_base64   = read_file_base64(pdf_path)
    stamp_base64 = read_file_base64(stamp_path)

    for flip_x, flip_y, out_file, label in combos:
        api_x, api_y = apply_axis_corrections(x, y, flip_x, flip_y)
        print(f"  [{label}]  →  X={api_x}%, Y={api_y}%  →  {out_file}")
        try:
            result = call_stamp_api({
                "pdf":          pdf_base64,
                "signature":    stamp_base64,
                "x_percent":    api_x,
                "y_percent":    api_y,
                "stamp_width":  CONFIG["STAMP_WIDTH"],
                "stamp_height": CONFIG["STAMP_HEIGHT"],
                "occurrence":   CONFIG["OCCURRENCE"],
                "pages":        CONFIG["PAGES"],
                "padding":      CONFIG["PADDING"],
            })
            save_pdf(result["pdf"], out_file)
        except Exception as e:
            print(f"    ❌ Failed: {e}")

    print("\n✅ Open the 4 PDFs and find which is correct.")
    print('   Then set "FLIP_X" and "FLIP_Y" in CONFIG.\n')

# ── BATCH ─────────────────────────────────────────────────

def batch_stamp(csv_path: str):
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    print(f"\n📋 Batch mode — {csv_path}\n")
    success = failed = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"  Found {len(rows)} job(s)\n")

    for i, row in enumerate(rows, 1):
        try:
            pdf_path    = row["pdf_path"].strip()
            stamp_path  = row["stamp_path"].strip()
            x_percent   = float(row["x_percent"])
            y_percent   = float(row["y_percent"])
            output_path = row.get("output_path", "").strip() or f"Stamped_{os.path.basename(pdf_path)}"

            out_dir = os.path.dirname(output_path)
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)

            print(f"  Job {i}/{len(rows)}: {pdf_path}")
            stamp_pdf(pdf_path, stamp_path, x_percent, y_percent, output_path)
            success += 1
        except Exception as e:
            print(f"  ❌ Job {i} failed: {e}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"  Done: {success} succeeded, {failed} failed")
    print(f"{'='*40}\n")

# ── GOOGLE DRIVE UPLOAD ───────────────────────────────────

def upload_to_drive(file_path: str, folder_id: str) -> str:
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        raise ImportError("Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")

    SCOPES     = ["https://www.googleapis.com/auth/drive.file"]
    creds      = None
    token_file = "token.json"

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                raise FileNotFoundError("credentials.json not found.")
            flow  = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as token:
            token.write(creds.to_json())

    service  = build("drive", "v3", credentials=creds)
    media    = MediaFileUpload(file_path, mimetype="application/pdf")
    uploaded = service.files().create(
        body={"name": os.path.basename(file_path), "parents": [folder_id]},
        media_body=media, fields="id,webViewLink"
    ).execute()
    url = uploaded.get("webViewLink", "")
    print(f"  ☁️  Uploaded → {url}")
    return url

# ── CLI ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="PDF Stamp — apply image at X/Y% position")
    parser.add_argument("--pdf",          help="Input PDF path")
    parser.add_argument("--stamp",        help="Stamp image path (PNG/JPG)")
    parser.add_argument("--x",  type=float, help="X position 0–100 (0=left, 100=right)")
    parser.add_argument("--y",  type=float, help="Y position 0–100 (0=bottom, 100=top)")
    parser.add_argument("--output",       help="Output PDF path")
    parser.add_argument("--debug",        action="store_true", help="Generate 4 test PDFs for axis-flip combos")
    parser.add_argument("--batch",        metavar="CSV",       help="Batch mode: path to CSV")
    parser.add_argument("--drive-folder", metavar="FOLDER_ID", help="Upload result to Google Drive folder")
    parser.add_argument("--flip-x",       action="store_true", help="Force flip X axis")
    parser.add_argument("--flip-y",       action="store_true", help="Force flip Y axis")
    parser.add_argument("--no-flip-y",    action="store_true", help="Disable Y flip")
    args = parser.parse_args()

    if args.flip_x:   CONFIG["FLIP_X"] = True
    if args.flip_y:   CONFIG["FLIP_Y"] = True
    if args.no_flip_y: CONFIG["FLIP_Y"] = False

    if args.batch:
        batch_stamp(args.batch)
        return

    if not all([args.pdf, args.stamp, args.x is not None, args.y is not None]):
        parser.error("--pdf, --stamp, --x, and --y are required.")

    if args.debug:
        debug_stamp(args.pdf, args.stamp, args.x, args.y)
        return

    output   = args.output or f"Stamped_{os.path.basename(args.pdf)}"
    out_path = stamp_pdf(args.pdf, args.stamp, args.x, args.y, output)

    if args.drive_folder:
        upload_to_drive(out_path, args.drive_folder)

if __name__ == "__main__":
    main()
