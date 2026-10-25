import importlib
from pathlib import Path

import pandas as pd
import pytest

import step1_audit
from data_validation import REQUIRED_COLUMNS, audit_dataframe
from step1_audit import load_excel, main, select_excel_file


def test_import_does_not_discover_or_read_excel(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("import時にファイルへアクセスしています")

    monkeypatch.setattr(pd, "read_excel", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    importlib.reload(step1_audit)


def test_auto_selection_ignores_lock_files_and_directories(tmp_path):
    expected = tmp_path / "synthetic.XLSX"
    expected.touch()
    (tmp_path / "~$synthetic.xlsx").touch()
    (tmp_path / "notes.txt").touch()
    (tmp_path / "directory.xlsx").mkdir()
    assert select_excel_file(directory=tmp_path) == expected


def test_auto_selection_rejects_no_excel(tmp_path):
    (tmp_path / "~$locked.xlsx").touch()
    with pytest.raises(FileNotFoundError, match="Excelファイル"):
        select_excel_file(directory=tmp_path)


def test_multiple_files_require_explicit_selection(tmp_path):
    first = tmp_path / "a.xlsx"
    first.touch()
    (tmp_path / "b.xlsx").touch()
    with pytest.raises(ValueError, match="複数あります"):
        select_excel_file(directory=tmp_path)
    assert select_excel_file(first, directory=tmp_path) == first


@pytest.mark.parametrize("filename", ["data.csv", "~$locked.xlsx"])
def test_explicit_selection_rejects_unsupported_files(tmp_path, filename):
    path = tmp_path / filename
    path.touch()
    with pytest.raises(ValueError, match=".xlsx"):
        select_excel_file(path)


def test_explicit_selection_rejects_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="ありません"):
        select_excel_file(tmp_path / "missing.xlsx")


def test_excel_read_options_are_preserved(monkeypatch, make_frame, tmp_path):
    expected = make_frame()
    calls = []

    def fake_read_excel(path, **kwargs):
        calls.append((path, kwargs))
        return expected

    monkeypatch.setattr(pd, "read_excel", fake_read_excel)
    path = tmp_path / "synthetic.xlsx"
    assert load_excel(path) is expected
    assert calls == [(path, {
        "sheet_name": "Sheet1", "header": 1, "usecols": "B:U", "na_values": ["Null"],
    })]


@pytest.mark.parametrize("empty", [False, True])
def test_load_excel_checks_structure(monkeypatch, empty, tmp_path):
    frame = pd.DataFrame(columns=REQUIRED_COLUMNS if empty else ["別の列"])
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: frame)
    with pytest.raises(ValueError, match="データが0件" if empty else "必須列"):
        load_excel(tmp_path / "synthetic.xlsx")


def test_real_excel_loader_with_generated_workbook(make_frame, tmp_path):
    # B:Uの20列を持つ合成ブックのみを生成・読み込みする。
    data = make_frame({}, {"待ち時間": "Null", "案内時刻": "取消12:10"})
    for i in range(20 - len(data.columns)):
        data[f"未使用{i}"] = None
    path = tmp_path / "synthetic.xlsx"
    data.to_excel(path, sheet_name="Sheet1", startrow=1, startcol=1, index=False)
    loaded = load_excel(path)
    assert loaded.shape == (2, 20)
    assert loaded.loc[0, "人数"] == 3
    assert loaded.loc[1, "案内時刻"] == "取消12:10"
    assert pd.isna(loaded.loc[1, "待ち時間"])


@pytest.mark.parametrize("column,status_column", [
    ("人数", "人数検証結果"),
    ("受付時間", "受付時刻検証結果"),
    ("待ち時間", "記録待ち時間検証結果"),
])
def test_excel_na_inference_differs_from_direct_validation(make_frame, tmp_path, column, status_column):
    data = make_frame(*({column: value} for value in (None, "NaN", "通常の文字列")))
    direct = audit_dataframe(data)
    for i in range(20 - len(data.columns)):
        data[f"未使用{i}"] = None
    path = tmp_path / "synthetic_na.xlsx"
    data.to_excel(path, sheet_name="Sheet1", startrow=1, startcol=1, index=False)

    # load_excel経由で現行read_excel設定（keep_default_naは既定値）を検証する。
    loaded = load_excel(path)
    assert len(loaded) == 3
    assert pd.isna(loaded.loc[0, column])
    assert pd.isna(loaded.loc[1, column])
    assert loaded.loc[2, column] == "通常の文字列"
    assert audit_dataframe(loaded)[status_column].tolist() == [
        "missing", "missing", "conversion_error",
    ]
    assert direct[status_column].tolist() == [
        "missing", "conversion_error", "conversion_error",
    ]


def test_main_reports_synthetic_data(monkeypatch, make_frame, tmp_path, capsys):
    path = tmp_path / "synthetic.xlsx"
    path.touch()
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: make_frame(
        {}, {"人数": 4}, {"案内時刻": "取消12:10", "待ち時間": None},
    ))
    assert main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "全データ: 3" in output
    assert "暫定学習候補: 1" in output
    assert "学習候補外: 2" in output
    assert "キャンセル: 1" in output


def test_main_reports_file_errors_without_reading(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
    assert "Excelファイル" in capsys.readouterr().err


def test_main_reports_schema_errors(monkeypatch, tmp_path, capsys):
    path = tmp_path / "synthetic.xlsx"
    path.touch()
    monkeypatch.setattr(pd, "read_excel", lambda *args, **kwargs: pd.DataFrame({"人数": [3]}))
    with pytest.raises(SystemExit) as error:
        main([str(path)])
    assert error.value.code == 2
    assert "必須列" in capsys.readouterr().err
