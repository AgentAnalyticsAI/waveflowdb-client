from __future__ import annotations

"""
models.py — Public data-transfer objects.

This module contains the core DTOs used across the VectorLake SDK.  All
objects are plain :func:`dataclasses <dataclasses.dataclass>` or
:class:`~enum.Enum` subclasses so they carry no dependencies beyond the
standard library.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── API response models ───────────────────────────────────────────────────────

@dataclass
class DocumentInfo:
    """Metadata and content snapshot for a single indexed document."""
    filename: str
    content: str
    size: int
    modified_time: Optional[datetime] = None


@dataclass
class MatchingDocsResponse:
    """
    Response envelope returned by the ``top_matching_docs`` endpoint.

    Attributes
    ----------
    status:
        Server-reported status string (e.g. ``"success"``).
    query:
        The original search query echoed back by the server.
    response:
        Serialised match results (JSON string or structured payload).
    with_data:
        Whether raw chunk text was included in the response.
    session_id:
        Session correlation token.
    user_id:
        Caller identity.
    timestamp:
        Server-side response timestamp.
    """
    status: str
    query: str
    response: str
    with_data: bool
    session_id: str
    user_id: str
    timestamp: datetime


@dataclass
class HealthResponse:
    """
    Response envelope returned by the ``health`` endpoint.

    Attributes
    ----------
    status:
        ``"ok"`` when the server is healthy.
    message:
        Human-readable status description.
    timestamp:
        Server-side response timestamp.
    details:
        Optional extra diagnostic information.
    """
    status: str
    message: str
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None


@dataclass
class BatchResult:
    """
    Outcome record for a single upload batch.

    Attributes
    ----------
    batch_number:
        1-based index of this batch within the run.
    response:
        Raw server response for the batch (dict or ``None`` on error).
    files_processed:
        List of chunk basenames included in the batch.
    success:
        ``True`` if the server accepted the batch without error.
    error_message:
        Human-readable error description when ``success`` is ``False``.
    processing_time:
        Wall-clock seconds from batch start to HTTP response.
    """
    batch_number: int
    response: Any
    files_processed: List[str]
    success: bool
    error_message: Optional[str] = None
    processing_time: Optional[float] = None


# ── Chunk name codec ──────────────────────────────────────────────────────────

_CHUNK_RE = re.compile(
    r"^(?P<stem>.+?)_part(?P<part>\d+)\.txt$"
)


@dataclass
class VersionedChunkName:
    """
    Encodes and decodes the chunk filename convention used by the SDK.

    All source files are split into one or more chunk files on disk.  This
    class provides a single source of truth for the naming scheme so that
    chunk files can be reliably round-tripped between their on-disk name and
    the structured fields that identify which source file and part they belong
    to.

    Pattern:  ``{stem}_part{part}.txt``

    Attributes
    ----------
    stem:
        Base name of the source file without extension
        (e.g. ``"report"`` for ``report.pdf``).
    part:
        1-based chunk index.
    ext:
        Always ``.txt`` — chunks are written as plain text regardless of the
        source format.
    version:
        Kept for backwards-compatibility; not encoded in filenames.

    Examples
    --------
    >>> VersionedChunkName.build("report.pdf", part=3).filename
    'report_part3.txt'

    >>> VersionedChunkName.parse("report_part3.txt")
    VersionedChunkName(stem='report', part=3, ext='.txt', version=1)
    """
    stem: str
    part: int
    ext: str = ".txt"
    version: int = 1  # retained for backwards-compat; not used in filenames

    @property
    def filename(self) -> str:
        """Return the on-disk chunk filename for this instance."""
        return f"{self.stem}_part{self.part}.txt"

    @classmethod
    def build(cls, source_name: str, part: int, version: int = 1) -> "VersionedChunkName":
        """
        Construct a :class:`VersionedChunkName` from a source filename.

        All chunks are written as ``.txt`` regardless of the source format.
        The *version* parameter is accepted for backwards-compatibility but
        has no effect on the generated filename.

        Parameters
        ----------
        source_name:
            Original filename (e.g. ``"annual_report.pdf"``).
        part:
            1-based chunk index.
        version:
            Ignored; retained for API compatibility.
        """
        p = Path(source_name)
        return cls(stem=p.stem, part=part, ext=".txt", version=version)

    @classmethod
    def parse(cls, filename: str) -> Optional["VersionedChunkName"]:
        """
        Decode a chunk filename back into a :class:`VersionedChunkName`.

        Returns ``None`` if *filename* does not match the SDK convention
        (i.e. it is a source file, sidecar, or unrelated file).

        Parameters
        ----------
        filename:
            Basename or full path of the chunk file to parse.
        """
        m = _CHUNK_RE.match(Path(filename).name)
        if not m:
            return None
        return cls(
            stem=m.group("stem"),
            part=int(m.group("part")),
            ext=".txt",
        )

    @staticmethod
    def is_versioned(filename: str) -> bool:
        """
        Return ``True`` if *filename* matches the SDK chunk naming convention.

        Useful for filtering out chunk files when listing a directory that
        contains both source files and their derived chunks.

        Parameters
        ----------
        filename:
            Basename or full path to test.
        """
        return _CHUNK_RE.match(Path(filename).name) is not None