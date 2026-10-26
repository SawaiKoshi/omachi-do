"""最終評価は合成入力だけで検証する。元Excelは使用しない。"""

from datetime import date, timedelta
import json

import numpy as np
import pandas as pd
import pytest
from sklearn.base import BaseEstimator, RegressorMixin

import final_evaluation
from final_evaluation import evaluate_final_test, final_test_metrics
from model_evaluation import ModelCandidates, candidate_models, split_development_test
from preprocessing import BASE_FEATURE_COLUMNS, prepare_training_data
import step3_evaluate
import step3_final_test


class RecordingRegressor(RegressorMixin, BaseEstimator):
    fits = []
    predictions = []

    def fit(self, X, y):
        self.mean_ = X.mean().to_numpy()
        self.fits.append((self, X.copy(), y.copy(), self.mean_.copy()))
        return self

    def predict(self, X):
        self.predictions.append(X.copy())
        return X["大人"].to_numpy()


@pytest.fixture
def synthetic_raw(make_frame):
    # 206開発行＋20最終テスト行＋候補外7行。日付も件数も合成した実験用入力。
    train_days = [date(2026, 5, 1) + timedelta(days=3 * day) for day in range(12)]
    rows = []
    for day_number, day in enumerate([*train_days, *step3_final_test.EXPECTED_TEST_DATES]):
        count = (17 if day_number < 11 else 19) if day_number < 12 else 10
        for within in range(count):
            wait = 2 + (within % 8)
            rows.append({"日付": day, "大人": wait, "子供": 0, "人数": wait,
                         "受付時間": "12:00", "案内時刻": f"12:{wait:02d}",
                         "待ち時間": wait, "氏名": "合成氏名非公開"})
    rows += [{"日付": train_days[0], "案内時刻": "取消"} for _ in range(7)]
    raw = make_frame(*rows, index=[5] * len(rows))
    return raw.iloc[np.random.default_rng(7).permutation(len(raw))].copy()


@pytest.fixture
def prepared(synthetic_raw):
    return prepare_training_data(synthetic_raw)


@pytest.fixture
def recording_models(monkeypatch):
    RecordingRegressor.fits = []
    RecordingRegressor.predictions = []
    calls = []

    def candidates(*, seed, include_lightgbm):
        calls.append((seed, include_lightgbm))
        return ModelCandidates({name: RecordingRegressor()
                                for name in ("dummy", "ridge", "random_forest")}, {})

    monkeypatch.setattr(final_evaluation, "candidate_models", candidates)
    return calls


def test_metric_formulas_and_inclusive_exclusive_boundaries():
    metrics = final_test_metrics([0, 10, 20, 21], [0, 0, 0, 0])
    assert metrics == pytest.approx({"n": 4, "mae": 12.75, "rmse": np.sqrt(941 / 4),
                                    "within_10_minutes_pct": 50, "over_20_minutes_pct": 25})


@pytest.mark.parametrize("error,within,over", [
    (0, 100, 0), (10, 100, 0), (-10, 100, 0),
    (10.000001, 0, 0), (-10.000001, 0, 0),
    (20, 0, 0), (-20, 0, 0), (20.000001, 0, 100), (-20.000001, 0, 100),
])
def test_percentage_boundaries_use_absolute_unrounded_errors(error, within, over):
    result = final_test_metrics([50], [50 + error])
    assert result["within_10_minutes_pct"] == within
    assert result["over_20_minutes_pct"] == over


@pytest.mark.parametrize("actual,prediction", [
    ([], []), ([1], [1, 2]), ([[1]], [[1]]), ([np.nan], [0]),
    ([1], [np.inf]), ([-1], [0]), ([1], [[0]]),
])
def test_invalid_metric_inputs_are_rejected(actual, prediction):
    with pytest.raises(ValueError):
        final_test_metrics(actual, prediction)


def test_training_and_prediction_use_separate_rows_and_keep_order(prepared, recording_models):
    before = [part.copy(deep=True) for part in prepared]
    partition = split_development_test(prepared, expected_test_dates=step3_final_test.EXPECTED_TEST_DATES)
    result = evaluate_final_test(
        prepared, expected_test_dates=step3_final_test.EXPECTED_TEST_DATES,
        expected_development_rows=206, expected_test_rows=20, expected_development_days=12,
    )
    assert recording_models == [(42, False)]
    assert result.summary["model"].tolist() == ["random_forest", "dummy"]
    assert result.split["final_test_evaluated"] is True
    assert len(RecordingRegressor.fits) == 2
    assert len({id(row[0]) for row in RecordingRegressor.fits}) == 2
    assert set(partition.development.metadata["日付"]).isdisjoint(partition.final_test.metadata["日付"])
    for _, X, y, statistics in RecordingRegressor.fits:
        assert tuple(X.columns) == BASE_FEATURE_COLUMNS
        pd.testing.assert_frame_equal(X, partition.development.X)
        pd.testing.assert_series_equal(y, partition.development.y)
        np.testing.assert_allclose(statistics, partition.development.X.mean().to_numpy())
    for X in RecordingRegressor.predictions:
        pd.testing.assert_frame_equal(X, partition.final_test.X)
    for _, rows in result.predictions.groupby("model", sort=False):
        assert rows["source_position"].tolist() == partition.final_test.metadata["元行位置"].tolist()
        assert rows["date"].tolist() == partition.final_test.metadata["日付"].tolist()
        np.testing.assert_array_equal(rows["y_true"], partition.final_test.y)
        np.testing.assert_array_equal(rows["y_pred"], partition.final_test.X["大人"])
    assert result.daily_metrics["n"].tolist() == [10] * 4
    assert result.daily_metrics["mae"].tolist() == [0] * 4
    assert result.daily_metrics["within_10_minutes_pct"].tolist() == [100] * 4
    for original, copied in zip(prepared, before):
        if isinstance(original, pd.Series):
            pd.testing.assert_series_equal(original, copied)
        else:
            pd.testing.assert_frame_equal(original, copied)


def test_final_values_cannot_change_fit_statistics_or_selection(prepared, recording_models):
    evaluate_final_test(prepared, expected_test_dates=step3_final_test.EXPECTED_TEST_DATES)
    first_fits = [(X, y, statistics) for _, X, y, statistics in RecordingRegressor.fits]
    test_positions = np.flatnonzero(prepared.metadata["日付"].isin(step3_final_test.EXPECTED_TEST_DATES))
    prepared.X.iloc[test_positions, 0] += 1000
    prepared.y.iloc[test_positions] += 2000
    RecordingRegressor.fits = []
    result = evaluate_final_test(prepared, expected_test_dates=step3_final_test.EXPECTED_TEST_DATES)
    for (old_X, old_y, old_statistics), (_, X, y, statistics) in zip(first_fits, RecordingRegressor.fits):
        pd.testing.assert_frame_equal(X, old_X)
        pd.testing.assert_series_equal(y, old_y)
        np.testing.assert_array_equal(statistics, old_statistics)
    assert result.summary["model"].tolist() == ["random_forest", "dummy"]


@pytest.mark.parametrize("options", [
    {"expected_test_dates": (date(2026, 7, 11), date(2026, 8, 7))},
    {"expected_development_rows": 205}, {"expected_test_rows": 19},
    {"expected_development_days": 11},
])
def test_changed_experiment_plan_is_rejected_before_fit(prepared, recording_models, options):
    settings = {"expected_test_dates": step3_final_test.EXPECTED_TEST_DATES, **options}
    with pytest.raises(ValueError):
        evaluate_final_test(prepared, **settings)
    assert RecordingRegressor.fits == []
    assert recording_models == []


def test_real_models_reuse_exact_existing_parameters_and_match_direct_fit(prepared):
    result = evaluate_final_test(prepared, expected_test_dates=step3_final_test.EXPECTED_TEST_DATES)
    partition = split_development_test(prepared)
    models = candidate_models(seed=42, include_lightgbm=False).models
    for name in ("random_forest", "dummy"):
        model = models[name]
        assert result.model_parameters[name] == model.get_params(deep=True)
        model.fit(partition.development.X, partition.development.y)
        expected = model.predict(partition.final_test.X)
        rows = result.predictions.loc[result.predictions["model"].eq(name)]
        np.testing.assert_array_equal(rows["y_pred"], expected)
        metrics = final_test_metrics(partition.final_test.y, expected)
        summary = result.summary.loc[result.summary["model"].eq(name)].iloc[0]
        for metric, value in metrics.items():
            assert summary[metric] == pytest.approx(value)
        for day, day_rows in rows.groupby("date"):
            daily = result.daily_metrics.loc[result.daily_metrics["model"].eq(name)
                                             & result.daily_metrics["date"].eq(day)].iloc[0]
            for metric, value in final_test_metrics(day_rows["y_true"], day_rows["y_pred"]).items():
                assert daily[metric] == pytest.approx(value)


def _fail(*args, **kwargs):
    pytest.fail("この経路で読込・最終評価・選定・他モデルの学習を実行してはいけません")


@pytest.fixture
def cli_source(monkeypatch, synthetic_raw, tmp_path):
    path = tmp_path / "synthetic.xlsx"
    monkeypatch.setattr(step3_final_test, "select_excel_file", lambda requested: path)
    monkeypatch.setattr(step3_final_test, "load_excel", lambda selected: synthetic_raw)
    return path


@pytest.mark.parametrize("arguments", [["--check-dependencies"], ["--help"], []])
def test_safe_cli_paths_never_read_or_score(monkeypatch, capsys, arguments):
    for name in ("select_excel_file", "load_excel", "candidate_models", "evaluate_final_test"):
        monkeypatch.setattr(step3_final_test, name, _fail)
    if arguments == ["--check-dependencies"]:
        assert step3_final_test.main(arguments) == 0
        assert "lightgbm" in json.loads(capsys.readouterr().out)
    else:
        with pytest.raises(SystemExit) as error:
            step3_final_test.main(arguments)
        assert error.value.code == (0 if arguments else 2)


@pytest.mark.parametrize("format", ["json", "text"])
def test_dedicated_cli_reports_aggregates_for_only_fixed_models(cli_source, monkeypatch, capsys, format):
    monkeypatch.setattr(final_evaluation, "compare_models", _fail, raising=False)
    from sklearn.linear_model import Ridge
    monkeypatch.setattr(Ridge, "fit", _fail)
    import model_evaluation
    monkeypatch.setattr(model_evaluation, "import_module", _fail)
    assert step3_final_test.main([str(cli_source), "--excel-date-system", "1900", "--format", format]) == 0
    output = capsys.readouterr().out
    for private in ("合成氏名非公開", "氏名", "呼び出し番号", "source_position", '"predictions"', "y_true", "y_pred"):
        assert private not in output
    if format == "json":
        report = json.loads(output)
        assert report["split"]["development_rows"] == 206
        assert report["split"]["final_test_rows"] == 20
        assert report["split"]["source_rows"] == 233
        assert report["split"]["final_test_dates"] == ["2026-07-12", "2026-08-07"]
        assert report["configuration"]["seed"] == 42
        assert [row["model"] for row in report["summary"]] == ["random_forest", "dummy"]
        assert all(row["n"] == 20 for row in report["summary"])
        assert len(report["daily_metrics"]) == 4
        assert report["versions"]["scikit-learn"]
        assert "再選定" in report["interpretation"]
    else:
        for label in ("MAE（分）", "RMSE（分）", "10分以内（%）", "20分超（%）", "再選定"):
            assert label in output


def test_cli_checks_frozen_test_dates_before_any_fit(cli_source, synthetic_raw, monkeypatch, capsys):
    synthetic_raw.loc[synthetic_raw["日付"].eq(date(2026, 8, 7)), "日付"] = date(2026, 8, 8)
    monkeypatch.setattr(final_evaluation, "candidate_models", _fail)
    with pytest.raises(SystemExit) as error:
        step3_final_test.main([str(cli_source)])
    assert error.value.code == 2
    assert "最終テスト日が事前指定と一致しません" in capsys.readouterr().err


def test_cli_has_no_model_seed_or_tuning_override(cli_source):
    for arguments in (["--seed", "7"], ["--model", "ridge"], ["--final-test-days", "1"]):
        with pytest.raises(SystemExit) as error:
            step3_final_test.main([str(cli_source), *arguments])
        assert error.value.code == 2


def test_regular_comparison_cli_never_calls_final_evaluation(synthetic_raw, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(final_evaluation, "evaluate_final_test", _fail)
    monkeypatch.setattr(step3_final_test, "evaluate_final_test", _fail)
    monkeypatch.setattr(step3_evaluate, "select_excel_file", lambda path: path)
    monkeypatch.setattr(step3_evaluate, "load_excel", lambda path: synthetic_raw)
    # 実LightGBMの学習時間に依存しない。通常CLIの比較自体は実際の3モデルで通す。
    candidates = candidate_models
    import model_evaluation
    monkeypatch.setattr(model_evaluation, "candidate_models", lambda **kw: candidates(include_lightgbm=False, **kw))
    monkeypatch.setattr(step3_evaluate, "candidate_models", lambda **kw: candidates(include_lightgbm=False, **kw))
    assert step3_evaluate.main([str(tmp_path / "synthetic.xlsx"), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["split"]["final_test_evaluated"] is False
    assert "within_10_minutes_pct" not in report["summary"][0]
    assert all(row["validation_end"] < "2026-07-12" for row in report["fold_metrics"])
