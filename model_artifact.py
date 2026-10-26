"""固定Random Forestの学習・ローカル保存。受付DBやExcelの探索は行わない。

joblibは読み込み時にコードを実行し得る。下記の整合性検証は安全な
デシリアライズの仕組みではなく、本人が管理する信頼済みファイル専用である。
"""

from datetime import datetime, timezone
from hashlib import sha256
from importlib import metadata
import os
from pathlib import Path
import platform
import re
import tempfile
import warnings

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.utils.validation import check_is_fitted

from model_evaluation import candidate_models
from preprocessing import BASE_FEATURE_COLUMNS, TrainingData, parse_date

FORMAT_VERSION = 1
MODEL_NAME = "random_forest"
MODEL_SEED = 42
ESTIMATOR_CLASS = "sklearn.ensemble.RandomForestRegressor"
CONFIRMED_TRAINING_ROWS = 226
CONFIRMED_TRAINING_DAYS = 14
CONFIRMED_TRAINING_START = "2026-05-24"
CONFIRMED_TRAINING_END = "2026-08-07"
SOURCE_FILES = ("preprocessing.py", "data_validation.py", "time_utils.py", "model_evaluation.py")
TARGET_DEFINITION = "Step 1/2: validated same-day seated minus reception minutes, round-to-even integer minutes"
SERVING_DOMAIN = {
    "timezone": "Asia/Tokyo", "weekdays": [4, 5, 6],
    "latest_reception_minutes": 1380, "requires_waiting_groups": True,
    "waiting_groups_is_model_feature": False,
    "accuracy_sufficient_for_operational_use": False,
}


class ModelArtifactError(ValueError):
    """配信契約に合わないモデル。メッセージにデータやファイル名を含めない。"""


def verify_serving_training(info: object) -> None:
    """本人確認後の全候補再学習のみ配信する運用ガード。出所の証明ではない。"""
    training = info.get("training") if isinstance(info, dict) else None
    expected = {
        "source": "confirmed_step3_local_excel",
        "rows": CONFIRMED_TRAINING_ROWS, "days": CONFIRMED_TRAINING_DAYS,
        "date_start": CONFIRMED_TRAINING_START, "date_end": CONFIRMED_TRAINING_END,
        "excel_date_system": "1900",
    }
    if (not isinstance(training, dict)
            or any(training.get(key) != value for key, value in expected.items())):
        raise ModelArtifactError("配信には本人確認済みの226件・14営業日全候補での再学習が必要です。")


def dependency_versions() -> dict[str, str]:
    """配信に必要な実行環境を記録。LightGBMは読み込まない。"""
    versions = {"python": platform.python_version()}
    for package in ("scikit-learn", "numpy", "pandas", "scipy", "joblib"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions


def preprocessing_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parent
    return {name: sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def _version(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
        raise ModelArtifactError("モデルバージョンは英数字から始まる64文字以内の英数字・._-で指定してください。")
    return value


def _aware_iso(value: object) -> None:
    if not isinstance(value, str):
        raise ModelArtifactError("学習日時にはタイムゾーンが必要です。")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ModelArtifactError("学習日時が不正です。") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ModelArtifactError("学習日時にはタイムゾーンが必要です。")


def _fixed_model() -> RandomForestRegressor:
    return candidate_models(seed=MODEL_SEED, include_lightgbm=False).models[MODEL_NAME]


def _training_dates(prepared: TrainingData) -> list[str]:
    if not isinstance(prepared, TrainingData):
        raise ModelArtifactError("Step 2のTrainingDataを指定してください。")
    if tuple(prepared.X.columns) != BASE_FEATURE_COLUMNS:
        raise ModelArtifactError("特徴量の列名・順序がStep 2と一致しません。")
    if (not len(prepared.X) or not len(prepared.X) == len(prepared.y) == len(prepared.metadata)
            or not prepared.X.index.equals(prepared.y.index)
            or not prepared.X.index.equals(prepared.metadata.index)
            or prepared.metadata.columns.has_duplicates
            or not {"日付", "元行位置"} <= set(prepared.metadata.columns)):
        raise ModelArtifactError("学習データの行対応が不正です。")
    positions = prepared.metadata["元行位置"]
    if (positions.duplicated().any() or any(
        isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < 0
        for value in positions
    )):
        raise ModelArtifactError("元行位置が不正です。")
    try:
        X, y = prepared.X.to_numpy(dtype=float), prepared.y.to_numpy(dtype=float)
    except (ValueError, TypeError, OverflowError) as error:
        raise ModelArtifactError("学習値には有限の数値が必要です。") from error
    if y.ndim != 1 or not np.isfinite(X).all() or not np.isfinite(y).all() or (y < 0).any():
        raise ModelArtifactError("学習値には有限の非負目的変数が必要です。")
    dates = [parse_date(value) for value in prepared.metadata["日付"]]
    if any(item.status != "valid" for item in dates):
        raise ModelArtifactError("学習日が不正です。")
    return sorted({item.value.isoformat() for item in dates})


def train_model(
    prepared: TrainingData, *, model_version: str,
    excel_date_system: str = "1900", training_source: str = "provided_prepared_dataframe",
) -> dict:
    """受け取った候補全件で固定RFをfit。評価・探索・自動再学習を行わない。"""
    version = _version(model_version)
    dates = _training_dates(prepared)
    if excel_date_system not in ("1900", "1904"):
        raise ModelArtifactError("Excel日付方式を明示してください。")
    if training_source not in ("provided_prepared_dataframe", "confirmed_step3_local_excel"):
        raise ModelArtifactError("学習元の種別が不正です。")
    model = _fixed_model()
    model.fit(prepared.X, prepared.y)
    artifact = {"model": model, "metadata": {
        "format_version": FORMAT_VERSION, "model_version": version,
        "model_name": MODEL_NAME, "estimator_class": ESTIMATOR_CLASS,
        "estimator_parameters": model.get_params(deep=True), "seed": MODEL_SEED,
        "feature_columns": list(BASE_FEATURE_COLUMNS),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training": {
            "rows": len(prepared.X), "days": len(dates), "dates": dates,
            "date_start": dates[0], "date_end": dates[-1],
            "excel_date_system": excel_date_system, "source": training_source,
            "target_definition": TARGET_DEFINITION,
            "purpose": "serving_refit_not_evaluation", "automatic_api_training": False,
            "evaluation_results_updated": False,
        },
        "dependencies": dependency_versions(), "source_sha256": preprocessing_hashes(),
        "serving_domain": dict(SERVING_DOMAIN),
    }}
    validate_artifact(artifact)
    return artifact


def validate_artifact(artifact: object) -> None:
    """環境・学習契約の整合性検証。ファイルの安全性・出所の証明ではない。"""
    if not isinstance(artifact, dict) or set(artifact) != {"model", "metadata"}:
        raise ModelArtifactError("モデルファイルの形式が不正です。")
    info, model = artifact["metadata"], artifact["model"]
    if not isinstance(info, dict):
        raise ModelArtifactError("モデル情報がありません。")
    expected = {
        "format_version": FORMAT_VERSION, "model_name": MODEL_NAME,
        "estimator_class": ESTIMATOR_CLASS, "seed": MODEL_SEED,
        "feature_columns": list(BASE_FEATURE_COLUMNS),
        "dependencies": dependency_versions(), "source_sha256": preprocessing_hashes(),
        "serving_domain": SERVING_DOMAIN,
    }
    if (type(info.get("format_version")) is not int or type(info.get("seed")) is not int
            or any(info.get(key) != value for key, value in expected.items())):
        raise ModelArtifactError("モデル情報・依存バージョン・前処理が配信契約と一致しません。")
    _version(info.get("model_version"))
    _aware_iso(info.get("trained_at"))
    if type(model) is not RandomForestRegressor:
        raise ModelArtifactError("固定Random Forest以外は配信できません。")
    parameters = _fixed_model().get_params(deep=True)
    if info.get("estimator_parameters") != parameters or model.get_params(deep=True) != parameters:
        raise ModelArtifactError("モデル設定がStep 3の固定設定と一致しません。")
    try:
        check_is_fitted(model)
    except (ValueError, TypeError) as error:
        raise ModelArtifactError("モデルは学習済みである必要があります。") from error
    if (getattr(model, "n_features_in_", None) != len(BASE_FEATURE_COLUMNS)
            or list(getattr(model, "feature_names_in_", [])) != list(BASE_FEATURE_COLUMNS)):
        raise ModelArtifactError("学習済みモデルの特徴量名・順序が一致しません。")
    training = info.get("training")
    if not isinstance(training, dict):
        raise ModelArtifactError("学習条件がありません。")
    rows, days, dates = (training.get(key) for key in ("rows", "days", "dates"))
    if (type(rows) is not int or rows < 1 or type(days) is not int or days < 1
            or not isinstance(dates, list) or len(dates) != days
            or any(not isinstance(value, str) for value in dates)):
        raise ModelArtifactError("学習件数・日付が不正です。")
    parsed = [parse_date(value) for value in dates]
    if (any(item.status != "valid" for item in parsed) or dates != sorted(set(dates))
            or rows < days or training.get("date_start") != dates[0]
            or training.get("date_end") != dates[-1]
            or training.get("excel_date_system") not in ("1900", "1904")
            or training.get("source") not in ("provided_prepared_dataframe", "confirmed_step3_local_excel")
            or training.get("target_definition") != TARGET_DEFINITION
            or training.get("purpose") != "serving_refit_not_evaluation"
            or training.get("automatic_api_training") is not False
            or training.get("evaluation_results_updated") is not False):
        raise ModelArtifactError("学習条件が配信契約と一致しません。")


def artifact_path(output_dir: str | Path, model_version: str) -> Path:
    return Path(output_dir) / f"random-forest-{_version(model_version)}.joblib"


def save_model(artifact: dict, *, output_dir: str | Path) -> Path:
    """単一bundleを同一FS上でatomic作成。既存バージョンを上書きしない。"""
    validate_artifact(artifact)
    destination = artifact_path(output_dir, artifact["metadata"]["model_version"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".model-", delete=False) as stream:
            temporary = Path(stream.name)
            joblib.dump(artifact, stream, compress=3)
            stream.flush()
            os.fsync(stream.fileno())
        # linkは既存destinationがある場合必ず失敗し、renameによる上書きを避ける。
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def load_model(path: str | Path) -> dict:
    """本人が指定した信頼できるローカルモデルのみ。URL等は扱わない。"""
    if not isinstance(path, (str, Path)) or "://" in str(path):
        raise ModelArtifactError("信頼できるローカルモデルを指定してください。")
    local = Path(path)
    if local.suffix != ".joblib" or not local.is_file():
        raise ModelArtifactError("ローカルモデルが未配置です。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            artifact = joblib.load(local)
        validate_artifact(artifact)
    except ModelArtifactError:
        raise
    except Exception as error:
        raise ModelArtifactError("モデルを読み込めません。信頼元と実行環境を確認してください。") from error
    return artifact
