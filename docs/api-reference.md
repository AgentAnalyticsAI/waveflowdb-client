# API Reference

This page provides an overview of the main classes, methods, configuration options, and exceptions available in the WaveflowDB Python client.

## VectorLake

`VectorLake` is the main client for connecting to WaveflowDB. Use `Index` to access and manage documents within a namespace.

### Initialize the Client

```python
from waveflowdb_client import VectorLake

client = VectorLake(
    api_key="YOUR_API_KEY",
    user_id="you@example.com",
)

index = client.Index("support_docs")
```

Replace the example credentials and namespace with your own values.

### Index Methods

| Method | Description |
|---|---|
| `index.add(...)` | Add documents or text to an index. |
| `index.query(...)` | Search indexed content. |
| `index.update(...)` | Update existing indexed content. |
| `index.insert(...)` | Insert new documents. |
| `index.delete(...)` | Delete indexed content using supported identifiers. |
| `index.get(...)` | Retrieve document information. |
| `index.describe_index_stats()` | Retrieve index statistics. |

### Client Methods

| Method | Description |
|---|---|
| `client.list_indexes()` | List available indexes or namespaces. |
| `client.health()` | Check the service health. |

For examples, see [Document Management](document-management.md) and [Search and Retrieval](search-and-retrieval.md).

## Configuration

The client supports configuration for authentication, service endpoints, document ingestion paths, and processing options.

See [Configuration](configuration.md) for setup instructions and environment-variable options.

## Errors and Return Values

Return values and exception behavior vary by method. Refer to the relevant method documentation for expected responses and possible errors.

### Exception Types

| Exception | Description |
|---|---|
| `ConfigError` | Raised when the configuration is invalid or incomplete. |
| `ValidationError` | Raised when arguments or input values are invalid. |
| `InvalidSearchTypeError` | Raised when an unsupported search type is specified. |
| `DocumentNotFoundError` | Raised when the requested document cannot be found. |
| `UnsupportedFileTypeError` | Raised when a file type is not supported. |
| `FileProcessingError` | Raised when a file cannot be accessed, decoded, or processed. |
| `APIError` | Raised when an API request fails. May include the HTTP status and response details. |
| `ThrottleError` | Raised when a rate limit is exceeded. May include retry information. |

### Error Handling

- Validate input arguments before making requests.
- Handle authentication and authorization errors appropriately.
- Check HTTP status codes and response details when available.
- Retry requests only when appropriate for the error and operation.
- Follow each method's documented return structure instead of assuming a specific format.
- Handle exceptions explicitly rather than assuming errors are returned as dictionaries.