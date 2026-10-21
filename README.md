<div align="center">

## WaveflowDB Client

A Python client library for [WaveflowDB](https://db.agentanalytics.ai/)

[![PyPI](https://badge.fury.io/py/waveflowdb-client.svg?cache=1)](https://pypi.org/project/waveflowdb-client/)
[![API Docs](https://img.shields.io/badge/Docs-OpenAPI%203.0-success)](https://www.agentanalytics.ai/docs/waveflow-db)
[![License](https://img.shields.io/badge/license-MIT-blue)]()

</div>

## How to Use WaveflowDB

1. Create a namespace in the [WaveflowDB UI](https://db.agentanalytics.ai/) and configure your API credentials.
2. Upload your documents using the client. Chunking and embedding generation are handled internally.
3. Run a search query to retrieve relevant data chunks from your uploaded documents.

## Installation
```bash
pip install waveflowdb-client

# Optional: Install additional document processing support
pip install waveflowdb-client[documents]
```

## Quick start
```python
from waveflowdb_client import VectorLake

# Initialize the client with your API key and email
client = VectorLake(
    api_key="your-api-key",  # Generate your API key from the UI
    user_id="your-email-address",
    host="https://waveflow-analytics.com",
    vector_lake_path="./upload"  # Local folder containing your files
)

# Connect to an existing namespace created in the UI
index = client.Index("test_namespace")

# Upload files from the configured folder
index.add(files=["faq.txt", "policy.txt"])

# Search for relevant information
results = index.query("refund policy", top_k=5)
print(results)
```
Generate your API key and create a namespace in the [WaveflowDB UI](https://db.agentanalytics.ai/) by following the [Setup Guide]([docs/SETUP_GUIDE.md](https://www.agentanalytics.ai/docs/waveflow-db))

## Search and Retrieval
### Basic Search 
Search your uploaded documents using a natural-language query. WaveflowDB returns relevant data chunks based on your query.
```python
results = index.query("refund policy", top_k=5)
print(results)
```

### Advanced Search: VQL - Vector Query Language
Use VQL when you need more control over your search, such as applying metadata filters or constructing more complex queries.

```python
QUERY = """SELECT TOP 5
WHERE QUERY IS "refund policy complaints"
CONTAINS {userId:ts09ab1234}"""

results = index.query(QUERY)
print(results)
```
For syntax details and practical examples, refer to the [VQL Documentation](docs/VQL_DOCUMENTATION.md).

## Upload Documents

Place your documents in the folder configured by vector_lake_path when initializing the client.

### Upload Specific Documents
```python
# Upload only the selected files
index.add(files=["faq.pdf", "policy.docx"])
```

### Upload All Documents
```python
# Upload all supported files from vector_lake_path
index.add()
```
Chunking and embedding generation are handled internally.

## Upload Documents with OCR
OCR can help extract text from documents that require optical character recognition.

Install the optional dependencies for your chosen processing method:

```bash
# Local document processing and OCR support
pip install "waveflowdb-client[documents]"

# Cloud OCR provider integrations
pip install "waveflowdb-client[cloud-ocr]"
```
Enable OCR when initializing the client:
```python
client = VectorLake(
    api_key="your-api-key",
    user_id="your-email-address",
    host="https://waveflow-analytics.com",
    vector_lake_path="./upload",
    enable_ocr=True, 
)

index = client.Index("test_namespace")

# Upload documents using the configured OCR processing
index.add(files=["faq.pdf", "policy.docx"])
```
Cloud OCR may also require provider-specific credentials and configuration. See the setup documentation for details.

## Delete Documents
Documents are stored as chunks, each with its own ID. Use the chunk IDs to delete specific chunks.
```python
# Delete the first and third chunks of faq.pdf
index.delete(ids=["faq_part1.pdf", "faq_part3.pdf"])
```
Chunk ID format: {file_name}_part{chunk_number}.{file_extension}

For example, hr_policy_part4.pdf identifies the fourth chunk of hr_policy.pdf.

To delete an entire document, provide the IDs of all its chunks.


## Index Management and Health

Use these methods to inspect documents, view namespace details, list available namespaces, and check server health.

```python
# List documents in the current namespace
index.get()

# View statistics and metadata for the current namespace
index.describe_index_stats()

# List all namespaces available to your account
index.list_indexes()

# Check server health
index.health()
```

## Documentation

For detailed documentation, API references, and usage examples, visit the [Official WaveflowDB Documentation](https://www.agentanalytics.ai/docs/waveflow-db).

## Support & Feedback

For platform access, visit the [WaveflowDB UI](https://db.agentanalytics.ai/).

If you encounter any issues or have suggestions, feel free to [open an issue](https://github.com/agentanalytics-ai/waveflowdb-client/issues) on GitHub.

## License

Copyright © DIBR Tech Private Limited. Licensed under the MIT License.