"""
config.py — SDK configuration, loaded from env / .env file or constructor args.

OCR configuration
─────────────────
Local OCR (Tesseract):
    enable_ocr      bool   master switch (default True)
    ocr_lang        str    Tesseract language code (default "eng")

Cloud OCR — select one provider via cloud_ocr_provider:
    None            (default) — cloud OCR disabled; local only
    "aws"           AWS Textract
    "gcp"           Google Cloud Vision
    "azure"         Azure Document Intelligence
    "mathpix"       Mathpix (math / scientific)
    "llama_parse"   LlamaParse (LLM-native RAG)

Cloud mode (cloud_ocr_mode):
    "hybrid"  (default) — local first, cloud fallback when quality is low
    "cloud"             — skip local; always use cloud engine

Priority (highest → lowest) for every setting:
    1. Constructor keyword arguments
    2. Environment variables / .env file
    3. Hard-coded defaults
"""

import logging
import os
from ipaddress import ip_address
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .exceptions import ConfigError

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(".env"), override=False)
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ── Env-var helpers ───────────────────────────────────────────────────────────

def _env_list(key: str, default: str) -> List[str]:
    raw = os.getenv(key, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# CHANGED: new helper — supports fractional MB values like 0.5
def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes")


def _env_str(key: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(key)
    if val is None:
        return default
    return val if val != "" else default


def _is_ip_host(host: Optional[str]) -> bool:
    """
    Return True if *host* — with or without a scheme/port/path — resolves
    to a raw IP address rather than a domain name.

    Domain hosts (e.g. https://waveflow-analytics.com) sit behind nginx,
    which routes /query and /upload by path — no port needed. A bare IP
    has no such reverse proxy in front of it, so it needs an explicit
    service_port. Used to auto-default service_port for IP hosts and avoid
    silently building a portless URL that falls back to HTTP's default
    port 80.
    """
    if not host:
        return False
    parsed = urlparse(host if "://" in host else f"//{host}")
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        ip_address(hostname)
        return True
    except ValueError:
        return False


# Default extension list — includes image formats for OCR
_DEFAULT_EXTENSIONS = (
    "txt,csv,xlsx,json,jsonl,py,docx,pdf,pptx,"
    "png,jpg,jpeg,tiff,tif,bmp,webp"
)

# Valid cloud provider identifiers
_CLOUD_PROVIDERS = frozenset({"aws", "gcp", "azure", "mathpix", "llama_parse"})


class Config:
    """
    All SDK settings in one place.

    Batch / payload sizing
    ──────────────────────
    max_files_per_batch (int, default 10)
        Maximum number of chunk files sent in a single API call.

    max_batch_size_mb (float, default 0.5)
        Maximum payload size per API call in MB.  Accepts fractional values,
        e.g. 0.5 for 500 KB.  The corresponding env var is
        VECTOR_LAKE_MAX_BATCH_SIZE_MB.

    streaming_minibatch_files (int, default 10)
        Streaming mode: flush and upload as soon as this many chunks are ready.

    streaming_minibatch_size_mb (float, default 0.5)
        Streaming mode: also flush if the pending buffer exceeds this size in
        MB.  Must be >= max_batch_size_mb so _flush_pending can always form at
        least one complete batch.

    streaming_max_concurrent_batches (int, default 1)
        Streaming mode: maximum number of minibatch uploads allowed to be
        in flight at once. ``1`` preserves the original strictly sequential
        behaviour (one upload at a time, waiting for each response before
        sending the next). Raising this lets extraction keep producing
        minibatches while several are being uploaded concurrently, which can
        meaningfully cut wall-clock time for I/O-bound uploads — at the cost
        of a proportionally higher peak memory footprint and server-side
        request rate. The corresponding env var is
        VECTOR_LAKE_STREAMING_MAX_CONCURRENT_BATCHES.

    Quick-start examples
    ─────────────────────
    # Local OCR only (default)
    Config(api_key="...")

    # AWS Textract hybrid (local first, Textract fallback)
    Config(
        api_key="...",
        cloud_ocr_provider="aws",
        aws_region="eu-west-1",
    )

    # Azure layout model, tables included
    Config(
        api_key="...",
        cloud_ocr_provider="azure",
        azure_model_id="prebuilt-layout",
        azure_endpoint="https://my-resource.cognitiveservices.azure.com/",
        azure_api_key="...",
    )

    # Mathpix for scientific PDFs
    Config(
        api_key="...",
        cloud_ocr_provider="mathpix",
        mathpix_output_format="mmd",
    )

    # LlamaParse — pure cloud, no local OCR
    Config(
        api_key="...",
        cloud_ocr_provider="llama_parse",
        cloud_ocr_strategy="cloud",      # skip local entirely
    )
    """

    def __init__(
        self,
        # ── Core ──────────────────────────────────────────────────────────────
        api_key:              Optional[str]       = None,
        host:                 Optional[str]       = None,
        service_port:         Optional[int]       = None,
        timeout:              Optional[int]       = None,
        max_retries:          Optional[int]       = None,
        # CHANGED: default 100 → 10  (max chunk files per API call)
        max_files_per_batch:        Optional[int]  = None,
        # CHANGED: type int → float, default 1 → 0.5 (500 KB per API call)
        max_batch_size_mb:          Optional[float] = None,
        # ── Streaming mode ────────────────────────────────────────────────────
        # Upload begins once this many chunks are ready (or size threshold hit).
        # Smaller → first upload fires sooner; larger → fewer round-trips.
        streaming_minibatch_files:    Optional[int]   = None,
        # CHANGED: type int → float, default 5 → 0.5  (must be >= max_batch_size_mb)
        streaming_minibatch_size_mb:  Optional[float] = None,
        # Parallel extraction threads for create_batches / streaming_create_batches.
        # Default 4 — good for I/O-bound OCR; set to 1 for sequential behaviour.
        extraction_workers:           Optional[int]   = None,
        # Max minibatch uploads in flight at once in streaming mode.
        # Default 1 — sequential uploads, one at a time (original behaviour).
        streaming_max_concurrent_batches: Optional[int] = None,
        vector_lake_path:     Optional[str]       = None,
        log_dir:              Optional[str]       = None,
        allowed_extensions:   Optional[List[str]] = None,

        # ── Local OCR ─────────────────────────────────────────────────────────
        enable_ocr:           Optional[bool]      = None,
        ocr_lang:             Optional[str]       = None,

        # ── Cloud OCR — universal ─────────────────────────────────────────────
        cloud_ocr_provider:   Optional[str]       = None,
        # "hybrid" = local first + cloud fallback  |  "cloud" = cloud only
        cloud_ocr_strategy:   Optional[str]       = None,
        # Quality threshold for hybrid mode (chars; below this → use cloud)
        cloud_ocr_threshold:  Optional[int]       = None,

        # ── AWS Textract ──────────────────────────────────────────────────────
        aws_region:             Optional[str]     = None,
        # "text" = detect_document_text  |  "tables" = analyze_document
        aws_textract_mode:      Optional[str]     = None,
        aws_access_key_id:      Optional[str]     = None,
        aws_secret_access_key:  Optional[str]     = None,
        aws_session_token:      Optional[str]     = None,
        aws_s3_bucket:          Optional[str]     = None,

        # ── Google Cloud Vision ───────────────────────────────────────────────
        # "document" = document_text_detection  |  "image" = text_detection
        gcp_vision_mode:        Optional[str]     = None,
        gcp_credentials_json:   Optional[str]     = None,   # path to .json key
        gcp_language_hints:     Optional[List[str]] = None, # e.g. ["en", "de"]

        # ── Azure Document Intelligence ───────────────────────────────────────
        azure_endpoint:         Optional[str]     = None,
        azure_api_key:          Optional[str]     = None,
        # any prebuilt-* or custom model ID
        azure_model_id:         Optional[str]     = None,
        azure_include_tables:   Optional[bool]    = None,

        # ── Mathpix ───────────────────────────────────────────────────────────
        mathpix_app_id:         Optional[str]     = None,
        mathpix_app_key:        Optional[str]     = None,
        # "markdown" | "mmd" | "text" | "latex"
        mathpix_output_format:  Optional[str]     = None,

        # ── LlamaParse ────────────────────────────────────────────────────────
        llama_parse_api_key:    Optional[str]     = None,
        # "markdown" | "text"
        llama_parse_result_type: Optional[str]   = None,
        llama_parse_language:   Optional[str]     = None,
    ):
        # ── Core ──────────────────────────────────────────────────────────────
        self.api_key: Optional[str] = api_key or _env_str("VECTOR_LAKE_API_KEY")
        self.host: str = (
            (host or _env_str("VECTOR_LAKE_HOST", "https://waveflow-analytics.com"))
            .rstrip("/")
        )
        self.service_port: Optional[int] = (
            service_port if service_port is not None
            else (_env_int("VECTOR_LAKE_PORT", 0) or None)
        )

        # ── IP-address hosts require an explicit service_port ────────────────
        # Domain hosts (https://waveflow-analytics.com) are routed by nginx
        # via URL path and need no port. A bare IP has no reverse proxy in
        # front of it — without a port, base_url_query/base_url_upload fall
        # back to "{host}/query" and "{host}/upload" with no port at all,
        # which resolves to HTTP's default port 80 and fails with a
        # confusing "connection refused" instead of a clear config error.
        # We default it to 8030 (the standard service port) so IP hosts
        # work out of the box; pass service_port=... explicitly to override.
        if self.service_port is None and _is_ip_host(self.host):
            self.service_port = 8030
            logger.warning(
                "host=%r looks like a raw IP address with no service_port "
                "set — defaulting service_port to %d (query %s:%d, upload "
                "%s:%d). Pass service_port=... explicitly to override or "
                "silence this warning.",
                self.host, self.service_port,
                self.host, self.service_port,
                self.host, self.service_port + 1,
            )
        self.timeout: int           = timeout          or _env_int("VECTOR_LAKE_TIMEOUT",              300)
        self.max_retries: int       = max_retries      or _env_int("VECTOR_LAKE_MAX_RETRIES",            2)

        # CHANGED: use is-not-None guard (handles 0 correctly); default 100 → 10
        self.max_files_per_batch: int = (
            max_files_per_batch
            if max_files_per_batch is not None
            else _env_int("VECTOR_LAKE_MAX_FILES_PER_BATCH", 1000)
        )

        # CHANGED: float type via _env_float; default 1 MB → 0.5 MB (500 KB)
        self.max_batch_size_mb: float = (
            max_batch_size_mb
            if max_batch_size_mb is not None
            else _env_float("VECTOR_LAKE_MAX_BATCH_SIZE_MB", .5)
        )

        # CHANGED: default 10 → 100 — acts as a safety ceiling for very small
        # chunks; in practice the 0.5 MB size threshold fires first for normal
        # text/PDF chunks, so 100 is effectively a backstop not a primary limit.
        self.streaming_minibatch_files: int = (
            streaming_minibatch_files
            if streaming_minibatch_files is not None
            else _env_int("VECTOR_LAKE_STREAMING_MINIBATCH_FILES", 100)
        )

        # CHANGED: float type; default 5 MB → 0.5 MB — must stay >= max_batch_size_mb
        self.streaming_minibatch_size_mb: float = (
            streaming_minibatch_size_mb
            if streaming_minibatch_size_mb is not None
            else _env_float("VECTOR_LAKE_STREAMING_MINIBATCH_SIZE_MB", 0.5)
        )

        # CHANGED: validate that minibatch threshold is at least one batch's worth
        if self.streaming_minibatch_size_mb < self.max_batch_size_mb:
            raise ConfigError(
                f"streaming_minibatch_size_mb ({self.streaming_minibatch_size_mb}) "
                f"must be >= max_batch_size_mb ({self.max_batch_size_mb}). "
                f"Otherwise the pending buffer can never fill a complete batch."
            )

        self.extraction_workers: int = (
            extraction_workers
            if extraction_workers is not None
            else _env_int("VECTOR_LAKE_EXTRACTION_WORKERS", 4)
        )

        self.streaming_max_concurrent_batches: int = (
            streaming_max_concurrent_batches
            if streaming_max_concurrent_batches is not None
            else _env_int("VECTOR_LAKE_STREAMING_MAX_CONCURRENT_BATCHES", 1)
        )
        if self.streaming_max_concurrent_batches < 1:
            raise ConfigError(
                f"streaming_max_concurrent_batches "
                f"({self.streaming_max_concurrent_batches}) must be >= 1."
            )

        self.vector_lake_path: str  = vector_lake_path or _env_str("VECTOR_LAKE_PATH",    "upload")
        self.log_dir: str           = log_dir          or _env_str("VECTOR_LAKE_LOG_DIR", "logs")

        self.allowed_extensions: List[str] = (
            allowed_extensions
            or _env_list("VECTOR_LAKE_ALLOWED_EXTENSIONS", _DEFAULT_EXTENSIONS)
        )

        # ── Local OCR ─────────────────────────────────────────────────────────
        self.enable_ocr: bool = (
            enable_ocr if enable_ocr is not None
            else _env_bool("VECTOR_LAKE_ENABLE_OCR", True)
        )
        self.ocr_lang: str = ocr_lang or _env_str("VECTOR_LAKE_OCR_LANG", "eng")

        # ── Cloud OCR — universal ─────────────────────────────────────────────
        raw_provider = (
            cloud_ocr_provider
            or _env_str("VECTOR_LAKE_CLOUD_OCR_PROVIDER")
        )
        self.cloud_ocr_provider: Optional[str] = (
            raw_provider.lower().replace("-", "_") if raw_provider else None
        )
        if self.cloud_ocr_provider and self.cloud_ocr_provider not in _CLOUD_PROVIDERS:
            raise ConfigError(
                f"Unknown cloud_ocr_provider={self.cloud_ocr_provider!r}. "
                f"Valid options: {', '.join(sorted(_CLOUD_PROVIDERS))}"
            )

        self.cloud_ocr_strategy: str = (
            cloud_ocr_strategy
            or _env_str("VECTOR_LAKE_CLOUD_OCR_STRATEGY", "hybrid")
        ).lower()
        if self.cloud_ocr_strategy not in ("hybrid", "cloud"):
            raise ConfigError(
                f"cloud_ocr_strategy must be 'hybrid' or 'cloud', "
                f"got {self.cloud_ocr_strategy!r}"
            )

        self.cloud_ocr_threshold: int = (
            cloud_ocr_threshold
            if cloud_ocr_threshold is not None
            else _env_int("VECTOR_LAKE_CLOUD_OCR_THRESHOLD", 100)
        )

        # ── AWS ───────────────────────────────────────────────────────────────
        self.aws_region: str            = aws_region          or _env_str("AWS_DEFAULT_REGION",        "us-east-1")
        self.aws_textract_mode: str     = aws_textract_mode   or _env_str("VECTOR_LAKE_AWS_MODE",      "text")
        self.aws_access_key_id          = aws_access_key_id   or _env_str("AWS_ACCESS_KEY_ID")
        self.aws_secret_access_key      = aws_secret_access_key or _env_str("AWS_SECRET_ACCESS_KEY")
        self.aws_session_token          = aws_session_token   or _env_str("AWS_SESSION_TOKEN")
        self.aws_s3_bucket              = aws_s3_bucket        or _env_str("VECTOR_LAKE_AWS_S3_BUCKET")

        # ── GCP ───────────────────────────────────────────────────────────────
        self.gcp_vision_mode: str       = gcp_vision_mode      or _env_str("VECTOR_LAKE_GCP_MODE",        "document")
        self.gcp_credentials_json       = gcp_credentials_json or _env_str("GOOGLE_APPLICATION_CREDENTIALS")
        self.gcp_language_hints: Optional[List[str]] = (
            gcp_language_hints
            or ([x for x in _env_str("VECTOR_LAKE_GCP_LANGUAGE_HINTS", "").split(",") if x] or None)
        )

        # ── Azure ─────────────────────────────────────────────────────────────
        self.azure_endpoint             = azure_endpoint        or _env_str("AZURE_FORM_RECOGNIZER_ENDPOINT")
        self.azure_api_key              = azure_api_key         or _env_str("AZURE_FORM_RECOGNIZER_KEY")
        self.azure_model_id: str        = azure_model_id        or _env_str("VECTOR_LAKE_AZURE_MODEL_ID", "prebuilt-read")
        self.azure_include_tables: bool = (
            azure_include_tables if azure_include_tables is not None
            else _env_bool("VECTOR_LAKE_AZURE_INCLUDE_TABLES", True)
        )

        # ── Mathpix ───────────────────────────────────────────────────────────
        self.mathpix_app_id             = mathpix_app_id       or _env_str("MATHPIX_APP_ID")
        self.mathpix_app_key            = mathpix_app_key      or _env_str("MATHPIX_APP_KEY")
        self.mathpix_output_format: str = (
            mathpix_output_format or _env_str("VECTOR_LAKE_MATHPIX_FORMAT", "markdown")
        )

        # ── LlamaParse ────────────────────────────────────────────────────────
        self.llama_parse_api_key        = llama_parse_api_key      or _env_str("LLAMA_CLOUD_API_KEY")
        self.llama_parse_result_type: str = (
            llama_parse_result_type or _env_str("VECTOR_LAKE_LLAMA_RESULT_TYPE", "markdown")
        )
        self.llama_parse_language: str  = (
            llama_parse_language or _env_str("VECTOR_LAKE_LLAMA_LANGUAGE", "en")
        )

        # ── Validate & create directories ──────────────────────────────────────
        if not self.api_key:
            raise ConfigError(
                "API key is required. Pass api_key= or set VECTOR_LAKE_API_KEY."
            )

        Path(self.log_dir).mkdir(parents=True, exist_ok=True)
        Path(self.vector_lake_path).mkdir(parents=True, exist_ok=True)

    # ── URL helpers ───────────────────────────────────────────────────────────

    @property
    def base_url_query(self) -> str:
        return f"{self.host}:{self.service_port}" if self.service_port else f"{self.host}/query"

    @property
    def base_url_upload(self) -> str:
        return f"{self.host}:{self.service_port + 1}" if self.service_port else f"{self.host}/upload"

    @property
    def endpoints(self) -> Dict[str, str]:
        if not hasattr(self, "_endpoints_cache"):
            q, u = self.base_url_query, self.base_url_upload
            self._endpoints_cache = {
                "top_matching_docs":            f"{q}/top_matching_docs",
                "top_matching_docs_with_data":  f"{q}/top_matching_docs_with_data",
                "full_corpus_search":           f"{q}/full_corpus_search",
                "add_docs":                     f"{u}/add_docs",
                "refresh_docs":                 f"{u}/refresh_docs",
                "delete_docs":                  f"{u}/delete_docs",
                "insert_docs":                  f"{u}/insert_docs",
                "health":                       f"{u}/health",
                "get_namespace_details_by_userid": f"{u}/get_namespace_details_by_userid",
                "get_docs_information":         f"{u}/get_docs_information",
            }
        return self._endpoints_cache

    # CHANGED: repr shows KB when < 1 MB for clarity
    def __repr__(self) -> str:
        cloud = self.cloud_ocr_provider or "none"
        mb = self.max_batch_size_mb
        size_str = f"{int(mb * 1024)} KB" if mb < 1.0 else f"{mb} MB"
        return (
            f"Config(host={self.host!r}, port={self.service_port}, "
            f"batch={self.max_files_per_batch} files/{size_str}, "
            f"extraction_workers={self.extraction_workers}, "
            f"streaming_max_concurrent_batches={self.streaming_max_concurrent_batches}, "
            f"local_ocr={self.enable_ocr}, ocr_lang={self.ocr_lang!r}, "
            f"cloud_ocr={cloud!r}, strategy={self.cloud_ocr_strategy!r})"
        )