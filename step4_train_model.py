"""本人が確認後に実行する配信用再学習CLI。APIからは実行しない。"""

import argparse
import json
from pathlib import Path
from typing import Sequence

from model_artifact import (
    CONFIRMED_TRAINING_DAYS, CONFIRMED_TRAINING_END, CONFIRMED_TRAINING_ROWS,
    CONFIRMED_TRAINING_START, artifact_path, dependency_versions, save_model, train_model,
)
from model_evaluation import candidate_models
from preprocessing import prepare_training_data
from step1_audit import load_excel, select_excel_file

EXPECTED_ROWS = CONFIRMED_TRAINING_ROWS
EXPECTED_DAYS = CONFIRMED_TRAINING_DAYS
EXPECTED_START = CONFIRMED_TRAINING_START
EXPECTED_END = CONFIRMED_TRAINING_END


def verify_confirmed_training_data(prepared) -> None:
    """本人確認済みの候補数・期間から変わった場合、fit前に停止する。"""
    dates = sorted({day.isoformat() for day in prepared.metadata["日付"]})
    if (len(prepared.X) != EXPECTED_ROWS or len(dates) != EXPECTED_DAYS
            or not dates or dates[0] != EXPECTED_START or dates[-1] != EXPECTED_END):
        raise ValueError("学習候補数・営業日数・期間が本人確認済みの条件と一致しません。学習は開始しません。")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本人確認後: 固定Random Forestを226候補全件で配信用に再学習（評価は実行しない）")
    parser.add_argument("excel_file", nargs="?", type=Path)
    parser.add_argument("--confirm-full-training", action="store_true", help="226候補全件を配信用学習に使うことを本人が確認済み")
    parser.add_argument("--excel-date-system", choices=("1900",), default="1900")
    parser.add_argument("--output-dir", type=Path, default=Path("models"))
    parser.add_argument("--model-version")
    parser.add_argument("--check-dependencies", action="store_true", help="Excelを読まず依存関係だけ確認")
    args = parser.parse_args(argv)
    if args.check_dependencies:
        print(json.dumps(dependency_versions(), ensure_ascii=False, indent=2))
        return 0
    if args.excel_file is None or not args.confirm_full_training or args.model_version is None:
        parser.error("Excel・--model-version・--confirm-full-trainingを明示してください。確認前はExcelを読み込みません。")
    try:
        output = artifact_path(args.output_dir, args.model_version)
        if output.exists():
            parser.error("同じモデルバージョンが存在します。上書きせず別のバージョンを指定してください。")
        candidate_models(seed=42, include_lightgbm=False)
        data = load_excel(select_excel_file(args.excel_file))
        prepared = prepare_training_data(data, excel_date_system="1900")
        verify_confirmed_training_data(prepared)
        artifact = train_model(
            prepared, model_version=args.model_version, excel_date_system="1900",
            training_source="confirmed_step3_local_excel",
        )
        save_model(artifact, output_dir=args.output_dir)
    except Exception:
        # 既存Excel reader等の例外に含まれるファイル名・行の値を表示しない。
        parser.error("モデル生成を完了できません。ローカルの入力・226候補/14営業日期間・依存関係・出力権限を確認してください。")
    print(json.dumps({
        "model_version": args.model_version, "training_rows": EXPECTED_ROWS,
        "training_days": EXPECTED_DAYS, "feature_columns": artifact["metadata"]["feature_columns"],
        "evaluation_executed": False, "automatic_retraining": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
