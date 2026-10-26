"""本人の環境で実行するStep 3 CLI。import・依存確認・helpではExcelを読まない。"""

import argparse
from importlib import metadata
import json
from pathlib import Path
import platform
from typing import Sequence

from model_evaluation import candidate_models, compare_models, split_development_test
from preprocessing import prepare_training_data
from step1_audit import load_excel, select_excel_file


def dependency_versions() -> dict[str, str]:
    versions = {"python": platform.python_version()}
    for package in ("numpy", "pandas", "openpyxl", "scikit-learn", "scipy", "lightgbm"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="おまちどう: 最終テストを温存した日付単位のモデル比較")
    parser.add_argument("excel_file", nargs="?", type=Path, help="本人がローカルで読む.xlsxファイル")
    parser.add_argument("--check-dependencies", action="store_true", help="依存バージョンだけ表示（Excel読込なし）")
    parser.add_argument("--excel-date-system", choices=("1900", "1904"), default=None)
    parser.add_argument("--initial-train-days", type=int, default=6)
    parser.add_argument("--validation-days", type=int, default=2)
    parser.add_argument("--final-test-days", type=int, default=2)
    parser.add_argument("--expected-test-dates", nargs="+", help="事前に固定した最終テスト日（YYYY-MM-DD）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    versions = dependency_versions()
    if args.check_dependencies:
        print(json.dumps(versions, ensure_ascii=False, indent=2))
        return 0
    if args.excel_file is None:
        parser.error("Excelファイルを明示指定してください。依存確認だけなら--check-dependenciesを使ってください。")
    try:
        # 必須依存の不足・壊れた任意依存は、Excelを開く前に報告する。
        candidate_models(seed=args.seed)
        path = select_excel_file(args.excel_file)
        data = load_excel(path)
        prepared = prepare_training_data(data, excel_date_system=args.excel_date_system)
        partition = split_development_test(
            prepared, final_test_days=args.final_test_days,
            expected_test_dates=args.expected_test_dates,
        )
        result = compare_models(
            partition.development, initial_train_days=args.initial_train_days,
            validation_days=args.validation_days, seed=args.seed,
        )
    except (OSError, ValueError, ImportError) as error:
        parser.error(str(error))
    configuration = {
        "seed": args.seed, "excel_date_system": args.excel_date_system,
        "initial_train_days": args.initial_train_days,
        "validation_days": args.validation_days, "final_test_days": args.final_test_days,
        "expected_test_dates": args.expected_test_dates,
    }
    split = {
        "source_rows": len(data), "candidate_rows": len(prepared.X),
        "development_rows": len(partition.development.X),
        "development_dates": sorted(set(partition.development.metadata["日付"])),
        "final_test_rows": len(partition.final_test.X),
        "final_test_dates": sorted(set(partition.final_test.metadata["日付"])),
        "final_test_evaluated": False,
    }
    if args.format == "json":
        # 行ごとの予測・元行位置・氏名・呼び出し番号を出力しない。
        report = {
            "configuration": configuration, "versions": versions, "split": split,
            "model_parameters": result.model_parameters, "skipped_models": result.skipped_models,
            "summary": result.summary.to_dict(orient="records"),
            "fold_metrics": result.fold_metrics.to_dict(orient="records"),
            "daily_metrics": result.daily_metrics.to_dict(orient="records"),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    else:
        print("実行設定:", json.dumps(configuration, ensure_ascii=False))
        print("依存バージョン:", json.dumps(versions, ensure_ascii=False))
        print("データ分割:", json.dumps(split, ensure_ascii=False, default=str))
        print("モデル設定:", json.dumps(result.model_parameters, ensure_ascii=False))
        for name, reason in result.skipped_models.items():
            print(f"未実行 {name}: {reason}")
        for title, table in (("モデル比較", result.summary), ("fold別", result.fold_metrics),
                             ("検証営業日別", result.daily_metrics)):
            print(f"\n{title}（誤差の単位: 分）")
            print(table.to_string(index=False))
        print("\nfold平均は等重み、stdはddof=0。OOFは検証行をまとめた誤差、日別平均は営業日を等重みにする。")
        print("最終テストは未評価。小さな性能差だけで優劣を断定せず、採用モデルは別途判断してください。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
