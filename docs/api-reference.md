# API Reference

This page summarizes the main interfaces, document operations, configuration options, and error types available in the WaveflowDB Python client. Exact method signatures, parameters, and return types may vary by SDK version.

## High-Level API: `VectorLake` and `Index`

`VectorLake` provides a high-level interface for interacting with WaveflowDB. Use `Index` to work with documents within a namespace.

### Initialize the client

```python
from waveflowdb_client import VectorLake

client = VectorLake(
    api_key="YOUR_API_KEY",
    user_id="you@example.com",
)

index = client.Index("support_docs")
```

Replace the example credentials and namespace with your own values.

### Index methods

| Method | Purpose |
|---|---|
| `index.add(...)` | Add files or text to the index. |
| `index.query(...)` | Search indexed documents. |
| `index.update(...)` | Re-index changed documents, where supported. |
| `index.delete(...)` | Delete indexed content using supported identifiers. |
| `index.get()` | Retrieve document information, where supported. |
| `index.describe_index_stats()` | Retrieve index statistics, where supported. |

### Client methods

| Method | Purpose |
|---|---|
| `client.list_indexes()` | List available namespaces or indexes, where supported. |
| `client.health()` | Check server health, where supported. |

Method availability depends on the installed release. In particular, namespace-level operations and health checks may belong to the client rather than the `Index` object.

The SDK may also provide `index.upsert(...)` as an alias for `index.add(...)`. Confirm that the alias exists in your installed version before using it.

For usage examples, see [Document Management](document-management.md) and [Search and Retrieval](search-and-retrieval.md).

## Low-Level Client

For lower-level control over ingestion and retrieval, the SDK may expose `Config` and `VectorLakeClient`.

### Initialize the low-level client

```python
from waveflowdb_client import Config, VectorLakeClient

config = Config(
    api_key="YOUR_API_KEY",
    host="https://waveflow-analytics.com",
    vector_lake_path="./upload",
)

client = VectorLakeClient(config)
```

- `api_key`: Credentials used to authenticate requests.
- `host`: WaveflowDB service endpoint.
- `vector_lake_path`: Local directory used for supported file-based ingestion workflows.

Use the endpoint and configuration values appropriate for your deployment. Confirm the accepted constructor arguments for your SDK version.

## Document Operations

### `add_documents()`

Adds documents to the index.

Depending on the implementation, ingestion may support:

- **File mode:** Process source files using the configured directory.
- **Direct-data mode:** Supply filenames and file content through parameters such as `files_name` and `files_data`.
- **Batch processing:** Configure how documents are grouped for ingestion.
- **Metadata:** Associate metadata with source documents.
- **Processing options:** Configure supported sequential or streaming workflows.

Check the installed method signature for accepted parameters and input formats.

### `refresh_documents()`

Reprocesses existing documents when their source content changes.

Depending on the implementation, refreshing may remove stale chunks and generate new chunks from the updated content before uploading them.

Verify how the operation identifies existing documents, handles metadata, and recovers from partial failures.

### `insert_documents()`

Inserts documents at a supported position in the indexed corpus.

The method may support both file-based and direct-data ingestion. Confirm the insertion-position semantics, accepted inputs, and supported parameters before using it.

### `delete_documents()`

Deletes documents using the identifier type accepted by the low-level API.

Some implementations support filename-based deletion without reprocessing document content. Confirm whether deletion operates on source filenames, document IDs, or individual chunk IDs.

### `get_matching_docs()`

Retrieves matching document chunks using semantic search and, where supported, hybrid filtering.

Parameters may include:

| Parameter | Purpose |
|---|---|
| `query` | Search query text. |
| `user_id` | User associated with the request. |
| `vector_lake_description` | Description of the target vector lake. |
| `pattern` | Search pattern or filtering expression. |
| `hybrid_filter` | Enables supported hybrid filtering. |
| `search_type` | Selects the retrieval strategy. |
| `top_docs` | Controls the requested number of results. |
| `threshold` | Applies a supported retrieval threshold. |
| `with_data` | Controls whether document data is included. |
| Inline files | Optional file inputs, if supported. |

The accepted parameters, defaults, and interactions depend on the installed implementation. Consult [Search and Retrieval](search-and-retrieval.md) and [VQL Documentation](VQL_DOCUMENTATION.md) for query behavior.

## Additional Low-Level Methods

The client may expose additional operations for health checks, namespace inspection, and corpus retrieval.

| Method | Purpose |
|---|---|
| `health_check()` | Check the service's health. |
| `get_namespace_details()` | Retrieve namespace information. |
| `get_docs_information()` | Retrieve document information. |
| `full_corpus_search()` | Search across the supported corpus. |

Confirm each method's availability, required parameters, and return structure in the installed SDK.

## Configuration

Client configuration controls authentication, the service endpoint, local ingestion paths, and supported processing options.

See [Configuration](configuration.md) for configuration examples and environment-variable settings.

## Errors and Return Values

Return values and exception behavior depend on the method and SDK version. Inspect the method documentation and handle errors according to the actual public API.

### Exception types

The following table describes exception types that may be exposed by the SDK.

| Exception | Purpose |
|---|---|
| `ConfigError` | Invalid or incomplete configuration. |
| `ValidationError` | Invalid arguments or mismatched input lengths. |
| `InvalidSearchTypeError` | Unsupported search type. |
| `DocumentNotFoundError` | A requested document cannot be resolved. |
| `UnsupportedFileTypeError` | Unsupported document extension. |
| `FileProcessingError` | File access, encoding, or parsing failure. |
| `APIError` | An API request fails; may include HTTP status and response details. |
| `ThrottleError` | A rate-limit response, typically HTTP 429; may include retry information. |

Not every release necessarily exposes all these exception classes, and individual methods may handle errors differently.

### Error-handling guidance

- Validate input arguments before submitting requests.
- Handle authentication and authorization failures explicitly.
- Check HTTP status information when available.
- Apply retries only when appropriate for the error and operation.
- Avoid assuming that every method returns a dictionary or suppresses exceptions.
- Inspect the actual return structure before accessing response fields.

## Version Compatibility

For reliable integration:

- Use the public methods available in your installed SDK version.
- Check method signatures and accepted parameter names.
- Confirm return types and exception behavior.
- Test document ingestion, updates, deletion, and retrieval with a small dataset.
- Consult the relevant feature guide for detailed usage examples.