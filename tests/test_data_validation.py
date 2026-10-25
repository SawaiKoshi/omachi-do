import numpy as np
import pandas as pd
import pytest

from data_validation import (
    REQUIRED_COLUMNS,
    audit_dataframe,
    is_cancelled,
    summarize_audit,
    validate_people,
)


@pytest.mark.parametrize(
    "total, adults, children, expected",
    [
        (3, 2, 1, "valid"),
        (3, 2, 2, "total_mismatch"),
        (-1, -1, 0, "out_of_range"),
        (2, 1.5, 0.5, "non_integer"),
        (0, 0, 0, "out_of_range"),
        ("不明", 2, 1, "conversion_error"),
        (None, 2, 1, "missing"),
        (float("inf"), 2, 1, "non_finite"),
        (True, 1, 0, "invalid_type"),
        ("3", 2, 1, "valid"),
        (1, 0, 1, "valid"),
        (1, 1, 0, "valid"),
        (3.0, 2.0, 1.0, "valid"),
        (" 3 ", "2", "1", "valid"),
        ("3e0", "2.0", "1.0", "valid"),
        (3, -1, 4, "out_of_range"),
        (3, 4, -1, "out_of_range"),
        (float("-inf"), 2, 1, "non_finite"),
        ("NaN", 2, 1, "conversion_error"),
        ("1_0", 9, 1, "conversion_error"),
        (3, "不明", None, "conversion_error"),
        (None, "不明", 1, "missing"),
    ]
)
def test_validate_people(total, adults, children, expected):
    assert validate_people(total, adults, children) == expected


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("missing", [None, float("nan"), pd.NA, pd.NaT, "", " \t "])
def test_missing_people_in_each_position(position, missing):
    values = [3, 2, 1]
    values[position] = missing
    assert validate_people(*values) == "missing"


@pytest.mark.parametrize("position", range(3))
@pytest.mark.parametrize("boolean", [True, False, np.bool_(True), np.bool_(False)])
def test_people_reject_booleans(position, boolean):
    values = [3, 2, 1]
    values[position] = boolean
    assert validate_people(*values) == "invalid_type"


@pytest.mark.parametrize("value,expected", [
    ("取消", True),
    ("取消21:10", True),
    (" \t取消21:10 ", True),
    ("取消済", True),
    ("12:10", False),
    ("受付取消", False),
    ("キャンセル", False),
    (None, False),
    (pd.NA, False),
    (float("nan"), False),
    ("", False),
    (123, False),
])
def test_cancel_classification(value, expected):
    assert is_cancelled(value) is expected


@pytest.mark.parametrize("recorded,expected_status,has_wait", [
    (None, "missing", False),
    (pd.NA, "missing", False),
    ("  ", "missing", False),
    ("10分", "valid", True),
    ("不明", "conversion_error", True),
    (-1, "out_of_range", True),
])
def test_cancel_is_separate_from_conversion_errors(make_frame, recorded, expected_status, has_wait):
    result = audit_dataframe(make_frame({"案内時刻": "取消21:10", "待ち時間": recorded})).iloc[0]
    assert result["待ち時間検証結果"] == "cancelled"
    assert result["案内時刻検証結果"] == "cancelled"
    assert result["記録待ち時間検証結果"] == expected_status
    assert bool(result["キャンセル待ち時間あり"]) is has_wait
    assert not result["学習候補"]
    assert not result["待ち時間不一致"]


@pytest.mark.parametrize("update,field,status", [
    ({"人数": None}, "人数検証結果", "missing"),
    ({"人数": "不明"}, "人数検証結果", "conversion_error"),
    ({"人数": 0}, "人数検証結果", "out_of_range"),
    ({"人数": 4}, "人数検証結果", "total_mismatch"),
    ({"大人": 1.5}, "人数検証結果", "non_integer"),
    ({"子供": True}, "人数検証結果", "invalid_type"),
    ({"人数": float("inf")}, "人数検証結果", "non_finite"),
    ({"受付時間": None}, "受付時刻検証結果", "missing"),
    ({"受付時間": "不明"}, "受付時刻検証結果", "conversion_error"),
    ({"案内時刻": pd.NA}, "案内時刻検証結果", "missing"),
    ({"案内時刻": "不明"}, "案内時刻検証結果", "conversion_error"),
    ({"案内時刻": 1.0}, "案内時刻検証結果", "out_of_range"),
    ({"待ち時間": ""}, "記録待ち時間検証結果", "missing"),
    ({"待ち時間": "不明"}, "記録待ち時間検証結果", "conversion_error"),
    ({"待ち時間": "NaN"}, "記録待ち時間検証結果", "conversion_error"),
    ({"待ち時間": "-10分"}, "記録待ち時間検証結果", "out_of_range"),
    ({"待ち時間": float("inf")}, "記録待ち時間検証結果", "non_finite"),
    ({"待ち時間": True}, "記録待ち時間検証結果", "invalid_type"),
    ({"待ち時間": "11分"}, "待ち時間検証結果", "wait_mismatch"),
])
def test_learning_candidates_exclude_each_error(make_frame, update, field, status):
    result = audit_dataframe(make_frame({}, update))
    assert result.loc[0, "学習候補"]
    assert result.loc[1, field] == status
    assert not result.loc[1, "学習候補"]
    assert not result["学習候補"].isna().any()


@pytest.mark.parametrize("reception,seated,recorded,calculated", [
    ("12:00", "11:50", -10, -10),
    ("12:00:20", "12:00:10", 0, 0),
    ("22:50", "00:10", 80, -1360),
])
def test_reversed_times_are_not_corrected(make_frame, reception, seated, recorded, calculated):
    data = make_frame({"受付時間": reception, "案内時刻": seated, "待ち時間": recorded})
    result = audit_dataframe(data).iloc[0]
    assert result["時刻逆転"]
    assert result["計算待ち時間"] == calculated
    assert result["受付時間"] == reception
    assert result["案内時刻"] == seated
    assert not result["学習候補"]


@pytest.mark.parametrize("seated,wait,expected_status,candidate", [
    ("23:00", 10, "valid", True),
    ("23:00:01", 10, "out_of_range", False),
    ("23:10", 20, "out_of_range", False),
])
def test_closing_time_boundary(make_frame, seated, wait, expected_status, candidate):
    result = audit_dataframe(make_frame({
        "受付時間": "22:50", "案内時刻": seated, "待ち時間": wait,
    })).iloc[0]
    assert result["案内時刻検証結果"] == expected_status
    assert bool(result["学習候補"]) is candidate


def test_reception_after_closing_is_also_out_of_range(make_frame):
    result = audit_dataframe(make_frame({
        "受付時間": "23:01", "案内時刻": "23:01", "待ち時間": 0,
    })).iloc[0]
    assert result["受付時刻検証結果"] == "out_of_range"
    assert not result["学習候補"]


@pytest.mark.parametrize("seated,rounded", [
    ("12:00:00", 0), ("12:00:30", 0), ("12:00:59", 1),
    ("12:01:30", 2), ("12:02:30", 2),
])
def test_existing_wait_rounding_is_preserved(make_frame, seated, rounded):
    result = audit_dataframe(make_frame({"案内時刻": seated, "待ち時間": rounded})).iloc[0]
    assert result["計算待ち時間"] == rounded
    assert result["学習候補"]


@pytest.mark.parametrize("index", [[10, 30], [10, 10]])
def test_audit_preserves_input_rows_and_index(make_frame, index):
    data = make_frame({}, {"人数": 4}, index=index)
    before = data.copy(deep=True)
    result = audit_dataframe(data)
    pd.testing.assert_frame_equal(data, before)
    pd.testing.assert_frame_equal(result[data.columns], before)
    assert result["学習候補"].tolist() == [True, False]
    assert result.index.tolist() == index


def test_duplicate_rows_are_reported_but_not_automatically_excluded(make_frame):
    result = audit_dataframe(make_frame({"呼び出し番号": 7}, {"呼び出し番号": 7}))
    assert result["重複"].tolist() == [True, True]
    assert result["学習候補"].tolist() == [True, True]


def test_summary_uses_actual_masks_and_counts_overlapping_reasons(make_frame):
    result = audit_dataframe(make_frame({}, {"人数": 4, "待ち時間": "11分"}))
    assert summarize_audit(result) == {
        "全データ": 2, "暫定学習候補": 1, "学習候補外": 1,
        "人数不一致": 1, "待ち時間不一致": 1, "キャンセル": 0, "重複行": 0,
    }


def test_scalar_and_dataframe_people_checks_agree(make_frame):
    data = make_frame({}, {"人数": None}, {"人数": 4}, {"子供": -1})
    result = audit_dataframe(data)
    expected = [validate_people(*row) for row in data[["人数", "大人", "子供"]].itertuples(index=False, name=None)]
    assert result["人数検証結果"].tolist() == expected


@pytest.mark.parametrize("missing_column", REQUIRED_COLUMNS)
def test_required_columns_are_checked(make_frame, missing_column):
    with pytest.raises(ValueError, match=f"必須列がありません: {missing_column}"):
        audit_dataframe(make_frame().drop(columns=missing_column))


def test_empty_data_is_rejected():
    with pytest.raises(ValueError, match="データが0件"):
        audit_dataframe(pd.DataFrame(columns=REQUIRED_COLUMNS))


def test_duplicate_column_names_are_rejected(make_frame):
    data = make_frame()
    data = pd.concat([data, data[["人数"]]], axis=1)
    with pytest.raises(ValueError, match="列名が重複しています: 人数"):
        audit_dataframe(data)
