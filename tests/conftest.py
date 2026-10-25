from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def make_frame():
    """実データに依存しない監査入力を作る。"""
    def make(*updates, index=None):
        base = {
            "日付": "2026-01-01",
            "呼び出し番号": 1,
            "人数": 3,
            "大人": 2,
            "子供": 1,
            "受付時間": "12:00",
            "案内時刻": "12:10",
            "待ち時間": "10分",
        }
        rows = [{**base, "呼び出し番号": i + 1, **update}
                for i, update in enumerate(updates or ({},))]
        return pd.DataFrame(rows, index=index)

    return make


@pytest.fixture(autouse=True)
def synthetic_excel_only(monkeypatch, tmp_path):
    """テストが元Excelを誤って読まないよう、実読込は一時領域内だけ許可する。"""
    read_excel = pd.read_excel

    def guarded_read_excel(io, *args, **kwargs):
        if isinstance(io, (str, Path)):
            assert Path(io).resolve().is_relative_to(tmp_path.resolve()), (
                "テストではtmp_pathで生成したExcelのみ読み込めます"
            )
        return read_excel(io, *args, **kwargs)

    monkeypatch.setattr(pd, "read_excel", guarded_read_excel)
