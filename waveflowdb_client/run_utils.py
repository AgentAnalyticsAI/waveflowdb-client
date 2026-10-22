"""
run_utils.py — WaveflowDB SDK helpers and public action runners
===============================================================
All credentials are read from .env / environment variables.
Public run_* functions accept explicit kwargs so run.py (or any script)
can pass its CONFIG values without relying on shared global state.

Stale-chunk handling
--------------------
The SDK caches extracted chunks in <VECTOR_LAKE_PATH>/chunks/.  Both
BatchManager.create_batches() and streaming_create_batches() skip re-extraction
when a chunk file already exists on disk (cache-hit fast path in utils.py).
This causes refresh / insert to silently re-upload old content when a source
file has changed.  The fix lives entirely here — before any SDK call:

  refresh_docs / insert_docs
      _purge_chunks_for_files()  — unconditionally deletes all *_part*.txt
      files for every source file, forcing full re-extraction.

  add_docs
      _invalidate_changed_files() — computes an MD5 of each source file and
      compares it to a .md5 sidecar stored in the chunks directory.  Only
      files whose content has changed (or that have never been indexed) have
      their chunks purged, so unchanged files still benefit from the cache.

Filename sanitization
----------------------
Before any of the above runs, every source file discovered on disk is passed
through utils.sanitize_filenames(), which renames files with unsafe/messy
names (spaces, illegal characters, ...) to their clean_filename() equivalent
*in place*. This guarantees chunk filenames, MD5 sidecars, and checkpoint
entries are always derived from a clean name — matching what
VectorLakeClient's own path-mode discovery does (see client.py).

Output formatting
------------------
parse_response() / to_text() (moved in from parser.py, unchanged) strip the
"Source: <file> [Section *n*]" prefix, doc_score, category, and similarities
noise out of a raw {"reply": {"content": [...]}} response, leaving clean
{filename: [chunk, ...]} data — used by run_index.py (query/"ringi" results)
and run_index_lookup.py ("sa" results) to format output before printing/saving.
"""

import hashlib
import json
import logging
import os,re
import re as _re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

from .config import Config
from .client import VectorLakeClient
from .utils import clean_filename, sanitize_filenames, WINDOWS_RESERVED  # WINDOWS_RESERVED only if used elsewhere in this file

# ── Credentials (from .env / environment) ────────────────────────────────────
API_KEY          = os.getenv("VECTOR_LAKE_API_KEY")
HOST             = os.getenv("VECTOR_LAKE_HOST", "https://waveflow-analytics.com")
VECTOR_LAKE_PATH = os.getenv("VECTOR_LAKE_PATH", "upload")
USER_ID          = os.getenv("USER_ID")
NAMESPACE        = os.getenv("NAMESPACE")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR = os.getenv("VECTOR_LAKE_LOG_DIR", "logs")
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

_fmt = logging.Formatter(
    "%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
_fh = logging.FileHandler(Path(LOG_DIR) / "run_operations.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_fh.setLevel(logging.DEBUG)

_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
_ch.setLevel(logging.INFO)

logging.basicConfig(level=logging.DEBUG, handlers=[_fh, _ch])
logger = logging.getLogger("run_utils")

_FAILED_FILES_LOG = Path(LOG_DIR) / "failed_files.jsonl"
_PART_RE          = _re.compile(r'_part\d+$', _re.IGNORECASE)
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# ══════════════════════════════════════════════════════════════════════════════
#  CORE UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _md5_file(path: str) -> str:
    """Stream-compute MD5 hex digest of a file without loading it into memory."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65_536), b""):
            h.update(block)
    return h.hexdigest()


def _client() -> VectorLakeClient:
    cfg = Config(
        api_key=API_KEY,
        host=HOST,
        vector_lake_path=VECTOR_LAKE_PATH,
        # service_port intentionally NOT hardcoded here — let Config resolve
        # it via VECTOR_LAKE_PORT / auto-detection, same as VectorLake/Index.
        # Domain hosts (nginx-routed) stay portless; IP hosts auto-default
        # to 8030 with a warning. Set VECTOR_LAKE_PORT explicitly to override.
    )
    return VectorLakeClient(cfg)


def _print_result(label: str, result: object) -> None:
    w = 70
    print(f"\n{'─' * w}")
    print(f"  {label}")
    print(f"{'─' * w}")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        keys = list(result[0].keys())
        col  = max(len(k) for k in keys) + 2
        print("  " + "".join(k.ljust(col) for k in keys))
        print("  " + "─" * (col * len(keys)))
        for row in result:
            print("  " + "".join(str(row.get(k, "")).ljust(col) for k in keys))
    else:
        print(json.dumps(result, indent=2, default=str))


# ══════════════════════════════════════════════════════════════════════════════
#  RESPONSE FORMATTING  (moved in from parser.py, unchanged)
# ══════════════════════════════════════════════════════════════════════════════

def parse_response(raw):
    """
    Convert the raw search-tool JSON response into a clean
    {filename: [chunk, chunk, ...]} structure, stripping out
    scores, similarities, categories, and other metadata noise.
    """
    data = json.loads(raw) if isinstance(raw, str) else raw

    content = data.get("reply", {}).get("content", [])
    result = {}

    for item in content:
        fname = item.get("file_name", "unknown")
        chunk_list = item.get("chunks", {}).get("chunk", [])

        cleaned_chunks = []
        for c in chunk_list:
            # Strip the repetitive "Source: <file> [Section *n*]" prefix
            # so only the actual content remains.
            c = re.sub(r'^Source:\s*\S+\s*\[Section \*\d+\*\]\s*', '', c)
            c = c.strip()
            if c:
                cleaned_chunks.append(c)

        # Merge chunks belonging to the same file_name key if it repeats
        result.setdefault(fname, []).extend(cleaned_chunks)

    return result


def to_text(parsed):
    """Render {filename: [chunks]} as plain filename:content blocks."""
    lines = []
    for fname, chunks in parsed.items():
        lines.append(f"{fname}:")
        for i, c in enumerate(chunks, 1):
            lines.append(f"  [{i}] {c}")
        lines.append("")  # blank line between files
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  STALE-CHUNK MANAGEMENT  (the core fix)
# ══════════════════════════════════════════════════════════════════════════════

def _purge_chunks_for_files(base_path: str, files: Optional[List[str]] = None) -> None:
    """
    Delete on-disk chunk files so the SDK is forced to re-extract from source.

    Called unconditionally before refresh_docs and insert_docs because those
    operations must always send the current version of each document.

    files=None  → purge every *_part*.txt in the chunks directory.
    files=[...] → purge only chunks whose stem matches a listed source file.
    """
    chunks_dir = Path(base_path) / "chunks"
    if not chunks_dir.exists():
        return

    patterns = (
        [f"{Path(f).stem}_part*.txt" for f in files]
        if files else ["*_part*.txt"]
    )
    total = 0
    for pat in patterns:
        for chunk in chunks_dir.glob(pat):
            chunk.unlink(missing_ok=True)
            total += 1

    if total:
        scope = f"{len(files)} file(s)" if files else "all files"
        logger.info("Purged %d stale chunk(s) for %s", total, scope)
    else:
        logger.debug("No stale chunks found to purge in %s", chunks_dir)


def _invalidate_changed_files(base_path: str, files: Optional[List[str]] = None) -> None:
    """
    MD5-based cache invalidation for add_docs.

    Compares each source file's MD5 against a .md5 sidecar stored next to
    the chunks.  Chunks are purged (and the sidecar updated) only when the
    source file has changed, so truly unchanged files keep their cache and
    avoid redundant OCR.

    files=None → checks every non-hidden file in base_path (after sanitizing
    filenames on disk — see sanitize_filenames()).
    """
    chunks_dir = Path(base_path) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)

    if files is None:
        files = [
            f for f in os.listdir(base_path)
            if os.path.isfile(os.path.join(base_path, f)) and not f.startswith(".")
        ]
        # Rename any messy-named source file to its clean equivalent before
        # computing MD5s, so the sidecar (and any chunks produced later) are
        # always keyed off the same clean name.
        files = sanitize_filenames(base_path, files)

    for fname in files:
        src = Path(base_path) / fname
        if not src.is_file():
            continue

        sidecar  = chunks_dir / f"{src.stem}.md5"
        current  = _md5_file(str(src))
        if sidecar.exists() and sidecar.read_text().strip() == current:
            logger.debug("MD5 match — cache valid for '%s'", fname)
            continue

        # Content changed or first index: purge old chunks then write sidecar
        purged = sum(
            1 for c in chunks_dir.glob(f"{src.stem}_part*.txt")
            if not c.unlink(missing_ok=True) or True   # unlink returns None
        )
        sidecar.write_text(current)
        if purged:
            logger.info("MD5 changed for '%s' — purged %d chunk(s)", fname, purged)
        else:
            logger.debug("First-time index of '%s' — MD5 sidecar written", fname)


# ══════════════════════════════════════════════════════════════════════════════
#  CHECKPOINT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _checkpoint_path(operation: str) -> Path:
    uid  = (USER_ID   or "unknown_user").replace("/", "_").replace("@", "_at_")
    ns   = (NAMESPACE or "unknown_ns").replace("/", "_")
    name = f"checkpoint_{uid}_{ns}_{operation.replace('/', '_')}.json"
    path = Path(VECTOR_LAKE_PATH) / "chunks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_checkpoint(operation: str) -> dict:
    cp = _checkpoint_path(operation)
    if not cp.exists():
        return {}
    try:
        return json.loads(cp.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read checkpoint %s: %s", cp, exc)
        return {}


def _save_checkpoint(operation: str, mode: str, **fields) -> None:
    cp   = _checkpoint_path(operation)
    data = _load_checkpoint(operation)
    data.update(fields)
    data.update({
        "operation":  operation,
        "user_id":    USER_ID,
        "namespace":  NAMESPACE,
        "mode":       mode,
        "updated_at": _utcnow(),
    })
    cp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.debug("Checkpoint saved → %s", cp)


def _clear_checkpoint(operation: str) -> None:
    cp = _checkpoint_path(operation)
    if cp.exists():
        cp.unlink()
        logger.info("Checkpoint cleared  op=%s", operation)


def _resolve_start_batch(
    operation: str, mode: str, resume: bool, start_from_batch: int
) -> int:
    if mode == "streaming":
        return 1
    if resume:
        last_ok = _load_checkpoint(operation).get("last_ok_batch")
        if last_ok is not None:
            resume_from = last_ok + 1
            logger.info("Resuming %s from batch #%d", operation, resume_from)
            print(f"   ↻  Resuming from batch #{resume_from} (last checkpoint: #{last_ok})")
            return resume_from
    return start_from_batch


def _resolve_completed_stems(operation: str, mode: str, resume: bool) -> set:
    if mode != "streaming" or not resume:
        return set()
    return set(_load_checkpoint(operation).get("completed_stems", []))


def _update_checkpoint_after_sequential(
    operation: str, mode: str, result: dict
) -> None:
    end   = result.get("end_batch")
    fails = result.get("failed_batches", 0)
    total = result.get("total_batches")
    if end is None:
        return
    if fails == 0 and end == total:
        _clear_checkpoint(operation)
    else:
        ok = [b["batch_number"] for b in result.get("batches", []) if b.get("success")]
        if ok:
            _save_checkpoint(operation, mode, last_ok_batch=max(ok))


def _update_checkpoint_after_streaming(
    operation: str, mode: str, completed_stems: set
) -> None:
    _save_checkpoint(operation, mode, completed_stems=sorted(completed_stems))


# ══════════════════════════════════════════════════════════════════════════════
#  FAILURE LOGGING
# ══════════════════════════════════════════════════════════════════════════════

def _log_failed_files(
    operation: str,
    files: List[str],
    error_type: str,
    error_message: str,
    batch_num: Optional[int] = None,
    extra: Optional[dict] = None,
) -> None:
    record = {
        "timestamp":     _utcnow(),
        "operation":     operation,
        "user_id":       USER_ID,
        "namespace":     NAMESPACE,
        "batch_num":     batch_num,
        "files":         files,
        "error_type":    error_type,
        "error_message": error_message,
        "traceback":     traceback.format_exc() if sys.exc_info()[0] else None,
        "extra":         extra or {},
    }
    try:
        _FAILED_FILES_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_FAILED_FILES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as log_exc:
        logger.error("Could not write to failed_files.jsonl: %s", log_exc)

    logger.error(
        "FAILED  op=%-14s  batch=%-4s  files=%s  error=%s: %s",
        operation,
        str(batch_num) if batch_num is not None else "—",
        files, error_type, error_message,
    )


def _audit_result_for_failures(operation: str, result: dict) -> None:
    for batch in result.get("batches", []):
        if batch.get("success"):
            continue
        resp = batch.get("response") or {}
        err  = batch.get("error") or {}
        _log_failed_files(
            operation=operation,
            files=batch.get("files", []),
            error_type=resp.get("error") or err.get("error") or "UnknownError",
            error_message=(
                resp.get("message") or err.get("message") or str(resp) or str(err)
            ),
            batch_num=batch.get("batch_number"),
            extra={
                "processing_time": batch.get("processing_time"),
                "elapsed_ms":      batch.get("elapsed_ms"),
                "server_skip":     batch.get("server_skip", False),
            },
        )


# ══════════════════════════════════════════════════════════════════════════════
#  STREAMING RECOVERY
# ══════════════════════════════════════════════════════════════════════════════

def _run_streaming_with_recovery(
    client: VectorLakeClient,
    sdk_operation: str,
    mode: str,
    resume: bool,
    files_meta: Optional[list] = None,
    max_concurrent_batches: Optional[int] = None,
    batch_delay: Optional[float] = None,
) -> dict:
    """
    Run a streaming upload with checkpoint-based recovery.

    sdk_operation: "add_docs" | "refresh_docs" | "insert_docs"

    Completed file stems are persisted to disk after every successful batch so
    that a crash or Ctrl+C loses at most one batch of work on restart.
    Note: stale-chunk purging for refresh/insert is handled by _run_upload
    before this function is called, so extraction here always sees fresh source.
    """
    base_path       = client.config.vector_lake_path
    completed_stems = _resolve_completed_stems(sdk_operation, mode, resume)

    all_files = sorted(
        f for f in os.listdir(base_path)
        if os.path.isfile(os.path.join(base_path, f))
        and client._file_processor.is_supported(f)
    )
    # Rename any messy-named files to their clean equivalent before this
    # function tracks/reports on them by name — matches what
    # VectorLakeClient's own path-mode discovery does, and keeps stems
    # (used below and in the checkpoint) stable and predictable.
    all_files = sanitize_filenames(base_path, all_files)

    if completed_stems:
        before    = len(all_files)
        all_files = [f for f in all_files if Path(f).stem not in completed_stems]
        skipped   = before - len(all_files)
        if skipped:
            logger.info(
                "Streaming recovery: skipping %d already-uploaded file(s)", skipped
            )

    if not all_files:
        logger.info("Streaming recovery: all files already uploaded  op=%s", sdk_operation)
        return {
            "success": True,
            "message": "All files already uploaded (checkpoint).",
            "mode":    "streaming",
        }

    new_completed: set = set(completed_stems)

    def _on_batch_success(batch_files: list) -> None:
        for fname in batch_files:
            raw_stem = Path(fname).stem
            new_completed.add(_PART_RE.sub("", raw_stem) or raw_stem)
        _update_checkpoint_after_streaming(sdk_operation, mode, new_completed)
        logger.debug("Checkpoint: %d stems completed", len(new_completed))

    method_map = {
        "add_docs":     client.add_documents,
        "refresh_docs": client.refresh_documents,
        "insert_docs":  client.run_insert_path,
    }
    result = method_map[sdk_operation](
        user_id=USER_ID,
        vector_lake_description=NAMESPACE,
        processing_mode="streaming",
        files=all_files,
        files_meta=files_meta,
        on_batch_success=_on_batch_success,
        max_concurrent_batches=max_concurrent_batches,
        batch_delay=batch_delay,
    )

    for batch in result.get("batches", []):
        if not batch.get("success") and not batch.get("server_skip"):
            resp = batch.get("response") or {}
            err  = batch.get("error") or {}
            _log_failed_files(
                operation=sdk_operation,
                files=batch.get("files", []),
                error_type=resp.get("error") or err.get("error") or "UnknownError",
                error_message=(
                    resp.get("message") or err.get("message") or str(resp or err)
                ),
                batch_num=batch.get("batch_number"),
            )

    if result.get("failed_batches", 0) == 0:
        _clear_checkpoint(sdk_operation)
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  UPLOAD DISPATCHER  (shared by add / refresh / insert)
# ══════════════════════════════════════════════════════════════════════════════

def _run_upload(
    sdk_op: str,
    client: VectorLakeClient,
    label: str,
    mode: str,
    resume: bool,
    start_from_batch: int,
    end_batch: Optional[int],
    files_name: Optional[List[str]],
    files_data: Optional[List[str]],
    files_meta: Optional[List[dict]],
    max_concurrent_batches: Optional[int] = None,
    batch_delay: Optional[float] = None,
) -> dict:
    """
    Route an upload to direct mode or path mode (streaming / sequential).

    Direct mode  — both files_name and files_data are provided; content is
                   passed in-memory directly to the SDK with no disk I/O.
                   No chunk purging needed (no chunks are ever written).

    Path mode    — reads source files from VECTOR_LAKE_PATH.
                   Stale chunks are cleared BEFORE extraction:
                     refresh_docs / insert_docs → unconditional purge.
                     add_docs                   → MD5-based invalidation only.
    """
    base_path = client.config.vector_lake_path
    logger.info(
        "%s  user=%s  ns=%s  mode=%s  direct=%s",
        sdk_op, USER_ID, NAMESPACE, mode, files_name is not None,
    )

    # ── Direct mode ───────────────────────────────────────────────────────────
    if files_name is not None and files_data is not None:
        direct_map = {
            "add_docs":     client.add_documents,
            "refresh_docs": client.refresh_documents,
            "insert_docs":  client.run_insert_path_direct,
        }
        result = direct_map[sdk_op](
            user_id=USER_ID,
            vector_lake_description=NAMESPACE,
            files_name=files_name,
            files_data=files_data,
            files_meta=files_meta,
        )
        if result.get("error"):
            _log_failed_files(
                sdk_op, files_name,
                result.get("error", "APIError"),
                result.get("message", str(result)),
            )
        _print_result(f"{label} (direct)", result)
        return result

    # ── Path mode: stale-chunk management ────────────────────────────────────
    # refresh and insert must ALWAYS send current content — purge everything.
    if sdk_op in ("refresh_docs", "insert_docs"):
        _purge_chunks_for_files(base_path)

    # add only re-extracts files whose MD5 has changed since last index.
    elif sdk_op == "add_docs":
        _invalidate_changed_files(base_path)

    # ── Path mode: dispatch to streaming or sequential ────────────────────────
    try:
        if mode == "streaming":
            result = _run_streaming_with_recovery(
                client, sdk_op, mode, resume, files_meta,
                max_concurrent_batches=max_concurrent_batches,
                batch_delay=batch_delay,
            )
        else:
            start = _resolve_start_batch(sdk_op, mode, resume, start_from_batch)
            path_map = {
                "add_docs":     client.add_documents,
                "refresh_docs": client.refresh_documents,
                "insert_docs":  client.run_insert_path,
            }
            result = path_map[sdk_op](
                user_id=USER_ID,
                vector_lake_description=NAMESPACE,
                start_from_batch=start,
                end_batch=end_batch,
                processing_mode="sequential",
                files_meta=files_meta,
            )
            _audit_result_for_failures(sdk_op, result)
            _update_checkpoint_after_sequential(sdk_op, mode, result)

        _print_result(f"{label} [{mode}]", result)
        return result

    except Exception as exc:
        _log_failed_files(sdk_op, [], type(exc).__name__, str(exc))
        logger.exception("%s failed", sdk_op)
        raise


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ACTION RUNNERS
# ══════════════════════════════════════════════════════════════════════════════

def run_add(
    *,
    mode: str = "streaming",
    resume: bool = True,
    start_from_batch: int = 1,
    end_batch: Optional[int] = None,
    files_name: Optional[List[str]] = None,
    files_data: Optional[List[str]] = None,
    files_meta: Optional[List[dict]] = None,
    max_concurrent_batches: Optional[int] = None,
    batch_delay: Optional[float] = None,
) -> dict:
    """
    Index new documents.

    Path mode (default): reads from VECTOR_LAKE_PATH.  An MD5 sidecar is
    written per source file so subsequent runs only re-extract files that
    have actually changed — unchanged files reuse cached chunks.

    Direct mode: supply files_name + files_data to pass content in-memory.

    max_concurrent_batches: (streaming mode only) maximum number of
    minibatch uploads allowed to be in flight at once. ``None`` falls back
    to ``Config.streaming_max_concurrent_batches``.
    batch_delay: seconds to sleep between consecutive batch/minibatch
    submissions. ``None`` keeps the SDK default. If you raise
    max_concurrent_batches, consider lowering this too — it throttles
    submissions regardless of concurrency.
    """
    return _run_upload(
        "add_docs", _client(), "ADD",
        mode, resume, start_from_batch, end_batch,
        files_name, files_data, files_meta,
        max_concurrent_batches=max_concurrent_batches,
        batch_delay=batch_delay,
    )


def run_refresh(
    *,
    mode: str = "streaming",
    resume: bool = True,
    start_from_batch: int = 1,
    end_batch: Optional[int] = None,
    files_name: Optional[List[str]] = None,
    files_data: Optional[List[str]] = None,
    files_meta: Optional[List[dict]] = None,
    max_concurrent_batches: Optional[int] = None,
    batch_delay: Optional[float] = None,
) -> dict:
    """
    Re-index existing documents.

    All on-disk chunks are purged before extraction so the server always
    receives the current version of each document, never cached content.

    max_concurrent_batches: (streaming mode only) maximum number of
    minibatch uploads allowed to be in flight at once. ``None`` falls back
    to ``Config.streaming_max_concurrent_batches``.
    batch_delay: seconds to sleep between consecutive batch/minibatch
    submissions. ``None`` keeps the SDK default.
    """
    return _run_upload(
        "refresh_docs", _client(), "REFRESH",
        mode, resume, start_from_batch, end_batch,
        files_name, files_data, files_meta,
        max_concurrent_batches=max_concurrent_batches,
        batch_delay=batch_delay,
    )


def run_insert(
    *,
    mode: str = "streaming",
    resume: bool = True,
    start_from_batch: int = 1,
    end_batch: Optional[int] = None,
    files_name: Optional[List[str]] = None,
    files_data: Optional[List[str]] = None,
    files_meta: Optional[List[dict]] = None,
    max_concurrent_batches: Optional[int] = None,
    batch_delay: Optional[float] = None,
) -> dict:
    """
    Insert documents at a server-controlled position in the corpus.

    All on-disk chunks are purged before extraction to guarantee fresh
    content is sent.  Unlike add (which always appends), insert lets the
    server control placement within the namespace.

    max_concurrent_batches: (streaming mode only) maximum number of
    minibatch uploads allowed to be in flight at once. ``None`` falls back
    to ``Config.streaming_max_concurrent_batches``.
    batch_delay: seconds to sleep between consecutive batch/minibatch
    submissions. ``None`` keeps the SDK default.
    """
    return _run_upload(
        "insert_docs", _client(), "INSERT",
        mode, resume, start_from_batch, end_batch,
        files_name, files_data, files_meta,
        max_concurrent_batches=max_concurrent_batches,
        batch_delay=batch_delay,
    )


def run_delete(
    files_name: List[str],
    *,
    session_id: Optional[str] = None,
) -> dict:
    """
    Remove documents from the index by exact filename.

    Lightweight metadata-only call — no chunking or disk I/O required.
    Pass basenames exactly as they appear in the namespace.
    """
    if not files_name:
        raise ValueError("run_delete requires at least one filename.")

    client = _client()
    logger.info("run_delete  user=%s  ns=%s  files=%s", USER_ID, NAMESPACE, files_name)

    try:
        result = client.delete_documents(
            user_id=USER_ID,
            vector_lake_description=NAMESPACE,
            files_name=files_name,
            session_id=session_id,
        )
        if result.get("error"):
            _log_failed_files(
                "delete_docs", files_name,
                result.get("error", "APIError"),
                result.get("message", str(result)),
            )
            logger.error(
                "run_delete failed  files=%s  error=%s",
                files_name, result.get("error"),
            )
        else:
            logger.info("run_delete succeeded  files=%s", files_name)
        _print_result("DELETE", result)
        return result

    except Exception as exc:
        _log_failed_files("delete_docs", files_name, type(exc).__name__, str(exc))
        logger.exception("run_delete failed")
        raise


def run_query(
    query: str,
    *,
    search_type: Optional[str] = "flat_filter",
    top_docs: int = 10,
    threshold: float = 0.2,
    with_data: bool = False,
    dynamic_files_name: Optional[List[str]] = None,
    dynamic_files_data: Optional[List[str]] = None,
) -> dict:
    """
    Semantic / VQL search across the indexed namespace.

    search_type:
      "flat"        — semantic + hybrid filter across full corpus; max recall.
                      hybrid_filter is set automatically.
      "flat_filter" — hybrid filter runs first as a hard gate, then semantic
                      ranking over the filtered set; higher precision.
                      hybrid_filter is set automatically.
      None          — pure semantic search, no hybrid filter.

    Supply dynamic_files_name + dynamic_files_data to search over inline
    temporary files without pre-indexing them.
    """
    client  = _client()
    dynamic = dynamic_files_name is not None and dynamic_files_data is not None

    kwargs: dict = dict(
        query=query,
        user_id=USER_ID,
        vector_lake_description=NAMESPACE,
        pattern="dynamic" if dynamic else "static",
        hybrid_filter=search_type is not None,
        search_type=search_type,
        top_docs=top_docs,
        threshold=threshold,
        with_data=with_data,
    )
    if dynamic:
        kwargs["files_name"] = dynamic_files_name
        kwargs["files_data"] = dynamic_files_data

    result = client.get_matching_docs(**kwargs)
    label  = f"QUERY [{search_type or 'semantic'}]" + (" [dynamic]" if dynamic else "")
    _print_result(label, result)
    return result


def run_health() -> dict:
    """Ping the server and confirm namespace connectivity."""
    result = _client().health_check(
        user_id=USER_ID, vector_lake_description=NAMESPACE
    )
    _print_result("HEALTH CHECK", result)
    return result


def run_docs_info(
    keyword: Optional[str] = None,
    threshold: int = 70,
) -> dict:
    """
    List documents indexed in the namespace.

    keyword   — optional fuzzy filename filter.
    threshold — fuzzy match sensitivity, 0–100 (default 70).
    """
    client = _client()
    kwargs = dict(user_id=USER_ID, vector_lake_description=NAMESPACE)
    if keyword:
        kwargs.update(keyword=keyword, threshold=threshold)
    result = client.get_docs_information(**kwargs)
    _print_result(
        "DOCS INFO" + (f" [keyword={keyword!r}]" if keyword else ""), result
    )
    return result


def run_namespace_details() -> dict:
    """Return storage and quota metadata for the configured namespace."""
    result = _client().get_namespace_details(
        user_id=USER_ID, vector_lake_description=NAMESPACE
    )
    _print_result("NAMESPACE DETAILS", result)
    return result


def run_full_corpus_search(keyword: str, top_docs: int = 10) -> dict:
    """
    Exact keyword scan across the entire indexed corpus.

    Hard-match lookup — not a semantic / vector search.  Use for compliance
    audits, finding specific identifiers, or when semantic search is too broad.
    """
    result = _client().full_corpus_search(
        user_id=USER_ID,
        vector_lake_description=NAMESPACE,
        keyword=keyword,
        top_docs=top_docs,
    )
    _print_result(f"FULL CORPUS SEARCH [keyword={keyword!r}]", result)
    return result