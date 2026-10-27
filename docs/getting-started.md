# Getting Started

This guide walks through the first steps for using the WaveflowDB Python client.

## 1. Install the client

```bash
python -m pip install waveflowdb-client
```

Install optional dependencies only when your workload needs them. See [Configuration](configuration.md) and [Document Processing and OCR](document-processing.md).

## 2. Prepare WaveflowDB access

Before running the examples, make sure you have:

- Access to the [WaveflowDB UI](https://db.agentanalytics.ai/).
- A valid API key.
- The email address or user identifier associated with your account.
- A namespace to use for your documents. Depending on your deployment, namespaces may need to be created in the UI first.

Keep API credentials private. Do not commit real credentials to source control.

## 3. Create a local upload folder

Create an `upload/` directory and place supported files in it. The client uses `vector_lake_path` to locate files when you call `index.add(files=[...])`.

Example layout:

```text
your-project/
├── app.py
└── upload/
    ├── faq.txt
    └── policy.txt
```

## 4. Initialize the client and add documents

```python
from waveflowdb_client import VectorLake

client = VectorLake(
    api_key="YOUR_API_KEY",
    user_id="you@example.com",
    host="https://waveflow-analytics.com",
    vector_lake_path="./upload",
)

index = client.Index("support_docs")
index.add(files=["faq.txt", "policy.txt"])
```

Use the host URL configured for your WaveflowDB deployment. The example host above is the one used in the supplied SDK README; confirm it with your administrator if your environment differs.

## 5. Search

```python
results = index.query("What is the refund policy?", top_k=5)
print(results)
```

The result format depends on the SDK version and backend response. Inspect the returned value in your environment before relying on particular fields.

## Next steps

- [Configuration](configuration.md)
- [Document Management](document-management.md)
- [Search and Retrieval](search-and-retrieval.md)
- [Troubleshooting](troubleshooting.md)
