"""
configure API_KEY & USER_ID
$env:WAVEFLOWDB_API_KEY = "your-api-key-here"
$env:WAVEFLOWDB_USER_ID = "your-user-email-here"
$env:WAVEFLOWDB_HOST    = "https://waveflow-analytics.com"  

run:
    python -m tests.smoke_test
"""

import json
import os
import shutil
import atexit

from dotenv import load_dotenv
load_dotenv()

from waveflowdb_client import VectorLake

# Configuration (from environment / .env)
API_KEY   = os.getenv("WAVEFLOWDB_API_KEY", "your-api-key")
USER_ID   = os.getenv("WAVEFLOWDB_USER_ID", "your-email-address")
HOST      = os.getenv("WAVEFLOWDB_HOST", "https://waveflow-analytics.com")
NAMESPACE = os.getenv("NAMESPACE", "test_namespace")
UPLOAD_DIR = "./tests/upload"   # folder the SDK reads files from

DUMMY_FILES = {
    "smoke_doc.txt":    "MSME schemes offer collateral-free loans and subsidies to small businesses.",
    "smoke_doc2.txt":   "The credit guarantee scheme helps MSME units get loans without collateral.",
    "smoke_stream.txt": "Streaming mode test document about MSME subsidy schemes.",
    "smoke_insert.txt": "Insert test document about MSME registration through Udyam.",
}

# VQL queries
QUERY_VQL          = """SELECT TOP 5 WHERE QUERY IS "msme schemes" """
QUERY_VQL_CONTAINS = """SELECT TOP 5 WHERE QUERY IS "msme schemes" CONTAINS {loan or subsidy} """

def setup_test_files():
    """Create the dummy txt files the upload tests read."""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for filename, content in DUMMY_FILES.items():
        with open(os.path.join(UPLOAD_DIR, filename), "w", encoding="utf-8") as f:
            f.write(content)


def cleanup():
    shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
    shutil.rmtree("./logs", ignore_errors=True)


atexit.register(cleanup)

def init_client():
    """Initialize WaveflowDB client."""
    return VectorLake(api_key=API_KEY, user_id=USER_ID, host=HOST, vector_lake_path=UPLOAD_DIR)


def check_status(name, operation):
    """Run the operation, print the response, and check for HTTP 200."""
    try:
        response = operation()
        #print(json.dumps(response, indent=2, default=str)) #uncomment see the responses

        status_code = (
            response.get("status_code")
            if isinstance(response, dict)
            else getattr(response, "status_code", None)
        )

        if status_code is None and isinstance(response, dict):
            status_code = response.get("response", {}).get("status_code") or (
                response.get("batches", [{}])[0]
                .get("response", {})
                .get("status_code")
            )

        if status_code == 200:
            print(f"[PASS] {name}: {status_code}")
            return True

        print(f"[FAIL] {name}: status_code={status_code}")

    except Exception as exc:
        print(f"[FAIL] {name}: {exc}")

    return False

def delete_smoke_docs(index):
    """Delete every doc this test created, using the ids the server reports."""
    docs = index.get().get("reply", {}).get("docs", [])
    ids = [d for d in docs if d.startswith("smoke_")]
    if not ids:
        return {"status_code": 200, "message": "nothing to delete"}
    return index.delete(ids=ids)

def main():
    client = init_client()
    index = client.Index(NAMESPACE)
    setup_test_files()

    tests = [
        ("0. Delete all uploaded files", lambda: delete_smoke_docs(index)),
        ("1. [info] List indexes", lambda: client.list_indexes()),
        ("2. [info] Health check", lambda: client.health()),
        ("3. [info] List Documents in namespace", lambda: index.get()),
        ("4. [info] Stats of namespace", lambda: index.describe_index_stats()),

        # ── Plain semantic search ─────────────────────────────────────────
        ("5. [search] Semantic search", lambda: index.query("msme schemes", top_k=5)),
        ("6. [search] Semantic search with threshold", lambda: index.query(
            "msme schemes", top_k=3, threshold=0.3
        )),
        ("7. [search] Metadata only (include_data=False)", lambda: index.query(
            "msme schemes", top_k=5, include_data=False
        )),

        # ── Hybrid filter (SDK builds the CONTAINS clause for you) ────────
        ("8. [search] Hybrid filter (default flat_filter)", lambda: index.query(
            "msme schemes", top_k=5, filter=True, filters=["loan", "subsidy"]
        )),
        ("9. [search] Hybrid filter, search_type=flat", lambda: index.query(
            "msme schemes", top_k=5, filter=True, filters=["loan"], search_type="flat"
        )),
        ("10. [search] Hybrid filter, search_type=flat_filter", lambda: index.query(
            "msme schemes", top_k=5, filter=True, filters=["loan"], search_type="flat_filter"
        )),

        # ── Raw VQL (write the full query yourself, leave filters unset) ──
        ("11. [vql] Basic VQL", lambda: index.query(QUERY_VQL, top_k=5, filter=True)),
        ("12. [vql] VQL with CONTAINS (flat_filter)", lambda: index.query(
            QUERY_VQL_CONTAINS, top_k=5, filter=True, search_type="flat_filter"
        )),
        ("13. [vql] VQL with CONTAINS (flat)", lambda: index.query(
            QUERY_VQL_CONTAINS, top_k=5, filter=True, search_type="flat"
        )),
        ("14. [vql] VQL, metadata only", lambda: index.query(
            QUERY_VQL, top_k=5, filter=True, include_data=False
        )),

        # ── Dynamic search (inline text, nothing is indexed) ──────────────
        ("15. [search] Dynamic search over inline text", lambda: index.query(
            "msme schemes",
            documents=["MSME loan scheme offers a subsidy.", "Shipping takes 5 days."],
            ids=["inline_a.txt", "inline_b.txt"],
        )),

        # ── Keyword search (exact match, non-semantic) ────────────────────
        ("16. [search] Keyword search", lambda: index.keyword_search("{msme}", top_k=5)),

        # ── Raw client ────────────────────────────────────────────────────
        ("17. [raw] get_matching_docs", lambda: client.raw.get_matching_docs(
            query="msme schemes",
            user_id=USER_ID, vector_lake_description=NAMESPACE,
            top_docs=5, threshold=0.2, with_data=True,
        )),
        ("18. [raw] get_matching_docs, VQL + hybrid filter", lambda: client.raw.get_matching_docs(
            query=QUERY_VQL_CONTAINS,
            user_id=USER_ID, vector_lake_description=NAMESPACE,
            hybrid_filter=True, search_type="flat_filter",
        )),
        ("19. [raw] full_corpus_search", lambda: client.raw.full_corpus_search(
            user_id=USER_ID, vector_lake_description=NAMESPACE,
            keyword="{msme}", top_docs=5,
        )),
        # ── ADD: file upload ──────────────────────────────────────────────
        ("20. [add] Upload one file", lambda: index.add(files=["smoke_doc.txt"])),
        ("21. [add] Upload multiple files", lambda: index.add(
            files=["smoke_doc.txt", "smoke_doc2.txt"]
        )),
        ("22. [add] Upload all files in directory", lambda: index.add()),
        ("23. [add] Upload file with dict metadata", lambda: index.add(
            files=["smoke_doc.txt"],
            metadatas={"smoke_doc.txt": {"department": "msme", "year": 2026}},
        )),
        ("24. [add] Upload file, sequential mode", lambda: index.add(
            files=["smoke_doc.txt"], processing_mode="sequential", batch_delay=0.1,
        )),
        ("25. [add] Upload file, streaming mode", lambda: index.add(
            files=["smoke_stream.txt"], processing_mode="streaming",
            max_concurrent_batches=2, batch_delay=0.1,
        )),
        ("26. [add] Upload with intelligent_segmentation=False", lambda: index.add(
            files=["smoke_doc2.txt"], intelligent_segmentation=False,
        )),

        # ── ADD: direct text ──────────────────────────────────────────────
        ("27. [add] Direct text (single)", lambda: index.add(
            documents=["MSME direct text sentence for testing."], ids=["direct_a.txt"],
        )),
        ("28. [add] Direct text (multiple) with list metadata", lambda: index.add(
            documents=["Direct text one about MSME.", "Direct text two about loans."],
            ids=["direct_b.txt", "direct_c.txt"],
            metadatas=[{"author": "alice"}, {"author": "bob"}],
        )),
        ("29. [add] Direct text with dict metadata", lambda: index.add(
            documents=["Direct text with dict metadata."], ids=["direct_d.txt"],
            metadatas={"direct_d.txt": {"tag": "smoke"}},
        )),

        # ── UPSERT ────────────────────────────────────────────────────────
        ("30. [upsert] Upsert direct text (new id)", lambda: index.upsert(
            documents=["Upserted new MSME text."], ids=["upsert_a.txt"],
        )),
        ("31. [upsert] Upsert direct text (existing id)", lambda: index.upsert(
            documents=["Upserted again, replaces the previous text."], ids=["upsert_a.txt"],
            metadatas={"upsert_a.txt": {"rev": 2}},
        )),
        ("32. [upsert] Upsert file", lambda: index.upsert(files=["smoke_doc.txt"])),

        # ── UPDATE ────────────────────────────────────────────────────────
        ("33. [update] Update with file", lambda: index.update(files=["smoke_doc.txt"])),
        ("34. [update] Update text by id", lambda: index.update(
            documents=["Updated direct text."], ids=["direct_a.txt"],
        )),
        ("35. [update] Update file with dict metadata", lambda: index.update(
            files=["smoke_doc2.txt"],
            metadatas={"smoke_doc2.txt": {"revision": 2}},
        )),
        ("36. [update] Update file, streaming mode", lambda: index.update(
            files=["smoke_stream.txt"], processing_mode="streaming",
        )),

        # ── INSERT ────────────────────────────────────────────────────────
        ("37. [insert] Insert file", lambda: index.insert(files=["smoke_insert.txt"])),
        ("38. [insert] Insert direct text", lambda: index.insert(
            documents=["Inserted section text."], ids=["smoke_insert.txt"],
        )),
        ("39. [insert] Insert all files in directory", lambda: index.insert()),
        ("40. [insert] Insert file, sequential mode", lambda: index.insert(
            files=["smoke_insert.txt"], processing_mode="sequential",
        )),

        # ── Verify ingestion landed ───────────────────────────────────────
        ("41. [verify] Search finds uploaded content", lambda: index.query("MSME loans", top_k=5)),
        ("42. [verify] Documents listed", lambda: index.get()),

        # ── DELETE (keep these LAST) ──────────────────────────────────────
        ("43. [delete] Delete uploaded files by id", lambda: index.delete(
            ids=["smoke_doc_part1", "smoke_doc2_part1", "smoke_stream_part1", "smoke_insert_part1"]
        )),
        ("44. [delete] Delete direct-text docs", lambda: index.delete(
            ids=["direct_a","direct_d"]
        )),
        # search: additional Search and Retrieval tests
        ("45. [search] Default top_k (not passed)", lambda: index.query("refund policy")),
        ("46. [search] include_data=True (explicit)", lambda: index.query(
            "refund policy", top_k=5, include_data=True
        )),
        ("47. [search] Hybrid filter, single keyword", lambda: index.query(
            "refund policy", filter=True, filters=["billing"]
        )),

        # VQL variants
        ("48. [vql] Uppercase VQL, no FROM, no filter flag", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing}'''
        )),
        ("49. [vql] Structured field term {field:value}", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {department:support}''',
            filter=True,
        )),
        ("50. [vql] VQL + search_type=flat", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing or payments}''',
            filter=True, search_type="flat",
        )),
        ("51. [vql] VQL + search_type=flat_filter", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing or payments}''',
            filter=True, search_type="flat_filter",
        )),
        ("52. [vql] VQL with filters set (filters ignored, warning logged)", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing}''',
            filter=True, filters=["payments"],
        )),
        ("53. [vql] VQL, include_data=False", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing}''',
            filter=True, include_data=False,
        )),
        ("54. [vql] VQL with threshold", lambda: index.query(
            '''SELECT TOP 5 WHERE QUERY IS "refund policy" CONTAINS {billing}''',
            filter=True, threshold=0.3,
        )),

        # Keyword search 
        ("55. [keyword] Braced keyword", lambda: index.keyword_search("{refund}", top_k=5)),
        ("56. [keyword] Braced keyword, top_k=1", lambda: index.keyword_search("{refund}", top_k=1)),
        ("57. [keyword] Braced keyword, different term", lambda: index.keyword_search("{billing}", top_k=5)),

        ("58. Delete all uploaded files", lambda: delete_smoke_docs(index))
    ]
    passed = 0

    for name, operation in tests:
        if check_status(name, operation):
            passed += 1

    failed = len(tests) - passed
    print(f"\nSummary: {passed} passed, {failed} failed")

    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)