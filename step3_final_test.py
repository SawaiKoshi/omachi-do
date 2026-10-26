"""本人が明示実行する最終テスト専用CLI。import/help/依存確認ではExcelを開かない。"""

import argparse
from datetime import date
import json
from pathlib import Path
from typing import Sequence

from final_evaluation import FINAL_MODEL_NAMES, FINAL_MODEL_SEED, evaluate_final_test
from model_evaluation import candidate_models
from preprocessing import prepare_training_data
from step1_audit import load_excel, select_excel_file
from step3_evaluate import dependency_versions

# 今回の実験計画。モデル生成・汎用分割ロジックの条件には埋め込まない。
EXPECTED_TEST_DATES = (date(2026, 7, 12), date(2026, 8, 7))
EXPECTED_DEVELOPMENT_ROWS = 206
EXPECTED_TEST_ROWS = 20
EXPECTED_DEVELOPMENT_DAYS = 12


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定したRandom ForestとDummyの最終テスト専用コマンド")
    parser.add_argument("excel_file", nargs="?", type=Path)
    parser.add_argument("--excel-date-system", choices=("1900", "1904"), default=None)
    parser.add_argument("--check-dependencies", action="store_true", help="Excelを読まず依存バージョンだけ表示")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    versions = dependency_versions()
    if args.check_dependencies:
        print(json.dumps(versions, ensure_ascii=False, indent=2))
        return 0
    if args.excel_file is None:
        parser.error("最終テスト対象のExcelファイルを明示指定してください。")
    try:
        candidate_models(seed=FINAL_MODEL_SEED, include_lightgbm=False)
        data = load_excel(select_excel_file(args.excel_file))
        prepared = prepare_training_data(data, excel_date_system=args.excel_date_system)
        result = evaluate_final_test(
            prepared, expected_test_dates=EXPECTED_TEST_DATES,
            expected_development_rows=EXPECTED_DEVELOPMENT_ROWS,
            expected_test_rows=EXPECTED_TEST_ROWS,
            expected_development_days=EXPECTED_DEVELOPMENT_DAYS,
        )
    except (OSError, ValueError, ImportError) as error:
        parser.error(str(error))
    configuration = {
        "selected_candidate": "random_forest", "required_baseline": "dummy",
        "models": FINAL_MODEL_NAMES, "seed": FINAL_MODEL_SEED,
        "expected_test_dates": EXPECTED_TEST_DATES,
        "expected_development_rows": EXPECTED_DEVELOPMENT_ROWS,
        "expected_test_rows": EXPECTED_TEST_ROWS,
        "expected_development_days": EXPECTED_DEVELOPMENT_DAYS,
        "excel_date_system": args.excel_date_system,
    }
    note = "10分以内・20分超の割合はサービス実用性の補助指標。最終テストによるモデル再選定・パラメータ調整は禁止。"
    report = {
        "configuration": configuration, "versions": versions,
        "split": {"source_rows": len(data), **result.split},
        "model_parameters": result.model_parameters,
        "summary": result.summary.to_dict(orient="records"),
        "daily_metrics": result.daily_metrics.to_dict(orient="records"),
        "interpretation": note,
    }
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str, allow_nan=False))
    else:
        for title, value in (("固定設定", configuration), ("依存バージョン", versions),
                             ("データ分割", report["split"]), ("モデル設定", result.model_parameters)):
            print(f"{title}:", json.dumps(value, ensure_ascii=False, default=str))
        labels = {"model": "モデル", "date": "営業日", "n": "件数", "mae": "MAE（分）",
                  "rmse": "RMSE（分）", "within_10_minutes_pct": "10分以内（%）",
                  "over_20_minutes_pct": "20分超（%）"}
        for title, table in (("最終テスト全体", result.summary), ("最終テスト営業日別", result.daily_metrics)):
            print(f"\n{title}")
            print(table.rename(columns=labels).to_string(index=False))
        print(f"\n{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
