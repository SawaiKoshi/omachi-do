"""Staff pages use existing sessions; every database is disposable synthetic data."""

from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

from prediction_api import create_app
from reception_migration.migration import migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from staff_sessions import COOKIE_NAME, IDLE_SECONDS


ORIGIN = "https://staff.localhost"
CUSTOMER = "http://127.0.0.1"
TOKEN = "synthetic-ui-shared-token-not-for-real-use-1234567890"
STAFF_ASSET_PATHS = (
    "/staff/assets/staff.mjs", "/staff/assets/staff-call.mjs",
    "/staff/assets/staff-call-journal.mjs", "/staff/assets/staff.css",
)
UI_PATHS = ("/staff/login", "/staff/", *STAFF_ASSET_PATHS)


@contextmanager
def ui_environment(*, origin=ORIGIN, token=TOKEN, version=2):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        app = create_app(
            db_path=database, staff_token=token, staff_browser_origin=origin,
            model_path=database.parent / "missing-synthetic-ui-model.joblib",
            clock=lambda: datetime(2026, 11, 1, 18, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            yield database, app, client


def login(client):
    response = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("path", UI_PATHS)
@pytest.mark.parametrize("host", [CUSTOMER, "https://localhost", "http://staff.localhost",
                                 "https://staff.localhost:8443"])
def test_ui_is_not_served_on_customer_insecure_or_wrong_port_origins(path, host):
    with ui_environment() as (database, _, client):
        login(client)
        cookie = client.cookies.get(COOKIE_NAME)
        before = database.read_bytes()
        response = client.get(host + path, follow_redirects=False, headers={
            "X-Staff-Token": TOKEN, "Cookie": f"{COOKIE_NAME}={cookie}",
        })
        assert response.status_code == 403
        assert "text/html" not in response.headers.get("content-type", "")
        assert TOKEN not in response.text
        assert database.read_bytes() == before


@pytest.mark.parametrize("path", UI_PATHS)
def test_ui_requires_enabled_origin_and_configured_credential(path, monkeypatch):
    monkeypatch.delenv("OMACHI_STAFF_TOKEN", raising=False)
    for options in ({"origin": None}, {"token": None}):
        with ui_environment(**options) as (database, _, client):
            before = database.read_bytes()
            response = client.get(path, follow_redirects=False)
            assert response.status_code in (403, 503)
            assert "text/html" not in response.headers.get("content-type", "")
            assert database.read_bytes() == before


def test_dashboard_requires_cookie_even_with_correct_native_header():
    with ui_environment() as (database, _, client):
        before = database.read_bytes()
        for headers in ({}, {"X-Staff-Token": TOKEN},
                        {"X-Staff-Token": TOKEN, "Cookie": f"{COOKIE_NAME}={'a' * 43}"}):
            response = client.get("/staff/", headers=headers, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/staff/login"
            assert response.text == ""
        assert client.get("/staff/receptions").status_code == 401
        assert database.read_bytes() == before


@pytest.mark.parametrize("path", UI_PATHS)
def test_staff_pages_and_assets_reject_cross_origin_proxy_headers_and_lan(path):
    with ui_environment() as (_, app, client):
        for headers in ({"Origin": "https://customer.example"}, {"Origin": "null"},
                        {"X-Forwarded-Proto": "https"}, {"Forwarded": "proto=https"},
                        {"X-Forwarded-Host": "staff.localhost"}, {"X-Forwarded-For": "127.0.0.1"}):
            assert client.get(path, headers=headers, follow_redirects=False).status_code == 403
        assert client.get("https://attacker.example" + path).status_code == 400
        with TestClient(app, base_url=ORIGIN, client=("192.168.1.3", 50001)) as lan:
            assert lan.get(path, follow_redirects=False).status_code == 403


def test_public_login_assets_and_authenticated_dashboard_have_restricted_policies():
    with ui_environment() as (database, _, client):
        before = database.read_bytes()
        for path in ("/staff/login", *STAFF_ASSET_PATHS):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "no-referrer"
            assert "set-cookie" not in response.headers
            assert TOKEN not in response.text
        auth = login(client)
        response = client.get("/staff/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        for page in (response, client.get("/staff/login")):
            policy = page.headers["content-security-policy"]
            for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'",
                              "connect-src 'self'", "object-src 'none'", "base-uri 'none'",
                              "form-action 'none'", "frame-ancestors 'none'"):
                assert directive in policy
            assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
            assert TOKEN not in page.text and auth["csrf_token"] not in page.text
            assert client.cookies.get(COOKIE_NAME) not in page.text
        for path in ("/staff/session", "/staff/receptions"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
        assert database.read_bytes() == before


def test_page_login_logout_and_idle_expiry_use_existing_secure_cookie_and_csrf():
    with ui_environment() as (database, app, client):
        now = [0.0]
        app.state.staff_sessions.clock = lambda: now[0]
        before = database.read_bytes()
        response = client.post("/staff/session", headers={"Origin": ORIGIN, "X-Staff-Token": TOKEN})
        assert response.status_code == 200
        for attribute in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            assert attribute in response.headers["set-cookie"]
        csrf = response.json()["csrf_token"]
        assert client.get("/staff/").status_code == 200
        assert client.delete("/staff/session", headers={"Origin": ORIGIN}).status_code == 403
        assert client.get("/staff/").status_code == 200
        response = client.delete("/staff/session", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        assert response.status_code == 200 and response.json() == {"authenticated": False}
        assert client.get("/staff/", follow_redirects=False).status_code == 303
        login(client)
        now[0] = IDLE_SECONDS
        assert client.get("/staff/", follow_redirects=False).status_code == 303
        assert client.get("/staff/receptions").status_code == 401
        assert database.read_bytes() == before


@pytest.mark.parametrize("version", [1, 2])
def test_read_only_ui_works_without_migration_or_mode_change_and_embeds_no_data(version):
    with ui_environment(version=version) as (database, _, client):
        registration = client.post(CUSTOMER + "/receptions", json={"adults": 2, "children": 1},
                                   headers={"Idempotency-Key": str(uuid4())})
        assert registration.status_code == 201
        receipt = registration.json()
        before = database.read_bytes()
        login(client)
        page = client.get("/staff/")
        assert page.status_code == 200
        assert receipt["id"] not in page.text  # data only arrives through authenticated GETs
        listing = client.get("/staff/receptions")
        assert listing.status_code == 200
        assert listing.json()["receptions"][0]["id"] == receipt["id"]
        assert listing.json()["metadata"]["write_enabled"] is False
        detail = client.get(f'/staff/receptions/{receipt["id"]}/operation-context')
        assert detail.status_code == 200
        assert detail.json()["reception"]["allowed_actions"] == []
        assert client.get(CUSTOMER + "/public/queue").status_code == 200
        assert database.read_bytes() == before


def test_staff_static_allowlist_does_not_expose_other_repository_files():
    with ui_environment() as (_, _, client):
        for path in ("/staff/assets/login.html", "/staff/assets/index.html", "/staff/assets/prediction_api.py",
                     "/staff/assets/unknown.js", "/staff/assets/staff_sessions.py"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"


@pytest.mark.parametrize("call_enabled", [False, True])
def test_new_call_assets_keep_lab_origin_gate_and_server_owned_capability(call_enabled):
    from staff_call_https_lab import synthetic_app as call_app
    from staff_https_lab import synthetic_app as readonly_app

    port = 8444 if call_enabled else 8443
    origin = f"https://staff.localhost:{port}"
    factory = call_app if call_enabled else readonly_app
    with factory(TOKEN, port) as wrapper:
        with TestClient(wrapper, base_url=origin, client=("127.0.0.1", 50000)) as client:
            database = wrapper.app.state.store.db_path
            before = database.read_bytes()
            for path in STAFF_ASSET_PATHS:
                # Client hints cannot manufacture an environment capability.
                response = client.get(path, headers={"X-Omachi-Preview-Actions": "call"})
                assert response.status_code == 200
                assert response.headers["x-omachi-preview"] == "synthetic"
                assert response.headers.get("x-omachi-preview-actions") == ("call" if call_enabled else None)
                assert response.headers["cache-control"] == "no-store"
                assert "set-cookie" not in response.headers
                assert TOKEN not in response.text
                for host in ("https://localhost", "http://staff.localhost", "https://staff.localhost:9443"):
                    rejected = client.get(host + path, follow_redirects=False)
                    assert rejected.status_code == 403
                    assert TOKEN not in rejected.text
            response = client.post("/staff/session", headers={"Origin": origin, "X-Staff-Token": TOKEN})
            assert response.status_code == 200
            csrf = response.json()["csrf_token"]
            listing = client.get("/staff/receptions")
            assert listing.headers.get("x-omachi-preview-actions") == ("call" if call_enabled else None)
            row = listing.json()["receptions"][0]
            metadata = listing.json()["metadata"]
            assert metadata["write_enabled"] is call_enabled
            command = dict(operation_id=str(uuid4()), action="seat", expected_revision=row["revision"],
                           write_epoch=metadata["write_epoch"])
            headers = {"Origin": origin, "X-CSRF-Token": csrf, "X-Omachi-Preview-Actions": "call"}
            # Reading the new JS never grants a broader write capability.
            assert client.post(f'/staff/receptions/{row["id"]}/operations', json=command,
                               headers=headers).status_code == 403
            if not call_enabled:
                assert client.post(f'/staff/receptions/{row["id"]}/operations',
                                   json={**command, "action": "call"}, headers=headers).status_code == 403
            assert database.read_bytes() == before
