# Configuration

The SDK supports constructor-based configuration and environment-based configuration.

## Constructor configuration

```python
from waveflowdb_client import VectorLake

client = VectorLake(
    api_key="YOUR_API_KEY",
    user_id="you@example.com",
    host="https://waveflow-analytics.com",
    vector_lake_path="./upload",
)
```

## Environment variables

| Variable | Documented default | Purpose |
|---|---|---|
| `VECTOR_LAKE_API_KEY` | Required | API key |
| `USER_ID` | Required | User identifier |
| `NAMESPACE` | Required | Target namespace / lake name |
| `VECTOR_LAKE_HOST` | `https://waveflow-analytics.com` | Server base URL |
| `VECTOR_LAKE_PATH` | `upload` | Directory containing source files |
| `VECTOR_LAKE_LOG_DIR` | `logs` | Directory for log files |
| `VECTOR_LAKE_MAX_FILES_PER_BATCH` | `100` | Maximum chunk files per upload batch |
| `VECTOR_LAKE_MAX_BATCH_SIZE_MB` | `1` | Maximum total batch size in MB |
| `VECTOR_LAKE_TIMEOUT` | `240` | HTTP request timeout in seconds |
| `VECTOR_LAKE_MAX_RETRIES` | `2` | Retry attempts on transient errors |
| `VECTOR_LAKE_ENABLE_OCR` | `true` | Enable local Tesseract OCR |
| `VECTOR_LAKE_OCR_LANG` | `eng` | Tesseract language code |
| `VECTOR_LAKE_STREAMING_MINIBATCH_FILES` | `10` | Chunks per streaming minibatch |
| `VECTOR_LAKE_STREAMING_MINIBATCH_SIZE_MB` | `2` | MB per streaming minibatch |


## `.env` example

If your application uses `python-dotenv`, a local `.env` file can contain configuration values:

```dotenv
VECTOR_LAKE_API_KEY=replace_with_your_api_key
VECTOR_LAKE_HOST=https://waveflow-analytics.com
USER_ID=you@example.com
NAMESPACE=support_docs
VECTOR_LAKE_PATH=./upload
```

## Optional dependencies

Install only the processing capabilities you require. 
```bash
pip install "waveflowdb-client[documents]"
pip install "waveflowdb-client[cloud-ocr]"
```

See [Document Processing and OCR](document-processing.md) for provider-specific notes.

## Configuration checklist

- Confirm the API key and user identifier belong to the intended account.
- Confirm the host URL is correct for the environment.
- Confirm the namespace exists or can be created by the configured API.
- Confirm the local upload path exists and contains the files you intend to process.
- Keep credentials out of source control and logs.
