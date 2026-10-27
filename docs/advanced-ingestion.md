# Advanced Ingestion

WaveflowDB ingestion can support batch processing, checkpoint-based recovery, failure auditing, and document reprocessing.

## Processing Modes

### Sequential Processing

Processes document extraction and chunk uploads in sequence. Depending on the implementation, it may support:

- **Checkpoint recovery:** Resume interrupted operations.
- **Change detection:** Detect unchanged files using MD5 checksums or sidecar files.
- **Extraction optimization:** Skip extraction for unchanged documents.
- **Document refresh:** Reprocess changed files and remove stale chunks when supported.
- **Manual batch selection:** Specify batch boundaries when supported and resume is not used.

Use sequential processing when execution order and recovery are important.

### Streaming Processing

Overlaps document extraction with batch uploads, allowing subsequent files to be prepared while the current minibatch uploads. It is useful for large collections and OCR-intensive workloads.

Depending on the implementation, streaming may support smaller minibatches, streaming-specific checkpoints, and recovery from interrupted operations. Manual batch boundaries may not be available.

Check supported configuration options before combining processing modes.

## Checkpoints and Resume

Checkpoints track ingestion progress to reduce repeated work after interruptions. A checkpoint may be stored at:

```text
{VECTOR_LAKE_PATH}/chunks/checkpoint_{USER_ID}_{NAMESPACE}_{operation}.json
```

The identity may include the user, namespace, and operation, such as `add`, `refresh`, or `insert`.

Depending on the workflow:

- Completed batches or files may be recorded.
- Interrupted operations may resume from saved progress.
- Successful runs may automatically remove checkpoints.
- Launcher options such as `RESUME` and `START_FROM_BATCH` may control recovery.

Cleanup behavior depends on the operation and whether failures occurred.

**Important:** Do not modify or delete checkpoints during active ingestion. Before restarting from scratch, follow your installed version's recovery procedure to avoid duplicate or inconsistent content.

## Batch Configuration

The following are example settings, not guaranteed defaults. Verify supported names and values for your release.

| Setting | Example default | Purpose |
|---|---:|---|
| `VECTOR_LAKE_MAX_FILES_PER_BATCH` | `100` | Maximum files per batch |
| `VECTOR_LAKE_MAX_BATCH_SIZE_MB` | `1` | Configured batch size in MB |
| `VECTOR_LAKE_STREAMING_MINIBATCH_FILES` | `10` | Files per streaming minibatch |
| `VECTOR_LAKE_STREAMING_MINIBATCH_SIZE_MB` | `2` | Configured streaming minibatch size in MB |
| `VECTOR_LAKE_TIMEOUT` | `240` seconds | Request timeout |
| `VECTOR_LAKE_MAX_RETRIES` | `2` | Maximum configured retries |

Actual batch sizes and limits depend on implementation.

**Tuning guidelines:**
- Start conservatively and increase values gradually.
- Monitor latency, memory usage, and failure rates.
- Account for document size and OCR overhead.
- Verify timeout and retry behavior for long-running uploads.
- Test sequential and streaming configurations separately.

## Failure Auditing and Logs

Example log paths and purposes are listed below. Actual paths and formats depend on configuration.

| File | Purpose |
|---|---|
| `logs/run_operations.log` | Run-level events and execution details |
| `logs/failed_files.jsonl` | Failed files or batches |
| `logs/api_errors.csv` | API errors and batch details |
| `logs/performance.csv` | Request latency, payload size, and HTTP status |
| `logs/skipped_files.csv` | Skipped files |
| `{VECTOR_LAKE_PATH}/chunks/run_<id>.json` | Per-run batch outcomes and processing results |

### Troubleshooting

1. Review run-level logs for the first error.
2. Identify affected documents in the failure audit trail.
3. Inspect API errors and HTTP status codes.
4. Check whether skipped files were unsupported, unchanged, or excluded.
5. Review run reports and checkpoint state before retrying.
6. Retry a small subset of failed files before processing a large batch.

Preserve relevant logs and reports when investigating recurring failures.

## Filename Sanitization and Chunk Naming

Documents may be split into chunks named using a pattern such as:

```text
{stem}_part{num}
```

Here, `{stem}` is the filename without its extension, and `{num}` is the chunk number.

Some implementations sanitize filenames before processing. Sanitized names may appear in chunks, metadata, sidecar files, and checkpoints.

**Do not assume source filenames remain unchanged.** Before production ingestion:

- Test using a copy of the source directory.
- Verify whether sanitization modifies source files or only internal identifiers.
- Check for collisions between sanitized filenames.
- Keep backups when original filenames must be preserved.
- Confirm the naming convention used by your SDK version.

## Launcher Operations

The `run.py` launcher may provide the following operations:

| Operation | Purpose |
|---|---|
| `add` | Add source documents |
| `refresh` | Reprocess existing documents |
| `update` | May act as an alias for `refresh` |
| `insert` | Insert documents at a supported corpus position |
| `delete` | Remove indexed content using supported identifiers |
| `query` | Retrieve relevant documents |
| `health` | Check server health |
| `info` | Inspect indexed documents or index information |

These are launcher operations and are not necessarily methods of the high-level `Index` class. For example, `refresh` or `health` may be available through the launcher but not through `Index`.

See the [API Reference](api-reference.md) for operation availability across the launcher, high-level SDK, and low-level client.

## Best Practices

- Test ingestion and recovery with a small document collection first.
- Back up original source documents.
- Use checkpoints and resume options supported by your installed version.
- Review failure logs and API errors before retrying.
- Never modify checkpoints during active ingestion.
- Tune batch sizes to document size, processing time, and backend limits.
- Verify whether refresh and update replace existing chunks or can create duplicates.
- Confirm launcher command behavior before using it in automated workflows.