"""Step 2の出力を使う日付単位の回帰比較。I/O・最終テスト評価は行わない。"""

from collections.abc import Mapping, Sequence
from datetime import date
from importlib import import_module
from numbers import Integral
from typing import NamedTuple

import numpy as np
import pandas as pd

from preprocessing import BASE_FEATURE_COLUMNS, TrainingData, parse_date


class ModelData(NamedTuple):
    X: pd.DataFrame
    y: pd.Series
    metadata: pd.DataFrame


class DataPartition(NamedTuple):
    development: ModelData
    final_test: ModelData


class DateFold(NamedTuple):
    train: np.ndarray
    validation: np.ndarray


class ModelCandidates(NamedTuple):
    models: dict[str, object]
    skipped: dict[str, str]


class ComparisonResult(NamedTuple):
    fold_metrics: pd.DataFrame
    daily_metrics: pd.DataFrame
    summary: pd.DataFrame
    predictions: pd.DataFrame
    model_parameters: dict[str, dict]
    skipped_models: dict[str, str]


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name}は正の整数で指定してください。")
    return int(value)


def _normalized_dates(values: Sequence) -> list[date]:
    parsed = [parse_date(value) for value in values]
    if not parsed or any(item.status != "valid" for item in parsed):
        raise ValueError("分割には有効な年付き日付が必要です。")
    return [item.value for item in parsed]


def _check_structure(data: ModelData | TrainingData) -> None:
    if tuple(data.X.columns) != BASE_FEATURE_COLUMNS:
        raise ValueError("XにはStep 2の既定の10列だけを同じ順序で指定してください。")
    if not (len(data.X) == len(data.y) == len(data.metadata)):
        raise ValueError("X・y・metadataの行数が一致しません。")
    if not (data.X.index.equals(data.y.index) and data.X.index.equals(data.metadata.index)):
        raise ValueError("X・y・metadataのインデックスと行順を一致させてください。")
    if data.metadata.columns.has_duplicates or not {"日付", "元行位置"} <= set(data.metadata.columns):
        raise ValueError("metadataには重複のない日付・元行位置の列が必要です。")
    positions = data.metadata["元行位置"]
    if (positions.duplicated().any() or any(
        isinstance(value, (bool, np.bool_)) or not isinstance(value, Integral) or value < 0
        for value in positions
    )):
        raise ValueError("metadataの元行位置は重複しない非負整数にしてください。")


def _subset(data: ModelData | TrainingData, positions: np.ndarray) -> ModelData:
    return ModelData(*(part.iloc[positions].copy(deep=True)
                       for part in (data.X, data.y, data.metadata)))


def split_development_test(
    prepared: TrainingData, *, final_test_days: int = 2,
    expected_test_dates: Sequence | None = None,
) -> DataPartition:
    """最新N営業日を先に隔離し、入力順を保った独立コピーを返す。

    expected_test_datesは事前に固定したテスト日との一致確認用。データが更新されたときに
    テスト期間が無言で変わることを防ぐ。特徴量・目的変数の値は分割判断に使わない。
    """
    _check_structure(prepared)
    count = _positive_integer(final_test_days, "final_test_days")
    days = _normalized_dates(prepared.metadata["日付"])
    unique = sorted(set(days))
    if len(unique) <= count:
        raise ValueError("最終テストの前に開発用の営業日が必要です。")
    test_dates = unique[-count:]
    if expected_test_dates is not None:
        expected = _normalized_dates(expected_test_dates)
        if len(set(expected)) != len(expected) or sorted(expected) != test_dates:
            raise ValueError("最終テスト日が事前指定と一致しません。日付とデータを確認してください。")
    test_mask = np.array([day >= test_dates[0] for day in days])
    return DataPartition(
        _subset(prepared, np.flatnonzero(~test_mask)),
        _subset(prepared, np.flatnonzero(test_mask)),
    )


def expanding_date_folds(
    dates: pd.Series, *, initial_train_days: int = 6, validation_days: int = 2,
) -> tuple[DateFold, ...]:
    """古い営業日で学習し、直後の営業日で検証する。位置は入力順に対応。

    前foldの検証日は次foldの学習に入る。検証窓は重複せず、不完全な末尾は捨てずに拒否する。
    """
    initial = _positive_integer(initial_train_days, "initial_train_days")
    width = _positive_integer(validation_days, "validation_days")
    days = _normalized_dates(dates)
    unique = sorted(set(days))
    remaining = len(unique) - initial
    if remaining < width or remaining % width:
        raise ValueError("営業日数が不足しているか検証窓が不完全です。学習・検証日数を確認してください。")
    folds = []
    for end in range(initial, len(unique), width):
        train_end = unique[end - 1]
        validation_end = unique[end + width - 1]
        folds.append(DateFold(
            np.flatnonzero([day <= train_end for day in days]),
            np.flatnonzero([train_end < day <= validation_end for day in days]),
        ))
    return tuple(folds)


def candidate_models(*, seed: int = 42, include_lightgbm: bool = True) -> ModelCandidates:
    """各モデル1設定の小さな比較。未導入のLightGBMを自動インストールしない。"""
    try:
        from sklearn.dummy import DummyRegressor
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.linear_model import Ridge
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except ModuleNotFoundError as error:
        if error.name == "sklearn":
            raise ImportError("Step 3にはscikit-learnが必要です。requirements-model.txtを確認してください。") from error
        raise
    models = {
        "dummy": DummyRegressor(strategy="median"),
        "ridge": Pipeline([
            ("scaler", StandardScaler()),
            ("ridge", Ridge(alpha=1.0, solver="svd", random_state=seed)),
        ]),
        "random_forest": RandomForestRegressor(
            n_estimators=100, max_depth=3, min_samples_leaf=5,
            max_features=1.0, random_state=seed, n_jobs=1,
        ),
    }
    skipped = {}
    if include_lightgbm:
        try:
            lightgbm = import_module("lightgbm")
        except ModuleNotFoundError as error:
            if error.name != "lightgbm":
                raise
            skipped["lightgbm"] = (
                "LightGBM未導入。LGBMRegressor比較にはlightgbmと対応するOpenMP実行環境が必要です。"
                "自動インストールは行いません。"
            )
        else:
            models["lightgbm"] = lightgbm.LGBMRegressor(
                objective="regression", n_estimators=100, learning_rate=0.05,
                max_depth=3, num_leaves=7, min_child_samples=10, reg_lambda=1.0,
                random_state=seed, n_jobs=1, deterministic=True, force_col_wise=True,
                verbosity=-1,
            )
    return ModelCandidates(models, skipped)


def _metrics(actual: np.ndarray, predicted: np.ndarray) -> tuple[float, float]:
    if predicted.shape != actual.shape or not np.isfinite(predicted).all():
        raise ValueError("予測値は目的変数と同じ形状の有限値である必要があります。")
    error = predicted - actual
    return float(np.mean(np.abs(error))), float(np.sqrt(np.mean(error ** 2)))


def _parameters(model: object) -> dict:
    # Pipelineの途中の推定器自体は保存せず、JSON化できる設定値とクラス名だけを残す。
    def simple(value):
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, (list, tuple)):
            return [simple(item) for item in value]
        return type(value).__name__
    return {key: simple(value) for key, value in model.get_params(deep=True).items()}


def compare_models(
    development: ModelData, *, models: Mapping[str, object] | None = None,
    initial_train_days: int = 6, validation_days: int = 2, seed: int = 42,
) -> ComparisonResult:
    """開発データだけを受け取り、foldごとにclone/fitする。最終採用モデルは選ばない。

    行の対応はすべてiloc位置で管理。最終テストの特徴量・ラベルはこのAPIへ渡さない。
    標準偏差はfold間の記述統計（ddof=0）で、信頼区間ではない。
    """
    from sklearn.base import clone

    if not isinstance(development, ModelData):
        raise ValueError("split_development_test()で分離したdevelopmentを指定してください。")
    _check_structure(development)
    folds = expanding_date_folds(
        development.metadata["日付"], initial_train_days=initial_train_days,
        validation_days=validation_days,
    )
    try:
        X_values = development.X.to_numpy(dtype=float)
        targets = development.y.to_numpy(dtype=float)
    except (TypeError, ValueError) as error:
        raise ValueError("Xとyには有限の数値が必要です。") from error
    if (targets.ndim != 1 or not np.isfinite(X_values).all()
            or not np.isfinite(targets).all() or (targets < 0).any()):
        raise ValueError("Xとyには有限の数値、yには非負の待ち時間が必要です。")
    choices = candidate_models(seed=seed) if models is None else ModelCandidates(dict(models), {})
    if not choices.models or any(not isinstance(name, str) or not name for name in choices.models):
        raise ValueError("少なくとも1つの名前付きモデルが必要です。")
    days = np.array(_normalized_dates(development.metadata["日付"]), dtype=object)
    source_positions = development.metadata["元行位置"].to_numpy()
    fold_rows, daily_rows, prediction_rows = [], [], []
    parameters = {name: _parameters(model) for name, model in choices.models.items()}
    for name, prototype in choices.models.items():
        for number, fold in enumerate(folds, start=1):
            model = clone(prototype)
            # DataFrameを維持し、予測入力にも許可された同じ10列だけを渡す。
            X_train = development.X.iloc[fold.train].copy(deep=True)
            X_valid = development.X.iloc[fold.validation].copy(deep=True)
            y_train = development.y.iloc[fold.train].copy(deep=True)
            model.fit(X_train, y_train)
            train_prediction = np.asarray(model.predict(X_train), dtype=float)
            valid_prediction = np.asarray(model.predict(X_valid), dtype=float)
            train_mae, train_rmse = _metrics(targets[fold.train], train_prediction)
            valid_mae, valid_rmse = _metrics(targets[fold.validation], valid_prediction)
            train_dates, valid_dates = days[fold.train], days[fold.validation]
            fold_rows.append({
                "model": name, "fold": number,
                "n_train": len(fold.train), "n_validation": len(fold.validation),
                "train_days": len(set(train_dates)), "validation_days": len(set(valid_dates)),
                "train_start": min(train_dates), "train_end": max(train_dates),
                "validation_start": min(valid_dates), "validation_end": max(valid_dates),
                "train_mae": train_mae, "validation_mae": valid_mae,
                "train_rmse": train_rmse, "validation_rmse": valid_rmse,
                "mae_gap": valid_mae - train_mae, "rmse_gap": valid_rmse - train_rmse,
            })
            for day in sorted(set(valid_dates)):
                mask = valid_dates == day
                mae, rmse = _metrics(targets[fold.validation][mask], valid_prediction[mask])
                daily_rows.append({"model": name, "fold": number, "date": day,
                                   "n": int(mask.sum()), "mae": mae, "rmse": rmse})
            for position, prediction in zip(fold.validation, valid_prediction):
                prediction_rows.append({
                    "model": name, "fold": number, "position": int(position),
                    "source_position": int(source_positions[position]), "date": days[position],
                    "y_true": float(targets[position]), "y_pred": float(prediction),
                })
    fold_metrics = pd.DataFrame(fold_rows)
    daily_metrics = pd.DataFrame(daily_rows)
    predictions = pd.DataFrame(prediction_rows)
    predictions = pd.concat([
        predictions.loc[predictions["model"].eq(name)].sort_values("position", kind="stable")
        for name in choices.models
    ], ignore_index=True)
    summary_rows = []
    for name in choices.models:
        scores = fold_metrics.loc[fold_metrics["model"].eq(name)]
        oof = predictions.loc[predictions["model"].eq(name)]
        oof_mae, oof_rmse = _metrics(oof["y_true"].to_numpy(), oof["y_pred"].to_numpy())
        row = {"model": name, "folds": len(scores)}
        for column in ("train_mae", "validation_mae", "validation_rmse"):
            row[f"{column}_mean"] = float(scores[column].mean())
            row[f"{column}_std"] = float(scores[column].std(ddof=0))
        row.update({
            "mae_gap_mean": float(scores["mae_gap"].mean()),
            "oof_mae": oof_mae, "oof_rmse": oof_rmse,
            "daily_mae_mean": float(daily_metrics.loc[daily_metrics["model"].eq(name), "mae"].mean()),
        })
        summary_rows.append(row)
    return ComparisonResult(fold_metrics, daily_metrics, pd.DataFrame(summary_rows),
                            predictions, parameters, choices.skipped)
