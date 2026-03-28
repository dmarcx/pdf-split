"""
chunk_ocr.py - Extract text from architectural drawing PDFs using chunk-based OCR.

Splits a PDF page into overlapping tiles at high DPI, runs Tesseract on each tile,
deduplicates results and filters out noise (short fragments, non-text lines).

Usage:
    python chunk_ocr.py "path/to/drawing.pdf" [page_number]
    python chunk_ocr.py "path/to/drawing.pdf" 0          # first page (default)

Dependencies:
    pip install pymupdf pytesseract pillow
    Tesseract must be installed with Hebrew support (heb.traineddata)
"""

import io
import re
import sys
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DPI = 400          # Render resolution (higher = more detail, slower)
COLS = 8           # Number of horizontal tiles
ROWS = 6           # Number of vertical tiles
OVERLAP = 0.20     # Tile overlap fraction (20%)
LANG = "heb+eng"   # Tesseract language string

# Quality filters
MIN_WORD_LEN = 2        # Ignore tokens shorter than this
MIN_LINE_CHARS = 3      # Ignore lines with fewer printable chars
HEBREW_CHAR_RE = re.compile(r'[\u05d0-\u05ea]')  # Hebrew unicode block
NOISE_RATIO_THRESHOLD = 0.30  # Lines with <30% Hebrew/Latin/digit chars are noise

TESSERACT_WIN_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _setup_tesseract():
    try:
        import pytesseract
        try:
            pytesseract.get_tesseract_version()
            return pytesseract
        except Exception:
            pass
        if Path(TESSERACT_WIN_PATH).is_file():
            pytesseract.pytesseract.tesseract_cmd = TESSERACT_WIN_PATH
            pytesseract.get_tesseract_version()
            return pytesseract
    except ImportError:
        pass
    raise RuntimeError("Tesseract not found. Install pytesseract and Tesseract-OCR.")


def _available_langs(tess):
    try:
        return tess.get_languages()
    except Exception:
        return ["eng"]


def _build_lang_string(tess, wanted: str) -> str:
    available = _available_langs(tess)
    usable = [l for l in wanted.split("+") if l in available]
    return "+".join(usable) if usable else "eng"


def _is_quality_line(line: str) -> bool:
    """Return True if the line looks like real text (not OCR noise)."""
    stripped = line.strip()
    if len(stripped) < MIN_LINE_CHARS:
        return False
    # Count meaningful characters: Hebrew, Latin letters, digits
    meaningful = sum(
        1 for c in stripped
        if c.isalpha() or c.isdigit()
    )
    total = len(stripped)
    if total == 0:
        return False
    ratio = meaningful / total
    if ratio < NOISE_RATIO_THRESHOLD:
        return False
    # Reject lines that are only punctuation/symbols/numbers with no letters
    has_letter = any(c.isalpha() for c in stripped)
    if not has_letter:
        return False
    return True


def _normalize_line(line: str) -> str:
    """Strip and collapse whitespace."""
    return re.sub(r'\s+', ' ', line.strip())


def ocr_pdf_chunks(pdf_path: str, page_index: int = 0) -> list[str]:
    """
    Render a PDF page in tiles, OCR each tile, deduplicate, and return clean lines.

    Returns list of unique text lines, ordered approximately by position.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("PyMuPDF not installed. Run: pip install pymupdf")

    from PIL import Image
    import pytesseract

    tess = _setup_tesseract()
    lang = _build_lang_string(tess, LANG)
    print(f"  Tesseract lang: {lang}", file=sys.stderr)

    doc = fitz.open(pdf_path)
    if page_index >= len(doc):
        raise ValueError(f"Page {page_index} out of range (doc has {len(doc)} pages)")
    page = doc[page_index]

    # Render the full page at DPI
    scale = DPI / 72.0
    mat = fitz.Matrix(scale, scale)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    img_bytes = pix.tobytes("png")
    full_img = Image.open(io.BytesIO(img_bytes)).convert("L")

    w, h = full_img.size
    print(f"  Page size at {DPI} DPI: {w} x {h} px", file=sys.stderr)

    # Calculate tile dimensions with overlap
    tile_w = int(w / COLS)
    tile_h = int(h / ROWS)
    pad_x = int(tile_w * OVERLAP)
    pad_y = int(tile_h * OVERLAP)

    seen: set[str] = set()
    results: list[tuple[int, int, str]] = []  # (row, col, line)

    total_tiles = COLS * ROWS
    for row in range(ROWS):
        for col in range(COLS):
            tile_num = row * COLS + col + 1
            print(f"  Tile {tile_num}/{total_tiles} (row={row}, col={col})...", end="\r", file=sys.stderr)

            x0 = max(0, col * tile_w - pad_x)
            y0 = max(0, row * tile_h - pad_y)
            x1 = min(w, (col + 1) * tile_w + pad_x)
            y1 = min(h, (row + 1) * tile_h + pad_y)

            tile = full_img.crop((x0, y0, x1, y1))

            # Upscale small tiles for better OCR
            tw, th = tile.size
            if tw < 300 or th < 300:
                scale_up = max(300 / tw, 300 / th)
                tile = tile.resize((int(tw * scale_up), int(th * scale_up)), Image.LANCZOS)

            # Binarize with Otsu threshold via point()
            # Simple global binarization — good for architectural drawings
            tile = tile.point(lambda p: 255 if p > 180 else 0, '1').convert('L')

            raw = pytesseract.image_to_string(
                tile,
                lang=lang,
                config="--psm 6 --oem 1",   # psm 6 = assume uniform block of text
            )

            for line in raw.split("\n"):
                norm = _normalize_line(line)
                if not _is_quality_line(norm):
                    continue
                if norm not in seen:
                    seen.add(norm)
                    results.append((row, col, norm))

    print(f"\n  Done. {len(results)} unique quality lines extracted.", file=sys.stderr)

    # Return lines in rough top-to-bottom, left-to-right order
    results.sort(key=lambda x: (x[0], x[1]))
    return [line for _, _, line in results]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    pdf_path = sys.argv[1]
    page_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    if not Path(pdf_path).is_file():
        print(f"Error: file not found: {pdf_path}")
        sys.exit(1)

    print(f"Processing: {pdf_path} (page {page_index})")
    print(f"Settings: {DPI} DPI, {COLS}x{ROWS} grid, {int(OVERLAP*100)}% overlap")
    print()

    lines = ocr_pdf_chunks(pdf_path, page_index)

    print("\n" + "=" * 60)
    print(f"EXTRACTED TEXT ({len(lines)} lines)")
    print("=" * 60)
    for i, line in enumerate(lines, 1):
        print(f"{i:3d}. {line}")

    # Save to text file
    out_path = Path(pdf_path).with_suffix("") .parent / (Path(pdf_path).stem + f"_page{page_index}_ocr.txt")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nSaved to: {out_path}")


if __name__ == "__main__":
    main()
