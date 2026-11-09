"""Real synthetic API JSON must satisfy the browser's call-receipt contract.

This is a Python/JavaScript serialization boundary test, not a real browser or
touch-device test. The child process receives synthetic receipt/operation IDs
only, never authentication credentials, session cookies or CSRF tokens.
"""

import json
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from staff_call_https_lab import synthetic_app


PROJECT = Path(__file__).resolve().parents[1]
ORIGIN = "https://staff.localhost:8444"
TOKEN = "synthetic-call-ui-contract-token-not-for-real-use-1234567890"
NODE_VALIDATOR = """
import { readFileSync } from 'node:fs';
import { validCallReceipt } from './web/staff/assets/staff-call.mjs';
const { receipt, record } = JSON.parse(readFileSync(0, 'utf8'));
if (!validCallReceipt(receipt, record)) process.exit(1);
"""


@pytest.mark.parametrize("scenario", ["waiting_call", "absent_call", "no_change", "rejected"])
def test_real_call_api_receipt_is_accepted_by_browser_validator(scenario):
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the real API/browser JSON contract test."
    with synthetic_app(TOKEN, 8444) as wrapper:
        with TestClient(wrapper, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            session = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
            assert session.status_code == 200
            headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
            listing = client.get("/staff/receptions")
            assert listing.status_code == 200
            status = "absent" if scenario == "absent_call" else "called" if scenario == "no_change" else "waiting"
            row = next(row for row in listing.json()["receptions"] if row["status"] == status)
            context = client.get(f'/staff/receptions/{row["id"]}/operation-context')
            assert context.status_code == 200
            metadata = context.json()["metadata"]
            command = {
                "operation_id": str(uuid4()), "action": "call",
                "expected_revision": row["revision"] + (scenario == "rejected"),
                "write_epoch": metadata["write_epoch"],
            }
            response = client.post(f'/staff/receptions/{row["id"]}/operations', json=command, headers=headers)
            assert response.status_code == (409 if scenario == "rejected" else 200)
            receipt = response.json()
            expected = "rejected" if scenario == "rejected" else "no_change" if scenario == "no_change" else "state_changed"
            assert receipt["outcome"] == expected
            assert receipt["response_status"] == response.status_code
            record = {
                "operation_id": command["operation_id"], "reception_id": row["id"],
                "database_id": metadata["database_id"], "request": command,
            }
            serialized = json.dumps({"receipt": receipt, "record": record}, allow_nan=False)
            # Keep the process boundary free of every authentication secret.
            assert TOKEN not in serialized and headers["X-CSRF-Token"] not in serialized
            completed = subprocess.run(
                [node, "--input-type=module", "-e", NODE_VALIDATOR],
                cwd=PROJECT, input=serialized, text=True, capture_output=True,
                timeout=15, check=False,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout == ""
