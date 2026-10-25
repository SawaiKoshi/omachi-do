"""日付を補完せず、時刻を午前0時からの分数に変換する。"""

import math
from datetime import datetime, time
from numbers import Real
from typing import NamedTuple

import numpy as np
import pandas as pd


class ParsedTime(NamedTuple):
    minutes: float
    status: str


def parse_time(value: object) -> ParsedTime:
    """変換結果と理由を返す。営業時間やキャンセルの解釈は呼び出し側の責務。"""
    nan = float("nan")
    if pd.api.types.is_scalar(value) and pd.isna(value):
        return ParsedTime(nan, "missing")
    if isinstance(value, str) and not value.strip():
        return ParsedTime(nan, "missing")
    if isinstance(value, (bool, np.bool_, datetime)):
        return ParsedTime(nan, "invalid_type")

    if isinstance(value, time):
        return ParsedTime(
            value.hour * 60 + value.minute + value.second / 60
            + value.microsecond / 60_000_000,
            "valid",
        )

    if isinstance(value, Real):
        if not math.isfinite(value):
            return ParsedTime(nan, "non_finite")
        if not 0 <= value < 1:
            return ParsedTime(nan, "out_of_range")
        return ParsedTime(float(value) * 1440, "valid")

    if isinstance(value, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
            return ParsedTime(
                parsed.hour * 60 + parsed.minute + parsed.second / 60,
                "valid",
            )

    return ParsedTime(nan, "conversion_error")


def to_minutes(value: object) -> float:
    """既存の呼び出し用。変換できない値はNaNを返す。"""
    return parse_time(value).minutes
