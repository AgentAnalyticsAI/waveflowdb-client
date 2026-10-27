# Document Management

## Add Files

Place source documents in the directory configured by `vector_lake_path` and specify the filenames to index:

```python
index.add(files=["faq.pdf", "policy.docx"])
```

To index all supported files in the configured directory:

```python
index.add()
```

**Note:** Existing filenames are skipped, even if their content has changed. Use `update()` to refresh existing documents.

## Add Text Directly

Index text without creating a local file:

```python
index.add(
    documents=["Employees are entitled to 12 days of paid sick leave per year."],
    ids=["hr_sick_leave_policy.txt"],
)
```

- `documents`: Text entries to index.
- `ids`: Unique identifiers for the entries.

Both lists must contain the same number of elements.

## Update Documents

Use `update()` to refresh an existing document with its latest content:

```python
index.update(files=["policy.docx"])
```

## Insert Documents

Use `insert()` to add content while retaining existing indexed chunks:

```python
index.insert(files=["policy.docx"])
```

Inserted chunks receive new IDs. Use `index.get()` to retrieve the actual IDs and any ID mappings returned by the operation.

## Delete Documents

Use `index.get()` to retrieve indexed chunk IDs. Chunk IDs follow the format `{file_name}_part{chunk_number}`, without the file extension.

```python
# List indexed documents
index.get()

# Delete specific chunks
index.delete(ids=["faq_part1", "faq_part2"])
```

To delete an entire document, provide the IDs of all its chunks.

## Inspect Indexed Documents

```python
documents = index.get()
stats = index.describe_index_stats()
health = client.health()
```

## Operational Guidelines

For batch ingestion and resumable workflows, see [Advanced Ingestion](advanced-ingestion.md).