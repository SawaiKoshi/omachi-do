"""合成データだけでStep 3の時間順検証と最終テストの隔離を確認する。"""

from datetime import date, timedelta
import sys

import numpy as np
import pandas as pd
import pytest
from sklearn.base import BaseEstimator, RegressorMixin
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from model_evaluation import (
    ModelData, candidate_models, compare_models, expanding_date_folds,
    split_development_test,
)
from preprocessing import BASE_FEATURE_COLUMNS, prepare_training_data


class RecordingRegressor(RegressorMixin, BaseEstimator):
    """cloneされた各fitに渡る行を記録する。予測値は合成した正解と一致する。"""

    fits = []

    def fit(self, X, y):
        type(self).fits.append((self, X.copy(deep=True), y.copy(deep=True)))
        self.fitted_ = True
        return self

    def predict(self, X):
        assert self.fitted_
        assert tuple(X.columns) == BASE_FEATURE_COLUMNS
        return X["大人"].to_numpy(dtype=float)


@pytest.fixture
def synthetic_prepared(make_frame):
    """14営業日×2件。重複ラベルと逆順の日付で位置による対応も検証する。"""
    rows = []
    for day_number in range(14):
        for within_day in range(2):
            wait = 2 + 2 * day_number + within_day
            rows.append({
                "日付": date(2030, 1, 1) + timedelta(days=day_number),
                "人数": wait,
                "大人": wait,
                "子供": 0,
                "受付時間": "12:00",
                "案内時刻": f"12:{wait:02d}",
                "待ち時間": wait,
                "予約件数_11時台": 999,
                "氏名": "合成データ",
            })
    raw = make_frame(*rows, index=[5, 5, 9, 9] * 7).iloc[::-1].copy()
    return prepare_training_data(raw)


def test_default_windows_use_business_dates_and_keep_input_order(synthetic_prepared):
    partition = split_development_test(synthetic_prepared)
    dates = partition.development.metadata["日付"]
    folds = expanding_date_folds(dates)

    assert len(folds) == 3
    for fold, training_days in zip(folds, (6, 8, 10)):
        train_days = set(dates.iloc[fold.train])
        validation_days = set(dates.iloc[fold.validation])
        assert len(train_days) == training_days
        assert len(validation_days) == 2
        assert max(train_days) < min(validation_days)
        assert train_days.isdisjoint(validation_days)
        assert np.all(np.diff(fold.train) > 0)
        assert np.all(np.diff(fold.validation) > 0)
        # 同じ営業日の全行が一つの区間へ入る。
        for positions, selected_days in ((fold.train, train_days),
                                         (fold.validation, validation_days)):
            expected = np.flatnonzero(dates.isin(selected_days).to_numpy())
            np.testing.assert_array_equal(positions, expected)


def test_date_windows_are_generated_from_dates_not_calendar_gaps():
    dates = pd.Series([
        "2030-06-30", "2030-01-01", "2030-04-04", "2030-01-01",
        "2030-08-31", "2030-02-03",
    ], index=[7] * 6)
    folds = expanding_date_folds(dates, initial_train_days=2, validation_days=1)
    assert len(folds) == 3
    assert folds[0].train.tolist() == [1, 3, 5]
    assert folds[0].validation.tolist() == [2]
    assert folds[-1].validation.tolist() == [4]


@pytest.mark.parametrize("values,options", [
    ([], {}),
    (["2030-01-01"] * 4, {}),
    (["2030-01-01", None, "2030-01-03"],
     {"initial_train_days": 1, "validation_days": 1}),
    (["2030-01-01", "1/2", "2030-01-03"],
     {"initial_train_days": 1, "validation_days": 1}),
    (["2030-01-01", "2030-01-02", "2030-01-03"],
     {"initial_train_days": 2, "validation_days": 2}),
    ([f"2030-01-{day:02d}" for day in range(1, 10)], {}),
])
def test_invalid_or_partial_date_windows_are_rejected(values, options):
    with pytest.raises(ValueError):
        expanding_date_folds(pd.Series(values, dtype=object), **options)


@pytest.mark.parametrize("name", ["initial_train_days", "validation_days"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2"])
def test_window_counts_must_be_positive_integers(name, value):
    with pytest.raises(ValueError):
        expanding_date_folds(pd.Series(pd.date_range("2030-01-01", periods=14)),
                             **{name: value})


def test_final_dates_are_reserved_with_exact_positions_and_copies(synthetic_prepared):
    before_X = synthetic_prepared.X.copy(deep=True)
    before_y = synthetic_prepared.y.copy(deep=True)
    before_metadata = synthetic_prepared.metadata.copy(deep=True)
    test_dates = [date(2030, 1, 13), date(2030, 1, 14)]
    partition = split_development_test(synthetic_prepared, expected_test_dates=test_dates)

    for part, mask in ((partition.development,
                        ~synthetic_prepared.metadata["日付"].isin(test_dates)),
                       (partition.final_test,
                        synthetic_prepared.metadata["日付"].isin(test_dates))):
        positions = np.flatnonzero(mask.to_numpy())
        pd.testing.assert_frame_equal(part.X, synthetic_prepared.X.iloc[positions])
        pd.testing.assert_series_equal(part.y, synthetic_prepared.y.iloc[positions])
        pd.testing.assert_frame_equal(part.metadata,
                                      synthetic_prepared.metadata.iloc[positions])
        assert part.X.index.tolist() == part.y.index.tolist() == part.metadata.index.tolist()
        part.X.iloc[0, 0] = -99
        part.y.iloc[0] = -99
        part.metadata.iloc[0, 0] = -99

    pd.testing.assert_frame_equal(synthetic_prepared.X, before_X)
    pd.testing.assert_series_equal(synthetic_prepared.y, before_y)
    pd.testing.assert_frame_equal(synthetic_prepared.metadata, before_metadata)


def test_unexpected_final_dates_fail_before_model_selection(synthetic_prepared):
    with pytest.raises(ValueError):
        split_development_test(synthetic_prepared,
                               expected_test_dates=["2030-01-12", "2030-01-14"])


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "2", 14, 15])
def test_invalid_final_day_counts_rejected(synthetic_prepared, value):
    with pytest.raises(ValueError):
        split_development_test(synthetic_prepared, final_test_days=value)


def test_model_cloning_row_alignment_and_prediction_provenance(synthetic_prepared):
    development = split_development_test(synthetic_prepared).development
    RecordingRegressor.fits = []
    supplied_model = RecordingRegressor()
    result = compare_models(development, models={"recording": supplied_model})
    folds = expanding_date_folds(development.metadata["日付"])

    assert len(RecordingRegressor.fits) == len(folds)
    assert len({id(model) for model, _, _ in RecordingRegressor.fits}) == len(folds)
    assert not hasattr(supplied_model, "fitted_")
    for (_, fitted_X, fitted_y), fold in zip(RecordingRegressor.fits, folds):
        pd.testing.assert_frame_equal(fitted_X, development.X.iloc[fold.train])
        pd.testing.assert_series_equal(fitted_y, development.y.iloc[fold.train])
        np.testing.assert_array_equal(fitted_X["大人"].to_numpy(), fitted_y.to_numpy())

    expected_positions = np.sort(np.concatenate([fold.validation for fold in folds]))
    assert result.predictions["position"].tolist() == expected_positions.tolist()
    assert result.predictions["source_position"].tolist() == (
        development.metadata["元行位置"].iloc[expected_positions].tolist()
    )
    assert result.predictions["date"].tolist() == (
        development.metadata["日付"].iloc[expected_positions].tolist()
    )
    np.testing.assert_array_equal(result.predictions["y_true"],
                                  development.y.iloc[expected_positions])
    np.testing.assert_array_equal(result.predictions["y_pred"],
                                  result.predictions["y_true"])
    assert (result.fold_metrics["validation_mae"] == 0).all()
    assert len(result.daily_metrics) == 6
    assert result.daily_metrics["n"].tolist() == [2] * 6
    assert (result.daily_metrics["mae"] == 0).all()


def test_held_out_values_cannot_reach_validation_or_fitting(synthetic_prepared):
    final_mask = synthetic_prepared.metadata["日付"] >= date(2030, 1, 13)
    # 最終テストの数値を読めば失敗する値でも、選定には影響しない。
    synthetic_prepared.X.iloc[np.flatnonzero(final_mask.to_numpy()), 0] = np.nan
    synthetic_prepared.y.iloc[np.flatnonzero(final_mask.to_numpy())] = np.inf
    partition = split_development_test(synthetic_prepared)
    RecordingRegressor.fits = []
    result = compare_models(partition.development, models={"recording": RecordingRegressor()})

    reserved_sources = set(partition.final_test.metadata["元行位置"])
    assert reserved_sources.isdisjoint(result.predictions["source_position"])
    assert max(result.predictions["date"]) < min(partition.final_test.metadata["日付"])
    for _, X, y in RecordingRegressor.fits:
        assert np.isfinite(X.to_numpy()).all()
        assert np.isfinite(y.to_numpy()).all()
    assert not hasattr(result, "final_test_metrics")


def test_model_selection_requires_explicitly_partitioned_development_data(synthetic_prepared):
    with pytest.raises((TypeError, ValueError)):
        compare_models(synthetic_prepared, models={"dummy": DummyRegressor()})


def test_mae_rmse_daily_metrics_and_fold_variation_are_correct(synthetic_prepared):
    original = split_development_test(synthetic_prepared).development
    positions = [23, 21, 19, 17]  # 一営業日一行、入力は時間順。
    X = original.X.iloc[positions].copy()
    y = pd.Series([0.0, 2.0, 4.0, 6.0], index=X.index, name=original.y.name)
    metadata = original.metadata.iloc[positions].copy()
    data = ModelData(X, y, metadata)
    result = compare_models(data, models={"zero": DummyRegressor(strategy="constant", constant=0)},
                            initial_train_days=2, validation_days=1)

    assert result.fold_metrics["train_mae"].tolist() == pytest.approx([1, 2])
    assert result.fold_metrics["validation_mae"].tolist() == pytest.approx([4, 6])
    assert result.fold_metrics["train_rmse"].tolist() == pytest.approx([np.sqrt(2), np.sqrt(20 / 3)])
    assert result.fold_metrics["validation_rmse"].tolist() == pytest.approx([4, 6])
    assert result.fold_metrics["mae_gap"].tolist() == pytest.approx([3, 4])
    assert result.fold_metrics["rmse_gap"].tolist() == pytest.approx([
        4 - np.sqrt(2), 6 - np.sqrt(20 / 3),
    ])
    assert result.daily_metrics["mae"].tolist() == pytest.approx([4, 6])
    assert result.daily_metrics["n"].tolist() == [1, 1]
    summary = result.summary.iloc[0]
    assert summary["folds"] == 2
    assert summary["validation_mae_mean"] == pytest.approx(5)
    assert summary["validation_mae_std"] == pytest.approx(1)
    assert summary["validation_rmse_mean"] == pytest.approx(5)
    assert summary["validation_rmse_std"] == pytest.approx(1)
    assert summary["oof_mae"] == pytest.approx(5)
    assert summary["oof_rmse"] == pytest.approx(np.sqrt(26))
    assert summary["daily_mae_mean"] == pytest.approx(5)


def test_fold_daily_and_pooled_mae_keep_different_sample_weights(synthetic_prepared):
    original = split_development_test(synthetic_prepared).development
    # 営業日ごとの件数を変え、fold平均と全検証行の平均の違いを明示する。
    positions = [23, 21, 19, 18, 17]
    X = original.X.iloc[positions].copy()
    y = pd.Series([0.0, 0.0, 2.0, 10.0, 4.0], index=X.index, name=original.y.name)
    data = ModelData(X, y, original.metadata.iloc[positions].copy())
    result = compare_models(data, models={"zero": DummyRegressor(strategy="constant", constant=0)},
                            initial_train_days=2, validation_days=1)

    assert result.daily_metrics["n"].tolist() == [2, 1]
    assert result.daily_metrics["mae"].tolist() == pytest.approx([6, 4])
    assert result.daily_metrics["rmse"].tolist() == pytest.approx([np.sqrt(52), 4])
    summary = result.summary.iloc[0]
    assert summary["validation_mae_mean"] == pytest.approx(5)
    assert summary["daily_mae_mean"] == pytest.approx(5)
    assert summary["oof_mae"] == pytest.approx(16 / 3)
    assert summary["oof_rmse"] == pytest.approx(np.sqrt(40))


def test_ridge_scaler_fits_only_each_training_interval(monkeypatch, synthetic_prepared):
    development = split_development_test(synthetic_prepared).development
    recorded = []
    original_fit = StandardScaler.fit

    def fit(self, X, y=None, **kwargs):
        result = original_fit(self, X, y, **kwargs)
        recorded.append((X.copy(deep=True), self.mean_.copy()))
        return result

    monkeypatch.setattr(StandardScaler, "fit", fit)
    ridge = Pipeline([("scaler", StandardScaler()), ("ridge", Ridge(solver="svd"))])
    compare_models(development, models={"ridge": ridge})
    folds = expanding_date_folds(development.metadata["日付"])
    assert len(recorded) == len(folds)
    for (fitted_X, fitted_mean), fold in zip(recorded, folds):
        expected_X = development.X.iloc[fold.train]
        pd.testing.assert_frame_equal(fitted_X, expected_X)
        np.testing.assert_allclose(fitted_mean, expected_X.mean().to_numpy())
        assert fitted_mean[0] < development.X.iloc[fold.validation]["大人"].mean()
    assert not hasattr(ridge.named_steps["scaler"], "mean_")


@pytest.mark.parametrize("column", ["案内時刻", "計算待ち時間", "予約件数_11時台", "待ち時間_分"])
def test_future_and_extra_features_are_rejected(synthetic_prepared, column):
    development = split_development_test(synthetic_prepared).development
    development.X[column] = 0
    with pytest.raises(ValueError):
        compare_models(development, models={"dummy": DummyRegressor()})


@pytest.mark.parametrize("change", [
    "missing_feature", "duplicate_feature", "reordered_features", "nonfinite_X",
    "nonnumeric_X", "nonfinite_y", "negative_y", "mismatched_index",
    "mismatched_length", "duplicate_source_position", "invalid_date",
])
def test_invalid_development_contracts_are_rejected(synthetic_prepared, change):
    data = split_development_test(synthetic_prepared).development
    X, y, metadata = data
    if change == "missing_feature":
        X = X.drop(columns="大人")
    elif change == "duplicate_feature":
        X.columns = ["子供", *X.columns[1:]]
    elif change == "reordered_features":
        X = X[list(reversed(X.columns))]
    elif change == "nonfinite_X":
        X.iloc[0, 0] = np.inf
    elif change == "nonnumeric_X":
        X["大人"] = X["大人"].astype(object)
        X.iloc[0, 0] = "not a number"
    elif change == "nonfinite_y":
        y.iloc[0] = np.nan
    elif change == "negative_y":
        y.iloc[0] = -1
    elif change == "mismatched_index":
        y = y.set_axis(range(len(y)))
    elif change == "mismatched_length":
        y = y.iloc[:-1]
    elif change == "duplicate_source_position":
        metadata.iloc[0, metadata.columns.get_loc("元行位置")] = metadata.iloc[1]["元行位置"]
    elif change == "invalid_date":
        metadata.iloc[0, metadata.columns.get_loc("日付")] = "unknown"
    with pytest.raises(ValueError):
        compare_models(ModelData(X, y, metadata), models={"dummy": DummyRegressor()})


def test_candidates_use_median_pipeline_and_bounded_seeded_forest():
    candidates = candidate_models(seed=123, include_lightgbm=False)
    models = list(candidates.models.values())
    assert len(models) == 3
    dummy = next(model for model in models if isinstance(model, DummyRegressor))
    assert dummy.strategy == "median"
    pipeline = next(model for model in models if isinstance(model, Pipeline))
    assert isinstance(pipeline.named_steps["scaler"], StandardScaler)
    assert isinstance(pipeline.named_steps["ridge"], Ridge)
    assert pipeline.named_steps["ridge"].random_state == 123
    forest = next(model for model in models if isinstance(model, RandomForestRegressor))
    assert forest.max_depth is not None and forest.max_depth <= 5
    assert forest.min_samples_leaf >= 5
    assert forest.random_state == 123
    assert forest.n_jobs == 1


def test_missing_lightgbm_is_explicitly_reported_without_installing(monkeypatch):
    monkeypatch.setitem(sys.modules, "lightgbm", None)
    candidates = candidate_models()
    assert len(candidates.models) == 3
    assert any("lgbm" in name.lower() or "lightgbm" in name.lower()
               for name in candidates.skipped)
    assert all(isinstance(reason, str) and reason for reason in candidates.skipped.values())


def test_same_seed_has_identical_synthetic_results(synthetic_prepared):
    development = split_development_test(synthetic_prepared).development
    models = candidate_models(seed=7, include_lightgbm=False).models
    first = compare_models(development, models=models, seed=7)
    second = compare_models(development, models=models, seed=7)
    pd.testing.assert_frame_equal(first.fold_metrics, second.fold_metrics)
    pd.testing.assert_frame_equal(first.summary, second.summary)
    pd.testing.assert_frame_equal(first.predictions, second.predictions)
    assert first.model_parameters == second.model_parameters
    assert first.predictions["model"].drop_duplicates().tolist() == list(models)
    for _, predictions in first.predictions.groupby("model", sort=False):
        assert predictions["position"].tolist() == sorted(predictions["position"])
