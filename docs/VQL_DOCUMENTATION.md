
## VQL — Vector Query Language

Use braces `{…}` to pin your search to documents or passages that contain specific terms.

```python
QUERY = """select top 5
where query is "patients with high sodium levels"
contains {sodium} {(clinical trial)} {PID 555}"""
```

### Syntax

```
{A B}             implicit AND: both A and B must match
{A or B}          either A or B
{(A B) or C}      phrase "A B", or term C
{A} {B}           groups combine with implicit AND
```

### Structured data filtering (JSON, NDJSON, CSV)

Because the tokeniser splits on `:`, `,`, `|`, `=`, you can filter by exact field:value pairs:

```python
# Log file: find a specific transaction
QUERY = """select top 1 where query is "event_type"
contains {transaction_id:c00d7eac-5b86-439e-956b-118583df6d67}"""

# CSV: filter by column values
QUERY = """select top 10 where query is "shipped orders from Delhi"
contains {status:shipped} {city:Delhi}"""
```

### Important rules

- Short tokens (≤ 2 chars) and pure numbers are not indexed — use `{context_status_code:400}` not `{400}`
- Token must appear exactly as written in the source
- If an entire brace group normalises to empty, it contributes no constraint (not zero results)
- If no filter hits, the system falls back to full semantic search automatically

---