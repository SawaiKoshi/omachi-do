"""Existing HTTP contracts against synthetic v1/v2 stores, no real model or data."""

from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import numpy as np
import pytest

import prediction_api
import reception_store
from reception_migration.migration import copy_database, migrate
from reception_migration.safety import temporary_workspace
from reception_migration.synthetic import create_v1
from reception_store import StoreValidationError


TOKEN = "synthetic-v2-compatibility-staff-token-123456789"
STAFF = {"X-Staff-Token": TOKEN}
ORIGIN = "http://127.0.0.1"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 24, 12, tzinfo=ZoneInfo("Asia/Tokyo"))

    def __call__(self):
        return self.value


class SyntheticEstimator:
    """Observe serving features without loading or fitting a model."""
    def __init__(self):
        self.inputs = []

    def predict(self, features):
        self.inputs.append((tuple(features.columns), features.to_numpy().tobytes()))
        return np.asarray([17.125])


def party(client, key, adults=2, children=0):
    return client.post("/receptions", json={"adults": adults, "children": children},
                       headers={"Idempotency-Key": str(UUID(int=key))})


def responses(client, clock):
    output = []
    def record(response):
        output.append((response.status_code, response.json(),
                       response.headers.get("idempotent-replayed"), response.headers.get("cache-control")))
        return response
    record(client.get("/health"))
    record(client.post("/predict", json={"adults": 2, "children": 0}))
    first = record(party(client, 201, 4, 1)).json()
    second = record(party(client, 202)).json()
    third = record(party(client, 203, 1)).json()
    record(client.get("/queue", headers=STAFF))
    record(client.get("/public/queue"))
    record(client.post("/predict", json={"adults": 2, "children": 0}))
    path = f'/receptions/{first["id"]}'
    for action in ("call", "absent", "return", "call", "seat"):
        clock.value += timedelta(seconds=30, microseconds=123456)
        record(client.post(f"{path}/{action}", headers=STAFF))
        record(client.post(f"{path}/{action}", headers=STAFF))
        record(client.get("/public/queue"))
    record(client.post(f'/receptions/{second["id"]}/cancel', headers=STAFF))
    record(client.post(f'/receptions/{third["id"]}/seat', headers=STAFF))
    record(party(client, 201, 4, 1))
    record(party(client, 201, 5, 1))
    record(client.post(f"{path}/cancel", headers=STAFF))
    record(client.post(f"{path}/call"))
    record(client.get("/queue", headers=STAFF))
    record(client.get("/public/queue"))
    return output


@pytest.mark.parametrize("with_estimator", [False, True])
def test_all_existing_http_responses_and_prediction_features_match_v1_v2(monkeypatch, with_estimator):
    with temporary_workspace() as lab:
        v1, _ = create_v1(lab, "v1.sqlite3", populated=False)
        v2 = copy_database(lab, v1, "v2.sqlite3")
        migrate(lab, v2, expected_sha256=sha256(v2.read_bytes()).hexdigest())
        output, features = [], []
        for database in (v1, v2):
            ids = iter(UUID(int=index) for index in (101, 102, 103))
            monkeypatch.setattr(reception_store, "uuid4", lambda: next(ids))
            clock = Clock()
            app = prediction_api.create_app(db_path=database, clock=clock, staff_token=TOKEN,
                                           model_path=lab.root / "missing-model.joblib")
            estimator = SyntheticEstimator()
            with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
                if with_estimator:
                    app.state.predictor._artifact = {
                        "model": estimator, "metadata": {"model_version": "synthetic-only"}}
                output.append(responses(client, clock))
                features.append(estimator.inputs)
                private = ("revision", "audit_kind", "operation_id", "before_state_json", "after_state_json",
                           "staff_write_mode", "database_id", "write_epoch", "seat_corrections_enabled")
                body = json.dumps(output[-1])
                assert not any(f'"{name}"' in body for name in private)
                with lab.connection(database) as conn:
                    if database == v2:
                        db_id, epoch = conn.execute("SELECT database_id,write_epoch FROM store_metadata").fetchone()
                        assert db_id not in body and epoch not in body
        assert output[0] == output[1]
        assert features[0] == features[1]
        if with_estimator:
            assert features[0]
            assert any(response[1].get("prediction", {}).get("minutes") == 17.125 for response in output[0])


def test_revision_required_returns_409_without_changing_rows_and_customer_reads_continue():
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        app = prediction_api.create_app(db_path=database, clock=Clock(), staff_token=TOKEN,
                                       model_path=lab.root / "missing.joblib")
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            receipt = party(client, 301).json()
            with lab.connection(database) as conn:
                conn.execute("UPDATE store_metadata SET staff_write_mode='revision_required'")
            original = database.read_bytes()
            for action in ("call", "absent", "return", "seat", "cancel"):
                response = client.post(f'/receptions/{receipt["id"]}/{action}', headers=STAFF)
                assert response.status_code == 409
                assert database.read_bytes() == original
            assert client.get("/public/queue").status_code == 200
            assert client.get("/queue", headers=STAFF).status_code == 200
            assert client.post("/predict", json={"adults": 2, "children": 0}).status_code == 200
            assert database.read_bytes() == original
            assert party(client, 302).status_code == 201


@pytest.mark.parametrize("version", [1, 2])
def test_bad_database_stops_lifespan_before_predictor_initialization(monkeypatch, version):
    with temporary_workspace() as lab:
        database, _ = create_v1(lab, populated=False)
        if version == 2:
            migrate(lab, database, expected_sha256=sha256(database.read_bytes()).hexdigest())
        with lab.connection(database) as conn:
            conn.execute("DROP INDEX queue_status_order")
        original = database.read_bytes()
        def forbidden(*args, **kwargs):
            pytest.fail("Predictor initialization must follow database validation")
        monkeypatch.setattr(prediction_api, "PredictionService", forbidden)
        app = prediction_api.create_app(db_path=database, staff_token=TOKEN)
        with pytest.raises(StoreValidationError):
            with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)):
                pass
        assert database.read_bytes() == original


def test_imported_v1_receipt_replay_and_subsequent_seating_keep_exact_http_contract():
    with temporary_workspace() as lab:
        v1, _ = create_v1(lab, "v1.sqlite3", populated=False)
        clock = Clock()
        app = prediction_api.create_app(db_path=v1, clock=clock, staff_token=TOKEN,
                                       model_path=lab.root / "missing.joblib")
        with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
            initial = party(client, 401).json()
            waiting = party(client, 402, 3).json()
            client.post(f'/receptions/{initial["id"]}/seat', headers=STAFF).raise_for_status()
            client.post(f'/receptions/{waiting["id"]}/call', headers=STAFF).raise_for_status()
        v2 = copy_database(lab, v1, "v2.sqlite3")
        migrate(lab, v2, expected_sha256=sha256(v2.read_bytes()).hexdigest())
        results = []
        for database in (v1, v2):
            # Use the same seating clock in each independently served copy.
            serve_clock = Clock()
            serve_clock.value += timedelta(minutes=3)
            app = prediction_api.create_app(db_path=database, clock=serve_clock, staff_token=TOKEN,
                                           model_path=lab.root / "missing.joblib")
            with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000)) as client:
                replay = party(client, 401)
                assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
                assert replay.json() == initial
                result = client.post(f'/receptions/{waiting["id"]}/seat', headers=STAFF)
                assert result.status_code == 200 and result.json()["actual_wait_minutes"] == 3.0
                results.append((replay.json(), result.json()))
            with lab.connection(database) as conn:
                raw = conn.execute("SELECT response_json FROM idempotency_keys WHERE key=?", (str(UUID(int=401)),)).fetchone()[0]
                prediction_json = conn.execute("SELECT result_json FROM predictions WHERE reception_id=?", (waiting["id"],)).fetchone()[0]
                results[-1] += (raw, prediction_json)
        assert results[0] == results[1]
