"""
utils.py — File processing, batch creation, and structured logging.

Image-origin tracking
---------------------
When a PDF or DOCX is processed, the extracted .txt chunks written to disk will
contain structured markers that identify which spans of text were derived from
images via OCR:

    <<<IMAGE_EXTRACT source="report.pdf" page=2 image_index=1 method="embedded_image_ocr">>>
    ... OCR text ...
    <<<END_IMAGE_EXTRACT>>>

These markers are readable by any text consumer and can be:
    - Left in place (VectorLake server handles them as regular text)
    - Stripped via  extractors.strip_image_markers(text)
    - Enumerated via extractors.list_image_regions(text)

Row-safe structured formats (CSV, XLSX)
----------------------------------------
CSV and XLSX are never routed through the generic text/paragraph chunker.
Each is split by its own dedicated function (_split_csv_safe /
_split_xlsx_safe) that guarantees a single row is never broken across two
chunk files, with the header repeated at the top of every chunk. XLSX is
flattened first via extractors.flatten_workbook() — which turns a
multi-section worksheet into (header, rows) — then written out as standard
CSV via the same _write_csv() writer _split_csv_safe uses, so both formats
produce byte-identical-style, properly quoted CSV chunk files on disk. See
extractors.XLSXExtractor's docstring for the flattening heuristics.

Streaming batching
------------------
BatchManager.streaming_create_batches() is a generator that yields
(batch_chunk_names, chunks_dir) tuples one minibatch at a time, interleaved
with extraction.  Unlike create_batches() (which blocks until every file is
extracted), this lets the caller start uploading as soon as the first minibatch
is ready — OCR of remaining files overlaps with in-flight HTTP requests.

    for batch, chunks_dir in bm.streaming_create_batches(files, base_path):
        upload(batch, chunks_dir)

The minibatch boundaries are controlled by:
    streaming_minibatch_files   — max chunks per yielded batch (default 10)
    streaming_minibatch_size_mb — max total bytes per yielded batch (default 0.5 MB / 500 KB)

These are separate from max_files_per_batch / max_batch_size_mb, which still
govern the regular create_batches() path.
"""
from __future__ import annotations 

import csv
import hashlib
import json
import logging
import os
import re as _re
import shutil
import time as _time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple
import os
import re
from .exceptions import FileProcessingError, UnsupportedFileTypeError
from .models import VersionedChunkName

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  EXTRACTOR AVAILABILITY
# ─────────────────────────────────────────────────────────────────────────────

try:
    from .extractors import ExtractorFactory, ImageFileExtractor, strip_image_markers, list_image_regions, flatten_workbook
    _RICH_EXTRACTORS = True
except ImportError:
    _RICH_EXTRACTORS = False

try:
    from .cloud_extractors import CloudOCRFactory, HybridExtractor
    _CLOUD_EXTRACTORS = True
except ImportError:
    _CLOUD_EXTRACTORS = False


# ─────────────────────────────────────────────────────────────────────────────
#  PLAIN-TEXT FALLBACKS  (no extra dependencies)
# ─────────────────────────────────────────────────────────────────────────────

def _read_plain(path: str, encoding: str = "utf-8") -> str:
    with open(path, "r", encoding=encoding, errors="replace") as f:
        return f.read()


def _extract_pdf_fallback(path: str) -> str:
    """Fallback PDF extraction using PyPDF2 (no OCR, no image tracking)."""
    try:
        import PyPDF2
    except ImportError as exc:
        raise FileProcessingError(f"PyPDF2 not installed: {exc}") from exc
    parts: List[str] = []
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            parts.append(page.extract_text() or "")
    return "\n\n".join(parts)


def _extract_docx_fallback(path: str) -> str:
    """Fallback DOCX extraction (no OCR, no image tracking)."""
    try:
        from docx import Document
    except ImportError as exc:
        raise FileProcessingError(f"python-docx not installed: {exc}") from exc
    doc = Document(path)
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _extract_pptx_fallback(path: str) -> str:
    """Fallback PPTX extraction (text + tables + notes, no OCR)."""
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise FileProcessingError(f"python-pptx not installed: {exc}") from exc
    prs = Presentation(path)
    slide_texts: List[str] = []
    for slide in prs.slides:
        parts: List[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text)
            if shape.has_table:
                for row in shape.table.rows:
                    row_text = " | ".join(c.text.strip() for c in row.cells)
                    if row_text.strip():
                        parts.append(row_text)
        if slide.has_notes_slide:
            notes_tf = slide.notes_slide.notes_text_frame
            if notes_tf and notes_tf.text.strip():
                parts.append(f"[Speaker notes] {notes_tf.text.strip()}")
        if parts:
            slide_texts.append("\n".join(parts))
    return "\n\n".join(slide_texts)


def _extract_py(path: str) -> str:
    return _read_plain(path)


def _extract_ipynb(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        nb = json.load(f)
    return "\n\n".join(
        "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
        for cell in nb.get("cells", [])
        if cell.get("cell_type") == "code"
    )


_FALLBACK_EXTRACTORS = {
    "txt":   _read_plain,
    "py":    _extract_py,
    "ipynb": _extract_ipynb,
    "pdf":   _extract_pdf_fallback,
    "docx":  _extract_docx_fallback,
    "pptx":  _extract_pptx_fallback,
}

# Extension buckets.
#
# _TEXT_LIKE is dispatch-driving: _extract_file_to_chunks() checks
# `ext in _TEXT_LIKE` directly (see the "Text-like" branch below) and routes
# every extension in it through extract_text() + paragraph_chunk().
#
# _RECORD_LIKE and _ATOMIC are documentation/classification only — they are
# NOT read anywhere in the dispatch logic. Every extension listed in them
# still needs its own explicit `elif ext == "..."` branch in
# _extract_file_to_chunks() (see the CSV / JSON / JSONL·NDJSON / XLSX
# branches). Adding an extension to _RECORD_LIKE or _ATOMIC without also
# adding a matching branch does nothing — the file will silently fall
# through to the generic/unknown-extension branch, which treats it as
# either plain text or raw binary. This is exactly how xlsx support ended
# up unwired previously: xlsx was added here, but no elif branch existed,
# so .xlsx files were copied through as raw ZIP-binary "chunks". pptx had
# the same problem in the other direction: it was listed in Config's
# allowed-extensions default but was never added to _TEXT_LIKE and had no
# elif branch of its own, so it fell all the way to the generic/unknown
# branch below, which read the binary .pptx (a ZIP archive) as
# UTF-8 text with errors="replace" — producing garbled mojibake chunk
# files instead of extracted slide text.
_TEXT_LIKE   = {"txt", "py", "ipynb", "pdf", "docx", "doc", "pptx"}
_RECORD_LIKE = {"csv", "jsonl", "ndjson", "xlsx"}
_ATOMIC      = {"json"}

# Image file extensions handled by ImageFileExtractor
_IMAGE_EXTS  = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}


# ─────────────────────────────────────────────────────────────────────────────
#  PRIMARY EXTRACTION ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def _assert_non_empty(text: str, file_path: str, method: str) -> str:
    """Raise FileProcessingError if extracted text is blank."""
    if not text or not text.strip():
        raise FileProcessingError(
            f"Extraction produced empty content for '{Path(file_path).name}' "
            f"(method={method!r}).\n"
            f"The file may be image-only with no OCR available, password-protected, "
            f"or genuinely empty."
        )
    return text


def extract_text(
    file_path: str,
    enable_ocr: bool = True,
    cloud_extractor=None,
    cloud_strategy: str = "hybrid",
    cloud_threshold: int = 100,
) -> str:
    """
    Return the full text of a file regardless of format.

    For PDF, DOCX, and image files the rich local extractor pipeline is used
    when available.  OCR-derived spans are wrapped in image-origin markers and
    included in the returned string so chunk files on disk carry provenance.

    If cloud_extractor is provided:
        strategy="hybrid"  — local first, cloud fallback when quality is low
        strategy="cloud"   — skip local, go straight to cloud engine

    Falls back to plain extraction when pymupdf / pytesseract are absent.
    """
    p   = Path(file_path)
    ext = p.suffix.lower()

    # ── Image files ───────────────────────────────────────────────────────────
    if ext in _IMAGE_EXTS:
        if cloud_extractor and cloud_strategy == "cloud":
            result = cloud_extractor.extract(p)
            _log_image_summary(result, file_path)
            return result.full_text
        if _RICH_EXTRACTORS:
            local  = ImageFileExtractor(enable_ocr=enable_ocr)
            if cloud_extractor:
                from .cloud_extractors import HybridExtractor
                result = HybridExtractor(local, cloud_extractor, cloud_threshold).extract(p)
            else:
                result = local.extract(p)
            _log_image_summary(result, file_path)
            return result.full_text
        raise FileProcessingError(
            "Image OCR requires Pillow + pytesseract.\n"
            "Install: pip install pillow pytesseract"
        )

    # ── PDF / DOCX / PPTX ────────────────────────────────────────────────────
    if ext in (".pdf", ".docx", ".pptx"):
        # Cloud-only mode
        if cloud_extractor and cloud_strategy == "cloud":
            try:
                result = cloud_extractor.extract(p)
                _log_image_summary(result, file_path)
                return _assert_non_empty(result.full_text, file_path, "cloud OCR")
            except Exception as exc:
                logger.warning("Cloud-only extraction failed for %s (%s); falling back.", file_path, exc)

        # Hybrid or local — requires pymupdf + pytesseract
        if _RICH_EXTRACTORS:
            try:
                local = ExtractorFactory.get_extractor(p, enable_ocr=enable_ocr)
                if cloud_extractor:
                    from .cloud_extractors import HybridExtractor
                    result = HybridExtractor(local, cloud_extractor, cloud_threshold).extract(p)
                else:
                    result = local.extract(p)
                _log_image_summary(result, file_path)
                return _assert_non_empty(result.full_text, file_path, "local OCR")
            except FileProcessingError:
                raise
            except Exception as exc:
                logger.warning(
                    "Rich extraction failed for %s (%s); falling back to plain extractor.",
                    file_path, exc,
                )

        # OCR was requested but rich stack not installed — hard error
        if enable_ocr:
            raise FileProcessingError(
                f"OCR was requested for '{p.name}' but pymupdf/pytesseract are not installed.\n"
                f"Run: pip install pymupdf pytesseract\n"
                f"Or set enable_ocr=False in Config to extract text layer only."
            )

    # ── Fallback extractors (text-layer only, no OCR) ─────────────────────────
    plain_ext = ext.lstrip(".")
    extractor = _FALLBACK_EXTRACTORS.get(plain_ext)
    if extractor:
        text = extractor(file_path)
        return _assert_non_empty(text, file_path, "plain extractor")

    text = _read_plain(file_path)
    return _assert_non_empty(text, file_path, "plain read")


def _log_image_summary(result, file_path: str) -> None:
    """Log how many image regions were found in an extraction."""
    count = result.image_region_count
    if count:
        logger.info(
            "%s — extracted %d image region(s) with OCR (markers embedded in output)",
            Path(file_path).name, count,
        )
    else:
        logger.debug("%s — no image content detected", Path(file_path).name)


# ─────────────────────────────────────────────────────────────────────────────
#  CHUNKING HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_BLOCK_RE = _re.compile(
    r'(<<<IMAGE_EXTRACT[^>]*>>>.*?<<<END_IMAGE_EXTRACT>>>)',
    _re.DOTALL,
)


def paragraph_chunk(text: str, max_chars: int) -> List[str]:
    """
    Split text on blank lines.

    Image-origin markers are kept together with their content: the chunker
    never splits inside a marker block so provenance is preserved across all
    output chunk files.

    NOTE: max_chars is treated as a UTF-8 byte limit (not a character count)
    so that on-disk chunk sizes are guaranteed to stay under the batch limit.
    """
    segments = _BLOCK_RE.split(text)
    paragraphs: List[str] = []

    for seg in segments:
        if _BLOCK_RE.fullmatch(seg):
            paragraphs.append(seg.strip())
        else:
            for p in seg.split("\n\n"):
                if p.strip():
                    paragraphs.append(p.strip())

    chunks: List[str] = []
    current: List[str] = []
    current_bytes = 0

    for para in paragraphs:
        para_bytes = len(para.encode("utf-8")) + 2  # +2 for "\n\n" separator
        if current and current_bytes + para_bytes > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_bytes = 0
        current.append(para)
        current_bytes += para_bytes

    if current:
        chunks.append("\n\n".join(current))

    return chunks or [text]


def _split_csv_safe(
    file_path: str,
    safe_limit: int,
    chunks_dir: Path,
    base_chunk_name_fn,
) -> List[str]:
    """Split a CSV by rows; never break a row across chunks."""
    p = Path(file_path)
    output_files: List[str] = []

    encoding = "utf-8"
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            with open(p, "r", newline="", encoding=enc) as f:
                csv.reader(f).__next__()
            encoding = enc
            break
        except (UnicodeDecodeError, StopIteration):
            continue

    with open(p, "r", newline="", encoding=encoding, errors="replace") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise FileProcessingError(f"CSV file is empty: {file_path}")

        header_bytes = len(",".join(header).encode("utf-8"))
        part, rows, current_size = 1, [], header_bytes

        for row in reader:
            try:
                row_bytes = len(",".join(row).encode("utf-8"))
            except Exception:
                continue

            if row_bytes > safe_limit:
                raise FileProcessingError(
                    f"A single CSV row exceeds safe limit ({safe_limit} bytes)"
                )

            if current_size + row_bytes > safe_limit and rows:
                out = chunks_dir / base_chunk_name_fn(part)
                _write_csv(out, header, rows)
                output_files.append(str(out))
                part += 1
                rows, current_size = [], header_bytes

            rows.append(row)
            current_size += row_bytes

        if rows:
            out = chunks_dir / base_chunk_name_fn(part)
            _write_csv(out, header, rows)
            output_files.append(str(out))

    return output_files


def _write_csv(path: Path, header: List[str], rows: List[List[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _split_xlsx_safe(
    file_path: str,
    safe_limit: int,
    chunks_dir: Path,
    base_chunk_name_fn,
    row_dilation: int = 1,
    col_dilation: int = 0,
) -> List[str]:
    """
    Flatten an .xlsx workbook (via extractors.flatten_workbook) and split the
    resulting rows into standard CSV chunk files, reusing _write_csv — the
    same writer _split_csv_safe uses for genuine .csv files. Real CSV
    quoting means embedded commas, quotes, or newlines in any cell value are
    escaped correctly instead of corrupting row/column boundaries. Never
    breaks a row across chunks.
    """
    if not _RICH_EXTRACTORS:
        raise FileProcessingError(
            "XLSX extraction requires openpyxl.\nRun: pip install openpyxl"
        )

    header, rows = flatten_workbook(
        file_path, row_dilation=row_dilation, col_dilation=col_dilation,
    )
    if not rows:
        raise FileProcessingError(f"XLSX file has no extractable data: {file_path}")

    output_files: List[str] = []
    header_bytes = len(",".join(header).encode("utf-8"))
    part, buf_rows, current_size = 1, [], header_bytes

    for row in rows:
        row_strs = [str(v) if v is not None else "" for v in row]
        # Byte estimate is approximate (doesn't account for CSV quoting
        # overhead) — same approximation _split_csv_safe already uses, kept
        # consistent rather than exact. +2 covers the row separator.
        row_bytes = len(",".join(row_strs).encode("utf-8")) + 2

        if row_bytes > safe_limit:
            raise FileProcessingError(
                f"A single XLSX row exceeds safe limit ({safe_limit} bytes)"
            )

        if current_size + row_bytes > safe_limit and buf_rows:
            out = chunks_dir / base_chunk_name_fn(part)
            _write_csv(out, header, buf_rows)
            output_files.append(str(out))
            part += 1
            buf_rows, current_size = [], header_bytes

        buf_rows.append(row_strs)
        current_size += row_bytes

    if buf_rows:
        out = chunks_dir / base_chunk_name_fn(part)
        _write_csv(out, header, buf_rows)
        output_files.append(str(out))

    return output_files


def _split_jsonl_safe(
    file_path: str,
    safe_limit: int,
    chunks_dir: Path,
    base_chunk_name_fn,
) -> List[str]:
    """Split JSONL by record; each line is atomic."""
    p = Path(file_path)
    output_files: List[str] = []
    part, buffer, current_size = 1, [], 0

    def flush():
        nonlocal part, buffer, current_size
        if not buffer:
            return
        out = chunks_dir / base_chunk_name_fn(part)
        with open(out, "w", encoding="utf-8") as f:
            for line in buffer:
                f.write(line + "\n")
        output_files.append(str(out))
        part += 1
        buffer.clear()
        current_size = 0

    with open(p, "r", encoding="utf-8", errors="replace") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except Exception as exc:
                raise FileProcessingError(
                    f"Invalid JSON at line {lineno} in {file_path}: {exc}"
                ) from exc

            line_bytes = len((line + "\n").encode("utf-8"))
            if line_bytes > safe_limit:
                raise FileProcessingError(
                    f"Single JSONL record exceeds safe limit ({safe_limit} bytes)"
                )
            if current_size + line_bytes > safe_limit:
                flush()
            buffer.append(line)
            current_size += line_bytes

    flush()
    return output_files


# ─────────────────────────────────────────────────────────────────────────────
#  INTERLEAVE HELPER
# ─────────────────────────────────────────────────────────────────────────────

_PART_SUFFIX_RE = _re.compile(r'_part(\d+)$', _re.IGNORECASE)


def _base_stem(chunk_name: str) -> str:
    """
    Extract logical base name from a chunk filename.

    Examples:
        "PD_ISSUE_part3.txt"  → "PD_ISSUE"
        "report_part1.txt"    → "report"
        "notes.txt"           → "notes"
    """
    stem = Path(chunk_name).stem
    return _PART_SUFFIX_RE.sub('', stem) or stem


def _interleave_chunks(
    file_info: List[Tuple[str, int]],
) -> List[Tuple[str, int]]:
    """
    Reorder (chunk_path, size) pairs so that parts from DIFFERENT logical
    files are placed consecutively, rather than all 200 parts of one big
    file filling batch after batch before the next file starts.

    Algorithm: round-robin across base names, preserving part order within
    each base.  This maximises inter-file diversity inside every batch.

    Example input order:  A1 A2 A3 B1 B2 C1
    Output order:         A1 B1 C1 A2 B2 A3
    """
    # Group by base name, preserving intra-group order
    groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for item in file_info:
        base = _base_stem(Path(item[0]).name)
        groups[base].append(item)

    if len(groups) == 1:
        # Only one logical file — nothing to interleave
        return file_info

    # Round-robin: take one chunk from each group in turn until exhausted
    result: List[Tuple[str, int]] = []
    queues = [list(v) for v in groups.values()]  # each queue is already sorted by part#

    while any(queues):
        for q in queues:
            if q:
                result.append(q.pop(0))

    logger.info(
        "Interleaved %d chunks from %d logical file(s) across batches",
        len(result), len(groups),
    )
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  SOURCE-FILE COUNT HELPER  (used by client.py to advance files_meta offset)
# ─────────────────────────────────────────────────────────────────────────────

# CHANGED: new public helper — counts distinct source files in a chunk batch.
# Fixes the metadata-alignment bug where file_offset was advanced by the
# number of chunks rather than the number of source files.
def _unique_source_count(batch: List[str]) -> int:
    """
    Return the number of distinct source files referenced by *batch*.

    A batch contains chunk basenames like ``report_part1.txt``,
    ``report_part2.txt``, ``notes_part1.txt``.  This function returns 2,
    because both report chunks belong to the same source file.

    Used by callers of ``_read_batch_contents`` to advance the ``file_offset``
    pointer into ``files_meta`` by the right amount (source files, not chunks).
    """
    seen: set = set()
    for fname in batch:
        parsed = VersionedChunkName.parse(fname)
        stem = parsed.stem if parsed else Path(fname).stem
        seen.add(stem)
    return len(seen)


# ─────────────────────────────────────────────────────────────────────────────
#  FILE PROCESSOR
# ─────────────────────────────────────────────────────────────────────────────

class FileProcessor:
    """
    Validates and reads files.

    allowed_extensions is passed from Config so .env is the single source.
    Image extensions (png, jpg, …) are automatically added to the allowed list
    when the rich extractor stack is available.
    """

    _IMAGE_EXTS = {"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"}

    def __init__(
        self,
        allowed_extensions: List[str],
        enable_ocr: bool = True,
        cloud_extractor=None,
        cloud_strategy: str = "hybrid",
        cloud_threshold: int = 100,
    ):
        self.enable_ocr       = enable_ocr
        self.cloud_extractor  = cloud_extractor
        self.cloud_strategy   = cloud_strategy
        self.cloud_threshold  = cloud_threshold

        base = [e.lower() for e in allowed_extensions]
        if _RICH_EXTRACTORS:
            base = list(set(base) | self._IMAGE_EXTS)
        self.allowed_extensions = base

    def is_supported(self, filename: str) -> bool:
        ext = Path(filename).suffix.lstrip(".").lower()
        return ext in self.allowed_extensions

    def assert_supported(self, filename: str) -> None:
        if not self.is_supported(filename):
            raise UnsupportedFileTypeError(filename, self.allowed_extensions)

    def read_content(self, filepath: str) -> str:
        """Read and return full text content of any supported file."""
        self.assert_supported(filepath)
        if VersionedChunkName.is_versioned(filepath):
            return _read_plain(filepath)
        return extract_text(
            filepath,
            enable_ocr=self.enable_ocr,
            cloud_extractor=self.cloud_extractor,
            cloud_strategy=self.cloud_strategy,
            cloud_threshold=self.cloud_threshold,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  BATCH MANAGER
# ─────────────────────────────────────────────────────────────────────────────

class BatchManager:
    """
    Converts a list of source files into upload-ready batches.

    Chunk naming: {stem}_part{part}.txt
    Chunks already on disk are reused — safe to re-run after a crash.

    Two batching strategies are provided:

    create_batches() — Original blocking strategy.
        All source files are extracted and chunked to disk before any
        batch is returned.  Use for sequential / parallel modes where the
        complete batch list must be known upfront.

    streaming_create_batches() — Pipelined streaming strategy.
        A generator that yields (batch_chunk_names, chunks_dir) tuples one
        minibatch at a time, interleaved with extraction.  The caller can
        start uploading the first minibatch while the SDK is still OCR-ing
        later files, eliminating the upfront blocking phase entirely.

        Minibatch size is controlled by two independent parameters (so you
        can tune for your server's payload limit vs latency trade-off):
            streaming_minibatch_files   — max chunk filenames per yield (default 10)
            streaming_minibatch_size_mb — max total byte size per yield (default 0.5 MB)

        Batches are assembled using round-robin interleaving within the
        pending buffer at flush time, preserving cross-file diversity.

    Image files (PNG, JPG, etc.) are OCR'd and written to .txt chunks whose
    content includes image-origin markers for provenance tracking.
    """

    def __init__(
        self,
        max_files: int,
        # CHANGED: float — supports fractional MB values like 0.5 (500 KB)
        max_size_mb: float,
        allowed_extensions: List[str],
        enable_ocr: bool = True,
        cloud_extractor=None,
        cloud_strategy: str = "hybrid",
        cloud_threshold: int = 100,
        streaming_minibatch_files: int = 10,
        # CHANGED: float — supports fractional MB values like 0.5 (500 KB)
        streaming_minibatch_size_mb: float = 0.5,
    ):
        self.max_files    = max_files
        # CHANGED: int() cast so downstream byte-math stays integer arithmetic
        self.max_bytes    = int(max_size_mb * 1024 * 1024)
        self.enable_ocr   = enable_ocr
        self.file_processor = FileProcessor(
            allowed_extensions,
            enable_ocr=enable_ocr,
            cloud_extractor=cloud_extractor,
            cloud_strategy=cloud_strategy,
            cloud_threshold=cloud_threshold,
        )
        # Streaming-mode knobs
        self.streaming_minibatch_files = max(1, streaming_minibatch_files)
        # CHANGED: was max(1, x) * 1024*1024  — broken for sub-1-MB values
        # because max(1, 0.5) = 1, giving 1 MB instead of 500 KB.
        # Fix: clamp at a small epsilon, then convert with int().
        self.streaming_minibatch_bytes = int(
            max(0.001, streaming_minibatch_size_mb) * 1024 * 1024
        )

    # ── Shared: extract one source file → list of (chunk_path, size) ──────────

    def _extract_file_to_chunks(
        self,
        fname: str,
        src: Path,
        chunks_dir: Path,
    ) -> List[Tuple[str, int]]:
        """
        Extract *src* and write on-disk chunk files under *chunks_dir*.

        Returns a list of (absolute_chunk_path, byte_size) tuples.
        Skips already-existing, non-empty chunks to avoid redundant OCR.
        Logs and returns [] on any extraction error so one bad file never
        aborts the entire run.

        Logging
        -------
        DEBUG — entry: file name + size on disk
        DEBUG — stage timings: extract_text, paragraph_chunk, write-to-disk
        INFO  — exit: N chunks produced, total bytes written, wall-clock elapsed
                (suppressed when every chunk already existed on disk)
        WARNING — extraction failure with exception type and message
        """
        t_total_start = _time.monotonic()
        safe_limit = self.max_bytes
        ext = src.suffix.lstrip(".").lower()
        file_info: List[Tuple[str, int]] = []

        src_bytes = src.stat().st_size if src.exists() else 0
        logger.debug(
            "[extract] START  '%s'  size=%.1f KB  ext=%s",
            fname, src_bytes / 1024, ext,
        )

        def _chunk_name(part: int, _fname=fname) -> str:
            return VersionedChunkName.build(_fname, part=part).filename

        def _timed_extract_text() -> str:
            t0 = _time.monotonic()
            text = extract_text(
                str(src),
                enable_ocr=self.enable_ocr,
                cloud_extractor=self.file_processor.cloud_extractor,
                cloud_strategy=self.file_processor.cloud_strategy,
                cloud_threshold=self.file_processor.cloud_threshold,
            )
            logger.debug(
                "[extract] text_extract  '%s'  chars=%d  elapsed=%.2fs",
                fname, len(text), _time.monotonic() - t0,
            )
            return text

        def _timed_chunk(text: str) -> List[str]:
            t0 = _time.monotonic()
            chunks = paragraph_chunk(text, max_chars=safe_limit)
            logger.debug(
                "[extract] chunk  '%s'  %d chunk(s)  elapsed=%.3fs",
                fname, len(chunks), _time.monotonic() - t0,
            )
            return chunks

        def _timed_write(dst: Path, content: str) -> None:
            t0 = _time.monotonic()
            dst.write_text(content, encoding="utf-8")
            logger.debug(
                "[extract] write  '%s'  bytes=%d  elapsed=%.3fs",
                dst.name, dst.stat().st_size, _time.monotonic() - t0,
            )

        newly_written = 0  # track how many chunks we actually wrote (vs cache hits)

        try:
            # ── Image files: OCR → write annotated .txt chunk ─────────────────
            if ext in {e.lstrip(".") for e in _IMAGE_EXTS}:
                cname = _chunk_name(1)
                dst = chunks_dir / cname
                if not dst.exists() or dst.stat().st_size == 0:
                    text = _timed_extract_text()
                    _timed_write(dst, text)
                    newly_written += 1
                file_info.append((str(dst), dst.stat().st_size))

            # ── JSON: atomic ───────────────────────────────────────────────────
            elif ext == "json":
                cname = _chunk_name(1)
                dst = chunks_dir / cname
                if not dst.exists():
                    t0 = _time.monotonic()
                    shutil.copy2(src, dst)
                    logger.debug(
                        "[extract] copy  '%s'  elapsed=%.3fs",
                        dst.name, _time.monotonic() - t0,
                    )
                    newly_written += 1
                file_info.append((str(dst), dst.stat().st_size))

            # ── CSV ────────────────────────────────────────────────────────────
            elif ext == "csv":
                t0 = _time.monotonic()
                parts = _split_csv_safe(str(src), safe_limit, chunks_dir, _chunk_name)
                logger.debug(
                    "[extract] csv_split  '%s'  %d part(s)  elapsed=%.3fs",
                    fname, len(parts), _time.monotonic() - t0,
                )
                newly_written += len(parts)
                for p in parts:
                    file_info.append((p, Path(p).stat().st_size))

            # ── XLSX ───────────────────────────────────────────────────────────
            elif ext == "xlsx":
                t0 = _time.monotonic()
                parts = _split_xlsx_safe(str(src), safe_limit, chunks_dir, _chunk_name)
                logger.debug(
                    "[extract] xlsx_split  '%s'  %d part(s)  elapsed=%.3fs",
                    fname, len(parts), _time.monotonic() - t0,
                )
                newly_written += len(parts)
                for p in parts:
                    file_info.append((p, Path(p).stat().st_size))

            # ── JSONL / NDJSON ─────────────────────────────────────────────────
            elif ext in ("jsonl", "ndjson"):
                t0 = _time.monotonic()
                parts = _split_jsonl_safe(str(src), safe_limit, chunks_dir, _chunk_name)
                logger.debug(
                    "[extract] jsonl_split  '%s'  %d part(s)  elapsed=%.3fs",
                    fname, len(parts), _time.monotonic() - t0,
                )
                newly_written += len(parts)
                for p in parts:
                    file_info.append((p, Path(p).stat().st_size))

            # ── Text-like (PDF, DOCX, TXT, PY, IPYNB) ─────────────────────────
            elif ext in _TEXT_LIKE:
                text = _timed_extract_text()
                chunks = _timed_chunk(text)
                t_write_start = _time.monotonic()
                for i, chunk_text in enumerate(chunks, 1):
                    cname = _chunk_name(i)
                    dst = chunks_dir / cname
                    if not dst.exists() or dst.stat().st_size == 0:
                        dst.write_text(chunk_text, encoding="utf-8")
                        newly_written += 1
                    file_info.append((str(dst), dst.stat().st_size))
                logger.debug(
                    "[extract] write_all  '%s'  %d chunk(s)  elapsed=%.3fs",
                    fname, len(chunks), _time.monotonic() - t_write_start,
                )

            # ── Unknown / generic ──────────────────────────────────────────────
            else:
                size = src.stat().st_size
                if size > safe_limit:
                    text = src.read_text(encoding="utf-8", errors="replace")
                    chunks = _timed_chunk(text)
                    t_write_start = _time.monotonic()
                    for i, chunk_text in enumerate(chunks, 1):
                        cname = _chunk_name(i)
                        dst = chunks_dir / cname
                        if not dst.exists():
                            dst.write_text(chunk_text, encoding="utf-8")
                            newly_written += 1
                        file_info.append((str(dst), dst.stat().st_size))
                    logger.debug(
                        "[extract] write_all  '%s'  %d chunk(s)  elapsed=%.3fs",
                        fname, len(chunks), _time.monotonic() - t_write_start,
                    )
                else:
                    cname = _chunk_name(1)
                    dst = chunks_dir / cname
                    if not dst.exists():
                        t0 = _time.monotonic()
                        shutil.copy2(src, dst)
                        logger.debug(
                            "[extract] copy  '%s'  elapsed=%.3fs",
                            dst.name, _time.monotonic() - t0,
                        )
                        newly_written += 1
                    file_info.append((str(dst), dst.stat().st_size))

        except Exception as exc:
            logger.warning(
                "Skipping '%s' — extraction failed: %s: %s",
                fname, type(exc).__name__, exc,
            )
            return []

        elapsed = _time.monotonic() - t_total_start
        total_bytes = sum(s for _, s in file_info)
        n_chunks = len(file_info)
        cache_hits = n_chunks - newly_written

        # One concise INFO line per file when real work was done; DEBUG for cache hits
        if newly_written > 0:
            slow_tag = f"  ⚠ slow ({elapsed:.1f}s)" if elapsed > 5.0 else ""
            logger.info(
                "✔ %-30s  %d chunk(s)  %.1f KB  %.2fs%s",
                fname, n_chunks, total_bytes / 1024, elapsed, slow_tag,
            )
        else:
            logger.debug("cache-hit '%s'  %d chunk(s)", fname, n_chunks)

        return file_info

    # ── Original blocking strategy ─────────────────────────────────────────────

    def create_batches(
        self,
        files: List[str],
        base_path: str,
        extraction_workers: int = 4,
    ) -> Tuple[List[List[str]], str]:
        """
        Process files into upload batches using a thread pool for extraction.

        Returns (batches, chunks_dir).
          batches    : list of lists of chunk basenames
          chunks_dir : directory where chunk files live

        ALL files are extracted (in parallel via ``extraction_workers`` threads)
        before any batch is returned.  Chunks are interleaved by logical base name
        before batching so each batch contains a mix of files.

        Parameters
        ----------
        extraction_workers:
            Number of concurrent extraction threads (default 4).  Increase for
            large corpora with expensive OCR; set to 1 to reproduce the original
            sequential behaviour.
        """
        chunks_dir = Path(base_path) / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)

        # Filter files upfront
        valid: List[Tuple[str, Path]] = []
        skipped_count = 0
        for fname in files:
            src = Path(base_path) / fname
            if not src.is_file():
                logger.warning("File not found, skipping: %s", src)
                skipped_count += 1
            elif not self.file_processor.is_supported(fname):
                logger.warning("Unsupported extension, skipping: %s", fname)
                skipped_count += 1
            else:
                valid.append((fname, src))

        total_files = len(valid)
        logger.info(
            "Sequential extract: %d file(s)  workers=%d  skipped=%d",
            total_files, extraction_workers, skipped_count,
        )

        # --- parallel extraction ---
        # Results dict preserves original order
        results: Dict[str, List[Tuple[str, int]]] = {}
        t_batch_start = _time.monotonic()
        done_count = 0
        _log_every = max(1, min(500, total_files // 50))

        with ThreadPoolExecutor(max_workers=extraction_workers) as pool:
            future_to_fname = {
                pool.submit(self._extract_file_to_chunks, fname, src, chunks_dir): fname
                for fname, src in valid
            }
            for future in as_completed(future_to_fname):
                fname = future_to_fname[future]
                try:
                    results[fname] = future.result()
                except Exception as exc:
                    logger.warning("Extraction failed for '%s': %s", fname, exc)
                    results[fname] = []
                done_count += 1

                # Concise progress with ETA
                if (done_count == 1
                        or done_count % _log_every == 0
                        or done_count == total_files):
                    elapsed = _time.monotonic() - t_batch_start
                    rate = done_count / elapsed if elapsed > 0 else 0.0
                    eta_s = (total_files - done_count) / rate if rate > 0 else float("inf")
                    eta_str = f"{eta_s:.0f}s" if eta_s != float("inf") else "?"
                    chunks_so_far = sum(len(v) for v in results.values())
                    logger.info(
                        "Extract  %d/%d  (%.0f%%)  chunks=%d  %.1fs elapsed  ETA %s",
                        done_count, total_files,
                        100.0 * done_count / total_files if total_files else 0,
                        chunks_so_far, elapsed, eta_str,
                    )

        # Preserve original file order, flatten
        file_info: List[Tuple[str, int]] = []
        for fname, _ in valid:
            file_info.extend(results.get(fname, []))

        # ── Interleave chunks across logical files BEFORE batching ─────────────
        file_info = _interleave_chunks(file_info)

        # ── Assemble batches ───────────────────────────────────────────────────
        batches: List[List[str]] = []
        current_batch: List[str] = []
        current_size = 0

        for abs_path, size in file_info:
            name = Path(abs_path).name
            if size > self.max_bytes:
                logger.warning(
                    "Skipping chunk '%s' (%d bytes) — exceeds max batch size (%d bytes). "
                    "Increase max_batch_size_mb in Config to include this file.",
                    name, size, self.max_bytes,
                )
                continue
            if current_batch and (
                len(current_batch) >= self.max_files
                or current_size + size > self.max_bytes
            ):
                batches.append(current_batch)
                current_batch, current_size = [], 0
            current_batch.append(name)
            current_size += size

        if current_batch:
            batches.append(current_batch)

        total_elapsed = _time.monotonic() - t_batch_start
        logger.info(
            "Extract done — %d files  %d chunks  %d batch(es)  %.1fs",
            len(valid) - skipped_count, len(file_info), len(batches), total_elapsed,
        )
        return batches, str(chunks_dir)

    # ── Streaming pipelined strategy ──────────────────────────────────────────

    def streaming_create_batches(
        self,
        files: List[str],
        base_path: str,
        extraction_workers: int = 4,
    ) -> Generator[Tuple[List[str], str], None, None]:
        """
        Extract source files in a thread pool and yield upload-ready minibatches
        via a thread-safe queue, without waiting for all files to be processed.

        Architecture
        ------------
        A producer thread pool runs ``_extract_file_to_chunks`` for each file
        concurrently and deposits completed (chunk_path, size) pairs into a
        ``queue.Queue``.  The main thread drains the queue, fills a pending
        buffer, and yields a minibatch whenever the buffer is full — exactly
        as before, but extraction of file N+1 is now overlapped with upload
        of file N's minibatch *and* with extraction of files N+2…N+k.

        Minibatch residence
        -------------------
        Each minibatch lives in the ``pending`` buffer until ``_flush_pending``
        is called.  Once yielded to the caller it is removed from the buffer
        (the list is cleared).  There is therefore at most one minibatch's
        worth of chunks in the buffer at any time; the queue holds completed
        futures that have not yet been drained into the buffer.

        For ``insert`` / ``refresh``, the same flow applies: chunks accumulate
        in the pending buffer; ``_flush_pending`` packs them into fixed-size
        upload batches and the generator yields each one.  Nothing sits in the
        queue longer than it takes the main thread to drain it.

        Parameters
        ----------
        files:
            Source filenames to process (basenames, relative to *base_path*).
        base_path:
            Directory containing the source files.
        extraction_workers:
            Number of parallel extraction threads (default 4).
        """
        import queue as _queue

        chunks_dir = Path(base_path) / "chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        chunks_dir_str = str(chunks_dir)

        # Filter upfront
        valid: List[Tuple[str, Path]] = []
        skipped_count = 0
        for fname in files:
            src = Path(base_path) / fname
            if not src.is_file():
                logger.warning("File not found, skipping: %s", src)
                skipped_count += 1
            elif not self.file_processor.is_supported(fname):
                logger.warning("Unsupported extension, skipping: %s", fname)
                skipped_count += 1
            else:
                valid.append((fname, src))

        total_files = len(valid)
        _SENTINEL = object()

        # Result queue: items are (fname, [(chunk_path, size), ...]) or _SENTINEL
        result_q: "_queue.Queue" = _queue.Queue()

        def _extract_worker(fname: str, src: Path) -> None:
            """Run in a thread pool; push result to queue."""
            # Fast-path: chunks already on disk
            stem = src.stem
            existing = sorted(chunks_dir.glob(f"{stem}_part*.txt"), key=lambda p: p.name)
            if existing:
                chunks = [(str(p), p.stat().st_size) for p in existing]
                logger.debug("cache-hit '%s'  %d chunk(s)", fname, len(chunks))
            else:
                chunks = self._extract_file_to_chunks(fname, src, chunks_dir)
            result_q.put((fname, chunks))

        # ── Pending buffer (same flush logic as before) ────────────────────────
        pending: List[Tuple[str, int]] = []
        pending_bytes = 0
        total_yielded_batches = 0

        def _flush_pending() -> List[List[str]]:
            nonlocal pending, pending_bytes
            if not pending:
                return []
            interleaved = _interleave_chunks(pending)
            pending = []
            pending_bytes = 0
            batches: List[List[str]] = []
            current_batch: List[str] = []
            current_size = 0
            for abs_path, size in interleaved:
                name = Path(abs_path).name
                if current_batch and (
                    len(current_batch) >= self.max_files
                    or current_size + size > self.max_bytes
                ):
                    batches.append(current_batch)
                    current_batch, current_size = [], 0
                current_batch.append(name)
                current_size += size
            if current_batch:
                batches.append(current_batch)
            return batches

        # ── Launch extraction threads ──────────────────────────────────────────
        t_start = _time.monotonic()
        _log_every = max(1, min(500, total_files // 50))
        done_count = 0

        with ThreadPoolExecutor(max_workers=extraction_workers) as pool:
            for fname, src in valid:
                pool.submit(_extract_worker, fname, src)

            # Drain results as they complete
            for _ in range(total_files):
                fname, new_chunks = result_q.get()
                done_count += 1

                for chunk_path, chunk_size in new_chunks:
                    pending.append((chunk_path, chunk_size))
                    pending_bytes += chunk_size

                # Concise progress + ETA
                if (done_count == 1
                        or done_count % _log_every == 0
                        or done_count == total_files):
                    elapsed = _time.monotonic() - t_start
                    rate = done_count / elapsed if elapsed > 0 else 0.0
                    eta_s = (total_files - done_count) / rate if rate > 0 else float("inf")
                    eta_str = f"{eta_s:.0f}s" if eta_s != float("inf") else "?"
                    logger.info(
                        "Stream extract  %d/%d (%.0f%%)  buf=%d chunks %.2f MB  "
                        "uploaded=%d  ETA %s",
                        done_count, total_files,
                        100.0 * done_count / total_files if total_files else 0,
                        len(pending), pending_bytes / (1024 * 1024),
                        total_yielded_batches, eta_str,
                    )

                # Flush when buffer is large enough
                should_flush = (
                    len(pending) >= self.streaming_minibatch_files
                    or pending_bytes >= self.streaming_minibatch_bytes
                )
                if should_flush:
                    logger.debug(
                        "Flush triggered  chunks=%d  %.2f MB  →  packing batches",
                        len(pending), pending_bytes / (1024 * 1024),
                    )
                    for batch in _flush_pending():
                        total_yielded_batches += 1
                        logger.debug(
                            "Yield minibatch #%d  %d chunk(s)", total_yielded_batches, len(batch),
                        )
                        yield batch, chunks_dir_str

        # ── Final flush ────────────────────────────────────────────────────────
        if pending:
            logger.debug(
                "Final flush  chunks=%d  %.2f MB", len(pending), pending_bytes / (1024 * 1024),
            )
        for batch in _flush_pending():
            total_yielded_batches += 1
            yield batch, chunks_dir_str

        logger.info(
            "Stream extract done — %d files  %d batches uploaded  %.1fs",
            done_count, total_yielded_batches, _time.monotonic() - t_start,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────────────────────────────────────

class Logger:
    """Structured CSV-based logger for skipped files, API errors, and performance."""

    def __init__(self, log_dir: str):
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        self._log_dir = Path(log_dir)

    def _append_csv(self, filename: str, header: List[str], row: list) -> None:
        path = self._log_dir / filename
        exists = path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(header)
            w.writerow(row)

    def log_skipped_file(self, filename: str, reason: str) -> None:
        self._append_csv(
            "skipped_files.csv",
            ["timestamp", "filename", "reason"],
            [_utcnow(), filename, reason],
        )

    def log_api_error(self, operation: str, batch_num: int, message: str) -> None:
        self._append_csv(
            "api_errors.csv",
            ["timestamp", "operation", "batch_num", "message"],
            [_utcnow(), operation, batch_num, message],
        )

    def log_performance(self, **kwargs) -> None:
        row = {"timestamp": _utcnow(), **{k: str(v) for k, v in kwargs.items()}}
        path = self._log_dir / "performance.csv"
        exists = path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=row.keys())
            if not exists:
                w.writeheader()
            w.writerow(row)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─────────────────────────────────────────────────────────────────────────────
#  FILENAME SANITIZATION
# ─────────────────────────────────────────────────────────────────────────────

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def clean_filename(fname: str, max_len: int = 255) -> str:
    """
    Light sanitization of original filename for storage/display.
    Keeps it as close to original as possible, just fixes unsafe chars.
    """
    name = str(fname).strip()

    if not name:
        return "unnamed_file"

    # remove illegal filesystem chars
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name)

    # collapse one or more spaces into a single underscore
    name = re.sub(r'\s+', '_', name)

    # collapse multiple underscores/spaces
    name = re.sub(r'_+', '_', name)
    name = name.strip("._- ")

    if not name:
        return "unnamed_file"

    # windows reserved names
    stem = os.path.splitext(name)[0]
    if stem.upper() in WINDOWS_RESERVED:
        name = f"_{name}"

    return name[:max_len]


def sanitize_filenames(base_path: str, filenames: List[str]) -> List[str]:
    """
    Ensure every file in *filenames* has a clean, filesystem-safe name
    (via ``clean_filename``) **on disk** in *base_path*, renaming in place
    where needed, and return the corrected filename list.

    Why this matters
    -----------------
    Chunk files are always named after whatever the *actual source file on
    disk* is called (``{source_stem}_part{n}.txt``). If the source file
    itself has spaces, illegal characters, or an over-long name, those
    exact same problems propagate into every chunk file, MD5 sidecar,
    checkpoint entry, and metadata ``filename`` field derived from it.

    Renaming the source file once, up front — rather than just cleaning the
    *name we pass around in memory* — keeps every downstream piece of code
    (chunk stem matching, refresh's stale-chunk purge, MD5 change detection,
    dict-based ``files_meta`` lookup) working exactly as before, since they
    all already assume "chunk stem == real source filename on disk".

    Safety
    ------
    - A no-op for any file whose name is already clean.
    - If the cleaned target name already exists and belongs to a
      *different* file, the rename is skipped and a warning is logged
      instead of silently overwriting or losing data — the original name
      is kept in the returned list for that entry.
    - Idempotent: calling this twice on an already-sanitized directory does
      nothing on the second call.

    Parameters
    ----------
    base_path:
        Directory containing the source files.
    filenames:
        Candidate filenames (basenames) to check/rename, as currently found
        on disk.

    Returns
    -------
    List[str]
        The same files, in the same order, with names updated wherever a
        rename actually happened.
    """
    base = Path(base_path)
    resolved: List[str] = []

    for fname in filenames:
        cleaned = clean_filename(fname)
        if cleaned == fname:
            resolved.append(fname)
            continue

        src = base / fname
        dst = base / cleaned

        if not src.is_file():
            # Nothing to rename — caller will warn downstream if it's
            # genuinely missing.
            resolved.append(fname)
            continue

        if dst.exists():
            try:
                same_file = dst.resolve() == src.resolve()
            except OSError:
                same_file = False
            if not same_file:
                logger.warning(
                    "Can't sanitize filename '%s' → '%s': target already "
                    "exists and belongs to a different file. Keeping the "
                    "original filename for this file.", fname, cleaned,
                )
                resolved.append(fname)
                continue
            # dst already *is* src (e.g. re-run) — nothing to do.
            resolved.append(cleaned)
            continue

        try:
            src.rename(dst)
            logger.info("Sanitized filename on disk: '%s' → '%s'", fname, cleaned)
            resolved.append(cleaned)
        except OSError as exc:
            logger.warning(
                "Could not sanitize filename '%s' → '%s': %s. Keeping the "
                "original filename for this file.", fname, cleaned, exc,
            )
            resolved.append(fname)

    return resolved