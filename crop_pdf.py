"""
crop_pdf.py
-----------
Remove white space margins from PDF pages (building permit drawings / גרמושקה).
Supports fixed grid split and content-aware smart split.

Usage:
    # Crop white margins only
    python crop_pdf.py input.pdf [output.pdf] [--margin 10]

    # Fixed equal grid (e.g. 3x3)
    python crop_pdf.py input.pdf --grid 3x3

    # Smart split: find natural gaps between drawings (e.g. 1 row x 2 cols)
    python crop_pdf.py input.pdf --auto-grid 1x2

Requirements:
    pip install pymupdf
"""

import sys
import argparse
from pathlib import Path
import fitz  # PyMuPDF


# ---------------------------------------------------------------------------
# Content detection
# ---------------------------------------------------------------------------

def find_content_bbox(page: fitz.Page, dpi: int = 150, threshold: int = 240) -> fitz.Rect:
    """
    Find the bounding box of actual content on the page.
    Uses PyMuPDF native vector/text/image detection, with pixel fallback.
    """
    bbox = fitz.Rect()

    for block in page.get_text("blocks"):
        r = fitz.Rect(block[:4])
        if not r.is_empty:
            bbox |= r

    for drawing in page.get_drawings():
        r = drawing.get("rect")
        if r and not fitz.Rect(r).is_empty:
            bbox |= fitz.Rect(r)

    for img_info in page.get_image_info():
        r = fitz.Rect(img_info["bbox"])
        if not r.is_empty:
            bbox |= r

    if not bbox.is_empty:
        return bbox

    # Fallback: pixel scan
    print(" [pixel scan fallback]", end="")
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
    samples = pix.samples
    W, H = pix.width, pix.height

    min_row, max_row = H, 0
    min_col, max_col = W, 0
    found = False
    for i, val in enumerate(samples):
        if val < threshold:
            row, col = i // W, i % W
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


def get_content_rect(page: fitz.Page, margin: float, dpi: int) -> fitz.Rect:
    """Find content bbox, add margin, clamp to MediaBox."""
    content_bbox = find_content_bbox(page, dpi=dpi)
    media_box = page.mediabox
    padded = fitz.Rect(
        max(media_box.x0, content_bbox.x0 - margin),
        max(media_box.y0, content_bbox.y0 - margin),
        min(media_box.x1, content_bbox.x1 + margin),
        min(media_box.y1, content_bbox.y1 + margin)
    )
    return padded & media_box


# ---------------------------------------------------------------------------
# Smart gap detection
# ---------------------------------------------------------------------------

def find_gap_splits(page: fitz.Page, n_cuts: int, axis: str,
                    clip: fitz.Rect, dpi: int = 36, threshold: int = 245) -> list:
    """
    Find n_cuts natural split positions along the given axis.

    Primary method: vector bounding-box analysis (precise, no DPI dependency).
    Fallback: pixel profile scan.

    axis='x': vertical cuts (left/right splits)
    axis='y': horizontal cuts (top/bottom splits)

    Returns sorted list of positions in PDF points.
    """
    if n_cuts <= 0:
        return []

    # ------------------------------------------------------------------
    # Primary: vector / bbox approach
    # ------------------------------------------------------------------
    intervals = []
    edge_buffer = (clip.x1 - clip.x0 if axis == 'x' else clip.y1 - clip.y0) * 0.03

    def _add(r):
        if axis == 'x':
            if r.x0 < clip.x1 and r.x1 > clip.x0:
                intervals.append((max(r.x0, clip.x0), min(r.x1, clip.x1)))
        else:
            if r.y0 < clip.y1 and r.y1 > clip.y0:
                intervals.append((max(r.y0, clip.y0), min(r.y1, clip.y1)))

    for block in page.get_text("blocks"):
        _add(fitz.Rect(block[:4]))
    for d in page.get_drawings():
        r = d.get("rect")
        if r:
            _add(fitz.Rect(r))
    for img in page.get_image_info():
        _add(fitz.Rect(img["bbox"]))

    if intervals:
        origin = clip.x0 if axis == 'x' else clip.y0
        end    = clip.x1 if axis == 'x' else clip.y1

        # Merge overlapping intervals
        intervals.sort()
        merged = [list(intervals[0])]
        for s, e in intervals[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        # Find interior gaps between merged intervals
        gaps = []
        for i in range(len(merged) - 1):
            g_start = merged[i][1]
            g_end   = merged[i + 1][0]
            g_size  = g_end - g_start
            g_center = (g_start + g_end) / 2.0
            if g_size > 0 and origin + edge_buffer < g_center < end - edge_buffer:
                gaps.append((g_size, g_center))

        if gaps:
            gaps.sort(key=lambda g: g[0], reverse=True)
            selected = sorted(gaps[:n_cuts], key=lambda g: g[1])
            return [g[1] for g in selected]

    # ------------------------------------------------------------------
    # Fallback: pixel profile scan - first try pure white gaps,
    # then valley detection (minimum content density) as last resort.
    # ------------------------------------------------------------------
    print(" [pixel scan]", end="", flush=True)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, clip=clip)
    samples = pix.samples
    W, H = pix.width, pix.height
    scale = 72 / dpi
    edge_skip = max(4, int((W if axis == 'x' else H) * 0.05))

    if axis == 'x':
        # dark pixel count per column (density profile)
        density = [0] * W
        for i, val in enumerate(samples):
            if val < threshold:
                density[i % W] += 1
        size, origin = W, clip.x0
    else:
        density = [0] * H
        for i, val in enumerate(samples):
            if val < threshold:
                density[i // W] += 1
        size, origin = H, clip.y0

    # --- Pass 1: pure white gaps (density == 0) ---
    gaps = []
    in_gap = False
    gap_start = 0
    for i in range(size):
        if density[i] == 0 and not in_gap:
            in_gap, gap_start = True, i
        elif density[i] > 0 and in_gap:
            in_gap = False
            ctr = (gap_start + i) / 2.0
            if edge_skip < ctr < size - edge_skip:
                gaps.append((i - gap_start, ctr))
    if in_gap:
        ctr = (gap_start + size) / 2.0
        if edge_skip < ctr < size - edge_skip:
            gaps.append((size - gap_start, ctr))

    if gaps:
        gaps.sort(key=lambda g: g[0], reverse=True)
        selected = sorted(gaps[:n_cuts], key=lambda g: g[1])
        return [origin + g[1] * scale for g in selected]

    # --- Pass 2: valley detection - find columns with minimum content ---
    # Smooth the density profile to reduce noise
    print(" [valley]", end="", flush=True)
    window = max(3, size // 80)
    smoothed = []
    for i in range(size):
        s = max(0, i - window // 2)
        e = min(size, i + window // 2 + 1)
        smoothed.append(sum(density[s:e]) / (e - s))

    interior_start = edge_skip
    interior_end = size - edge_skip
    interior_len = interior_end - interior_start

    # Each cut k should sit near position (k+1)/(n_cuts+1) of the interior.
    # Search in a zone of ±(1 / (2*(n_cuts+1))) around that expected position
    # so that cuts don't steal each other's valleys.
    half_zone = max(10, interior_len // (2 * (n_cuts + 1)))

    splits = []
    for k in range(n_cuts):
        expected = int(interior_start + (k + 1) * interior_len / (n_cuts + 1))
        zone_s = max(interior_start, expected - half_zone)
        zone_e = min(interior_end, expected + half_zone)
        best_idx = min(range(zone_s, zone_e), key=lambda i: smoothed[i])
        splits.append(best_idx)

    return [origin + idx * scale for idx in splits]


# ---------------------------------------------------------------------------
# Split modes
# ---------------------------------------------------------------------------

def crop_pdf(input_path: str, output_path: str, margin: float = 10.0, dpi: int = 150):
    """Crop white margins only."""
    input_path = Path(input_path)
    output_path = Path(output_path)

    print(f"Opening: {input_path.name}")
    doc = fitz.open(str(input_path))
    print(f"Pages: {len(doc)}")

    for page_num in range(len(doc)):
        page = doc[page_num]
        orig = page.rect
        print(f"  Page {page_num + 1}: {orig.width:.0f} x {orig.height:.0f} pt", end="")
        cropped = get_content_rect(page, margin, dpi)
        page.set_cropbox(cropped)
        print(f"  ->  {cropped.width:.0f} x {cropped.height:.0f} pt")

    print(f"\nSaving: {output_path.name}")
    doc.save(str(output_path), garbage=4, deflate=True)
    doc.close()
    print(f"Done: {output_path}")


def split_grid(input_path: str, output_path: str, rows: int, cols: int,
               margin: float = 10.0, dpi: int = 150):
    """Fixed equal grid split."""
    input_path = Path(input_path)
    output_path = Path(output_path)

    print(f"Opening: {input_path.name}")
    src = fitz.open(str(input_path))
    out = fitz.open()
    print(f"Pages: {len(src)}  |  Grid: {rows}x{cols} = {rows * cols} cells per page")

    for page_num in range(len(src)):
        page = src[page_num]
        orig = page.rect
        content = get_content_rect(page, margin, dpi)
        cell_w = content.width / cols
        cell_h = content.height / rows

        print(f"\n  Page {page_num + 1}: {orig.width:.0f} x {orig.height:.0f} pt")
        print(f"    Content: {content.width:.0f} x {content.height:.0f} pt  |  Cell: {cell_w:.0f} x {cell_h:.0f} pt")

        for row in range(rows):
            for col in range(cols):
                clip = fitz.Rect(
                    content.x0 + col * cell_w,
                    content.y0 + row * cell_h,
                    content.x0 + (col + 1) * cell_w,
                    content.y0 + (row + 1) * cell_h,
                )
                new_page = out.new_page(width=cell_w, height=cell_h)
                new_page.show_pdf_page(new_page.rect, src, page_num, clip=clip)
                cell_num = page_num * rows * cols + row * cols + col + 1
                print(f"    ({row+1},{col+1}) -> page {cell_num}")

    total = rows * cols * len(src)
    print(f"\nSaving: {output_path.name}")
    out.save(str(output_path), garbage=4, deflate=True)
    src.close()
    out.close()
    print(f"Done: {output_path}  ({total} pages total)")


def split_auto_grid(input_path: str, output_path: str, rows: int, cols: int,
                    margin: float = 10.0, dpi: int = 150, scan_dpi: int = 36):
    """
    Content-aware smart split: finds natural whitespace gaps between drawings.
    rows/cols define HOW MANY sections, splits are placed at the largest gaps.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    print(f"Opening: {input_path.name}")
    src = fitz.open(str(input_path))
    out = fitz.open()
    print(f"Pages: {len(src)}  |  Smart split: {rows}x{cols} = {rows * cols} sections per page")

    for page_num in range(len(src)):
        page = src[page_num]
        orig = page.rect
        content = get_content_rect(page, margin, dpi)

        print(f"\n  Page {page_num + 1}: {orig.width:.0f} x {orig.height:.0f} pt")
        print(f"    Content area: {content.width:.0f} x {content.height:.0f} pt")
        print(f"    Scanning for gaps at {scan_dpi} DPI...", end="", flush=True)

        col_splits = find_gap_splits(page, cols - 1, 'x', content, dpi=scan_dpi)
        row_splits = find_gap_splits(page, rows - 1, 'y', content, dpi=scan_dpi)
        print(" done")

        if col_splits:
            print(f"    Col splits (x): {[f'{x:.0f}' for x in col_splits]} pt")
        if row_splits:
            print(f"    Row splits (y): {[f'{y:.0f}' for y in row_splits]} pt")

        x_bounds = [content.x0] + col_splits + [content.x1]
        y_bounds = [content.y0] + row_splits + [content.y1]

        for ri in range(rows):
            for ci in range(cols):
                clip = fitz.Rect(x_bounds[ci], y_bounds[ri],
                                 x_bounds[ci + 1], y_bounds[ri + 1])
                w, h = clip.width, clip.height
                new_page = out.new_page(width=w, height=h)
                new_page.show_pdf_page(new_page.rect, src, page_num, clip=clip)
                cell_num = page_num * rows * cols + ri * cols + ci + 1
                print(f"    ({ri+1},{ci+1}) [{w:.0f}x{h:.0f} pt] -> page {cell_num}")

    total = rows * cols * len(src)
    print(f"\nSaving: {output_path.name}")
    out.save(str(output_path), garbage=4, deflate=True)
    src.close()
    out.close()
    print(f"Done: {output_path}  ({total} pages total)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_grid(value: str) -> tuple:
    try:
        parts = value.lower().split("x")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        raise argparse.ArgumentTypeError(f"Must be RxC format, e.g. 3x3 (got: {value!r})")


def main():
    parser = argparse.ArgumentParser(
        description="Crop white margins from PDF. Optionally split into grid."
    )
    parser.add_argument("input", help="Input PDF file path")
    parser.add_argument("output", nargs="?", help="Output PDF file path")
    parser.add_argument("--margin", type=float, default=10.0,
                        help="Margin around content in points (default: 10)")
    parser.add_argument("--dpi", type=int, default=150,
                        help="DPI for content detection fallback (default: 150)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--grid", metavar="RxC",
                      help="Fixed equal grid, e.g. --grid 3x3")
    mode.add_argument("--auto-grid", metavar="RxC",
                      help="Smart grid using gap detection, e.g. --auto-grid 1x2")

    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: File not found: {input_path}")
        sys.exit(1)

    if args.grid:
        rows, cols = parse_grid(args.grid)
        suffix = f"_grid{rows}x{cols}"
        output_path = Path(args.output) if args.output else \
            input_path.with_name(input_path.stem + suffix + input_path.suffix)
        split_grid(str(input_path), str(output_path), rows, cols,
                   margin=args.margin, dpi=args.dpi)

    elif args.auto_grid:
        rows, cols = parse_grid(args.auto_grid)
        suffix = f"_auto{rows}x{cols}"
        output_path = Path(args.output) if args.output else \
            input_path.with_name(input_path.stem + suffix + input_path.suffix)
        split_auto_grid(str(input_path), str(output_path), rows, cols,
                        margin=args.margin, dpi=args.dpi)

    else:
        output_path = Path(args.output) if args.output else \
            input_path.with_name(input_path.stem + "_cropped" + input_path.suffix)
        crop_pdf(str(input_path), str(output_path), margin=args.margin, dpi=args.dpi)


if __name__ == "__main__":
    main()
