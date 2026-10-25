"""Excel監査のCLI。import時にはファイルを探索・読み込みしない。"""

import argparse
from pathlib import Path
from typing import Sequence

import pandas as pd

from data_validation import audit_dataframe, summarize_audit, validate_input_frame


def select_excel_file(path: Path | None = None, *, directory: Path = Path(".")) -> Path:
    """明示指定を優先し、自動選択はロックファイル以外のxlsxが1つの場合のみ。"""
    if path is not None:
        path = Path(path)
        if path.name.startswith("~$") or path.suffix.lower() != ".xlsx":
            raise ValueError("Excelのロックファイル以外の.xlsxファイルを指定してください。")
        if not path.is_file():
            raise FileNotFoundError(f"Excelファイルがありません: {path}")
        return path

    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() == ".xlsx" and not path.name.startswith("~$")
    )
    if not files:
        raise FileNotFoundError(f"Excelファイル（.xlsx）がありません: {directory}")
    if len(files) > 1:
        names = ", ".join(path.name for path in files)
        raise ValueError(f"Excelファイルが複数あります。対象を引数で指定してください: {names}")
    return files[0]


def load_excel(path: Path) -> pd.DataFrame:
    """従来のシート・ヘッダー・列範囲・Nullの扱いを維持する。"""
    data = pd.read_excel(
        path, sheet_name="Sheet1", header=1, usecols="B:U", na_values=["Null"],
    )
    validate_input_frame(data)
    return data


def print_report(data: pd.DataFrame, audited: pd.DataFrame) -> None:
    print("データ件数・項目数:", data.shape)
    print("\n項目一覧:")
    print(data.columns.tolist())
    print("\n最初の3件:")
    print(data.iloc[:3, :10].to_string(index=False))
    print("\n【欠損値の確認（元データ）】")
    print(data.isna().sum())

    print("\n【検証結果の分類】")
    for column in (
        "人数検証結果", "受付時刻検証結果", "案内時刻検証結果",
        "記録待ち時間検証結果", "待ち時間検証結果",
    ):
        print(f"\n{column}:")
        print(audited[column].value_counts().to_string())

    print("\n【比較可能性と数値の妥当性】")
    for label, mask in (
        ("人数の比較可能", audited["人数比較可能"]),
        ("人数の比較不能", ~audited["人数比較可能"]),
        ("人数の変換失敗行", audited["人数変換失敗"]),
        ("不正な人数", audited["不正人数"]),
        ("待ち時間の比較可能", audited["待ち時間比較可能"]),
        ("待ち時間の比較不能", ~audited["待ち時間比較可能"]),
        ("待ち時間の未記録", audited["記録待ち時間検証結果"].eq("missing")),
        ("不正な記録待ち時間", ~audited["キャンセル"] & audited["記録待ち時間検証結果"].isin(
            ["invalid_type", "non_finite", "out_of_range"],
        )),
        ("時刻逆転（丸め前）", audited["時刻逆転"]),
        ("キャンセルかつ待ち時間未記録", audited["キャンセル"] & audited["記録待ち時間検証結果"].eq("missing")),
        ("キャンセル以外の比較不能", ~audited["キャンセル"] & ~audited["待ち時間比較可能"]),
        ("キャンセルなのに待ち時間がある", audited["キャンセル待ち時間あり"]),
    ):
        print(f"{label}: {int(mask.sum())}")

    print("\n【要確認行（修正・削除は行いません）】")
    details = [
        "日付", "呼び出し番号", "人数", "大人", "子供", "受付時間", "案内時刻", "待ち時間",
        "記録待ち時間", "計算待ち時間", "人数検証結果", "受付時刻検証結果",
        "案内時刻検証結果", "記録待ち時間検証結果", "待ち時間検証結果", "重複",
    ]
    print(audited.loc[~audited["学習候補"] | audited["重複"], details].to_string(index=False))

    print("\n【暫定学習候補データの確認】")
    for label, count in summarize_audit(audited).items():
        print(f"{label}: {count}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="おまちどうのExcelデータ品質監査")
    parser.add_argument("excel_file", nargs="?", type=Path, help="監査する.xlsxファイル")
    args = parser.parse_args(argv)
    try:
        path = select_excel_file(args.excel_file)
        data = load_excel(path)
        audited = audit_dataframe(data)
    except (OSError, ValueError, ImportError) as error:
        parser.error(str(error))
    print_report(data, audited)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
