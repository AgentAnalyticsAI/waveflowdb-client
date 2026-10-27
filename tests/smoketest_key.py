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

def cleanup():
    """Remove test files and logs when the script exits."""
    for path in [VECTOR_LAKE_PATH, "./logs"]:
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
            print(f"[CLEANUP] Removed {path}")


atexit.register(cleanup)

def setup_test_files():
    """Create test documents before running smoke tests."""
    os.makedirs(VECTOR_LAKE_PATH, exist_ok=True)

    files = {
        "faq.txt": "Customers can request a refund within 30 days of purchase.",
        "policy.txt": "Refund requests must comply with the company's refund policy.",
    }

    for filename, content in files.items():
        file_path = os.path.join(VECTOR_LAKE_PATH, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

    print(f"[SETUP] Created {len(files)} test files in {VECTOR_LAKE_PATH}")

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
        print(json.dumps(response, indent=2, default=str)) #uncomment see the responses
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

def main():
    setup_test_files()
    client, index = init_client()

    tests = [
        ("0. Health check", lambda: client.health()),
        ("1. Upload documents", lambda: index.add(
            files=["faq.txt", "policy.txt"]
        )),
        ("2. Search documents", lambda: index.query(
            "refund policy", top_k=5
        )),
        ("3. Get documents", lambda: index.get()),
        ("4. Index statistics", lambda: index.describe_index_stats()),
        ("5. List namespaces", lambda: client.list_indexes(namespace=NAMESPACE)),
        ("6. Delete by ID", lambda: index.delete(ids=["faq_part1","policy_part1"])),
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
