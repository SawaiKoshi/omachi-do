
import math
from datetime import datetime, time

import numpy as np
import pandas as pd
import pytest

from time_utils import parse_time, to_minutes


@pytest.mark.parametrize(
    "value, expected",
    [
        ("12:30", 750),
        ("12:30:00", 750),
        (0.5, 720),
        (time(12, 0, 59), 720 + 59 / 60),
        (time(12, 0, 0, 500000), 720 + 0.5 / 60),
        (" 12:30 ", 750),
        ("00:00", 0),
        ("23:59:59", 1439 + 59 / 60),
        (0, 0),
        (np.float64(0.5), 720),
    ]
)
def test_valid_times(value, expected):
    assert to_minutes(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [
        -0.5,
        45000.5,
        True,
        float("inf"),
        "25:00",
        "取消21:10",
        datetime(2026, 1, 1, 12, 0),
        pd.Timestamp("2026-01-01 12:00"),
        None,
        pd.NA,
        pd.NaT,
        float("nan"),
        np.bool_(True),
        "",
        "   ",
    ]
)
def test_invalid_times(value):
    assert math.isnan(to_minutes(value))


@pytest.mark.parametrize("value,status", [
    (None, "missing"),
    (pd.NA, "missing"),
    ("  ", "missing"),
    ("不明", "conversion_error"),
    ("25:00", "conversion_error"),
    ("取消21:10", "conversion_error"),
    (True, "invalid_type"),
    (np.bool_(False), "invalid_type"),
    (datetime(2026, 1, 1), "invalid_type"),
    (-0.5, "out_of_range"),
    (1.0, "out_of_range"),
    (45000.5, "out_of_range"),
    (float("inf"), "non_finite"),
    ("12:30", "valid"),
    ("23:10", "valid"),  # 汎用変換に店舗の閉店時刻を埋め込まない。
])
def test_time_failure_reasons(value, status):
    assert parse_time(value).status == status
