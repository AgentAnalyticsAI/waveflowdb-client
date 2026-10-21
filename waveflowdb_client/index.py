"""
index.py — Idiomatic, Pinecone/Chroma-style façade over VectorLakeClient.

Most vector DB SDKs (Pinecone, Chroma, Qdrant, Weaviate, ...) converge on the
same two-object shape:

    client = SomeDB(api_key=...)
    index  = client.Index("my-index")     # or .collection(...)
    index.add(...) / .upsert(...)
    index.query(...)
    index.delete(...)

This module gives VectorLake that same shape. It is a thin wrapper — every
method here is a direct pass-through to :class:`~vectorlake.client.VectorLakeClient`
(exposed as ``VectorLake().raw``). Nothing here talks to the network itself,
so it inherits all retry, batching, chunking, and OCR behaviour of the
underlying client for free.

Nothing about the lower-level API changes or is removed — this is purely an
additive convenience layer. Reach for :class:`VectorLakeClient` directly if
you need checkpoint/resume (`start_from_batch`/`end_batch`), streaming
ingestion, or cloud-OCR configuration beyond what's exposed here.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union
from .utils import clean_filename
from .client import VectorLakeClient, FilesMetaArg
from .config import Config

logger = logging.getLogger(__name__)


class Index:
    """
    A handle to a single namespace ("vector lake"), bound to a caller identity.

    Don't construct this directly — get one from :meth:`VectorLake.Index`.
    Every method operates on documents within this one namespace, the way
    a Pinecone ``Index`` or Chroma ``Collection`` scopes operations to one
    collection.
    """

    def __init__(self, raw_client: VectorLakeClient, name: str, user_id: str):
        self._client = raw_client
        self.name = name
        self.user_id = user_id

    def __repr__(self) -> str:
        return f"Index(name={self.name!r}, user_id={self.user_id!r})"

    # ── Write ────────────────────────────────────────────────────────────

    def add(
        self,
        documents: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        metadatas: FilesMetaArg = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Add documents to the index.

        Two ways to call it:

        In-memory text (Chroma-style)::

            index.add(documents=["full text of doc one", "..."], ids=["a.txt", "b.txt"])

        Local files (reads from ``config.vector_lake_path``, chunks, uploads)::

            index.add(files=["report.pdf", "notes.docx"])
            index.add()   # ingest everything under vector_lake_path

        Custom metadata (``metadatas``)
        --------------------------------
        Accepts two shapes, matching :class:`~vectorlake.client.VectorLakeClient`'s
        ``files_meta`` (see ``FilesMetaArg``):

        - **dict**, keyed by original filename — the recommended shape when
          using *files* (path mode), since it's resolved per source file
          regardless of chunk-splitting/batch order::

              index.add(
                  files=["report.pdf", "notes.txt"],
                  metadatas={
                      "report.pdf": {"department": "finance", "year": 2026},
                      "notes.txt":  {"author": "alice"},
                  },
              )

        - **list**, positionally aligned with *documents*/*ids* — the
          original in-memory-mode behaviour, unchanged::

              index.add(
                  documents=["full text one", "full text two"],
                  ids=["a.txt", "b.txt"],
                  metadatas=[{"author": "alice"}, {"author": "bob"}],
              )

          A list is matched to *documents* by position and must be the same
          length as *documents* — it is not supported together with *files*
          (path mode requires the dict shape, since there's no fixed
          per-document ordering to align a list against).

        Any extra keyword (``processing_mode="streaming"``,
        ``intelligent_segmentation=False``, ...) is forwarded to
        :meth:`VectorLakeClient.add_documents`.
        """
        if documents is not None:
            ids = ids or [f"doc_{i}.txt" for i in range(len(documents))]
            if len(ids) != len(documents):
                raise ValueError("ids and documents must be the same length")
            ids = [clean_filename(i) for i in ids]

            if isinstance(metadatas, list) and len(metadatas) != len(documents):
                raise ValueError("metadatas and documents must be the same length")

            return self._client.add_documents(
                user_id=self.user_id,
                vector_lake_description=self.name,
                files_name=ids,
                files_data=documents,
                files_meta=metadatas,
                **kwargs,
            )

        if isinstance(metadatas, list):
            raise ValueError(
                "metadatas must be a dict keyed by filename when using files= "
                "(path mode) — a positional list can't be reliably aligned to "
                "files once the SDK splits/interleaves them into chunks. "
                'Use e.g. metadatas={"report.pdf": {"author": "alice"}} instead.'
            )

        return self._client.add_documents(
            user_id=self.user_id,
            vector_lake_description=self.name,
            files=files,
            files_meta=metadatas,
            **kwargs,
        )

    # add_docs is idempotent on chunk name, so "upsert" is a safe alias for
    # callers coming from Pinecone/Qdrant who expect that name.
    upsert = add

    def update(
        self,
        documents: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        metadatas: FilesMetaArg = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Re-index documents that already exist in this index (explicit refresh
        semantics — stale chunks are purged and regenerated server-side).
        Same calling convention as :meth:`add`, including the ``metadatas``
        dict-vs-list rules documented there.
        """
        if documents is not None:
            ids = ids or []
            if len(ids) != len(documents):
                raise ValueError("ids and documents must be the same length")
            ids = [clean_filename(i) for i in ids]

            if isinstance(metadatas, list) and len(metadatas) != len(documents):
                raise ValueError("metadatas and documents must be the same length")

            return self._client.refresh_documents(
                user_id=self.user_id,
                vector_lake_description=self.name,
                files_name=ids,
                files_data=documents,
                files_meta=metadatas,
                **kwargs,
            )

        if isinstance(metadatas, list):
            raise ValueError(
                "metadatas must be a dict keyed by filename when using files= "
                "(path mode) — a positional list can't be reliably aligned to "
                "files once the SDK splits/interleaves them into chunks. "
                'Use e.g. metadatas={"report.pdf": {"author": "alice"}} instead.'
            )

        return self._client.refresh_documents(
            user_id=self.user_id,
            vector_lake_description=self.name,
            files=files,
            files_meta=metadatas,
            **kwargs,
        )

    def delete(self, ids: List[str]) -> Dict[str, Any]:
        """Remove documents from the index by id (filename)."""
        return self._client.delete_documents(
            user_id=self.user_id,
            vector_lake_description=self.name,
            files_name=ids,
        )

    # ── Read ─────────────────────────────────────────────────────────────

    def query(
        self,
        query_text: str,
        top_k: int = 10,
        filter: bool = False,
        filters: Optional[List[str]] = None,
        search_type: Optional[str] = None,
        threshold: float = 0.2,
        include_data: bool = True,
        documents: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Search for the *top_k* documents matching *query_text*.

            results = index.query("refund policy", top_k=5)

        Two backend endpoints exist and this method picks between them for
        you via *include_data* (default ``True``):

        - ``top_matching_docs`` (``include_data=False``) — returns matched
          document metadata only.
        - ``top_matching_docs_with_data`` (``include_data=True``, the
          default here) — also returns the underlying chunk text alongside
          each match.

        Pass *documents* + *ids* to search inline text instead of the
        persisted index (maps to the server's "dynamic" search pattern).

        Hybrid filtering
        -----------------
        ``filter=True`` turns on hybrid keyword+vector filtering — but the
        filter only actually takes effect if *query_text* is written in the
        server's VQL syntax, specifically a ``contains {...}`` clause:

            select top 5 where query is "refund policy" contains {billing or payments}

        If ``contains {...}`` is missing, the server silently treats the
        request as **plain semantic search** — no error, it just ignores the
        filter — even with ``filter=True`` set. So writing that VQL string
        by hand is error-prone; pass *filters* instead and it's built for
        you:

            index.query("refund policy", filter=True, filters=["billing", "payments"])

        which sends exactly:

            select top 5 where query is "refund policy" contains {billing or payments}

        ``search_type`` — flat vs. flat_filter
        ----------------------------------------
        Only meaningful when ``filter=True``; ignored/invalid otherwise.

        - ``"flat"`` — searches the **entire** corpus semantically; the
          hybrid filter is only a scoring signal (nudges ranking) and never
          removes a document from consideration.
        - ``"flat_filter"`` — **narrows the candidate set** to documents that
          pass the hybrid filter *before* semantic ranking runs. Fewer,
          more targeted results.

        If ``filter=True`` and *search_type* is left as ``None``, this
        defaults to ``"flat_filter"`` — narrowing is the more common intent
        when someone opts into filtering at all. Pass
        ``search_type="flat"`` explicitly to keep the full corpus in play
        while still using the filter as a ranking signal.

        Raw VQL — full control (FROM clauses, multi-term filters, etc.)
        -------------------------------------------------------------------
        *filters* only builds a single ``contains {a or b}`` clause. For
        anything more expressive — a ``from`` clause, multiple conditions,
        VQL your server supports that this wrapper doesn't model — write the
        full VQL string yourself and pass it as *query_text*, leaving
        *filters* unset (``None`` or ``[]``):

            index.query(
                'select top 10 from support_docs where query is "refund policy" contains {billing or payments}',
                filter=True,
            )

        If *query_text* already looks like VQL (starts with ``select``),
        this method treats it as already-complete and passes it through
        as-is — *filters* is ignored for that call even if you also set it,
        to avoid nesting one VQL string inside another (which the server
        rejects with ``400: Only AND/OR allowed outside {} in CONTAINS``).
        A warning is logged if this happens so the ignored *filters* isn't
        a silent surprise.
        """
        q = query_text
        looks_like_vql = query_text.strip().lower().startswith("select")

        if looks_like_vql and filters:
            logger.warning(
                "query_text already looks like raw VQL (starts with 'select') "
                "— ignoring filters=%r to avoid nesting one VQL string inside "
                "another. Pass filters=None when writing VQL by hand, or drop "
                "the hand-written 'select ...' wrapper and pass plain query "
                "text instead.",
                filters,
            )
        elif filter and filters:
            filter_expr = " or ".join(filters)
            q = f'select top {top_k} where query is "{query_text}" contains {{{filter_expr}}}'

        resolved_search_type = search_type
        if filter and resolved_search_type is None:
            resolved_search_type = "flat_filter"

        return self._client.get_matching_docs(
            query=q,
            user_id=self.user_id,
            vector_lake_description=self.name,
            top_docs=top_k,
            hybrid_filter=filter,
            search_type=resolved_search_type,
            threshold=threshold,
            with_data=include_data,
            files_name=ids,
            files_data=documents,
            **kwargs,
        )

    def keyword_search(self, keyword: str, top_k: int = 10) -> Dict[str, Any]:
        """Full-text (non-semantic) keyword search across the whole index."""
        return self._client.full_corpus_search(
            user_id=self.user_id,
            vector_lake_description=self.name,
            keyword=keyword,
            top_docs=top_k,
        )

    def get(self, keyword: Optional[str] = None, threshold: int = 70) -> Dict[str, Any]:
        """List documents currently stored in this index, optionally filtered."""
        return self._client.get_docs_information(
            user_id=self.user_id,
            vector_lake_description=self.name,
            keyword=keyword,
            threshold=threshold,
        )

    def describe_index_stats(self) -> Dict[str, Any]:
        """Namespace-level metadata for this index (Pinecone-style name)."""
        return self._client.get_namespace_details(
            user_id=self.user_id,
            vector_lake_description=self.name,
        )

    def list_indexes(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """List every namespace visible to *user_id*."""
        resolved_user = user_id or self.user_id
        if not resolved_user:
            raise ValueError("user_id is required to list indexes.")
        return self._client.get_namespace_details(user_id=resolved_user)

    def health(self) -> Dict[str, Any]:
        """Ping the server. Returns ``{"status": "ok", ...}`` when healthy."""
        return self._client.health_check(
            user_id=self.user_id,
            vector_lake_description=self.name,
        )


class VectorLake:
    """
    Top-level connection object — the entry point for the idiomatic API.

    Example
    -------
    from vectorlake import VectorLake

    vl = VectorLake(api_key="...", user_id="demo@acme.com")
    index = vl.Index("support_docs")

    index.add(files=["faq.pdf", "policy.docx"])
    results = index.query("refund policy", top_k=5)
    index.delete(ids=["faq.pdf"])

    Anything accepted by :class:`~vectorlake.config.Config` (host, OCR
    settings, batching, ...) can be passed straight through as a kwarg here,
    or you can build a ``Config`` yourself and pass it as *config*.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        user_id: Optional[str] = None,
        config: Optional[Config] = None,
        **kwargs: Any,
    ):
        self._client = VectorLakeClient(config=config, api_key=api_key, **kwargs)
        self.user_id = user_id

    def Index(self, name: str, user_id: Optional[str] = None) -> Index:
        """
        Get a handle to a namespace. Namespaces are created implicitly the
        first time you ``add()``/``upsert()`` documents into them — there's
        no separate create-index call.
        """
        resolved_user = user_id or self.user_id
        if not resolved_user:
            raise ValueError(
                "user_id is required — pass it to VectorLake(...) or Index(...)."
            )
        return Index(self._client, name, resolved_user)

    # lower-case alias for folks coming from Qdrant/Weaviate-style SDKs
    index = Index

    rjt: cr start
    # def list_indexes(self, user_id: Optional[str] = None) -> Dict[str, Any]:
    #     """List every namespace visible to *user_id*."""
    #     resolved_user = user_id or self.user_id
    #     if not resolved_user:
    #         raise ValueError("user_id is required to list indexes.")
    #     return self._client.get_namespace_details(user_id=resolved_user)

    # def health(self) -> Dict[str, Any]:
    #     """Ping the server. Returns ``{"status": "ok", ...}`` when healthy."""
    #     return self._client.health_check(
    #         user_id=self.user_id or "sdk-health-check",
    #         vector_lake_description="__health__",
    #     )
    rjt: cr end

    @property
    def raw(self) -> VectorLakeClient:
        """
        Escape hatch to the full lower-level :class:`VectorLakeClient` —
        use this for streaming ingestion, checkpoint/resume, or anything
        else not exposed on :class:`Index`.
        """
        return self._client