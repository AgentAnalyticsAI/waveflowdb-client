# VQL — Vector Query Language

Vector Query Language (VQL) lets you combine a natural-language search query with brace-based terms to narrow retrieval results. You can use it to search documents semantically while applying additional constraints to the content being retrieved.

## Basic Query

Use `SELECT TOP` to specify the maximum number of results, `WHERE QUERY IS` to describe what you are searching for, and `CONTAINS` to add targeted search terms.

```python
query = '''SELECT TOP 5
WHERE QUERY IS "patients with high sodium levels"
CONTAINS {sodium} {(clinical trial)} {PID 555}'''

results = index.query(query)
print(results)
```

- `SELECT TOP 5`: Requests up to five results.
- `WHERE QUERY IS`: Defines the natural-language search query.
- `CONTAINS`: Specifies additional terms or constraints to guide retrieval.

The exact parsing behavior and supported syntax may depend on your backend version.

## Brace Syntax

Use braces to specify terms and combine conditions.

| Syntax | Meaning |
|---|---|
| `{A B}` | Requires both terms to match (implicit AND). |
| `{A or B}` | Allows either term to match. |
| `{(A B) or C}` | Groups a phrase-like expression with an alternative term. |
| `{A} {B}` | Combines separate groups using implicit AND. |

Example:

```text
{A B}
{A or B}
{(A B) or C}
{A} {B}
```

Use these expressions to make searches more specific than a natural-language query alone. Confirm phrase matching and operator precedence for your backend version.

## Structured Data Filtering

When searching indexed JSON, NDJSON, or CSV data, you can use field-value expressions to target specific records.

### Filter by an exact field-value pair

```python
query = '''SELECT TOP 1
WHERE QUERY IS "event_type"
CONTAINS {transaction_id:c00d7eac-5b86-439e-956b-118583df6d67}'''

results = index.query(query)
```

### Combine multiple field filters

For example, to search for shipped orders from Delhi:

```python
query = '''SELECT TOP 10
WHERE QUERY IS "shipped orders from Delhi"
CONTAINS {status:shipped} {city:Delhi}'''

results = index.query(query)
```

Here, `status:shipped` and `city:Delhi` represent field-value constraints. Ensure the field names and values match the data available in your indexed documents.

## Search Modes

VQL retrieval can support different search strategies, depending on the backend configuration.

- **`flat`** — Combines semantic search results with hybrid filtering.
- **`flat_filter`** — Applies filtering as a constraint before semantic ranking.

The low-level API may expose these options through parameters such as `search_type` and `hybrid_filter`. Check the [API Reference](api-reference.md) for the supported parameters and their requirements in your installed version.

## Search Behavior and Limitations

Keep the following considerations in mind when constructing VQL queries:

- **Short tokens and numbers:** Tokens of two characters or fewer and numeric-only tokens may not be indexed. Prefer contextual expressions such as `{context_status_code:400}` over `{400}` when searching for numeric values.
- **Term matching:** Search terms may need to match the indexed text after normalization. Check whether stemming, synonyms, or other text transformations are supported.
- **Empty filter groups:** A group that becomes empty after normalization may not contribute a constraint.
- **No matching filter terms:** Depending on the backend's fallback behavior, retrieval may continue with semantic search when filter terms do not match.

These behaviors can affect search results. Test them against known documents before relying on them in production.

## Tips for Effective Queries

- Start with a clear natural-language query and add constraints incrementally.
- Use `SELECT TOP` to control the number of requested results.
- Use brace expressions to narrow the search to relevant terms or field values.
- Keep structured field names and values consistent across your indexed data.
- Avoid relying on very short tokens or numeric-only terms.
- Test queries against documents with known content to verify the expected results.