"""リファクタ前の判定式を合成データで照合する。実データの件数は固定しない。"""

import math
from itertools import product

import numpy as np
import pandas as pd
import pytest

from data_validation import audit_dataframe, summarize_audit, validate_people
from time_utils import to_minutes


def legacy_scalar_people_valid(total, adults, children):
    """旧validate_peopleがvalidを返す条件。比較用としてテスト内だけに保持。"""
    numbers = []
    for value in (total, adults, children):
        if value is None or isinstance(value, bool):
            return False
        if isinstance(value, str) and not value.strip():
            return False
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        if not math.isfinite(number):
            return False
        numbers.append(number)
    return (
        all(number.is_integer() for number in numbers)
        and numbers[0] >= 1 and numbers[1] >= 0 and numbers[2] >= 0
        and numbers[0] == numbers[1] + numbers[2]
    )


def legacy_masks(data):
    """旧スクリプトの人数・待ち時間・学習候補の式を固定する。"""
    counts = data[["人数", "大人", "子供"]].apply(pd.to_numeric, errors="coerce")
    values = counts.to_numpy(dtype=float, na_value=np.nan)
    people_valid = pd.Series(
        np.isfinite(values).all(axis=1)
        & (values >= 0).all(axis=1)
        & (values == np.floor(values)).all(axis=1)
        & (values[:, 0] >= 1),
        index=data.index,
    )
    people_mismatch = counts.notna().all(axis=1) & counts["人数"].ne(counts["大人"] + counts["子供"])
    old_people_valid = counts.notna().all(axis=1) & people_valid & ~people_mismatch
    scalar_people_valid = pd.Series(
        [legacy_scalar_people_valid(*row) for row in data[["人数", "大人", "子供"]].itertuples(index=False, name=None)],
        index=data.index,
    )
    reception = data["受付時間"].map(to_minutes)
    seated = data["案内時刻"].map(to_minutes)
    calculated = (seated - reception).round()
    recorded = pd.to_numeric(
        data["待ち時間"].astype("string").str.replace("分", "", regex=False).str.strip(),
        errors="coerce",
    )
    cancelled = data["案内時刻"].astype("string").str.strip().str.startswith("取消").fillna(False)
    comparable = reception.notna() & seated.notna() & recorded.notna()
    wait_mismatch = comparable & recorded.ne(calculated)
    wait_values = recorded.to_numpy(dtype=float, na_value=np.nan)
    invalid_wait = pd.Series(~np.isfinite(wait_values) | (wait_values < 0), index=data.index)
    candidate = (
        ~cancelled & old_people_valid & scalar_people_valid & comparable
        & ~invalid_wait & ~calculated.lt(0).fillna(False) & ~wait_mismatch
    )
    return {
        "旧人数valid": old_people_valid,
        "旧関数人数valid": scalar_people_valid,
        "人数不一致": people_mismatch,
        "待ち時間不一致": wait_mismatch,
        "キャンセル": cancelled,
        "学習候補": candidate,
    }


def test_known_exclusion_patterns_with_synthetic_rows(make_frame):
    # 正常3行、人数不一致1行、待ち時間不一致4行、キャンセル2行。
    data = make_frame(
        {}, {}, {}, {"人数": 4},
        *({"待ち時間": f"{minutes}分"} for minutes in (11, 12, 13, 14)),
        {"案内時刻": "取消21:10", "待ち時間": None},
        {"案内時刻": "取消21:20", "待ち時間": None},
    )
    expected = legacy_masks(data)
    actual = audit_dataframe(data)
    for column in ("人数不一致", "待ち時間不一致", "キャンセル", "学習候補"):
        assert actual[column].tolist() == expected[column].tolist()
    people_valid = actual["人数検証結果"].eq("valid")
    assert people_valid.tolist() == expected["旧人数valid"].tolist()
    assert people_valid.tolist() == expected["旧関数人数valid"].tolist()
    assert summarize_audit(actual) == {
        "全データ": 10, "暫定学習候補": 3, "学習候補外": 7,
        "人数不一致": 1, "待ち時間不一致": 4, "キャンセル": 2, "重複行": 0,
    }


@pytest.mark.parametrize("total,adults,children", list(product(
    [3, "3", 0, -1, 2.5, None, "不明", float("inf"), True],
    [2, 0, -1, "2", None],
    [1, 0, -1, "1", None],
)))
def test_people_candidate_matches_both_legacy_checks(make_frame, total, adults, children):
    data = make_frame({"人数": total, "大人": adults, "子供": children})
    expected = legacy_masks(data)
    valid = validate_people(total, adults, children) == "valid"
    assert valid == bool(expected["旧人数valid"].iloc[0] & expected["旧関数人数valid"].iloc[0])


@pytest.mark.parametrize("update", [
    {}, {"待ち時間": 10}, {"待ち時間": " 10 分 "},
    {"待ち時間": ""}, {"待ち時間": None}, {"待ち時間": "不明"},
    {"待ち時間": -10}, {"待ち時間": float("inf")}, {"待ち時間": "11分"},
    {"受付時間": None}, {"受付時間": "不明"},
    {"案内時刻": None}, {"案内時刻": "不明"},
    {"案内時刻": "取消21:10", "待ち時間": None},
    {"案内時刻": "取消21:10", "待ち時間": "10分"},
    {"案内時刻": "11:50", "待ち時間": -10},
    {"受付時間": 0.5, "案内時刻": 0.5, "待ち時間": 0},
    {"案内時刻": "12:00:30", "待ち時間": 0},
    {"案内時刻": "12:01:30", "待ち時間": 2},
])
def test_learning_candidate_matches_legacy_formula(make_frame, update):
    data = make_frame({}, update)
    expected = legacy_masks(data)
    actual = audit_dataframe(data)
    assert actual["学習候補"].tolist() == expected["学習候補"].tolist()
    assert actual["待ち時間不一致"].tolist() == expected["待ち時間不一致"].tolist()


@pytest.mark.parametrize("update", [
    {"受付時間": "23:01", "案内時刻": "23:11", "待ち時間": 10},
    {"受付時間": "12:00:20", "案内時刻": "12:00:10", "待ち時間": 0},
])
def test_intentional_boundary_tightening(make_frame, update):
    data = make_frame(update)
    assert legacy_masks(data)["学習候補"].iloc[0]
    assert not audit_dataframe(data)["学習候補"].iloc[0]
