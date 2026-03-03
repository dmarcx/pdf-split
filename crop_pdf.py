"""
crop_pdf.py
-----------
Remove white space margins from PDF pages (building permit drawings / גרמושקה).
Detects actual content bounding box per page and crops accordingly.

Usage:
    python crop_pdf.py input.pdf [output.pdf] [--margin 10] [--dpi 150]

Requirements:
    pip install pymupdf
"""

import sys
import argparse
from pathlib import Path
import fitz  # PyMuPDF


def find_content_bbox(page: fitz.Page, dpi: int = 150, threshold: int = 240) -> fitz.Rect:
    """
    Find the bounding box of actual content on the page.

    Uses PyMuPDF's native vector/text detection (fast, accurate),
    with a pixel-based fallback for image-only pages.

    Args:
        page: PyMuPDF page object
        dpi: Resolution for pixel fallback rendering
        threshold: White threshold for pixel fallback (0-255)

    Returns:
        fitz.Rect with the content bounding box in PDF coordinates (points)
    """
    bbox = fitz.Rect()  # empty rect - will be expanded

    # 1. Text blocks
    for block in page.get_text("blocks"):
        r = fitz.Rect(block[:4])
        if not r.is_empty:
            bbox |= r

    # 2. Vector drawings (lines, rectangles, curves, fills)
    for drawing in page.get_drawings():
        r = drawing.get("rect")
        if r and not fitz.Rect(r).is_empty:
            bbox |= fitz.Rect(r)

    # 3. Embedded images
    for img_info in page.get_image_info():
        r = fitz.Rect(img_info["bbox"])
        if not r.is_empty:
            bbox |= r

    # If vector detection found content, return it
    if not bbox.is_empty:
        return bbox

    # Fallback: pixel-based scan (for scanned/rasterized pages)
    print(" [pixel scan fallback]", end="")
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    img_data = pix.samples
    width, height = pix.width, pix.height

    min_row, max_row = height, 0
    min_col, max_col = width, 0
    found = False

    for row in range(height):
        for col in range(width):
            if img_data[row * width + col] < threshold:
                found = True
                if row < min_row: min_row = row
                if row > max_row: max_row = row
                if col < min_col: min_col = col
                if col > max_col: max_col = col

    if not found:
        return page.rect

    scale = 72 / dpi
    return fitz.Rect(min_col * scale, min_row * scale,
                     (max_col + 1) * scale, (max_row + 1) * scale)


def crop_pdf(input_path: str, output_path: str, margin: float = 10.0, dpi: int = 150):
    """
    Crop all pages in a PDF to remove white space margins.

    Args:
        input_path: Path to input PDF
        output_path: Path to output PDF
        margin: Extra margin to add around detected content (in points, 1pt ≈ 0.35mm)
        dpi: Resolution for content detection
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    print(f"Opening: {input_path.name}")
    doc = fitz.open(str(input_path))

    print(f"Pages: {len(doc)}")

    for page_num in range(len(doc)):
        page = doc[page_num]
        original_rect = page.rect

        print(f"  Page {page_num + 1}: original size = {original_rect.width:.1f} x {original_rect.height:.1f} pt", end="")

        # Find content bounding box
        content_bbox = find_content_bbox(page, dpi=dpi)

        # Add margin, clamped to MediaBox
        media_box = page.mediabox
        padded_bbox = fitz.Rect(
            max(media_box.x0, content_bbox.x0 - margin),
            max(media_box.y0, content_bbox.y0 - margin),
            min(media_box.x1, content_bbox.x1 + margin),
            min(media_box.y1, content_bbox.y1 + margin)
        )

        # Intersect with MediaBox to ensure validity
        padded_bbox = padded_bbox & media_box

        # Set the crop box
        page.set_cropbox(padded_bbox)

        print(f"  ->  cropped to {padded_bbox.width:.1f} x {padded_bbox.height:.1f} pt")

    print(f"\nSaving: {output_path.name}")
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()

    print(f"Done! Output saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Crop white margins from PDF pages (building permit drawings)"
    )
    parser.add_argument("input", help="Input PDF file path")
    parser.add_argument("output", nargs="?", help="Output PDF file path (default: input_cropped.pdf)")
    parser.add_argument("--margin", type=float, default=10.0,
                        help="Extra margin around content in points (default: 10)")
    parser.add_argument("--dpi", type=int, default=150,
                        help="Resolution for content detection (default: 150)")
    parser.add_argument("--threshold", type=int, default=240,
                        help="White threshold 0-255, lower = stricter (default: 240)")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_name(input_path.stem + "_cropped" + input_path.suffix)

    crop_pdf(str(input_path), str(output_path), margin=args.margin, dpi=args.dpi)


if __name__ == "__main__":
    main()
