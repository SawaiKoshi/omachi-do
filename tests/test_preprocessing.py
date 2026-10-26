"""生Excelにアクセスせず、予測時の情報制約と日付境界を検証する。"""

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from preprocessing import (
    BASE_FEATURE_COLUMNS, RECEPTION_COLUMNS, WEATHER_CATEGORIES,
    build_features, parse_date, prepare_training_data, split_by_date,
)


@pytest.mark.parametrize("value", [
    "2026-01-05", " 2026/1/5 ", date(2026, 1, 5), datetime(2026, 1, 5),
    pd.Timestamp("2026-01-05"), np.datetime64("2026-01-05"),
])
def test_explicit_calendar_dates(value):
    assert parse_date(value) == (date(2026, 1, 5), "valid")


@pytest.mark.parametrize("value,status", [
    (None, "missing"), (pd.NaT, "missing"), (pd.NA, "missing"),
    (np.datetime64("NaT", "D"), "missing"), (" ", "missing"),
    ("1/5", "conversion_error"), ("01/05/2026", "conversion_error"),
    ("2026-02-29", "conversion_error"), ("2026-1/5", "conversion_error"),
    ("2026-01-05 12:00", "conversion_error"),
    ("2026年1月5日", "conversion_error"),
    (46000, "invalid_type"), (True, "invalid_type"),
    (datetime(2026, 1, 5, 12), "time_component"),
    (pd.Timestamp("2026-01-05 00:00:00.000000001"), "time_component"),
    (datetime(2026, 1, 5, tzinfo=timezone.utc), "timezone_aware"),
])
def test_dates_are_not_guessed_or_truncated(value, status):
    assert parse_date(value) == (None, status)


def test_reception_only_inputs_and_fixed_weekday_columns(make_frame):
    data = make_frame({"日付": "2026-01-05", "受付時間": "12:00:30", "曜日": "日"})
    X = build_features(data[list(RECEPTION_COLUMNS)])
    assert tuple(X.columns) == BASE_FEATURE_COLUMNS
    assert X.loc[0, "受付時刻_分"] == 720.5
    assert X.loc[0, "大人"] == 2
    assert X.loc[0, "子供"] == 1
    assert X.filter(like="曜日_").sum(axis=1).tolist() == [1]
    assert X.loc[0, "曜日_月"] == 1
    assert np.isfinite(X.to_numpy()).all()


def test_future_outcomes_reservations_identifiers_and_dates_cannot_enter_X(make_frame):
    data = make_frame({"日付": "2026-01-05"})
    before = build_features(data)
    blocked = [
        *(f"予約件数_{hour}時台" for hour in (11, 13, 17, 18, 19, 20)),
        "当日予約追加後か", "案内時刻", "待ち時間", "記録待ち時間", "計算待ち時間",
        "キャンセル", "学習候補", "呼び出し番号", "氏名", "曜日", "天気",
    ]
    for column in blocked:
        data[column] = "受付時点では不明"
    # 一週間後は日付が違うが曜日は同じ。日付そのものも特徴量に入らない。
    data["日付"] = "2026-01-12"
    pd.testing.assert_frame_equal(build_features(data), before)


def test_weather_requires_opt_in_and_is_fixed_even_for_unseen_values(make_frame):
    data = make_frame(*({"天気": item} for item in (" 晴れ ", None, "雷雨", "雨")))
    assert not any(column.startswith("天気_") for column in build_features(data))
    X = build_features(data, weather_at_reception=True)
    assert X.filter(like="天気_").columns.tolist() == [f"天気_{v}" for v in WEATHER_CATEGORIES]
    assert X.filter(like="天気_").sum(axis=1).tolist() == [1, 1, 1, 1]
    assert X["天気_不明"].tolist() == [0, 1, 0, 0]
    assert X["天気_その他"].tolist() == [0, 0, 1, 0]
    future = build_features(make_frame({"天気": "雪"}), weather_at_reception=True)
    assert X.columns.equals(future.columns)
    with pytest.raises(ValueError, match="必須列.*天気"):
        build_features(make_frame(), weather_at_reception=True)
    with pytest.raises(ValueError, match="天気は文字列"):
        build_features(make_frame({"天気": True}), weather_at_reception=True)


@pytest.mark.parametrize("update,reason", [
    ({"日付": "1/5"}, "日付=conversion_error"),
    ({"人数": 4}, "人数=total_mismatch"),
    ({"子供": True}, "人数=invalid_type"),
    ({"大人": None}, "人数=missing"),
    ({"受付時間": "23:00:01"}, "受付時間=out_of_range"),
    ({"受付時間": None}, "受付時間=missing"),
])
def test_invalid_prediction_input_is_rejected_without_echoing_values(make_frame, update, reason):
    with pytest.raises(ValueError, match=f"行位置0: {reason}"):
        build_features(make_frame(update))


def test_numeric_strings_and_excel_time_remain_supported(make_frame):
    native = make_frame({"受付時間": "12:00"})
    excel = make_frame({"人数": "3", "大人": "2", "子供": "1", "受付時間": 0.5})
    pd.testing.assert_frame_equal(build_features(native), build_features(excel))


def test_training_exclusions_alignment_and_unchanged_input(make_frame):
    data = make_frame(
        {"案内時刻": "12:01:30", "待ち時間": 2},
        {"日付": None},
        {"案内時刻": "取消", "待ち時間": None},
        {"待ち時間": 11},
        {"人数": 4},
        {}, index=[10, 10, 20, 30, 40, 10],
    )
    # 外部から渡された診断値を信用しない。
    data["学習候補"] = True
    data["計算待ち時間"] = 9999
    before = data.copy(deep=True)
    result = prepare_training_data(data)
    pd.testing.assert_frame_equal(data, before)
    assert result.y.tolist() == [2, 10]
    assert result.y.name == "待ち時間_分"
    assert result.metadata["元行位置"].tolist() == [0, 5]
    assert result.metadata["日付"].tolist() == [date(2026, 1, 1)] * 2
    assert result.X.index.tolist() == result.y.index.tolist() == result.metadata.index.tolist() == [10, 10]
    assert result.diagnostics["前処理学習候補"].tolist() == [True, False, False, False, False, True]
    assert result.diagnostics["日付検証結果"].iloc[1] == "missing"
    assert len(result.diagnostics) == len(data)
    assert "案内時刻" not in result.metadata


def test_same_day_duplicate_is_retained_per_step1_policy(make_frame):
    result = prepare_training_data(make_frame({"呼び出し番号": 1}, {"呼び出し番号": 1}))
    assert result.diagnostics["重複"].tolist() == [True, True]
    assert len(result.X) == 2


def test_no_candidates_and_empty_inference_have_stable_schema(make_frame):
    result = prepare_training_data(make_frame({"日付": "不明"}))
    assert result.X.empty and result.y.empty and result.metadata.empty
    assert tuple(result.X.columns) == BASE_FEATURE_COLUMNS
    assert tuple(build_features(make_frame().iloc[:0]).columns) == BASE_FEATURE_COLUMNS


def test_column_contract_rejects_missing_and_duplicate_columns(make_frame):
    with pytest.raises(ValueError, match="必須列.*受付時間"):
        build_features(make_frame().drop(columns="受付時間"))
    with pytest.raises(ValueError, match="列名が重複"):
        build_features(pd.concat([make_frame(), make_frame()[["大人"]]], axis=1))


def test_date_split_is_chronological_grouped_and_uses_positions():
    dates = pd.Series(["2026-01-03", "2026/1/1", "2026-01-02", "2026-01-01", "2026-01-04"], index=[9]*5)
    parts = split_by_date(dates, train_end="2026-01-01", validation_end="2026-01-02")
    assert [part.tolist() for part in parts] == [[1, 3], [2], [0, 4]]
    normalized = [parse_date(value).value for value in dates]
    groups = [{normalized[i] for i in part} for part in parts]
    assert max(groups[0]) < min(groups[1]) <= max(groups[1]) < min(groups[2])
    assert not groups[0] & groups[1] and not groups[1] & groups[2] and not groups[0] & groups[2]
    assert sorted(np.concatenate(parts).tolist()) == list(range(5))


@pytest.mark.parametrize("days,train_end,validation_end", [
    (["2026-01-01"] * 3, "2026-01-01", "2026-01-02"),
    (["2026-01-01", None, "2026-01-03"], "2026-01-01", "2026-01-02"),
    (["2026-01-01", "2026-01-02", "2026-01-03"], "1/1", "2026-01-02"),
    (["2026-01-01", "2026-01-02", "2026-01-03"], "2026-01-02", "2026-01-01"),
    (["2026-01-01", "2026-01-02", "2026-01-03"], "2026-01-02", "2026-01-02"),
    ([], "2026-01-01", "2026-01-02"),
])
def test_split_cannot_fall_back_to_random_rows(days, train_end, validation_end):
    with pytest.raises(ValueError):
        split_by_date(pd.Series(days), train_end=train_end, validation_end=validation_end)
