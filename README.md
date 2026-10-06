# vwd-site-quality-audit

app-navi.biz の公開記事について、AdSense「有用性の低いコンテンツ」対応のための**読み取り専用**品質監査ツール。パイロット(20〜30ページ)での検証を経て、全件クロール(`post-sitemap.xml`の全URL、2026年10月時点で635件)にも対応。

## スコープと安全性

- **読み取り専用。** `GET` リクエストのみ。WordPress管理画面・Xserverサーバーパネル・記事の公開状態には一切触れない。
- 対象記事は、`post-sitemap.xml`(WordPress標準のサイトマップ)から取得した実在する公開記事URLに限定。推測でURLを組み立てない。**REST APIは使用しない**(サイト側でセキュリティ対策として匿名REST APIが無効化されているため。この監査ツール側でREST APIを再有効化するような変更は一切行わない)。
- `robots.txt` の `Disallow` を尊重する。
- 失敗時のリトライは最大2回まで(無限リトライなし)。失敗は `failures.jsonl` に記録して処理を継続。
- リクエスト間に1.5秒の間隔を空け、サイトへの負荷を抑える。
- 現在は `workflow_dispatch`(手動トリガー)のみ。無人夜間実行(スケジュール実行)は、パイロット結果を確認してから別途追加する。

## 実行方法

- パイロット(20〜30ページ、全テーマ+ホームページを均等サンプリング):GitHubの「Actions」タブ → `Pilot content quality audit (app-navi.biz)` → `Run workflow`。
- 全件(`post-sitemap.xml`の全URL+ホームページ):同タブ → `Full content quality audit (app-navi.biz)` → `Run workflow`。所要時間は概ね20分前後(635ページ×約1.8秒/件、1.5秒間隔込み)。

完了後、Artifacts(パイロットは`audit-report`、全件は`full-audit-report`)をダウンロードすると以下が入っている。

- `report.md` — 人間向けサマリー(全サンプルの一覧表、構造化データの問題、類似度が高い/固有テキストが少ないページの一覧)
- `report.json` — 機械可読な詳細データ
- `sample.json` — 今回選ばれたサンプルページの一覧
- `failures.jsonl` — 取得失敗したURLとその理由
- `estimated_load.md` — 全件クロールに拡大した場合の推定所要時間・リクエスト数
- `magi_classification.md` / `.json` — 内部MAGI判定表に基づくA/B/C/D一次分類(下記参照)

## MAGI一次分類(A/B/C/D)

`report.json` を元に `audit/classify_magi.py` が同じワークフロー内で自動実行され、各記事を以下の3軸でスコアリングする(100点正規化、A≧75 / B≧60 / C≧40 / D<40)。

- テーマ内類似率(30点)
- 固有文比率(25点)
- 全国版との類似率(20点)

**地域固有性(本来25点分)はここでは自動判定していない。** `report.json` には比較に使った後の本文テキストが残っておらず、「地域固有の観察が書かれているか」はテキスト内容を読まないと判断できないため。すべてのページに `region_specificity_pending: true` が付き、C/D判定のページは地域固有性を人力(またはテキストを読む別パス)で確認してから下書き化などの判断をすること。感染症・人口・道路のテーマは `priority_review` フラグ付きで別表にまとめる。

## 計測項目(第1段階)

- URL生存(HTTPステータス)
- 本文文字数
- 同一テーマ内の本文類似率(`difflib` によるペア比較)
- 全国版と地域版の類似率
- ページ固有テキスト量(推定)
- 内部リンク数・外部(出典候補)リンク数(本文内のみ、およびナビゲーション込みのページ全体、の2系統で計測)
- Dataset構造化データ(JSON-LD)の有無・`description`/`license`の有無

## 第2段階(未着手)

地図表示速度、JavaScript地図初期化、画像読み込みなどのPlaywright系検査は、この第1段階が安定してから追加する。
