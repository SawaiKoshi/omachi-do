"""受付時点の特徴量・学習対象・日付分割。ファイルI/Oや学習は行わない。"""

import math
import re
from datetime import date, datetime, time, timedelta
from numbers import Real
from typing import NamedTuple

import numpy as np
import pandas as pd

from data_validation import (
    CLOSING_MINUTES, audit_dataframe, is_missing, validate_people,
)
from time_utils import parse_time

RECEPTION_COLUMNS = ("日付", "人数", "大人", "子供", "受付時間")
WEEKDAYS = tuple("月火水木金土日")
WEATHER_CATEGORIES = ("晴れ", "曇り", "雨", "雪", "その他", "不明")
BASE_FEATURE_COLUMNS = (
    "大人", "子供", "受付時刻_分", *(f"曜日_{day}" for day in WEEKDAYS),
)


class ParsedDate(NamedTuple):
    value: date | None
    status: str


class TrainingData(NamedTuple):
    X: pd.DataFrame
    y: pd.Series
    metadata: pd.DataFrame
    diagnostics: pd.DataFrame


class DateSplit(NamedTuple):
    train: np.ndarray
    validation: np.ndarray
    test: np.ndarray


def parse_date(value: object, *, excel_date_system: str | None = None) -> ParsedDate:
    """年付き暦日を採用。Excel数値は1900/1904方式の明示時だけ変換する。"""
    if excel_date_system not in (None, "1900", "1904"):
        raise ValueError("excel_date_systemはNone、'1900'、'1904'のいずれかです。")
    if is_missing(value):
        return ParsedDate(None, "missing")
    if isinstance(value, (bool, np.bool_)):
        return ParsedDate(None, "invalid_type")
    if isinstance(value, Real) and excel_date_system is not None:
        try:
            serial = float(value)
        except OverflowError:
            return ParsedDate(None, "out_of_range")
        if not math.isfinite(serial):
            return ParsedDate(None, "non_finite")
        minimum, maximum = (1, 2958465) if excel_date_system == "1900" else (0, 2957003)
        if not minimum <= serial <= maximum:
            return ParsedDate(None, "out_of_range")
        if not serial.is_integer():
            return ParsedDate(None, "time_component")
        # 1900方式の60は実在しない1900-02-29。他の暦日に補正しない。
        if excel_date_system == "1900":
            if serial == 60:
                return ParsedDate(None, "invalid_date")
            origin = date(1899, 12, 31)
            offset = int(serial) - int(serial > 60)
        else:
            origin, offset = date(1904, 1, 1), int(serial)
        return ParsedDate(origin + timedelta(days=offset), "valid")
    if isinstance(value, np.datetime64):
        value = pd.Timestamp(value)
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return ParsedDate(None, "timezone_aware")
        if value.time() != time(0) or getattr(value, "nanosecond", 0) != 0:
            return ParsedDate(None, "time_component")
        return ParsedDate(value.date(), "valid")
    if isinstance(value, date):
        return ParsedDate(value, "valid")
    if isinstance(value, str):
        text = value.strip()
        # 日/月順序、年の補完、日時文字列の解釈は行わない。
        if not re.fullmatch(r"[0-9]{4}([-\/])[0-9]{1,2}\1[0-9]{1,2}", text):
            return ParsedDate(None, "conversion_error")
        try:
            year, month, day = map(int, re.split(r"[-/]", text))
            return ParsedDate(date(year, month, day), "valid")
        except ValueError:
            return ParsedDate(None, "conversion_error")
    return ParsedDate(None, "invalid_type")


def _check_columns(data: pd.DataFrame, required: tuple[str, ...]) -> None:
    if data.columns.has_duplicates:
        raise ValueError("列名が重複しています。")
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError(f"必須列がありません: {', '.join(missing)}")


def _weather_category(value: object) -> str:
    if is_missing(value):
        return "不明"
    if not isinstance(value, str):
        raise ValueError("天気は文字列または欠損値で指定してください。")
    text = value.strip()
    return text if text in WEATHER_CATEGORIES else "その他"


def build_features(
    data: pd.DataFrame, *, weather_at_reception: bool = False,
    excel_date_system: str | None = None,
) -> pd.DataFrame:
    """予測時にも使う同一変換。余分な列は参照せず、既知の列だけを返す。

    天気は受付時点の値と確認できた場合だけ明示的に有効化する。
    不正入力は値自体をエラーに出さず、0始まりの行位置と理由で報告する。
    空入力は同じ列構成の空DataFrameとなる。元インデックスは維持する。
    """
    required = RECEPTION_COLUMNS + (("天気",) if weather_at_reception else ())
    _check_columns(data, required)
    dates, minutes, weather = [], [], []
    rows = data[list(RECEPTION_COLUMNS)].itertuples(index=False, name=None)
    for position, (day, total, adults, children, reception) in enumerate(rows):
        parsed_day = parse_date(day, excel_date_system=excel_date_system)
        people_status = validate_people(total, adults, children)
        parsed_time = parse_time(reception)
        time_status = parsed_time.status
        if time_status == "valid" and parsed_time.minutes > CLOSING_MINUTES:
            time_status = "out_of_range"
        for field, status in (
            ("日付", parsed_day.status), ("人数", people_status), ("受付時間", time_status),
        ):
            if status != "valid":
                raise ValueError(f"行位置{position}: {field}={status}")
        dates.append(parsed_day.value)
        minutes.append(parsed_time.minutes)
    if weather_at_reception:
        weather = [_weather_category(value) for value in data["天気"]]
    values = {
        "大人": pd.to_numeric(data["大人"]),
        "子供": pd.to_numeric(data["子供"]),
        "受付時刻_分": minutes,
        **{f"曜日_{label}": [float(day.weekday() == number) for day in dates]
           for number, label in enumerate(WEEKDAYS)},
    }
    if weather_at_reception:
        values.update({f"天気_{label}": [float(item == label) for item in weather]
                       for label in WEATHER_CATEGORIES})
    return pd.DataFrame(values, index=data.index, dtype=float)


def prepare_training_data(
    data: pd.DataFrame, *, weather_at_reception: bool = False,
    excel_date_system: str | None = None,
) -> TrainingData:
    """Step 1を再判定し、有効な日付の候補を抽出。診断には全行を残す。

    受け取った学習候補・計算待ち時間等の派生列は信用せず再計算する。
    行位置は元DataFrameに対するiloc用。ラベルが重複しても対応を保つ。
    """
    audited = audit_dataframe(data)
    parsed = [parse_date(value, excel_date_system=excel_date_system) for value in data["日付"]]
    date_ok = np.array([item.status == "valid" for item in parsed])
    mask = audited["学習候補"].to_numpy(dtype=bool) & date_ok
    positions = np.flatnonzero(mask)
    selected = audited.iloc[positions]
    X = build_features(selected, weather_at_reception=weather_at_reception,
                       excel_date_system=excel_date_system)
    y = selected["計算待ち時間"].rename("待ち時間_分").copy()
    metadata = pd.DataFrame({
        "元行位置": positions,
        "日付": [parsed[position].value for position in positions],
    }, index=selected.index)
    diagnostics = audited[["人数検証結果", "待ち時間検証結果", "学習候補", "重複"]].copy()
    diagnostics["日付検証結果"] = [item.status for item in parsed]
    diagnostics["前処理学習候補"] = mask
    diagnostics["元行位置"] = np.arange(len(data))
    return TrainingData(X, y, metadata, diagnostics)


def split_by_date(
    dates: pd.Series, *, train_end: object, validation_end: object,
) -> DateSplit:
    """暦日単位の過去→未来の3分割。返値は入力順に対するiloc位置。

    train <= train_end < validation <= validation_end < test。
    比率や行数から境界を推測しない。各区間は少なくとも1行を必要とする。
    """
    cutoffs = [parse_date(value) for value in (train_end, validation_end)]
    if any(item.status != "valid" for item in cutoffs):
        raise ValueError("分割境界には有効な年付き日付を指定してください。")
    first, second = (item.value for item in cutoffs)
    if first >= second:
        raise ValueError("train_endはvalidation_endより前にしてください。")
    parsed = [parse_date(value) for value in dates]
    if any(item.status != "valid" for item in parsed):
        raise ValueError("分割対象に不正な日付があります。")
    days = [item.value for item in parsed]
    result = DateSplit(*(
        np.flatnonzero(np.array(mask, dtype=bool)) for mask in (
            [day <= first for day in days],
            [first < day <= second for day in days],
            [second < day for day in days],
        )
    ))
    if any(len(part) == 0 for part in result):
        raise ValueError("学習・検証・テストの各区間にデータが必要です。日数と境界を確認してください。")
    return result
