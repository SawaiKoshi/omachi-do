from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

import prediction_service as prediction
from model_artifact import save_model, train_model
from preprocessing import BASE_FEATURE_COLUMNS, build_features, prepare_training_data


class RecordingModel:
    def __init__(self, output=12.3456789):
        self.output = output
        self.received = []

    def predict(self, X):
        self.received.append(X.copy())
        return np.array([self.output])


@pytest.fixture(scope="module")
def confirmed_synthetic_bundle():
    dates = [date(2026, 5, 24) + timedelta(days=i) for i in range(13)] + [date(2026, 8, 7)]
    raw = pd.DataFrame([{
        "日付": dates[i % 14], "呼び出し番号": i + 1, "人数": 2, "大人": 2,
        "子供": 0, "受付時間": "12:00", "案内時刻": "12:10", "待ち時間": "10分",
    } for i in range(226)])
    return train_model(
        prepare_training_data(raw, excel_date_system="1900"),
        model_version="synthetic-only", training_source="confirmed_step3_local_excel",
    )


@pytest.fixture
def predictor(monkeypatch, confirmed_synthetic_bundle):
    model = RecordingModel()
    monkeypatch.setattr(prediction, "load_model", lambda path: {
        "model": model, "metadata": deepcopy(confirmed_synthetic_bundle["metadata"]),
    })
    return prediction.PredictionService("trusted.joblib"), model


def request(service, **updates):
    return service.predict(**{
        "adults": 2, "children": 1,
        "received_at": datetime(2026, 7, 12, 12, 10, 30, 500000, tzinfo=prediction.JST),
        "waiting_groups": 3, **updates,
    })


def test_prediction_uses_exact_step2_features_jst_and_fractional_seconds(predictor):
    service, model = predictor
    moment = datetime(2026, 7, 11, 15, 0, 30, 500000, tzinfo=timezone.utc)
    result = request(service, received_at=moment)
    assert result["status"] == "available"
    assert result["minutes"] == 12.3456789  # 丸めや切上げを加えない。
    assert result["model_version"] == "synthetic-only"
    assert result["predicted_at"] == moment.isoformat()
    X = model.received[0]
    assert tuple(X.columns) == BASE_FEATURE_COLUMNS and X.shape == (1, 10)
    assert X.iloc[0]["受付時刻_分"] == pytest.approx(30.5 / 60)
    assert X.iloc[0]["曜日_日"] == 1 and X.iloc[0]["曜日_土"] == 0
    direct = build_features(pd.DataFrame([{
        "日付": moment.astimezone(prediction.JST).date(), "人数": 3, "大人": 2, "子供": 1,
        "受付時間": moment.astimezone(prediction.JST).time().replace(tzinfo=None),
    }]))
    pd.testing.assert_frame_equal(X, direct)


def test_waiting_groups_is_only_a_domain_condition_not_an_extra_feature(predictor):
    service, model = predictor
    request(service, waiting_groups=1)
    request(service, waiting_groups=99)
    pd.testing.assert_frame_equal(*model.received)


@pytest.mark.parametrize("updates,status", [
    ({"waiting_groups": 0}, "no_waiting_groups"),
    ({"received_at": datetime(2026, 7, 13, 12, tzinfo=prediction.JST)}, "unsupported_weekday"),
    ({"received_at": datetime(2026, 7, 12, 23, 0, 0, 1, tzinfo=prediction.JST)}, "unsupported_time"),
])
def test_unsupported_domain_skips_model_and_does_not_infer_zero(predictor, updates, status):
    service, model = predictor
    result = request(service, **updates)
    assert result["status"] == status
    assert result["minutes"] is None and result["model_version"] is None
    assert model.received == []


def test_23_exactly_is_supported_and_friday_cautions_are_returned(predictor):
    service, model = predictor
    result = request(service, received_at=datetime(2026, 8, 7, 23, tzinfo=prediction.JST))
    assert result["status"] == "available"
    assert any("2営業日・12件" in message for message in result["warnings"])
    assert model.received[0].iloc[0]["受付時刻_分"] == 1380


@pytest.mark.parametrize("updates", [
    {"adults": -1}, {"children": -1}, {"adults": True}, {"children": 1.0},
    {"adults": 0, "children": 0}, {"adults": 100, "children": 1},
    {"waiting_groups": -1}, {"waiting_groups": False}, {"waiting_groups": 1.0},
    {"received_at": datetime(2026, 7, 12, 12)}, {"received_at": "2026-07-12T12:00:00+09:00"},
    {"received_at": datetime.max.replace(tzinfo=timezone.utc)},
])
def test_invalid_input_is_rejected_even_without_model(updates):
    with pytest.raises(ValueError):
        request(prediction.PredictionService(), **updates)


def test_child_only_group_follows_existing_people_contract(predictor):
    service, _ = predictor
    assert request(service, adults=0, children=1)["status"] == "available"


def test_missing_or_corrupt_model_does_not_crash_startup(tmp_path):
    missing = prediction.PredictionService(tmp_path / "missing.joblib")
    corrupt = tmp_path / "corrupt.joblib"
    corrupt.write_bytes(b"broken")
    services = [prediction.PredictionService(), missing, prediction.PredictionService(corrupt)]
    for service in services:
        assert service.available is False
        result = request(service)
        assert result["status"] == "model_unavailable" and result["minutes"] is None
        assert result["model_version"] is None


def test_confirmed_full_candidate_bundle_can_be_loaded_and_predict(confirmed_synthetic_bundle, tmp_path):
    path = save_model(confirmed_synthetic_bundle, output_dir=tmp_path)
    service = prediction.PredictionService(path)
    assert service.available is True
    result = request(service)
    assert result["status"] == "available" and result["minutes"] == pytest.approx(10.0)


def test_small_generic_training_bundle_stays_loadable_but_is_not_served(tmp_path):
    raw = pd.DataFrame([{
        "日付": date(2026, 7, 12), "呼び出し番号": i + 1, "人数": 2, "大人": 2,
        "子供": 0, "受付時間": "12:00", "案内時刻": "12:10", "待ち時間": "10分",
    } for i in range(20)])
    bundle = train_model(prepare_training_data(raw), model_version="small-generic")
    path = save_model(bundle, output_dir=tmp_path)
    assert prediction.load_model(path)["metadata"]["training"]["rows"] == 20
    service = prediction.PredictionService(path)
    assert service.available is False
    assert request(service)["status"] == "model_unavailable"


@pytest.mark.parametrize("updates", [
    {"source": "provided_prepared_dataframe"}, {"rows": 20}, {"days": 2},
    {"date_start": "2026-05-23"}, {"date_end": "2026-07-12"},
    {"excel_date_system": "1904"},
])
def test_unconfirmed_or_partial_training_bundle_is_not_served(
    confirmed_synthetic_bundle, tmp_path, updates,
):
    bundle = deepcopy(confirmed_synthetic_bundle)
    training = bundle["metadata"]["training"]
    training.update(updates)
    # library loadは汎用のまま。整合する条件にして、配信専用ガードを確認する。
    if "days" in updates:
        training["dates"] = [training["date_start"], training["date_end"]]
    elif "date_start" in updates:
        training["dates"][0] = updates["date_start"]
    elif "date_end" in updates:
        training["dates"][-1] = updates["date_end"]
    path = save_model(bundle, output_dir=tmp_path)
    assert prediction.load_model(path)["metadata"]["training"] == training
    service = prediction.PredictionService(path)
    assert service.available is False and service.unavailable_reason == "model_load_failed"
    assert request(service)["status"] == "model_unavailable"


@pytest.mark.parametrize("output", [-1, np.nan, np.inf, -np.inf, "not a number"])
def test_invalid_prediction_fails_without_stopping_reception(predictor, output):
    service, model = predictor
    model.output = output
    result = request(service)
    assert result["status"] == "prediction_failed" and result["minutes"] is None
    assert result["model_version"] == "synthetic-only"


def test_model_exception_is_not_exposed(predictor, monkeypatch):
    service, model = predictor

    def fail(X):
        raise RuntimeError("private-local-data-path")

    monkeypatch.setattr(model, "predict", fail)
    result = request(service)
    assert result["status"] == "prediction_failed"
    assert "private-local-data-path" not in str(result)


def test_service_never_reads_excel_or_writes_reception_history(monkeypatch, tmp_path):
    monkeypatch.setattr(pd, "read_excel", lambda *a, **k: pytest.fail("Excel must not be read"))
    monkeypatch.chdir(tmp_path)
    assert request(prediction.PredictionService())["status"] == "model_unavailable"
    assert list(tmp_path.iterdir()) == []
