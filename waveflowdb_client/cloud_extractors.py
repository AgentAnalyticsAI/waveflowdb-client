"""
cloud_extractors.py — Paid cloud OCR engines for VectorLake SDK.

Supported engines
─────────────────
Provider               Class                          Best for
─────────────────────  ─────────────────────────────  ──────────────────────────────────────────
AWS Textract           AWSTextractExtractor           Tables, forms, handwriting, multi-column
Google Cloud Vision    GCPVisionExtractor             Highest accuracy, 100+ languages
Azure Doc Intelligence AzureDocIntelligenceExtractor  Forms, invoices, receipts, custom models
Mathpix                MathpixExtractor               Math, scientific papers, LaTeX equations
LlamaParse             LlamaParseExtractor            LLM-native parsing, complex layouts, MDX

All extractors
──────────────
• Return ExtractedContent (same dataclass as local extractors).
• Wrap every OCR-derived span in image-origin markers so chunk files on disk
  carry full provenance:

    <<<IMAGE_EXTRACT source="scan.pdf" page=2 image_index=1 method="aws_textract">>>
    ... extracted text ...
    <<<END_IMAGE_EXTRACT>>>

• The `method` field always names the engine that produced the block.

Factory / auto-selection
────────────────────────
Use CloudOCRFactory.build(config) to build the right extractor from Config.

Hybrid strategy
───────────────
HybridExtractor tries local first (fast, free), falls back to cloud when
local text is below a quality threshold. Markers reflect the actual engine.

Install extras (only what you use)
────────────────────────────────────
pip install boto3                            # AWS Textract
pip install google-cloud-vision              # GCP Vision
pip install azure-ai-documentintelligence   # Azure Doc Intelligence
pip install requests                         # Mathpix + LlamaParse (REST APIs)
pip install llama-parse                      # LlamaParse (optional; SDK wrapper)
"""

from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Re-use marker helpers and ExtractedContent from local extractors ──────────

try:
    from .extractors import ExtractedContent, _wrap_image_text
except ImportError:
    from extractors import ExtractedContent, _wrap_image_text  # type: ignore


# ── Optional SDK imports (each individually guarded) ─────────────────────────

try:
    import boto3 as _boto3
    import botocore.exceptions as _botocore_exc
except ImportError:
    _boto3 = None
    _botocore_exc = None

try:
    from google.cloud import vision as _gcp_vision
except ImportError:
    _gcp_vision = None

try:
    from azure.ai.documentintelligence import DocumentIntelligenceClient as _AzureClient
    from azure.ai.documentintelligence.models import AnalyzeDocumentRequest as _AzureRequest
    from azure.core.credentials import AzureKeyCredential as _AzureKey
    _AZURE_NEW_SDK = True
except ImportError:
    try:
        from azure.ai.formrecognizer import DocumentAnalysisClient as _AzureClient  # type: ignore
        from azure.core.credentials import AzureKeyCredential as _AzureKey  # type: ignore
        _AzureRequest = None
        _AZURE_NEW_SDK = False
    except ImportError:
        _AzureClient = None
        _AzureKey = None
        _AzureRequest = None
        _AZURE_NEW_SDK = False

try:
    import requests as _requests
except ImportError:
    _requests = None


# ─────────────────────────────────────────────────────────────────────────────
#  Base class
# ─────────────────────────────────────────────────────────────────────────────

class CloudOCRExtractor(ABC):
    """
    Base class for all cloud OCR engines.

    Subclasses implement `extract()` and return ExtractedContent so their
    output integrates seamlessly with chunking, marker-parsing, and the
    HybridExtractor.

    The `engine_name` class attribute appears in every image-origin marker
    written by this extractor.
    """

    engine_name: str = "cloud_ocr"

    @abstractmethod
    def extract(self, file_path: Path) -> ExtractedContent:
        """Extract text from a document and return marked-up ExtractedContent."""

    def extract_text(self, file_path: Path) -> str:
        """Convenience: returns full_text (markers included)."""
        return self.extract(file_path).full_text

    # ── Shared builder ────────────────────────────────────────────────────────

    def _make_content(
        self,
        file_path: Path,
        pages: List[str],
        metadata: Dict[str, Any],
    ) -> ExtractedContent:
        """
        Wrap per-page / per-region text strings in image-origin markers and
        return an ExtractedContent whose full_text carries complete provenance.
        """
        source    = file_path.name
        annotated: List[str] = []
        for i, page_text in enumerate(pages):
            if page_text.strip():
                annotated.append(
                    _wrap_image_text(
                        page_text,
                        source=source,
                        page=i + 1,
                        image_index=i + 1,
                        method=self.engine_name,
                    )
                )
        return ExtractedContent(
            text="",
            images_text=annotated,
            metadata=metadata,
        )


# ─────────────────────────────────────────────────────────────────────────────
#  AWS Textract
# ─────────────────────────────────────────────────────────────────────────────

class AWSTextractExtractor(CloudOCRExtractor):
    """
    AWS Textract OCR.

    Modes
    ─────
    "text"   — detect_document_text (~$1.50 / 1 000 pages)
               Fast plain-text extraction in reading order.
    "tables" — analyze_document with TABLES + FORMS (~$15 / 1 000 pages)
               Tables rendered as tab-separated rows; form fields captured.

    Authentication
    ──────────────
    boto3 credential chain (first match wins):
      1. Constructor kwargs: aws_access_key_id / aws_secret_access_key
      2. Env vars: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY
      3. ~/.aws/credentials or IAM instance role

    Large files (> 5 MB)
    ────────────────────
    Textract's sync API accepts up to 5 MB. For larger files pass s3_bucket=
    and the SDK uploads to S3 then polls the async job automatically.

    Pricing reference  https://aws.amazon.com/textract/pricing/
    """

    engine_name  = "aws_textract"
    _SYNC_LIMIT  = 5 * 1024 * 1024   # 5 MB

    def __init__(
        self,
        region_name:           str           = "us-east-1",
        mode:                  str           = "text",        # "text" | "tables"
        aws_access_key_id:     Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token:     Optional[str] = None,
        s3_bucket:             Optional[str] = None,
        poll_interval:         int           = 5,
    ):
        if not _boto3:
            raise ImportError("boto3 is not installed.\nRun: pip install boto3")
        if mode not in ("text", "tables"):
            raise ValueError(f"mode must be 'text' or 'tables', got {mode!r}")

        self.mode          = mode
        self.s3_bucket     = s3_bucket
        self.poll_interval = poll_interval

        session_kw: Dict[str, Any] = {"region_name": region_name}
        if aws_access_key_id:
            session_kw["aws_access_key_id"]     = aws_access_key_id
            session_kw["aws_secret_access_key"] = aws_secret_access_key
            if aws_session_token:
                session_kw["aws_session_token"] = aws_session_token

        session        = _boto3.Session(**session_kw)
        self._textract = session.client("textract")
        self._s3       = session.client("s3") if s3_bucket else None

    # ── Public ────────────────────────────────────────────────────────────────

    def extract(self, file_path: Path) -> ExtractedContent:
        file_bytes = file_path.read_bytes()

        if len(file_bytes) > self._SYNC_LIMIT and not self.s3_bucket:
            raise ValueError(
                f"{file_path.name} is {len(file_bytes)//1024//1024} MB, which exceeds "
                f"Textract's 5 MB inline limit. Set s3_bucket= to use async S3 upload."
            )

        if len(file_bytes) > self._SYNC_LIMIT:
            pages    = self._extract_async_via_s3(file_path, file_bytes)
            metadata = {"source": "s3_async", "mode": self.mode}
        else:
            pages, metadata = self._extract_sync(file_bytes)

        return self._make_content(file_path, pages, metadata)

    # ── Sync path ─────────────────────────────────────────────────────────────

    def _extract_sync(self, file_bytes: bytes) -> tuple:
        doc = {"Bytes": file_bytes}
        if self.mode == "tables":
            resp  = self._textract.analyze_document(
                Document=doc, FeatureTypes=["TABLES", "FORMS"]
            )
            text  = self._blocks_to_text(resp["Blocks"])
            meta  = {
                "mode":         "tables",
                "block_count":  len(resp["Blocks"]),
                "tables_found": sum(1 for b in resp["Blocks"] if b["BlockType"] == "TABLE"),
            }
        else:
            resp  = self._textract.detect_document_text(Document=doc)
            lines = [b["Text"] for b in resp["Blocks"] if b["BlockType"] == "LINE"]
            text  = "\n".join(lines)
            meta  = {"mode": "text", "line_count": len(lines)}
        return [text], meta

    # ── Async path (S3) ───────────────────────────────────────────────────────

    def _extract_async_via_s3(self, file_path: Path, file_bytes: bytes) -> List[str]:
        s3_key = f"vectorlake-ocr-tmp/{file_path.name}"
        logger.info("Uploading %s → s3://%s/%s", file_path.name, self.s3_bucket, s3_key)
        self._s3.put_object(Bucket=self.s3_bucket, Key=s3_key, Body=file_bytes)
        s3_obj = {"Bucket": self.s3_bucket, "Name": s3_key}

        if self.mode == "tables":
            job_id = self._textract.start_document_analysis(
                DocumentLocation={"S3Object": s3_obj},
                FeatureTypes=["TABLES", "FORMS"],
            )["JobId"]
            blocks = self._poll("get_document_analysis", job_id)
        else:
            job_id = self._textract.start_document_text_detection(
                DocumentLocation={"S3Object": s3_obj}
            )["JobId"]
            blocks = self._poll("get_document_text_detection", job_id)

        try:
            self._s3.delete_object(Bucket=self.s3_bucket, Key=s3_key)
        except Exception:
            pass

        return [self._blocks_to_text(blocks)]

    def _poll(self, method_name: str, job_id: str) -> List[dict]:
        method     = getattr(self._textract, method_name)
        blocks:    List[dict] = []
        next_token: Optional[str] = None

        while True:
            kw: Dict[str, Any] = {"JobId": job_id}
            if next_token:
                kw["NextToken"] = next_token
            resp   = method(**kw)
            status = resp["JobStatus"]
            if status == "FAILED":
                raise RuntimeError(f"Textract job {job_id} failed: {resp.get('StatusMessage')}")
            if status == "IN_PROGRESS":
                logger.info("Textract job %s in progress — waiting %ds …", job_id, self.poll_interval)
                time.sleep(self.poll_interval)
                continue
            blocks.extend(resp.get("Blocks", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break

        return blocks

    # ── Block → text renderer ─────────────────────────────────────────────────

    @staticmethod
    def _blocks_to_text(blocks: List[dict]) -> str:
        word_map = {b["Id"]: b.get("Text", "") for b in blocks if b["BlockType"] == "WORD"}

        def _cell_text(block: dict) -> str:
            words = []
            for rel in block.get("Relationships", []):
                if rel["Type"] == "CHILD":
                    words.extend(word_map.get(iid, "") for iid in rel["Ids"])
            return " ".join(w for w in words if w)

        lines:           List[str] = []
        rendered_tables: set       = set()

        for block in blocks:
            bt = block["BlockType"]
            if bt == "LINE":
                lines.append(block.get("Text", ""))
            elif bt == "TABLE" and block["Id"] not in rendered_tables:
                rendered_tables.add(block["Id"])
                cells: Dict[tuple, str] = {}
                max_row = max_col = 0
                for rel in block.get("Relationships", []):
                    if rel["Type"] == "CHILD":
                        for cid in rel["Ids"]:
                            cb = next((b for b in blocks if b["Id"] == cid), None)
                            if cb and cb["BlockType"] == "CELL":
                                r, c = cb["RowIndex"], cb["ColumnIndex"]
                                cells[(r, c)] = _cell_text(cb)
                                max_row = max(max_row, r)
                                max_col = max(max_col, c)
                if cells:
                    lines.append("[TABLE]")
                    for r in range(1, max_row + 1):
                        lines.append("\t".join(cells.get((r, c), "") for c in range(1, max_col + 1)))
                    lines.append("[/TABLE]")

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Google Cloud Vision
# ─────────────────────────────────────────────────────────────────────────────

class GCPVisionExtractor(CloudOCRExtractor):
    """
    Google Cloud Vision OCR.

    Modes
    ─────
    "document" — document_text_detection (default; dense text, PDFs)
    "image"    — text_detection (faster; labels, signs, sparse text)

    Authentication
    ──────────────
    1. credentials_json  — path to service-account JSON key file
    2. credentials_dict  — already-loaded dict (from secrets manager)
    3. GOOGLE_APPLICATION_CREDENTIALS env var
    4. gcloud Application Default Credentials

    Pricing reference  https://cloud.google.com/vision/pricing
    (~$1.50 / 1 000 images; first 1 000 free per month)
    """

    engine_name = "gcp_vision"

    def __init__(
        self,
        mode:             str                  = "document",
        credentials_json: Optional[Path]       = None,
        credentials_dict: Optional[dict]       = None,
        language_hints:   Optional[List[str]]  = None,
    ):
        if not _gcp_vision:
            raise ImportError(
                "google-cloud-vision is not installed.\n"
                "Run: pip install google-cloud-vision"
            )
        if mode not in ("document", "image"):
            raise ValueError(f"mode must be 'document' or 'image', got {mode!r}")

        self.mode           = mode
        self.language_hints = language_hints or []

        if credentials_dict:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(
                credentials_dict,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            self._client = _gcp_vision.ImageAnnotatorClient(credentials=creds)
        elif credentials_json:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(
                str(credentials_json),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            self._client = _gcp_vision.ImageAnnotatorClient(credentials=creds)
        else:
            self._client = _gcp_vision.ImageAnnotatorClient()

    def extract(self, file_path: Path) -> ExtractedContent:
        content       = file_path.read_bytes()
        image         = _gcp_vision.Image(content=content)
        image_context = (
            _gcp_vision.ImageContext(language_hints=self.language_hints)
            if self.language_hints else None
        )

        call_kw: Dict[str, Any] = {"image": image}
        if image_context:
            call_kw["image_context"] = image_context

        if self.mode == "document":
            resp = self._client.document_text_detection(**call_kw)
        else:
            resp = self._client.text_detection(**call_kw)

        if resp.error.message:
            raise RuntimeError(f"GCP Vision error: {resp.error.message}")

        raw = ""
        if resp.full_text_annotation:
            raw = resp.full_text_annotation.text
        elif resp.text_annotations:
            raw = resp.text_annotations[0].description

        page_count = len(resp.full_text_annotation.pages) if resp.full_text_annotation else 1
        metadata   = {"mode": self.mode, "page_count": page_count}

        return self._make_content(file_path, [raw], metadata)


# ─────────────────────────────────────────────────────────────────────────────
#  Azure Document Intelligence
# ─────────────────────────────────────────────────────────────────────────────

class AzureDocIntelligenceExtractor(CloudOCRExtractor):
    """
    Azure Document Intelligence OCR (formerly Azure Form Recognizer).

    Pre-built models
    ────────────────
    "prebuilt-read"       — general OCR; all file types      (~$1.50 / 1 000 pages)
    "prebuilt-layout"     — layout + tables + checkboxes     (~$10   / 1 000 pages)
    "prebuilt-invoice"    — invoice field extraction         (~$10   / 1 000 pages)
    "prebuilt-receipt"    — receipt parsing                  (~$1.50 / 1 000 pages)
    "prebuilt-idDocument" — passports, driver's licences
    Any custom model ID from your Azure portal

    Authentication
    ──────────────
    Pass endpoint= and api_key=, or set env vars:
        AZURE_FORM_RECOGNIZER_ENDPOINT
        AZURE_FORM_RECOGNIZER_KEY

    Pricing reference  https://azure.microsoft.com/en-us/pricing/details/ai-document-intelligence/

    Install
    ───────
    pip install azure-ai-documentintelligence    (new SDK — preferred)
    pip install azure-ai-formrecognizer          (legacy SDK — also supported)
    """

    engine_name = "azure_doc_intelligence"

    def __init__(
        self,
        endpoint:       Optional[str] = None,
        api_key:        Optional[str] = None,
        model_id:       str           = "prebuilt-read",
        include_tables: bool          = True,
    ):
        if not _AzureClient:
            raise ImportError(
                "Azure SDK not installed.\n"
                "Run: pip install azure-ai-documentintelligence\n"
                "  or: pip install azure-ai-formrecognizer  (legacy)"
            )

        endpoint = endpoint or os.getenv("AZURE_FORM_RECOGNIZER_ENDPOINT", "")
        api_key  = api_key  or os.getenv("AZURE_FORM_RECOGNIZER_KEY",      "")

        if not endpoint or not api_key:
            raise ValueError(
                "Azure endpoint and api_key are required. "
                "Pass them or set AZURE_FORM_RECOGNIZER_ENDPOINT / AZURE_FORM_RECOGNIZER_KEY."
            )

        self._client       = _AzureClient(endpoint=endpoint, credential=_AzureKey(api_key))
        self.model_id      = model_id
        self.include_tables = include_tables

    def extract(self, file_path: Path) -> ExtractedContent:
        with open(file_path, "rb") as fh:
            if _AZURE_NEW_SDK and _AzureRequest:
                poller = self._client.begin_analyze_document(
                    self.model_id,
                    analyze_request=fh,
                    content_type="application/octet-stream",
                )
            else:
                poller = self._client.begin_analyze_document(self.model_id, document=fh)

        result = poller.result()
        pages  = self._render(result)
        metadata = {
            "model_id":    self.model_id,
            "page_count":  len(result.pages)  if hasattr(result, "pages")  else 1,
            "table_count": len(result.tables) if hasattr(result, "tables") else 0,
        }
        return self._make_content(file_path, pages, metadata)

    def _render(self, result: Any) -> List[str]:
        pages: List[str] = []
        if hasattr(result, "pages"):
            for page in result.pages:
                lines = [line.content for line in (page.lines or [])]
                text  = "\n".join(lines)
                if self.include_tables and hasattr(result, "tables"):
                    for table in result.tables:
                        on_this_page = any(
                            getattr(br, "page_number", None) == page.page_number
                            for cell in (table.cells or [])
                            for br   in (cell.bounding_regions or [])
                        )
                        if on_this_page:
                            text += "\n" + self._render_table(table)
                pages.append(text)
        elif hasattr(result, "content"):
            pages.append(result.content)
        return pages

    @staticmethod
    def _render_table(table: Any) -> str:
        rows: Dict[int, Dict[int, str]] = {}
        for cell in (table.cells or []):
            rows.setdefault(cell.row_index, {})[cell.column_index] = cell.content or ""
        lines = ["[TABLE]"]
        for r in sorted(rows):
            col_max = max(rows[r].keys()) if rows[r] else 0
            lines.append("\t".join(rows[r].get(c, "") for c in range(col_max + 1)))
        lines.append("[/TABLE]")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
#  Mathpix  (math / scientific documents)
# ─────────────────────────────────────────────────────────────────────────────

class MathpixExtractor(CloudOCRExtractor):
    """
    Mathpix OCR — purpose-built for scientific and mathematical content.

    Best for
    ────────
    • Academic papers with inline and display LaTeX
    • Textbooks, technical reports, chemical formulas
    • Any document where equation fidelity matters

    Output formats
    ──────────────
    "markdown" — Markdown with $…$ / $$…$$ math (recommended for RAG)
    "mmd"      — Mathpix Markdown (superset; best table support)
    "text"     — plain text with LaTeX equations
    "latex"    — full LaTeX document

    Authentication
    ──────────────
    Pass app_id= and app_key=, or set:
        MATHPIX_APP_ID
        MATHPIX_APP_KEY

    Pricing reference  https://mathpix.com/pricing
    (Free: 20 req/month; paid from ~$0.004 / page)

    Install
    ───────
    pip install requests   (already a common dependency)
    """

    engine_name = "mathpix"
    _API_URL    = "https://api.mathpix.com/v3/pdf"

    def __init__(
        self,
        app_id:        Optional[str] = None,
        app_key:       Optional[str] = None,
        output_format: str           = "markdown",
        poll_interval: int           = 3,
        max_wait:      int           = 300,
    ):
        if not _requests:
            raise ImportError("requests is not installed.\nRun: pip install requests")

        self.app_id        = app_id  or os.getenv("MATHPIX_APP_ID",  "")
        self.app_key       = app_key or os.getenv("MATHPIX_APP_KEY", "")
        self.output_format = output_format
        self.poll_interval = poll_interval
        self.max_wait      = max_wait

        if not self.app_id or not self.app_key:
            raise ValueError(
                "Mathpix app_id and app_key are required. "
                "Pass them or set MATHPIX_APP_ID / MATHPIX_APP_KEY."
            )

    def extract(self, file_path: Path) -> ExtractedContent:
        # 1. Upload PDF
        with open(file_path, "rb") as fh:
            resp = _requests.post(
                self._API_URL,
                headers={"app_id": self.app_id, "app_key": self.app_key},
                files={"file": (file_path.name, fh, "application/pdf")},
                data={
                    "options_json": json.dumps({
                        "conversion_formats": {self.output_format: True},
                        "math_inline_delimiters":  ["$",  "$"],
                        "math_display_delimiters": ["$$", "$$"],
                    })
                },
                timeout=60,
            )
        resp.raise_for_status()
        pdf_id = resp.json()["pdf_id"]
        logger.info("Mathpix job submitted: pdf_id=%s", pdf_id)

        # 2. Poll for completion
        waited = 0
        while waited < self.max_wait:
            time.sleep(self.poll_interval)
            waited += self.poll_interval
            s = _requests.get(
                f"{self._API_URL}/{pdf_id}",
                headers={"app_id": self.app_id, "app_key": self.app_key},
                timeout=30,
            )
            s.raise_for_status()
            status = s.json().get("status", "")
            logger.debug("Mathpix pdf_id=%s status=%s (%ds)", pdf_id, status, waited)
            if status == "completed":
                break
            if status == "error":
                raise RuntimeError(f"Mathpix failed for {file_path.name}")
        else:
            raise TimeoutError(f"Mathpix job did not complete within {self.max_wait}s")

        # 3. Download result
        r = _requests.get(
            f"{self._API_URL}/{pdf_id}.{self.output_format}",
            headers={"app_id": self.app_id, "app_key": self.app_key},
            timeout=60,
        )
        r.raise_for_status()
        full_text = r.text

        metadata = {"pdf_id": pdf_id, "output_format": self.output_format}
        return self._make_content(file_path, [full_text], metadata)


# ─────────────────────────────────────────────────────────────────────────────
#  LlamaParse  (LLM-native document parsing)
# ─────────────────────────────────────────────────────────────────────────────

class LlamaParseExtractor(CloudOCRExtractor):
    """
    LlamaParse — LLM-native document parsing by LlamaIndex.

    Best for
    ────────
    • Complex mixed-layout PDFs (images, tables, charts side-by-side)
    • RAG pipelines where downstream is an LLM
    • Docs where semantic structure matters more than pixel layout

    Output formats
    ──────────────
    "markdown" — Markdown with table support (recommended for RAG)
    "text"     — plain text

    Authentication
    ──────────────
    Pass api_key=, or set:
        LLAMA_CLOUD_API_KEY

    Pricing reference  https://cloud.llamaindex.ai/
    (Free: 1 000 pages/day; paid from ~$0.003 / page)

    Install
    ───────
    pip install requests    (REST API — no extra package required)
    """

    engine_name = "llama_parse"
    _API_BASE   = "https://api.cloud.llamaindex.ai/api/parsing"

    def __init__(
        self,
        api_key:               Optional[str] = None,
        result_type:           str           = "markdown",
        language:              str           = "en",
        poll_interval:         int           = 3,
        max_wait:              int           = 300,
        skip_diagonal_text:    bool          = False,
        do_not_unroll_columns: bool          = False,
    ):
        if not _requests:
            raise ImportError("requests is not installed.\nRun: pip install requests")

        self.api_key               = api_key or os.getenv("LLAMA_CLOUD_API_KEY", "")
        self.result_type           = result_type
        self.language              = language
        self.poll_interval         = poll_interval
        self.max_wait              = max_wait
        self.skip_diagonal_text    = skip_diagonal_text
        self.do_not_unroll_columns = do_not_unroll_columns

        if not self.api_key:
            raise ValueError(
                "LlamaParse api_key is required. "
                "Pass it or set LLAMA_CLOUD_API_KEY."
            )

    @property
    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def extract(self, file_path: Path) -> ExtractedContent:
        # 1. Upload
        with open(file_path, "rb") as fh:
            up = _requests.post(
                f"{self._API_BASE}/upload",
                headers=self._auth,
                files={"file": (file_path.name, fh, "application/octet-stream")},
                data={
                    "language":              self.language,
                    "skip_diagonal_text":    str(self.skip_diagonal_text).lower(),
                    "do_not_unroll_columns": str(self.do_not_unroll_columns).lower(),
                },
                timeout=60,
            )
        up.raise_for_status()
        job_id = up.json()["id"]
        logger.info("LlamaParse job submitted: id=%s", job_id)

        # 2. Poll
        waited = 0
        while waited < self.max_wait:
            time.sleep(self.poll_interval)
            waited += self.poll_interval
            s = _requests.get(
                f"{self._API_BASE}/job/{job_id}",
                headers=self._auth, timeout=30,
            )
            s.raise_for_status()
            status = s.json().get("status", "")
            logger.debug("LlamaParse job=%s status=%s (%ds)", job_id, status, waited)
            if status == "SUCCESS":
                break
            if status in ("ERROR", "PARTIAL_SUCCESS"):
                raise RuntimeError(
                    f"LlamaParse processing failed (status={status}) for {file_path.name}"
                )
        else:
            raise TimeoutError(f"LlamaParse job {job_id} did not complete within {self.max_wait}s")

        # 3. Fetch result
        r = _requests.get(
            f"{self._API_BASE}/job/{job_id}/result/{self.result_type}",
            headers=self._auth, timeout=60,
        )
        r.raise_for_status()
        data = r.json()

        key   = "md" if self.result_type == "markdown" else "text"
        pages = [p.get(key, "") for p in data.get("pages", [])]
        if not pages:
            pages = [data.get("content", "")]

        metadata = {
            "job_id":      job_id,
            "result_type": self.result_type,
            "page_count":  len(pages),
        }
        return self._make_content(file_path, pages, metadata)


# ─────────────────────────────────────────────────────────────────────────────
#  Hybrid extractor  (local-first, cloud fallback)
# ─────────────────────────────────────────────────────────────────────────────

class HybridExtractor:
    """
    Local-first, cloud-fallback strategy.

    1. Attempt local extraction (PyMuPDF + Tesseract) — free, fast, private.
    2. If the result is below quality_threshold characters, switch to cloud OCR.
    3. Return ExtractedContent whose image-origin markers identify the actual
       engine that produced each region.
    """

    def __init__(
        self,
        local_extractor,
        cloud_extractor:   Optional[CloudOCRExtractor] = None,
        quality_threshold: int                         = 100,
    ):
        self.local_extractor   = local_extractor
        self.cloud_extractor   = cloud_extractor
        self.quality_threshold = quality_threshold

    def extract(self, file_path: Path) -> ExtractedContent:
        local_result: Optional[ExtractedContent] = None
        try:
            local_result = self.local_extractor.extract(file_path)
            if len(local_result.full_text.strip()) >= self.quality_threshold:
                logger.info("Local extraction sufficient for %s", file_path.name)
                return local_result
            logger.info(
                "Local extraction low quality (%d chars) for %s — trying cloud.",
                len(local_result.full_text.strip()), file_path.name,
            )
        except Exception as exc:
            logger.warning("Local extraction failed for %s: %s", file_path.name, exc)

        if self.cloud_extractor:
            try:
                result = self.cloud_extractor.extract(file_path)
                logger.info(
                    "Cloud OCR (%s) succeeded for %s",
                    self.cloud_extractor.engine_name, file_path.name,
                )
                return result
            except Exception as exc:
                logger.error("Cloud OCR failed for %s: %s", file_path.name, exc)
                raise

        return local_result or ExtractedContent(
            text="", images_text=[], metadata={"error": "all extractors failed"}
        )


# ─────────────────────────────────────────────────────────────────────────────
#  Factory  —  build from Config
# ─────────────────────────────────────────────────────────────────────────────

class CloudOCRFactory:
    """
    Instantiate the right CloudOCRExtractor from a Config object.

    Usage
    ─────
    extractor = CloudOCRFactory.build(config)
    if extractor:
        result = extractor.extract(Path("document.pdf"))

    Config attributes read
    ──────────────────────
    cloud_ocr_provider      : "aws" | "gcp" | "azure" | "mathpix" | "llama_parse" | None

    AWS:
        aws_region              (default "us-east-1")
        cloud_ocr_mode          "text" | "tables"  (default "text")
        aws_access_key_id       (optional; falls back to env/IAM)
        aws_secret_access_key   (optional)
        aws_session_token       (optional)
        aws_s3_bucket           (optional; required for files > 5 MB)

    GCP:
        cloud_ocr_mode          "document" | "image"  (default "document")
        gcp_credentials_json    path to service-account .json  (optional)
        gcp_language_hints      list of BCP-47 codes e.g. ["en","de"]  (optional)

    Azure:
        azure_endpoint          required
        azure_api_key           required
        azure_model_id          (default "prebuilt-read")
        azure_include_tables    bool (default True)

    Mathpix:
        mathpix_app_id          required
        mathpix_app_key         required
        mathpix_output_format   "markdown" | "mmd" | "text" | "latex"  (default "markdown")

    LlamaParse:
        llama_parse_api_key     required
        llama_parse_result_type "markdown" | "text"  (default "markdown")
        llama_parse_language    BCP-47 language code  (default "en")
    """

    _PROVIDERS = ("aws", "gcp", "azure", "mathpix", "llama_parse")

    @classmethod
    def build(cls, config: Any) -> Optional[CloudOCRExtractor]:
        """Return the configured extractor, or None if no provider is set."""
        provider = getattr(config, "cloud_ocr_provider", None)
        if not provider:
            return None

        provider = provider.lower().replace("-", "_")
        if provider not in cls._PROVIDERS:
            raise ValueError(
                f"Unknown cloud_ocr_provider={provider!r}. "
                f"Valid options: {', '.join(cls._PROVIDERS)}"
            )

        logger.info("Building cloud OCR extractor: provider=%s", provider)

        if provider == "aws":
            return AWSTextractExtractor(
                region_name           = getattr(config, "aws_region",             "us-east-1"),
                mode                  = getattr(config, "cloud_ocr_mode",         "text"),
                aws_access_key_id     = getattr(config, "aws_access_key_id",      None),
                aws_secret_access_key = getattr(config, "aws_secret_access_key",  None),
                aws_session_token     = getattr(config, "aws_session_token",      None),
                s3_bucket             = getattr(config, "aws_s3_bucket",          None),
            )

        if provider == "gcp":
            creds = getattr(config, "gcp_credentials_json", None)
            return GCPVisionExtractor(
                mode             = getattr(config, "cloud_ocr_mode",    "document"),
                credentials_json = Path(creds) if creds else None,
                language_hints   = getattr(config, "gcp_language_hints", None),
            )

        if provider == "azure":
            return AzureDocIntelligenceExtractor(
                endpoint       = getattr(config, "azure_endpoint",      None),
                api_key        = getattr(config, "azure_api_key",       None),
                model_id       = getattr(config, "azure_model_id",      "prebuilt-read"),
                include_tables = getattr(config, "azure_include_tables", True),
            )

        if provider == "mathpix":
            return MathpixExtractor(
                app_id        = getattr(config, "mathpix_app_id",        None),
                app_key       = getattr(config, "mathpix_app_key",       None),
                output_format = getattr(config, "mathpix_output_format", "markdown"),
            )

        if provider == "llama_parse":
            return LlamaParseExtractor(
                api_key     = getattr(config, "llama_parse_api_key",      None),
                result_type = getattr(config, "llama_parse_result_type",  "markdown"),
                language    = getattr(config, "llama_parse_language",     "en"),
            )

        return None
