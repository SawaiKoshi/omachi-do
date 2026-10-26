"""選定済み候補と必須基準の最終評価。I/O・モデル選択・パラメータ探索は行わない。"""

from collections.abc import Sequence
from typing import NamedTuple

import numpy as np
import pandas as pd

from model_evaluation import (
    _metrics, _parameters, _positive_integer, candidate_models, split_development_test,
)
from preprocessing import TrainingData

FINAL_MODEL_NAMES = ("random_forest", "dummy")
FINAL_MODEL_SEED = 42


class FinalTestResult(NamedTuple):
    summary: pd.DataFrame
    daily_metrics: pd.DataFrame
    predictions: pd.DataFrame
    model_parameters: dict[str, dict]
    split: dict


def final_test_metrics(actual, predicted) -> dict[str, float | int]:
    """誤差は分、割合は0〜100%。10分ちょうどは以内、20分ちょうどは超に含めない。"""
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    if (actual.ndim != 1 or actual.size == 0 or not np.isfinite(actual).all()
            or (actual < 0).any()):
        raise ValueError("最終テストの正解には非負・有限の待ち時間が1件以上必要です。")
    mae, rmse = _metrics(actual, predicted)
    absolute_error = np.abs(predicted - actual)
    return {
        "n": len(actual), "mae": mae, "rmse": rmse,
        "within_10_minutes_pct": float(100 * np.mean(absolute_error <= 10)),
        "over_20_minutes_pct": float(100 * np.mean(absolute_error > 20)),
    }


def evaluate_final_test(
    prepared: TrainingData, *, expected_test_dates: Sequence,
    expected_development_rows: int | None = None, expected_test_rows: int | None = None,
    expected_development_days: int | None = None,
) -> FinalTestResult:
    """事前指定の日付を確認し、開発全行で固定2モデルを再学習して最終評価する。

    件数・日付の期待値は呼出し側の実験設定。最終テストはfit・統計量推定・選定に渡さない。
    返す行別予測は入力順に対応する。通常のcompare_models()からは呼び出さない。
    """
    from sklearn.base import clone

    partition = split_development_test(
        prepared, final_test_days=len(expected_test_dates), expected_test_dates=expected_test_dates,
    )
    development, test = partition
    train_dates = sorted(set(development.metadata["日付"]))
    test_dates = sorted(set(test.metadata["日付"]))
    if set(train_dates) & set(test_dates) or max(train_dates) >= min(test_dates):
        raise ValueError("学習日と最終テスト日は重複せず、学習日が過去である必要があります。")
    if set(development.metadata["元行位置"]) & set(test.metadata["元行位置"]):
        raise ValueError("学習行と最終テスト行が重複しています。")
    for name, expected, actual in (
        ("expected_development_rows", expected_development_rows, len(development.X)),
        ("expected_test_rows", expected_test_rows, len(test.X)),
        ("expected_development_days", expected_development_days, len(train_dates)),
    ):
        if expected is not None and _positive_integer(expected, name) != actual:
            raise ValueError(f"{name}が事前指定と一致しません。最終テストを実行せず終了します。")
    for part in partition:
        try:
            values = part.X.to_numpy(dtype=float)
            targets = part.y.to_numpy(dtype=float)
        except (TypeError, ValueError) as error:
            raise ValueError("X・yには有限の数値が必要です。") from error
        if (targets.ndim != 1 or not np.isfinite(values).all()
                or not np.isfinite(targets).all() or (targets < 0).any()):
            raise ValueError("X・yは有限値、yは非負の待ち時間である必要があります。")
    prototypes = candidate_models(seed=FINAL_MODEL_SEED, include_lightgbm=False).models
    parameters, summaries, daily, predictions = {}, [], [], []
    actual = test.y.to_numpy(dtype=float)
    dates = test.metadata["日付"].to_numpy()
    source_positions = test.metadata["元行位置"].to_numpy()
    for name in FINAL_MODEL_NAMES:
        prototype = prototypes[name]
        parameters[name] = _parameters(prototype)
        model = clone(prototype)
        model.fit(development.X.copy(deep=True), development.y.copy(deep=True))
        predicted = np.asarray(model.predict(test.X.copy(deep=True)), dtype=float)
        summaries.append({"model": name, **final_test_metrics(actual, predicted)})
        for day in test_dates:
            mask = dates == day
            daily.append({"model": name, "date": day,
                          **final_test_metrics(actual[mask], predicted[mask])})
        # モデル別に、元入力での最終テスト行の順序を保つ。ラベルで結合しない。
        for position, source, day, target, prediction in zip(
            range(len(test.X)), source_positions, dates, actual, predicted,
        ):
            predictions.append({
                "model": name, "position": position, "source_position": int(source),
                "date": day, "y_true": float(target), "y_pred": float(prediction),
            })
    split = {
        "candidate_rows": len(prepared.X), "development_rows": len(development.X),
        "development_dates": train_dates, "final_test_rows": len(test.X),
        "final_test_dates": test_dates, "final_test_evaluated": True,
    }
    return FinalTestResult(pd.DataFrame(summaries), pd.DataFrame(daily),
                           pd.DataFrame(predictions), parameters, split)
