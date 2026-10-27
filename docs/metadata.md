# Metadata

WaveflowDB supports metadata associated with documents. Metadata provides additional information about indexed content and can be used to organize, identify, and filter documents where supported by the backend.

## Custom Metadata for Files

When uploading files, provide a dictionary keyed by each source filename to associate metadata with individual documents.

```python
index.add(
    files=["report.pdf", "notes.txt"],
    metadatas={
        "report.pdf": {"department": "finance", "year": 2026},
        "notes.txt": {"author": "alice"},
    },
)
```

Each filename in `metadatas` should correspond to a file included in the `files` argument.

This format associates metadata with the appropriate source document, including when a document is divided into multiple chunks during ingestion.

## Metadata for Direct Text Uploads

When adding text directly, provide a list of metadata dictionaries aligned with the corresponding `documents` and `ids` entries.

```python
index.add(
    documents=["Full text one", "Full text two"],
    ids=["a.txt", "b.txt"],
    metadatas=[
        {"author": "alice"},
        {"author": "bob"},
    ],
)
```

Each metadata dictionary corresponds to the text entry at the same position in the lists.

Ensure that `documents`, `ids`, and `metadatas` contain the same number of entries when using this format.

**Important:** File-based uploads and direct-text uploads use different metadata formats:

- `files=` uses a dictionary keyed by source filename.
- `documents=` uses a list of metadata dictionaries aligned with the text entries.

Do not pass a list of metadata dictionaries with `files=` unless your installed SDK explicitly supports that format.

## Automatically Generated Metadata

During document ingestion, WaveflowDB may generate metadata fields to identify and describe the source document.

Common fields include:

- `filename` — The source document's filename.
- `extension` — The source file's extension, such as `.pdf` or `.txt`.
- `timestamp` — A timestamp associated with the document or ingestion operation.

The source extension can help preserve information about the original file type when its contents are converted into text chunks.

The exact fields, timestamp format, and behavior when custom metadata provides an existing value depend on the ingestion implementation and SDK version.

## Use Metadata with VQL

Where supported by the backend, metadata and structured field-value expressions can help narrow retrieval results.

For example, the following query searches for shipped orders from Delhi:

```python
query = '''SELECT TOP 10
WHERE QUERY IS "shipped orders from Delhi"
CONTAINS {status:shipped} {city:Delhi}'''

results = index.query(query)
print(results)
```

Here, `status:shipped` and `city:Delhi` represent field-value constraints.

The fields must exist in the indexed content or metadata, and the backend must support filtering on them. Metadata availability alone does not guarantee that a field is searchable or filterable.

For query syntax, tokenization rules, and additional examples, see [VQL Documentation](VQL_DOCUMENTATION.md).

## Best Practices

- Use consistent metadata field names and value types across documents.
- Prefer filename-keyed dictionaries for file-based ingestion.
- Align metadata dictionaries with their corresponding text entries when using direct-text ingestion.
- Use meaningful metadata values for filtering, organization, and auditing.
- Avoid assuming that custom metadata replaces automatically generated fields.
- Verify which metadata fields are preserved during document updates or re-indexing.
- Test metadata filtering against known documents before using it in production.