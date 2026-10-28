"""Customer queue reads publish only the minimal, current reception information."""

from datetime import datetime, timedelta
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from prediction_api import PublicQueueResponse, create_app
from prediction_service import QUEUE_ORDER_NOTICE
from reception_store import StoreBusy


ORIGIN = "http://127.0.0.1"
STAFF_TOKEN = "public-queue-private-staff-token-1234567890"
STAFF = {"X-Staff-Token": STAFF_TOKEN}
JST = ZoneInfo("Asia/Tokyo")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 24, 23, 59, tzinfo=JST)

    def __call__(self):
        return self.value


@pytest.fixture
def public_api(tmp_path, monkeypatch):
    def no_excel(*args, **kwargs):
        pytest.fail("Public queue reads must not read Excel")

    monkeypatch.setattr(pd, "read_excel", no_excel)
    clock = Clock()
    database = tmp_path / "public-queue.sqlite3"
    app = create_app(db_path=database, model_path=tmp_path / "missing.joblib",
                     staff_token=STAFF_TOKEN, clock=clock)
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        yield client, app, database, clock


def register(client, adults=2, children=0):
    response = client.post("/receptions", json={"adults": adults, "children": children},
                           headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 201
    return response.json()


def public_group(receipt, status="waiting"):
    return {"display_number": receipt["display_number"],
            "total_people": receipt["total_people"], "status": status}


def assert_public_shape(payload):
    # Exact keys at every object level prevent accidental nested metadata disclosure.
    assert set(payload) == {"waiting_groups", "groups", "notice"}
    assert type(payload["waiting_groups"]) is int
    assert payload["waiting_groups"] == len(payload["groups"])
    assert payload["notice"] == QUEUE_ORDER_NOTICE
    for group in payload["groups"]:
        assert set(group) == {"display_number", "total_people", "status"}
        assert isinstance(group["display_number"], str)
        assert type(group["total_people"]) is int
        assert group["status"] in {"waiting", "called", "absent"}


def test_public_queue_is_available_without_staff_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("OMACHI_STAFF_TOKEN", raising=False)
    app = create_app(db_path=tmp_path / "no-staff.sqlite3", clock=Clock())
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
        response = client.get("/public/queue")
        assert response.status_code == 200
        assert response.json() == {"waiting_groups": 0, "groups": [], "notice": QUEUE_ORDER_NOTICE}
        assert client.get("/queue").status_code == 503


def test_public_queue_only_publishes_minimal_fields_in_cross_day_reception_order(public_api):
    client, _, _, clock = public_api
    first = register(client, 4, 1)
    second = register(client, 1, 0)
    clock.value += timedelta(minutes=2)
    third = register(client, 2, 1)
    assert first["display_number"] == "20261024-001"
    assert third["display_number"] == "20261025-001"
    assert client.post(f'/receptions/{first["id"]}/call', headers=STAFF).status_code == 200
    assert client.post(f'/receptions/{first["id"]}/absent', headers=STAFF).status_code == 200
    assert client.post(f'/receptions/{third["id"]}/call', headers=STAFF).status_code == 200
    response = client.get("/public/queue")
    assert response.status_code == 200
    payload = response.json()
    assert_public_shape(payload)
    assert payload["groups"] == [public_group(first, "absent"), public_group(second),
                                 public_group(third, "called")]
    assert payload["waiting_groups"] == 3
    for private in (first["id"], second["id"], third["id"], STAFF_TOKEN,
                    first["received_at"], "reception_order", "snapshot", "prediction",
                    "adults", "children", "called_at", "source", "recorded_at"):
        assert private not in response.text
    assert client.get("/queue").status_code == 401
    assert client.get("/queue", headers={"X-Staff-Token": "incorrect"}).status_code == 401
    assert client.get("/queue", headers=STAFF).json()["groups"][0]["id"] == first["id"]


def test_subsequent_public_reads_reflect_staff_changes_and_remove_finished_groups(public_api):
    client, _, _, _ = public_api
    first = register(client, 3)
    second = register(client, 2, 1)
    third = register(client, 1)
    path = f'/receptions/{second["id"]}'
    for action, status in (("call", "called"), ("absent", "absent"), ("return", "waiting")):
        assert client.post(f"{path}/{action}", headers=STAFF).status_code == 200
        payload = client.get("/public/queue").json()
        assert_public_shape(payload)
        assert payload["groups"] == [public_group(first), public_group(second, status),
                                     public_group(third)]
    assert client.post(f'/receptions/{third["id"]}/seat', headers=STAFF).status_code == 200
    assert client.post(f'/receptions/{first["id"]}/cancel', headers=STAFF).status_code == 200
    payload = client.get("/public/queue").json()
    assert payload == {"waiting_groups": 1, "groups": [public_group(second)],
                       "notice": QUEUE_ORDER_NOTICE}
    assert client.post(f"{path}/seat", headers=STAFF).status_code == 200
    assert client.get("/public/queue").json() == {
        "waiting_groups": 0, "groups": [], "notice": QUEUE_ORDER_NOTICE,
    }


def test_public_reads_use_one_queue_snapshot_without_writing_or_predicting(public_api, monkeypatch):
    client, app, database, _ = public_api
    receipt = register(client)
    before = database.read_bytes()
    original_get_queue = app.state.store.get_queue
    calls = []

    def get_queue():
        calls.append(True)
        return original_get_queue()

    def no_prediction(**kwargs):
        pytest.fail("Public queue must not invoke prediction")

    monkeypatch.setattr(app.state.store, "get_queue", get_queue)
    monkeypatch.setattr(app.state.predictor, "predict", no_prediction)
    for _ in range(2):
        assert client.get("/public/queue").json()["groups"] == [public_group(receipt)]
    assert len(calls) == 2
    assert database.read_bytes() == before


def test_public_response_defensively_filters_additional_internal_store_fields(public_api, monkeypatch):
    client, app, _, _ = public_api
    row = {"display_number": "20261024-001", "total_people": 3, "status": "waiting",
           "id": "private-id", "adults": 2, "children": 1,
           "events": [{"token": STAFF_TOKEN}], "prediction": {"minutes": 17},
           "snapshot": {"source": "private-source"}}
    monkeypatch.setattr(app.state.store, "get_queue", lambda: [row])
    response = client.get("/public/queue")
    assert_public_shape(response.json())
    assert response.json()["groups"] == [public_group(row)]
    assert "private" not in response.text
    assert STAFF_TOKEN not in response.text
    # The response model also filters extras if later handlers return broader dicts.
    filtered = PublicQueueResponse.model_validate({
        "waiting_groups": 1, "groups": [row], "notice": QUEUE_ORDER_NOTICE,
        "internal_history": [{"token": STAFF_TOKEN}],
    }).model_dump()
    assert filtered == response.json()


def test_public_queue_preserves_same_origin_cache_and_content_security_headers(public_api):
    client, _, _, _ = public_api
    response = client.get("/public/queue", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("application/json")
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("headers, status", [
    ({"Origin": "https://other.example"}, 403),
    ({"Origin": "http://localhost"}, 403),
    ({"Host": "other.example"}, 400),
])
def test_public_queue_preserves_origin_and_host_limits(public_api, headers, status):
    client, _, _, _ = public_api
    assert client.get("/public/queue", headers=headers).status_code == status


def test_public_queue_rejects_non_loopback_peers_despite_forwarded_headers(public_api):
    _, app, _, _ = public_api
    with TestClient(app, base_url=ORIGIN, client=("192.0.2.1", 50000)) as client:
        response = client.get("/public/queue", headers={"X-Forwarded-For": "127.0.0.1"})
        assert response.status_code == 403


@pytest.mark.parametrize("error_type", [StoreBusy, sqlite3.OperationalError])
def test_public_queue_database_failure_is_503_without_internal_details(public_api, monkeypatch, error_type):
    client, app, _, _ = public_api

    def unavailable():
        raise error_type("private SQLite path or history")

    monkeypatch.setattr(app.state.store, "get_queue", unavailable)
    response = client.get("/public/queue")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text
    assert "groups" not in response.json()
