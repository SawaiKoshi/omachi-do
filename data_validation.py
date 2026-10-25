"""I/Oを行わないデータ品質判定。入力行を修正・削除しない。"""

import math
from typing import NamedTuple

import numpy as np
import pandas as pd

from time_utils import ParsedTime, parse_time

PEOPLE_COLUMNS = ("人数", "大人", "子供")
REQUIRED_COLUMNS = (
    "日付", "呼び出し番号", *PEOPLE_COLUMNS, "受付時間", "案内時刻", "待ち時間",
)
CLOSING_MINUTES = 23 * 60


class _ParsedNumber(NamedTuple):
    value: float
    status: str


def is_missing(value: object) -> bool:
    """None、pandasの欠損値、空文字・空白のみを欠損とする。"""
    if isinstance(value, str):
        return not value.strip()
    return bool(pd.api.types.is_scalar(value) and pd.isna(value))


def _parse_number(value: object, *, minutes_unit: bool = False) -> _ParsedNumber:
    if is_missing(value):
        return _ParsedNumber(float("nan"), "missing")
    if isinstance(value, (bool, np.bool_)):
        return _ParsedNumber(float("nan"), "invalid_type")
    if minutes_unit:
        # 現行の待ち時間処理と同じく「分」を除去してから数値化する。
        value = str(value).replace("分", "").strip()
    try:
        number = float(pd.to_numeric(value, errors="coerce"))
    except (TypeError, ValueError, OverflowError):
        return _ParsedNumber(float("nan"), "conversion_error")
    if math.isnan(number):
        # 元の値が欠損ではないため、"NaN"などの文字列は変換失敗。
        return _ParsedNumber(number, "conversion_error")
    if not math.isfinite(number):
        return _ParsedNumber(number, "non_finite")
    return _ParsedNumber(number, "valid")


def _people_checks(total: object, adults: object, children: object) -> dict:
    parsed = [_parse_number(value) for value in (total, adults, children)]
    numbers = [item.value for item in parsed]
    comparable = all(item.status in ("valid", "non_finite") for item in parsed)
    mismatch = comparable and numbers[0] != numbers[1] + numbers[2]
    status = next((item.status for item in parsed if item.status != "valid"), "valid")
    if status == "valid":
        if not all(number.is_integer() for number in numbers):
            status = "non_integer"
        elif numbers[0] < 1 or numbers[1] < 0 or numbers[2] < 0:
            status = "out_of_range"
        elif mismatch:
            status = "total_mismatch"
    return {
        "人数検証結果": status,
        "人数比較可能": comparable,
        "人数変換失敗": any(item.status == "conversion_error" for item in parsed),
        "人数不一致": mismatch,
        "不正人数": status in ("invalid_type", "non_finite", "non_integer", "out_of_range"),
    }


def validate_people(total: object, adults: object, children: object) -> str:
    """人数の代表理由を返す。項目順の入力エラー、整数、範囲、合計の順に判定。"""
    return _people_checks(total, adults, children)["人数検証結果"]


def is_cancelled(seated: object) -> bool:
    """従来どおり、前後の空白を除いた案内時刻が「取消」で始まる行。"""
    return isinstance(seated, str) and seated.strip().startswith("取消")


def _business_time(value: object) -> ParsedTime:
    parsed = parse_time(value)
    if parsed.status == "valid" and parsed.minutes > CLOSING_MINUTES:
        return ParsedTime(parsed.minutes, "out_of_range")
    return parsed


def _wait_checks(reception: object, seated: object, recorded: object) -> dict:
    cancelled = is_cancelled(seated)
    reception_time = _business_time(reception)
    seated_time = (
        ParsedTime(float("nan"), "cancelled") if cancelled else _business_time(seated)
    )
    recorded_wait = _parse_number(recorded, minutes_unit=True)
    if recorded_wait.status == "valid" and recorded_wait.value < 0:
        recorded_wait = _ParsedNumber(recorded_wait.value, "out_of_range")

    difference = seated_time.minutes - reception_time.minutes
    # 丸め前に逆転を検出する。日付またぎの補正は行わない。
    reversed_time = math.isfinite(difference) and difference < 0
    calculated = float(round(difference)) if math.isfinite(difference) else float("nan")
    comparable = not cancelled and not any(math.isnan(value) for value in (
        reception_time.minutes, seated_time.minutes, recorded_wait.value,
    ))
    mismatch = comparable and recorded_wait.value != calculated

    statuses = (reception_time.status, seated_time.status, recorded_wait.status)
    if cancelled:
        status = "cancelled"
    else:
        status = next((item for item in statuses if item != "valid"), "valid")
        if status == "valid":
            if reversed_time:
                status = "time_reversed"
            elif mismatch:
                status = "wait_mismatch"

    return {
        "受付時刻検証結果": reception_time.status,
        "案内時刻検証結果": seated_time.status,
        "記録待ち時間検証結果": recorded_wait.status,
        "待ち時間検証結果": status,
        "記録待ち時間": recorded_wait.value,
        "計算待ち時間": calculated,
        "待ち時間比較可能": comparable,
        "待ち時間不一致": mismatch,
        "時刻逆転": reversed_time,
        "キャンセル": cancelled,
        "キャンセル待ち時間あり": cancelled and not is_missing(recorded),
    }


def validate_input_frame(data: pd.DataFrame) -> None:
    """判定前に構造を検証する。入力の補正や空行の削除はしない。"""
    duplicates = data.columns[data.columns.duplicated()].tolist()
    if duplicates:
        raise ValueError(f"列名が重複しています: {', '.join(map(str, duplicates))}")
    missing = [column for column in REQUIRED_COLUMNS if column not in data.columns]
    if missing:
        raise ValueError(f"必須列がありません: {', '.join(missing)}")
    if data.empty:
        raise ValueError("データが0件です。シート・ヘッダー行を確認してください。")


def audit_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """全行・元のインデックスを保ち、コピーに診断列を付けて返す。

    学習候補はキャンセル以外で人数・待ち時間の両方がvalidの行。
    重複は報告のみとし、従来どおり学習候補の除外条件には加えない。
    """
    validate_input_frame(data)
    people_rows = data[list(PEOPLE_COLUMNS)].itertuples(index=False, name=None)
    wait_rows = data[["受付時間", "案内時刻", "待ち時間"]].itertuples(index=False, name=None)
    people = pd.DataFrame(
        [_people_checks(*row) for row in people_rows],
        index=data.index,
    )
    waits = pd.DataFrame(
        [_wait_checks(*row) for row in wait_rows],
        index=data.index,
    )
    result = data.copy(deep=True)
    for column in people:
        result[column] = people[column]
    for column in waits:
        result[column] = waits[column]
    result["重複"] = data.duplicated(subset=["日付", "呼び出し番号"], keep=False)
    result["学習候補"] = (
        ~result["キャンセル"]
        & result["人数検証結果"].eq("valid")
        & result["待ち時間検証結果"].eq("valid")
    )
    return result


def summarize_audit(audited: pd.DataFrame) -> dict[str, int]:
    """実際の判定から主要件数を集計する。除外理由の件数は重複し得る。"""
    return {
        "全データ": len(audited),
        "暫定学習候補": int(audited["学習候補"].sum()),
        "学習候補外": int((~audited["学習候補"]).sum()),
        "人数不一致": int(audited["人数不一致"].sum()),
        "待ち時間不一致": int(audited["待ち時間不一致"].sum()),
        "キャンセル": int(audited["キャンセル"].sum()),
        "重複行": int(audited["重複"].sum()),
    }
