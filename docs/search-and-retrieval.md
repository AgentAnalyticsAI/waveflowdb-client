# Search and Retrieval

The `Index` interface provides methods to search documents within a namespace. You can perform semantic searches using natural-language queries or use Vector Query Language (VQL) to apply more specific search constraints.

## Basic Semantic Search

Use `index.query()` to retrieve documents relevant to a natural-language query.

```python
results = index.query("refund policy", top_k=5)
print(results)
```

- `query`: The natural-language text describing what you want to find.
- `top_k`: The maximum number of results requested.

The returned results may contain document content, identifiers, metadata, scores, or other fields, depending on the SDK and backend configuration. Inspect the response structure before accessing individual fields.

## VQL (Vector Query Language)

VQL combines natural-language search with brace-based terms and optional structured filtering to narrow retrieval results.

```python
query = '''SELECT TOP 5
WHERE QUERY IS "refund policy complaints"
CONTAINS {userId:ts09ab1234}'''

results = index.query(query)
print(results)
```

In this example, the query searches for refund policy complaints and includes a constraint on the `userId` field.

Use VQL when you need more control over retrieval than a plain semantic query provides.

For syntax, examples, structured-data filtering, and search behavior, see [VQL Documentation](VQL_DOCUMENTATION.md).

## Search Modes and Hybrid Filtering

Depending on the backend configuration, search operations may support different strategies for combining semantic retrieval with filtering.

- **`flat`** — Runs semantic search and hybrid filtering independently, then combines their results. This approach is intended to improve recall by considering results from both retrieval paths.
- **`flat_filter`** — Applies hybrid filtering as a constraint before semantic ranking, limiting ranking to eligible candidates.

The low-level API may expose these options through `search_type` and `hybrid_filter`. When using these parameters, check the API signature and confirm whether hybrid filtering must be enabled explicitly.

Search-mode availability and behavior may vary between the high-level `Index` interface and the low-level client.

## Low-Level Search

For more advanced retrieval workflows, the low-level API may expose `get_matching_docs()` with parameters such as:

| Parameter | Purpose |
|---|---|
| `query` | Search query text. |
| `user_id` | User identifier associated with the request. |
| `vector_lake_description` | Description of the vector lake or indexed content. |
| `pattern` | Search pattern or filtering expression. |
| `hybrid_filter` | Controls hybrid filtering when supported. |
| `search_type` | Selects the retrieval strategy when supported. |
| `top_docs` | Controls the requested number of documents. |
| `threshold` | Sets a retrieval threshold when supported. |
| `with_data` | Controls whether document data is included in the response. |
| Inline files | Optional file inputs, if supported by the method. |


## Search Tips

- Start with a clear natural-language query for general semantic retrieval.
- Use VQL when specific terms or structured attributes should narrow the results.
- Use meaningful field names and values when filtering structured documents.
- Compare broad and targeted queries against representative documents.
- Test different search modes when supported to determine which works best for your use case.
- Review [VQL Documentation](VQL_DOCUMENTATION.md) for tokenization, normalization, and short-token considerations.