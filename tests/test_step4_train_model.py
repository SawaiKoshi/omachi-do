from datetime import date, timedelta
import json

import pandas as pd
import pytest

from model_artifact import load_model
import step4_train_model as cli


def synthetic_excel_frame():
    dates = [date(2026, 5, 24) + timedelta(days=i) for i in range(13)] + [date(2026, 8, 7)]
    return pd.DataFrame([{
        "日付": dates[i % 14], "呼び出し番号": i + 1, "人数": 2, "大人": 2, "子供": 0,
        "受付時間": "12:00", "案内時刻": "12:10", "待ち時間": "10分",
    } for i in range(226)])


@pytest.mark.parametrize("arguments", [[], ["private.xlsx"], ["private.xlsx", "--model-version", "v1"],
    ["private.xlsx", "--confirm-full-training"], ["--help"], ["--check-dependencies"]])
def test_help_dependency_check_and_unconfirmed_training_never_open_excel(monkeypatch, arguments, capsys):
    monkeypatch.setattr(cli, "select_excel_file", lambda *a, **k: pytest.fail("must not select Excel"))
    monkeypatch.setattr(cli, "load_excel", lambda *a, **k: pytest.fail("must not load Excel"))
    monkeypatch.setattr(cli, "train_model", lambda *a, **k: pytest.fail("must not fit"))
    if arguments == ["--check-dependencies"]:
        assert cli.main(arguments) == 0
        assert "scikit-learn" in json.loads(capsys.readouterr().out)
    else:
        with pytest.raises(SystemExit) as error:
            cli.main(arguments)
        assert error.value.code == (0 if arguments == ["--help"] else 2)


def test_cli_refits_all_226_synthetic_rows_without_evaluating(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "select_excel_file", lambda path: path)
    monkeypatch.setattr(cli, "load_excel", lambda path: synthetic_excel_frame())
    original_prepare = cli.prepare_training_data
    seen = []

    def prepare(data, **kwargs):
        seen.append(kwargs)
        return original_prepare(data, **kwargs)

    monkeypatch.setattr(cli, "prepare_training_data", prepare)
    assert cli.main(["private.xlsx", "--confirm-full-training", "--model-version", "synthetic-v1",
                     "--output-dir", str(tmp_path)]) == 0
    info = load_model(tmp_path / "random-forest-synthetic-v1.joblib")["metadata"]
    assert info["training"]["rows"] == 226 and info["training"]["days"] == 14
    assert info["training"]["source"] == "confirmed_step3_local_excel"
    assert seen == [{"excel_date_system": "1900"}]
    result = json.loads(capsys.readouterr().out)
    assert result["evaluation_executed"] is False and result["automatic_retraining"] is False
    assert "private.xlsx" not in str(result)


@pytest.mark.parametrize("change", ["rows", "days", "start", "end"])
def test_cli_refuses_changed_candidate_contract_before_fit(monkeypatch, tmp_path, change, capsys):
    frame = synthetic_excel_frame()
    if change == "rows":
        frame = frame.iloc[:-1]
    elif change == "days":
        frame.loc[frame["日付"].eq(date(2026, 5, 25)), "日付"] = date(2026, 5, 24)
    elif change == "start":
        frame.loc[frame["日付"].eq(date(2026, 5, 24)), "日付"] = date(2026, 5, 23)
    else:
        frame.loc[frame["日付"].eq(date(2026, 8, 7)), "日付"] = date(2026, 8, 8)
    monkeypatch.setattr(cli, "select_excel_file", lambda path: path)
    monkeypatch.setattr(cli, "load_excel", lambda path: frame)
    monkeypatch.setattr(cli, "train_model", lambda *a, **k: pytest.fail("fit must not run"))
    with pytest.raises(SystemExit) as error:
        cli.main(["private.xlsx", "--confirm-full-training", "--model-version", "synthetic-v1",
                  "--output-dir", str(tmp_path)])
    assert error.value.code == 2
    assert list(tmp_path.iterdir()) == []
    assert "private.xlsx" not in capsys.readouterr().err


def test_cli_hides_reader_exception_containing_private_information(monkeypatch, capsys):
    monkeypatch.setattr(cli, "select_excel_file", lambda path: path)

    def fail(path):
        raise ValueError("private-customer-name-and-file")

    monkeypatch.setattr(cli, "load_excel", fail)
    with pytest.raises(SystemExit):
        cli.main(["private.xlsx", "--confirm-full-training", "--model-version", "synthetic-v1"])
    error = capsys.readouterr().err
    assert "private-customer-name-and-file" not in error and "private.xlsx" not in error


def test_existing_version_is_rejected_before_excel_read(monkeypatch, tmp_path):
    path = tmp_path / "random-forest-synthetic-v1.joblib"
    path.write_bytes(b"existing")
    monkeypatch.setattr(cli, "load_excel", lambda *a, **k: pytest.fail("must not read"))
    with pytest.raises(SystemExit):
        cli.main(["private.xlsx", "--confirm-full-training", "--model-version", "synthetic-v1",
                  "--output-dir", str(tmp_path)])
    assert path.read_bytes() == b"existing"
