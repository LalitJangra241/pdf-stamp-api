"""
PDF STAMP AUTOMATION — Python Client
=====================================
Features:
  - Single PDF stamping with X/Y percent coordinates
  - Y-axis flip fix (API origin = top-left, Y increases downward)
  - Debug mode: tests all 4 axis combinations to find correct orientation
  - Batch processing from a CSV file
  - CLI arguments for easy usage
  - Optional Google Drive support

Install dependencies:
  pip install requests
  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client  # for Drive support

Usage examples:
  python pdf_stamp.py --pdf input.pdf --stamp stamp.png --x 10 --y 90
  python pdf_stamp.py --pdf input.pdf --stamp stamp.png --x 10 --y 90 --debug
  python pdf_stamp.py --batch jobs.csv
  python pdf_stamp.py --pdf input.pdf --stamp stamp.png --x 10 --y 90 --drive-folder YOUR_FOLDER_ID
"""

import argparse
import base64
import csv
import json
import os
import sys
import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

CONFIG = {
    "API_URL":      "https://pdf-stamp-api-tcau.onrender.com/stamp",
    "API_KEY":      "PDF_Stamp",
    "STAMP_WIDTH":  70,
    "STAMP_HEIGHT": 30,
    "OCCURRENCE":   "last",
    "PAGES":        "all",
    "PADDING":      6,

    # Y-axis correction:
    #   True  → flips Y before sending  (API origin = top-left, Y increases downward)
    #   False → sends Y as-is           (API origin = bottom-left, Y increases upward)
    "FLIP_Y": True,

    # X-axis correction (usually not needed, but here for completeness):
    "FLIP_X": False,
}

# ── HELPERS ───────────────────────────────────────────────────────────────────

def read_file_base64(file_path: str) -> str:
    """Read a local file and return as base64 string."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    with open(file_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def save_pdf(base64_pdf: str, output_path: str):
    """Decode base64 PDF and save to file."""
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(base64_pdf))
    print(f"  ✅ Saved → {output_path}")


def call_stamp_api(payload: dict) -> dict:
    """POST to the stamp API and return the result dict."""
    try:
        response = requests.post(
            CONFIG["API_URL"],
            headers={
                "Content-Type": "application/json",
                "x-api-key":    CONFIG["API_KEY"],
            },
            data=json.dumps(payload),
            timeout=60,
        )
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Network error calling API: {e}")

    if response.status_code != 200:
        raise RuntimeError(f"API HTTP {response.status_code}: {response.text[:300]}")

    result = response.json()
    if "error" in result:
        raise RuntimeError(f"API Error: {result['error']}")

    return result


def apply_axis_corrections(x_percent: float, y_percent: float,
                            flip_x: bool = None, flip_y: bool = None):
    """Apply axis flips based on CONFIG (or overrides)."""
    fx = CONFIG["FLIP_X"] if flip_x is None else flip_x
    fy = CONFIG["FLIP_Y"] if flip_y is None else flip_y

    api_x = round((100 - x_percent) if fx else x_percent, 2)
    api_y = round((100 - y_percent) if fy else y_percent, 2)
    return api_x, api_y


# ── CORE STAMP FUNCTION ───────────────────────────────────────────────────────

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
    """
    Stamp a PDF with an image at the given X/Y percentage position.

    Args:
        pdf_path    : Path to input PDF
        stamp_path  : Path to stamp/signature image (PNG/JPG)
        x_percent   : 0–100, left→right  (user-facing)
        y_percent   : 0–100, bottom→top  (user-facing)
        output_path : Where to save the stamped PDF
        flip_x/y    : Override CONFIG axis flips for this call
        verbose     : Print progress messages

    Returns:
        output_path (str)
    """
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


# ── DEBUG MODE ────────────────────────────────────────────────────────────────

def debug_stamp(pdf_path: str, stamp_path: str,
                x_percent: float, y_percent: float):
    """
    Generate 4 PDFs — one for each axis-flip combination.
    Visually compare them to find which one places the stamp correctly,
    then set CONFIG['FLIP_X'] and CONFIG['FLIP_Y'] to match.

    Output files:
      debug_noflip.pdf      → FLIP_X=False, FLIP_Y=False
      debug_flipY.pdf       → FLIP_X=False, FLIP_Y=True   ← most common fix
      debug_flipX.pdf       → FLIP_X=True,  FLIP_Y=False
      debug_flipXY.pdf      → FLIP_X=True,  FLIP_Y=True
    """
    combos = [
        (False, False, "debug_noflip.pdf",  "No flips      (FLIP_X=False, FLIP_Y=False)"),
        (False, True,  "debug_flipY.pdf",   "Flip Y only   (FLIP_X=False, FLIP_Y=True) ← most likely fix"),
        (True,  False, "debug_flipX.pdf",   "Flip X only   (FLIP_X=True,  FLIP_Y=False)"),
        (True,  True,  "debug_flipXY.pdf",  "Flip X and Y  (FLIP_X=True,  FLIP_Y=True)"),
    ]

    print(f"\n🔍 DEBUG MODE — generating 4 test PDFs for X={x_percent}%, Y={y_percent}%")
    print("   Open each output PDF and find which one places the stamp correctly.\n")

    pdf_base64   = read_file_base64(pdf_path)
    stamp_base64 = read_file_base64(stamp_path)

    for flip_x, flip_y, out_file, label in combos:
        api_x, api_y = apply_axis_corrections(x_percent, y_percent, flip_x, flip_y)
        print(f"  [{label}]")
        print(f"    Sending X={api_x}%, Y={api_y}% → {out_file}")
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

    print("\n✅ Debug complete. Open the 4 PDFs and find which one is correct.")
    print("   Then update CONFIG at the top of this file:")
    print('   "FLIP_X": True/False')
    print('   "FLIP_Y": True/False\n')


# ── BATCH PROCESSING ──────────────────────────────────────────────────────────

def batch_stamp(csv_path: str):
    """
    Process multiple stamp jobs from a CSV file.

    CSV format (with header row):
      pdf_path, stamp_path, x_percent, y_percent, output_path

    Example:
      pdf_path,stamp_path,x_percent,y_percent,output_path
      invoice1.pdf,sig.png,10,90,out/invoice1_signed.pdf
      invoice2.pdf,sig.png,85,15,out/invoice2_signed.pdf
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    print(f"\n📋 Batch mode — reading jobs from: {csv_path}\n")

    success = 0
    failed  = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)

    print(f"  Found {len(rows)} job(s)\n")

    for i, row in enumerate(rows, 1):
        try:
            pdf_path    = row["pdf_path"].strip()
            stamp_path  = row["stamp_path"].strip()
            x_percent   = float(row["x_percent"])
            y_percent   = float(row["y_percent"])
            output_path = row.get("output_path", "").strip() or f"Stamped_{os.path.basename(pdf_path)}"

            # Auto-create output directory if needed
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
    print(f"  Batch complete: {success} succeeded, {failed} failed")
    print(f"{'='*40}\n")


# ── GOOGLE DRIVE SUPPORT ──────────────────────────────────────────────────────

def upload_to_drive(file_path: str, folder_id: str) -> str:
    """
    Upload a local file to Google Drive and return its shareable URL.
    Requires: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client
    Also requires a 'credentials.json' file from Google Cloud Console.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
    except ImportError:
        raise ImportError(
            "Google Drive libraries not installed.\n"
            "Run: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client"
        )

    SCOPES = ["https://www.googleapis.com/auth/drive.file"]
    creds  = None
    token_file = "token.json"

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists("credentials.json"):
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow  = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as token:
            token.write(creds.to_json())

    service    = build("drive", "v3", credentials=creds)
    file_name  = os.path.basename(file_path)
    media      = MediaFileUpload(file_path, mimetype="application/pdf")
    file_meta  = {"name": file_name, "parents": [folder_id]}

    uploaded   = service.files().create(body=file_meta, media_body=media, fields="id,webViewLink").execute()
    url        = uploaded.get("webViewLink", "")
    print(f"  ☁️  Uploaded to Drive → {url}")
    return url


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="PDF Stamp Automation — apply an image stamp at X/Y% position",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single stamp (top-left area)
  python pdf_stamp.py --pdf input.pdf --stamp sig.png --x 10 --y 90

  # Debug: generate 4 variants to find correct axis orientation
  python pdf_stamp.py --pdf input.pdf --stamp sig.png --x 10 --y 90 --debug

  # Batch from CSV
  python pdf_stamp.py --batch jobs.csv

  # Single stamp + upload result to Google Drive
  python pdf_stamp.py --pdf input.pdf --stamp sig.png --x 10 --y 90 --drive-folder FOLDER_ID
        """
    )

    # Single job args
    parser.add_argument("--pdf",          help="Path to input PDF")
    parser.add_argument("--stamp",        help="Path to stamp/signature image")
    parser.add_argument("--x",  type=float, help="X position 0–100 (0=left, 100=right)")
    parser.add_argument("--y",  type=float, help="Y position 0–100 (0=bottom, 100=top)")
    parser.add_argument("--output",       help="Output PDF path (default: Stamped_<input>.pdf)")

    # Modes
    parser.add_argument("--debug",  action="store_true", help="Generate 4 test PDFs for all axis-flip combos")
    parser.add_argument("--batch",  metavar="CSV",       help="Batch mode: path to CSV file of jobs")

    # Drive upload
    parser.add_argument("--drive-folder", metavar="FOLDER_ID",
                        help="Upload stamped PDF to this Google Drive folder ID")

    # Axis overrides
    parser.add_argument("--flip-x", action="store_true", help="Force flip X axis")
    parser.add_argument("--flip-y", action="store_true", help="Force flip Y axis")
    parser.add_argument("--no-flip-y", action="store_true", help="Force disable Y flip (override CONFIG)")

    return parser


def main():
    parser = build_parser()
    args   = parser.parse_args()

    # Apply CLI axis overrides
    if args.flip_x:
        CONFIG["FLIP_X"] = True
    if args.flip_y:
        CONFIG["FLIP_Y"] = True
    if args.no_flip_y:
        CONFIG["FLIP_Y"] = False

    # ── BATCH MODE ──
    if args.batch:
        batch_stamp(args.batch)
        return

    # ── SINGLE / DEBUG MODE — require pdf, stamp, x, y ──
    if not all([args.pdf, args.stamp, args.x is not None, args.y is not None]):
        parser.error("--pdf, --stamp, --x, and --y are required for single-job mode.")

    if args.debug:
        debug_stamp(args.pdf, args.stamp, args.x, args.y)
        return

    output = args.output or f"Stamped_{os.path.basename(args.pdf)}"
    out_path = stamp_pdf(args.pdf, args.stamp, args.x, args.y, output)

    if args.drive_folder:
        upload_to_drive(out_path, args.drive_folder)


if __name__ == "__main__":
    main()
