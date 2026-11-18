"""Browser authentication and read APIs: disposable synthetic databases only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha256
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import StateConflict
from staff_sessions import (
    ABSOLUTE_SECONDS, COOKIE_NAME, IDLE_SECONDS, SHARED_PRINCIPAL, SessionCapacity,
    SessionRejected, StaffSessions, browser_origin,
)


TOKEN = "synthetic-browser-staff-token-not-persisted-1234567890"
ORIGIN = "https://staff.localhost"
CUSTOMER = "http://127.0.0.1"
STAFF = {"X-Staff-Token": TOKEN}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 18, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


@contextmanager
def environment(*, version=2, mode="revision_required", enabled=0, browser_origin=ORIGIN, token=TOKEN):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode=?,seat_corrections_enabled=?",
                             (mode, enabled))
        clock = Clock()
        app = create_app(db_path=database, clock=clock, staff_token=token,
                         model_path=database.parent / "missing-synthetic-model.joblib",
                         staff_browser_origin=browser_origin)
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            yield lab, database, app, clock, client


def register(client):
    response = client.post(CUSTOMER + "/receptions", json={"adults": 2, "children": 1},
                           headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()


def login(client):
    response = client.post("/staff/session", headers={**STAFF, "Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response


def context(client, receipt):
    response = client.get(f'/staff/receptions/{receipt["id"]}/operation-context')
    assert response.status_code == 200, response.text
    return response.json()


def command_body(client, receipt, action):
    state = context(client, receipt)
    return dict(operation_id=str(uuid4()), action=action, expected_revision=state["reception"]["revision"],
                write_epoch=state["metadata"]["write_epoch"])


def csrf_headers(client):
    response = client.get("/staff/session")
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def act(client, receipt, action):
    body = command_body(client, receipt, action)
    response = client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=csrf_headers(client))
    assert response.status_code == 200, response.text
    return response, body


@pytest.mark.parametrize("value", ["http://staff.localhost", "https://localhost", "https://127.0.0.1",
                                   "https://shop.example", "https://staff.localhost/", "https://staff.localhost/x",
                                   "https://staff.localhost?x=1", "https://staff.localhost#x",
                                   "https://a@staff.localhost", "https://staff.localhost:bad",
                                   "https://staff.localhost.evil", "https://STAFF.localhost"])
def test_cookie_origin_rejects_customer_host_insecure_and_ambiguous_origins(value):
    with pytest.raises(ValueError):
        browser_origin(value)


@pytest.mark.parametrize("value", [None, ORIGIN, ORIGIN + ":8443"])
def test_cookie_origin_is_opt_in_and_separate_https_only(value):
    assert browser_origin(value) == value


def test_sessions_are_opaque_server_side_rotated_revocable_and_bounded():
    now = [0.0]
    sessions = StaffSessions(clock=lambda: now[0], capacity=1)
    first, csrf = sessions.create()
    assert len(first) == len(csrf) == 43 and first != csrf
    assert first not in sessions._sessions  # only hashed keys on the server
    assert sessions.authenticate(first)["principal"] == SHARED_PRINCIPAL
    with pytest.raises(SessionCapacity):
        sessions.create()
    second, second_csrf = sessions.create(first)
    assert second != first and second_csrf != csrf
    with pytest.raises(SessionRejected):
        sessions.authenticate(first)
    with pytest.raises(SessionRejected):
        sessions.authenticate(second, csrf=csrf, require_csrf=True)
    sessions.authenticate(second, csrf=second_csrf, require_csrf=True)
    sessions.revoke(second)
    with pytest.raises(SessionRejected):
        sessions.authenticate(second)


def test_idle_and_absolute_expiry_do_not_extend_with_polling():
    now = [0.0]
    sessions = StaffSessions(clock=lambda: now[0])
    first, _ = sessions.create()
    now[0] = IDLE_SECONDS
    with pytest.raises(SessionRejected):
        sessions.authenticate(first)
    second, _ = sessions.create()
    for _ in range(4):
        now[0] += IDLE_SECONDS - 1
        sessions.authenticate(second)
    now[0] = IDLE_SECONDS + ABSOLUTE_SECONDS
    with pytest.raises(SessionRejected):
        sessions.authenticate(second)


def test_login_attempt_budget_and_concurrent_capacity_are_bounded():
    now = [0.0]
    sessions = StaffSessions(clock=lambda: now[0], capacity=2)
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(lambda _: sessions.allow_login(), range(12))) == 10
    now[0] = 60
    assert sessions.allow_login()

    def attempt(_):
        try:
            sessions.create()
            return True
        except SessionCapacity:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(attempt, range(8))) == 2


def test_login_cookie_flags_no_secret_echo_rotation_logout_and_read_only_db(caplog):
    with environment() as (_, database, app, _, client):
        before = database.read_bytes()
        response = login(client)
        cookie_header = response.headers["set-cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=/", "Max-Age=3600"):
            assert flag in cookie_header
        assert "Domain=" not in cookie_header
        first = client.cookies.get(COOKIE_NAME)
        assert TOKEN not in response.text and first not in response.text
        assert response.json()["principal"] == SHARED_PRINCIPAL
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/staff/session").json()["csrf_token"] == response.json()["csrf_token"]
        rotated = login(client)
        second = client.cookies.get(COOKIE_NAME)
        assert second != first
        assert rotated.json()["csrf_token"] != response.json()["csrf_token"]
        with pytest.raises(SessionRejected):
            app.state.staff_sessions.authenticate(first)
        assert client.delete("/staff/session", headers=csrf_headers(client)).json() == {"authenticated": False}
        assert client.get("/staff/session").status_code == 401
        assert database.read_bytes() == before
        assert TOKEN not in caplog.text and first not in caplog.text and second not in caplog.text


@pytest.mark.parametrize("headers,status", [({}, 403), ({"Origin": "null", **STAFF}, 403),
    ({"Origin": "https://evil.example", **STAFF}, 403), ({"Origin": ORIGIN}, 401),
    ({"Origin": ORIGIN, "X-Staff-Token": "wrong"}, 401)])
def test_login_requires_header_credential_and_exact_origin(headers, status):
    with environment() as (_, database, _, _, client):
        before = database.read_bytes()
        response = client.post("/staff/session", headers=headers)
        assert response.status_code == status
        assert "set-cookie" not in response.headers
        assert database.read_bytes() == before


def test_login_rate_limit_unknown_cookie_no_header_fallback_and_duplicate_cookies():
    with environment() as (_, database, _, _, client):
        for _ in range(10):
            assert client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": "wrong"}).status_code == 401
        assert client.post("/staff/session", headers={"Origin": ORIGIN, **STAFF}).status_code == 429
        before = database.read_bytes()
        for cookie in ("a" * 43, "broken", f'{"a" * 43}; {COOKIE_NAME}={"b" * 43}'):
            response = client.get("/staff/receptions", headers={**STAFF, "Cookie": COOKIE_NAME + "=" + cookie})
            assert response.status_code == 401
        assert database.read_bytes() == before


@pytest.mark.parametrize("kwargs", [{"browser_origin": None}, {"token": None}])
def test_browser_sessions_disabled_without_explicit_origin_or_credential(kwargs, monkeypatch):
    monkeypatch.delenv("OMACHI_STAFF_TOKEN", raising=False)
    with environment(**kwargs) as (_, database, _, _, client):
        before = database.read_bytes()
        assert client.post("/staff/session", headers={"Origin": ORIGIN, **STAFF}).status_code in (403, 503)
        assert database.read_bytes() == before


@pytest.mark.parametrize("path", ["/", "/assets/reception.mjs", "/public/queue", "/predict", "/receptions", "/health"])
def test_customer_content_and_public_apis_not_served_on_staff_cookie_origin(path):
    with environment() as (_, _, _, _, client):
        login(client)
        assert client.get(path).status_code == 403


@pytest.mark.parametrize("base", ["http://staff.localhost", "https://staff.localhost:8443", "https://localhost", CUSTOMER])
def test_copied_cookie_rejected_on_other_scheme_port_and_customer_origins(base):
    with environment() as (_, database, _, _, client):
        login(client)
        cookie = client.cookies.get(COOKIE_NAME)
        before = database.read_bytes()
        response = client.get(base + "/staff/receptions", headers={**STAFF, "Cookie": COOKIE_NAME + "=" + cookie})
        assert response.status_code in (401, 403)
        assert database.read_bytes() == before


def test_lan_forwarded_scheme_and_host_cannot_enable_browser_authentication():
    with environment() as (_, _, app, _, client):
        assert client.post("http://staff.localhost/staff/session", headers={"Origin": ORIGIN,
                           "X-Forwarded-Proto": "https", **STAFF}).status_code == 403
        assert client.get("https://evil.example/staff/receptions", headers=STAFF).status_code == 400
        with TestClient(app, base_url=ORIGIN, client=("192.168.1.7", 50001)) as lan:
            assert lan.post("/staff/session", headers={"Origin": ORIGIN, **STAFF}).status_code == 403


@pytest.mark.parametrize("headers", [{}, {"Origin": ORIGIN}, {"X-CSRF-Token": "wrong"},
    {"Origin": ORIGIN, "X-CSRF-Token": "wrong"}, {"Origin": "https://evil.example", "X-CSRF-Token": "wrong"}])
def test_cookie_mutations_require_csrf_and_origin_before_any_db_write(headers):
    with environment() as (_, database, _, _, client):
        receipt = register(client)
        login(client)
        body = command_body(client, receipt, "seat")
        before = database.read_bytes()
        result = client.post(f'/staff/receptions/{receipt["id"]}/operations', headers=headers, json=body)
        assert result.status_code in (401, 403)
        assert client.delete("/staff/session", headers=headers).status_code in (401, 403)
        assert database.read_bytes() == before


@pytest.mark.parametrize("version,mode", [(1, "legacy"), (2, "legacy"), (2, "revision_required")])
def test_read_context_v1_v2_legacy_and_required_modes_without_mutation(version, mode):
    with environment(version=version, mode=mode) as (_, database, _, _, client):
        receipt = register(client)
        login(client)
        before = database.read_bytes()
        state = context(client, receipt)
        metadata, row = state["metadata"], state["reception"]
        assert metadata["schema_version"] == version and metadata["staff_write_mode"] == mode
        assert metadata["write_enabled"] == (mode == "revision_required")
        assert row["revision"] == (0 if version == 2 else None)
        assert row["id"] == receipt["id"] and row["display_number"] == "20261030-001"
        assert row["reception_order"] == receipt["reception_order"] and row["received_at"] == receipt["received_at"]
        assert row["adults"] == 2 and row["children"] == 1
        assert row["allowed_actions"] == (["call", "seat", "cancel"] if mode == "revision_required" else [])
        assert row["valid_seating_event"] is None
        listed = client.get("/staff/receptions").json()
        assert listed["receptions"] == [row] and listed["metadata"] == metadata
        if version == 2:
            assert metadata["write_epoch"] and metadata["database_id"]
        else:
            assert metadata["write_epoch"] is None and metadata["database_id"] is None
        assert database.read_bytes() == before
        if mode == "legacy":
            body = dict(operation_id=str(uuid4()), action="seat", expected_revision=0,
                        write_epoch=metadata["write_epoch"] or str(uuid4()))
            assert client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body,
                               headers=csrf_headers(client)).status_code == 409
            assert database.read_bytes() == before


def test_all_browser_operations_reuse_protected_store_and_old_routes_never_work():
    with environment() as (_, database, _, _, client):
        receipt = register(client)
        login(client)
        for revision, (action, status) in enumerate([
            ("call", "called"), ("absent", "absent"), ("return", "waiting"), ("seat", "seated")], 1):
            response, _ = act(client, receipt, action)
            assert response.json()["reception"]["status"] == status
            assert context(client, receipt)["reception"]["revision"] == revision
        other = register(client)
        act(client, other, "cancel")
        before = database.read_bytes()
        for action in ("call", "absent", "return", "seat", "cancel"):
            assert client.post(f'/receptions/{receipt["id"]}/{action}', headers=csrf_headers(client)).status_code == 403
            assert client.post(CUSTOMER + f'/receptions/{receipt["id"]}/{action}',
                               headers={**STAFF, "Cookie": COOKIE_NAME + "=" + client.cookies.get(COOKIE_NAME)}).status_code == 403
        assert database.read_bytes() == before


def test_lost_response_replay_lookup_stale_revision_and_snapshot_are_preserved():
    with environment() as (lab, database, _, clock, client):
        receipt = register(client)
        login(client)
        stale = command_body(client, receipt, "cancel")
        clock.value += timedelta(minutes=25)
        original, body = act(client, receipt, "seat")
        assert original.json()["reception"]["actual_wait_minutes"] == 25
        before = database.read_bytes()
        for _ in range(2):
            retry = client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=csrf_headers(client))
            assert retry.content == original.content
            result = client.get('/staff/operations/' + body["operation_id"])
            assert result.json() == original.json()
            assert database.read_bytes() == before
        conflict = client.post(f'/staff/receptions/{receipt["id"]}/operations', json=stale, headers=csrf_headers(client))
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "revision_conflict"
        assert context(client, receipt)["reception"]["revision"] == 1
        saved = client.get(f'/staff/receptions/{receipt["id"]}').json()["reception"]
        assert saved["snapshot"] == receipt["snapshot"] and saved["prediction"] == receipt["prediction"]
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events WHERE event_type='seated'").fetchone()[0] == 1
            assert conn.execute("SELECT DISTINCT principal_kind,principal_id FROM staff_operations").fetchall() == [
                ("shared_staff_token", "shared-staff")]


def test_browser_quality_writes_are_forbidden_even_with_valid_csrf():
    with environment() as (_, database, _, _, client):
        receipt = register(client)
        login(client)
        before = database.read_bytes()
        for endpoint, decision in (("quality-reviews", "clear"), ("quality-assessments", "eligible")):
            response = client.post(f'/staff/receptions/{receipt["id"]}/{endpoint}',
                                   json={"decision": decision}, headers=csrf_headers(client))
            assert response.status_code == 403
        assert client.get(f'/staff/receptions/{receipt["id"]}/data-quality').json()["training_use"] == "blocked"
        assert database.read_bytes() == before


def test_recent_seating_target_and_001_002_correction_flow_preserve_original_order():
    with environment(enabled=1) as (lab, database, _, clock, client):
        first = register(client)
        login(client)
        clock.value += timedelta(minutes=10)
        wrong_seating, old_request = act(client, first, "seat")
        clock.value += timedelta(seconds=30)
        second = register(client)
        recent = client.get("/staff/receptions?view=recently_seated").json()["receptions"]
        assert [row["id"] for row in recent] == [first["id"]]
        target = context(client, first)
        seat = target["reception"]["valid_seating_event"]
        assert seat["correctable"] and seat["from_status"] == "waiting"
        assert target["reception"]["allowed_actions"] == ["seating_correction"]
        clock.value += timedelta(seconds=30)
        body = dict(operation_id=str(uuid4()), expected_revision=target["reception"]["revision"],
                    write_epoch=target["metadata"]["write_epoch"], target_event_id=seat["event_id"],
                    reason_code="accidental_seating", reason_detail="合成試験で受付を確認")
        response = client.post(f'/staff/receptions/{first["id"]}/seating-corrections', json=body, headers=csrf_headers(client))
        assert response.status_code == 200
        assert client.get("/staff/receptions?view=recently_seated").json()["receptions"] == []
        active = client.get("/staff/receptions").json()["receptions"]
        assert [row["id"] for row in active] == [first["id"], second["id"]]
        assert context(client, first)["reception"]["valid_seating_event"] is None
        assert client.post(f'/staff/receptions/{first["id"]}/operations', json=old_request,
                           headers=csrf_headers(client)).json() == wrong_seating.json()
        assert context(client, first)["reception"]["status"] == "waiting"
        clock.value += timedelta(minutes=14)
        actual, _ = act(client, first, "seat")
        assert actual.json()["reception"]["actual_wait_minutes"] == 25
        new_target = context(client, first)["reception"]["valid_seating_event"]
        assert new_target["event_id"] > seat["event_id"]
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT issue_kind FROM data_quality_issues WHERE reception_id=? ORDER BY issue_id",
                                (second["id"],)).fetchall() == [("snapshot_context",), ("prediction_gate",)]
        for receipt in (first, second):
            saved = client.get(f'/staff/receptions/{receipt["id"]}').json()["reception"]
            assert saved["prediction"] == receipt["prediction"] and saved["snapshot"] == receipt["snapshot"]


def test_disabled_correction_exposes_target_but_does_not_offer_or_run_it():
    with environment() as (_, database, _, _, client):
        receipt = register(client)
        login(client)
        act(client, receipt, "seat")
        state = context(client, receipt)
        assert state["reception"]["allowed_actions"] == []
        seat = state["reception"]["valid_seating_event"]
        assert not seat["correctable"]
        before = database.read_bytes()
        result = client.post(f'/staff/receptions/{receipt["id"]}/seating-corrections', headers=csrf_headers(client),
                             json=dict(operation_id=str(uuid4()), expected_revision=1,
                                       write_epoch=state["metadata"]["write_epoch"], target_event_id=seat["event_id"],
                                       reason_code="other", reason_detail="synthetic"))
        assert result.status_code == 409 and database.read_bytes() == before


@pytest.mark.parametrize("view", ["active", "recently_seated"])
def test_keyset_pages_have_bounds_order_no_duplicates_and_no_database_writes(view):
    with environment() as (_, database, _, clock, client):
        login(client)
        receipts = []
        for _ in range(5):
            receipt = register(client)
            receipts.append(receipt)
            if view == "recently_seated":
                act(client, receipt, "seat")
        expected = receipts if view == "active" else list(reversed(receipts))
        before = database.read_bytes()
        pages, cursor, pinned = [], None, None
        while True:
            params = {"view": view, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/staff/receptions", params=params)
            assert response.status_code == 200
            page = response.json()
            pinned = page["as_of"] if pinned is None else pinned
            assert page["as_of"] == pinned
            pages.extend(page["receptions"])
            cursor = page["next_cursor"]
            clock.value += timedelta(seconds=1)
            if not page["has_more"]:
                assert cursor is None
                break
        assert [row["id"] for row in pages] == [row["id"] for row in expected]
        assert database.read_bytes() == before


@pytest.mark.parametrize("mutation", ["register", "call", "cancel", "mode"])
def test_paging_rejects_changed_reception_snapshot_or_configuration(mutation):
    with environment(mode="legacy" if mutation == "mode" else "revision_required") as (lab, database, _, _, client):
        login(client)
        first, _ = register(client), register(client)
        cursor = client.get("/staff/receptions?limit=1").json()["next_cursor"]
        if mutation == "register":
            register(client)
        elif mutation == "mode":
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required'")
        else:
            act(client, first, mutation)
        before = database.read_bytes()
        response = client.get("/staff/receptions", params={"limit": 1, "cursor": cursor})
        assert response.status_code == 409 and database.read_bytes() == before


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "hours=0", "hours=169", "limit=1.5",
                                  "view=all", "cursor=", "cursor=invalid", "cursor=" + "x" * 2049])
def test_list_bounds_and_malformed_cursor_refused(query):
    with environment() as (_, database, _, _, client):
        login(client)
        before = database.read_bytes()
        assert client.get("/staff/receptions?" + query).status_code == 422
        assert database.read_bytes() == before


def test_cursor_expiry_parameter_tampering_and_every_page_requires_auth():
    with environment() as (_, _, app, _, client):
        login(client)
        register(client)
        register(client)
        now = [0.0]
        app.state.staff_reads._cursor_clock = lambda: now[0]
        cursor = client.get("/staff/receptions?limit=1").json()["next_cursor"]
        assert client.get("/staff/receptions", params={"limit": 2, "cursor": cursor}).status_code == 422
        assert client.get("/staff/receptions", params={"limit": 1, "cursor": "x" + cursor[1:]}).status_code == 422
        now[0] = 300
        assert client.get("/staff/receptions", params={"limit": 1, "cursor": cursor}).status_code == 422
        client.cookies.clear()
        assert client.get("/staff/receptions", params={"limit": 1, "cursor": cursor}).status_code == 401


def test_recent_window_uses_seating_time_across_midnight_not_intake_date():
    with environment() as (_, _, _, clock, client):
        login(client)
        early = register(client)
        clock.value += timedelta(days=2, hours=6)
        act(client, early, "seat")
        same_time = register(client)
        act(client, same_time, "seat")
        recent = client.get("/staff/receptions?view=recently_seated&hours=1").json()["receptions"]
        assert [row["id"] for row in recent] == [same_time["id"], early["id"]]
        clock.value += timedelta(hours=1, seconds=1)
        assert client.get("/staff/receptions?view=recently_seated&hours=1").json()["receptions"] == []


def test_auth_expiry_restart_and_fault_rollback_do_not_repeat_or_partially_commit(monkeypatch):
    with environment() as (_, database, app, _, client):
        receipt = register(client)
        login(client)
        body = command_body(client, receipt, "seat")
        headers = csrf_headers(client)
        original_save = app.state.store._save_staff_operation

        def fail(*args):
            raise sqlite3.OperationalError("synthetic interruption")

        monkeypatch.setattr(app.state.store, "_save_staff_operation", fail)
        before = database.read_bytes()
        assert client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=headers).status_code == 503
        assert database.read_bytes() == before
        monkeypatch.setattr(app.state.store, "_save_staff_operation", original_save)
        assert client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=headers).status_code == 200
        app.state.staff_sessions.clear()  # process restart invalidates all cookies
        before = database.read_bytes()
        assert client.get("/staff/session").status_code == 401
        assert client.get('/staff/operations/' + body["operation_id"], headers=STAFF).status_code == 401
        assert client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body, headers=headers).status_code == 401
        login(client)
        assert client.get('/staff/operations/' + body["operation_id"]).status_code == 200
        assert database.read_bytes() == before


def test_concurrent_browser_operations_commit_only_one_expected_revision():
    with environment() as (lab, database, _, _, client):
        receipt = register(client)
        login(client)
        bodies = [command_body(client, receipt, action) for action in ("seat", "cancel")]
        headers = {**csrf_headers(client), "Cookie": COOKIE_NAME + "=" + client.cookies.get(COOKIE_NAME)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda body: client.post(f'/staff/receptions/{receipt["id"]}/operations',
                                                           json=body, headers=headers), bodies))
        assert sorted(result.status_code for result in results) == [200, 409]
        assert context(client, receipt)["reception"]["revision"] == 1
        with lab.connection(database, readonly=True) as conn:
            assert conn.execute("SELECT COUNT(*) FROM reception_events").fetchone()[0] == 2


def test_atomic_browser_mode_check_at_store_boundary_rejects_legacy():
    with environment(mode="legacy") as (_, database, app, _, client):
        receipt = register(client)
        state = app.state.store.get_staff_reception(receipt["id"])
        before = database.read_bytes()
        with pytest.raises(StateConflict):
            app.state.store.staff_transition(receipt["id"], "seat", operation_id=str(uuid4()),
                expected_revision=0, write_epoch=state["write_epoch"], **SHARED_PRINCIPAL, require_revision_mode=True)
        assert database.read_bytes() == before


def test_public_api_and_customer_assets_expose_no_staff_internal_information():
    with environment() as (_, _, _, _, client):
        receipt = register(client)
        login(client)
        forbidden = (TOKEN, COOKIE_NAME, "write_epoch", "database_id", "revision", "valid_seating_event",
                     "csrf_token", "staff_operations", "quality_assessment")
        for path in ("/", "/assets/reception.mjs", "/public/queue"):
            response = client.get(CUSTOMER + path)
            assert response.status_code == 200
            for name in forbidden:
                assert name not in response.text
        for name in forbidden:
            assert name not in str(receipt)


def test_header_staff_compatibility_without_browser_feature_on_v1_and_v2():
    for version in (1, 2):
        with environment(version=version, mode="legacy", browser_origin=None) as (_, database, _, _, client):
            receipt = register(client)
            read = client.get(CUSTOMER + "/staff/receptions", headers=STAFF)
            assert read.status_code == 200 and not read.json()["metadata"]["write_enabled"]
            result = client.post(CUSTOMER + f'/receptions/{receipt["id"]}/seat', headers=STAFF)
            assert result.status_code == 200 and result.json()["status"] == "seated"
            before = database.read_bytes()
            recent = client.get(CUSTOMER + "/staff/receptions?view=recently_seated", headers=STAFF)
            assert recent.status_code == 200 and recent.json()["receptions"][0]["valid_seating_event"]
            assert database.read_bytes() == before


def test_read_transaction_keeps_row_event_and_revision_consistent_during_concurrent_write(monkeypatch):
    with environment() as (lab, database, app, _, client):
        receipt = register(client)
        login(client)
        initial = context(client, receipt)
        # WAL permits a writer to commit while this read snapshot stays open.
        with lab.connection(database) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        original = app.state.staff_reads._row
        written = []

        def concurrent_write(conn, row, metadata):
            if not written:
                written.append(app.state.store.staff_transition(receipt["id"], "seat", operation_id=str(uuid4()),
                    expected_revision=0, write_epoch=initial["metadata"]["write_epoch"], **SHARED_PRINCIPAL))
            return original(conn, row, metadata)

        monkeypatch.setattr(app.state.staff_reads, "_row", concurrent_write)
        during = context(client, receipt)
        assert during["reception"]["status"] == "waiting" and during["reception"]["revision"] == 0
        assert during["reception"]["valid_seating_event"] is None
        after = context(client, receipt)
        assert after["reception"]["status"] == "seated" and after["reception"]["revision"] == 1
        assert after["metadata"]["view_version"] != during["metadata"]["view_version"]


def test_http_session_idle_expiry_fails_closed_without_using_header_fallback():
    with environment() as (_, database, app, _, client):
        now = [0.0]
        app.state.staff_sessions.clock = lambda: now[0]
        login(client)
        before = database.read_bytes()
        now[0] = IDLE_SECONDS
        assert client.get("/staff/receptions", headers=STAFF).status_code == 401
        assert client.get("/staff/session").status_code == 401
        assert database.read_bytes() == before


def test_duplicate_origin_csrf_and_credential_headers_are_refused():
    with environment() as (_, database, _, _, client):
        before = database.read_bytes()
        assert client.post("/staff/session", headers=[("Origin", ORIGIN), ("Origin", ORIGIN),
                                                    ("X-Staff-Token", TOKEN)]).status_code == 403
        assert client.post("/staff/session", headers=[("Origin", ORIGIN), ("X-Staff-Token", TOKEN),
                                                    ("X-Staff-Token", TOKEN)]).status_code == 401
        login(client)
        csrf = csrf_headers(client)["X-CSRF-Token"]
        assert client.delete("/staff/session", headers=[("Origin", ORIGIN), ("X-CSRF-Token", csrf),
                                                      ("X-CSRF-Token", csrf)]).status_code == 403
        assert database.read_bytes() == before


def test_no_cross_origin_credentialed_cors_or_proxy_trust_is_added():
    with environment() as (_, database, _, _, client):
        before = database.read_bytes()
        response = client.options("/staff/session", headers={"Origin": "https://evil.example",
                    "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "X-Staff-Token"})
        assert response.status_code == 403 and "access-control-allow-origin" not in response.headers
        for name in ("Forwarded", "X-Forwarded-Proto", "X-Forwarded-Host", "X-Forwarded-For"):
            assert client.post("/staff/session", headers={"Origin": ORIGIN, **STAFF, name: "https"}).status_code == 403
        assert database.read_bytes() == before


def test_unknown_schema_fails_read_and_browser_write_without_auto_repair():
    with environment() as (lab, database, _, _, client):
        receipt = register(client)
        login(client)
        body = command_body(client, receipt, "seat")
        with lab.connection(database) as conn:
            conn.execute("PRAGMA user_version=99")
        before = database.read_bytes()
        assert client.get("/staff/receptions").status_code == 422
        assert client.get(f'/staff/receptions/{receipt["id"]}/operation-context').status_code == 422
        assert client.post(f'/staff/receptions/{receipt["id"]}/operations', json=body,
                           headers=csrf_headers(client)).status_code == 422
        assert database.read_bytes() == before


def test_customer_html_cannot_bootstrap_cookie_session_or_read_staff_with_no_credential():
    with environment() as (_, database, _, _, client):
        before = database.read_bytes()
        for path in ("/staff/receptions", f"/staff/receptions/{uuid4()}/operation-context"):
            assert client.get(CUSTOMER + path).status_code == 401
        assert client.post(CUSTOMER + "/staff/session", headers={"Origin": CUSTOMER, **STAFF}).status_code == 403
        assert database.read_bytes() == before


def test_authenticated_missing_target_keeps_existing_archived_rejection_and_replay_protocol():
    with environment() as (_, database, _, _, client):
        login(client)
        metadata = client.get("/staff/receptions").json()["metadata"]
        missing = str(uuid4())
        body = dict(operation_id=str(uuid4()), action="seat", expected_revision=0,
                    write_epoch=metadata["write_epoch"])
        path = f"/staff/receptions/{missing}/operations"
        first = client.post(path, json=body, headers=csrf_headers(client))
        assert first.status_code == 404 and first.json()["error"]["code"] == "reception_not_found"
        before = database.read_bytes()
        assert client.post(path, json=body, headers=csrf_headers(client)).content == first.content
        assert client.get('/staff/operations/' + body["operation_id"]).json() == first.json()
        assert database.read_bytes() == before


def test_authenticated_invalid_target_format_still_returns_validation_error_without_writing():
    with environment() as (_, database, _, _, client):
        login(client)
        metadata = client.get("/staff/receptions").json()["metadata"]
        body = dict(operation_id=str(uuid4()), action="seat", expected_revision=0,
                    write_epoch=metadata["write_epoch"])
        before = database.read_bytes()
        assert client.post('/staff/receptions/not-a-uuid/operations', json=body,
                           headers=csrf_headers(client)).status_code == 422
        assert database.read_bytes() == before
