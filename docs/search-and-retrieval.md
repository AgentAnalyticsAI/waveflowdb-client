# Search and Retrieval

| Method | Use it for |
|---|---|
| `index.query()` | Semantic search; optional hybrid keyword filtering and VQL |
| `index.keyword_search()` | Exact keyword search across the whole index |

## Semantic Search

```python
results = index.query("refund policy", top_k=5)
```

| Parameter | Default | Description |
|---|---|---|
| `query_text` | required | Natural-language query, or a raw VQL string |
| `top_k` | `10` | Maximum number of results |
| `threshold` | `0.2` | Minimum similarity score |
| `include_data` | `True` | `True` returns chunk text with each match; `False` returns document metadata only |
| `filter` | `False` | Enables hybrid keyword + vector filtering |
| `filters` | `None` | Keywords for the hybrid filter (used with `filter=True`) |
| `search_type` | `None` | `"flat"` or `"flat_filter"` (only with `filter=True`) |
| `documents`, `ids` | `None` | Search inline text instead of the index (both required) |

```python
index.query("refund policy", top_k=5, threshold=0.3)       # raise the score cutoff
index.query("refund policy", include_data=False)           # metadata only
```

## Hybrid Filtering

Narrow results with keywords. `filters` are combined with `or`.

```python
index.query("refund policy", filter=True, filters=["billing", "payments"])
```

This sends: `select top 5 where query is "refund policy" contains {billing or payments}`

**`search_type`** (only valid with `filter=True`; defaults to `"flat_filter"`):

| Value | Behaviour |
|---|---|
| `"flat_filter"` | Applies the filter first, then ranks only the matching documents. Fewer, more targeted results. |
| `"flat"` | Ranks the whole corpus. The filter only nudges scores and never excludes a document. |

```python
index.query("refund policy", filter=True, filters=["billing"], search_type="flat")
```

> The filter takes effect only when the query contains a `contains {...}` clause. Without it, the server silently runs plain semantic search, even with `filter=True`. Use `filters=[...]` and the SDK builds the clause for you.

## VQL

Write the full query yourself for `FROM` clauses, structured fields or multiple conditions.

```python
query = '''SELECT TOP 5
WHERE QUERY IS "refund policy complaints"
CONTAINS {userId:ts09ab1234}'''

index.query(query, filter=True)
```

- A query starting with `select` is sent as-is.
- Leave `filters` unset. If you set it, it is ignored and a warning is logged.
- `top_k` does not change the `TOP N` written in the VQL.

See [VQL Documentation](VQL_DOCUMENTATION.md) for syntax, structured-data filtering and tokenization.

## Keyword Search

Full-text, non-semantic search over the whole index. **Wrap the keyword in curly braces.**

```python
index.keyword_search("{refund}", top_k=5)
```

## Search Inline Text

Search text you supply instead of the stored index. Nothing is indexed.

```python
index.query(
    "refund policy",
    documents=["Refunds are accepted within 30 days.", "Shipping takes 5 days."],
    ids=["a.txt", "b.txt"],
)
```

## Tips

- Start with a plain semantic query, then add `filters` to narrow it.
- Use `flat_filter` for precision and `flat` when you don't want to risk excluding relevant documents.
- If results are empty, lower `threshold` or try `include_data=True`.
- Inspect the response before reading fields; its structure depends on `include_data`.