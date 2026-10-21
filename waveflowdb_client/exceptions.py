"""
exceptions.py — VectorLake SDK exception hierarchy.

"""


class VectorLakeError(Exception):
    """Base class for all SDK errors."""

    def to_response(self) -> dict:
        return {
            "success": False,
            "error": self.__class__.__name__,
            "message": str(self),
        }


# ── Configuration ─────────────────────────────────────────────────────────────

class ConfigError(VectorLakeError):
    """Raised when the SDK cannot be initialised due to bad config."""


# ── Validation ────────────────────────────────────────────────────────────────

class ValidationError(VectorLakeError):
    """Raised when caller-supplied arguments fail pre-flight checks."""


class InvalidSearchTypeError(ValidationError):
    """
    Raised when search_type is not one of the accepted values.

    Valid values
    ────────────
    "flat"        — semantic search across ALL documents; hybrid_filter is
                    still applied as a scoring signal but does not restrict scope.
    "flat_filter" — semantic search ONLY over documents that pass the
                    hybrid_filter.  Narrows candidates before ranking.

    hybrid_filter (bool) is ALWAYS required alongside search_type.
    """
    VALID = ("flat", "flat_filter")

    def __init__(self, value: str):
        super().__init__(
            f"Invalid search_type={value!r}. "
            f"Must be one of: {', '.join(repr(v) for v in self.VALID)}"
        )
        self.value = value


# ── File handling ─────────────────────────────────────────────────────────────

class FileProcessingError(VectorLakeError):
    """Raised for any file I/O, encoding, or parsing failure."""


class UnsupportedFileTypeError(FileProcessingError):
    """Raised when a file extension is not in the allowed list."""

    def __init__(self, filename: str, allowed: list[str]):
        ext = filename.rsplit(".", 1)[-1] if "." in filename else "(none)"
        super().__init__(
            f"File '{filename}' has unsupported extension '.{ext}'. "
            f"Allowed: {', '.join(f'.{e}' for e in allowed)}"
        )
        self.filename = filename
        self.allowed = allowed


# ── Network ───────────────────────────────────────────────────────────────────

class APIError(VectorLakeError):
    """Raised when the server returns an HTTP error or network failure occurs."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_text: str | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text

    def to_response(self) -> dict:
        return {
            "success": False,
            "error": "APIError",
            "message": str(self),
            "status_code": self.status_code,
            "response_text": self.response_text,
        }


class ThrottleError(APIError):
    """HTTP 429 — server is rate-limiting the client."""

    def __init__(self, retry_after: int | None = None):
        super().__init__("Request throttled (HTTP 429)", status_code=429)
        self.retry_after = retry_after


# ── Document mutation ─────────────────────────────────────────────────────────

class DocumentNotFoundError(ValidationError):
    """
    Raised when a filename supplied to delete_documents or insert_documents
    cannot be resolved to a known document in the index.
    """

    def __init__(self, filename: str):
        super().__init__(
            f"Document '{filename}' was not found in the index. "
            "Verify the filename and try again."
        )
        self.filename = filename