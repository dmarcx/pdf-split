# PDF Split - Building Permit Drawings Cropper

## Project Purpose
Crop white margins from PDF pages containing building permit drawings (גרמושקה).
The drawings don't fill the full page - this tool detects actual content bounds and removes surrounding whitespace.

## Main Script
`crop_pdf.py` - Single-file tool, no configuration needed.

**Dependency:** `pip install pymupdf`

## Usage
```bash
# Basic - creates input_cropped.pdf
python crop_pdf.py "input.pdf"

# With custom output name
python crop_pdf.py "input.pdf" "output.pdf"

# With larger margins (default: 10 points ≈ 3.5mm)
python crop_pdf.py "input.pdf" --margin 20

# Higher detection accuracy (slower)
python crop_pdf.py "input.pdf" --dpi 200
```

## How Content Detection Works
1. **Primary (vector):** Uses PyMuPDF native detection:
   - `page.get_text("blocks")` - text bounding boxes
   - `page.get_drawings()` - vector lines, rectangles, curves
   - `page.get_image_info()` - embedded images
   - Unions all bounding boxes → content bbox
2. **Fallback (pixel scan):** For scanned/rasterized pages - renders to grayscale and finds non-white pixels.

The vector approach is used when available - it's faster and catches thin lines that pixel scanning misses.

## Key Notes
- Uses `page.set_cropbox()` to crop - non-destructive, original data preserved
- `--margin` adds padding around detected content (in PDF points, 1pt ≈ 0.35mm)
- Windows terminal: use UTF-8 if needed (`set PYTHONIOENCODING=utf-8`)
- Output file saved as `{input}_cropped.pdf` by default

## Tested On
- `1165 - H - 28.07.25_1.pdf` (1 page, 3370×2384 pt → 2384×1678 pt)
