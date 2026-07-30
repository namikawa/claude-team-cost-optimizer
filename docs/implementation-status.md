# 実装ステータス

- 最終更新: 2026-07-30
- 対象設計: [Claude利活用・シート適正化機能 実装設計書](./implementation-design.md)
- 次のタスク: Step 4 QualityIssue

## 1. この文書の目的

実装設計書の仕様と、日々変わる進捗を分離して管理する。

- 実装内容・受け入れ条件: `implementation-design.md`
- 着手・完了・ブロック状況: 本文書
- 機能の背景・優先順位: `claude-adoption-cost-management-proposal.md`

実装設計を変更せずに、各Stepの進捗と検証結果を更新できるようにする。

## 2. ステータス

| ステータス | 意味 |
|---|---|
| `未着手` | 作業を開始していない |
| `進行中` | 現在作業中 |
| `ブロック` | 外部条件・判断待ちで進められない |
| `完了` | 受け入れ条件と必要な検証を満たした |
| `見送り` | 検証結果または方針により実装しない |

`条件付き完了`は使用しない。制約を発見すること自体が目的のFeasibility Stepは、
制約と対応方針を検証記録へ残したうえで`完了`とする。

## 3. 更新ルール

### 着手時

1. 対象Stepを`進行中`へ変更する
2. 同時に複数Stepを`進行中`にしない
3. 依存Stepが`完了`していることを確認する

### 完了時

1. 設計書の受け入れ条件をすべて確認する
2. 必要なテスト・smoke testを実行する
3. 本文書を`完了`へ更新する
4. 完了日と検証結果を「検証記録」へ追記する
5. 次のタスクを更新する

### ブロック時

1. ステータスを`ブロック`へ変更する
2. ブロック理由を検証記録へ記載する
3. 解消条件を明記する
4. 独立して進められる次Stepがなければ作業を止める

### 公開情報

本文書には次を記載しない。

- 実在するOrganization名
- 実在するrepository名
- メンバー名・メール
- 実利用金額
- 組織ごとの実人数・実件数
- token、Cookie、認証情報

検証結果は、実装判断に必要な内容へ一般化して記載する。

## 4. 進捗サマリ

| Track | 内容 | 完了 | 進行中 | ブロック | 未着手 | 見送り |
|---|---|---:|---:|---:|---:|---:|
| 0 | 実機Feasibility | 3 | 0 | 0 | 0 | 0 |
| 1 | 入力と品質 | 3 | 0 | 0 | 2 | 0 |
| 2 | Code中心の利用可視化 | 0 | 0 | 0 | 3 | 0 |
| 3 | シート変更履歴 | 0 | 0 | 0 | 4 | 0 |
| 4 | V2判定 | 0 | 0 | 0 | 7 | 0 |
| 5 | 変更後評価 | 0 | 0 | 0 | 5 | 0 |
| 6 | Browser-assisted取得 | 0 | 0 | 0 | 7 | 0 |
| 7 | GitHub | 0 | 0 | 0 | 8 | 0 |
| 8 | Billingと表示 | 0 | 0 | 0 | 3 | 0 |
| **合計** |  | **6** | **0** | **0** | **39** | **0** |

## 5. Step一覧

### Track 0: 実機Feasibility

| Step | タスク | ステータス | 完了日 | 備考 |
|---|---|---|---|---|
| 0A | GitHub認証の手動smoke test | `完了` | 2026-07-30 | 大規模Organization向けの分割取得要件を確認 |
| 0B | Playwright管理画面smoke test | `完了` | 2026-07-30 | 外部セキュリティ検証が反復するため不採用 |
| 0C | 通常ブラウザ＋download watcher smoke test | `完了` | 2026-07-30 | 実機でSpend CSVの検出・一時コピーに成功 |

### Track 1: 入力と品質

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 1 | Spend任意カラム | `完了` | 2026-07-30 |
| 2 | Members任意カラム | `完了` | 2026-07-30 |
| 3 | subject_id | `完了` | 2026-07-30 |
| 4 | QualityIssue | `未着手` |  |
| 5 | doctorの既存入力検査 | `未着手` |  |

### Track 2: Code中心の利用可視化

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 6 | product policy config | `未着手` |  |
| 7 | Code/全product特徴量 | `未着手` |  |
| 8 | usage-summary.csv | `未着手` |  |

### Track 3: シート変更履歴

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 9 | SeatChangeEvent | `未着手` |  |
| 10 | seat-change-events.csv | `未着手` |  |
| 11 | decision snapshot保存 | `未着手` |  |
| 12 | 推奨と実変更の照合 | `未着手` |  |

### Track 4: V2判定

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 13 | V2 domain | `未着手` |  |
| 14 | asymmetric history config | `未着手` |  |
| 15 | Upgrade rule | `未着手` |  |
| 16 | Downgrade rule | `未着手` |  |
| 17 | Admin credit loader | `未着手` |  |
| 18 | Credit comparator | `未着手` |  |
| 19 | decision-evidence.csv | `未着手` |  |

### Track 5: 変更後評価

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 20 | UsageInterval | `未着手` |  |
| 21 | 2週間評価 | `未着手` |  |
| 22 | 4週間評価 | `未着手` |  |
| 23 | 8週間評価 | `未着手` |  |
| 24 | review.md | `未着手` |  |

### Track 6: Browser-assisted取得

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 25 | Download watcher基盤 | `未着手` |  |
| 26 | Spend検出・配置 | `未着手` |  |
| 27 | Members検出・配置 | `未着手` |  |
| 28 | Code Analytics検出・配置 | `未着手` |  |
| 29 | Collection manifest | `未着手` |  |
| 30 | collect CLI | `未着手` |  |
| 31 | Admin credit入力補助 | `未着手` |  |

### Track 7: GitHub

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 32 | GitHub mapping loader | `未着手` |  |
| 33 | GitHub doctor | `未着手` |  |
| 34 | Repository自動発見 | `未着手` |  |
| 35 | PR検索とraw cache | `未着手` |  |
| 36 | マージPR数 | `未着手` |  |
| 37 | PR lead time | `未着手` |  |
| 38 | github-summary.csv | `未着手` |  |
| 39 | GitHub follow-up参考表示 | `未着手` |  |

### Track 8: Billingと表示

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 40 | 購入席loader | `未着手` |  |
| 41 | 割当と購入の分離 | `未着手` |  |
| 42 | Dashboard統合 | `未着手` |  |

## 6. 検証記録

### 2026-07-30 — Step 0A GitHub認証の手動smoke test

ステータス: `完了`

確認したこと:

- 対象範囲のrepository metadataに必要なread-only権限を確認
- Organization単位でrepositoryを自動列挙可能
- PR本文・diff・codeを取得せず、PR作成日時・マージ日時を取得可能
- GitHub上のissue、PR、repository、設定を変更していない

発見した制約:

- 大規模Organizationを1か月単位で一括検索するとsecondary rate limitへ到達する
- primary rate limitに余裕があってもsecondary rate limitは別に発生し得る

設計へ反映済みの対応:

- 期間を週単位へ分割する
- 直列で取得する
- ローカルcacheへupsertする
- rate limit時に中断・再開する
- 部分取得を確定値として扱わない

コード・ファイル変更:

- なし

テスト:

- コード変更がないため未実施

### 2026-07-30 — Step 0B Playwright管理画面smoke test

ステータス: `完了`

確認したこと:

- headed browserを一時profileで起動できた
- scriptに管理画面のclick、入力、設定変更処理がない
- 対話的な認証フローを試行した

発見した制約:

- 外部セキュリティ検証が反復し、管理画面へ安定して到達できない
- Spend CSVの取得受け入れ条件は満たせなかった

対応方針:

- セキュリティ検証の回避は行わない
- Playwright方式は不採用
- Playwright検証用scriptは削除
- 通常ブラウザとdownload watcherを組み合わせるStep 0Cへ切り替える

コード・ファイル変更:

- Playwright検証用scriptはproductionへ残していない

テスト:

- headed browserの起動と外部セキュリティ検証の制約を確認

### 2026-07-30 — Step 0C 通常ブラウザ＋download watcher smoke test

ステータス: `完了`

確認したこと:

- 通常ブラウザのセッションを利用できた
- 利用者の手動Export後、新しいSpend CSVを5分以内に検出できた
- headerからSpend CSVであることを判定できた
- Spend CSVをGit管理外の一時ディレクトリへコピーできた
- シート・creditを変更していない

セキュリティ確認:

- browser profile、Cookie、認証情報へアクセスしていない
- CSV本文と元filenameをログ出力していない
- 検証用の実データコピーは検証後に削除した
- Downloads内の元ファイルは変更・削除していない

設計へ反映済みの対応:

- Playwright自動取得を通常ブラウザ＋download watcherへ変更
- 管理画面の画面操作とDOM取得を実装対象外へ変更
- CSVがないcredit値は小さな手入力CSVで補完する

コード・ファイル変更:

- `scripts/spike_download_watcher.py`を追加

テスト:

- 合成Spend CSVによる監視・header判定・一時コピー
- 通常ブラウザからの実CSVダウンロードsmoke test
- `uv run ruff check .`
- `uv run pytest`（167件成功）

### 2026-07-30 — Step 1 Spend任意カラム

ステータス: `完了`

実装したこと:

- Spendの`account_uuid`、`user_id`、`gross_spend`、`web_search_count`を正準化
- 現行形式のunderscore区切りと従来形式のspace区切りを同じaliasで処理
- 4列が存在しない旧CSVでは列を`NA`で追加
- Spend CSVを文字列として読み、ID列の先頭ゼロを保持したまま前後空白を除去
- 金額・件数列を数値化
- 現行形式を示す列がある場合、4列の部分欠損を警告
- 4列のalias設定欠損を起動時に検出
- キャッシュ内訳列を誤って`NA`補完対象へ追加しない不変条件テストを追加

確認したこと:

- 新形式CSVを読み込める
- 4つの任意列がない旧形式CSVを読み込める
- ID列に空セルが混在しても、数値形式IDの先頭ゼロと欠損を保持できる
- 空白のみのIDセルを欠損として扱う
- 旧形式と新形式でV1が参照する列の値・型が一致する
- 旧CSVの既存警告へ新しい任意列名を追加しない
- 現行形式の4列が部分的に欠けた場合は、欠損列名を警告する
- report追加、`subject_id`生成、既存判定の変更を行っていない

コード・ファイル変更:

- `config.yaml`
- `src/seat_analyzer/config.py`
- `src/seat_analyzer/ingest.py`
- `tests/test_hardening.py`
- `tests/test_ingest.py`
- `tests/test_pricing.py`

テスト:

- `uv run pytest tests/test_ingest.py tests/test_hardening.py tests/test_pricing.py`（31件成功）
- `uv run ruff check .`
- `uv run pytest`（174件成功）

### 2026-07-30 — Step 2 Members任意カラム

ステータス: `完了`

実装したこと:

- Membersの`account_uuid`、`user_id`、`member_status`を正準化
- 3列が存在しない既存CSVでは列を`NA`で追加
- Members CSVを文字列として読み、数値形式IDの先頭ゼロを保持
- IDとstatusの前後空白を除去し、空白のみの値を`NA`へ統一
- 未知のstatus値は大小文字を含めて変換せず保持
- 3列のalias設定欠損を起動時に検出

確認したこと:

- 新形式と既存形式のMembers CSVを読み込める
- ID列に空セル・空白のみセルが混在しても、数値形式IDの先頭ゼロと欠損を保持できる
- 空白のみのmember statusを欠損として扱う
- 未知のmember statusを入力値のまま保持できる
- 既存CSVの警告へ新しい任意列名を追加しない
- 任意列の有無によらずV1のemailとseat判定の値・型が一致する
- ID join変更、report追加を行っていない

コード・ファイル変更:

- `config.yaml`
- `docs/implementation-design.md`
- `src/seat_analyzer/config.py`
- `src/seat_analyzer/ingest.py`
- `tests/test_hardening.py`
- `tests/test_ingest.py`

テスト:

- `uv run pytest tests/test_ingest.py tests/test_hardening.py tests/test_member_changes.py tests/test_members_info_snapshots.py`（44件成功）
- `uv run ruff check .`
- `uv run pytest`（178件成功）

### 2026-07-30 — Step 3 subject_id

ステータス: `完了`

実装したこと:

- Identity証拠をemail・`account_uuid`・`user_id`で連結する独立モジュールを追加
- `account:<account_uuid>`、`user:<user_id>`、`email:<normalized_email>`の優先順位で
  `subject_id`を生成
- stable IDを共有する複数emailを同一subjectとして解決
- 同じemailだけがstable IDを持つ別入力へIDを伝播
- `stable`、`email_consistent`、`email_fallback`、`conflict`、`unresolved`の品質を実装
- 同一identity内で同種stable IDが複数に分岐した場合をconflictとして検出

確認したこと:

- `account_uuid`と`user_id`が1つずつ併存してもconflictにしない
- email変更があっても同じstable IDなら同じsubjectになる
- stable IDがないemailの履歴十分性を証拠行数から推測しない
- conflict時は`subject_id`を確定しない
- conflictの影響範囲を成分内のemail・stable IDから確認できる
- 空白・欠損だけの証拠は`unresolved`になる
- unresolvedを含めても入力証拠の先頭位置に基づく決定的な順序になる
- 非スカラーのIdentity証拠を入力元名・問題値付きの明確なエラーで拒否する
- V1のemail join、report、シート判定を変更していない

コード・ファイル変更:

- `docs/implementation-design.md`
- `src/seat_analyzer/identity.py`
- `tests/test_identity.py`

テスト:

- `uv run pytest tests/test_identity.py`（14件成功）
- `uv run ruff check .`
- `uv run pytest`（192件成功）
