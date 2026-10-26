from datetime import date, timedelta
import warnings

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor

import model_artifact as artifacts
from preprocessing import BASE_FEATURE_COLUMNS, TrainingData, build_features


@pytest.fixture
def prepared_synthetic():
    dates = [date(2026, 5, 24) + timedelta(days=i) for i in range(13)] + [date(2026, 8, 7)]
    raw = pd.DataFrame([{
        "日付": dates[i % 14], "人数": 1 + i % 5, "大人": 1 + i % 5,
        "子供": 0, "受付時間": "12:10:30",
    } for i in range(226)])
    X = build_features(raw)
    return TrainingData(
        X, pd.Series([float(i % 31) for i in range(226)], name="待ち時間_分"),
        pd.DataFrame({"元行位置": range(226), "日付": raw["日付"]}), pd.DataFrame(),
    )


@pytest.fixture
def bundle(prepared_synthetic):
    return artifacts.train_model(prepared_synthetic, model_version="synthetic-test-v1")


def test_fit_uses_all_candidates_and_preserves_fixed_parameters(prepared_synthetic, monkeypatch):
    original = artifacts.RandomForestRegressor.fit
    received = []

    def fit(self, X, y, *args, **kwargs):
        received.append((X.copy(), y.copy()))
        return original(self, X, y, *args, **kwargs)

    monkeypatch.setattr(artifacts.RandomForestRegressor, "fit", fit)
    artifact = artifacts.train_model(prepared_synthetic, model_version="all-226")
    assert len(received) == 1 and len(received[0][0]) == 226
    pd.testing.assert_frame_equal(received[0][0], prepared_synthetic.X)
    pd.testing.assert_series_equal(received[0][1], prepared_synthetic.y)
    assert artifact["model"].get_params() == artifacts._fixed_model().get_params()
    info = artifact["metadata"]
    assert info["feature_columns"] == list(BASE_FEATURE_COLUMNS)
    assert info["training"]["rows"] == 226 and info["training"]["days"] == 14
    assert info["training"]["automatic_api_training"] is False
    assert info["training"]["evaluation_results_updated"] is False
    assert set(info["dependencies"]) == {"python", "scikit-learn", "numpy", "pandas", "scipy", "joblib"}
    assert info["source_sha256"] == artifacts.preprocessing_hashes()


def test_atomic_roundtrip_and_existing_version_is_not_overwritten(bundle, tmp_path):
    path = artifacts.save_model(bundle, output_dir=tmp_path)
    before = path.read_bytes()
    loaded = artifacts.load_model(path)
    assert loaded["metadata"] == bundle["metadata"]
    np.testing.assert_array_equal(loaded["model"].feature_names_in_, BASE_FEATURE_COLUMNS)
    with pytest.raises(FileExistsError):
        artifacts.save_model(bundle, output_dir=tmp_path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("key,value", [
    ("format_version", 2), ("format_version", True), ("model_version", "../invalid"),
    ("feature_columns", list(reversed(BASE_FEATURE_COLUMNS))),
    ("dependencies", {"scikit-learn": "0.0"}), ("source_sha256", {}),
    ("estimator_class", "DummyRegressor"), ("trained_at", "2026-01-01T12:00:00"),
    ("serving_domain", {}),
])
def test_tampered_metadata_is_rejected(bundle, tmp_path, key, value):
    bundle["metadata"][key] = value
    path = tmp_path / "tampered.joblib"
    joblib.dump(bundle, path)
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.load_model(path)


def test_estimator_parameter_change_is_rejected_even_when_metadata_matches(bundle):
    bundle["model"].set_params(max_depth=4)
    bundle["metadata"]["estimator_parameters"]["max_depth"] = 4
    with pytest.raises(artifacts.ModelArtifactError, match="固定設定"):
        artifacts.validate_artifact(bundle)


@pytest.mark.parametrize("change", ["other_model", "feature_order", "unfitted", "feature_count"])
def test_wrong_or_unfitted_model_is_rejected(bundle, change):
    if change == "other_model":
        bundle["model"] = DummyRegressor()
    elif change == "unfitted":
        bundle["model"] = artifacts._fixed_model()
    elif change == "feature_count":
        bundle["model"].n_features_in_ = 11
    else:
        bundle["model"].feature_names_in_ = np.array(list(reversed(BASE_FEATURE_COLUMNS)))
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.validate_artifact(bundle)


@pytest.mark.parametrize("path", ["https://example.invalid/model.joblib", "missing.joblib", "other.txt"])
def test_only_explicit_existing_local_joblib_is_accepted(path):
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.load_model(path)


def test_corrupt_and_warning_producing_model_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.joblib"
    path.write_bytes(b"not a model")
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.load_model(path)

    def warned_load(path):
        warnings.warn("incompatible model", UserWarning)

    monkeypatch.setattr(artifacts.joblib, "load", warned_load)
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.load_model(path)


@pytest.mark.parametrize("change", ["empty", "column_order", "row_order", "nan_target", "duplicate_position"])
def test_invalid_training_data_fails_before_fit(prepared_synthetic, monkeypatch, change):
    parts = list(prepared_synthetic)
    if change == "empty":
        parts[:3] = [part.iloc[:0] for part in parts[:3]]
    elif change == "column_order":
        parts[0] = parts[0].iloc[:, ::-1]
    elif change == "row_order":
        parts[1] = parts[1].iloc[::-1]
    elif change == "nan_target":
        parts[1].iloc[0] = np.nan
    else:
        parts[2].loc[1, "元行位置"] = 0
    prepared = TrainingData(*parts)
    monkeypatch.setattr(artifacts.RandomForestRegressor, "fit", lambda *a, **k: pytest.fail("fit must not run"))
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.train_model(prepared, model_version="invalid")


@pytest.mark.parametrize("change", ["rows", "dates", "automatic_api_training"])
def test_inconsistent_training_metadata_rejected(bundle, change):
    info = bundle["metadata"]["training"]
    info[change] = {"rows": 0, "dates": ["not-a-date"], "automatic_api_training": True}[change]
    with pytest.raises(artifacts.ModelArtifactError):
        artifacts.validate_artifact(bundle)
