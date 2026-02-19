"""
End-to-end test script for the Insurance RAG Pipeline.

Usage:
    python scripts/test_pipeline.py

Prerequisites:
    - API running at http://localhost:8000
    - .env configured with OpenAI + Pinecone keys
    - Sample PDFs in the same directory or specify path

This script:
    1. Creates a test tenant
    2. Creates a test policyholder record
    3. Uploads a sample policy PDF
    4. Queries the policy with test questions
    5. Verifies citations are returned
    6. Tests policyholder verification flow
"""

import requests
import json
import time
import sys
import os

BASE_URL = "http://localhost:8000"

# ── Helpers ────────────────────────────────────────────────────────────────

def pretty(data):
    print(json.dumps(data, indent=2, default=str))

def check_health():
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        if r.status_code == 200:
            print("✓ API is healthy")
            return True
    except requests.ConnectionError:
        pass
    print("✗ API is not running at", BASE_URL)
    print("  Start it with: docker-compose up -d")
    return False

def step(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


# ── Direct DB Setup (bypasses auth for testing) ───────────────────────────

def setup_test_data():
    """
    Insert test tenant + policyholder directly via a setup endpoint.
    In production, this would go through proper admin flows.
    """
    step("Setting up test data via DB")

    # We'll create a simple setup script that hits the DB directly
    # For now, we'll work with the API as-is and handle auth later
    print("  Note: For this test, we'll use a simplified flow.")
    print("  In production, tenant creation goes through admin endpoints.")
    return True


# ── Test Pipeline ──────────────────────────────────────────────────────────

def test_upload_policy(token: str, tenant_id: str, pdf_path: str, policy_number: str):
    """Upload a policy PDF and wait for processing."""
    step(f"Uploading policy: {policy_number}")

    if not os.path.exists(pdf_path):
        print(f"  ✗ PDF not found: {pdf_path}")
        return None

    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/api/v1/policies/upload",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
            data={"policy_number": policy_number},
        )

    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Upload successful")
        print(f"    Job ID: {data.get('job_id')}")
        print(f"    Status: {data.get('status')}")
        print(f"    Policy: {data.get('policy_number')}")
        return data
    else:
        print(f"  ✗ Upload failed ({r.status_code})")
        pretty(r.json())
        return None


def test_query_policy(token: str, policy_number: str, question: str):
    """Query a policy and display the response with citations."""
    step(f"Querying: \"{question}\"")

    r = requests.post(
        f"{BASE_URL}/api/v1/policies/{policy_number}/query",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"question": question},
    )

    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Answer received ({data.get('latency_ms', '?')}ms)")
        print(f"  Confidence: {data.get('confidence', 0):.2%}")
        print(f"\n  ANSWER:")
        print(f"  {data.get('answer', 'No answer')}")
        print(f"\n  CITATIONS ({len(data.get('citations', []))}):")
        for i, cite in enumerate(data.get("citations", []), 1):
            print(f"    [{i}] Page {cite.get('page')}, Section: {cite.get('section')}")
            print(f"        Score: {cite.get('similarity_score', 0):.4f}")
            text_preview = cite.get("text", "")[:100]
            print(f"        Text: {text_preview}...")
        return data
    else:
        print(f"  ✗ Query failed ({r.status_code})")
        pretty(r.json())
        return None


def test_check_available(token: str, policy_number: str):
    """Check if a policy is available for querying."""
    r = requests.get(
        f"{BASE_URL}/api/v1/policies/{policy_number}/available",
        headers={"Authorization": f"Bearer {token}"},
    )
    if r.status_code == 200:
        data = r.json()
        status = "✓ Available" if data.get("available") else "✗ Not available"
        print(f"  {status} - {data.get('chunk_count', 0)} chunks indexed")
        return data
    return None


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  INSURANCE RAG - END-TO-END PIPELINE TEST")
    print("="*60)

    # Check API health
    if not check_health():
        sys.exit(1)

    # For testing, we need to create a staff token manually.
    # In production this comes from Auth0.
    # We'll create a token using the app's security module.
    step("Creating test tokens")
    print("  Generating test staff token...")

    # Call a test setup endpoint (we'll add this)
    r = requests.post(f"{BASE_URL}/api/v1/auth/test-setup")
    if r.status_code == 200:
        setup = r.json()
        staff_token = setup["staff_token"]
        tenant_id = setup["tenant_id"]
        print(f"  ✓ Tenant ID: {tenant_id}")
        print(f"  ✓ Staff token created")
    else:
        print(f"  ✗ Test setup failed ({r.status_code})")
        print("  Make sure the /api/v1/auth/test-setup endpoint exists")
        pretty(r.json() if r.headers.get("content-type", "").startswith("application") else {"error": r.text[:200]})
        sys.exit(1)

    # Find sample PDFs
    pdf_dirs = [
        "./sample_policies",
        "../sample_policies",
        os.path.expanduser("~/sample_policies"),
    ]
    pdf_path = None
    for d in pdf_dirs:
        p = os.path.join(d, "POL-2024-HO-001.pdf")
        if os.path.exists(p):
            pdf_path = p
            break

    if not pdf_path:
        print("\n  ✗ No sample PDFs found. Place them in ./sample_policies/")
        print("  Expected: POL-2024-HO-001.pdf")
        sys.exit(1)

    # Upload
    result = test_upload_policy(staff_token, tenant_id, pdf_path, "POL-2024-HO-001")
    if not result:
        sys.exit(1)

    # Small delay for indexing
    time.sleep(2)

    # Check availability
    step("Checking policy availability")
    test_check_available(staff_token, "POL-2024-HO-001")

    # Query tests
    test_questions = [
        "What is the coverage limit for the dwelling?",
        "What is the deductible for wind and hail damage?",
        "Is flood damage covered under this policy?",
        "What is the personal liability coverage limit?",
        "How long do I have to file a proof of loss?",
        "Who is the mortgage holder on this policy?",
    ]

    for question in test_questions:
        test_query_policy(staff_token, "POL-2024-HO-001", question)
        time.sleep(1)  # Rate limiting courtesy

    # Policyholder verification test
    step("Testing policyholder verification")
    r = requests.post(
        f"{BASE_URL}/api/v1/auth/verify-policyholder",
        json={
            "tenant_id": tenant_id,
            "policy_number": "POL-2024-HO-001",
            "last_name": "Smith",
        },
    )
    if r.status_code == 200:
        verify = r.json()
        print(f"  ✓ Policyholder verified: {verify.get('verified')}")
        if verify.get("token"):
            # Test query with policyholder token
            test_query_policy(
                verify["token"],
                "POL-2024-HO-001",
                "What is my deductible?"
            )
    else:
        print(f"  ✗ Verification failed ({r.status_code})")
        pretty(r.json())

    print("\n" + "="*60)
    print("  TEST COMPLETE")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()