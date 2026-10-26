"""CLIの読込条件と公開レポートを合成データだけで確認する。"""

from datetime import date, timedelta
import json

import pytest

import model_evaluation
import step3_evaluate


def _unexpected_read(*args, **kwargs):
    pytest.fail("この経路でExcelを選択・読込してはいけません")


@pytest.fixture
def synthetic_source(monkeypatch, make_frame, tmp_path):
    dates = [date(2026, 1, 1) + timedelta(days=number) for number in range(14)]
    data = make_frame(*(
        {"日付": day.isoformat(), "呼び出し番号": 10000 + number * 2 + guest,
         "氏名": "合成データの氏名は非公開", "受付時間": f"{12 + guest}:00",
         "案内時刻": f"{12 + guest}:{10 + number:02d}",
         "待ち時間": 10 + number}
        for number, day in enumerate(dates) for guest in range(2)
    ))
    path = tmp_path / "synthetic.xlsx"
    calls = []

    def select(requested):
        assert requested == path
        calls.append("select")
        return path

    def load(selected):
        assert selected == path
        calls.append("load")
        return data

    def missing_lightgbm(name):
        assert name == "lightgbm"
        raise ModuleNotFoundError("synthetic optional dependency absent", name=name)

    monkeypatch.setattr(step3_evaluate, "select_excel_file", select)
    monkeypatch.setattr(step3_evaluate, "load_excel", load)
    monkeypatch.setattr(model_evaluation, "import_module", missing_lightgbm)
    return data, path, dates, calls


@pytest.mark.parametrize("arguments", [
    ["--check-dependencies"],
    ["does-not-exist.xlsx", "--check-dependencies"],
])
def test_dependency_check_does_not_select_or_read_excel(monkeypatch, capsys, arguments):
    monkeypatch.setattr(step3_evaluate, "select_excel_file", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "load_excel", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "candidate_models", _unexpected_read)
    assert step3_evaluate.main(arguments) == 0
    versions = json.loads(capsys.readouterr().out)
    assert versions["python"]
    assert {"numpy", "pandas", "openpyxl", "scikit-learn", "scipy", "lightgbm"} <= versions.keys()


def test_help_does_not_select_or_read_excel(monkeypatch, capsys):
    monkeypatch.setattr(step3_evaluate, "select_excel_file", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "load_excel", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "candidate_models", _unexpected_read)
    with pytest.raises(SystemExit) as error:
        step3_evaluate.main(["--help"])
    assert error.value.code == 0
    assert "--expected-test-dates" in capsys.readouterr().out


def test_evaluation_requires_explicit_excel_file(monkeypatch, capsys):
    monkeypatch.setattr(step3_evaluate, "select_excel_file", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "load_excel", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "candidate_models", _unexpected_read)
    with pytest.raises(SystemExit) as error:
        step3_evaluate.main([])
    assert error.value.code == 2
    assert "Excelファイルを明示指定" in capsys.readouterr().err


def test_dependency_error_is_reported_before_excel_is_read(monkeypatch, capsys):
    def missing_dependency(*, seed):
        raise ImportError("Step 3にはscikit-learnが必要です。")

    monkeypatch.setattr(step3_evaluate, "candidate_models", missing_dependency)
    monkeypatch.setattr(step3_evaluate, "select_excel_file", _unexpected_read)
    monkeypatch.setattr(step3_evaluate, "load_excel", _unexpected_read)
    with pytest.raises(SystemExit) as error:
        step3_evaluate.main(["does-not-exist.xlsx"])
    assert error.value.code == 2
    assert "scikit-learnが必要" in capsys.readouterr().err


def _assert_private_rows_are_not_reported(output):
    for value in ("合成データの氏名は非公開", "氏名", "呼び出し番号", "元行位置",
                  "source_position", "y_true", "y_pred", '"predictions"'):
        assert value not in output


def test_default_text_report_uses_three_real_models_and_reserves_final_test(synthetic_source, capsys):
    _, path, dates, calls = synthetic_source
    assert step3_evaluate.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert calls == ["select", "load"]
    for text in ("モデル比較", "fold別", "検証営業日別", "dummy", "ridge", "random_forest",
                 "未実行 lightgbm", "最終テストは未評価", "stdはddof=0"):
        assert text in output
    split_line = next(line for line in output.splitlines() if line.startswith("データ分割:"))
    split = json.loads(split_line.removeprefix("データ分割:").strip())
    assert split["development_rows"] == 24
    assert split["final_test_rows"] == 4
    assert split["final_test_dates"] == [day.isoformat() for day in dates[-2:]]
    assert split["final_test_evaluated"] is False
    _assert_private_rows_are_not_reported(output)


def test_json_report_has_only_aggregate_development_metrics(synthetic_source, capsys):
    _, path, dates, calls = synthetic_source
    arguments = [str(path), "--format", "json", "--expected-test-dates",
                 *(day.isoformat() for day in dates[-2:])]
    assert step3_evaluate.main(arguments) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert calls == ["select", "load"]
    assert report["split"]["source_rows"] == report["split"]["candidate_rows"] == 28
    assert report["split"]["final_test_dates"] == [day.isoformat() for day in dates[-2:]]
    assert report["split"]["final_test_evaluated"] is False
    assert report["configuration"]["expected_test_dates"] == [day.isoformat() for day in dates[-2:]]
    assert {row["model"] for row in report["summary"]} == {"dummy", "ridge", "random_forest"}
    assert all(row["folds"] == 3 for row in report["summary"])
    assert len(report["fold_metrics"]) == 9
    assert len(report["daily_metrics"]) == 18
    assert all(row["train_end"] < row["validation_start"]
               <= row["validation_end"] < dates[-2].isoformat()
               for row in report["fold_metrics"])
    assert all(row["date"] < dates[-2].isoformat() for row in report["daily_metrics"])
    assert "lightgbm" in report["skipped_models"]
    assert report["model_parameters"]["dummy"]["strategy"] == "median"
    _assert_private_rows_are_not_reported(output)


def test_mismatched_fixed_test_dates_fail_before_model_comparison(synthetic_source, monkeypatch, capsys):
    _, path, dates, calls = synthetic_source
    monkeypatch.setattr(step3_evaluate, "compare_models", _unexpected_read)
    with pytest.raises(SystemExit) as error:
        step3_evaluate.main([str(path), "--expected-test-dates",
                             *(day.isoformat() for day in dates[-3:-1])])
    assert error.value.code == 2
    assert calls == ["select", "load"]
    assert "最終テスト日が事前指定と一致しません" in capsys.readouterr().err


def test_explicit_1900_date_system_reaches_existing_preprocessing(synthetic_source, monkeypatch, capsys):
    data, path, dates, _ = synthetic_source
    data["日付"] = [(day - date(1899, 12, 30)).days for day in dates for _ in range(2)]
    prepare = step3_evaluate.prepare_training_data
    forwarded = []

    def spy_prepare(frame, *, excel_date_system):
        assert frame is data
        forwarded.append(excel_date_system)
        return prepare(frame, excel_date_system=excel_date_system)

    monkeypatch.setattr(step3_evaluate, "prepare_training_data", spy_prepare)
    assert step3_evaluate.main([str(path), "--excel-date-system", "1900", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert forwarded == ["1900"]
    assert report["configuration"]["excel_date_system"] == "1900"
    assert report["split"]["candidate_rows"] == 28
    assert report["split"]["final_test_dates"] == [day.isoformat() for day in dates[-2:]]
