
from pathlib import Path
import pandas as pd

# Excelファイルを探す
files = list(Path(".").glob("*.xlsx"))

# Excelデータの読み込み
df = pd.read_excel(
    files[0],
    sheet_name="Sheet1",
    header=1,
    usecols="B:U",
    na_values=["Null"]
)

# データ件数と項目名を確認
print("データ件数・項目数:", df.shape)

print("\n項目一覧:")
print(df.columns.tolist())

print("\n最初の3件:")
print(df.iloc[:3, :10].to_string(index=False))
