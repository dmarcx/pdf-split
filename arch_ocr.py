"""
arch_ocr.py - OCR optimized for architectural drawing PDFs.

Strategy:
  1. Render page at high DPI
  2. Remove long horizontal/vertical lines (architecture lines, not text)
  3. Run Tesseract with --psm 12 (sparse text) for scattered text
  4. Also run focused OCR on the title block (bottom-right 35% of page)
  5. Filter and deduplicate results

Usage:
    python arch_ocr.py "path/to/drawing.pdf" [page_number]

Dependencies:
    pip install pymupdf pytesseract pillow
"""

import io
import re
import sys
from pathlib import Path

DPI = 400
LANG = "heb+eng"
TESSERACT_WIN_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
MIN_LINE_CHARS = 3
NOISE_RATIO_THRESHOLD = 0.35


def _setup_tesseract():
    import pytesseract
    try:
        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception:
        if Path(TESSERACT_WIN_PATH).is_file():
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_WIN_PATH
            pytesseract.get_tesseract_version()
            return pytesseract
    raise RuntimeError("Tesseract not found.")


def _build_lang_string(tess, wanted: str) -> str:
    try:
        available = tess.get_languages()
    except Exception:
        available = ["eng"]
    usable = [l for l in wanted.split("+") if l in available]
    return "+".join(usable) if usable else "eng"


def _is_quality_line(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) < MIN_LINE_CHARS:
        return False
    meaningful = sum(1 for c in stripped if c.isalpha() or c.isdigit())
    total = len(stripped)
    if total == 0 or meaningful / total < NOISE_RATIO_THRESHOLD:
        return False
    return any(c.isalpha() for c in stripped)


def _normalize(line: str) -> str:
    return re.sub(r'\s+', ' ', line.strip())


def _remove_long_lines(img):
    """
    Remove long horizontal and vertical lines using PIL morphology.
    This removes architectural dimension lines, wall lines, etc.
    Returns cleaned image (PIL Image, mode 'L').
    """
    from PIL import Image, ImageFilter, ImageChops

    gray = img.convert("L")
    # Binarize
    bw = gray.point(lambda p: 0 if p < 180 else 255)

    # Detect horizontal lines: erode vertically (keep only wide horizontal spans)
    # We simulate this by looking for rows with very high black-pixel density
    import struct, array

    w, h = bw.size
    pixels = bw.load()

    # Create a mask of "line pixels" to remove
    mask = Image.new("L", (w, h), 0)
    mask_px = mask.load()

    MIN_LINE_LEN = 80  # pixels; lines shorter than this are likely text strokes

    # Horizontal line removal: find rows of consecutive black pixels
    for y in range(h):
        run_start = None
        for x in range(w):
            is_black = pixels[x, y] < 128
            if is_black and run_start is None:
                run_start = x
            elif not is_black and run_start is not None:
                run_len = x - run_start
                if run_len >= MIN_LINE_LEN:
                    for xi in range(run_start, x):
                        mask_px[xi, y] = 255
                run_start = None
        if run_start is not None and w - run_start >= MIN_LINE_LEN:
            for xi in range(run_start, w):
                mask_px[xi, y] = 255

    # Vertical line removal
    for x in range(w):
        run_start = None
        for y in range(h):
            is_black = pixels[x, y] < 128
            if is_black and run_start is None:
                run_start = y
            elif not is_black and run_start is not None:
                run_len = y - run_start
                if run_len >= MIN_LINE_LEN:
                    for yi in range(run_start, y):
                        if mask_px[x, yi] == 255:  # already masked horiz, skip
                            pass
                        mask_px[x, yi] = 255
                run_start = None
        if run_start is not None and h - run_start >= MIN_LINE_LEN:
            for yi in range(run_start, h):
                mask_px[x, yi] = 255

    # Apply mask: wherever mask=255 (line), set pixel to white
    result = gray.copy()
    result_px = result.load()
    for y in range(h):
        for x in range(w):
            if mask_px[x, y] == 255:
                result_px[x, y] = 255

    return result


def _remove_long_lines_fast(img):
    """
    Fast version using PIL row/column statistics instead of pixel-by-pixel.
    """
    from PIL import Image
    import struct

    gray = img.convert("L")
    w, h = gray.size

    # Get raw bytes for fast access
    data = list(gray.getdata())  # flat list of pixel values

    MIN_LINE_LEN = 60

    # Work with a mutable copy
    result_data = list(data)

    # Horizontal line detection and removal
    for y in range(h):
        row = data[y * w: (y + 1) * w]
        run_start = None
        for x, val in enumerate(row):
            is_black = val < 200
            if is_black and run_start is None:
                run_start = x
            elif not is_black and run_start is not None:
                if x - run_start >= MIN_LINE_LEN:
                    for xi in range(run_start, x):
                        result_data[y * w + xi] = 255
                run_start = None
        if run_start is not None and w - run_start >= MIN_LINE_LEN:
            for xi in range(run_start, w):
                result_data[y * w + xi] = 255

    # Vertical line detection and removal (work on result_data now)
    for x in range(w):
        col = [result_data[y * w + x] for y in range(h)]
        run_start = None
        for y, val in enumerate(col):
            is_black = val < 200
            if is_black and run_start is None:
                run_start = y
            elif not is_black and run_start is not None:
                if y - run_start >= MIN_LINE_LEN:
                    for yi in range(run_start, y):
                        result_data[yi * w + x] = 255
                run_start = None
        if run_start is not None and h - run_start >= MIN_LINE_LEN:
            for yi in range(run_start, h):
                result_data[yi * w + x] = 255

    result = Image.new("L", (w, h))
    result.putdata(result_data)
    return result


def ocr_image(img, tess, lang: str, psm: int = 6) -> list[str]:
    """Run Tesseract on a PIL image and return quality lines."""
    raw = tess.image_to_string(img, lang=lang, config=f"--psm {psm} --oem 1")
    lines = []
    for line in raw.split("\n"):
        norm = _normalize(line)
        if _is_quality_line(norm):
            lines.append(norm)
    return lines


def process_pdf(pdf_path: str, page_index: int = 0) -> dict:
    import fitz
    from PIL import Image

    tess = _setup_tesseract()
    lang = _build_lang_string(tess, LANG)
    print(f"  Tesseract lang: {lang}", file=sys.stderr)

    doc = fitz.open(pdf_path)
    page = doc[page_index]

    scale = DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    full_img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    w, h = full_img.size
    print(f"  Image: {w}x{h} px at {DPI} DPI", file=sys.stderr)

    results = {}

    # --- Strategy 1: Full page, sparse text mode ---
    print("  [1/4] Full page, sparse text (psm 12)...", file=sys.stderr)
    lines_sparse = ocr_image(full_img, tess, lang, psm=12)
    results["full_sparse"] = lines_sparse

    # --- Strategy 2: Line-removed image, block mode ---
    print("  [2/4] Removing architectural lines...", file=sys.stderr)
    cleaned = _remove_long_lines_fast(full_img)
    print("  [3/4] OCR on line-removed image (psm 12)...", file=sys.stderr)
    lines_cleaned = ocr_image(cleaned, tess, lang, psm=12)
    results["line_removed"] = lines_cleaned

    # --- Strategy 3: Title block focus (bottom-right 40% x 30%) ---
    print("  [4/4] Title block OCR (bottom-right region)...", file=sys.stderr)
    tb_x0 = int(w * 0.55)
    tb_y0 = int(h * 0.65)
    title_block = full_img.crop((tb_x0, tb_y0, w, h))
    # Scale up for better OCR
    tb_w, tb_h = title_block.size
    title_block = title_block.resize((tb_w * 2, tb_h * 2))
    lines_tb = []
    for psm in (6, 4, 12):
        lines = ocr_image(title_block, tess, lang, psm=psm)
        lines_tb.extend(lines)

    # Also try top strip (title/header area)
    header = full_img.crop((0, 0, w, int(h * 0.12)))
    header_w, header_h = header.size
    header = header.resize((header_w, header_h * 2))
    lines_header = ocr_image(header, tess, lang, psm=6)

    results["title_block"] = lines_tb
    results["header"] = lines_header

    return results


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]
    page_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if not Path(pdf_path).is_file():
        print(f"Error: {pdf_path}")
        sys.exit(1)

    print(f"Processing: {pdf_path} (page {page_index})")
    print()

    results = process_pdf(pdf_path, page_index)

    all_lines: set[str] = set()
    output_sections = []

    for section, lines in results.items():
        unique = [l for l in lines if l not in all_lines]
        all_lines.update(lines)
        if unique:
            output_sections.append((section, unique))

    print("\n" + "=" * 60)
    for section, lines in output_sections:
        print(f"\n--- {section.upper()} ({len(lines)} lines) ---")
        for line in lines:
            print(f"  {line}")

    # Save combined output
    combined = []
    for section, lines in output_sections:
        combined.append(f"=== {section} ===")
        combined.extend(lines)
        combined.append("")

    out = Path(pdf_path).parent / (Path(pdf_path).stem + f"_p{page_index}_arch_ocr.txt")
    out.write_text("\n".join(combined), encoding="utf-8")
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
