# Advanced Ingestion

WaveflowDB ingestion workflows can include batch processing, checkpoint-based recovery, failure auditing, and document reprocessing. Available features and configuration options depend on the installed SDK version and whether you use the high-level `Index` interface, low-level API, or `run.py` launcher.

## Processing Modes

### Sequential Processing

Sequential processing extracts and chunks source documents before uploading batches one at a time.

Depending on the implementation, this mode can support:

- **Checkpoint-based recovery:** Resume an interrupted ingestion operation from a saved progress point.
- **Change detection:** Use MD5 checksums or sidecar files to identify unchanged source documents.
- **Extraction optimization:** Skip extraction for documents that have not changed.
- **Document refresh:** Reprocess changed files and remove stale chunks when supported.
- **Manual batch selection:** Specify batch boundaries when checkpoint-based resume is not being used, if supported by the launcher.

Sequential processing can be useful when predictable execution order and recovery from failures are important.

### Streaming Processing

Streaming processing overlaps document extraction with batch uploads. While one minibatch is being uploaded, subsequent files can continue through extraction and preparation.

This approach can be useful for large document collections and workloads involving expensive OCR operations.

Depending on the implementation, streaming mode may:

- Upload smaller minibatches while extraction continues.
- Track completed files through streaming-specific checkpoints.
- Support recovery from interrupted operations.

Manual batch boundaries may not be supported in streaming mode. Check the available configuration options before combining processing modes with other ingestion settings.

## Checkpoints and Resume

Checkpoints record ingestion progress so that an interrupted operation can resume without unnecessarily repeating completed work.

A checkpoint may be stored under a path similar to:

```text
{VECTOR_LAKE_PATH}/chunks/checkpoint_{USER_ID}_{NAMESPACE}_{operation}.json
```

The checkpoint identity may include the user, namespace, and operation, such as `add`, `refresh`, or `insert`.

### Resume behavior

Depending on the configured workflow:

- Completed batches or files may be tracked in the checkpoint.
- An interrupted run may resume from its saved progress.
- Successful completion may trigger automatic checkpoint cleanup.
- Launcher options such as `RESUME` or `START_FROM_BATCH` may control recovery behavior.

Checkpoint cleanup conditions can depend on the operation and whether any failures occurred.

**Important:** Do not modify or delete checkpoint files while an ingestion operation is running. Before restarting an operation from scratch, follow the recovery procedure for your installed version to avoid duplicate or inconsistent indexed content.

## Batch Configuration

Batch settings control how many files or how much data is processed together. The following settings and values are configuration examples; confirm the names and defaults supported by your release before applying them.

| Setting | Example default | Purpose |
|---|---:|---|
| `VECTOR_LAKE_MAX_FILES_PER_BATCH` | `100` | Maximum number of files per batch. |
| `VECTOR_LAKE_MAX_BATCH_SIZE_MB` | `1` | Maximum configured batch size in MB. |
| `VECTOR_LAKE_STREAMING_MINIBATCH_FILES` | `10` | Number of files in a streaming minibatch. |
| `VECTOR_LAKE_STREAMING_MINIBATCH_SIZE_MB` | `2` | Configured streaming minibatch size in MB. |
| `VECTOR_LAKE_TIMEOUT` | `240` seconds | Request timeout. |
| `VECTOR_LAKE_MAX_RETRIES` | `2` | Maximum configured retry count. |

Actual batch sizes and processing limits may depend on how the implementation interprets these settings.

When tuning batch configuration:

- Start with conservative values and increase them gradually.
- Monitor request latency, memory usage, and failure rates.
- Account for document size and OCR processing overhead.
- Verify how timeouts and retries affect long-running uploads.
- Test sequential and streaming configurations separately.

## Failure Auditing and Logs

Ingestion logs help diagnose failed uploads, skipped files, API errors, and performance bottlenecks.

The following are example log paths and their intended purposes. Actual paths and formats depend on your configuration.

| File | Purpose |
|---|---|
| `logs/run_operations.log` | Run-level events and execution details. |
| `logs/failed_files.jsonl` | Records of failed files or batches. |
| `logs/api_errors.csv` | API errors and associated batch information. |
| `logs/performance.csv` | Request latency, payload size, and HTTP status. |
| `logs/skipped_files.csv` | Files skipped during ingestion. |
| `{VECTOR_LAKE_PATH}/chunks/run_<id>.json` | Per-run batch outcomes and processing results. |

### Troubleshooting failed ingestion

1. Review the run-level logs for the first reported error.
2. Check the failed-file audit trail for affected documents.
3. Inspect API error details and HTTP status codes.
4. Review skipped files to determine whether they were unsupported, unchanged, or excluded for another reason.
5. Check the per-run report and checkpoint state before retrying.
6. Reprocess a small subset of failed files before retrying a large batch.

Preserve relevant logs and reports when investigating recurring failures.

## Filename Sanitization and Chunk Naming

During ingestion, a source document may be split into smaller text chunks. A generated chunk filename may follow a pattern such as:

```text
{stem}_part{num}.txt
```

Here, `{stem}` represents the document filename without its extension, and `{num}` represents a chunk number.

Some ingestion implementations may sanitize filenames before processing. If so, the sanitized name may be used in generated chunks, metadata, sidecar files, and checkpoint entries.

**Do not assume that source files remain unchanged.** If your ingestion workflow modifies filenames in place, it can affect downstream processes that depend on the original names.

Before processing production data:

- Test the workflow using a copy of the source directory.
- Check whether filename sanitization modifies source files or only internal identifiers.
- Look for potential collisions between sanitized filenames.
- Keep backups when original filenames must be preserved.
- Confirm the actual chunk naming convention used by your installed SDK.

## Launcher Operations

The `run.py` launcher may provide a command interface for common document-management and retrieval operations.

| Operation | Purpose |
|---|---|
| `add` | Add source documents to the index. |
| `refresh` | Reprocess existing documents. |
| `update` | May act as an alias for `refresh` in the launcher. |
| `insert` | Insert documents at a supported position in the corpus. |
| `delete` | Remove indexed content using the identifiers supported by the operation. |
| `query` | Retrieve relevant documents. |
| `health` | Check server health. |
| `info` | Inspect indexed documents or available index information. |

These are launcher operations, not necessarily methods on the high-level `Index` class. For example, a launcher may support `refresh` or `health` even when the corresponding method is unavailable on `Index`.

Consult the [API Reference](api-reference.md) to identify which operations are available through the launcher, high-level SDK, and low-level client.

## Best Practices

- Test ingestion and recovery workflows with a small document collection first.
- Keep original source documents backed up.
- Use checkpoints and resume options only as supported by your installed version.
- Monitor failed-file logs and API errors before retrying operations.
- Avoid modifying checkpoints during active ingestion.
- Tune batch sizes based on document size, processing time, and backend limits.
- Verify whether refresh and update operations replace existing chunks or can create duplicates.
- Confirm the behavior of launcher commands before incorporating them into automated workflows.