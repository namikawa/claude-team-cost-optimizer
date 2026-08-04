# 実装ステータス

- 最終更新: 2026-08-04
- 対象設計: [Claude利活用・シート適正化機能 実装設計書](./implementation-design.md)
- 次のタスク: Step 6 product policy config

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
| 1 | 入力と品質 | 5 | 0 | 0 | 0 | 0 |
| 2 | Code中心の利用可視化 | 0 | 0 | 0 | 3 | 0 |
| 3 | シート変更履歴 | 0 | 0 | 0 | 4 | 0 |
| 4 | V2判定 | 0 | 0 | 0 | 7 | 0 |
| 5 | 変更後評価 | 0 | 0 | 0 | 5 | 0 |
| 6 | Browser-assisted取得 | 0 | 0 | 0 | 7 | 0 |
| 7 | GitHub | 0 | 0 | 0 | 8 | 0 |
| 8 | Billingと表示 | 0 | 0 | 0 | 3 | 0 |
| **合計** |  | **8** | **0** | **0** | **37** | **0** |

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
| 4 | QualityIssue | `完了` | 2026-08-01 |
| 5 | doctorの既存入力検査 | `完了` | 2026-08-04 |

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

### 2026-08-01 — Step 4 QualityIssue

ステータス: `完了`

実装したこと:

- 構造化品質issueの語彙を独立モジュールとして追加（severity・code・scope・JSON serializer）
- Severityはerror/warningの2値のみ（doctorのexit code意味論から確定。info等は設けない）
- IssueCodeは設計書§17の25 codeをvalue == nameで過不足なく定義
- QualityIssueはfrozen dataclass。scopeは構築時に検証・正準化し読み取り専用で保持
  （スカラーとスカラー列のみ許可、list→tuple化、非スカラー・非strキー・NaN/infは
  キー名・型名入りエラーで拒否）
- 決定的なJSON直列化（ensure_ascii=False・allow_nan=False・キー順固定）と
  全順序整列を追加
- messageの決定性制約（タイムスタンプ・乱数・実行環境依存値の禁止）をdocstringへ明記

確認したこと:

- `sort_issues`を通せば、同一issue集合から構築順・scopeキー挿入順によらずバイト一致のJSONに
  なる（`issues_to_json`単体は渡された順を保持する。整列は呼び出し側の責務）
- 日本語messageがエスケープされず出力される
- 入力Mappingの後続変更がissueへ波及しない
- 等価なissueのhashが一致し、set/dictキーとして使える
- 既存ファイルの変更ゼロで「既存warning不変」を構造的に担保
- CLI配線・既存warningからの変換は行っていない（Step 5の担当）

コード・ファイル変更:

- `src/seat_analyzer/domain.py`
- `src/seat_analyzer/data_quality.py`
- `tests/test_data_quality.py`

テスト:

- `uv run pytest tests/test_data_quality.py`（23件成功）
- `uv run ruff check .`
- `uv run pytest`（215件成功）

### 2026-08-04 — Step 5 doctorの既存入力検査

ステータス: `完了`

実装したこと:

- `doctor`サブコマンドを追加（`--org`複数指定・`--month`省略時は最新月・`--format text|json`）
- 検査本体を`data_quality.inspect_input`として追加（1組織・1対象月で読み取り専用）
- Spendの検査: 対象月の欠損、ファイル名から採用ファイルを決められない、読めない（必須カラム
  欠落・文字コード）、部分月、ヒステリシス窓の欠月、単価表に無いモデル、必須数値列の解釈失敗
- Membersの検査: 1件も無い、読めない、対象月が無く別月へフォールバック、シート種別の判別不能
- Spend×Membersの突き合わせ: spendに行があるがmembersに居ない、未割当なのに利用実績がある
- errorが1件でもあればexit 1、warningのみ・問題なしはexit 0
- json出力はstdoutをJSONのみに保ち、対象月の通知はstderrへ出す
- IssueCodeへ3 code追加（`MEMBER_ROW_MISSING`・`SEAT_TYPE_UNKNOWN`・`UNASSIGNED_WITH_USAGE`）。
  設計書§17は「最低限のcode」の規定であり、Step 5で検出対象が確定したため追記した

確認したこと:

- 問題の無い入力では issue ゼロ・exit 0 になる
- 警告だけなら exit 0、errorがあれば exit 1 になる
- ingestの例外メッセージに含まれる入力ディレクトリのパスをmessageへ持ち込まない
  （入力ディレクトリからの相対表記へ落とし、同一入力で常に同じ文字列になる）
- 同じ入力を2回検査するとJSON出力がバイト一致する
- 複数組織で組織ごとに検査し、scopeのorgで識別できる。旧レイアウト（単一組織）では
  scopeにorgを入れない
- doctorはファイルを作成・変更しない（実行前後でツリーが一致）
- `analyze`・`report`・`ingest`・`pricing`のコードを変更していない
- GitHub・browser・admin設定・code-analytics・members-infoは検査していない

コード・ファイル変更:

- `README.md`
- `docs/implementation-design.md`
- `src/seat_analyzer/cli.py`
- `src/seat_analyzer/data_quality.py`
- `src/seat_analyzer/domain.py`
- `tests/test_cli.py`
- `tests/test_data_quality.py`

テスト:

- `uv run pytest tests/test_cli.py tests/test_data_quality.py`（58件成功）
- `uv run ruff check .`
- `uv run pytest`（243件成功）
- 実データ3組織でのsmoke test（exit 0）。既知の状態（観測月が1ヶ月のみの組織で履歴月欠落、
  対象月当時のメンバー一覧が無い組織でフォールバック）をwarningとして検出し、
  それ以外の組織ではissueが出ないことを確認

外部レビュー Round 1（指摘9件・全件をコードで再現確認のうえ修正）:

1. 欠月時のmessageが`analyze`の実挙動と不一致だった。`analyze`は暦上の連続性ではなく
   存在する過去月だけで連続同推奨を判定するため、欠月があっても「変更推奨」は出る。
   「要観察に留まる」という誤案内を、判定の質が下がる旨の文言へ修正
2. `spend/`を持たない組織を検査対象にできなかった（全組織モードでは黙って除外、
   `--org`指定ではJSONを出さずにエラー終了）。doctorの組織発見条件を「既知の入力
   サブディレクトリかmembers-info.csvを持つディレクトリ」へ広げた
3. データ行が無いメンバー一覧（ヘッダのみ）がerrorにならなかった。全ユーザがシート不明に
   なり判定が成立しないためerrorとし、突き合わせ検査は行わないようにした
4. `PermissionError`等の読み取り失敗が構造化issueにならず、traceback で終了して
   `--format json`のstdoutがJSONにならなかった。読み取り経路の`OSError`を
   `MISSING_SPEND`/`MISSING_MEMBERS`のerrorへ変換した
5. 対象月を決められない場合にMembersの検査まで打ち切っていた。Spendの解決可否とMembersの
   有無を独立に検査し、「ファイル名から解決できない」と「1件も無い」を区別した
6. 複数組織のJSONを全体整列していなかったため、`--org`の指定順で同一issue集合でも
   バイト列が変わり、warningがerrorより前に出ていた。JSON化の直前に全体を整列した
7. modelセルが空の行はdefault単価が適用されるのに`UNKNOWN_MODEL`を出せなかった
   （`unmatched_models`が欠損を除外するため）。空セルの行数を別に数えて警告へ含めた
8. 単日日付で命名されたスペンドを部分月として検出できなかった。ただし実際の集計期間は
   ファイル名から分からないため、日数を断定せず「全月と確認できない」警告にした
9. `NUMERIC_PARSE_FAILED`の`scope.rows`が失敗セル数になっていた。影響行数へ修正し、
   セル数は別キーへ分離した

テスト（Round 1 修正後）:

- `uv run ruff check .`
- `uv run pytest`（253件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一（新たな誤検出なし）

外部レビュー Round 2（指摘7件・全件をコードで再現確認。6件を修正、1件は文書を修正）:

1. 組織名が`members`・`code-analytics`のとき、その組織ディレクトリを旧レイアウトの目印と
   誤認して「混在」エラーになっていた（analyzeでは組織として扱える）。組織候補として
   発見したディレクトリは旧レイアウト判定から除外した
2. 対象月を確定できない経路では、メンバー一覧の有無しか見ずデータ行の有無・可読性を
   検査していなかった（Round 1 の指摘がこの経路で再発）。最新候補を実際にロードして
   「読めない」「データ行が無い」をerrorにした
3. 組織の発見中に発生した`OSError`（存在しない・読めない入力ディレクトリ）が構造化issueに
   ならず、`--format json`のstdoutが空だった。`input_unavailable_issues`で構造化errorへ
   変換した。使い方の誤り（組織名の誤り・レイアウト混在）は従来どおりstderr + exit 1
4. 月が存在しても`month=None`で呼ぶと`MISSING_SPEND`errorになり、「対象月を特定できません
   （存在する月: ...）」と自己矛盾した案内をしていた。`month=None`を「最新月を対象にする」
   意味に統一し、月を確定できない場合だけ月単位の検査を省くようにした
5. 先頭ドットのディレクトリをvalidate_org_nameより前に除外していたため、入力構造を持つ
   不正名の組織をdoctorだけが黙って無視していた。候補に含めてanalyzeと同じ検証で拒否する
6. 欠月messageのテストがdoctorの文字列しか検証しておらず、analyzeの実挙動が変わっても
   通り続ける状態だった。同じ入力でanalyzeも実行し、statusが「変更推奨」になることを
   同一テストで照合した
7. `issues_to_json`が入力順に依存する点は、整列（`sort_issues`）と直列化を分離したStep 4の
   意図した契約であり、テストでも両方を固定している。暗黙ソートは名前から読み取れない
   順序変更になり、呼び出し側が出力順（組織単位・時系列・検出順）を選べなくなるため採らない。
   この裁定をレビュアへ差し戻して意見を求め、「条件付き同意（正準出力用の名前付きAPIを
   分けること）」との回答を得たため、次のとおり整理した。
   - `issues_to_json` は指定順を保持する低水準の直列化（契約は変更せず、docstringを明確化）
   - `issues_to_canonical_json` を追加し、内部で`sort_issues`を適用する。機械可読出力は
     この境界だけを使う（整列を呼び出し側の記述に依存させない）
   - 不変条件の記述を正確にした。保証されるのは「同一のissue多重集合を`sort_issues`で
     正準順序化してから直列化した場合に同一の文字列になること」であり、直列化関数単体の
     性質ではない（集合ではなく多重集合、バイト一致ではなく文字列一致）
   - レビュアの指摘のうち「暗黙ソートに機能上の利得がない」「完了済みStepのテスト変更に
     なる」という当初の論拠2点は誤りと認め、維持の根拠を「順序保持という契約自体の妥当性」に
     置き換えた

テスト（Round 2 修正後）:

- `uv run ruff check .`
- `uv run pytest`（259件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一

外部レビュー Round 3（指摘5件・全件をコードで再現確認のうえ修正。high severityなし）:

1. 直下の`spend/`まで旧レイアウト判定から除外していたため、組織名`spend`をdoctorだけが
   通常組織として受理し（analyzeは混在エラー）、直下の旧形式CSVを黙って無視していた。
   `spend/`は常に旧レイアウトの目印とし、除外は`members`・`code-analytics`に限定した
2. `_reason`が`入力ディレクトリ + "/"`しか置換していなかったため、パスが文中に単独で現れる
   例外（`{input_dir} に入力データがありません`）で絶対パスがmessageに残っていた。
   Round 2で追加した経路自体が決定性の不変条件を破っていた。入力ディレクトリ自身も固定語へ
   置換し、あわせて自分が投げる例外にはパスを埋め込まない方針にした
3. 入力ディレクトリが存在しない場合でも`--org`を付けると組織名の検証が先に走り、構造化issueに
   ならなかった（Round 2の修正が`--org`指定時に再発）。入力ディレクトリの可否を組織名の検証より
   先に判定するようにした
4. 対象月なし経路のメンバー一覧の採用がファイル名の辞書順だったため、同一月に複数ある場合に
   スナップショット日付の古い方を選び、誤った`MISSING_MEMBERS`が出ることがあった。
   自前の選択をやめて`ingest.load_members`の重複解決規則に委ねた
5. 組織も対象月も未解決のとき、text出力の見出しが「入力 入力検査」と重複していた

各巡の指摘は「前巡の修正が作った新しい経路」に集中した（Round 2は7件中3件、Round 3は5件中3件）。
新経路を追加したら、既存経路に対して確立した不変条件を同じ強さで満たしているかを確認する。

テスト（Round 3 修正後）:

- `uv run ruff check .`
- `uv run pytest`（264件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一
