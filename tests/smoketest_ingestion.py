"""
configure API_KEY & USER_ID
$env:WAVEFLOWDB_API_KEY = "your-api-key-here"
$env:WAVEFLOWDB_USER_ID = "your-user-email-here"

run:
    python -m tests.smoke_test
"""

import json
import os
from waveflowdb_client import VectorLake
import atexit
import shutil

# Configuration
API_KEY = os.getenv("WAVEFLOWDB_API_KEY", "your-api-key")
USER_ID = os.getenv("WAVEFLOWDB_USER_ID", "your-email-address")
HOST = os.getenv("WAVEFLOWDB_HOST", "https://waveflow-analytics.com")
VECTOR_LAKE_PATH = "./tests/upload"
NAMESPACE = os.getenv("NAMESPACE", "test_namespace")

DOC = "smoke_doc.txt"      # used for steps 1-6
NEW_DOC = "smoke_new.txt"  # used for step 7


def cleanup():
    """Remove test files and logs when the script exits."""
    for path in [VECTOR_LAKE_PATH, "./logs"]:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f"[CLEANUP] Removed {path}")


atexit.register(cleanup)


def write_file(filename, content):
    """Create or overwrite a test document."""
    os.makedirs(VECTOR_LAKE_PATH, exist_ok=True)
    with open(os.path.join(VECTOR_LAKE_PATH, filename), "w", encoding="utf-8") as f:
        f.write(content)


def with_file(filename, content, operation):
    """Write the file with the given content, then run the operation."""
    write_file(filename, content)
    return operation()


def init_client():
    """Initialize WaveflowDB client and index."""
    client = VectorLake(
        api_key=API_KEY,
        user_id=USER_ID,
        host=HOST,
        vector_lake_path=VECTOR_LAKE_PATH,
    )
    return client, client.Index(NAMESPACE)


def check_status(name, operation):
    """Check whether the operation returns HTTP 200."""
    try:
        response = operation()
        print(json.dumps(response, indent=2, default=str))  # comment out to hide responses
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
    os.makedirs(VECTOR_LAKE_PATH, exist_ok=True)
    client, index = init_client()

    V1 = "MSME schemes offer collateral-free loans to small businesses."
    V2 = "MSME schemes offer collateral-free loans and a 15 percent capital subsidy."
    V3 = V2 + " Udyam registration is mandatory to apply."

    # Each step is followed automatically by "Get documents".
    steps = [
        ("Upload a document", lambda: with_file(
            DOC, V1, lambda: index.add(files=[DOC]))),
        ("Upload same document again (unchanged)", lambda: index.add(files=[DOC])),
        ("Upload same document again, content updated", lambda: with_file(
            DOC, V2, lambda: index.add(files=[DOC]))),
        ("Update document, same file name, same content", lambda: index.update(files=[DOC])),
        ("Update document, same file name, content modified", lambda: with_file(
            DOC, V3, lambda: index.update(files=[DOC]))),
        ("Insert into same file name", lambda: with_file(
            DOC, V3 + " Applications are processed online.",
            lambda: index.insert(files=[DOC]))),
        ("Insert a new file name", lambda: with_file(
            NEW_DOC, "The credit guarantee scheme covers MSME loans without collateral.",
            lambda: index.insert(files=[NEW_DOC]))),
    ]

    tests = [("0. Get documents (before start)", lambda: index.get())]
    for i, (label, operation) in enumerate(steps, start=1):
        tests.append((f"{i}. {label}", operation))
        tests.append((f"{i}a. Get documents", lambda: index.get()))

    n = len(steps) + 1
    tests += [
        (f"{n}. Delete all uploaded files", lambda: delete_smoke_docs(index)),
        (f"{n}a. Get documents (after delete)", lambda: index.get()),
    ]

    passed = 0

    for name, operation in tests:
        print(f"\n=== {name} ===")
        if check_status(name, operation):
            passed += 1

    failed = len(tests) - passed
    print(f"\nSummary: {passed} passed, {failed} failed")

    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)