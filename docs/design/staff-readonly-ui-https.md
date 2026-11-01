# スタッフ閲覧画面の開発専用HTTPS確認

`staff_https_lab.py` は新しく作る使い捨て合成DBだけを表示する明示的な開発コマンドである。
本番起動方法ではなく、実DB・モデル・既存の実験DBを指定する引数もない。
顧客画面の起動方法や既存オリジンは変更しない。

## プレビューの安全境界

- 接続先は `https://staff.localhost:8443/staff/login`、待受は `127.0.0.1` 固定。
- TLS証明書と鍵は外部で用意する。リポジトリ内のファイル、直接のシンボリックリンク、複数ハードリンク、他ユーザー所有のファイルを拒否する。秘密鍵は所有者だけが読める必要がある。
- HTTP用リスナー、証明書検証の回避、HTTPへのフォールバック、proxyヘッダー信頼は追加しない。
- workerは1、reloadとアクセスログは無効。共有資格情報は対話端末の非表示入力、または開発用 `OMACHI_LAB_STAFF_TOKEN` からだけ受け取る。引数、画面ソース、URL、起動メッセージに含めない。
- DBは `temporary_workspace()` 内に合成v1を生成してから、そのファイルだけをv2へ移行する。`legacy`・訂正無効を維持し、終了時にディレクトリごと削除する。
- 追加ラッパーでも `/staff/` のGET/HEADとセッション作成・破棄だけを通す。顧客用経路、受付・案内・訂正・品質レビューの書込みは拒否する。
- モデルは一時ディレクトリの不存在パスを明示し、既存モデルや環境変数の実DB設定を参照しない。
- サンプルの業務時刻は2026-10-29 00:01（JST）で固定する。最近の案内3件と未案内3件を確認できる。セッションの期限計測は実時間のまま。

## WindowsブラウザとWSL2の準備（手動）

この作業では手動確認用の永続証明書作成、Windowsの信頼ストア変更、hosts変更、ネットワーク設定変更を実行していない。
自動試験用の証明書と鍵だけを一時ディレクトリに生成し、テスト専用の信頼設定で使用した。
以下は開発者が自分の開発機で行う確認手順である。

1. Windows用mkcertを[公式プロジェクトの手順](https://github.com/FiloSottile/mkcert)で導入する。
   WindowsのPowerShellで、ローカル開発用CAを信頼し、スタッフホストだけの証明書を作る。

   ```powershell
   $omachiTls = Join-Path $env:LOCALAPPDATA "omachi-do-dev-tls"
   New-Item -ItemType Directory -Force $omachiTls
   mkcert -install
   mkcert -cert-file "$omachiTls\staff.localhost.pem" -key-file "$omachiTls\staff.localhost-key.pem" staff.localhost
   ```

   `mkcert -install` は信頼ストアを変更するため、意味を確認して手動で実行する。
   Windowsで作ったCA秘密鍵 `rootCA-key.pem` はWSLへコピーしない。ローカルCAは開発機専用とし、店舗端末へ展開しない。

2. サーバー用の証明書と葉の秘密鍵だけをWSLのリポジトリ外へコピーする。
   次のWindowsユーザー名は実環境の値に置き換える。

   ```bash
   omachi_tls_dir="$HOME/.local/share/omachi-do-dev-tls"
   install -d -m 700 "$omachi_tls_dir"
   install -m 644 /mnt/c/Users/WindowsUser/AppData/Local/omachi-do-dev-tls/staff.localhost.pem "$omachi_tls_dir/staff.localhost.pem"
   install -m 600 /mnt/c/Users/WindowsUser/AppData/Local/omachi-do-dev-tls/staff.localhost-key.pem "$omachi_tls_dir/staff.localhost-key.pem"
   ```

3. Windows上で `staff.localhost` が `127.0.0.1` へ解決されるか確認する。
   解決しない環境ではWindowsのhostsファイルへ `127.0.0.1 staff.localhost` を手動設定する方法を検討する。
   Windowsブラウザが使う名前解決と、WSL内の名前解決は別に確認する。

4. リポジトリのWSL端末から起動する。

   ```bash
   .venv/bin/python -B -m staff_https_lab \
     --cert "$omachi_tls_dir/staff.localhost.pem" \
     --key "$omachi_tls_dir/staff.localhost-key.pem"
   ```

   開発専用の32文字以上のASCII共有資格情報を端末へ入力し、同じ値でログインする。
   実運用の資格情報をサンプルに使わない。起動時の入力にはTTYが必要で、入力を隠せない場合は開始しない。

5. Windowsブラウザで `https://staff.localhost:8443/staff/login` を開く。
   証明書の警告が出たら先へ進まず、名前と信頼設定を確認する。HTTPへ切り替えない。
   [MicrosoftのWSLネットワーク説明](https://learn.microsoft.com/en-us/windows/wsl/networking)ではWindowsからWSLのアプリへlocalhostで接続できる。
   この開発環境でも接続できるか個別に検証する。失敗しても `0.0.0.0`、LAN向けportproxy、ファイアウォール緩和へ切り替えない。

## 実ブラウザの手動検証項目

自動試験の証拠と、Windowsブラウザでの確認結果は分けて記録する。
ブラウザの開発者ツールに見える資格情報・Cookie・CSRF値をスクリーンショットやログとして共有しない。

| 確認 | 期待する挙動 |
| --- | --- |
| 未認証でスタッフ画面へ移動 | 受付情報を表示せずログインへ進む |
| 不正な資格情報 | 汎用エラー、秘密情報を表示しない |
| 正しいログイン | HttpOnly Cookieを使って未案内3件・最近の案内3件を表示 |
| Cookie属性 | `__Host-omachi-staff`、Secure、HttpOnly、SameSite=Strict、Path=/、Domainなし |
| ブラウザストレージ | localStorage/sessionStorageに資格情報・セッション・CSRFを保存しない |
| 番号選択 | 最新の読み取りAPIで閲覧専用の詳細を表示、操作・訂正・LINEボタンなし |
| 通信をオフラインへ変更 | 取得失敗を表示、古い内容を最新として表示しない |
| セッション期限切れ／サーバー再起動 | 受付情報を消し、再認証が必要 |
| ログアウト | Cookieを削除し、古いCookieでもスタッフAPIは401 |
| 別タブでログアウト | 元のタブも次の認証確認で情報を消す |
| 顧客オリジンからスタッフ情報取得 | CORS許可なし、受付情報を返さない |
| HTTPS不備・異なるHost/Origin | 正常なスタッフ画面として動作しない |

CSRF・Originの拒否は合成データの自動API/TLS試験でも確認する。
ブラウザで開いているだけでは、他のオリジンからの攻撃やSameSiteの全挙動を検証したことにはならない。
この環境にはChromium/Chrome/Firefox、Playwright/Seleniumがなく、Windowsの実ブラウザ確認は未実施。

## 自動検証の範囲

`tests/test_staff_https_lab.py` は一時証明書、合成DB、loopbackソケットだけを用いる。
外部TLSファイルの条件、強制loopback設定、環境変数による実DB/モデル指定の無効化、
legacy・訂正無効、書込み拒否、DBバイト列の不変、終了時削除を確認する。
実TLS試験では一時自己署名証明書をテスト専用SSLContextだけで信頼し、ホスト名検証を維持して
ログイン・Cookie属性・認証済み読取り・Origin拒否・CSRF拒否・ログアウト後失効を確認する。
OSやブラウザの信頼ストアを変更しない。

次のスワイプ・状態変更UIを有効にする前に、Windows/WSL2の実機で上記項目を完了する。
このランチャーを店舗LAN用、実DB用、複数worker用へ拡張して流用しない。

## 起動エラーの安全な切り分け（2026-11-01）

修正前はLabConfigurationError、EOFError、OSErrorをまとめて同じ文言へ置き換えていたため、
証明書、認証入力、一時DBのどこで失敗したかを表示から判別できなかった。
起動経路は「TLSファイル検査・証明書/鍵の読込み → 認証入力 → 入力検証 → 合成DB準備 →
Synthetic data onlyの表示 → Uvicorn起動」であった。

今回の報告は入力プロンプトあり・Synthetic data onlyなしだった。
その実行ではTLS事前検査を通過しており、Uvicornの待受やホスト名解決より前の失敗に絞れる。
認証入力の具体的な条件は未確認のため、短い認証情報だった等と断定しない。
調査時のファイルメタデータは証明書644・鍵600、所有者一致、通常ファイル、単一リンクで、8443に待受はなかった。
実際の認証情報・秘密鍵の内容は表示・収集していない。
WSL側のstaff.localhostの名前解決はサーバー起動には使わず、待受は数値の127.0.0.1である。
ブラウザ側の名前解決・証明書信頼は別途必要である。

合成データによる修正前の再現では、31文字のASCII、32文字の非ASCII、入力のEOFが同じ汎用エラーになり、
32文字の有効ASCIIではサーバー起動経路まで到達した。認証の制約自体は変更していない。
修正後は固定コードだけで失敗理由を示し、任意の例外メッセージ、入力値、鍵、内部パス、トレースバックを診断に含めない。
Uvicornの起動失敗がSystemExitで伝わる経路も扱う。

| 固定コード | 対応 |
| --- | --- |
| `credential_length` | 32文字以上の開発専用の印字可能な半角ASCIIを入力する |
| `credential_ascii` / `credential_printable` / `credential_blank` | 非ASCII・制御文字・空白だけの入力を避ける |
| `credential_terminal` / `credential_input` | WSLの対話端末で非表示入力し、入力中断を避ける |
| `tls_files` / `tls_path` | リポジトリ外の通常ファイルとディレクトリのアクセスを確認する |
| `tls_owner` / `tls_key_permissions` | 所有者と秘密鍵の権限を確認する。今回報告の600は条件を満たす |
| `tls_pair` | 同じ生成時の証明書・非暗号化の葉の鍵を使う。内容をチャットへ貼らない |
| `port_in_use` | 既存サービスを勝手に停止せず、必要なら明示的に別ポートを指定する |
| `port_permission` / `port_unavailable` | loopbackへの待受権限・環境を確認する |
| `synthetic_database` | /tmpへのアクセス、空き容量などを確認する。実DBを代わりに指定しない |
| `server_startup` | TLSサーバー起動が完了しなかった。HTTP/LAN公開に切り替えない |

最初にWSLのプロジェクトディレクトリで、以下の確認コマンドを実行する。
子プロセスだけ既存の開発認証環境変数を外し、意図しない値の再利用を避ける。

```bash
omachi_tls_dir="$HOME/.local/share/omachi-do-dev-tls"
env -u OMACHI_LAB_STAFF_TOKEN .venv/bin/python -B -m staff_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem" \
  --check
```

共有認証情報は32文字以上の印字可能ASCIIで、端末の非表示入力にだけ入力する。
`[check_ok]`ならTLS・ファイル条件・loopbackポート・認証条件・使い捨て合成DB準備が通過した。
このコマンドはサーバーを起動せず、ポート検査も短時間のbindのみでlistenしない。
合成DBは終了時に破棄する。チェック時の認証情報も保存しない。

次に通常起動し、その起動時に入力した値をブラウザのログインにも使う。

```bash
env -u OMACHI_LAB_STAFF_TOKEN .venv/bin/python -B -m staff_https_lab \
  --cert "$omachi_tls_dir/staff.localhost.pem" \
  --key "$omachi_tls_dir/staff.localhost-key.pem"
```

Windowsブラウザでは `https://staff.localhost:8443/staff/login` を開く。
明示的に `--port 8444` に変えた場合は確認・通常起動の両方へ同じ指定を付け、ブラウザのURLも8444にする。
ポートの自動変更や待受アドレスの拡大は行わない。
事前の空きポート検査後に他プロセスが同じポートを使う競合は残るため、その場合も起動失敗を扱う。
再び失敗した場合は固定コードだけを報告し、認証情報・鍵・その内容が見える画面は共有しない。

追加試験では、各入力条件・TLS所有者/権限・実際の鍵不一致・ポート競合・安全な例外表示・
check専用経路と、通常CLIを子プロセスで起動する実TLSログインを確認する。
証明書は一時ディレクトリの合成品だけで、ブラウザやOSの信頼設定は変更しない。
Windows実ブラウザでの最終確認は引き続き未実施である。

今回の検証状況：診断修正後の全Pythonは1612件、JavaScriptは175件が成功した。
その後、終了した接続のTIME_WAITをポート競合と誤認しないよう、ポート検査をPOSIXのUvicornと同じ
SO_REUSEADDR設定に合わせ、再起動試験1件を追加した。
この最終変更後の全Python再実行はツールの実行許可が拒否され、未完了である。
制限内で実行できるHTTPS関連41件は成功したが、再起動試験はsocket作成時の
PermissionError（Operation not permitted）で実行できなかった。
最終コードの全件成功、今回のWindows/WSL2での個別原因解消を確認済みとは扱わない。
