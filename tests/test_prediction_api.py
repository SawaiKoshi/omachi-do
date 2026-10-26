"""Local HTTP contracts with a temporary SQLite DB and synthetic clocks/models."""

from contextlib import closing
from datetime import date, datetime, timedelta
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pandas as pd
import pytest

from prediction_api import create_app
from reception_store import StoreBusy


STAFF_TOKEN = "synthetic-local-staff-token-1234567890"
STAFF = {"X-Staff-Token": STAFF_TOKEN}
JST = ZoneInfo("Asia/Tokyo")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 24, 12, tzinfo=JST)

    def __call__(self):
        return self.value


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    def no_excel(*args, **kwargs):
        pytest.fail("API must not read any Excel file")

    monkeypatch.setattr(pd, "read_excel", no_excel)
    clock = Clock()
    path = tmp_path / "api.sqlite3"
    app = create_app(db_path=path, model_path=tmp_path / "missing.joblib",
                     staff_token=STAFF_TOKEN, clock=clock)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        yield client, app, path, clock


def register(client, adults=2, children=0, *, key=None):
    return client.post("/receptions", json={"adults": adults, "children": children},
                       headers={"Idempotency-Key": key or str(uuid4())})


def counts(path):
    with closing(sqlite3.connect(path)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}


def test_factory_and_module_do_not_open_database_until_lifespan(tmp_path):
    path = tmp_path / "later" / "api.sqlite3"
    create_app(db_path=path, staff_token=STAFF_TOKEN)
    assert not path.parent.exists()


def test_health_and_registration_work_without_a_model_or_excel(local_api):
    client, _, _, _ = local_api
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "model_status": "unavailable", "staff_operations_enabled": True}
    first = register(client)
    assert first.status_code == 201
    assert first.json()["prediction"]["status"] == "no_waiting_groups"
    second = register(client, 3, 1)
    assert second.status_code == 201
    result = second.json()
    assert result["prediction"]["status"] == "model_unavailable"
    assert result["prediction"]["minutes"] is None
    assert result["snapshot"]["waiting_groups"] == 1
    assert result["snapshot"]["party_size_groups"] == {"1-2": 1, "3-4": 0, "5+": 0}
    assert result["display_number"] == "20261024-002"
    assert result["id"] != result["display_number"]
    assert result["status"] == "waiting"
    assert result["seated_at"] is result["actual_wait_minutes"] is None
    assert second.headers["cache-control"] == "no-store"


def test_predict_only_uses_current_queue_and_never_saves_history(local_api):
    client, _, path, clock = local_api
    register(client, 3, 1)
    before = counts(path)
    original_bytes = path.read_bytes()
    clock.value += timedelta(minutes=5)
    for _ in range(2):
        prediction = client.post("/predict", json={"adults": 2, "children": 0})
        assert prediction.status_code == 200
        assert prediction.json()["snapshot"]["waiting_groups"] == 1
        assert prediction.json()["snapshot"]["captured_at"] == clock.value.isoformat()
        assert prediction.json()["prediction"]["status"] == "model_unavailable"
    assert counts(path) == before
    assert path.read_bytes() == original_bytes
    assert register(client).json()["display_number"] == "20261024-002"


def test_idempotency_replays_initial_receipt_and_conflicting_payload_is_409(local_api):
    client, _, path, _ = local_api
    key = str(uuid4())
    first = register(client, key=key)
    receipt = first.json()
    assert client.post(f'/receptions/{receipt["id"]}/seat', headers=STAFF).status_code == 200
    before = counts(path)
    replay = register(client, key=key.upper())
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == receipt
    assert register(client, 3, 0, key=key).status_code == 409
    assert counts(path) == before


@pytest.mark.parametrize("payload", [
    {}, {"adults": 2}, {"adults": 0, "children": 0},
    {"adults": -1, "children": 1}, {"adults": 1, "children": -1},
    {"adults": 100, "children": 1}, {"adults": True, "children": 0},
    {"adults": 1, "children": False}, {"adults": "2", "children": 0},
    {"adults": 1.0, "children": 0}, {"adults": None, "children": 0},
    {"adults": 2, "children": 0, "received_at": "2026-10-24T11:00:00+09:00"},
    {"adults": 2, "children": 0, "waiting_groups": 3},
    {"adults": 2, "children": 0, "weather": "晴れ"},
    {"adults": 2, "children": 0, "name": "MUST-NOT-ECHO-OR-SAVE"},
])
@pytest.mark.parametrize("endpoint", ["/predict", "/receptions"])
def test_invalid_or_extra_customer_input_is_422_without_db_changes(local_api, endpoint, payload):
    client, _, path, _ = local_api
    before = counts(path)
    response = client.post(endpoint, json=payload, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 422
    assert "MUST-NOT-ECHO-OR-SAVE" not in response.text
    assert counts(path) == before


def test_zero_adults_matches_existing_people_rules(local_api):
    client, _, _, _ = local_api
    result = register(client, 0, 1)
    assert result.status_code == 201
    assert result.json()["total_people"] == 1


def test_idempotency_header_is_required_and_must_be_a_uuid(local_api):
    client, _, path, _ = local_api
    before = counts(path)
    assert client.post("/receptions", json={"adults": 2, "children": 0}).status_code == 422
    assert register(client, key="duplicate").status_code == 422
    assert counts(path) == before


def test_staff_auth_is_required_and_customer_receipt_does_not_list_other_groups(local_api):
    client, _, _, _ = local_api
    first = register(client, 4).json()
    second = register(client, 2).json()
    assert first["id"] not in str(second)
    assert first["display_number"] not in str(second)
    for path in ("/queue", f'/receptions/{first["id"]}/seat', f'/receptions/{first["id"]}/cancel'):
        method = client.get if path == "/queue" else client.post
        assert method(path).status_code == 401
        assert method(path, headers={"X-Staff-Token": "incorrect"}).status_code == 401
    result = client.get("/queue", headers=STAFF)
    assert result.status_code == 200
    assert result.json()["waiting_groups"] == 2
    assert [group["id"] for group in result.json()["groups"]] == [first["id"], second["id"]]
    assert "prediction" not in result.json()["groups"][0]


def test_unconfigured_staff_operations_fail_closed_but_intake_works(tmp_path, monkeypatch):
    monkeypatch.delenv("OMACHI_STAFF_TOKEN", raising=False)
    app = create_app(db_path=tmp_path / "no-staff.sqlite3", clock=Clock())
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert register(client).status_code == 201
        assert client.get("/queue").status_code == 503
        assert client.get("/health").json()["staff_operations_enabled"] is False


def test_call_absence_and_seating_are_distinct_and_seating_can_skip_queue_order(local_api):
    client, _, _, clock = local_api
    first = register(client, 4).json()
    later = register(client, 2).json()
    frozen = later["snapshot"]
    clock.value += timedelta(minutes=1)
    assert client.post(f'/receptions/{later["id"]}/absent', headers=STAFF).status_code == 409
    called = client.post(f'/receptions/{later["id"]}/call', headers=STAFF).json()
    assert called["called_at"] == clock.value.isoformat()
    assert called["seated_at"] is called["actual_wait_minutes"] is None
    clock.value += timedelta(minutes=1)
    assert client.post(f'/receptions/{later["id"]}/absent', headers=STAFF).json()["status"] == "absent"
    assert client.get("/queue", headers=STAFF).json()["waiting_groups"] == 2
    assert client.post(f'/receptions/{later["id"]}/return', headers=STAFF).json()["status"] == "waiting"
    clock.value += timedelta(minutes=1, seconds=30)
    seated = client.post(f'/receptions/{later["id"]}/seat', headers=STAFF).json()
    assert seated["actual_wait_minutes"] == 3.5
    assert seated["called_at"] != seated["seated_at"]
    assert seated["snapshot"] == frozen
    assert client.get("/queue", headers=STAFF).json()["groups"][0]["id"] == first["id"]
    assert client.post(f'/receptions/{later["id"]}/cancel', headers=STAFF).status_code == 409
    cancelled = client.post(f'/receptions/{first["id"]}/cancel', headers=STAFF).json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["actual_wait_minutes"] is None


def test_missing_or_malformed_ids_and_bad_clock_are_rejected(local_api):
    client, _, path, clock = local_api
    assert client.post(f'/receptions/{uuid4()}/seat', headers=STAFF).status_code == 404
    assert client.post('/receptions/not-an-id/seat', headers=STAFF).status_code == 422
    row = register(client).json()
    before = counts(path)
    clock.value -= timedelta(seconds=1)
    assert client.post(f'/receptions/{row["id"]}/seat', headers=STAFF).status_code == 422
    assert register(client).status_code == 422
    assert counts(path) == before
    clock.value = datetime(2026, 10, 24, 12)
    assert client.post('/predict', json={"adults": 2, "children": 0}).status_code == 422


def test_prediction_exception_does_not_stop_intake_but_db_failure_is_503(local_api, monkeypatch):
    client, app, _, _ = local_api

    def broken(**kwargs):
        raise RuntimeError("private model error")

    monkeypatch.setattr(app.state.predictor, "predict", broken)
    result = register(client)
    assert result.status_code == 201
    assert result.json()["prediction"]["status"] == "prediction_failed"
    assert "private model error" not in result.text

    def busy(*args, **kwargs):
        raise StoreBusy("private db path")

    monkeypatch.setattr(app.state.store, "register", busy)
    unavailable = register(client)
    assert unavailable.status_code == 503
    assert unavailable.headers["retry-after"] == "1"
    assert "private db path" not in unavailable.text


def test_local_host_origin_and_peer_restrictions_and_docs_are_disabled(local_api):
    client, app, _, _ = local_api
    assert client.get('/health', headers={"Host": "evil.example"}).status_code == 400
    assert client.post('/predict', json={"adults": 2, "children": 0},
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get('/health', headers={"Origin": "http://127.0.0.1"}).status_code == 200
    for path in ('/docs', '/redoc', '/openapi.json'):
        assert client.get(path).status_code == 404
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 50000)) as remote:
        assert remote.get('/health', headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_weak_staff_token_is_rejected_before_database_is_created(tmp_path):
    path = tmp_path / 'weak.sqlite3'
    with pytest.raises(ValueError):
        create_app(db_path=path, staff_token="short")
    assert not path.exists()


@pytest.mark.parametrize("action", ["seat", "cancel", "call", "absent", "return"])
def test_staff_cannot_supply_timestamps_or_extra_data(local_api, action):
    client, _, path, _ = local_api
    row = register(client).json()
    before = counts(path)
    response = client.post(f'/receptions/{row["id"]}/{action}', headers=STAFF,
                           json={"seated_at": "invalid-date", "name": "not-saved"})
    assert response.status_code == 422
    assert counts(path) == before


def test_full_synthetic_model_predicts_through_http_and_saves_version_separately_from_actual_wait(tmp_path, monkeypatch):
    from model_artifact import save_model, train_model
    from preprocessing import prepare_training_data

    days = [date(2026, 5, 24) + timedelta(days=i) for i in range(13)] + [date(2026, 8, 7)]
    synthetic = pd.DataFrame([{
        "日付": days[i % 14], "呼び出し番号": i + 1, "人数": 2, "大人": 2, "子供": 0,
        "受付時間": "12:00", "案内時刻": "12:10", "待ち時間": "10分",
    } for i in range(226)])
    artifact = train_model(prepare_training_data(synthetic), model_version="http-synthetic-v1",
                           training_source="confirmed_step3_local_excel")
    model_path = save_model(artifact, output_dir=tmp_path)
    clock = Clock()
    app = create_app(db_path=tmp_path / "with-model.sqlite3", model_path=model_path,
                     staff_token=STAFF_TOKEN, clock=clock)

    def no_excel(*args, **kwargs):
        pytest.fail("serving must not read Excel")

    monkeypatch.setattr(pd, "read_excel", no_excel)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.get("/health").json()["model_status"] == "available"
        register(client)
        preview = client.post("/predict", json={"adults": 2, "children": 0}).json()
        record = register(client).json()
        assert record["prediction"] == preview["prediction"]
        assert record["prediction"]["status"] == "available"
        assert record["prediction"]["minutes"] == pytest.approx(10.0)
        assert record["prediction"]["model_version"] == "http-synthetic-v1"
        assert record["actual_wait_minutes"] is None
        clock.value += timedelta(minutes=2, seconds=30)
        seated = client.post(f'/receptions/{record["id"]}/seat', headers=STAFF).json()
        assert seated["actual_wait_minutes"] == 2.5
        assert seated["prediction"] == record["prediction"]
        assert app.state.store.get(record["id"])["prediction"] == record["prediction"]
