"""8446 transport and store contracts, using only freshly generated synthetic data."""

from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import socket
import sqlite3
import ssl
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
import pytest
import uvicorn

import staff_seat_https_lab as lab
from test_staff_call_absent_https_lab import command_for, endpoint
from test_staff_https_lab import certificate

ORIGIN = "https://staff.localhost:8446"
TOKEN = "seat-lab-test-credential-only-0123456789"


def login(client):
    response = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
    assert response.status_code == 200
    assert response.headers.get_list("x-omachi-preview-actions") == ["seat"]
    assert response.headers.get_list("x-omachi-preview") == ["synthetic"]
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture
def seat_lab(monkeypatch):
    monkeypatch.setenv("OMACHI_DB_PATH", "/must-not-open-real.sqlite3")
    monkeypatch.setenv("OMACHI_MODEL_PATH", "/must-not-load-real.joblib")
    with lab.synthetic_app(TOKEN) as wrapper:
        with TestClient(wrapper, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            headers = login(client)
            store = wrapper.app.state.store
            root = store.db_path.parent
            assert root.parent == Path("/tmp") and root.name.startswith("omachi-migration-")
            assert not wrapper.app.state.predictor.available
            data = client.get("/staff/receptions").json()
            meta = data["metadata"]
            assert meta["schema_version"] == 2 and meta["staff_write_mode"] == "revision_required"
            assert meta["seat_corrections_enabled"] is False
            rows = {r["status"]: r for r in data["receptions"]}
            yield store, client, headers, rows
    assert not root.exists()


def intake_tables(store):
    with closing(sqlite3.connect(store.db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        return {name: conn.execute(f'SELECT * FROM {name} ORDER BY rowid').fetchall()
                for name in ("daily_counters", "reception_snapshots", "predictions", "idempotency_keys")}


@pytest.mark.parametrize("status", ["waiting", "called", "absent"])
def test_seat_preserves_intake_and_appends_one_event_across_midnight(seat_lab, status):
    # The gate authorizes the action only. The UI separately limits initiation to waiting/called.
    store, client, headers, rows = seat_lab
    row = rows[status]
    before = store.get(row["id"])
    events = store.get_events(row["id"])
    saved_intake = intake_tables(store)
    command = command_for(client, row, "seat")
    response = client.post(endpoint(row), headers=headers, json=command)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["outcome"] == "state_changed" and receipt["resulting_revision"] == row["revision"] + 1
    after = store.get(row["id"])
    for key in ("id", "display_number", "received_at", "reception_order", "adults", "children",
                "total_people", "called_at", "snapshot", "prediction"):
        assert before[key] == after[key]
    start, end = (datetime.fromisoformat(after[k]) for k in ("received_at", "seated_at"))
    assert end.date() > start.date() and end.microsecond == 123456
    assert after["actual_wait_minutes"] == (end - start).total_seconds() / 60
    assert receipt["recorded_at"] == after["seated_at"] and after["status"] == "seated"
    appended = store.get_events(row["id"])
    assert appended[:-1] == events
    event = appended[-1]
    assert (event["from_status"], event["to_status"], event["occurred_at"], event["operation_id"]) == (
        status, "seated", after["seated_at"], command["operation_id"])
    assert client.post(endpoint(row), headers=headers, json=command).json() == receipt
    assert client.get("/staff/operations/" + command["operation_id"]).json() == receipt
    assert store.get_events(row["id"]) == appended and intake_tables(store) == saved_intake


def test_gate_denials_never_forward_to_application():
    async def unexpected(scope, receive, send):
        pytest.fail("Denied request reached the inner application")

    client = TestClient(lab.SeatOnlyLab(unexpected), base_url=ORIGIN)
    row = {"id": str(uuid4())}
    command = dict(action="seat", operation_id=str(uuid4()), expected_revision=0, write_epoch=str(uuid4()))
    headers = {"Origin": ORIGIN}
    for action in ("call", "absent", "return", "cancel"):
        response = client.post(endpoint(row), headers=headers, json={**command, "action": action})
        assert response.status_code == 403 and response.headers["x-omachi-preview-actions"] == "seat"
    for suffix in ("seating-corrections", "quality-reviews", "quality-assessments"):
        assert client.post(f'/staff/receptions/{row["id"]}/{suffix}', headers=headers, json=command).status_code == 403
    assert client.post(f'/receptions/{row["id"]}/seat', headers=headers).status_code == 403
    raw = json.dumps(command)
    for body in ("{", raw.replace('"seat"', '"call","action":"seat"'),
                 json.dumps({**command, "operation_id": "invalid"})):
        assert client.post(endpoint(row), content=body, headers={**headers, "Content-Type": "application/json"}).status_code == 422
    client.close()


def test_session_origin_csrf_host_and_scheme(seat_lab):
    store, client, headers, rows = seat_lab
    row = rows["waiting"]
    command = command_for(client, row, "seat")
    before = store.db_path.read_bytes()
    for invalid in ({"Origin": ORIGIN}, {"X-CSRF-Token": headers["X-CSRF-Token"]},
                    {**headers, "Origin": "https://staff.localhost:8445"},
                    {**headers, "Host": "localhost:8446"}):
        assert client.post(endpoint(row), json=command, headers=invalid).status_code == 403
    assert client.post("http://staff.localhost:8446" + endpoint(row), json=command, headers=headers).status_code == 403
    assert client.post(endpoint(row), json=command, headers={**headers, "X-CSRF-Token": "wrong"}).status_code == 401
    client.cookies.clear()
    assert client.post(endpoint(row), json=command, headers={**headers, "X-Staff-Token": TOKEN}).status_code == 401
    assert before == store.db_path.read_bytes()


@pytest.mark.parametrize("field,code", [("expected_revision", "revision_conflict"), ("write_epoch", "write_epoch_conflict")])
def test_conflict_archived_without_seating(seat_lab, field, code):
    store, client, headers, rows = seat_lab
    row = rows["called"]
    command = command_for(client, row, "seat")
    command[field] = row["revision"] + 1 if field == "expected_revision" else str(uuid4())
    events = store.get_events(row["id"])
    response = client.post(endpoint(row), json=command, headers=headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code and response.json()["outcome"] == "rejected"
    assert client.get("/staff/operations/" + command["operation_id"]).json() == response.json()
    assert store.get_events(row["id"]) == events and store.get(row["id"])["seated_at"] is None


def test_lost_post_response_recovers_without_second_event():
    with lab.synthetic_app(TOKEN) as wrapper:
        lose_next = True

        async def transport(scope, receive, send):
            async def lose_once(message):
                nonlocal lose_next
                if lose_next and scope.get("path", "").endswith("/operations") and message["type"] == "http.response.body":
                    lose_next = False
                    raise OSError("lost committed response")
                await send(message)
            await wrapper(scope, receive, lose_once)

        with TestClient(transport, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            headers = login(client)
            row = client.get("/staff/receptions").json()["receptions"][0]
            command = command_for(client, row, "seat")
            events = wrapper.app.state.store.get_events(row["id"])
            with pytest.raises(OSError, match="lost committed response"):
                client.post(endpoint(row), headers=headers, json=command)
            saved = client.get("/staff/operations/" + command["operation_id"]).json()
            assert saved["outcome"] == "state_changed" and saved["operation_id"] == command["operation_id"]
            assert client.post(endpoint(row), headers=headers, json=command).json() == saved
            assert len(wrapper.app.state.store.get_events(row["id"])) == len(events) + 1


def test_cli_serves_verified_tls_with_fixed_seat_authority(certificate, monkeypatch):
    """Use a reserved ephemeral loopback socket so tests cannot claim or contact a running lab."""
    cert, key = certificate
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", TOKEN)
    import staff_call_absent_https_lab as shared

    checked_ports = []
    monkeypatch.setattr(shared, "check_loopback_port", checked_ports.append)

    def exercise(app, **options):
        assert options["host"] == "127.0.0.1" and options["port"] == 8446
        assert options["workers"] == 1 and not options["proxy_headers"] and not options["reload"]
        server = uvicorn.Server(uvicorn.Config(app, **options))
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 10
                while not server.started and thread.is_alive() and monotonic() < deadline:
                    sleep(0.01)
                assert server.started
                # Physical destination is private to this test. HTTP authority and TLS SNI are fixed.
                with httpx.Client(base_url=f"https://127.0.0.1:{port}", trust_env=False,
                                  verify=ssl.create_default_context(cafile=str(cert)),
                                  headers={"Host": "staff.localhost:8446"}) as client:
                    tls = {"sni_hostname": "staff.localhost"}
                    session = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN},
                                          extensions=tls)
                    assert session.status_code == 200 and session.headers["x-omachi-preview-actions"] == "seat"
                    headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
                    listing = client.get("/staff/receptions", extensions=tls).json()
                    row = next(row for row in listing["receptions"] if row["status"] == "called")
                    command = dict(operation_id=str(uuid4()), action="seat", expected_revision=row["revision"],
                                   write_epoch=listing["metadata"]["write_epoch"])
                    result = client.post(endpoint(row), headers=headers, json=command, extensions=tls)
                    assert result.status_code == 200 and result.json()["reception"]["status"] == "seated"
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()

    monkeypatch.setattr(uvicorn, "run", exercise)
    assert lab.main(["--cert", str(cert), "--key", str(key)]) == 0
    assert checked_ports == [8446]
