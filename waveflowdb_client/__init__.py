"""
waveflowdb_client — VectorLake Python SDK v1.5.0
"""

__version__ = "1.0.6"

from .client import VectorLakeClient
from .config import Config
from .index import VectorLake, Index
from .exceptions import (
    VectorLakeError,
    ConfigError,
    ValidationError,
    InvalidSearchTypeError,
    FileProcessingError,
    UnsupportedFileTypeError,
    APIError,
    ThrottleError,
    DocumentNotFoundError,
)
from .models import (
    DocumentInfo,
    MatchingDocsResponse,
    HealthResponse,
    BatchResult,
    VersionedChunkName,
)

# ── Local image-tracking helpers (require pymupdf + pytesseract) ──────────────
try:
    from .extractors import (
        strip_image_markers,
        list_image_regions,
        ExtractorFactory,
        PDFExtractor,
        DOCXExtractor,
        PPTXExtractor,
        ImageFileExtractor,
        XLSXExtractor,
        flatten_workbook,
        ExtractedContent,
    )
    _IMAGE_SUPPORT = True
except ImportError:
    _IMAGE_SUPPORT = False

# ── Cloud OCR engines + factory (each engine's SDK is optional) ───────────────
try:
    from .cloud_extractors import (
        CloudOCRExtractor,
        AWSTextractExtractor,
        GCPVisionExtractor,
        AzureDocIntelligenceExtractor,
        MathpixExtractor,
        LlamaParseExtractor,
        HybridExtractor,
        CloudOCRFactory,
    )
    _CLOUD_SUPPORT = True
except ImportError:
    _CLOUD_SUPPORT = False


__all__ = [
    # Core
    "VectorLakeClient",
    "Config",
    # Idiomatic façade (Pinecone/Chroma-style)
    "VectorLake",
    "Index",
    # Exceptions
    "VectorLakeError",
    "ConfigError",
    "ValidationError",
    "InvalidSearchTypeError",
    "FileProcessingError",
    "UnsupportedFileTypeError",
    "APIError",
    "ThrottleError",
    "DocumentNotFoundError",
    # Models
    "DocumentInfo",
    "MatchingDocsResponse",
    "HealthResponse",
    "BatchResult",
    "VersionedChunkName",
    # Local image tracking (conditionally available)
    "strip_image_markers",
    "list_image_regions",
    "ExtractorFactory",
    "PDFExtractor",
    "DOCXExtractor",
    "PPTXExtractor",
    "ImageFileExtractor",
    "XLSXExtractor",
    "flatten_workbook",
    "ExtractedContent",
    # Cloud OCR (conditionally available)
    "CloudOCRExtractor",
    "AWSTextractExtractor",
    "GCPVisionExtractor",
    "AzureDocIntelligenceExtractor",
    "MathpixExtractor",
    "LlamaParseExtractor",
    "HybridExtractor",
    "CloudOCRFactory",
]