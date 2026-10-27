"""
extractors.py — Production-grade text extraction with OCR support.
VectorLake SDK v2.2.0 compatible.

Recommended stack:
- pymupdf (fitz) for PDFs — fast, reliable, handles both text and images
- python-docx for Word documents
- python-pptx for PowerPoint decks — used by PPTXExtractor to pull slide
  text, tables, and speaker notes (embedded images are OCR'd like DOCX)
- openpyxl for .xlsx workbooks — used by XLSXExtractor / flatten_workbook()
  to flatten multi-section sheets into self-contained rows (no OCR involved)
- pytesseract + Tesseract OCR for image text extraction
- Pillow for image processing

Each dependency above is imported defensively (try/except) so the module
loads without all of them installed; the corresponding extractor simply
raises ImportError with an install hint if you invoke it without its
dependency present. All four of PDFExtractor, DOCXExtractor, PPTXExtractor,
and XLSXExtractor are registered in ExtractorFactory and are dispatched to
automatically by utils.extract_text() / utils._extract_file_to_chunks()
based on file extension — you don't need to call them directly unless
you're extracting outside of the normal upload pipeline (see README:
"Using Extractors Directly").
"""

from __future__ import annotations 

import csv
import io
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pdfspine.fitz as fitz # PyMuPDF alt
except ImportError:
    fitz = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

try:
    from pptx import Presentation as PptxPresentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE
except ImportError:
    PptxPresentation = None
    MSO_SHAPE_TYPE = None

try:
    from PIL import Image
    import pytesseract
    # ── Windows: auto-locate Tesseract binary if not on PATH ──────────────────
    import platform, shutil
    if platform.system() == "Windows" and not shutil.which("tesseract"):
        import os
        _win_candidates = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expanduser(r"~\AppData\Local\Tesseract-OCR\tesseract.exe"),
        ]
        for _path in _win_candidates:
            if os.path.isfile(_path):
                pytesseract.pytesseract.tesseract_cmd = _path
                break
except ImportError:
    Image = None
    pytesseract = None


logger = logging.getLogger(__name__)

# ── Image-origin marker format ────────────────────────────────────────────────
# These wrap OCR-derived spans so downstream consumers know provenance.
# Format: <<<IMAGE_EXTRACT source="x" page=N image_index=N method="y">>>
#         ... OCR text ...
#         <<<END_IMAGE_EXTRACT>>>

_MARKER_OPEN_RE = re.compile(
    r'<<<IMAGE_EXTRACT\s+([^>]*)>>>',
    re.IGNORECASE,
)
_MARKER_CLOSE   = "<<<END_IMAGE_EXTRACT>>>"
_MARKER_BLOCK_RE = re.compile(
    r'<<<IMAGE_EXTRACT\s+[^>]*>>>.*?<<<END_IMAGE_EXTRACT>>>',
    re.DOTALL | re.IGNORECASE,
)


def _make_marker(source: str, page: int, image_index: int, method: str) -> str:
    return (
        f'<<<IMAGE_EXTRACT source="{source}" page={page} '
        f'image_index={image_index} method="{method}">>>'
    )


def strip_image_markers(text: str) -> str:
    """
    Remove IMAGE_EXTRACT open/close marker lines but keep the OCR text inside.

    Example
    -------
    Input:
        <<<IMAGE_EXTRACT source="report.pdf" page=2 image_index=1 method="embedded_image_ocr">>>
        Some OCR text here
        <<<END_IMAGE_EXTRACT>>>

    Output:
        Some OCR text here
    """
    # Remove open markers
    text = _MARKER_OPEN_RE.sub("", text)
    # Remove close markers
    text = text.replace(_MARKER_CLOSE, "")
    # Collapse excess blank lines left behind
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def list_image_regions(text: str) -> List[Dict]:
    """
    Return a list of dicts describing each IMAGE_EXTRACT block found in *text*.

    Each dict contains:
        source        (str)  — origin filename
        page          (int)  — 1-based page number
        image_index   (int)  — 1-based image index within that page
        method        (str)  — extraction method label
        ocr_text      (str)  — the OCR text inside the block
    """
    regions = []
    for block_match in _MARKER_BLOCK_RE.finditer(text):
        block = block_match.group(0)
        # Parse attributes from opening marker
        header_match = _MARKER_OPEN_RE.search(block)
        if not header_match:
            continue
        attrs_str = header_match.group(1)

        def _attr(name: str, default="") -> str:
            m = re.search(rf'{name}=["\']?([^"\'>\s]+)["\']?', attrs_str)
            return m.group(1) if m else default

        # Extract OCR text (everything between open and close markers)
        inner = _MARKER_OPEN_RE.sub("", block).replace(_MARKER_CLOSE, "").strip()

        regions.append({
            "source":      _attr("source"),
            "page":        int(_attr("page", "0")),
            "image_index": int(_attr("image_index", "0")),
            "method":      _attr("method"),
            "ocr_text":    inner,
        })
    return regions


# ── Core data model ───────────────────────────────────────────────────────────

@dataclass
class ExtractedContent:
    """Unified extraction result with provenance markers."""
    text: str
    images_text: List[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        """
        Combined text + OCR results.
        OCR spans are wrapped in IMAGE_EXTRACT markers so callers can
        use strip_image_markers() / list_image_regions() on the result.
        """
        parts = [self.text]
        parts.extend(self.images_text)   # already contain markers
        return "\n".join(p for p in parts if p)

    @property
    def image_region_count(self) -> int:
        """Number of OCR image regions extracted. Used by utils._log_image_summary()."""
        return len(self.images_text)


# ── Base extractor ────────────────────────────────────────────────────────────

class BaseExtractor(ABC):
    """Abstract base for all extractors."""

    def __init__(self, enable_ocr: bool = True, ocr_lang: str = "eng"):
        self.enable_ocr = enable_ocr
        self.ocr_lang   = ocr_lang

    @abstractmethod
    def extract(self, file_path: Path) -> ExtractedContent:
        pass

    def _ocr_image(self, image: "Image.Image") -> str:
        """Run Tesseract OCR on a PIL Image. Returns empty string on failure."""
        if not self.enable_ocr or not pytesseract or not Image:
            return ""
        try:
            gray = image.convert("L")
            return pytesseract.image_to_string(
                gray,
                lang=self.ocr_lang,
                config="--psm 6",
            ).strip()
        except Exception as exc:
            logger.warning("OCR failed: %s", exc)
            return ""


# ── PDF extractor ─────────────────────────────────────────────────────────────

class PDFExtractor(BaseExtractor):
    """
    Production PDF extraction using PyMuPDF.

    Strategy
    --------
    1. Extract native text from each page.
    2. Pages with fewer than *min_text_threshold* chars are treated as scanned;
       the entire page is rasterised and OCR'd.
    3. Embedded images on text-heavy pages are extracted and OCR'd separately.
    4. OCR spans are wrapped in IMAGE_EXTRACT markers for provenance tracking.
    """

    def __init__(
        self,
        enable_ocr: bool = True,
        ocr_lang: str = "eng",
        ocr_dpi: int = 300,
        min_text_threshold: int = 50,
    ):
        super().__init__(enable_ocr, ocr_lang)
        self.ocr_dpi             = ocr_dpi
        self.min_text_threshold  = min_text_threshold

    def extract(self, file_path: Path) -> ExtractedContent:
        if not fitz:
            raise ImportError("PyMuPDF not installed. Run: pip install pymupdf")

        doc          = fitz.open(str(file_path))
        text_parts   = []
        images_text  = []
        scanned_pages = []
        source_name  = file_path.name

        for page_num, page in enumerate(doc):
            page_text = page.get_text()

            if len(page_text.strip()) < self.min_text_threshold:
                # Scanned page — rasterise and OCR
                scanned_pages.append(page_num + 1)
                if self.enable_ocr:
                    ocr_text = self._ocr_page(page)
                    if ocr_text:
                        marker = _make_marker(
                            source      = source_name,
                            page        = page_num + 1,
                            image_index = 0,
                            method      = "full_page_ocr",
                        )
                        images_text.append(
                            f"{marker}\n{ocr_text}\n{_MARKER_CLOSE}"
                        )
            else:
                text_parts.append(page_text)

            # Extract and OCR embedded images on every page
            if self.enable_ocr:
                for img_text in self._extract_page_images(page, page_num + 1, source_name):
                    images_text.append(img_text)

        doc.close()

        return ExtractedContent(
            text        = "\n".join(text_parts),
            images_text = images_text,
            metadata    = {
                "page_count":    len(doc) if not doc.is_closed else "closed",
                "scanned_pages": scanned_pages,
                "has_images":    len(images_text) > 0,
            },
        )

    def _ocr_page(self, page: "fitz.Page") -> str:
        if not Image or not pytesseract:
            return ""
        try:
            pix       = page.get_pixmap(dpi=self.ocr_dpi)
            img_bytes = pix.tobytes("png")
            img       = Image.open(io.BytesIO(img_bytes))
            return self._ocr_image(img)
        except Exception as exc:
            logger.warning("Failed to OCR page: %s", exc)
            return ""

    def _extract_page_images(
        self, page: "fitz.Page", page_num: int, source_name: str
    ) -> List[str]:
        if not Image or not pytesseract:
            return []
        results = []
        for img_index, img_info in enumerate(page.get_images(full=True), 1):
            try:
                xref       = img_info[0]
                base_image = page.parent.extract_image(xref)
                img        = Image.open(io.BytesIO(base_image["image"]))
                text       = self._ocr_image(img)
                if text:
                    marker = _make_marker(
                        source      = source_name,
                        page        = page_num,
                        image_index = img_index,
                        method      = "embedded_image_ocr",
                    )
                    results.append(f"{marker}\n{text}\n{_MARKER_CLOSE}")
            except Exception as exc:
                logger.warning(
                    "Failed to extract image %d from page %d: %s",
                    img_index, page_num, exc,
                )
        return results


# ── DOCX extractor ────────────────────────────────────────────────────────────

class DOCXExtractor(BaseExtractor):
    """Extract text + embedded images from Word documents."""

    def extract(self, file_path: Path) -> ExtractedContent:
        if not DocxDocument:
            raise ImportError("python-docx not installed. Run: pip install python-docx")

        doc         = DocxDocument(str(file_path))
        text_parts  = []
        images_text = []
        source_name = file_path.name

        for para in doc.paragraphs:
            if para.text.strip():
                text_parts.append(para.text)

        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(c.text.strip() for c in row.cells)
                if row_text.strip():
                    text_parts.append(row_text)

        if self.enable_ocr and Image and pytesseract:
            for img_index, rel in enumerate(
                (r for r in doc.part.rels.values() if "image" in r.target_ref), 1
            ):
                try:
                    img  = Image.open(io.BytesIO(rel.target_part.blob))
                    text = self._ocr_image(img)
                    if text:
                        marker = _make_marker(
                            source      = source_name,
                            page        = 0,
                            image_index = img_index,
                            method      = "embedded_image_ocr",
                        )
                        images_text.append(f"{marker}\n{text}\n{_MARKER_CLOSE}")
                except Exception as exc:
                    logger.warning("Failed to OCR embedded image: %s", exc)

        return ExtractedContent(
            text        = "\n\n".join(text_parts),
            images_text = images_text,
            metadata    = {
                "paragraph_count": len(doc.paragraphs),
                "table_count":     len(doc.tables),
            },
        )


# ── PPTX extractor ────────────────────────────────────────────────────────────

class PPTXExtractor(BaseExtractor):
    """Extract text + embedded images from PowerPoint decks."""

    def extract(self, file_path: Path) -> ExtractedContent:
        if not PptxPresentation:
            raise ImportError("python-pptx not installed. Run: pip install python-pptx")

        prs          = PptxPresentation(str(file_path))
        text_parts   = []
        images_text  = []
        source_name  = file_path.name
        slide_count  = 0
        notes_count  = 0

        for slide_num, slide in enumerate(prs.slides, 1):
            slide_count += 1
            slide_parts = []

            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        para_text = "".join(run.text for run in para.runs) or para.text
                        if para_text and para_text.strip():
                            slide_parts.append(para_text)

                if shape.has_table:
                    for row in shape.table.rows:
                        row_text = " | ".join(c.text.strip() for c in row.cells)
                        if row_text.strip():
                            slide_parts.append(row_text)

                if (
                    self.enable_ocr and Image and pytesseract and MSO_SHAPE_TYPE
                    and shape.shape_type == MSO_SHAPE_TYPE.PICTURE
                ):
                    try:
                        img  = Image.open(io.BytesIO(shape.image.blob))
                        text = self._ocr_image(img)
                        if text:
                            marker = _make_marker(
                                source      = source_name,
                                page        = slide_num,
                                image_index = 1,
                                method      = "embedded_image_ocr",
                            )
                            images_text.append(f"{marker}\n{text}\n{_MARKER_CLOSE}")
                    except Exception as exc:
                        logger.warning(
                            "Failed to OCR embedded image on slide %d: %s",
                            slide_num, exc,
                        )

            if slide.has_notes_slide:
                notes_tf = slide.notes_slide.notes_text_frame
                if notes_tf and notes_tf.text.strip():
                    notes_count += 1
                    slide_parts.append(f"[Speaker notes] {notes_tf.text.strip()}")

            if slide_parts:
                text_parts.append("\n".join(slide_parts))

        return ExtractedContent(
            text        = "\n\n".join(text_parts),
            images_text = images_text,
            metadata    = {
                "slide_count": slide_count,
                "notes_count": notes_count,
                "has_images":  len(images_text) > 0,
            },
        )


# ── Standalone image extractor ────────────────────────────────────────────────

class ImageFileExtractor(BaseExtractor):
    """
    OCR a standalone image file (PNG, JPG, TIFF, BMP, WEBP, etc.).

    Required by utils.py:
        local = ImageFileExtractor(enable_ocr=enable_ocr)
        result = local.extract(p)
    """

    def extract(self, file_path: Path) -> ExtractedContent:
        if not Image or not pytesseract:
            from .exceptions import FileProcessingError
            raise FileProcessingError(
                "Image OCR requires Pillow + pytesseract.\n"
                "Run: pip install pillow pytesseract"
            )

        try:
            img  = Image.open(str(file_path))
            text = self._ocr_image(img)
        except Exception as exc:
            from .exceptions import FileProcessingError
            raise FileProcessingError(
                f"Could not OCR image '{file_path.name}': {exc}"
            ) from exc

        marker = _make_marker(
            source      = file_path.name,
            page        = 1,
            image_index = 1,
            method      = "standalone_image_ocr",
        )
        images_text = [f"{marker}\n{text}\n{_MARKER_CLOSE}"] if text else []

        return ExtractedContent(
            text        = text,
            images_text = images_text,
            metadata    = {
                "filename": file_path.name,
                "format":   getattr(img, "format", "unknown"),
                "size":     getattr(img, "size",   None),
            },
        )


# ── XLSX extractor ────────────────────────────────────────────────────────────
#
# Multi-section worksheets (a clean summary table stacked above a "form"-style
# block of side-by-side mini-tables, banner rows, group labels, etc.) don't
# flatten sanely with pandas.read_excel — blank-for-grouping cells and
# repeated headers need to be resolved first. This uses connected-component
# ("island") detection over the occupied-cell grid, plus cell style (fill +
# bold) to recover section banners and group labels, then emits ONE
# self-contained row per fact: sheet, section, group, row_label, column_label,
# value. Self-contained because each row may become an independent retrieval
# chunk downstream — it must carry its own context, not rely on a neighbor.
#
# XLSXExtractor supports two output modes (see class docstring below):
#   - simple_mode=True:  every island dumped as raw CSV, in reading order,
#     with NO banner/group/label inference. Use this if you just want
#     "everything, as text, comma separated, in the order you see it" and
#     don't want any risk of cells being dropped by the heuristics below.
#   - simple_mode=False (default): flatten_workbook()'s structured
#     sheet/section/group/row_label/column_label/value output.

def _xlsx_has_solid_fill(cell) -> bool:
    """Style-only check — ignores cell.value. A banner band fills many cells
    but usually only the leftmost one carries text."""
    fill = cell.fill
    return bool(
        fill and fill.patternType == "solid"
        and fill.fgColor and fill.fgColor.rgb not in (None, "00000000", "FFFFFFFF")
    )


def _xlsx_is_bold_only(cell) -> bool:
    return bool(cell.font and cell.font.bold) and not _xlsx_has_solid_fill(cell)


def _xlsx_dilate(mask, row_radius: int = 1, col_radius: int = 0):
    """
    Grow occupied regions to bridge cosmetic gaps — vertically by default
    (a blank spacer row inside one real table is common). Horizontal growth
    defaults to 0: a blank column reliably means a *separate* table block in
    this convention — even a narrow gap between two side-by-side mini-tables
    must NOT be bridged, or they'll be read as one table.
    """
    n_rows, n_cols = len(mask), len(mask[0])
    out = [row[:] for row in mask]

    for _ in range(row_radius):
        new = [row[:] for row in out]
        for r in range(n_rows):
            for c in range(n_cols):
                if out[r][c]:
                    continue
                if (r > 0 and out[r - 1][c]) or (r < n_rows - 1 and out[r + 1][c]):
                    new[r][c] = True
        out = new

    for _ in range(col_radius):
        new = [row[:] for row in out]
        for r in range(n_rows):
            for c in range(n_cols):
                if out[r][c]:
                    continue
                if (c > 0 and out[r][c - 1]) or (c < n_cols - 1 and out[r][c + 1]):
                    new[r][c] = True
        out = new

    return out


def _xlsx_find_islands(mask):
    from collections import deque
    n_rows, n_cols = len(mask), len(mask[0])
    seen = [[False] * n_cols for _ in range(n_rows)]
    islands = []
    for r in range(n_rows):
        for c in range(n_cols):
            if mask[r][c] and not seen[r][c]:
                q = deque([(r, c)])
                seen[r][c] = True
                cells = []
                while q:
                    cr, cc = q.popleft()
                    cells.append((cr, cc))
                    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                        nr, nc = cr + dr, cc + dc
                        if (0 <= nr < n_rows and 0 <= nc < n_cols
                                and mask[nr][nc] and not seen[nr][nc]):
                            seen[nr][nc] = True
                            q.append((nr, nc))
                rs = [x[0] for x in cells]
                cs = [x[1] for x in cells]
                islands.append((min(rs), max(rs), min(cs), max(cs)))
    return islands


def _xlsx_split_on_blank_rows(island, occ):
    """
    Split an island back apart at any row that's fully blank in the
    *undilated* occupancy grid (within that island's column span).
    Row dilation may have bridged a real blank separator row between two
    stacked tables — this undoes that so they don't get merged into one
    table with the wrong header applied to the second table's rows.
    """
    r0, r1, c0, c1 = island
    sub_islands = []
    run_start = None
    run_end = None
    for r in range(r0, r1 + 1):
        row_has_content = any(occ[r][c] for c in range(c0, c1 + 1))
        if row_has_content:
            if run_start is None:
                run_start = r
            run_end = r
        else:
            if run_start is not None:
                sub_islands.append((run_start, run_end, c0, c1))
                run_start = None
    if run_start is not None:
        sub_islands.append((run_start, run_end, c0, c1))
    return sub_islands if sub_islands else [island]


def extract_all_islands_as_csv(path, row_dilation: int = 1, col_dilation: int = 1):
    """
    Dump every table/island on every sheet, in reading order (top-to-bottom,
    then left-to-right among tables that start on the same row), as raw CSV
    blocks separated by blank lines.

    No banner/group/label inference is applied — cells are written exactly
    as they appear in the sheet grid. This avoids the failure mode in
    flatten_workbook() where banner-row / bold-row detection zeroes out
    cells it misjudges as headers or group tags, which can silently drop
    right-hand / secondary tables from the output.

    Returns a single string, e.g. for writing straight to a .txt file:
        Path("output.txt").write_text(extract_all_islands_as_csv(path))
    """
    if not openpyxl:
        raise ImportError("openpyxl not installed. Run: pip install openpyxl")

    wb = openpyxl.load_workbook(str(path), data_only=True)
    out_lines = []

    for ws in wb.worksheets:
        n_rows, n_cols = ws.max_row, ws.max_column
        if n_rows == 0 or n_cols == 0:
            continue

        occ = [[False] * n_cols for _ in range(n_rows)]
        for r in range(1, n_rows + 1):
            for c in range(1, n_cols + 1):
                if ws.cell(row=r, column=c).value not in (None, ""):
                    occ[r - 1][c - 1] = True

        dilated = _xlsx_dilate(occ, row_radius=row_dilation, col_radius=col_dilation)
        raw_islands = _xlsx_find_islands(dilated)

        islands = []
        for island in raw_islands:
            islands.extend(_xlsx_split_on_blank_rows(island, occ))

        # reading order: top row first, then left-to-right among ties
        islands = sorted(islands, key=lambda b: (b[0], b[2]))

        out_lines.append(f"=== Sheet: {ws.title} ===")

        for (r0, r1, c0, c1) in islands:
            real_cells = [
                (r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
                if occ[r][c]
            ]
            if not real_cells:
                continue
            tr0 = min(r for r, _ in real_cells)
            tr1 = max(r for r, _ in real_cells)
            tc0 = min(c for _, c in real_cells)
            tc1 = max(c for _, c in real_cells)

            buf = io.StringIO()
            writer = csv.writer(buf)
            for r in range(tr0, tr1 + 1):
                row_vals = [
                    ws.cell(row=r + 1, column=c + 1).value
                    if ws.cell(row=r + 1, column=c + 1).value is not None else ""
                    for c in range(tc0, tc1 + 1)
                ]
                writer.writerow(row_vals)

            out_lines.append(buf.getvalue().rstrip("\n"))
            out_lines.append("")  # blank line between islands

    return "\n".join(out_lines)


def flatten_workbook(path, row_dilation: int = 1, col_dilation: int = 1):
    """
    Flatten a multi-section .xlsx into (header, rows) for row-safe CSV
    chunking by utils._split_xlsx_safe().

    Returns header = ["sheet", "table", "row"] where each entry in *rows* is
    one physical spreadsheet row, exactly as it appears in the sheet grid:
        rows[i] == [sheet_name, table_index, row_as_csv_text]

    ``row_as_csv_text`` is the full row — every column of whichever table
    island it belongs to — pre-joined into one properly-quoted CSV string
    (commas/quotes/newlines inside cell values are escaped correctly), so no
    column data is lost and nothing needs to be reconstructed downstream.
    ``table`` is a 1-based index distinguishing separate table islands on
    the same sheet (in reading order: top-to-bottom, then left-to-right)
    in case a sheet has more than one.

    Internally this uses the same island-detection approach as
    extract_all_islands_as_csv(): connected-component ("island") detection
    over the occupied-cell grid, with row/column dilation to bridge cosmetic
    blank gaps (row_dilation bridges a blank spacer row inside one table;
    col_dilation bridges a blank spacer column, e.g. between two columns of
    the same logical table). No banner-fill / bold-row inference is applied
    — that heuristic previously caused whole tables (especially right-hand
    or colored-header ones) to be silently dropped or have their columns
    disconnected from their headers. Every occupied cell is preserved.
    """
    if not openpyxl:
        raise ImportError("openpyxl not installed. Run: pip install openpyxl")

    wb = openpyxl.load_workbook(str(path), data_only=True)
    header = ["sheet", "table", "row"]
    out_rows = []

    for ws in wb.worksheets:
        n_rows, n_cols = ws.max_row, ws.max_column
        if n_rows == 0 or n_cols == 0:
            continue

        occ = [[False] * n_cols for _ in range(n_rows)]
        for r in range(1, n_rows + 1):
            for c in range(1, n_cols + 1):
                if ws.cell(row=r, column=c).value not in (None, ""):
                    occ[r - 1][c - 1] = True

        dilated = _xlsx_dilate(occ, row_radius=row_dilation, col_radius=col_dilation)
        raw_islands = _xlsx_find_islands(dilated)

        islands = []
        for island in raw_islands:
            islands.extend(_xlsx_split_on_blank_rows(island, occ))

        islands = sorted(islands, key=lambda b: (b[0], b[2]))

        for table_idx, (r0, r1, c0, c1) in enumerate(islands, 1):
            real_cells = [
                (r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)
                if occ[r][c]
            ]
            if not real_cells:
                continue
            tr0 = min(r for r, _ in real_cells)
            tr1 = max(r for r, _ in real_cells)
            tc0 = min(c for _, c in real_cells)
            tc1 = max(c for _, c in real_cells)

            for r in range(tr0, tr1 + 1):
                row_vals = [
                    ws.cell(row=r + 1, column=c + 1).value
                    if ws.cell(row=r + 1, column=c + 1).value is not None else ""
                    for c in range(tc0, tc1 + 1)
                ]
                buf = io.StringIO()
                csv.writer(buf).writerow(row_vals)
                row_text = buf.getvalue().rstrip("\r\n")
                out_rows.append([ws.title, table_idx, row_text])

    return header, out_rows


class XLSXExtractor(BaseExtractor):
    """
    Two modes:
      - simple_mode=True  (DEFAULT): every island dumped as raw CSV, in
        reading order, with NO banner/group/label inference. Nothing gets
        silently dropped or mislabeled — see extract_all_islands_as_csv()
        for details. This is what ExtractorFactory / the normal upload
        pipeline now uses automatically, with no call-site changes needed.
      - simple_mode=False (opt-in): flatten_workbook()'s structured
        sheet/section/group/row_label/column_label/value output. See
        flatten_workbook() docstring for the detection heuristics and their
        known edge cases — this mode can silently drop or mis-align cells
        on sheets without clear colored banner rows (e.g. a plain header
        row like Functionality/Elements/Sub-Feature/... with no fill),
        which is why it's no longer the default.

    ExtractedContent.text is a standard CSV rendering — built with Python's
    csv module (via io.StringIO), so embedded commas, quotes, or newlines
    in any cell value are escaped correctly rather than corrupting column
    boundaries. Downstream consumers can parse it with any ordinary CSV
    reader (pandas.read_csv, csv.reader, etc.).
    """

    def __init__(self, enable_ocr: bool = False, ocr_lang: str = "eng",
                 row_dilation: int = 1, col_dilation: int = 1,
                 simple_mode: bool = True):
        super().__init__(enable_ocr, ocr_lang)
        self.row_dilation = row_dilation
        self.col_dilation = col_dilation
        self.simple_mode = simple_mode

    def extract(self, file_path: Path) -> ExtractedContent:
        if self.simple_mode:
            text = extract_all_islands_as_csv(
                file_path, row_dilation=self.row_dilation, col_dilation=self.col_dilation,
            )
            sheet_count = len(
                [ln for ln in text.splitlines() if ln.startswith("=== Sheet:")]
            )
            return ExtractedContent(
                text=text,
                metadata={
                    "mode": "simple_islands",
                    "sheet_count": sheet_count,
                },
            )

        header, rows = flatten_workbook(
            file_path, row_dilation=self.row_dilation, col_dilation=self.col_dilation,
        )
        buf = io.StringIO()
        if rows:
            writer = csv.writer(buf)
            writer.writerow(header)
            writer.writerows(rows)
        return ExtractedContent(
            text=buf.getvalue(),
            metadata={
                "mode": "structured_flatten",
                "row_count": len(rows),
                "header": header,
                "rows": rows,          # structured access — see class docstring
                "sheet_count": len({r[0] for r in rows}) if rows else 0,
            },
        )


# ── Factory ───────────────────────────────────────────────────────────────────

class ExtractorFactory:
    """
    Auto-select the right extractor for a given file path.

    Used by utils.py:
        local = ExtractorFactory.get_extractor(p, enable_ocr=enable_ocr)
        result = local.extract(p)
    """

    _registry = {
        ".pdf":  PDFExtractor,
        ".docx": DOCXExtractor,
        ".xlsx": XLSXExtractor,
        ".pptx": PPTXExtractor,
    }

    @classmethod
    def get_extractor(cls, file_path: Path, **kwargs) -> BaseExtractor:
        ext = file_path.suffix.lower()
        extractor_cls = cls._registry.get(ext)
        if not extractor_cls:
            raise ValueError(
                f"No extractor registered for '{ext}'. "
                f"Supported: {', '.join(cls._registry)}"
            )
        return extractor_cls(**kwargs)

    @classmethod
    def extract(cls, file_path: Path, **kwargs) -> ExtractedContent:
        """Convenience: auto-detect format and extract."""
        return cls.get_extractor(file_path, **kwargs).extract(file_path)