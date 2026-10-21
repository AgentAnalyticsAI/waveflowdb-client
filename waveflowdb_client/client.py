"""
client.py — VectorLakeClient

Main entry point for the VectorLake SDK.  Handles file chunking, batching,
uploads, and all communication with the VectorLake server.

Processing modes
----------------
``sequential`` (default)
    Files are fully chunked first, then batches are uploaded one at a time.
    This is the safest choice when memory or network concurrency is
    constrained, and the only mode that supports checkpoint/resume via
    ``start_from_batch`` / ``end_batch``.

``streaming``
    Extraction and uploading overlap: the first minibatch is sent as soon as
    ``streaming_minibatch_files`` chunks (or ``streaming_minibatch_size_mb``
    MB) have been extracted.  For large collections with expensive OCR this
    can cut wall-clock time significantly.  Note that streaming does **not**
    support resume — use sequential mode when checkpoint/resume is needed.
"""
from __future__ import annotations

import glob as _glob
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid as _uuid_mod
from datetime import datetime as _dt, timezone as _timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import requests

from .config import Config
from .exceptions import (
    APIError, ConfigError, DocumentNotFoundError, InvalidSearchTypeError,
    ThrottleError, ValidationError, VectorLakeError,
)
from .models import VersionedChunkName
from .utils import BatchManager, FileProcessor, Logger, _unique_source_count
from .utils import (
    BatchManager, FileProcessor, Logger, _unique_source_count,
    clean_filename, sanitize_filenames,
)

try:
    from .cloud_extractors import CloudOCRFactory
    _CLOUD_FACTORY_AVAILABLE = True
except ImportError:
    _CLOUD_FACTORY_AVAILABLE = False

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = True  # delegate to root logger (handlers configured by run_utils)

_VALID_SEARCH_TYPES = ("flat", "flat_filter")

# Processing mode type alias  (parallel mode removed)
ProcessingMode = Literal["sequential", "streaming"]

# ── files_meta argument shape ────────────────────────────────────────────────
#
# Every public upload method accepts per-file metadata in one of two shapes:
#
#   list  — legacy, positional. files_meta[i] is matched to the i-th file
#           purely by index. Works fine in *direct* mode (files_name/files_data
#           are supplied by the caller in a known, fixed order) but is fragile
#           in *path* mode, where the SDK controls chunk/batch ordering
#           (interleaving across files, multi-chunk files, etc.) and the
#           "i-th entry" isn't necessarily the file you think it is.
#
#   dict  — keyed by *original filename* (e.g. "report.pdf"), resolved against
#           each chunk's actual source file regardless of chunking/interleave
#           order, then merged on top of the auto-generated
#           {"filename": ..., "extension": ...} stub. This is the recommended
#           way to attach custom metadata in path mode:
#
#               client.add_documents(
#                   user_id="u1",
#                   vector_lake_description="my_lake",
#                   files_meta={
#                       "report.pdf": {"department": "finance", "year": 2026},
#                       "notes.txt":  {"author": "alice"},
#                   },
#               )
#
#           Files not present as a key simply get the default
#           {"filename", "extension", "timestamp"} stub with no extra fields.
FilesMetaArg = Optional[Union[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]]


# ── Progress bar ───────────────────────────────────────────────────────────────

try:
    from tqdm import tqdm as _tqdm

    def _progress_bar(iterable=None, *, total: int, desc: str, initial: int = 0, unit: str = "it"):
        return _tqdm(
            iterable, total=total, desc=desc, unit=unit,
            initial=initial, dynamic_ncols=True, file=sys.stdout,
        )

except ImportError:
    class _FallbackBar:
        def __init__(self, iterable=None, *, total, desc, initial=0, unit="it"):
            self._it = iterable; self._total = total; self._desc = desc; self._n = initial

        def __iter__(self):
            for item in self._it:
                self._n += 1
                yield item

        def set_postfix_str(self, s, **_):
            pct = int(self._n / self._total * 100) if self._total else 0
            print(f"  [{self._desc}] {self._n}/{self._total} ({pct}%) — {s}", flush=True)

        def update(self, n=1): self._n += n

        # `total` exposed as a plain attribute (mirrors tqdm) so callers can
        # grow the estimate mid-run without caring which bar implementation
        # is in use.
        @property
        def total(self): return self._total

        @total.setter
        def total(self, value): self._total = value

        def refresh(self, **_): pass  # no-op — set_postfix_str prints on every call anyway

        def __enter__(self): return self
        def __exit__(self, *_): pass

    def _progress_bar(iterable=None, *, total, desc, initial=0, unit="it"):
        return _FallbackBar(iterable, total=total, desc=desc, initial=initial, unit=unit)


# ── Utilities ──────────────────────────────────────────────────────────────────

def _md5(filepath: str) -> str:
    """Compute the MD5 hex digest of a file in streaming fashion."""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()




def _source_ext(chunk_name: str, base_path: str) -> str:
    """
    Recover the original source file extension from a chunk filename.

    Parses the chunk stem via ``VersionedChunkName``, then looks for the source
    file in *base_path* to obtain its real extension.  Falls back to ``"txt"``
    if the source file is not found (e.g. direct-upload mode).
    """
    parsed = VersionedChunkName.parse(chunk_name)
    if parsed:
        candidates = list(Path(base_path).glob(f"{parsed.stem}.*"))
        candidates = [c for c in candidates if not VersionedChunkName.is_versioned(c.name)]
        if candidates:
            return candidates[0].suffix.lstrip(".").lower()
    return Path(chunk_name).suffix.lstrip(".").lower() or "txt"


def _resolve_source_filename(chunk_name: str, base_path: str) -> str:
    """
    Recover the original source filename (with its real extension) for a
    chunk file, e.g. ``"report_part2.txt"`` → ``"report.pdf"``.

    Falls back to the chunk name itself when no source file can be located
    on disk (e.g. direct-upload mode, where chunks aren't written at all).

    Used to key user-supplied metadata dicts by filename regardless of how
    a file happens to be split across chunks or reordered across batches.
    """
    parsed = VersionedChunkName.parse(chunk_name)
    if parsed:
        candidates = list(Path(base_path).glob(f"{parsed.stem}.*"))
        candidates = [c for c in candidates if not VersionedChunkName.is_versioned(c.name)]
        if candidates:
            return candidates[0].name
        return parsed.stem
    return chunk_name


def _lookup_meta(
    meta_by_name: Dict[str, Dict[str, Any]], *keys: str
) -> Optional[Dict[str, Any]]:
    """Return the first metadata dict found in *meta_by_name* for any of *keys*."""
    for k in keys:
        if k in meta_by_name:
            return meta_by_name[k]
    return None


def _utcnow_iso() -> str:
    """
    Return the current UTC time as an ISO-8601 string, e.g.
    ``"2026-08-05T14:22:07+00:00"``.

    Used to stamp every file's auto-generated metadata stub with a
    ``"timestamp"`` field by default (see ``_read_batch_contents`` /
    ``_direct_upload``) — one call per batch/upload, so every file
    processed together shares the same value.
    """
    return _dt.now(_timezone.utc).isoformat(timespec="seconds")







# ── Console reporting ──────────────────────────────────────────────────────────

def _print_run_summary(
    operation: str,
    batch_outputs: List[Dict],
    start_batch: int,
    end_batch: int,
    ok_count: int,
    fail_count: int,
    parallel_batches: int = 1,
) -> None:
    """Log a human-readable summary after a batch run completes."""
    total        = len(batch_outputs)
    skip_count   = sum(1 for b in batch_outputs if b.get("server_skip"))
    upload_count = ok_count - skip_count
    latencies    = [b["processing_time"] for b in batch_outputs if b.get("success") or b.get("server_skip")]
    avg_lat      = round(sum(latencies) / len(latencies), 2) if latencies else 0
    max_lat      = round(max(latencies), 2) if latencies else 0
    failures     = [b for b in batch_outputs if not b.get("success") and not b.get("server_skip")]

    status_line = "All batches succeeded" if fail_count == 0 else f"{fail_count} batch(es) failed"
    logger.info(
        "%s COMPLETE — %s | range #%d→#%d | uploaded %d/%d | skipped %d | avg %.2fs max %.2fs",
        operation.upper(), status_line, start_batch, end_batch,
        upload_count, total, skip_count, avg_lat, max_lat,
    )
    for b in failures:
        msg = ""
        if b.get("response"):
            msg = b["response"].get("message") or b["response"].get("error") or ""
        elif b.get("error"):
            msg = (b["error"] or {}).get("message") or (b["error"] or {}).get("error") or ""
        files_str = ", ".join(b.get("files", []))[:60]
        logger.warning("  failed batch #%d  files=%s  error=%s", b["batch_number"], files_str, str(msg)[:80])


def _checkpoint_path(chunks_dir: str, operation: str, vector_lake_description: str) -> Path:
    safe_lake = re.sub(r"[^A-Za-z0-9_-]", "_", vector_lake_description)
    return Path(chunks_dir) / f"checkpoint_{operation}_{safe_lake}.json"


def _load_checkpoint(chunks_dir: str, operation: str, vector_lake_description: str) -> Dict[str, Any]:
    """
    Load the incremental checkpoint for *operation* on *vector_lake_description*,
    if one exists. Returns ``{}`` if there's no checkpoint or it's unreadable
    (corrupt/partial write) — resume degrades to "start from scratch" rather
    than crashing on a bad checkpoint file.
    """
    p = _checkpoint_path(chunks_dir, operation, vector_lake_description)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Checkpoint at %s is unreadable — ignoring, starting fresh.", p)
        return {}


def _write_checkpoint(
    chunks_dir: str,
    operation: str,
    vector_lake_description: str,
    completed_source_files: set,
    ok_count: int,
    fail_count: int,
    total_files: int,
) -> None:
    """
    Persist checkpoint progress to disk. Called after **every** batch/
    minibatch (not just at the end of a run) so an interrupted process —
    Ctrl+C, crash, killed terminal — leaves behind an accurate record of
    which source files were already fully uploaded. This is what
    ``resume=True`` reads on the next call to skip already-done work.

    Unlike ``_save_run_report`` (one JSON per run, written only after the
    run completes, mainly for post-hoc diagnostics), this file has a fixed,
    predictable name per (operation, lake) pair and is overwritten in place
    so it always reflects the latest known-good state.
    """
    p = _checkpoint_path(chunks_dir, operation, vector_lake_description)
    Path(chunks_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "operation":               operation,
        "vector_lake_description": vector_lake_description,
        "updated":                 _dt.now().isoformat(timespec="seconds"),
        "completed_source_files":  sorted(completed_source_files),
        "ok_count":                ok_count,
        "fail_count":              fail_count,
        "total_files":             total_files,
    }
    # Write to a temp file then replace — avoids a half-written checkpoint
    # if the process is killed mid-write.
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(p)


def _apply_resume_filter(
    files: List[str],
    chunks_dir: str,
    operation: str,
    vector_lake_description: str,
    resume: bool,
) -> tuple:
    """
    If *resume* is True, drop any file from *files* whose stem is already
    recorded as completed in the checkpoint for (operation, lake).

    Returns (filtered_files, already_completed_set) — the latter is seeded
    into the running "completed" set so newly-completed files get appended
    to it rather than overwriting history on the next checkpoint write.
    """
    if not resume:
        return files, set()

    checkpoint = _load_checkpoint(chunks_dir, operation, vector_lake_description)
    done = set(checkpoint.get("completed_source_files", []))
    if not done:
        return files, set()

    remaining = [f for f in files if Path(f).stem not in done]
    skipped = len(files) - len(remaining)
    if skipped:
        logger.info(
            "resume=True: skipping %d already-completed file(s) from checkpoint "
            "(%d remaining).", skipped, len(remaining),
        )
    return remaining, done

def _save_run_report(
    chunks_dir: str,
    operation: str,
    batch_outputs: List[Dict],
    start_batch: int,
    end_batch: int,
    ok_count: int,
    fail_count: int,
    parallel_batches: int = 1,
) -> str:
    """
    Persist a JSON run report to *chunks_dir* and return the run ID.

    The report captures timing, per-batch outcomes, and error messages so
    failed runs can be diagnosed and retried without re-uploading everything.
    """
    run_id    = _uuid_mod.uuid4().hex[:6]
    timestamp = _dt.now().isoformat(timespec="seconds")
    report    = {
        "run_id":           run_id,
        "timestamp":        timestamp,
        "operation":        operation,
        "parallel_batches": parallel_batches,
        "start_batch":      start_batch,
        "end_batch":        end_batch,
        "successful":       ok_count,
        "failed":           fail_count,
        "server_skipped":   sum(1 for b in batch_outputs if b.get("server_skip")),
        "batches": [
            {
                "batch_number":    b["batch_number"],
                "success":         b["success"],
                "server_skip":     b.get("server_skip", False),
                "files":           b.get("files", []),
                "processing_time": b.get("processing_time"),
                "elapsed_ms":      b.get("elapsed_ms"),
                "error": (
                    (b.get("response") or {}).get("message")
                    or (b.get("error") or {}).get("message")
                ) if not b.get("success") and not b.get("server_skip") else None,
            }
            for b in sorted(batch_outputs, key=lambda x: x["batch_number"])
        ],
    }
    Path(chunks_dir).mkdir(parents=True, exist_ok=True)
    report_path = Path(chunks_dir) / f"run_{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    logger.info("Run report → %s", report_path)
    return run_id


# ── Main client ────────────────────────────────────────────────────────────────

class VectorLakeClient:
    """
    Main entry point for the VectorLake SDK.

    Parameters
    ----------
    config:
        A pre-built :class:`~vectorlake.config.Config` instance.  When omitted
        the client constructs one from *kwargs* (which are forwarded verbatim).

    Image OCR support
    -----------------
    When ``enable_ocr=True`` (the default), PDF and DOCX files have their
    embedded images OCR'd automatically.  Standalone image files (PNG, JPG,
    TIFF, …) are also supported and will be OCR'd into ``.txt`` chunks.

    OCR-derived text spans are wrapped in structured markers inside the chunk
    files so you (or the server) can always tell which text came from an image::

        <<<IMAGE_EXTRACT source="report.pdf" page=2 image_index=1 method="embedded_image_ocr">>>
        ... OCR text ...
        <<<END_IMAGE_EXTRACT>>>

    To work with these markers programmatically::

        from vectorlake.extractors import strip_image_markers, list_image_regions

        chunk_text = Path("upload/chunks/report_part1.txt").read_text()
        regions    = list_image_regions(chunk_text)   # list of dicts
        clean_text = strip_image_markers(chunk_text)  # markers removed, OCR text kept

    Processing modes
    ----------------
    Pass ``processing_mode="streaming"`` to any upload method to overlap chunk
    extraction with API calls.  The default ``"sequential"`` mode preserves the
    original behaviour (all chunks first, then upload) and supports
    checkpoint/resume via ``start_from_batch`` / ``end_batch``.
    """

    def __init__(self, config: Optional[Config] = None, **kwargs):
        if config is None:
            config = Config(**kwargs)

        self.config = config
        logger.info("VectorLakeClient ready — %s", config)

        self._cloud_extractor = None
        if _CLOUD_FACTORY_AVAILABLE and getattr(config, "cloud_ocr_provider", None):
            try:
                self._cloud_extractor = CloudOCRFactory.build(config)
                logger.info(
                    "Cloud OCR engine: %s (strategy=%s)",
                    config.cloud_ocr_provider,
                    config.cloud_ocr_strategy,
                )
            except Exception as exc:
                logger.warning("Could not initialise cloud OCR extractor: %s", exc)

        self._logger = Logger(config.log_dir)
        self._batch_manager = BatchManager(
            max_files=config.max_files_per_batch,
            max_size_mb=config.max_batch_size_mb,
            allowed_extensions=config.allowed_extensions,
            enable_ocr=config.enable_ocr,
            cloud_extractor=self._cloud_extractor,
            cloud_strategy=getattr(config, "cloud_ocr_strategy", "hybrid"),
            cloud_threshold=getattr(config, "cloud_ocr_threshold", 100),
            streaming_minibatch_files=getattr(config, "streaming_minibatch_files", 10),
            streaming_minibatch_size_mb=getattr(config, "streaming_minibatch_size_mb", 5),
        )
        self._file_processor = FileProcessor(
            config.allowed_extensions,
            enable_ocr=config.enable_ocr,
            cloud_extractor=self._cloud_extractor,
            cloud_strategy=getattr(config, "cloud_ocr_strategy", "hybrid"),
            cloud_threshold=getattr(config, "cloud_ocr_threshold", 100),
        )

    # ── Internal: HTTP ────────────────────────────────────────────────────────

    def _headers(self) -> Dict[str, str]:
        """Return the standard HTTP headers required for all API calls."""
        return {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
        }

    def _make_request(
        self,
        endpoint: str,
        payload: Dict[str, Any],
        operation: str = "",
        batch_num: int = 0,
    ) -> Dict[str, Any]:
        """
        POST *payload* to *endpoint* with automatic retry and exponential backoff.

        Retries on HTTP 429 (respecting ``Retry-After``) and transient network
        errors up to ``config.max_retries`` attempts.  Always returns a plain
        ``dict`` — never raises; HTTP 4xx/5xx is folded into the error schema
        ``{"success": False, "error": ..., "message": ...}``.

        Parameters
        ----------
        endpoint:
            Fully-qualified URL to POST to.
        payload:
            JSON-serialisable request body.
        operation:
            Human-readable label used for performance logging.
        batch_num:
            Batch index used for log correlation.
        """
        headers    = self._headers()
        request_kb = len(json.dumps(payload).encode()) / 1024
        logger.debug("Batch %d — payload %.1f KB", batch_num, request_kb)
        last_error: Optional[APIError] = None

        for attempt in range(self.config.max_retries):
            try:
                t0   = time.time()
                resp = requests.post(
                    endpoint, json=payload, headers=headers, timeout=self.config.timeout,
                )
                latency_ms = (time.time() - t0) * 1000
                logger.debug("Batch %d — HTTP %d in %.0f ms", batch_num, resp.status_code, latency_ms)

                try:
                    body = resp.json()
                except Exception:
                    body = {"status_code": resp.status_code, "text": resp.text}

                if operation:
                    self._logger.log_performance(
                        operation=operation,
                        batch_num=batch_num,
                        latency_ms=round(latency_ms, 1),
                        request_kb=round(request_kb, 1),
                        response_kb=round(len(resp.content) / 1024, 1),
                        http_status=resp.status_code,
                    )

                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning(
                        "Rate-limited on batch %d (attempt %d); retrying in %ds",
                        batch_num, attempt + 1, wait,
                    )
                    if operation:
                        self._logger.log_api_error(
                            operation, batch_num,
                            f"ThrottleError: rate-limited, waiting {wait}s",
                        )
                    time.sleep(wait)
                    last_error = ThrottleError()
                    continue

                if resp.status_code >= 400:
                    msg = (
                        body.get("message", f"HTTP {resp.status_code}")
                        if isinstance(body, dict) else resp.text
                    )
                    err = APIError(msg, resp.status_code, resp.text)
                    if operation:
                        self._logger.log_api_error(operation, batch_num, str(err))
                    return err.to_response()

                return body

            except requests.exceptions.Timeout as exc:
                wait = 2 ** attempt
                logger.warning("Timeout on attempt %d; retrying in %ds", attempt + 1, wait)
                last_error = APIError(f"Request timed out: {exc}")
                time.sleep(wait)

            except requests.exceptions.ConnectionError as exc:
                wait = 2 ** attempt
                logger.warning("Connection error on attempt %d; retrying in %ds", attempt + 1, wait)
                last_error = APIError(f"Connection error: {exc}")
                time.sleep(wait)

            except requests.exceptions.RequestException as exc:
                err = APIError(str(exc))
                if operation:
                    self._logger.log_api_error(operation, batch_num, str(err))
                return err.to_response()

        final_error = last_error or APIError("Max retries exceeded")
        if operation:
            self._logger.log_api_error(operation, batch_num, str(final_error))
        return final_error.to_response()

    # ── Internal: chunk helpers ───────────────────────────────────────────────

    def _compute_grand_total(self, base_path: str) -> int:
        """
        Estimate the total number of upload batches for the given *base_path*.

        Existing chunks on disk are counted directly to avoid re-running OCR.
        If no chunks exist yet the source files are used to produce an estimate.
        The chunks directory is also cleaned of stale entries before counting.
        """
        chunks_dir = Path(base_path) / "chunks"

        existing_chunks = list(chunks_dir.glob("*_part*.txt"))
        if existing_chunks:
            names = [c.name for c in existing_chunks]
            batches = []
            current: List[str] = []
            for name in names:
                current.append(name)
                if len(current) >= self._batch_manager.max_files:
                    batches.append(current)
                    current = []
            if current:
                batches.append(current)
            return len(batches)

        all_files = [
            f for f in os.listdir(base_path)
            if os.path.isfile(os.path.join(base_path, f))
            and self._file_processor.is_supported(f)
        ]
        if not all_files:
            return 0
        all_batches, _ = self._batch_manager.create_batches(all_files, base_path)
        return len(all_batches)

    # ── Internal: sequential batch processing ────────────────────────────────

    def _process_files_in_batches(
        self,
        operation: str,
        user_id: str,
        vector_lake_description: str,
        start_from_batch: int = 1,
        end_batch: Optional[int] = None,
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
        batch_delay: float = 1.0,
        grand_total_batches: Optional[int] = None,
        files_meta: FilesMetaArg = None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        Upload files to *operation* sequentially, one batch at a time.

        All source files are chunked upfront by :class:`~utils.BatchManager`
        before any HTTP call is made.  Batches are then sent in order with an
        optional *batch_delay* between each to avoid overwhelming the server.
        Progress is reported via a live progress bar.

        This is the ``"sequential"`` processing mode.

        Parameters
        ----------
        operation:
            Endpoint key (``"add_docs"``, ``"refresh_docs"``, ``"insert_docs"``).
        user_id:
            Caller identity forwarded to the server.
        vector_lake_description:
            Namespace / lake identifier.
        start_from_batch:
            Resume from this 1-based batch number (inclusive). Requires you
            to already know the batch number — prefer *resume* (below) for
            automatic, crash-safe resume instead.
        end_batch:
            Stop after this 1-based batch number (inclusive).  ``None`` means
            process all batches.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        files:
            Explicit list of source filenames to process.  When ``None`` every
            supported file in ``config.vector_lake_path`` is used.
        batch_delay:
            Seconds to sleep between consecutive batch uploads.
        grand_total_batches:
            Override the displayed total for progress reporting (useful when
            this call is part of a larger multi-phase run).
        files_meta:
            Optional per-file metadata, forwarded verbatim to the server.
            Accepts either a positional list (aligned to file/chunk order —
            fragile in path mode, see module-level ``FilesMetaArg`` docs) or
            a dict keyed by original filename (e.g. ``{"report.pdf": {...}}``),
            which is resolved per-chunk against its real source file and
            merged onto the auto-generated ``{filename, extension, timestamp}`` stub —
            the recommended way to attach custom metadata in path mode.
        resume:
            If ``True``, skip source files already recorded as completed in
            this operation/lake's checkpoint file (written incrementally
            after every batch — see ``_write_checkpoint``). Unlike
            *start_from_batch*, this survives an actual crash or Ctrl+C
            mid-run: just call again with ``resume=True`` and it picks up
            where it left off automatically, no batch number required.
        """
        base_path  = self.config.vector_lake_path
        chunks_dir = str(Path(base_path) / "chunks")

        if files is None:
            files = [
                f for f in os.listdir(base_path)
                if os.path.isfile(os.path.join(base_path, f))
                and self._file_processor.is_supported(f)
            ]

        # Rename any source file with an unsafe/messy name to its cleaned
        # equivalent *on disk* before anything reads it, so chunk filenames,
        # MD5 sidecars, and checkpoint entries are all derived from the same
        # clean name — not the raw name from the source directory. Also
        # applies when the caller passed `files=` explicitly.
        files = sanitize_filenames(base_path, files)

        files, _already_done = _apply_resume_filter(
            files, chunks_dir, operation, vector_lake_description, resume,
        )
        completed_source_files = set(_already_done)
        _total_files_for_checkpoint = len(files) + len(_already_done)

        if not files:
            if _already_done:
                return {
                    "success": True,
                    "message": "All files already completed per checkpoint — nothing to do.",
                    "already_completed": sorted(_already_done),
                }
            return {
                "success": False,
                "error":   "NoFilesError",
                "message": "No supported files found in upload directory.",
            }

        batches, chunks_dir = self._batch_manager.create_batches(
            files, base_path, extraction_workers=getattr(self.config, "extraction_workers", 4),
        )
        total_batches = len(batches)
        display_total = grand_total_batches or total_batches
        total_chunks  = sum(len(b) for b in batches)

        resolved_end = min(end_batch, total_batches) if end_batch is not None else total_batches
        if resolved_end < start_from_batch:
            return {
                "success": False,
                "error":   "InvalidRangeError",
                "message": f"end_batch ({resolved_end}) must be >= start_from_batch ({start_from_batch}).",
            }

        self._print_run_header(
            operation, files, total_chunks, total_batches, display_total,
            start_from_batch, resolved_end, workers=1,
        )

        endpoint      = self.config.endpoints[operation]
        batch_outputs: List[Dict] = []
        ok_count = fail_count = 0
        active   = [
            (i, b) for i, b in enumerate(batches)
            if start_from_batch <= (i + 1) <= resolved_end
        ]
        file_offset = 0
        _t_run_start = time.time()

        with _progress_bar(
            active, total=display_total, desc=operation,
            initial=start_from_batch - 1, unit="batch",
        ) as bar:
            for i, batch in bar:
                batch_num = i + 1
                t0        = time.time()

                try:
                    if operation == "refresh_docs":
                        self._regenerate_chunks_for_refresh(batch, base_path, chunks_dir)

                    file_contents, files_meta_batch = self._read_batch_contents(
                        batch, base_path, chunks_dir, files_meta, file_offset,
                    )
                    file_offset += _unique_source_count(batch)

                    payload = {
                        "session_id":               session_id,
                        "user_id":                  user_id,
                        "vector_lake_description":  vector_lake_description,
                        "files_name":               batch,
                        "files_data":               file_contents,
                        "files_meta":               files_meta_batch,
                        "intelligent_segmentation": intelligent_segmentation,
                    }

                    t_req    = time.time()
                    result   = self._make_request(endpoint, payload, operation, batch_num)
                    elapsed_ms = round((time.time() - t_req) * 1000, 1)
                    elapsed    = round(time.time() - t0, 2)

                    has_error = bool(result.get("error"))
                    succeeded = not has_error

                    chunks_str   = ", ".join(batch)
                    source_stems = sorted({f.split("_part")[0] for f in batch})
                    stems_str    = ", ".join(source_stems)

                    if succeeded:
                        ok_count += 1
                        status = "✓"
                        completed_source_files.update(source_stems)
                        _write_checkpoint(
                            chunks_dir, operation, vector_lake_description,
                            completed_source_files, ok_count, fail_count,
                            _total_files_for_checkpoint,
                        )
                    else:
                        fail_count += 1
                        status = "✗"
                        logger.warning(
                            "Batch %d/%d FAILED  sources=[%s]  chunks=[%s]  error=%s: %s",
                            batch_num, display_total,
                            stems_str, chunks_str,
                            result.get("error", "UnknownError"),
                            result.get("message", ""),
                        )

                    # Concise per-batch log with running ETA
                    batches_done = ok_count + fail_count
                    elapsed_run  = time.time() - _t_run_start
                    rate_b       = batches_done / elapsed_run if elapsed_run > 0 else 0.0
                    remaining    = display_total - (start_from_batch - 1) - batches_done
                    eta_s        = remaining / rate_b if rate_b > 0 else float("inf")
                    eta_str      = f"{eta_s:.0f}s" if eta_s != float("inf") else "?"
                    logger.info(
                        "Batch %d/%d  %s  %.1fms  ok=%d fail=%d  ETA %s  sources=[%s]  chunks=[%s]",
                        batch_num, display_total, status, elapsed_ms,
                        ok_count, fail_count, eta_str, stems_str, chunks_str,
                    )

                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         succeeded,
                        "server_skip":     bool(result.get("server_skip")),
                        "processing_time": elapsed,
                        "elapsed_ms":      elapsed_ms,
                        "response":        result,
                    })
                    bar.set_postfix_str(
                        f"{status} {elapsed_ms:.0f}ms  ok={ok_count} fail={fail_count}  ETA {eta_str}"
                    )

                except VectorLakeError as exc:
                    elapsed    = round(time.time() - t0, 2)
                    elapsed_ms = round((time.time() - t0) * 1000, 1)
                    fail_count += 1
                    logger.error("Batch %d/%d error: %s", batch_num, display_total, exc)
                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         False,
                        "processing_time": elapsed,
                        "elapsed_ms":      elapsed_ms,
                        "response":        None,
                        "error":           exc.to_response(),
                    })
                    bar.set_postfix_str(f"✗ VectorLakeError  fail={fail_count}")

                except Exception as exc:
                    elapsed    = round(time.time() - t0, 2)
                    elapsed_ms = round((time.time() - t0) * 1000, 1)
                    fail_count += 1
                    logger.exception("Batch %d/%d unexpected error", batch_num, display_total)
                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         False,
                        "processing_time": elapsed,
                        "elapsed_ms":      elapsed_ms,
                        "response":        None,
                        "error":           {"error": type(exc).__name__, "message": str(exc)},
                    })
                    bar.set_postfix_str(f"✗ {type(exc).__name__}  fail={fail_count}")

                if batch_num < resolved_end:
                    time.sleep(batch_delay)

        _print_run_summary(
            operation, batch_outputs, start_from_batch, resolved_end,
            ok_count, fail_count, parallel_batches=1,
        )
        run_id = _save_run_report(
            chunks_dir, operation, batch_outputs, start_from_batch, resolved_end,
            ok_count, fail_count, parallel_batches=1,
        )

        return self._build_run_result(
            "batch", run_id, total_batches, start_from_batch, resolved_end,
            ok_count, fail_count, batch_outputs,
        )

    # ── Internal: streaming batch processing ──────────────────────────────────

    def _process_files_streaming(
        self,
        operation: str,
        user_id: str,
        vector_lake_description: str,
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
        batch_delay: float = 0.5,
        files_meta: FilesMetaArg = None,
        on_batch_success=None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        Upload files using the streaming pipeline: extract one file at a time
        and upload each minibatch as soon as it is ready.

        Unlike the sequential mode (which blocks until ALL files are chunked
        before the first HTTP call), this method begins uploading the moment
        the first ``streaming_minibatch_files`` chunks (or
        ``streaming_minibatch_size_mb`` MB) have been extracted.  For
        collections with expensive OCR or cloud extraction this can cut
        total wall-clock time dramatically.

        Minibatch size is controlled by ``Config.streaming_minibatch_files``
        and ``Config.streaming_minibatch_size_mb``.

        Note: ``start_from_batch`` / ``end_batch`` are not supported here
        because minibatch numbers aren't known upfront (they depend on how
        extraction happens to chunk incoming files). Use *resume* instead —
        see below — which works by source filename rather than batch number,
        so it applies equally to streaming and sequential mode.

        Parameters
        ----------
        operation:
            Endpoint key (``"add_docs"``, ``"refresh_docs"``, ``"insert_docs"``).
        user_id:
            Caller identity forwarded to the server.
        vector_lake_description:
            Namespace / lake identifier.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        files:
            Explicit list of source filenames to process.  When ``None`` every
            supported file in ``config.vector_lake_path`` is used.
        batch_delay:
            Seconds to sleep between consecutive minibatch uploads.  Shorter
            than the sequential default (1.0 s) since minibatches are smaller.
        files_meta:
            Optional per-file metadata, forwarded verbatim to the server.
            Accepts either a positional list or a dict keyed by original
            filename — see module-level ``FilesMetaArg`` docs. The dict form
            is recommended here since streaming order is determined by
            extraction completion time, not input order.
        resume:
            If ``True``, skip source files already recorded as completed in
            this operation/lake's checkpoint file, written incrementally
            after every minibatch. If a streaming run is interrupted —
            Ctrl+C, crash, killed terminal — call again with ``resume=True``
            and it picks up from the last minibatch that finished, without
            needing to know any batch number (there isn't a stable one in
            streaming mode).

        Returns
        -------
        dict
            Standard run-result dict (same schema as sequential / parallel).
        """
        base_path       = self.config.vector_lake_path
        chunks_dir_path = str(Path(base_path) / "chunks")

        if files is None:
            files = [
                f for f in os.listdir(base_path)
                if os.path.isfile(os.path.join(base_path, f))
                and self._file_processor.is_supported(f)
            ]

        # Same rename-to-clean-name step as sequential mode — streaming
        # previously skipped this entirely, so chunk files inherited
        # whatever messy name the source directory happened to have.
        files = sanitize_filenames(base_path, files)

        files, _already_done = _apply_resume_filter(
            files, chunks_dir_path, operation, vector_lake_description, resume,
        )
        completed_source_files = set(_already_done)
        _total_files_for_checkpoint = len(files) + len(_already_done)

        if not files:
            if _already_done:
                return {
                    "success": True,
                    "message": "All files already completed per checkpoint — nothing to do.",
                    "already_completed": sorted(_already_done),
                }
            return {
                "success": False,
                "error":   "NoFilesError",
                "message": "No supported files found in upload directory.",
            }

        endpoint = self.config.endpoints[operation]
        batch_outputs: List[Dict] = []
        ok_count = fail_count = 0
        batch_num = 0
        file_offset = 0
        _t_run_start = time.time()

        cloud_label = (
            f"{self.config.cloud_ocr_provider} ({self.config.cloud_ocr_strategy})"
            if getattr(self.config, "cloud_ocr_provider", None)
            else "disabled"
        )
        _workers = getattr(self.config, "extraction_workers", 4)
        _smb = self._batch_manager.streaming_minibatch_bytes
        _smb_str = f"{_smb // 1024} KB" if _smb < 1024 * 1024 else f"{_smb // (1024*1024)} MB"
        logger.info(
            "%s [streaming] | files=%d minibatch=%d chunks/%s ocr=%s cloud=%s workers=%d",
            operation.upper(), len(files),
            self._batch_manager.streaming_minibatch_files, _smb_str,
            self.config.enable_ocr, cloud_label, _workers,
        )

        # Progress is tracked by *distinct source file* completed, not by raw
        # batch count. A single large/many-page file can explode into far
        # more chunks — and therefore far more minibatches — than there are
        # files (streaming_minibatch_files/streaming_minibatch_bytes cap each
        # yielded batch, with no relationship to file boundaries). Driving
        # the bar off batch count made `total=len(files)` a guess that blew
        # past its own denominator mid-run. Tracking files instead means the
        # denominator (445, in your run) is exact from the start and never
        # needs to be revised.
        _files_progressed: set = set()

        with _progress_bar(
            total=len(files),
            desc=f"{operation}[stream]",
            unit="file",
        ) as bar:
            for batch, chunks_dir in self._batch_manager.streaming_create_batches(
                files, base_path, extraction_workers=_workers,
            ):
                # Sleep *between* batches only
                if batch_num > 0:
                    time.sleep(batch_delay)
                batch_num += 1
                t0 = time.time()

                # Computed up front (depends only on `batch`, not on the
                # request outcome) so it's available even if an exception is
                # raised before a result comes back.
                chunks_str   = ", ".join(batch)
                source_stems = sorted({f.split("_part")[0] for f in batch})
                stems_str    = ", ".join(source_stems)

                try:
                    # refresh_docs must regenerate chunks before re-uploading
                    if operation == "refresh_docs":
                        self._regenerate_chunks_for_refresh(batch, base_path, chunks_dir)

                    file_contents, files_meta_batch = self._read_batch_contents(
                        batch, base_path, chunks_dir, files_meta, file_offset,
                    )
                    file_offset += _unique_source_count(batch)

                    payload = {
                        "session_id":               session_id,
                        "user_id":                  user_id,
                        "vector_lake_description":  vector_lake_description,
                        "files_name":               batch,
                        "files_data":               file_contents,
                        "files_meta":               files_meta_batch,
                        "intelligent_segmentation": intelligent_segmentation,
                    }

                    t_req      = time.time()
                    result     = self._make_request(endpoint, payload, operation, batch_num)
                    elapsed_ms = round((time.time() - t_req) * 1000, 1)
                    elapsed    = round(time.time() - t0, 2)

                    succeeded = not bool(result.get("error"))

                    if succeeded:
                        ok_count += 1
                        status = "✓"
                        completed_source_files.update(source_stems)
                        _write_checkpoint(
                            chunks_dir_path, operation, vector_lake_description,
                            completed_source_files, ok_count, fail_count,
                            _total_files_for_checkpoint,
                        )
                        if on_batch_success is not None:
                            on_batch_success(batch)
                    else:
                        fail_count += 1
                        status = "✗"
                        logger.warning(
                            "Batch #%d FAILED  sources=[%s]  chunks=[%s]  error=%s: %s",
                            batch_num,
                            stems_str, chunks_str,
                            result.get("error", "UnknownError"),
                            result.get("message", ""),
                        )

                    logger.info(
                        "Batch #%d  %s  %.1fms  ok=%d fail=%d  sources=[%s]  chunks=[%s]",
                        batch_num, status, elapsed_ms, ok_count, fail_count,
                        stems_str, chunks_str,
                    )
                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         succeeded,
                        "server_skip":     bool(result.get("server_skip")),
                        "processing_time": elapsed,
                        "elapsed_ms":      elapsed_ms,
                        "response":        result,
                    })
                    bar.set_postfix_str(
                        f"{status} {elapsed_ms:.0f}ms  ok={ok_count} fail={fail_count}"
                    )

                except VectorLakeError as exc:
                    elapsed = round(time.time() - t0, 2)
                    fail_count += 1
                    logger.error("Stream #%d error: %s", batch_num, exc)
                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         False,
                        "processing_time": elapsed,
                        "elapsed_ms":      round(elapsed * 1000, 1),
                        "response":        None,
                        "error":           exc.to_response(),
                    })
                    bar.set_postfix_str(f"✗ VectorLakeError  fail={fail_count}")

                except Exception as exc:
                    elapsed = round(time.time() - t0, 2)
                    fail_count += 1
                    logger.exception("Stream #%d unexpected error", batch_num)
                    batch_outputs.append({
                        "batch_number":    batch_num,
                        "files":           batch,
                        "success":         False,
                        "processing_time": elapsed,
                        "elapsed_ms":      round(elapsed * 1000, 1),
                        "response":        None,
                        "error":           {"error": type(exc).__name__, "message": str(exc)},
                    })
                    bar.set_postfix_str(f"✗ {type(exc).__name__}  fail={fail_count}")

                finally:
                    # Advance the bar by whichever of this batch's source
                    # files we haven't already counted. A file's chunks can
                    # be spread across several batches, so only count each
                    # stem the first time it's seen — otherwise a 30-chunk
                    # file would advance the bar 30 times over.
                    _new = [s for s in source_stems if s not in _files_progressed]
                    if _new:
                        _files_progressed.update(_new)
                        bar.update(len(_new))

        # Files whose extraction failed outright (e.g. OCR skipped/empty —
        # see the "Skipping '<file>'" log lines) never produce a single chunk,
        # so they never appear in any batch above and the bar would otherwise
        # stall short of 100%. Top it up now that the run is finished so the
        # denominator and the final count always agree.
        _remaining = len(files) - len(_files_progressed)
        if _remaining > 0:
            bar.update(_remaining)

        total_batches = batch_num or 1
        _print_run_summary(
            operation, batch_outputs, 1, total_batches,
            ok_count, fail_count, parallel_batches=1,
        )
        # Always use the pre-computed chunks_dir_path — the loop variable
        # `chunks_dir` is only set when at least one batch was yielded, so
        # referencing it here caused a NameError on zero-batch runs.
        run_id = _save_run_report(
            chunks_dir_path, operation, batch_outputs, 1, total_batches,
            ok_count, fail_count, parallel_batches=1,
        )

        return self._build_run_result(
            "streaming", run_id, total_batches, 1, total_batches,
            ok_count, fail_count, batch_outputs,
        )

    # ── Internal: shared helpers ──────────────────────────────────────────────

    def _print_run_header(
        self,
        operation: str,
        files: List[str],
        total_chunks: int,
        total_batches: int,
        display_total: int,
        start_from_batch: int,
        resolved_end: int,
        workers: int = 1,
    ) -> None:
        """Log the pre-run summary before the progress bar starts."""
        mode_tag = f" [parallel × {workers}]" if workers > 1 else ""
        cloud_label = (
            f"{self.config.cloud_ocr_provider} ({self.config.cloud_ocr_strategy})"
            if getattr(self.config, "cloud_ocr_provider", None)
            else "disabled"
        )
        logger.info(
            "%s%s | files=%d chunks=%d batches=%d range=#%d→#%d ocr=%s cloud=%s",
            operation.upper(), mode_tag, len(files), total_chunks, total_batches,
            start_from_batch, resolved_end, self.config.enable_ocr, cloud_label,
        )

    def _read_batch_contents(
        self,
        batch: List[str],
        base_path: str,
        chunks_dir: str,
        files_meta: FilesMetaArg = None,
        file_offset: int = 0,
    ) -> tuple[List[str], List[Dict[str, Any]]]:
        """
        Read chunk file contents and assemble per-file metadata for *batch*.

        Chunk files are looked up first in *chunks_dir*, then in *base_path*
        as a fallback (for direct-mode uploads).

        Metadata resolution
        -------------------
        - If *files_meta* is a **dict** (keyed by original filename, e.g.
          ``"report.pdf"``), each chunk's real source filename is resolved
          via :func:`_resolve_source_filename` and looked up in the dict
          (trying the source filename, its stem, then the chunk name itself).
          Any match is merged on top of the auto-generated
          ``{"filename": chunk_name, "extension": ...}`` stub. This is
          order-independent — safe under interleaving/chunk-splitting.
        - If *files_meta* is a **list**, entries are taken positionally via
          ``file_offset + position_in_batch`` (legacy behaviour).
        - If *files_meta* is ``None``, only the auto-generated stub is used.

        Parameters
        ----------
        batch:
            List of chunk basenames to read.
        base_path:
            Root directory for source files.
        chunks_dir:
            Directory where chunked ``.txt`` files live.
        files_meta:
            Optional per-file metadata — list (positional) or dict (keyed by
            filename). See module-level ``FilesMetaArg`` docs.
        file_offset:
            Absolute index of the first file in *batch* within *files_meta*
            when *files_meta* is a list.

        Returns
        -------
        (file_contents, files_meta_batch)
            Two parallel lists — one with file text, one with metadata dicts.
        """
        file_contents: List[str] = []
        files_meta_batch: List[Dict[str, Any]] = []

        meta_by_name = files_meta if isinstance(files_meta, dict) else None
        # One timestamp per call — every file in this batch gets the same
        # value, reflecting when this batch was processed/uploaded.
        batch_timestamp = _utcnow_iso()

        for idx, fname in enumerate(batch):
            chunk_path = os.path.join(chunks_dir, fname)
            if not os.path.exists(chunk_path):
                chunk_path = os.path.join(base_path, fname)
            file_contents.append(self._file_processor.read_content(chunk_path))

            stub = {
                "filename":  fname,
                "extension": _source_ext(fname, base_path),
                "timestamp": batch_timestamp,
            }

            if meta_by_name is not None:
                source_name = _resolve_source_filename(fname, base_path)
                source_stem = Path(source_name).stem
                user_meta = _lookup_meta(meta_by_name, source_name, source_stem, fname)
                if user_meta:
                    stub.update(user_meta)
                files_meta_batch.append(stub)
            elif files_meta is not None and (file_offset + idx) < len(files_meta):
                files_meta_batch.append(files_meta[file_offset + idx])
            else:
                files_meta_batch.append(stub)

        return file_contents, files_meta_batch

    def _regenerate_chunks_for_refresh(
        self,
        batch: List[str],
        base_path: str,
        chunks_dir: str,
    ) -> None:
        """
        Delete and regenerate on-disk chunks for all source files referenced
        by *batch* before a ``refresh_docs`` upload.

        For each chunk filename in *batch* the corresponding source file is
        located, its existing chunks are purged, and
        :meth:`~utils.BatchManager.create_batches` is called to re-extract
        content.  This ensures the server always receives the latest version of
        each document during a refresh operation.
        """
        sources_to_reprocess: set = set()
        for fname in batch:
            parsed = VersionedChunkName.parse(fname)
            if parsed:
                source_candidates = [
                    c for c in Path(base_path).glob(f"{parsed.stem}.*")
                    if not VersionedChunkName.is_versioned(c.name)
                    and c.suffix.lower() != ".md5"
                ]
                if source_candidates:
                    sources_to_reprocess.add(source_candidates[0].name)

        if sources_to_reprocess:
            for source_name in sources_to_reprocess:
                stem = Path(source_name).stem
                for old_chunk in Path(chunks_dir).glob(f"{stem}_part*.txt"):
                    logger.debug("Deleting stale chunk: %s", old_chunk)
                    try:
                        old_chunk.unlink()
                    except Exception as exc:
                        logger.warning("Could not delete chunk %s: %s", old_chunk.name, exc)
            logger.info(
                "[refresh] Re-extracting %d source file(s): %s",
                len(sources_to_reprocess), sorted(sources_to_reprocess),
            )
            self._batch_manager.create_batches(list(sources_to_reprocess), base_path)
            logger.info(
                "[refresh] Chunk regeneration complete for: %s", sorted(sources_to_reprocess)
            )

    @staticmethod
    def _build_run_result(
        mode: str,
        run_id: str,
        total_batches: int,
        start_batch: int,
        end_batch: int,
        ok_count: int,
        fail_count: int,
        batch_outputs: List[Dict],
        parallel_batches: int = 1,
    ) -> Dict[str, Any]:
        """
        Build the standard return dict for a completed batch run.

        All public upload methods return this structure so callers can
        inspect outcomes uniformly regardless of processing mode.
        """
        result = {
            "mode":                   mode,
            "run_id":                 run_id,
            "total_batches":          total_batches,
            "start_batch":            start_batch,
            "end_batch":              end_batch,
            "successful_batches":     ok_count,
            "failed_batches":         fail_count,
            "failed_batch_nums":      [
                b["batch_number"] for b in batch_outputs
                if not b["success"]
            ],
            "batches":                sorted(batch_outputs, key=lambda x: x["batch_number"]),
        }
        if parallel_batches > 1:
            result["parallel_batches"] = parallel_batches
        return result

    # ── Internal: direct upload ───────────────────────────────────────────────

    def _direct_upload(
        self,
        operation: str,
        user_id: str,
        vector_lake_description: str,
        files_name: List[str],
        files_data: List[str],
        intelligent_segmentation: bool,
        session_id: Optional[str],
        batch_num: int = 1,
        files_meta: FilesMetaArg = None,
    ) -> Dict[str, Any]:
        """
        Send a single, pre-assembled batch to *operation* without touching disk.

        The caller supplies both filenames and content directly.  No chunking,
        no sidecar writing, no progress bar.  Used as the implementation of
        ``run_insert_path_direct`` and as the fast path in ``add_documents``
        and ``refresh_documents`` when ``files_name``/``files_data`` are given.

        Parameters
        ----------
        operation:
            Endpoint key (e.g. ``"insert_docs"``).
        user_id:
            Caller identity forwarded to the server.
        vector_lake_description:
            Namespace / lake identifier.
        files_name:
            Ordered list of filenames (basenames extracted automatically).
        files_data:
            Ordered list of file contents.  Must be the same length as
            ``files_name``.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        batch_num:
            Batch index used for log correlation (default 1).
        files_meta:
            Optional per-file metadata. Accepts either:
              - a **list**, positionally aligned with *files_name* (used
                as-is, no merging with the auto {filename, extension, timestamp} stub —
                include those keys yourself if you need them); or
              - a **dict** keyed by filename (basename or as given in
                *files_name*), which is merged on top of the auto-generated
                ``{"filename", "extension", "timestamp"}`` stub for each file.
            ``None`` auto-generates ``{filename, extension, timestamp}`` stubs for
            every file.
        """
        if len(files_name) != len(files_data):
            return ValidationError(
                "files_name and files_data must have the same length"
            ).to_response()

        meta_by_name = files_meta if isinstance(files_meta, dict) else None
        # One timestamp for this direct-upload call — shared across every
        # file in files_name/files_data.
        call_timestamp = _utcnow_iso()

        if meta_by_name is not None:
            resolved_meta = []
            for n in files_name:
                base = os.path.basename(n)
                stub = {
                    "filename":  base,
                    "extension": Path(base).suffix.lstrip(".").lower(),
                    "timestamp": call_timestamp,
                }
                user_meta = _lookup_meta(meta_by_name, base, Path(base).stem, n)
                if user_meta:
                    stub.update(user_meta)
                resolved_meta.append(stub)
        else:
            resolved_meta = files_meta if files_meta is not None else [
                {
                    "filename":  os.path.basename(n),
                    "extension": Path(os.path.basename(n)).suffix.lstrip(".").lower(),
                    "timestamp": call_timestamp,
                }
                for n in files_name
            ]

        payload = {
            "session_id":               session_id,
            "user_id":                  user_id,
            "vector_lake_description":  vector_lake_description,
            "files_name":               [os.path.basename(n) for n in files_name],
            "files_data":               files_data,
            "files_meta":               resolved_meta,
            "intelligent_segmentation": intelligent_segmentation,
        }
        return self._make_request(
            self.config.endpoints[operation], payload, operation, batch_num=batch_num
        )

    # ── Internal: mode dispatcher ─────────────────────────────────────────────

    def _dispatch(
        self,
        operation: str,
        user_id: str,
        vector_lake_description: str,
        processing_mode: ProcessingMode,
        start_from_batch: int,
        end_batch: Optional[int],
        intelligent_segmentation: bool,
        session_id: Optional[str],
        files: Optional[List[str]],
        files_meta: FilesMetaArg,
        grand_total: Optional[int] = None,
        on_batch_success=None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        Route a path-based upload to the correct processing implementation.

        ``"sequential"`` calls :meth:`_process_files_in_batches`.
        ``"streaming"``  calls :meth:`_process_files_streaming`.

        Parameters
        ----------
        operation:
            Endpoint key.
        processing_mode:
            ``"sequential"`` or ``"streaming"``.
        grand_total:
            Pre-computed grand total for the progress display.  Computed on
            demand when ``None``.  Ignored in ``"streaming"`` mode.
        resume:
            Forwarded to whichever processing implementation runs. Skips
            source files already recorded as completed in the operation/lake
            checkpoint — works the same way in both modes.
        """
        if processing_mode == "streaming":
            return self._process_files_streaming(
                operation, user_id, vector_lake_description,
                intelligent_segmentation, session_id, files,
                files_meta=files_meta,
                on_batch_success=on_batch_success,
                resume=resume,
            )

        if processing_mode not in ("sequential", "streaming"):
            return {
                "success": False,
                "error":   "InvalidModeError",
                "message": f"processing_mode must be 'sequential' or 'streaming', got {processing_mode!r}.",
            }

        if grand_total is None:
            grand_total = self._compute_grand_total(self.config.vector_lake_path)

        return self._process_files_in_batches(
            operation, user_id, vector_lake_description,
            start_from_batch, end_batch,
            intelligent_segmentation, session_id, files,
            grand_total_batches=grand_total,
            files_meta=files_meta,
            resume=resume,
        )

    # ── Public: upload ────────────────────────────────────────────────────────

    def add_documents(
        self,
        user_id: str,
        vector_lake_description: str,
        start_from_batch: int = 1,
        end_batch: Optional[int] = None,
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
        files_name: Optional[List[str]] = None,
        files_data: Optional[List[str]] = None,
        files_meta: FilesMetaArg = None,
        processing_mode: ProcessingMode = "sequential",
        on_batch_success=None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        Add **new** documents to the index.

        Two calling conventions are supported:

        **Path mode** (default)
            The SDK reads source files from ``config.vector_lake_path``,
            produces chunked ``.txt`` files on disk, and uploads them in
            batches.  Use *resume=True* to pick back up after an interrupted
            run — see below.

        **Direct mode** (when *files_name* and *files_data* are provided)
            The caller supplies filenames and content directly.  No disk I/O,
            no chunking, no progress bar.  Useful for programmatic pipelines
            where content is already in memory. *resume* has no effect here
            — there's nothing to check a checkpoint against.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        start_from_batch:
            (Path mode, sequential only) Resume from this 1-based batch
            number. Requires knowing the batch number yourself — prefer
            *resume* for automatic, crash-safe resume that also works in
            streaming mode.
        end_batch:
            (Path mode) Stop after this 1-based batch number.  ``None`` means
            process all remaining batches.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        files:
            (Path mode) Explicit list of source filenames to process.  When
            ``None`` every supported file in ``config.vector_lake_path`` is
            used.
        files_name:
            (Direct mode) Ordered list of filenames.
        files_data:
            (Direct mode) Ordered list of file contents (plain text or
            base-64).  Must match *files_name* in length.
        files_meta:
            Custom per-file metadata to send to the server, merged into the
            request's ``files_meta`` payload. Two shapes are accepted:

              - **dict**, keyed by original filename, e.g.::

                    files_meta={
                        "report.pdf": {"department": "finance", "year": 2026},
                        "notes.txt":  {"author": "alice"},
                    }

                This is the recommended shape for **path mode**: each chunk's
                real source file is resolved automatically (regardless of
                chunk-splitting or batch interleaving order) and your dict
                entry is merged on top of the auto-generated
                ``{"filename", "extension", "timestamp"}`` stub. Files you don't mention
                just get the default stub.

              - **list**, positionally aligned to files/chunks (legacy).
                Reliable in **direct mode** (you control the exact
                files_name/files_data order) but fragile in path mode.

            ``None`` (default) sends only the auto-generated
            ``{"filename", "extension", "timestamp"}`` stub for every file.
        processing_mode:
            ``"sequential"`` (default) — chunk all files first, then upload
            one batch at a time.
            ``"streaming"`` — begin uploading as soon as the first minibatch
            of chunks is ready; overlaps extraction with HTTP calls.
        resume:
            (Path mode) If ``True``, skip source files already recorded as
            completed in this operation/lake's checkpoint file — written
            incrementally after every batch/minibatch in **both**
            processing modes. If a run is interrupted (Ctrl+C, crash, killed
            terminal), just call again with the same *files*/*processing_mode*
            and ``resume=True``; it picks up automatically from the last
            completed batch, no batch number needed. Works the same way for
            ``"sequential"`` and ``"streaming"``.

        Returns
        -------
        dict
            Run summary on success, or a structured error dict on failure.

        Examples
        --------
        # Path mode — sequential (default)
        client.add_documents(user_id="u1", vector_lake_description="my_lake")

        # Path mode — streaming
        client.add_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            processing_mode="streaming",
        )

        # Resume an interrupted streaming run — same call, resume=True
        client.add_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            processing_mode="streaming",
            resume=True,
        )

        # Path mode with custom metadata, keyed by filename
        client.add_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            files=["report.pdf", "notes.txt"],
            files_meta={
                "report.pdf": {"department": "finance", "year": 2026},
                "notes.txt":  {"author": "alice"},
            },
        )

        # Direct mode
        client.add_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            files_name=["report.txt"],
            files_data=["Full text content here …"],
        )
        """
        try:
            if files_name is not None and files_data is not None:
                logger.info("ADD_DOCUMENTS direct mode | files=%d", len(files_name))
                result = self._direct_upload(
                    "add_docs", user_id, vector_lake_description,
                    files_name, files_data, intelligent_segmentation, session_id,
                    files_meta=files_meta,
                )
                status = "done" if not result.get("error") else result.get("message", result.get("error"))
                logger.info("ADD_DOCUMENTS direct %s", status)
                return result

            return self._dispatch(
                "add_docs", user_id, vector_lake_description,
                processing_mode, start_from_batch, end_batch,
                intelligent_segmentation, session_id, files, files_meta,
                on_batch_success=on_batch_success,
                resume=resume,
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def refresh_documents(
        self,
        user_id: str,
        vector_lake_description: str,
        start_from_batch: int = 1,
        end_batch: Optional[int] = None,
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
        files_name: Optional[List[str]] = None,
        files_data: Optional[List[str]] = None,
        files_meta: FilesMetaArg = None,
        processing_mode: ProcessingMode = "sequential",
        on_batch_success=None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """
        Refresh (update) **existing** documents in the index.

        Stale on-disk chunks are automatically purged and regenerated from the
        current source files before uploading.  Both path mode and direct mode
        are supported (see :meth:`add_documents` for the distinction).

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        start_from_batch:
            (Path mode, sequential only) Resume from this 1-based batch
            number. Prefer *resume* for automatic, crash-safe resume that
            also works in streaming mode.
        end_batch:
            (Path mode) Stop after this 1-based batch number.  ``None`` means
            process all remaining batches.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        files:
            (Path mode) Explicit list of source filenames to refresh.  When
            ``None`` every supported file in ``config.vector_lake_path`` is
            used.
        files_name:
            (Direct mode) Ordered list of filenames.
        files_data:
            (Direct mode) Ordered list of file contents.  Must match
            *files_name* in length.
        files_meta:
            Custom per-file metadata — see :meth:`add_documents` for the
            accepted shapes (dict keyed by filename, recommended for path
            mode; positional list for direct mode).
        processing_mode:
            ``"sequential"`` (default) or ``"streaming"``.
        resume:
            (Path mode) See :meth:`add_documents` — same checkpoint-based
            resume, works identically here for both processing modes.

        Returns
        -------
        dict
            Run summary on success, or a structured error dict on failure.

        Examples
        --------
        # Path mode — sequential
        client.refresh_documents(user_id="u1", vector_lake_description="my_lake")

        # Direct mode — supply updated content inline
        client.refresh_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            files_name=["report.txt"],
            files_data=["Updated content …"],
        )
        """
        try:
            if files_name is not None and files_data is not None:
                logger.info("REFRESH_DOCUMENTS direct mode | files=%d", len(files_name))
                result = self._direct_upload(
                    "refresh_docs", user_id, vector_lake_description,
                    files_name, files_data, intelligent_segmentation, session_id,
                    files_meta=files_meta,
                )
                status = "done" if not result.get("error") else result.get("message", result.get("error"))
                logger.info("REFRESH_DOCUMENTS direct %s", status)
                return result

            return self._dispatch(
                "refresh_docs", user_id, vector_lake_description,
                processing_mode, start_from_batch, end_batch,
                intelligent_segmentation, session_id, files, files_meta,
                on_batch_success=on_batch_success,
                resume=resume,
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def delete_documents(
        self,
        user_id: str,
        vector_lake_description: str,
        files_name: List[str],
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Remove one or more documents from the index.

        Calls the ``delete_docs`` endpoint with a JSON payload that identifies
        the documents to remove.  No local chunking or batching is required —
        the operation is a lightweight metadata call.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        files_name:
            List of filenames (basenames only) to delete from the index.
            At least one entry is required.
        session_id:
            Optional session correlation token.

        Returns
        -------
        dict
            Server response on success, or a structured error dict on failure.

        Raises (as response dicts)
        --------------------------
        ``ValidationError``
            If *files_name* is empty.
        ``APIError``
            If the server returns HTTP 4xx / 5xx.

        Examples
        --------
        client.delete_documents(
            user_id="u1",
            vector_lake_description="my_lake",
            files_name=["report.pdf", "notes.txt"],
        )
        """
        try:
            if not files_name:
                return ValidationError(
                    "files_name must contain at least one filename."
                ).to_response()

            logger.info("DELETE_DOCUMENTS | files=%d  %s", len(files_name), files_name)
    
            payload: Dict[str, Any] = {
                "session_id":              session_id,
                "user_id":                 user_id,
                "vector_lake_description": vector_lake_description,
                "files_name":              [os.path.basename(n) for n in files_name],
            }
            result = self._make_request(
                self.config.endpoints["delete_docs"], payload, "delete_docs"
            )
            status = "done" if not result.get("error") else result.get("message", result.get("error"))
            logger.info("DELETE_DOCUMENTS %s", status)
            return result

        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    # ── Public: insert ────────────────────────────────────────────────────────

    def run_insert_path(
        self,
        user_id: str,
        vector_lake_description: str,
        start_from_batch: int = 1,
        end_batch: Optional[int] = None,
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
        files_meta: FilesMetaArg = None,
        processing_mode: ProcessingMode = "sequential",
        on_batch_success=None,
    ) -> Dict[str, Any]:
        """
        Insert documents into the index by reading source files from disk.

        Reads from ``config.vector_lake_path``, produces chunked ``.txt`` files,
        and calls the ``insert_docs`` endpoint.  Supports sequential and
        streaming processing modes.

        Use this method when your source files are already placed in the
        configured upload folder and you want the SDK to handle chunking.  For
        fully in-memory content use :meth:`run_insert_path_direct` instead.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        start_from_batch:
            Resume from this 1-based batch number (inclusive).
        end_batch:
            Stop after this 1-based batch number (inclusive).  ``None`` means
            process all remaining batches.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.
        session_id:
            Optional session correlation token.
        files:
            Explicit list of source filenames to insert.  When ``None`` every
            supported file in ``config.vector_lake_path`` is used.
        files_meta:
            Custom per-file metadata — see :meth:`add_documents` for the
            accepted shapes (dict keyed by filename, recommended; positional
            list also supported).
        processing_mode:
            ``"sequential"`` (default) — chunk all files first, then upload
            one batch at a time.
            ``"streaming"`` — begin uploading as soon as the first minibatch
            of chunks is ready.
            Note: ``"streaming"`` does not support resume via *start_from_batch*
            / *end_batch*; use ``"sequential"`` if you need checkpoint/resume.

        Returns
        -------
        dict
            Run summary on success, or a structured error dict on failure.

        Examples
        --------
        # Sequential (default)
        client.run_insert_path(user_id="u1", vector_lake_description="my_lake")

        # Streaming
        client.run_insert_path(
            user_id="u1",
            vector_lake_description="my_lake",
            processing_mode="streaming",
        )
        """
        try:
            return self._dispatch(
                "insert_docs", user_id, vector_lake_description,
                processing_mode, start_from_batch, end_batch,
                intelligent_segmentation, session_id, files, files_meta,
                on_batch_success=on_batch_success,
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def run_insert_path_direct(
        self,
        user_id: str,
        vector_lake_description: str,
        files_name: List[str],
        files_data: List[str],
        intelligent_segmentation: bool = True,
        session_id: Optional[str] = None,
        files_meta: FilesMetaArg = None,
    ) -> Dict[str, Any]:
        """
        Insert documents into the index using caller-supplied content.

        Unlike :meth:`run_insert_path`, this method bypasses all disk I/O.
        The caller provides filenames and their text content directly.  A
        single API call is made with no batching or progress bar.

        Use this method when content is already available in memory (e.g. text
        extracted from a database, API response, or in-process pipeline).

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        files_name:
            Ordered list of filenames.  Basenames are extracted automatically.
        files_data:
            Ordered list of file contents (plain text or base-64 strings).
            Must be the same length as *files_name*.
        intelligent_segmentation:
            Whether the server should apply intelligent chunking.  Defaults to
            ``True``.
        session_id:
            Optional session correlation token.
        files_meta:
            Custom per-file metadata. Accepts a dict keyed by filename
            (merged onto the auto-generated ``{filename, extension, timestamp}`` stub)
            or a positional list matching *files_name* (used as-is). When
            omitted the SDK auto-generates ``{filename, extension, timestamp}`` stubs.

        Returns
        -------
        dict
            Server response on success, or a structured error dict on failure.

        Raises (as response dicts)
        --------------------------
        ``ValidationError``
            If *files_name* or *files_data* are empty, or if their lengths
            differ.
        ``APIError``
            If the server returns HTTP 4xx / 5xx.

        Examples
        --------
        client.run_insert_path_direct(
            user_id="u1",
            vector_lake_description="my_lake",
            files_name=["appendix.txt"],
            files_data=["Section A content …"],
        )

        # With explicit metadata, keyed by filename
        client.run_insert_path_direct(
            user_id="u1",
            vector_lake_description="my_lake",
            files_name=["report.pdf"],
            files_data=[extracted_text],
            files_meta={"report.pdf": {"author": "Alice"}},
        )
        """
        try:
            if not files_name or not files_data:
                return ValidationError(
                    "files_name and files_data must each contain at least one entry."
                ).to_response()

            logger.info("INSERT_DOCUMENTS direct mode | files=%d  %s", len(files_name), files_name)
            result = self._direct_upload(
                "insert_docs", user_id, vector_lake_description,
                files_name, files_data, intelligent_segmentation, session_id,
                files_meta=files_meta,
            )
            status = "done" if not result.get("error") else result.get("message", result.get("error"))
            logger.info("INSERT_DOCUMENTS direct %s", status)
            return result

        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    # ── Public: query ─────────────────────────────────────────────────────────

    def get_matching_docs(
        self,
        query: str,
        user_id: str,
        vector_lake_description: str,
        pattern: str = "static",
        session_id: Optional[str] = None,
        hybrid_filter: bool = False,
        search_type: Optional[str] = None,
        top_docs: int = 10,
        threshold: float = 0.2,
        files_name: Optional[List[str]] = None,
        files_data: Optional[List[str]] = None,
        with_data: bool = False,
    ) -> Dict[str, Any]:
        """
        Retrieve the top semantically matching documents for *query*.

        Supports both static (pre-indexed) and dynamic (inline documents)
        search patterns, as well as optional hybrid filtering to restrict
        the candidate set before semantic ranking.

        Parameters
        ----------
        query:
            Natural-language search query.
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        pattern:
            ``"static"`` (default) — search against the persistent index.
            ``"dynamic"`` — set automatically when *files_name* / *files_data*
            are provided; searches inline documents instead.
        session_id:
            Optional session correlation token.
        hybrid_filter:
            When ``True``, activates the hybrid keyword + vector filter.
            **Required** when *search_type* is set.
        search_type:
            ``"flat"`` — semantic search across all documents; *hybrid_filter*
            is a scoring signal but does not restrict scope.
            ``"flat_filter"`` — semantic search limited to documents that pass
            the *hybrid_filter*.
            ``None`` (default) — server decides the strategy.
        top_docs:
            Maximum number of documents to return.  Defaults to ``10``.
        threshold:
            Minimum similarity score (0–1) for a document to be included.
            Defaults to ``0.2``.
        files_name:
            (Dynamic mode) Ordered list of filenames to search against.
        files_data:
            (Dynamic mode) Ordered list of file contents.  Must match
            *files_name* in length.
        with_data:
            When ``True``, the response includes the raw chunk text alongside
            match metadata.

        Returns
        -------
        dict
            Server response with ranked matching documents, or a structured
            error dict on failure.

        Raises (as response dicts)
        --------------------------
        ``InvalidSearchTypeError``
            If *search_type* is not ``None``, ``"flat"``, or ``"flat_filter"``.
        ``ValidationError``
            If *search_type* is set but *hybrid_filter* is ``False``, or if
            *files_name* / *files_data* have mismatched lengths.

        Examples
        --------
        # Simple semantic search
        client.get_matching_docs(
            query="quarterly revenue breakdown",
            user_id="u1",
            vector_lake_description="finance_lake",
        )

        # Hybrid filtered search
        client.get_matching_docs(
            query="revenue",
            user_id="u1",
            vector_lake_description="finance_lake",
            hybrid_filter=True,
            search_type="flat_filter",
            top_docs=5,
        )
        """
        try:
            if search_type is not None and search_type not in _VALID_SEARCH_TYPES:
                return InvalidSearchTypeError(search_type).to_response()

            if search_type is not None and not hybrid_filter:
                return ValidationError(
                    f"hybrid_filter=True is required when search_type={search_type!r}."
                ).to_response()

            endpoint_key = (
                "top_matching_docs_with_data" if with_data else "top_matching_docs"
            )

            payload: Dict[str, Any] = {
                "session_id":              session_id,
                "user_id":                 user_id,
                "vector_lake_description": vector_lake_description,
                "query":                   query,
                "hybrid_filter":           hybrid_filter,
                "top_docs":                top_docs,
                "threshold":               threshold,
                "pattern":                 pattern,
            }

            if search_type is not None:
                payload["search_type"] = search_type

            if files_name and files_data:
                if len(files_name) != len(files_data):
                    return ValidationError(
                        "files_name and files_data must have the same length"
                    ).to_response()
                payload.update({
                    "files_name": [os.path.basename(n) for n in files_name],
                    "files_data": files_data,
                    "pattern":    "dynamic",
                })

            return self._make_request(
                self.config.endpoints[endpoint_key], payload, endpoint_key
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    # ── Public: info ──────────────────────────────────────────────────────────

    def health_check(
        self,
        user_id: str,
        vector_lake_description: str,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Ping the server and return its health status.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        session_id:
            Optional session correlation token.

        Returns
        -------
        dict
            ``{"status": "ok", "message": "...", "timestamp": "..."}`` on
            success, or a structured error dict on failure.

        Examples
        --------
        result = client.health_check(user_id="u1", vector_lake_description="my_lake")
        print(result["status"])
        """
        try:
            payload = {
                "user_id":                 user_id,
                "vector_lake_description": vector_lake_description,
                "session_id":              session_id,
            }
            return self._make_request(self.config.endpoints["health"], payload, "health")
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def get_namespace_details(
        self,
        user_id: str,
        vector_lake_description: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Retrieve metadata for all namespaces belonging to *user_id*.

        When *vector_lake_description* is supplied the response is narrowed to
        that single namespace.

        Parameters
        ----------
        user_id:
            Caller identity whose namespaces are queried.
        vector_lake_description:
            Optional namespace filter.
        session_id:
            Optional session correlation token.

        Returns
        -------
        dict
            Namespace metadata list on success, or a structured error dict on
            failure.

        Examples
        --------
        client.get_namespace_details(user_id="u1")
        client.get_namespace_details(user_id="u1", vector_lake_description="my_lake")
        """
        try:
            payload: Dict[str, Any] = {"user_id": user_id, "session_id": session_id}
            if vector_lake_description:
                payload["vector_lake_description"] = vector_lake_description
            return self._make_request(
                self.config.endpoints["get_namespace_details_by_userid"],
                payload, "get_namespace_details",
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def get_docs_information(
        self,
        user_id: str,
        vector_lake_description: str,
        session_id: Optional[str] = None,
        keyword: Optional[str] = None,
        threshold: int = 70,
    ) -> Dict[str, Any]:
        """
        Retrieve metadata and indexing information for documents in a namespace.

        Optionally filters by *keyword* with fuzzy matching controlled by
        *threshold*.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        session_id:
            Optional session correlation token.
        keyword:
            Optional fuzzy-match filter applied to document names.
        threshold:
            Minimum fuzzy-match score (0–100) for *keyword* filtering.
            Defaults to ``70``.

        Returns
        -------
        dict
            Document information list on success, or a structured error dict
            on failure.

        Examples
        --------
        # All documents
        client.get_docs_information(user_id="u1", vector_lake_description="my_lake")

        # Filtered by keyword
        client.get_docs_information(
            user_id="u1",
            vector_lake_description="my_lake",
            keyword="quarterly",
            threshold=80,
        )
        """
        try:
            payload: Dict[str, Any] = {
                "session_id":              session_id,
                "user_id":                 user_id,
                "vector_lake_description": vector_lake_description,
                "threshold":               threshold,
            }
            if keyword:
                payload["keyword"] = keyword
            return self._make_request(
                self.config.endpoints["get_docs_information"], payload, "get_docs_information"
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}

    def full_corpus_search(
        self,
        user_id: str,
        vector_lake_description: str,
        keyword: str,
        session_id: Optional[str] = None,
        top_docs: int = 10,
    ) -> Dict[str, Any]:
        """
        Perform a full-text keyword search across the entire indexed corpus.

        Unlike :meth:`get_matching_docs` (which uses vector similarity), this
        method performs a traditional keyword scan over all indexed document
        text.  Useful for exact-match lookups, compliance searches, or when
        semantic search returns noisy results for specific terms.

        Parameters
        ----------
        user_id:
            Caller identity forwarded verbatim to the server.
        vector_lake_description:
            Namespace / lake identifier.
        keyword:
            Search term or phrase to scan for.
        session_id:
            Optional session correlation token.
        top_docs:
            Maximum number of matching documents to return.  Defaults to ``10``.

        Returns
        -------
        dict
            Ranked keyword-match results on success, or a structured error dict
            on failure.

        Examples
        --------
        client.full_corpus_search(
            user_id="u1",
            vector_lake_description="my_lake",
            keyword="GDPR",
            top_docs=5,
        )
        """
        try:
            payload = {
                "session_id":              session_id,
                "user_id":                 user_id,
                "vector_lake_description": vector_lake_description,
                "keyword":                 keyword,
                "top_docs":                top_docs,
            }
            return self._make_request(
                self.config.endpoints["full_corpus_search"], payload, "full_corpus_search"
            )
        except VectorLakeError as exc:
            return exc.to_response()
        except Exception as exc:
            return {"success": False, "error": type(exc).__name__, "message": str(exc)}