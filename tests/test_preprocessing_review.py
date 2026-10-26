"""最終レビューで不足していたExcelシリアルとX/y分離の確認だけを追加。"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from preprocessing import build_features, parse_date, prepare_training_data, split_by_date


@pytest.mark.parametrize("system,serial,expected", [
    ("1900", 1, date(1900, 1, 1)),
    ("1900", 59, date(1900, 2, 28)),
    ("1900", 61, date(1900, 3, 1)),
    ("1900", 36585, date(2000, 2, 29)),
    ("1900", 40729, date(2011, 7, 5)),
    ("1900", 2958465, date(9999, 12, 31)),
    ("1904", 0, date(1904, 1, 1)),
    ("1904", 1, date(1904, 1, 2)),
    ("1904", 39267, date(2011, 7, 5)),
    ("1904", 2957003, date(9999, 12, 31)),
    ("1900", np.float64(40729), date(2011, 7, 5)),
    ("1904", np.int64(39267), date(2011, 7, 5)),
])
def test_excel_serial_dates_with_explicit_system(system, serial, expected):
    assert parse_date(serial, excel_date_system=system) == (expected, "valid")


@pytest.mark.parametrize("system,serial,status", [
    ("1900", 0, "out_of_range"),
    ("1900", -1, "out_of_range"),
    ("1900", 60, "invalid_date"),
    ("1900", 40729.5, "time_component"),
    ("1900", 2958466, "out_of_range"),
    ("1904", -1, "out_of_range"),
    ("1904", 39267.5, "time_component"),
    ("1904", 2957004, "out_of_range"),
    ("1900", float("inf"), "non_finite"),
    ("1904", float("-inf"), "non_finite"),
    ("1900", 10**1000, "out_of_range"),
    ("1900", True, "invalid_type"),
    ("1904", np.bool_(False), "invalid_type"),
    ("1900", "40729", "conversion_error"),
])
def test_excel_serial_rejects_unrepresentable_dates(system, serial, status):
    assert parse_date(serial, excel_date_system=system) == (None, status)


@pytest.mark.parametrize("missing", [None, float("nan"), pd.NA])
def test_excel_serial_missing_values_stay_missing(missing):
    assert parse_date(missing, excel_date_system="1900") == (None, "missing")


def test_excel_date_system_is_not_guessed_or_silently_misspelled():
    assert parse_date(40729) == (None, "invalid_type")
    with pytest.raises(ValueError, match="excel_date_system"):
        parse_date(40729, excel_date_system="1901")


@pytest.mark.parametrize("system,first_serial", [("1900", 40729), ("1904", 39267)])
def test_serial_dates_flow_through_features_training_and_split(make_frame, system, first_serial):
    dates = [first_serial + offset for offset in (2, 0, 1, 0)]
    data = make_frame(*({"日付": day} for day in dates), index=[7] * 4)
    before = data.copy(deep=True)
    result = prepare_training_data(data, excel_date_system=system)
    native = make_frame(*({"日付": day} for day in (
        "2011-07-07", "2011-07-05", "2011-07-06", "2011-07-05",
    )), index=[7] * 4)
    pd.testing.assert_frame_equal(result.X, build_features(native))
    pd.testing.assert_frame_equal(result.X, build_features(data, excel_date_system=system))
    assert result.y.tolist() == [10] * 4
    parts = split_by_date(result.metadata["日付"],
                          train_end="2011-07-05", validation_end="2011-07-06")
    assert [part.tolist() for part in parts] == [[1, 3], [2], [0]]
    pd.testing.assert_frame_equal(data, before)


def test_invalid_serials_are_reported_without_changing_step1_candidates(make_frame):
    data = make_frame(*({"日付": day} for day in (40729, 60, None, 40729.5, -1)))
    result = prepare_training_data(data, excel_date_system="1900")
    assert result.diagnostics["学習候補"].tolist() == [True] * 5
    assert result.diagnostics["日付検証結果"].tolist() == [
        "valid", "invalid_date", "missing", "time_component", "out_of_range",
    ]
    assert result.metadata["元行位置"].tolist() == [0]
    assert len(result.X) == len(result.y) == 1


def test_changed_valid_outcome_changes_y_but_not_X(make_frame):
    initial = prepare_training_data(make_frame())
    changed = prepare_training_data(make_frame({"案内時刻": "12:20", "待ち時間": "20分"}))
    pd.testing.assert_frame_equal(initial.X, changed.X)
    pd.testing.assert_frame_equal(initial.metadata, changed.metadata)
    assert initial.y.tolist() == [10]
    assert changed.y.tolist() == [20]
    # 不一致なら行の適格性が変わる。Xの変換規則への混入とは区別する。
    inconsistent = prepare_training_data(make_frame({"待ち時間": "20分"}))
    assert inconsistent.X.empty
    assert inconsistent.diagnostics["待ち時間検証結果"].tolist() == ["wait_mismatch"]
