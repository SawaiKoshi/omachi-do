"""受付登録と独立した予測。時刻・待ち組数は呼び出し元が自動取得する。"""

from datetime import datetime
from numbers import Integral
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data_validation import CLOSING_MINUTES
from model_artifact import load_model, verify_serving_training
from preprocessing import BASE_FEATURE_COLUMNS, build_features

JST = ZoneInfo("Asia/Tokyo")
QUEUE_ORDER_NOTICE = "人数や席の空き状況により、ご案内順が前後する場合があります。"
PREDICTION_WARNINGS = (
    "予測は参考値です。正確な待ち時間案内に十分な精度は確認されていません。",
    "待ち組が発生した金・土・日曜を中心とする少数データに基づき、通常の平日や待ち組がない状況への精度は未検証です。",
    QUEUE_ORDER_NOTICE,
)


class PredictionService:
    """起動時に一度loadし、未配置・不整合でも受付業務を継続できる。"""

    def __init__(self, model_path: str | Path | None = None):
        self._artifact = None
        self.unavailable_reason = "model_not_configured"
        if model_path is not None:
            try:
                artifact = load_model(model_path)
                verify_serving_training(artifact["metadata"])
                self._artifact = artifact
                self.unavailable_reason = None
            except Exception:
                # パスやpickleの例外にはデータが含まれ得るので外へ出さない。
                self.unavailable_reason = "model_load_failed"

    @property
    def available(self) -> bool:
        return self._artifact is not None

    def predict(
        self, *, adults: int, children: int, received_at: datetime, waiting_groups: int,
    ) -> dict:
        for value in (adults, children, waiting_groups):
            if isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral) or value < 0:
                raise ValueError("人数・待ち組数には非負の整数が必要です。")
        if not 1 <= adults + children <= 100:
            raise ValueError("APIの合計人数は1〜100人で指定してください。")
        if (not isinstance(received_at, datetime) or received_at.tzinfo is None
                or received_at.utcoffset() is None):
            raise ValueError("受付日時にはタイムゾーンが必要です。")
        try:
            local = received_at.astimezone(JST)
        except (ValueError, OverflowError) as error:
            raise ValueError("受付日時を店舗のタイムゾーンで表現できません。") from error
        result = {
            "status": "model_unavailable", "minutes": None, "model_version": None,
            "predicted_at": received_at.isoformat(), "warnings": list(PREDICTION_WARNINGS),
        }
        if waiting_groups == 0:
            result.update(status="no_waiting_groups")
            result["warnings"].append("待ち組がない場合の予測は返しません。即時案内や待ち時間0分を保証するものではありません。")
            return result
        if local.weekday() not in (4, 5, 6):
            result.update(status="unsupported_weekday")
            return result
        minutes = local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60_000_000
        if minutes > CLOSING_MINUTES:
            result.update(status="unsupported_time")
            result["warnings"].append("23:00を超える受付時刻は現在の前処理・モデルの対象外です。")
            return result
        if not self.available:
            result["warnings"].append("モデルを利用できないため予測値は未取得です。受付業務は継続できます。")
            return result
        result["model_version"] = self._artifact["metadata"]["model_version"]
        try:
            # 待ち組数は予測条件だけに利用し、未学習の特徴量として追加しない。
            reception = pd.DataFrame([{
                "日付": local.date(), "人数": adults + children,
                "大人": adults, "子供": children, "受付時間": local.time().replace(tzinfo=None),
            }])
            features = build_features(reception)
            if tuple(features.columns) != BASE_FEATURE_COLUMNS:
                raise ValueError("特徴量契約が一致しません。")
            prediction = np.asarray(self._artifact["model"].predict(features), dtype=float)
            if prediction.shape != (1,) or not np.isfinite(prediction).all() or prediction[0] < 0:
                raise ValueError("予測値が不正です。")
        except Exception:
            result.update(status="prediction_failed")
            result["warnings"].append("予測を取得できませんでした。受付業務は継続できます。")
            return result
        result.update(status="available", minutes=float(prediction[0]))
        if local.weekday() == 4:
            result["warnings"].append("金曜日の学習候補は2営業日・12件に限られます。")
        return result
