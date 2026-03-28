"""
dwf_mcp_server.py - MCP server for reading DWF (Design Web Format) files.

Exposes six tools to Claude:
  read_dwf_info      - metadata, page count, sections
  list_dwf_layers    - layer names and visibility
  extract_dwf_text   - text entities from the drawing
  extract_dwf_geometry - geometry summary (entity counts, extents)
  read_dwf_page      - full details for one specific page/section
  read_pdf_text      - extract text from a PDF (direct + embedded-image OCR)

Run this server:
    python dwf_mcp_server.py

Configure in Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "dwf-reader": {
          "command": "python",
          "args": ["C:/path/to/dwf_mcp_server.py"],
          "env": {
            "ODA_CONVERTER_PATH": "C:/Program Files/ODA/ODAFileConverter.exe"
          }
        }
      }
    }

Optional environment variables (see dwf_reader.py for details):
    ODA_CONVERTER_PATH  - enables geometry extraction via ODA File Converter
    ASPOSE_CAD_LICENSE  - path to Aspose.CAD license XML
    DWF_TEMP_DIR        - temp directory for ODA conversion output
"""

from mcp.server.fastmcp import FastMCP
import dwf_reader

# ---------------------------------------------------------------------------
# MCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "dwf-reader",
    instructions=(
        "Reads Autodesk DWF (Design Web Format) files and extracts metadata, "
        "layers, text, and geometry. DWF files are typically building drawings, "
        "architectural plans, or engineering schematics. "
        "Use read_dwf_info first to understand the file structure, then use the "
        "other tools to extract specific data."
    ),
)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@mcp.tool()
def read_dwf_info(filepath: str) -> dict:
    """
    Read general information about a DWF file.

    Returns the DWF version, page/section count, dimensions of each section,
    file metadata (title, author, date, authoring software), and a summary
    of the archive contents (XML files, W2D binary files, images).

    Also reports which parsing backends are available (stdlib / ODA / aspose-cad),
    so you know which other tools will return full vs. partial data.

    Args:
        filepath: Path to the .dwf file (absolute or relative to the working directory).
    """
    try:
        return dwf_reader.get_dwf_info(filepath)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except ValueError as e:
        return {"error": "INVALID_DWF", "message": str(e)}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


@mcp.tool()
def list_dwf_layers(filepath: str) -> dict:
    """
    List all layer definitions in a DWF file.

    Returns each layer's name, visibility state, and color.
    The 'backend' field in the response indicates the data source:
      - 'oda+ezdxf': full layer data via ODA File Converter (requires ODA_CONVERTER_PATH)
      - 'aspose': full layer data via aspose-cad (requires pip install aspose-cad)
      - 'stdlib': partial data from XML resources only (layer names, visibility may be null)

    If no layers are found, a 'note' field will explain what additional tooling
    is needed to extract layer information.

    Args:
        filepath: Path to the .dwf file.
    """
    try:
        return dwf_reader.get_dwf_layers(filepath)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except ValueError as e:
        return {"error": "INVALID_DWF", "message": str(e)}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


@mcp.tool()
def extract_dwf_text(filepath: str, section_index: int = -1) -> dict:
    """
    Extract all text content from a DWF file.

    Returns a list of text items, each with the text string and (when available)
    its position (x, y), layer name, font, and text height.

    The 'backend' field indicates what tier provided the data:
      - 'oda+ezdxf' or 'aspose': full text from drawing entities (TEXT, MTEXT)
      - 'stdlib': text found in XML resources only (may miss drawing-embedded text)

    Args:
        filepath: Path to the .dwf file.
        section_index: 0-based index of the section to read (-1 = all sections).
                       Use read_dwf_info to see available sections and their indices.
    """
    idx = None if section_index == -1 else section_index
    try:
        return dwf_reader.get_dwf_text(filepath, idx)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except ValueError as e:
        return {"error": "INVALID_SECTION" if "section_index" in str(e) else "INVALID_DWF",
                "message": str(e)}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


@mcp.tool()
def extract_dwf_geometry(filepath: str, section_index: int = -1) -> dict:
    """
    Extract a geometry summary from a DWF file.

    Returns counts of each entity type (lines, arcs, circles, polylines,
    splines, hatches, dimensions, etc.), total entity count, and the
    drawing extents (bounding box in drawing units).

    IMPORTANT: DWF geometry is stored in W2D binary format. The stdlib
    fallback CANNOT parse W2D and will return zero counts with a 'note'
    explaining what tools are needed. For real geometry data you need either:
      - ODA File Converter (free): set ODA_CONVERTER_PATH env var
      - aspose-cad (commercial): pip install aspose-cad

    Args:
        filepath: Path to the .dwf file.
        section_index: 0-based section index (-1 = all sections combined).
    """
    idx = None if section_index == -1 else section_index
    try:
        return dwf_reader.get_dwf_geometry(filepath, idx)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except ValueError as e:
        return {"error": "INVALID_SECTION" if "section_index" in str(e) else "INVALID_DWF",
                "message": str(e)}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


@mcp.tool()
def read_dwf_page(filepath: str, section_index: int) -> dict:
    """
    Read comprehensive information about one specific page/section in a DWF file.

    Combines section metadata, resource list, layers, text, and geometry
    summary for a single section into one response.

    Use read_dwf_info first to get the list of sections and their 0-based indices.

    Args:
        filepath: Path to the .dwf file.
        section_index: 0-based index of the section to read.
                       Must be in range [0, page_count - 1].
    """
    try:
        return dwf_reader.get_dwf_page(filepath, section_index)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except ValueError as e:
        return {"error": "INVALID_SECTION" if "section_index" in str(e) else "INVALID_DWF",
                "message": str(e)}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


@mcp.tool()
def read_pdf_text(filepath: str) -> dict:
    """
    Extract text from a PDF file.

    Uses two strategies in sequence and combines results:
      1. Direct text layer extraction (fast, zero-loss — works if PDF has embedded text)
      2. OCR on every embedded image inside the PDF (catches title blocks, stamps,
         labels, and architectural annotations that were rendered as raster images)

    This is especially useful for PDFs exported from DWF via Autodesk Design Review,
    which produce rasterized PDFs with no text layer but may still contain embedded
    image fragments (logos, title blocks, form labels) that are OCR-readable.

    Returns:
        text_items: list of {text, page, image_name, backend, ...}
        text_count: total items found
        backend: which method(s) provided data
        note: explanation if nothing was found

    Args:
        filepath: Path to the .pdf file.
    """
    try:
        return dwf_reader.get_pdf_text(filepath)
    except FileNotFoundError:
        return {"error": "FILE_NOT_FOUND", "message": f"File not found: {filepath}"}
    except PermissionError:
        return {"error": "PERMISSION_DENIED", "message": f"Cannot read: {filepath}"}
    except Exception as e:
        return {"error": "UNEXPECTED", "message": str(e), "type": type(e).__name__}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
