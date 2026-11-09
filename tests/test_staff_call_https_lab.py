"""The call lab has one disposable DB and one narrowly allowed business write."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import HTTPResponse
from http.cookies import SimpleCookie
import json
from pathlib import Path
import socket
import sqlite3
import ssl
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

import staff_call_https_lab as lab
from staff_sessions import COOKIE_NAME, IDLE_SECONDS
from test_staff_https_lab import certificate  # Shared disposable TLS fixture.


TOKEN = "synthetic-call-only-shared-credential-123456789012345"
ORIGIN = "https://staff.localhost:8444"


@contextmanager
def environment(*, lose_response=False):
    with lab.synthetic_app(TOKEN, 8444) as wrapper:
        pending_loss = [lose_response]

        async def transport(scope, receive, send):
            async def interrupted_send(message):
                if (pending_loss[0] and scope.get("path", "").endswith("/operations")
                        and message["type"] == "http.response.body"):
                    pending_loss[0] = False
                    raise OSError("synthetic response transport lost after commit")
                await send(message)
            await wrapper(scope, receive, interrupted_send)

        with TestClient(transport, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            response = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
            assert response.status_code == 200
            headers = {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
            rows = client.get("/staff/receptions").json()["receptions"]
            row = next(row for row in rows if row["status"] == "waiting")
            context = client.get(f'/staff/receptions/{row["id"]}/operation-context').json()
            command = dict(operation_id=str(uuid4()), action="call", expected_revision=row["revision"],
                           write_epoch=context["metadata"]["write_epoch"])
            yield wrapper.app, client, headers, row, command


def endpoint(row):
    return f'/staff/receptions/{row["id"]}/operations'


def read_database(app):
    # Only a newly generated database from the current context can be passed here.
    path = app.state.store.db_path
    assert path.parent.parent == Path("/tmp") and path.parent.name.startswith("omachi-migration-")
    with closing(sqlite3.connect(path)) as conn:
        tables = ("receptions", "reception_snapshots", "predictions", "idempotency_keys",
                  "reception_events", "daily_counters", "sqlite_sequence", "staff_operations")
        return {table: conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}


def test_fresh_db_only_ignores_real_settings_and_is_removed(monkeypatch):
    monkeypatch.setenv("OMACHI_DB_PATH", "/must-not-open-real.sqlite3")
    monkeypatch.setenv("OMACHI_MODEL_PATH", "/must-not-load-real.joblib")
    identities = []
    roots = []
    for _ in range(2):
        with environment() as (app, client, _, row, _):
            root = app.state.store.db_path.parent
            roots.append(root)
            context = client.get(f'/staff/receptions/{row["id"]}/operation-context').json()
            metadata = context["metadata"]
            assert metadata["schema_version"] == 2
            assert metadata["staff_write_mode"] == "revision_required"
            assert metadata["seat_corrections_enabled"] is False
            assert metadata["write_enabled"] is True
            assert not app.state.predictor.available
            identities.append((metadata["database_id"], metadata["write_epoch"]))
            for view in ("active", "recently_seated"):
                receptions = client.get('/staff/receptions', params={"view": view}).json()["receptions"]
                assert len(receptions) == 3
                for reception in receptions:
                    assert client.get('/staff/receptions/' + reception["id"]).status_code == 200
            assert client.get("/staff/login").status_code == 200
            assert client.get("/staff/receptions").headers["x-omachi-preview"] == "synthetic"
            assert client.get("/staff/receptions").headers["x-omachi-preview-actions"] == "call"
        assert not root.exists()
    assert roots[0] != roots[1] and identities[0] != identities[1]


@pytest.mark.parametrize("initial", ["waiting", "absent"])
def test_call_uses_existing_atomic_operation_and_preserves_history(initial):
    with environment() as (app, client, headers, _, command):
        rows = client.get("/staff/receptions").json()["receptions"]
        row = next(row for row in rows if row["status"] == initial)
        command["expected_revision"] = row["revision"]
        before = read_database(app)
        response = client.post(endpoint(row), json=command, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["outcome"] == "state_changed" and result["resulting_revision"] == row["revision"] + 1
        current = client.get(f'/staff/receptions/{row["id"]}/operation-context').json()["reception"]
        assert current["status"] == "called" and current["called_at"]
        assert current["actual_wait_minutes"] is None and current["seated_at"] is None
        for key in ("id", "display_number", "received_at", "adults", "children"):
            assert current[key] == row[key]
        after = read_database(app)
        for table in ("reception_snapshots", "predictions", "idempotency_keys", "daily_counters"):
            assert before[table] == after[table]
        assert after["reception_events"][:-1] == before["reception_events"]
        assert len(after["staff_operations"]) == 1
        assert TOKEN.encode() not in app.state.store.db_path.read_bytes()
        # Treat the first response as lost: lookup and fixed-request replay recover
        # the saved receipt, with no second mutation or revision increase.
        committed = app.state.store.db_path.read_bytes()
        lookup = client.get('/staff/operations/' + command["operation_id"])
        assert lookup.status_code == 200 and lookup.json() == result
        assert client.post(endpoint(row), json=command, headers=headers).json() == result
        assert app.state.store.db_path.read_bytes() == committed
        assert [row["id"] for row in client.get("/staff/receptions").json()["receptions"]] == [row["id"] for row in rows]


@pytest.mark.parametrize("action", ["seat", "absent", "return", "cancel", "seating_correction", "CALL", ""])
def test_other_actions_are_rejected_without_operation_record(action):
    with environment() as (app, client, headers, row, command):
        before = app.state.store.db_path.read_bytes()
        response = client.post(endpoint(row), json={**command, "action": action}, headers=headers)
        assert response.status_code in (403, 422)
        assert app.state.store.db_path.read_bytes() == before


@pytest.mark.parametrize("suffix", ["seating-corrections", "call", "seat", "absent", "return", "cancel",
                                    "quality-reviews", "quality-assessments"])
def test_other_write_routes_and_customer_routes_remain_closed(suffix):
    with environment() as (app, client, headers, row, command):
        before = app.state.store.db_path.read_bytes()
        for path in (f'/staff/receptions/{row["id"]}/{suffix}', f'/receptions/{row["id"]}/{suffix}', '/receptions'):
            assert client.post(path, json=command, headers=headers).status_code == 403
        assert client.get('/queue').status_code == 403
        assert app.state.store.db_path.read_bytes() == before


@pytest.mark.parametrize("change", [
    {"expected_revision": True}, {"expected_revision": "0"}, {"expected_revision": -1},
    {"expected_revision": 2**63}, {"operation_id": "not-a-uuid"}, {"write_epoch": None},
    {"operation_id": "00000000-0000-0000-0000-000000000000"},
    {"principal_id": "admin"}, {"staff_write_mode": "legacy"}, {"target_event_id": 1},
])
def test_strict_existing_command_contract_rejects_extras_and_wrong_types(change):
    with environment() as (app, client, headers, row, command):
        before = app.state.store.db_path.read_bytes()
        response = client.post(endpoint(row), json={**command, **change}, headers=headers)
        assert response.status_code == 422
        assert app.state.store.db_path.read_bytes() == before


@pytest.mark.parametrize("raw", [b'[]', b'null', b'"call"', b'{', b'{}{}', b'\xff',
    b'{"action":"seat","action":"call"}', b'{"expected_revision":NaN}',
    b'{"expected_revision":Infinity}', b'{"expected_revision":-Infinity}', b'[' * 1100])
def test_ambiguous_or_invalid_json_never_reaches_app(raw):
    with environment() as (app, client, headers, row, _):
        before = app.state.store.db_path.read_bytes()
        response = client.post(endpoint(row), content=raw, headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 422
        assert "input" not in response.text and TOKEN not in response.text
        assert app.state.store.db_path.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing-type", "text", "encoding", "query", "oversize", "duplicate-type"])
def test_body_envelope_has_narrow_limits(kind):
    with environment() as (app, client, headers, row, command):
        body = json.dumps(command).encode()
        extra = {"Content-Type": "application/json"}
        path = endpoint(row)
        if kind == "missing-type":
            extra = {}
        elif kind == "text":
            extra = {"Content-Type": "text/plain"}
        elif kind == "encoding":
            extra["Content-Encoding"] = "gzip"
        elif kind == "query":
            path += "?action=seat"
        elif kind == "oversize":
            body += b' ' * lab.MAX_BODY_BYTES
        before = app.state.store.db_path.read_bytes()
        combined = list({**headers, **extra}.items())
        if kind == "duplicate-type":
            combined.append(("Content-Type", "application/json"))
        assert client.post(path, content=body, headers=combined).status_code in (413, 422)
        assert app.state.store.db_path.read_bytes() == before


@pytest.mark.parametrize("kind", ["no-cookie", "bad-cookie", "no-csrf", "bad-csrf", "no-origin", "other-origin",
                                   "native-host", "http", "duplicate-host", "forwarded"])
def test_authentication_origin_and_csrf_cannot_be_bypassed(kind):
    with environment() as (app, client, headers, row, command):
        headers = dict(headers)
        path = endpoint(row)
        if kind == "no-cookie":
            client.cookies.clear()
            headers["X-Staff-Token"] = TOKEN
        elif kind == "bad-cookie":
            headers["Cookie"] = COOKIE_NAME + "=invalid"
            headers["X-Staff-Token"] = TOKEN
        elif kind == "no-csrf":
            headers.pop("X-CSRF-Token")
        elif kind == "bad-csrf":
            headers["X-CSRF-Token"] = "invalid"
        elif kind == "no-origin":
            headers.pop("Origin")
        elif kind == "other-origin":
            headers["Origin"] = "https://localhost:8444"
        elif kind == "native-host":
            client.cookies.clear()
            headers["X-Staff-Token"] = TOKEN
            path = "https://localhost:8444" + path
        elif kind == "http":
            path = "http://staff.localhost:8444" + path
        elif kind == "duplicate-host":
            headers = list(headers.items()) + [("Host", "staff.localhost:8444"), ("Host", "localhost:8444")]
        elif kind == "forwarded":
            headers["X-Forwarded-Host"] = "staff.localhost:8444"
        before = app.state.store.db_path.read_bytes()
        assert client.post(path, json=command, headers=headers).status_code in (401, 403)
        assert app.state.store.db_path.read_bytes() == before


def test_conflicts_replay_and_historical_receipt_are_distinct_from_current_state():
    with environment() as (app, client, headers, row, command):
        old_context = client.get(f'/staff/receptions/{row["id"]}/operation-context').json()
        success = client.post(endpoint(row), json=command, headers=headers).json()
        for changed in ({"expected_revision": 1}, {"write_epoch": str(uuid4())}):
            before = app.state.store.db_path.read_bytes()
            assert client.post(endpoint(row), json={**command, **changed}, headers=headers).status_code == 409
            assert app.state.store.db_path.read_bytes() == before
        other = next(r for r in client.get('/staff/receptions').json()["receptions"] if r["id"] != row["id"])
        assert client.post(endpoint(other), json=command, headers=headers).status_code == 409
        for changed, error in (({}, "revision_conflict"), ({"write_epoch": str(uuid4())}, "write_epoch_conflict")):
            stale = {**command, **changed, "operation_id": str(uuid4())}
            response = client.post(endpoint(row), json=stale, headers=headers)
            assert response.status_code == 409 and response.json()["error"]["code"] == error
            receipt = client.get('/staff/operations/' + stale["operation_id"])
            assert receipt.status_code == 200 and receipt.json()["response_status"] == 409
        assert client.get('/staff/operations/' + str(uuid4())).status_code == 404
        # A later valid no-change operation has its own receipt. Original replay
        # still returns its historical result, not a fabricated current context.
        noop = client.post(endpoint(row), json={**command, "operation_id": str(uuid4()), "expected_revision": 1}, headers=headers)
        assert noop.json()["outcome"] == "no_change"
        assert client.post(endpoint(row), json=command, headers=headers).json() == success
        now = client.get(f'/staff/receptions/{row["id"]}/operation-context').json()
        assert old_context["reception"]["status"] == "waiting" and now["reception"]["status"] == "called"
        assert now["reception"]["revision"] == 1


@pytest.mark.parametrize("same_id", [True, False])
def test_simultaneous_call_requests_do_not_duplicate_state_change(same_id):
    with environment() as (app, client, headers, row, command):
        headers = {**headers, "Cookie": COOKIE_NAME + "=" + client.cookies.get(COOKIE_NAME)}
        before = read_database(app)
        commands = [command, {**command, "operation_id": command["operation_id"] if same_id else str(uuid4())}]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: client.post(endpoint(row), json=body, headers=headers), commands))
        assert sorted(r.status_code for r in results) == ([200, 200] if same_id else [200, 409])
        if same_id:
            assert results[0].json() == results[1].json()
        after = read_database(app)
        assert len(after["reception_events"]) == len(before["reception_events"]) + 1
        assert len(after["staff_operations"]) == (1 if same_id else 2)


@pytest.mark.parametrize("stage", ["event", "operation"])
def test_transaction_failure_rolls_back_then_fixed_request_recovers(monkeypatch, stage):
    with environment() as (app, client, headers, row, command):
        attribute = "_append_event" if stage == "event" else "_save_staff_operation"
        original = getattr(app.state.store, attribute)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("synthetic failure with secret-like text")

        monkeypatch.setattr(app.state.store, attribute, fail)
        before = app.state.store.db_path.read_bytes()
        response = client.post(endpoint(row), json=command, headers=headers)
        assert response.status_code == 503 and "secret-like" not in response.text
        assert app.state.store.db_path.read_bytes() == before
        assert client.get('/staff/operations/' + command["operation_id"]).status_code == 404
        monkeypatch.setattr(app.state.store, attribute, original)
        assert client.post(endpoint(row), json=command, headers=headers).status_code == 200


def test_expired_session_and_relogin_recover_receipt_without_replay_mutation():
    with environment() as (app, client, headers, row, command):
        result = client.post(endpoint(row), json=command, headers=headers).json()
        app.state.staff_sessions.clock = lambda: monotonic() + IDLE_SECONDS + 1
        before = app.state.store.db_path.read_bytes()
        assert client.get('/staff/operations/' + command["operation_id"]).status_code == 401
        assert client.post(endpoint(row), json=command, headers=headers).status_code == 401
        response = client.post('/staff/session', headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
        assert response.status_code == 200
        headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert client.get('/staff/operations/' + command["operation_id"]).json() == result
        assert client.post(endpoint(row), json=command, headers=headers).json() == result
        assert client.delete('/staff/session', headers=headers).status_code == 200
        assert client.get('/staff/operations/' + command["operation_id"]).status_code == 401
        assert app.state.store.db_path.read_bytes() == before


def test_stream_limits_disconnect_timeout_and_duplicate_length_never_forward(monkeypatch):
    forwarded = []

    async def inner(*args):
        forwarded.append(True)

    wrapper = lab.CallOnlyLab(inner, 8444)

    async def run(messages, extra=(), slow=False):
        sent = []
        scope = dict(type="http", scheme="https", method="POST", path=f'/staff/receptions/{uuid4()}/operations',
                     query_string=b'', headers=[(b'host', b'staff.localhost:8444'), (b'content-type', b'application/json'), *extra])
        iterator = iter(messages)

        async def receive():
            if slow:
                await asyncio.sleep(1)
            return next(iterator)

        await wrapper(scope, receive, lambda message: _collect(sent, message))
        return sent

    monkeypatch.setattr(lab, "BODY_TIMEOUT_SECONDS", 0.01)
    assert asyncio.run(run([{"type": "http.disconnect"}])) == []
    cases = [
        ([{"type": "http.request", "body": b'x' * 2048, "more_body": True}] * 3, (), 413),
        ([{"type": "http.request", "body": b'{}'}], ((b'content-length', b'1'),), 422),
        ([], ((b'content-length', b'2'), (b'content-length', b'2')), 422),
        ([], ((b'content-length', b'-1'),), 422),
        ([], ((b'content-length', b'999999999999999999'),), 413),
    ]
    for messages, extra, expected in cases:
        assert asyncio.run(run(messages, extra))[0]["status"] == expected
    assert asyncio.run(run([], slow=True))[0]["status"] == 408
    assert not forwarded


async def _collect(sent, message):
    sent.append(message)


def test_actual_response_transport_exception_after_commit_recovers_same_operation():
    with environment(lose_response=True) as (app, client, headers, row, command):
        before = read_database(app)
        with pytest.raises(OSError, match="synthetic response transport lost"):
            client.post(endpoint(row), json=command, headers=headers)
        committed = app.state.store.db_path.read_bytes()
        receipt = client.get('/staff/operations/' + command["operation_id"])
        assert receipt.status_code == 200 and receipt.json()["outcome"] == "state_changed"
        assert client.post(endpoint(row), json=command, headers=headers).json() == receipt.json()
        assert app.state.store.db_path.read_bytes() == committed
        after = read_database(app)
        assert len(after["reception_events"]) == len(before["reception_events"]) + 1
        assert len(after["staff_operations"]) == 1


def test_failed_synthetic_preparation_removes_only_its_new_workspace(monkeypatch):
    roots = []

    def fail(workspace, database, **kwargs):
        roots.append(workspace.root)
        assert database.parent == workspace.root
        raise OSError("synthetic migration failure")

    monkeypatch.setattr(lab, "migrate", fail)
    with pytest.raises(OSError, match="synthetic migration failure"):
        with lab.synthetic_app(TOKEN, 8444):
            pytest.fail("Incomplete preparation must not expose an app")
    assert len(roots) == 1 and not roots[0].exists()


def test_chunked_valid_body_is_forwarded_once_with_original_bytes():
    with environment() as (_, client, headers, row, command):
        raw = json.dumps(command).encode()
        response = client.post(endpoint(row), content=iter([raw[:40], raw[40:]]),
                               headers={**headers, "Content-Type": "application/json"})
        assert response.status_code == 200 and response.json()["outcome"] == "state_changed"


@pytest.mark.parametrize("option", ["--db", "--database", "--model", "--host", "--workers", "--allow-writes", "--action"])
def test_cli_has_no_way_to_select_other_data_or_expand_permissions(option):
    with pytest.raises(SystemExit) as failure:
        lab.main(["--cert", "unused", "--key", "unused", option, "unsafe"])
    assert failure.value.code == 2


@pytest.mark.parametrize("check", [False, True])
def test_cli_fixes_tls_loopback_single_worker_and_cleanup(certificate, monkeypatch, capsys, check):
    cert, key = certificate
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", TOKEN)
    ports, calls, roots = [], [], []
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: ports.append(port))
    real_factory = lab.synthetic_app

    @contextmanager
    def observe(*args):
        with real_factory(*args) as wrapper:
            # Inspect only the newly created, disposable app during its lifespan.
            with TestClient(wrapper, base_url=ORIGIN, client=("127.0.0.1", 50000)):
                roots.append(wrapper.app.state.store.db_path.parent)
            yield wrapper

    monkeypatch.setattr(lab, "synthetic_app", observe)
    monkeypatch.setattr(lab.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert lab.main(["--cert", str(cert), "--key", str(key)] + (["--check"] if check else [])) == 0
    assert ports == [8444] and all(not root.exists() for root in roots)
    assert len(calls) == (0 if check else 1)
    if calls:
        assert calls[0] == dict(host="127.0.0.1", port=8444, workers=1, reload=False,
            proxy_headers=False, forwarded_allow_ips="", access_log=False, log_level="warning",
            ssl_certfile=str(cert), ssl_keyfile=str(key), ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


@pytest.mark.parametrize("stage,code", [("tls", "tls_pair"), ("input", "credential_length"),
                                       ("db", "synthetic_database"), ("server", "server_startup")])
def test_cli_diagnostics_do_not_disclose_secrets(certificate, monkeypatch, capsys, stage, code):
    cert, key = certificate
    monkeypatch.setenv("OMACHI_LAB_STAFF_TOKEN", "short" if stage == "input" else TOKEN)
    monkeypatch.setattr(lab, "check_loopback_port", lambda port: None)

    def fail(*args, **kwargs):
        if stage == "tls":
            raise lab.LabConfigurationError("tls_pair")
        raise OSError("sensitive-exception")

    if stage in ("tls", "db"):
        monkeypatch.setattr(lab, {"tls": "validate_tls", "db": "synthetic_app"}[stage], fail)
    elif stage == "server":
        monkeypatch.setattr(lab.uvicorn, "run", fail)
    with pytest.raises(SystemExit) as failure:
        lab.main(["--cert", str(cert), "--key", str(key)])
    assert failure.value.code == 2
    output = capsys.readouterr()
    assert f'[{code}]' in output.err
    assert all(secret not in output.out + output.err for secret in (TOKEN, str(key), "sensitive-exception", "Traceback"))


def test_real_verified_tls_call_cookie_csrf_replay_and_other_action_denial(certificate):
    cert, key = certificate
    tls = ssl.create_default_context(cafile=str(cert))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        origin = f'https://staff.localhost:{port}'

        def request(method, path, headers=None, data=None):
            body = json.dumps(data).encode() if data is not None else b''
            values = {"Host": f'staff.localhost:{port}', "Connection": "close", "Content-Length": str(len(body)),
                      "Content-Type": "application/json", **(headers or {})}
            with socket.create_connection(("127.0.0.1", port), timeout=3) as plain:
                with tls.wrap_socket(plain, server_hostname="staff.localhost") as stream:
                    message = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in values.items()) + '\r\n'
                    stream.sendall(message.encode() + body)
                    response = HTTPResponse(stream)
                    response.begin()
                    return response.status, dict(response.getheaders()), json.loads(response.read())

        with lab.synthetic_app(TOKEN, port) as wrapper:
            server = lab.uvicorn.Server(lab.uvicorn.Config(wrapper, host="127.0.0.1", port=port,
                workers=1, proxy_headers=False, forwarded_allow_ips="", access_log=False, log_level="error",
                ssl_certfile=str(cert), ssl_keyfile=str(key), ssl_keyfile_password="", ssl_version=ssl.PROTOCOL_TLS_SERVER))
            thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
            thread.start()
            try:
                deadline = monotonic() + 10
                while not server.started and thread.is_alive() and monotonic() < deadline:
                    sleep(0.01)
                assert server.started
                status, response_headers, session = request('POST', '/staff/session', {"Origin": origin, "X-Staff-Token": TOKEN})
                assert status == 200
                cookie = SimpleCookie(response_headers['set-cookie'])[COOKIE_NAME]
                assert cookie['secure'] and cookie['httponly'] and cookie['samesite'] == 'strict'
                auth = {"Cookie": cookie.OutputString(attrs=[]), "Origin": origin, "X-CSRF-Token": session['csrf_token']}
                status, _, rows = request('GET', '/staff/receptions', auth)
                assert status == 200
                row = next(r for r in rows['receptions'] if r['status'] == 'waiting')
                _, _, context = request('GET', f'/staff/receptions/{row["id"]}/operation-context', auth)
                command = dict(operation_id=str(uuid4()), action='call', expected_revision=row['revision'], write_epoch=context['metadata']['write_epoch'])
                bad = {k: v for k, v in auth.items() if k != 'X-CSRF-Token'}
                assert request('POST', endpoint(row), bad, command)[0] == 403
                status, _, result = request('POST', endpoint(row), auth, command)
                assert status == 200 and result['outcome'] == 'state_changed'
                assert request('POST', endpoint(row), auth, command)[2] == result
                assert request('GET', '/staff/operations/' + command['operation_id'], auth)[2] == result
                assert request('POST', endpoint(row), auth, {**command, 'action': 'seat'})[0] == 403
                assert request('DELETE', '/staff/session', auth)[0] == 200
                assert request('GET', '/staff/receptions', auth)[0] == 401
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
