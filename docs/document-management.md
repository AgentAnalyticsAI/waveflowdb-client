# Document Management

## Add Files

Place your source documents in the directory configured by `vector_lake_path` and specify the filenames you want to index:

```python
index.add(files=["faq.pdf", "policy.docx"])
```

To index all supported files discovered in the configured directory:

```python
index.add()
```

File ingestion reads from the configured source directory.

## Add Text Directly

You can index text directly without creating a local document file:

```python
index.add(
    documents=[
        "Employees are entitled to 12 days of paid sick leave per year."
    ],
    ids=["hr_sick_leave_policy.txt"],
)
```

- `documents`: A list of text entries to index.
- `ids`: A unique identifier for each text entry.

The `documents` and `ids` lists must contain the same number of elements. Use stable, unique IDs to identify and manage your entries.

## Add Files with Metadata

You can associate metadata with individual files during ingestion using a dictionary keyed by the source filename:

```python
index.add(
    files=["faq.txt", "policy.txt"],
    metadatas={
        "faq.txt": {"department": "finance", "year": 2026},
        "policy.txt": {"author": "alice"},
    },
)
```

Metadata lets you associate additional information with documents, such as department, author, category, or year.

See [Metadata](metadata.md) for supported metadata formats and filtering options.

## Update Existing Documents

When a source document changes, re-index it so that searches can reflect the updated content.

```python
index.update(files=["policy.docx"])
```

Uploading the same file again will be skipped if it has already been processed and recognized by the ingestion system.

## Insert Documents

Document insertion can add content at a specified position in the indexed corpus.

```python
index.insert(files=["policy.docx"])
```

## Delete Documents

Documents are stored as chunks, each with its own ID. Use the chunk IDs to delete specific chunks.

```python
index.delete(ids=["faq_part1.pdf", "faq_part3.pdf"])
```

## Inspect Indexed Documents

```python
documents = index.get()
stats = index.describe_index_stats()
health = client.health()
```

## Operational Guidelines

For batch ingestion and resumable workflows, see [Advanced Ingestion](advanced-ingestion.md).