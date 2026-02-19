"""
Communication Bucket + Multi-Policy Stress Test

Usage:
    python scripts/test_communications.py

Tests:
    1. Uploads all 3 sample policies (homeowners, auto, commercial)
    2. Uploads 4 communication documents (letter, agent notes, E&O, claims)
    3. Queries communications with staff token (cross-document search)
    4. Tests communication type filtering
    5. Tests policy isolation (policyholder can't see comms)
    6. Queries across different policies
"""

import requests
import json
import time
import sys
import os
import glob

BASE_URL = "http://localhost:8000"
STAFF_TOKEN = None
TENANT_ID = None


def pretty(data):
    print(json.dumps(data, indent=2, default=str))

def step(msg):
    print(f"\n{'='*70}")
    print(f"  {msg}")
    print(f"{'='*70}")

def substep(msg):
    print(f"\n  --- {msg} ---")


# ── Setup ──────────────────────────────────────────────────────────────────

def setup():
    global STAFF_TOKEN, TENANT_ID

    r = requests.get(f"{BASE_URL}/health", timeout=5)
    if r.status_code != 200:
        print("✗ API not running")
        sys.exit(1)
    print("✓ API healthy")

    r = requests.post(f"{BASE_URL}/api/v1/auth/test-setup")
    data = r.json()
    STAFF_TOKEN = data["staff_token"]
    TENANT_ID = data["tenant_id"]
    print(f"✓ Tenant: {TENANT_ID}")
    print(f"✓ Staff token ready")


def auth_header(token=None):
    return {"Authorization": f"Bearer {token or STAFF_TOKEN}"}


# ── Upload Helpers ─────────────────────────────────────────────────────────

def upload_policy(pdf_path, policy_number):
    """Upload a policy PDF. Skip if already indexed."""
    # Check if already available
    r = requests.get(
        f"{BASE_URL}/api/v1/policies/{policy_number}/available",
        headers=auth_header(),
    )
    if r.status_code == 200 and r.json().get("available"):
        chunks = r.json().get("chunk_count", "?")
        print(f"  ⏭ {policy_number} already indexed ({chunks} chunks)")
        return True

    if not os.path.exists(pdf_path):
        print(f"  ✗ File not found: {pdf_path}")
        return False

    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/api/v1/policies/upload",
            headers=auth_header(),
            files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
            data={"policy_number": policy_number},
        )

    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ {policy_number} uploaded - Status: {data['status']}")
        return True
    else:
        print(f"  ✗ {policy_number} upload failed ({r.status_code})")
        print(f"    {r.json().get('error', r.text[:200])}")
        return False


def upload_communication(pdf_path, comm_type, title=None):
    """Upload a communication document."""
    if not os.path.exists(pdf_path):
        print(f"  ✗ File not found: {pdf_path}")
        return None

    with open(pdf_path, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/api/v1/communications/upload",
            headers=auth_header(),
            files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
            data={
                "communication_type": comm_type,
                "title": title or os.path.basename(pdf_path),
            },
        )

    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Uploaded [{comm_type}] {os.path.basename(pdf_path)} - Status: {data['status']}")
        return data
    else:
        print(f"  ✗ Upload failed ({r.status_code})")
        print(f"    {r.json().get('error', r.text[:200])}")
        return None


def query_policy(policy_number, question, token=None):
    """Query a specific policy."""
    r = requests.post(
        f"{BASE_URL}/api/v1/policies/{policy_number}/query",
        headers={**auth_header(token), "Content-Type": "application/json"},
        json={"question": question},
    )
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Answer ({data.get('latency_ms', '?')}ms, conf: {data.get('confidence', 0):.1%})")
        print(f"    {data['answer'][:200]}...")
        print(f"    Citations: {len(data.get('citations', []))}")
        return data
    else:
        print(f"  ✗ Query failed ({r.status_code}): {r.json().get('error', '')}")
        return None


def query_communications(question, comm_type=None, token=None):
    """Query communications bucket."""
    payload = {"question": question}
    if comm_type:
        payload["communication_type"] = comm_type

    r = requests.post(
        f"{BASE_URL}/api/v1/communications/query",
        headers={**auth_header(token), "Content-Type": "application/json"},
        json=payload,
    )
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Answer ({data.get('latency_ms', '?')}ms, conf: {data.get('confidence', 0):.1%})")
        print(f"    {data['answer'][:300]}...")
        print(f"    Citations: {len(data.get('citations', []))}")
        return data
    else:
        error = r.json().get('error', r.text[:200]) if r.headers.get('content-type', '').startswith('application/json') else r.text[:200]
        print(f"  ✗ Query failed ({r.status_code}): {error}")
        return None


# ── Tests ──────────────────────────────────────────────────────────────────

def test_upload_all_policies():
    step("PHASE 1: Upload All Policies")

    policies = [
        ("sample_policies/POL-2024-HO-001.pdf", "POL-2024-HO-001"),
        ("sample_policies/POL-2024-AU-002.pdf", "POL-2024-AU-002"),
        ("sample_policies/POL-2024-CGL-003.pdf", "POL-2024-CGL-003"),
    ]

    for pdf_path, policy_num in policies:
        upload_policy(pdf_path, policy_num)
        time.sleep(1)


def test_upload_communications():
    step("PHASE 2: Upload Communications")

    comms = [
        ("sample_communications/letter_renewal_smith.pdf", "letter", "Renewal Notice - Smith POL-2024-HO-001"),
        ("sample_communications/agent_notes_smith_2024.pdf", "agent_note", "Agent Notes - Smith 2024"),
        ("sample_communications/eo_incident_rodriguez_2024.pdf", "e_and_o", "E&O Incident - Rodriguez Unlisted Driver"),
        ("sample_communications/claims_letter_smith_fence.pdf", "claims", "Claim Settlement - Smith Fence #CLM-2024-03-0047"),
    ]

    for pdf_path, comm_type, title in comms:
        upload_communication(pdf_path, comm_type, title)
        time.sleep(1)


def test_list_communications():
    step("PHASE 3: List Communications")

    r = requests.get(
        f"{BASE_URL}/api/v1/communications",
        headers=auth_header(),
    )
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ Total communications: {data['total']}")
        for comm in data['communications']:
            print(f"    [{comm['communication_type']}] {comm['title']} - {comm['status']}")
    else:
        print(f"  ✗ List failed ({r.status_code})")

    # Test type filter
    substep("Filter by type: e_and_o")
    r = requests.get(
        f"{BASE_URL}/api/v1/communications?communication_type=e_and_o",
        headers=auth_header(),
    )
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ E&O documents: {data['total']}")
    else:
        print(f"  ✗ Filter failed")


def test_communication_queries():
    step("PHASE 4: Communication Bucket Queries (Staff Only)")

    queries = [
        ("What E&O incidents have been reported?", None),
        ("What happened with the Rodriguez auto claim?", "e_and_o"),
        ("What was the fence damage claim settlement amount?", "claims"),
        ("What is the renewal premium for Smith's homeowners policy?", "letter"),
        ("What did the agent note about the roof condition?", "agent_note"),
        ("What corrective measures were implemented after the E&O incident?", None),
        ("What is claim number CLM-2024-03-0047 about?", None),
        ("Who is the adjuster assigned to the Smith fence claim?", None),
    ]

    for question, comm_type in queries:
        filter_label = f" [filter: {comm_type}]" if comm_type else ""
        substep(f'"{question}"{filter_label}')
        query_communications(question, comm_type)
        time.sleep(1)


def test_cross_policy_queries():
    step("PHASE 5: Cross-Policy Queries")

    queries = [
        ("POL-2024-HO-001", "What is my dwelling coverage limit?"),
        ("POL-2024-AU-002", "What are my liability limits?"),
        ("POL-2024-CGL-003", "What is the general aggregate limit?"),
        ("POL-2024-AU-002", "What vehicles are covered?"),
        ("POL-2024-CGL-003", "Is product liability covered?"),
        ("POL-2024-HO-001", "What perils are covered?"),
    ]

    for policy, question in queries:
        substep(f"{policy}: \"{question}\"")
        query_policy(policy, question)
        time.sleep(1)


def test_policyholder_isolation():
    step("PHASE 6: Policyholder Isolation Tests")

    # Verify as Smith
    substep("Verify policyholder: Smith (POL-2024-HO-001)")
    r = requests.post(
        f"{BASE_URL}/api/v1/auth/verify-policyholder",
        json={
            "tenant_id": TENANT_ID,
            "policy_number": "POL-2024-HO-001",
            "last_name": "Smith",
        },
    )
    if r.status_code != 200:
        print(f"  ✗ Verification failed ({r.status_code})")
        return

    smith_token = r.json()["token"]
    print(f"  ✓ Smith verified, token received")

    # Smith can query their own policy
    substep("Smith queries own policy (should work)")
    query_policy("POL-2024-HO-001", "What is my deductible?", token=smith_token)

    # Smith cannot query another policy
    substep("Smith queries Rodriguez's policy (should be denied)")
    r = requests.post(
        f"{BASE_URL}/api/v1/policies/POL-2024-AU-002/query",
        headers={**auth_header(smith_token), "Content-Type": "application/json"},
        json={"question": "What are the liability limits?"},
    )
    if r.status_code == 403:
        print(f"  ✓ Correctly denied: {r.json().get('error', r.json().get('detail', ''))}")
    else:
        print(f"  ✗ Should have been denied but got {r.status_code}")

    # Smith cannot query communications
    substep("Smith queries communications (should be denied)")
    r = requests.post(
        f"{BASE_URL}/api/v1/communications/query",
        headers={**auth_header(smith_token), "Content-Type": "application/json"},
        json={"question": "What E&O incidents happened?"},
    )
    if r.status_code == 403:
        print(f"  ✓ Correctly denied: {r.json().get('error', r.json().get('detail', ''))}")
    else:
        print(f"  ✗ Should have been denied but got {r.status_code}")

    # Verify Rodriguez (commercial - company name)
    substep("Verify policyholder: Springfield Hardware (POL-2024-CGL-003)")
    r = requests.post(
        f"{BASE_URL}/api/v1/auth/verify-policyholder",
        json={
            "tenant_id": TENANT_ID,
            "policy_number": "POL-2024-CGL-003",
            "company_name": "Springfield Hardware",
        },
    )
    if r.status_code == 200:
        hw_token = r.json()["token"]
        print(f"  ✓ Springfield Hardware verified")
        substep("Springfield Hardware queries own CGL policy")
        query_policy("POL-2024-CGL-003", "What is my per-occurrence limit?", token=hw_token)
    else:
        print(f"  ✗ Verification failed ({r.status_code})")


def test_negative_verification():
    step("PHASE 7: Negative Verification Tests")

    # Wrong last name
    substep("Wrong last name (should fail)")
    r = requests.post(
        f"{BASE_URL}/api/v1/auth/verify-policyholder",
        json={
            "tenant_id": TENANT_ID,
            "policy_number": "POL-2024-HO-001",
            "last_name": "Johnson",
        },
    )
    if r.status_code == 401:
        print(f"  ✓ Correctly rejected wrong last name")
    else:
        print(f"  ✗ Should have been rejected but got {r.status_code}")

    # Wrong policy number
    substep("Non-existent policy (should fail)")
    r = requests.post(
        f"{BASE_URL}/api/v1/auth/verify-policyholder",
        json={
            "tenant_id": TENANT_ID,
            "policy_number": "POL-FAKE-999",
            "last_name": "Smith",
        },
    )
    if r.status_code == 401:
        print(f"  ✓ Correctly rejected non-existent policy")
    else:
        print(f"  ✗ Should have been rejected but got {r.status_code}")


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*70)
    print("  INSURANCE RAG - COMMUNICATION BUCKET + STRESS TEST")
    print("="*70)

    setup()

    test_upload_all_policies()
    test_upload_communications()
    test_list_communications()
    test_communication_queries()
    test_cross_policy_queries()
    test_policyholder_isolation()
    test_negative_verification()

    print("\n" + "="*70)
    print("  ALL TESTS COMPLETE")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()