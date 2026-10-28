# 人数選択と受付完了通知の確認手順

今回の変更はタブレットUIだけ。トップ画面の基本構成、モデル・予測条件・認証・DB契約は維持する。
実ブラウザは作業環境にないため、以下の実描画・タッチ確認は未実施。HTML構造、CSS規則、JavaScript、HTTP統合テストで検証している。

## 1024×768での確認

1. READMEの開発用DBでサーバーを再起動し、`http://127.0.0.1:8000/`を開く。ブラウザの表示領域を1024×768 CSSピクセル、拡大率100%にする。
2. トップの参考待ち時間・待ち組数・受付ボタンの構成を確認し、「受付する」を押す。
3. 大人・子供・合計人数、選択人数の参考予測、受付確定ボタンがスクロールなしで見えることを確認する。0人・101人が送信できず、子供だけ1人は送信できることも確認する。
4. 予測が取得中・予測不可・通信失敗でも確定できること、説明を開いても確定ボタンが下へ押し出されないことを確認する。予測条件の都合で数値が出ない場合は、そのまま確認し、モデル条件を変えて数値を作らない。
5. 確定すると直ちにトップへ戻り、「受付完了」「日付付き受付番号」「受付人数」だけが見えることを確認する。通知に内部スクロールがなく、一覧と次の受付ボタンが使えることを確認する。
6. 通知中に次の受付を開始し、前のお客様の情報が消えることを確認する。操作しない場合は30秒で通知が消える。未送信の人数選択だけは60秒の無操作で戻り、送信中・結果不明では戻らない。
7. 通信遮断や受付APIの応答喪失を模擬し、完了通知が出ず、同じ人数・同じ`Idempotency-Key`で再送されることをNetworkで確認する。公開一覧の更新と登録数を開発用DBで確認し、再送で別受付が増えないことを確認する。

1366×768、768×1024、390×844、文字拡大時も確認する。狭い画面や長いエラー文では、文章を隠す代わりにページスクロールを許す。人数ボタンは64px、本文は18pxを維持する。

## ブラウザ上の寸法確認

人数選択画面を開き、開発者ツールのConsoleで実行すると主要要素が表示領域内か確認できる。これはブラウザ内の読取検査だけで、受付やAPI通信を発生させない。ソフトウェアキーボードを出していない状態で確認する。

```js
const ids = ["adults", "children", "total-people", "entry-prediction-value", "submit-button"];
console.table(ids.map(id => {
  const element = document.getElementById(id);
  const r = element.getBoundingClientRect();
  return { id, top: r.top, bottom: r.bottom, left: r.left, right: r.right,
    visible: r.width > 0 && r.height > 0 && r.top >= 0 && r.left >= 0
      && r.bottom <= innerHeight && r.right <= innerWidth };
}));
```

完了通知が出ている間は、次の検査で内部スクロールの有無を確認できる。

```js
const notice = document.getElementById("receipt-view");
console.table({
  shown: !notice.hidden,
  hasVerticalOverflow: notice.scrollHeight > notice.clientHeight + 1,
  hasHorizontalOverflow: notice.scrollWidth > notice.clientWidth + 1,
  overflowY: getComputedStyle(notice).overflowY,
});
```

寸法検査だけで読みやすさ・指での操作・読み上げを保証するものではない。実機では文字の読取、誤操作のしにくさ、人数入力のキーボード操作も確認する。
