"""
dwf_reader.py - DWF (Design Web Format) parsing engine.

DWF files are ZIP archives containing XML metadata, W2D binary geometry,
and other resources. This module provides three-tier parsing:

  Tier 0 (stdlib)     - always available: metadata, sections, XML-based layers/text
  Tier 1 (aspose-cad) - optional commercial: full geometry + W2D layers/text
  Tier 2 (ODA+ezdxf)  - optional free: full geometry via DWF→DXF conversion

Usage:
    import dwf_reader
    info = dwf_reader.get_dwf_info("drawing.dwf")
    layers = dwf_reader.get_dwf_layers("drawing.dwf")

Configuration (env vars):
    ODA_CONVERTER_PATH  - path to ODAFileConverter.exe (enables Tier 2)
    ASPOSE_CAD_LICENSE  - path to Aspose license XML file (for Tier 1 full mode)
    DWF_TEMP_DIR        - directory for ODA temp files (default: system temp)
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

ODA_CONVERTER_PATH = os.environ.get("ODA_CONVERTER_PATH", "")
ASPOSE_CAD_LICENSE = os.environ.get("ASPOSE_CAD_LICENSE", "")
DWF_TEMP_DIR = os.environ.get("DWF_TEMP_DIR", tempfile.gettempdir())

# ---------------------------------------------------------------------------
# Capability detection (cached at import time)
# ---------------------------------------------------------------------------

ASPOSE_AVAILABLE: bool = importlib.util.find_spec("aspose") is not None and (
    importlib.util.find_spec("aspose.cad") is not None
)

ODA_AVAILABLE: bool = bool(ODA_CONVERTER_PATH) and Path(ODA_CONVERTER_PATH).is_file()


def detect_backend() -> dict:
    """Return dict describing available parsing backends."""
    return {
        "aspose": ASPOSE_AVAILABLE,
        "oda": ODA_AVAILABLE,
        "oda_path": ODA_CONVERTER_PATH if ODA_AVAILABLE else "",
        "ezdxf": importlib.util.find_spec("ezdxf") is not None,
    }


# ---------------------------------------------------------------------------
# XML namespace helpers
# ---------------------------------------------------------------------------

# Common DWF namespaces (varies by authoring tool - we scan all XML files)
_DWF_NS = {
    "dwf": "http://www.autodesk.com/dwf/",
    "dc":  "http://purl.org/dc/elements/1.1/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


def _parse_xml_safe(data: bytes) -> Optional[ET.Element]:
    """Parse XML bytes, return root or None on failure."""
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _strip_ns(tag: str) -> str:
    """Strip namespace URI from tag: '{uri}name' -> 'name'."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _find_text(root: ET.Element, *paths: str) -> Optional[str]:
    """Try multiple tag paths and return first non-empty text found."""
    for path in paths:
        el = root.find(path)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return None


# ---------------------------------------------------------------------------
# Tier 0: stdlib ZIP + XML parsing
# ---------------------------------------------------------------------------

def open_dwf_zip(filepath: str) -> zipfile.ZipFile:
    """
    Open and validate a DWF file as a ZIP archive.

    Raises FileNotFoundError if path does not exist.
    Raises ValueError if file is not a valid DWF ZIP.
    """
    p = Path(filepath)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    if not p.is_file():
        raise ValueError(f"Not a file: {filepath}")
    try:
        zf = zipfile.ZipFile(str(p), "r")
    except zipfile.BadZipFile:
        raise ValueError(f"Not a valid DWF (ZIP) file: {filepath}")
    return zf


def list_zip_contents(zf: zipfile.ZipFile) -> list:
    """
    List all entries in the DWF archive with type classification.

    Returns list of dicts:
        path, size_bytes, is_xml, is_w2d, is_image, is_rels
    """
    results = []
    for info in zf.infolist():
        name = info.filename.lower()
        results.append({
            "path": info.filename,
            "size_bytes": info.file_size,
            "is_xml": name.endswith(".xml"),
            "is_w2d": name.endswith(".w2d"),
            "is_image": any(name.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp")),
            "is_rels": name.endswith(".rels"),
        })
    return results


def parse_manifest(zf: zipfile.ZipFile) -> dict:
    """
    Parse the DWF manifest to find sections/pages.

    Tries multiple manifest locations since different DWF producers
    (AutoCAD, Revit, Navisworks) use slightly different internal paths.

    Returns:
        {
            "dwf_version": str,
            "sections": [{"name": str, "role": str, "descriptor": str, "resource_path": str}]
        }
    """
    sections = []
    dwf_version = "unknown"

    names = {info.filename for info in zf.infolist()}

    # Strategy 1: look for a .rels file that references a manifest
    rels_path = "_rels/.rels"
    if rels_path in names:
        try:
            root = _parse_xml_safe(zf.read(rels_path))
            if root is not None:
                for rel in root.iter():
                    if _strip_ns(rel.tag) == "Relationship":
                        target = rel.get("Target", "")
                        rel_type = rel.get("Type", "")
                        if "manifest" in rel_type.lower() or "manifest" in target.lower():
                            manifest_path = target.lstrip("/")
                            if manifest_path in names:
                                result = _parse_manifest_xml(zf, manifest_path)
                                sections.extend(result.get("sections", []))
                                dwf_version = result.get("dwf_version", dwf_version)
        except Exception:
            pass

    # Strategy 2: look for any file named *manifest*.xml
    if not sections:
        for name in names:
            if "manifest" in name.lower() and name.endswith(".xml"):
                try:
                    result = _parse_manifest_xml(zf, name)
                    sections.extend(result.get("sections", []))
                    dwf_version = result.get("dwf_version", dwf_version)
                    if sections:
                        break
                except Exception:
                    pass

    # Strategy 3: scan all XML files for section-like elements
    if not sections:
        sections = _scan_xml_for_sections(zf)

    return {"dwf_version": dwf_version, "sections": sections}


def _parse_manifest_xml(zf: zipfile.ZipFile, path: str) -> dict:
    """Parse a single manifest XML file for section entries."""
    sections = []
    dwf_version = "unknown"

    try:
        data = zf.read(path)
        root = _parse_xml_safe(data)
        if root is None:
            return {"dwf_version": dwf_version, "sections": sections}

        tag = _strip_ns(root.tag)

        # Check root tag for version hint
        version_attr = root.get("version") or root.get("dwf:version")
        if version_attr:
            dwf_version = version_attr

        # Look for Section elements (DWF manifest uses <dwf:Section ...>)
        for child in root.iter():
            ctag = _strip_ns(child.tag)
            if ctag == "Section":
                name = child.get("name") or ""
                title = child.get("title") or child.get("label") or name
                role = child.get("type") or child.get("role") or ctag
                # Find descriptor path from nested <Toc><Resource role="descriptor">
                descriptor = ""
                for res in child.iter():
                    if _strip_ns(res.tag) == "Resource":
                        if "descriptor" in (res.get("role") or "").lower():
                            descriptor = (res.get("href") or "").replace("\\", "/")
                            break
                if name or title or descriptor:
                    sections.append({
                        "name": name,
                        "title": title,
                        "role": role,
                        "descriptor": descriptor,
                        "resource_path": "",
                    })

    except Exception:
        pass

    return {"dwf_version": dwf_version, "sections": sections}


def _scan_xml_for_sections(zf: zipfile.ZipFile) -> list:
    """
    Fallback: scan all XML files and gather any section-like entries.
    Used when manifest parsing yields nothing.
    """
    sections = []
    for info in zf.infolist():
        if not info.filename.lower().endswith(".xml"):
            continue
        if "manifest" in info.filename.lower() or "_rels" in info.filename.lower():
            continue
        try:
            root = _parse_xml_safe(zf.read(info.filename))
            if root is None:
                continue
            for child in root.iter():
                ctag = _strip_ns(child.tag)
                if ctag in ("Section", "Page", "Sheet"):
                    name = child.get("name") or child.get("title") or ""
                    role = child.get("role") or ctag
                    if name:
                        sections.append({
                            "name": name,
                            "role": role,
                            "descriptor": "",
                            "resource_path": info.filename,
                        })
        except Exception:
            continue
    return sections


def parse_section_descriptor(zf: zipfile.ZipFile, descriptor_path: str) -> dict:
    """
    Parse a section descriptor XML for dimensions and resource list.

    Returns:
        {
            "label": str, "page_number": int|None,
            "width": float, "height": float, "units": str,
            "resources": [{"path": str, "role": str, "mime": str}]
        }
    """
    result = {
        "label": "",
        "page_number": None,
        "width": 0.0,
        "height": 0.0,
        "units": "inches",
        "resources": [],
    }

    if not descriptor_path:
        return result

    names = {info.filename for info in zf.infolist()}
    if descriptor_path not in names:
        return result

    try:
        root = _parse_xml_safe(zf.read(descriptor_path))
        if root is None:
            return result

        # Label / title
        result["label"] = (
            root.get("label") or root.get("title") or root.get("name") or
            _find_text(root, "label", "title", "name") or ""
        )

        # Page number
        pg = root.get("page") or root.get("pageNumber")
        if pg is not None:
            try:
                result["page_number"] = int(pg)
            except (ValueError, TypeError):
                pass

        # Dimensions - look for Paper first (DWF ePlot), then Page/Size/Dimensions
        # Note: "Page" is often the root element itself (no dimensions), so check
        # all candidates and prefer ones that actually carry width/height.
        for etag_target in ("Paper", "Size", "Dimensions", "Page"):
            for el in root.iter():
                etag = _strip_ns(el.tag)
                if etag == etag_target:
                    w = el.get("width") or el.get("w")
                    h = el.get("height") or el.get("h")
                    if w or h:
                        u = el.get("units") or el.get("unit", "inches")
                        if w:
                            try:
                                result["width"] = float(w)
                            except (ValueError, TypeError):
                                pass
                        if h:
                            try:
                                result["height"] = float(h)
                            except (ValueError, TypeError):
                                pass
                        result["units"] = u
                        break
            if result["width"] or result["height"]:
                break

        # Resources
        for el in root.iter():
            etag = _strip_ns(el.tag)
            if etag == "Resource":
                path = el.get("href") or el.get("path") or ""
                role = el.get("role") or el.get("type") or ""
                mime = el.get("mime") or el.get("type") or ""
                if path:
                    result["resources"].append({
                        "path": path, "role": role, "mime": mime
                    })

    except Exception:
        pass

    return result


def extract_xml_properties(zf: zipfile.ZipFile) -> dict:
    """
    Scan all XML files for Dublin Core and DWF metadata properties.

    Returns:
        {"title": str, "author": str, "date": str, "software": str, "raw": dict}
    """
    meta = {"title": "", "author": "", "date": "", "software": ""}
    raw = {}

    dc_tags = {
        "title": ("title", "layout name"),
        "author": ("creator", "author"),
        "date": ("date", "created", "modified", "creation time", "modification time"),
        "software": ("software", "generator", "producer", "creator"),
    }

    for info in zf.infolist():
        if not info.filename.lower().endswith(".xml"):
            continue
        try:
            root = _parse_xml_safe(zf.read(info.filename))
            if root is None:
                continue
            for el in root.iter():
                tag = _strip_ns(el.tag).lower()

                # DWF stores metadata as <Property name="Author" value="User"/>
                if tag == "property":
                    prop_name = (el.get("name") or "").lower()
                    prop_val = (el.get("value") or el.text or "").strip()
                    if prop_name and prop_val:
                        raw[prop_name] = prop_val
                        for key, aliases in dc_tags.items():
                            if prop_name in aliases and not meta[key]:
                                meta[key] = prop_val
                    continue

                text = (el.text or "").strip()
                if not text:
                    continue
                raw[tag] = text
                for key, aliases in dc_tags.items():
                    if tag in aliases and not meta[key]:
                        meta[key] = text
        except Exception:
            continue

    meta["raw"] = raw
    return meta


def extract_xml_layers(zf: zipfile.ZipFile) -> list:
    """
    Extract layer definitions from XML resources.

    Different DWF producers store layers differently:
    - AutoCAD: <Layer name="..." visible="true"/>
    - Revit: layer info embedded in object properties XML

    Returns list of dicts: {name, visible, source}
    """
    layers = {}  # name -> dict, deduplication by name

    for info in zf.infolist():
        fname = info.filename.lower()
        if not fname.endswith(".xml"):
            continue
        try:
            root = _parse_xml_safe(zf.read(info.filename))
            if root is None:
                continue
            for el in root.iter():
                etag = _strip_ns(el.tag).lower()
                if etag in ("layer", "layers", "layerinfo", "layerdefinition"):
                    name = el.get("name") or el.get("label") or el.get("id") or ""
                    if not name:
                        continue
                    vis_raw = el.get("visible") or el.get("on") or el.get("show")
                    if vis_raw is not None:
                        visible = vis_raw.lower() not in ("false", "0", "no", "off")
                    else:
                        visible = None
                    if name not in layers:
                        layers[name] = {
                            "name": name,
                            "visible": visible,
                            "color": None,
                            "source": "xml",
                        }
        except Exception:
            continue

    return list(layers.values())


def extract_xml_text(zf: zipfile.ZipFile, section_path: Optional[str] = None) -> list:
    """
    Extract text content from XML resources within the DWF.

    Searches for text-like XML elements: Text, Label, String, RichText, etc.

    Args:
        section_path: if given, only search XML files under that path prefix

    Returns list of dicts: {text, x, y, source_file}
    """
    items = []
    seen = set()  # avoid duplicates

    for info in zf.infolist():
        fname = info.filename
        if not fname.lower().endswith(".xml"):
            continue
        if section_path and not fname.startswith(section_path):
            continue
        try:
            root = _parse_xml_safe(zf.read(fname))
            if root is None:
                continue
            for el in root.iter():
                etag = _strip_ns(el.tag).lower()
                if etag in ("text", "label", "string", "richtext", "textstring",
                            "title", "annotation", "caption"):
                    text = (el.text or "").strip()
                    if not text or len(text) < 1:
                        continue
                    if text in seen:
                        continue
                    seen.add(text)
                    x = _float_attr(el, "x", "left", "posx")
                    y = _float_attr(el, "y", "top", "posy")
                    items.append({
                        "text": text,
                        "x": x,
                        "y": y,
                        "source_file": fname,
                    })
        except Exception:
            continue

    return items


def _float_attr(el: ET.Element, *attr_names: str) -> Optional[float]:
    """Read first matching attribute as float, return None if not found."""
    for name in attr_names:
        val = el.get(name)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Tier 1: aspose-cad (optional, commercial)
# ---------------------------------------------------------------------------

def read_with_aspose(filepath: str) -> dict:
    """
    Use aspose.cad to load a DWF file and extract all data.

    Returns:
        {"layers": [...], "text_entities": [...], "geometry": {...}, "metadata": {...}}

    Raises ImportError if aspose.cad is not available.
    """
    if not ASPOSE_AVAILABLE:
        raise ImportError("aspose-cad is not installed (pip install aspose-cad)")

    import aspose.cad as cad  # type: ignore

    # Apply license if configured
    if ASPOSE_CAD_LICENSE and Path(ASPOSE_CAD_LICENSE).is_file():
        try:
            license_obj = cad.License()
            license_obj.set_license(ASPOSE_CAD_LICENSE)
        except Exception:
            pass

    result = {
        "layers": [],
        "text_entities": [],
        "geometry": _empty_geometry(),
        "metadata": {"title": "", "author": "", "date": "", "software": ""},
    }

    try:
        image = cad.Image.load(filepath)

        # Layers
        if hasattr(image, "layers"):
            for layer in image.layers.get_layers_names():
                layer_info = image.layers[layer]
                result["layers"].append({
                    "name": str(layer),
                    "visible": getattr(layer_info, "is_visible", True),
                    "color": getattr(layer_info, "color", None),
                    "source": "aspose",
                })

        # Entities
        geo = result["geometry"]
        for entity in _iter_aspose_entities(image):
            etype = type(entity).__name__.lower()
            text_val = None

            if "text" in etype or "mtext" in etype:
                text_val = getattr(entity, "text", None) or getattr(entity, "value", None)
            elif "line" in etype and "poly" not in etype:
                geo["lines"] += 1
            elif "arc" in etype:
                geo["arcs"] += 1
            elif "circle" in etype:
                geo["circles"] += 1
            elif "lwpolyline" in etype:
                geo["lwpolylines"] += 1
            elif "polyline" in etype:
                geo["polylines"] += 1
            elif "spline" in etype:
                geo["splines"] += 1
            elif "insert" in etype:
                geo["inserts"] += 1
            elif "hatch" in etype:
                geo["hatches"] += 1
            elif "dimension" in etype:
                geo["dimensions"] += 1
            elif "ellipse" in etype:
                geo["ellipses"] += 1
            elif "point" in etype:
                geo["points"] += 1
            else:
                geo["other"] += 1

            if text_val:
                result["text_entities"].append({
                    "text": str(text_val).strip(),
                    "x": None,
                    "y": None,
                    "layer": getattr(entity, "layer", None),
                    "font": None,
                    "height": None,
                    "source": "aspose",
                })

        geo["total_entities"] = sum(
            geo[k] for k in ("lines", "arcs", "circles", "polylines",
                             "lwpolylines", "splines", "inserts", "hatches",
                             "dimensions", "ellipses", "points", "other")
        )

    except Exception as e:
        result["error"] = str(e)

    return result


def _iter_aspose_entities(image) -> list:
    """Yield all entities from an aspose image, handling layered structures."""
    try:
        for entity in image.entities:
            yield entity
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tier 2: ODA File Converter + ezdxf
# ---------------------------------------------------------------------------

def convert_dwf_to_dxf(dwf_path: str, output_dir: str, oda_exe: str) -> str:
    """
    Convert a DWF file to DXF using ODA File Converter.

    ODA CLI: ODAFileConverter.exe <in_dir> <out_dir> ACAD2018 DXF 0 1 *.DWF

    Args:
        dwf_path: absolute path to the .dwf file
        output_dir: directory to write the .dxf output
        oda_exe: path to ODAFileConverter.exe

    Returns path to the resulting .dxf file.
    Raises RuntimeError on conversion failure or timeout.
    """
    import shutil

    dwf_path = str(Path(dwf_path).resolve())
    dwf_name = Path(dwf_path).name

    # ODA requires a dedicated input directory (converts everything in it)
    with tempfile.TemporaryDirectory(dir=DWF_TEMP_DIR) as in_dir:
        # Copy the DWF into the temp input dir
        shutil.copy2(dwf_path, Path(in_dir) / dwf_name)

        cmd = [oda_exe, in_dir, output_dir, "ACAD2018", "DXF", "0", "1", "*.DWF"]

        # Suppress window on Windows
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
                startupinfo=startupinfo,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("ODA File Converter timed out after 30 seconds")
        except FileNotFoundError:
            raise RuntimeError(f"ODA File Converter not found: {oda_exe}")

        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")
            raise RuntimeError(f"ODA converter failed (rc={proc.returncode}): {stderr}")

    # Find the output DXF
    stem = Path(dwf_path).stem
    expected = Path(output_dir) / (stem + ".dxf")
    if expected.exists():
        return str(expected)

    # Search for any .dxf in output dir
    dxfs = list(Path(output_dir).glob("*.dxf"))
    if dxfs:
        return str(dxfs[0])

    raise RuntimeError(f"ODA conversion completed but no .dxf found in: {output_dir}")


def read_dxf_with_ezdxf(dxf_path: str) -> dict:
    """
    Read a DXF file with ezdxf and extract layers, text, and geometry summary.

    Returns:
        {
            "layers": [{name, visible, color}],
            "text_entities": [{text, x, y, layer, type}],
            "geometry": {...entity counts + extents},
            "metadata": {title, author, units}
        }
    """
    import ezdxf  # type: ignore
    from ezdxf.enums import TextEntityAlignment  # noqa: F401

    result = {
        "layers": [],
        "text_entities": [],
        "geometry": _empty_geometry(),
        "metadata": {"title": "", "author": "", "units": ""},
    }

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as e:
        result["error"] = str(e)
        return result

    # Layers
    for layer in doc.layers:
        result["layers"].append({
            "name": layer.dxf.name,
            "visible": layer.is_on(),
            "color": layer.dxf.color if layer.dxf.hasattr("color") else None,
            "source": "dxf",
        })

    # Header metadata
    header = doc.header
    result["metadata"]["units"] = str(header.get("$INSUNITS", ""))

    # Entities (modelspace)
    geo = result["geometry"]
    msp = doc.modelspace()

    type_map = {
        "LINE": "lines",
        "ARC": "arcs",
        "CIRCLE": "circles",
        "POLYLINE": "polylines",
        "LWPOLYLINE": "lwpolylines",
        "SPLINE": "splines",
        "INSERT": "inserts",
        "HATCH": "hatches",
        "DIMENSION": "dimensions",
        "ELLIPSE": "ellipses",
        "POINT": "points",
    }

    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for entity in msp:
        etype = entity.dxftype()
        counter_key = type_map.get(etype, "other")
        geo[counter_key] += 1

        # Text extraction
        if etype in ("TEXT", "ATTRIB"):
            text = entity.dxf.get("text", "").strip()
            if text:
                pos = entity.dxf.get("insert", None)
                x = float(pos.x) if pos else None
                y = float(pos.y) if pos else None
                result["text_entities"].append({
                    "text": text,
                    "x": x,
                    "y": y,
                    "layer": entity.dxf.layer,
                    "type": etype,
                    "font": None,
                    "height": entity.dxf.get("height", None),
                    "source": "dxf",
                })
                if x is not None:
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                if y is not None:
                    min_y = min(min_y, y)
                    max_y = max(max_y, y)

        elif etype == "MTEXT":
            text = entity.plain_mtext().strip() if hasattr(entity, "plain_mtext") else ""
            if not text:
                text = entity.dxf.get("text", "").strip()
            if text:
                pos = entity.dxf.get("insert", None)
                x = float(pos.x) if pos else None
                y = float(pos.y) if pos else None
                result["text_entities"].append({
                    "text": text,
                    "x": x,
                    "y": y,
                    "layer": entity.dxf.layer,
                    "type": etype,
                    "font": None,
                    "height": entity.dxf.get("char_height", None),
                    "source": "dxf",
                })

        # Extents tracking for lines
        elif etype == "LINE":
            try:
                start = entity.dxf.start
                end = entity.dxf.end
                for pt in (start, end):
                    min_x = min(min_x, pt.x)
                    max_x = max(max_x, pt.x)
                    min_y = min(min_y, pt.y)
                    max_y = max(max_y, pt.y)
            except Exception:
                pass

    geo["total_entities"] = sum(
        geo[k] for k in ("lines", "arcs", "circles", "polylines", "lwpolylines",
                         "splines", "inserts", "hatches", "dimensions",
                         "ellipses", "points", "other")
    )

    geo["extents"] = {
        "min_x": min_x if min_x != float("inf") else None,
        "min_y": min_y if min_y != float("inf") else None,
        "max_x": max_x if max_x != float("-inf") else None,
        "max_y": max_y if max_y != float("-inf") else None,
    }

    return result


def _empty_geometry() -> dict:
    """Return zero-filled geometry summary dict."""
    return {
        "lines": 0, "arcs": 0, "circles": 0,
        "polylines": 0, "lwpolylines": 0, "splines": 0,
        "inserts": 0, "hatches": 0, "dimensions": 0,
        "ellipses": 0, "points": 0, "other": 0,
        "total_entities": 0,
        "extents": {"min_x": None, "min_y": None, "max_x": None, "max_y": None},
    }


# ---------------------------------------------------------------------------
# High-level API (called by MCP tools)
# ---------------------------------------------------------------------------

def get_dwf_info(filepath: str) -> dict:
    """
    Get comprehensive information about a DWF file.

    Always uses stdlib tier. Returns metadata, sections, and archive stats.
    """
    zf = open_dwf_zip(filepath)
    try:
        contents = list_zip_contents(zf)
        manifest = parse_manifest(zf)
        props = extract_xml_properties(zf)

        # Enrich sections with descriptor data
        enriched_sections = []
        for i, sec in enumerate(manifest["sections"]):
            desc = {}
            if sec.get("descriptor"):
                desc = parse_section_descriptor(zf, sec["descriptor"])
            enriched_sections.append({
                "index": i,
                "name": sec.get("name", ""),
                "role": sec.get("role", ""),
                "label": desc.get("label") or sec.get("title") or sec.get("name", ""),
                "page_number": desc.get("page_number"),
                "width": desc.get("width", 0.0),
                "height": desc.get("height", 0.0),
                "units": desc.get("units", ""),
            })

        p = Path(filepath)
        return {
            "filepath": str(p.resolve()),
            "file_size_bytes": p.stat().st_size,
            "dwf_version": manifest["dwf_version"],
            "page_count": len(enriched_sections),
            "sections": enriched_sections,
            "metadata": {
                "title": props.get("title", ""),
                "author": props.get("author", ""),
                "date": props.get("date", ""),
                "software": props.get("software", ""),
            },
            "archive_contents": {
                "total_files": len(contents),
                "xml_files": sum(1 for c in contents if c["is_xml"]),
                "w2d_files": sum(1 for c in contents if c["is_w2d"]),
                "image_files": sum(1 for c in contents if c["is_image"]),
                "other_files": sum(1 for c in contents if not any(
                    [c["is_xml"], c["is_w2d"], c["is_image"], c["is_rels"]]
                )),
            },
            "backend": "stdlib",
            "capabilities": detect_backend(),
        }
    finally:
        zf.close()


def get_dwf_layers(filepath: str) -> dict:
    """
    List all layers defined in the DWF file.

    Tries ODA+ezdxf then aspose then stdlib XML, in order of richness.
    """
    backend = "stdlib"
    note = None
    layers = []

    # Tier 2: ODA + ezdxf
    if ODA_AVAILABLE:
        try:
            with tempfile.TemporaryDirectory(dir=DWF_TEMP_DIR) as out_dir:
                dxf_path = convert_dwf_to_dxf(filepath, out_dir, ODA_CONVERTER_PATH)
                data = read_dxf_with_ezdxf(dxf_path)
                layers = data["layers"]
                backend = "oda+ezdxf"
        except Exception as e:
            note = f"ODA conversion failed ({e}), fell back to stdlib"

    # Tier 1: aspose
    if not layers and ASPOSE_AVAILABLE:
        try:
            data = read_with_aspose(filepath)
            layers = data["layers"]
            backend = "aspose"
        except Exception as e:
            note = f"aspose-cad failed ({e}), fell back to stdlib"

    # Tier 0: stdlib XML
    if not layers:
        zf = open_dwf_zip(filepath)
        try:
            layers = extract_xml_layers(zf)
            backend = "stdlib"
            if not layers:
                note = (
                    "No layer definitions found in XML resources. "
                    "For full layer data, install ODA File Converter or aspose-cad."
                )
        finally:
            zf.close()

    return {
        "layers": layers,
        "layer_count": len(layers),
        "backend": backend,
        "note": note,
    }


def get_dwf_text(filepath: str, section_index: Optional[int] = None) -> dict:
    """
    Extract all text content from a DWF file.

    Args:
        section_index: if given, limit extraction to that section (0-based)
    """
    backend = "stdlib"
    note = None
    text_items = []

    # Determine section path filter for stdlib
    section_path = None
    if section_index is not None:
        zf = open_dwf_zip(filepath)
        try:
            manifest = parse_manifest(zf)
            secs = manifest["sections"]
            if section_index < 0 or section_index >= len(secs):
                zf.close()
                raise ValueError(
                    f"section_index {section_index} out of range "
                    f"(0..{len(secs)-1})"
                )
            section_path = secs[section_index].get("resource_path", "")
        finally:
            zf.close()

    # Tier 2: ODA + ezdxf
    if ODA_AVAILABLE:
        try:
            with tempfile.TemporaryDirectory(dir=DWF_TEMP_DIR) as out_dir:
                dxf_path = convert_dwf_to_dxf(filepath, out_dir, ODA_CONVERTER_PATH)
                data = read_dxf_with_ezdxf(dxf_path)
                text_items = data["text_entities"]
                backend = "oda+ezdxf"
        except Exception as e:
            note = f"ODA conversion failed ({e}), fell back to stdlib"

    # Tier 1: aspose
    if not text_items and ASPOSE_AVAILABLE:
        try:
            data = read_with_aspose(filepath)
            text_items = data["text_entities"]
            backend = "aspose"
        except Exception as e:
            note = f"aspose-cad failed ({e}), fell back to stdlib"

    # Tier 0: stdlib XML
    if not text_items:
        zf = open_dwf_zip(filepath)
        try:
            text_items = extract_xml_text(zf, section_path)
            backend = "stdlib"
            if not text_items:
                note = (
                    "No text found in XML resources. "
                    "Binary W2D text requires ODA File Converter or aspose-cad."
                )
        finally:
            zf.close()

    # Add section_index field to each item if not present
    for item in text_items:
        if "section_index" not in item:
            item["section_index"] = section_index

    return {
        "text_items": text_items,
        "text_count": len(text_items),
        "backend": backend,
        "note": note,
    }


def get_dwf_geometry(filepath: str, section_index: Optional[int] = None) -> dict:
    """
    Extract geometry entity counts and extents from a DWF file.

    W2D binary geometry requires Tier 1 or Tier 2; stdlib returns zeros with a note.
    """
    backend = "stdlib"
    note = None
    geo = _empty_geometry()

    # Tier 2: ODA + ezdxf
    if ODA_AVAILABLE:
        try:
            with tempfile.TemporaryDirectory(dir=DWF_TEMP_DIR) as out_dir:
                dxf_path = convert_dwf_to_dxf(filepath, out_dir, ODA_CONVERTER_PATH)
                data = read_dxf_with_ezdxf(dxf_path)
                geo = data["geometry"]
                backend = "oda+ezdxf"
        except Exception as e:
            note = f"ODA conversion failed ({e}), geometry unavailable"

    # Tier 1: aspose
    elif ASPOSE_AVAILABLE:
        try:
            data = read_with_aspose(filepath)
            geo = data["geometry"]
            backend = "aspose"
        except Exception as e:
            note = f"aspose-cad failed ({e}), geometry unavailable"

    # Tier 0: stdlib - cannot parse W2D binary
    else:
        note = (
            "DWF geometry is stored in W2D binary format which cannot be parsed "
            "without additional tools. Install ODA File Converter (free) and set "
            "ODA_CONVERTER_PATH, or install aspose-cad."
        )

    return {
        "geometry_summary": geo,
        "backend": backend,
        "note": note,
    }


def get_dwf_page(filepath: str, section_index: int) -> dict:
    """
    Get comprehensive information about a specific DWF section/page.

    Args:
        section_index: 0-based index from get_dwf_info sections list

    Raises ValueError if section_index is out of range.
    """
    zf = open_dwf_zip(filepath)
    try:
        manifest = parse_manifest(zf)
        secs = manifest["sections"]
        if section_index < 0 or section_index >= len(secs):
            raise ValueError(
                f"section_index {section_index} out of range (0..{len(secs)-1})"
            )
        sec = secs[section_index]
        desc = parse_section_descriptor(zf, sec.get("descriptor", ""))

        # Enrich resources with size info
        names_size = {info.filename: info.file_size for info in zf.infolist()}
        for res in desc["resources"]:
            res["size_bytes"] = names_size.get(res["path"], 0)
    finally:
        zf.close()

    # Gather layers, text, geometry filtered to this section
    layers_result = get_dwf_layers(filepath)
    text_result = get_dwf_text(filepath, section_index)
    geo_result = get_dwf_geometry(filepath, section_index)

    return {
        "section_index": section_index,
        "name": sec.get("name", ""),
        "label": desc.get("label", sec.get("name", "")),
        "role": sec.get("role", ""),
        "page_number": desc.get("page_number"),
        "dimensions": {
            "width": desc.get("width", 0.0),
            "height": desc.get("height", 0.0),
            "units": desc.get("units", ""),
        },
        "resources": desc.get("resources", []),
        "layers": layers_result["layers"],
        "text_items": text_result["text_items"],
        "geometry_summary": geo_result["geometry_summary"],
        "backend": geo_result["backend"],
    }
