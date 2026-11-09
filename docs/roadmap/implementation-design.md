# Claude利活用・シート適正化機能 実装設計書

- ステータス: Draft
- 最終更新: 2026-10-12
- 対象: `seat-analyzer` v1.0 以降の段階的拡張
- 関連文書: [Claude利活用・シート適正化機能提案書](./claude-adoption-cost-management-proposal.md)
- 進捗管理: [実装ステータス](./implementation-status.md)

## 1. この設計書の目的

本書は、提案書の機能を一度に実装せず、小さく独立した変更として積み上げるための
実装契約である。

実装者が都度大きな設計判断を行わなくても済むよう、各Stepに次を定義する。

- 目的
- 対象ファイル
- 実装内容
- 受け入れ条件
- 今回は実装しないこと
- 依存するStep

## 2. 小さく実装するための規則

### 2.1 1 Step、1つの振る舞い

各Stepは、原則として1つの外部から確認可能な振る舞いだけを追加する。

例:

- 任意カラムを読める
- シート変更をCSVへ出せる
- upgrade候補だけをV2で判定できる
- GitHub認証をdoctorで確認できる
- 管理画面CSVを専用プロファイルのChromeと同梱拡張機能で取得できる

複数の振る舞いを同じPRへまとめない。

### 2.2 変更量の目安

- production codeは原則1〜3ファイル
- テストは対象機能ごとに1ファイル
- 大規模なrename、移動、全面リファクタリングを同時に行わない
- 新機能と既存コード整理を同じStepで行わない
- 既存のHTML全体を書き換えない

この目安を超える場合は、Stepをさらに分割する。

### 2.3 各Stepを独立してリリース可能にする

- 既存V1を常に動作可能にする
- 新機能は未設定時に無効になる
- 新しい入力はすべて任意とする
- 新しい出力は既存出力と別ファイルから始める
- V2はfeature flagまたはCLI引数で有効にする
- 途中Stepでも全テストが成功する

### 2.4 将来機能を先回りしない

現在のスコープ外:

- OpenTelemetry
- effort、speed、subagent
- retry、error、compaction
- Jira、CI/CD、DORA
- Enterprise Analytics API
- 管理画面のシート・credit自動変更

これらの抽象化や汎用基盤を現在のStepで作らない。

## 3. 確定した要件

### 3.1 目的

- コスト削減より、Standard/Premium/usage creditの適正化を優先する
- Claude Code利用の促進を重視する
- シート変更担当者とは別の観点から判断をレビューする
- 担当者個人ではなく、シート変更という意思決定を評価する

### 3.2 判定

```text
Standard → Premium:
  1か月でも十分に強い需要があれば候補化

Premium → Standard:
  2か月連続の低利用で候補化

一時的なStandard高需要:
  Premiumとusage creditを比較
```

### 3.3 運用

- 週次にCSVを取得する
- 週次は急増・停止・変更検出
- 月次はシート候補・変更後効果・GitHub参考指標
- シート変更は担当者が管理画面で行う
- システムはread-only
- 変更履歴の手入力を要求しない
- 追加の月次手作業は5分以内を目標とする

### 3.4 データ取得

- Spend、Members、Code Analyticsは公式CSV
- 管理画面CSVは専用プロファイルのChromeと同梱拡張機能による取得を許容（§14）
- CSVがない管理画面表は小さな手入力CSVを使用する
- GitHubはローカルの認証済み`gh`を使用
- GitHubリポジトリはOrganization単位で自動発見
- ソースコード、diff、PR本文は取得しない

### 3.5 プロダクト

- Claude Codeをprimaryとする
- Chat、Cowork、Design等はsupplementaryとする
- 費用には全プロダクトを含める
- シートの判定（昇格・降格・credit）は全プロダクトの需要で行い、primary / supplementaryの
  区別を条件にしない（Track 11）。開発者以外が主に使う組織では、CoworkやChatが主な利用先に
  なるため
- 活用の可視化（Code / 全productの特徴量、usage-summary、dashboardのproduct軸、§27）は
  primaryをClaude Codeとして続ける
- 禁止機能はpolicy warningとする

### 3.6 分析単位

- 新機能はClaude組織ごとに独立して分析する
- GitHub OrganizationもClaude組織ごとに1つ設定する
- 組織を跨いだユーザー結合・最適化を行わない
- 既存の複数組織一括実行機能は維持してよい

ここでの組織は入力ディレクトリ（`input/<組織名>/`）の単位。1つの組織が複数のTeamスペース
（claude.ai上では別のorganization）を持つ場合の扱いは§26。

## 4. ゴールと非ゴール

### 4.1 ゴール

- stable IDを利用できる
- シート変更をMembers差分から検出できる
- upgradeとdowngradeで異なる履歴条件を使える
- Standard/Premium/creditを比較できる
- 推奨と実変更を照合できる
- 変更後2/4/8週を評価できる
- GitHub PR数・リードタイムを参考表示できる
- 管理画面CSVを専用プロファイルのChromeと同梱拡張機能で取得できる
- 既存V1を壊さずV2へ移行できる

### 4.2 非ゴール

- シート・creditの自動変更
- 個人の人事評価
- PR指標を判定の正解ラベルにすること
- 5時間枠・週次上限の直接観測
- OpenTelemetryの導入
- 常時稼働サーバ
- リポジトリの手動列挙
- 非公開APIの利用
- プロンプト、レスポンス、コード本文の収集

## 5. アーキテクチャ

### 5.1 現行

```text
CSV
  → ingest.py
  → pricing.py
  → analyze.py
  → report.py
  → report / dashboard / recommendations（6.2の命名規則）
```

### 5.2 目標

```text
Official CSV ───────────────┐
Admin table / settings ─────┤
GitHub PR metadata ─────────┤
                            ▼
                 Loader / Data Doctor
                            ▼
                   Canonical DataFrame
                            ▼
             Features / Seat change events
                            ▼
           V1 decision      V2 decision
                  │             │
                  └──────┬──────┘
                         ▼
               Evidence / Decision audit
                         ▼
                Markdown / CSV / HTML
```

### 5.3 モジュール

既存:

```text
src/seat_analyzer/
  cli.py
  config.py
  ingest.py
  pricing.py
  analyze.py
  report.py
```

段階的に追加:

```text
src/seat_analyzer/
  domain.py             # enum、dataclass、reason code
  data_quality.py       # 構造化issue、doctor
  identity.py           # subject_id
  product_usage.py      # Codeと全productの特徴量
  seat_changes.py       # Members差分の正準event
  usage_intervals.py    # 累積snapshotから区間利用へ変換
  decision_v2.py        # upgrade/downgrade/credit
  decision_audit.py     # 推奨と実変更の照合・追跡
  admin_inputs.py       # seat/creditの正準入力
  claude_export.py      # claude.ai の CSV 取得（計画・検証・配置・Chrome の起動と終了）
  browser_extension/    # Chrome 拡張機能（非 .py の同梱物。ページ操作とダウンロードの振り分け）
  github_collect.py     # ghによるPR metadata
  github_metrics.py     # PR数・lead time
  report_v2.py          # 新規CSV/Markdown
```

上の2つのブロックは設計時点の想定で、現状とは次の点が異なる。モジュールの一覧と層の正は`src/seat_analyzer/`の構成と`tests/test_module_deps.py`の`LAYERS`で、以下はその要点だけを書く。

- `report.py`は`report/`パッケージになっている。`__init__.py`が公開APIとオーケストレーションで、主なファイルは出力形式ごとの`markdown.py`/`html.py`/`csv_out.py`と、共通処理の`document.py`/`format.py`/`text.py`
- `analyze.py`も`analyze/`パッケージになっている（主なファイルは`__init__.py`/`pipeline.py`/`credits.py`/`midmonth.py`）
- 段階的に追加する一覧のどれが既にあるかは`src/seat_analyzer/`の構成で確かめる。一覧に無いものとして、考察執筆まわりの`discussion.py`/`leakcheck.py`/`public_text.py`もある
- `report_v2.py`という単一モジュールは作らない。新しい出力は`report/`パッケージの中へ、出力形式ごとに足す（CSVなら`report/*_csv.py`）。以降のStepで対象として`report_v2.py`または`report.py`と書かれている箇所は、この方針に読み替える。責務ごとの実ファイル名は各Trackの着手時に個別に確定させる（着手しないTrackを先回りして書き換えない。§2.4）
- V2判定の結線（SubjectHistoryの組み立てと判定の実行）は`decision_evidence.py`（層25）、CSVの書き出しは`report/evidence_csv.py`
- 設定の既定は`src/seat_analyzer/default-config.yaml`が唯一の源。リポジトリ直下・ワークスペースの`config.yaml`は差分だけを書く任意の上書きファイルで、gitignore済み。以降のStepで対象として`config.yaml`と書かれている箇所は`default-config.yaml`に読み替える
- email → GitHub loginの対応表は`members-info.csv`の`GitHub ID`列で持つ。以降のStepで`github-members.csv`と書かれている箇所は`members-info.csv`の`GitHub ID`列に読み替える
- モジュールを増やすときは`tests/test_module_deps.py`の`LAYERS`へ層を割り当てる。パッケージ内importは自分より厳密に下の層だけを指してよく、同層どうしのimportも認めない。層に無いモジュールを足すとテストが落ちる
- Track 6 は方式を変更し、Step 25〜30 を見送って Step 50〜51 で実装する（§14）

## 6. ディレクトリ

### 6.1 入力

```text
input/<org>/
  spend/
  members/
  code-analytics/
  members-info.csv        （GitHub ID 列を含む）
  admin/
    organization-YYYY-MM-DD.csv
    users-YYYY-MM-DD.csv
  github-cache/
    prs-YYYY-MM.json      （schema 2 から PR ごとのコミットを含む。§7.6）
  otel-cache/
    otel-YYYY-MM.json     （Claude Code の OpenTelemetry の集計。§7.7・§27）
```

### 6.2 出力

既存:

```text
reports/<org>/<month>/
  report-<YYYYMM>-<org>.md
  details-<YYYYMM>-<org>.md
  dashboard-<YYYYMM>-<org>.html
  recommendations-<YYYYMM>-<org>.csv
  usage-summary-<YYYYMM>-<org>.csv
  preview-<YYYYMM>-<org>.md              # 速報モードのみ
  preview-dashboard-<YYYYMM>-<org>.html  # 速報モードのみ
reports/summary/
  <month>.md              # 複数組織を一括実行した場合のみ
```

組織×月ディレクトリの成果物は`{種別}-{YYYYMM}-{組織名}.{拡張子}`で命名する（共有で
フォルダの外へ出したときにファイル名だけで判別できるようにするため）。名前を組み立てる
のは`report/naming.py`の1箇所。`summary/<month>.md`は対象外（担当者へ共有しない内部の
文書で、月は既に名前にある）。以下で種別名だけを書いている箇所も同じ規則に従う。

`details`・`usage-summary`・`preview`・`preview-dashboard`・`summary/<month>.md`はこの
設計書の作成後に追加された既存出力で、いずれもgoldenテストのバイト一致比較の対象。
不変条件を書くときはこれらを落とさない。

追加:

```text
reports/<org>/<month>/
  data-quality.json
  seat-change-events.csv
  decision-evidence.csv
  decision-audit.csv
  follow-up.csv
  github-summary.csv
  maturity-panel.csv       # §27.8。読者は CTO と開発責任者
  maturity-dashboard.html  # 同上
  review.md
  history/
    <snapshot-id>/
      decision-evidence.csv
      source-manifest.json
```

`maturity-panel`・`maturity-dashboard`は読者が違う（§27.8）。既存の成果物と同じ月ディレクトリに
置くが、組織の担当者へ共有する成果物の集合には含めない。

### 6.3 取得のstaging

```text
~/.seat-analyzer/exports/<run_id>/
```

- stagingの配置（実行ごとに置くファイルと`run_id`の形）は§14.3が正
- 置き場所は`claude_export.staging_dir`で変更できる（§14.3・§21）
- CSV全体を走査せず、種別判定に必要なheaderだけを読む
- stagingのファイルは変更しない（`input/`へはコピーする）
- 利用者の通常のブラウザプロファイル・認証情報には触れない（使うのはツール専用のプロファイルだけ）

## 7. 正準データ

### 7.1 Spend

既存カラムに任意列を追加する。

| カラム | 必須 | 説明 |
|---|---:|---|
| `email` | Yes | 正規化済みメール |
| `account_uuid` | No | 安定ID |
| `user_id` | No | 安定ID |
| `product` | Yes | product |
| `model` | Yes | model |
| `requests` | No | request数 |
| `prompt_tokens` | Yes | input tokens |
| `completion_tokens` | Yes | output tokens |
| `gross_spend` | No | 割引前 |
| `net_spend` | Yes | 実課金 |
| `uncached_input_tokens` | No | 未cache |
| `cache_read_tokens` | No | cache read |
| `cache_write_5m_tokens` | No | cache write |
| `cache_write_1h_tokens` | No | cache write |
| `web_search_count` | No | Web Search |
| `month` | Yes | `YYYY-MM` |
| `source_file` | Yes | 原本 |

キャッシュ内訳の4列は、4列そろうか1列も無いかのどちらか。一部だけある場合はロード時エラー。

任意カラムがない場合は`NA`とする。0で補わない。

### 7.2 Members

| カラム | 必須 | 説明 |
|---|---:|---|
| `email` | Yes | 正規化済みメール |
| `account_uuid` | No | 安定ID |
| `user_id` | No | 安定ID |
| `seat_type` | Yes | standard/premium/unassigned/unknown |
| `member_status` | No | active/invited/deactivated等 |
| `snapshot_date` | No | snapshot日 |
| `source_file` | Yes | 原本 |

`member_status`は前後空白だけを除去し、大小文字を含む入力値を保持する。
既知statusとの比較が必要な場合は、入力値を失わないよう利用側で正規化する。

### 7.3 GitHub members

`input/<org>/members-info.csv`の`GitHub ID`列

```csv
email,部署,チーム,職種,追加クレジット上限,備考,GitHub ID
user@example.com,架空推進3部,Nebula-AI,エンジニア,250,,example-user
```

値:

- 空欄 = 未記入（未対応として扱いwarning）
- `なし` / `none` / `-` = 対象のGitHub OrganizationでPRを作らない（アカウントが無い、または別の
  Organizationで開発する）と判断した人（未対応として扱うがwarningなし。月々のPRの有無では
  書き換えず、開発する場所が変わったらloginを書く）
- それ以外 = GitHubのlogin

制約:

- emailは一意（対応表として読むときは重複をerrorにする。属性のloaderは最後の行で畳む）
- loginは組織内で一意（大小文字を区別しない）
- 重複はerror
- 未対応はwarning

### 7.4 Admin organization

`input/<org>/admin/organization-YYYY-MM-DD.csv`

| カラム | 必須 | 説明 |
|---|---:|---|
| `snapshot_date` | Yes | 取得日 |
| `standard_purchased` | No | 購入数 |
| `premium_purchased` | No | 購入数 |
| `billing_frequency` | No | monthly/annual |
| `renewal_date` | No | 更新日 |
| `standard_unit_price_usd` | No | 契約単価 |
| `premium_unit_price_usd` | No | 契約単価 |
| `org_credit_enabled` | No | 組織credit |
| `org_credit_limit_usd` | No | 組織上限 |
| `source` | Yes | browser/manual/invoice |

1ファイル1行とする。

### 7.5 Admin users

`input/<org>/admin/users-YYYY-MM-DD.csv`

| カラム | 必須 | 説明 |
|---|---:|---|
| `snapshot_date` | Yes | 取得日 |
| `email` | Yes | メンバー |
| `account_uuid` | No | stable ID |
| `credit_enabled` | No | 有効・無効 |
| `credit_limit_usd` | No | 個人上限 |
| `credit_mtd_usd` | No | 月途中消費 |
| `source` | Yes | browser/manual |

### 7.6 GitHub PR cache

保存する:

- repository
- PR number
- author login
- createdAt
- mergedAt
- additions
- deletions
- isDraft
- author type

schema 2 から、PR ごとのコミットも保存する（§15.7。行数をコミットの作者に帰属させるため）:

- oid（SHA）
- author login（GitHub アカウントに結び付く場合。結び付かなければ null）
- author email（コミットの作者のメールアドレス。login が無いときの突き合わせに使う）
- additions
- deletions
- committedDate
- マージコミットか（親が2つ以上）
- PR の `merge_commit_oid`（PR のマージで作られたコミットの SHA。無ければ null）
- コミットの総数（GitHub は PR のコミットを250件までしか返さない。保存した件数は総数か、
  総数が250を超えるときは250に一致する）

schema 1 のキャッシュはそのまま読める。`collect`は schema 1 の内容を引き継がず、その月の
全窓を取り直して schema 2 で書く（キャッシュは GitHub から作り直せる）。

保存しない:

- branch name
- title
- body
- comments
- review本文
- files
- diff
- commit message
- commit author name
- code

一意キー:

```text
repository + "#" + PR number
```

### 7.7 OpenTelemetry cache

Claude Code の OpenTelemetry（§27）を BigQuery から読み、人 × 日 × 次元の集計だけを月ごとに
保存する。

保存する:

- schema
- organization（入力ディレクトリの組織名）
- month
- source（project・dataset・table の名前と、使った SQL テンプレートの名前と版）
- organization_ids（絞り込みに使った claude.ai の組織 UUID）
- query（読み取り量の見積もりと実績 byte・実行日時 UTC）
- rows: 1 行が 人（`user.email`）× 日（UTC）× 集計名 × 属性の組 × 値
  - 集計名と属性は §27.5 の表に閉じる（それ以外の属性は SQL に書かない）
  - 値は件数・合計・秒・USD・トークン数のいずれか

保存しない:

- プロンプト本文・応答本文（`prompt`・`prompt_text`・`response`）
- ツールの引数・入力・出力（`tool_parameters`・`tool_input`・Bash のコマンド）
- API の本文（`body`）
- エラー文（`error`）
- ホストのパス（`workspace.host_paths`）
- リポジトリの URL（`vcs.repository.url.full`。owner と name だけを保存する）
- セッション ID・request ID（集計の粒度より細かい識別子）

一意キー:

```text
email + "|" + date + "|" + 集計名 + "|" + 属性の組
```

## 8. Identity

### 8.1 subject_id

優先順位:

1. `account:<account_uuid>`
2. `user:<user_id>`
3. `email:<normalized_email>`

### 8.2 ID伝播

1. 全入力から`email ↔ stable ID`を収集
2. emailにstable IDが1つだけなら確定
3. Spendのstable IDを同じemailのMembersへ伝播
4. conflict時は伝播しない

### 8.3 品質

| 値 | 条件 |
|---|---|
| `stable` | stable ID |
| `email_consistent` | 必要期間で同じemail |
| `email_fallback` | 履歴不足のemail |
| `conflict` | 複数ID |
| `unresolved` | emailもない |

conflictはシート判断を保留する。

- 1つの`account_uuid`と1つの`user_id`の併存は正常とする
- 同一identity内で同種stable IDが複数に分岐した場合をconflictとする
- conflictは矛盾した連結成分全体を保留し、部分的なsubject確定を行わない
- conflictをQualityIssueへ変換する際は、影響件数と影響したemail・stable IDを
  `scope`へ含める
- conflictのレポートセクションはissueが存在するときだけ表示する
- `email_consistent`は必要期間の履歴を呼び出し側が確認した場合だけ使用し、
  証拠行数から自動推測しない

## 9. Product usage

### 9.1 Policy

```yaml
product_policy:
  primary:
    - "Claude Code"
  supplementary:
    - "Chat"
    - "Cowork"
    - "Design"
    - "Research"
    - "Code Review"
    - "Claude in Slack"
  prohibited: []
  supplementary_high_usd: 100.0
```

`supplementary_high_usd`は`supplementary_high`の閾値。supplementary productの需要が
この額以上なら真とする。

`prohibited`は導入組織のポリシーに応じて設定する。公開する既定例には、
特定組織のセキュリティ方針を含めない。

### 9.2 特徴量

| 特徴量 | 定義 |
|---|---|
| `total_demand_usd` | 全productのAPI等価需要 |
| `code_demand_usd` | primary productの需要 |
| `code_demand_share` | code / total |
| `total_requests` | 全product |
| `code_requests` | primary product |
| `product_breadth` | requests比5%以上のproduct数 |
| `supplementary_high` | supplementary需要が閾値以上 |
| `prohibited_observed` | 禁止productの行がある |

`total_demand_usd=0`ならshareは`NA`とする。

### 9.3 判定への利用

- シート判定（§12）は`total_demand_usd`だけを見る。`code_demand_usd`・`supplementary_high`は
  判定の条件にしない（Track 11で分類軸を外した）
- 活用の可視化と習熟の時系列（§27）は`code_demand_usd`を主にする
- 禁止productはseat判定へ影響させずpolicy warning

## 10. Seat change event

### 10.1 正準event

```python
@dataclass(frozen=True)
class SeatChangeEvent:
    subject_id: str
    email: str
    from_seat: str
    to_seat: str
    changed_after: date
    changed_before: date
    detected_at: date
    previous_source: str
    current_source: str
```

### 10.2 event type

- `standard_to_premium`
- `premium_to_standard`
- `assigned_to_unassigned`
- `unassigned_to_assigned`
- `member_added`
- `member_removed`

### 10.3 重複

一意キー:

```text
subject_id + from_seat + to_seat + changed_after + changed_before
```

同じ入力で再実行してもeventを重複出力しない。

### 10.4 分類できない観測と不完全なスナップショット

- データ行が無いスナップショットは観測として扱わず、ペアの対象にしない
  （既存のdoctorが同じ入力をerrorとするのと整合させる）
- 不在と判断した側にIdentity値を1つも持たない行がある場合、その不在を根拠とする
  event（member_added / member_removed）は作らない（不在を確定できないため）
- unknown・identity conflict・同一時点でのシート食い違いは、変更の実体を分類
  できないためeventにしない。ただし「変更なし」と区別できるよう、検出器はeventとは
  別に未分類の観測（subject・区間・理由）を返す。V2判定はrecent窓と重なる未分類
  区間を`OBSERVE`側へ倒す材料としてこれを使う（§12.7）
- identityが確定しているsubjectについて、区間の両端が同じ値の組は、unknown
  どうしであっても観測を残さない。unknownの連なりへの出入りは必ず端のペアで
  未分類観測になるため、recent窓との重なりはそれで捕捉できる。窓が unknown の
  連なりの内側に収まる場合は月末時点のシートがunknownなので、V2のhard blocker
  （current seat unknown）が受け持つ
- identity conflictはシート値によらず観測を残す（両端が同じシートでも、別人の
  入れ替わり＝同一emailの再割当と区別できず、変更なしと確定できないため）

## 11. Usage interval

2/4/8週評価のため、月初からの累積Spendスナップショットを区間利用へ変換する。

### 11.1 同一月

```text
snapshot A: 07-01..07-07 cumulative=100
snapshot B: 07-01..07-14 cumulative=180

interval A: 07-01..07-07 demand=100
interval B: 07-08..07-14 demand=80
```

### 11.2 月替わり

新しい月の最初のsnapshotは、そのファイルの開始日から終了日を1区間とする。
前月累積値との差分を取らない。

### 11.3 interval columns

- subject_id
- interval_start
- interval_end
- observed_days
- total_demand_usd
- code_demand_usd
- billed_extra_usd
- total_requests
- code_requests
- source_before
- source_after

### 11.4 品質

- 累積値が減少した場合はwarning
- シート変更と同じ区間にある利用は`mixed_seat_interval=true`
- mixed intervalは主評価から除外し、参考値にする
- 7日未満のintervalは短期変動としてflagを付ける

## 12. Decision V2

### 12.1 enum

```python
class DecisionStatus(StrEnum):
    RECOMMENDED = "recommended"
    OBSERVE = "observe"
    NO_DECISION = "no_decision"
    KEEP = "keep"
    EXCLUDED = "excluded"


class SeatAction(StrEnum):
    KEEP = "keep"
    UPGRADE_TO_PREMIUM = "upgrade_to_premium"
    DOWNGRADE_TO_STANDARD = "downgrade_to_standard"
    NONE = "none"


class CreditAction(StrEnum):
    KEEP = "keep"
    ENABLE_WITH_CAP = "enable_with_cap"
    REVIEW = "review"
    NONE = "none"
```

StrEnumは文字列と等値になるため、語彙をまたいだ等値比較が成立する（3つの`KEEP`
どうし、IssueCodeと同名のReasonCode等）。混同は型では防がれないので、V2の
値オブジェクト・関数境界は受け取る語彙を`isinstance`で検証し（`QualityIssue`と
同じ流儀）、異なる語彙を同じset・dictのキー空間に混ぜない。

### 12.2 reason code

- `SUSTAINED_LOW_TOTAL_DEMAND`
- `TOTAL_DEMAND_ABOVE_PREMIUM_LINE`
- `SUSTAINED_OVERAGE`
- `SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE`
- `CREDIT_LIMIT_REACHED`
- `CREDIT_SETTING_UNKNOWN`
- `STANDARD_BILLING_ABOVE_SEAT_GAP`
- `CREDIT_CONSUMPTION_RISING`
- `RECENT_MEMBER`
- `RECENT_SEAT_CHANGE`
- `PARTIAL_MONTH`
- `INSUFFICIENT_HISTORY`
- `IDENTITY_CONFLICT`
- `CURRENT_SEAT_UNKNOWN`
- `CAPACITY_SIGNAL_UNAVAILABLE`
- `DATA_CONFIDENCE_LOW`（分類できない観測（§10.4）がrecent窓に重なるときの保留理由）

Track 11で削除した語彙（それ以前のdecision-evidenceにだけ現れる）: `SeatAction.REVIEW_ASSIGNMENT`、
`ReasonCode`の`ONE_MONTH_STRONG_CODE_DEMAND`・`SUSTAINED_LOW_CODE_DEMAND`・`HIGH_SUPPLEMENTARY_USAGE`・
`REVIEW_NON_CODE_USAGE`。いずれも需要の中身（Codeか補助productか）をシート判定の条件にしていた
名残で、§3.5・§9.3の変更で不要になった。

### 12.3 Cost

変更先シートの従量課金は観測できず、推定もしない。判定に使うのは観測できる事実と、方針
として決めた値だけとする。

- 観測: 現シートの実課金、usage credit上限κへの到達
- 方針: `premium_justification_usd`（Premiumシートが正当化される全product需要の下限）

月次需要だけから変更先の課金は推定しないため、V2判定ではallowanceを使わない。
V1は従来どおりallowanceを使う。需要の方針線は課金額の推定値ではない。

費用の比較はシート差額に閉じる。Standardの実課金がシート差額（Premium − Standard）を
`observed_billing_margin_usd`以上上回っていることを、席を上げる方針条件とする。差額と
ちょうど同額はこの条件を満たさない（条件は「上回った」ことなので）。

需要の側は方針線との比較だけで見る。降格は評価窓の全完全月で全product需要が方針線未満、
実課金が観測できない組織の昇格は対象月の全product需要が方針線以上であることを条件とする。
方針線は推定値ではないので、allowanceとの大小関係は検証しない。

昇格と降格を毎月往復しないための歯止めは、ヒステリシスだけが担う。降格は評価窓
（`downgrade.min_complete_months`完全月）の全月で条件が成立することを要し、κ無効の昇格は
方針線以上が2完全月続くこと（または月内の継続上昇）を要し、recent窓（`recent_seat_change_days`）
がシート変更の直後の判定を観察へ倒す。方針線の付近を行き来する利用者の席は、最短でも数か月の
周期でしか動かない。往復が起きても戻せばよい（昇格・降格はいずれも可逆な操作）という運用を
前提に、需要の中身（Code需要）によるゲートは置かない（§3.5）。

5時間枠・週次上限を観測できないため、`CAPACITY_SIGNAL_UNAVAILABLE`を情報として付ける。
この理由だけで全候補を保留にはしない。

### 12.4 Upgrade

必要履歴:

```yaml
decision_v2:
  upgrade:
    min_complete_months: 1
```

候補条件:

1. current seatがStandard
2. 完全月が1か月以上
3. usage credit上限κの状態に応じた信号が成立
4. partial month、identity conflictではない

条件3は費用の軸で、シートの込み枠がproduct共通であることから全product合算で評価する。使う
信号は3つあり、どれを見るかはκの状態が決める。

| κ | 候補化の信号 | 結論 |
|---|---|---|
| 有効（正の有限値・無制限） | 実課金がシート差額を上回る、またはκへ到達 | 成立なら`UPGRADE_TO_PREMIUM`。不成立なら現状維持 |
| 無効（0） | 全product需要が方針線以上 | 継続性があれば`UPGRADE_TO_PREMIUM`、無ければ`ENABLE_WITH_CAP`（§12.6） |
| 不明（None） | 実課金がシート差額を上回る | 成立なら`UPGRADE_TO_PREMIUM` + `CreditAction.REVIEW`。不成立で需要が方針線以上なら席は動かさず`REVIEW`だけ |

実課金の経路は`observed_billing_margin_usd`以上の超過を要求し、超過が正であることも要求
する（`observed_billing_margin_usd: 0`の設定でも、シート差額とちょうど同額の実課金は
「上回った」を満たさない）。usage credit上限への到達だけは金額差ではなく上限そのものを
根拠にし、到達には実課金の発生を伴う（実課金ゼロを到達とみなさない）。

κが不明な場合、席を動かす判断は実課金だけで行う。有効か無効かが分からないと、実課金ゼロが
「枠内に収まっている」のか「そもそも課金されない設定」なのか決められないため。需要が方針線
以上でも席は動かさず、クレジット設定の確認（`REVIEW`）だけを作業として出す。

κが0（無効）と記入されているのに対象月の実課金が正である場合は、記入と観測が矛盾している
（記入ミス、または月中の設定変更）。この場合はκ不明として扱い、実課金という観測を捨てない。
見るのは対象月の実課金だけとする（前月の課金は設定変更より前のものでありうる）。

需要の中身（Codeか補助productか）は昇格の条件にしない（§3.5・§9.3）。費用の軸が成立した候補は
そのまま昇格の推奨とし、理由コードには候補化の信号（§12.6）と`CAPACITY_SIGNAL_UNAVAILABLE`を
添える。

### 12.5 Downgrade

必要履歴:

```yaml
decision_v2:
  downgrade:
    min_complete_months: 2
```

候補条件:

1. current seatがPremium
2. 直近2完全月で全product需要が低い
3. 実課金・credit上限到達がない
4. 直近のシート変更ではない
5. partial month、identity conflictではない

条件2〜3の評価窓は直近の完全月`min_complete_months`ヶ月で、間に部分月が挟まる場合は
飛ばして完全月だけを採る。誤ったdowngradeは業務を止めるため、条件は窓の全月で成立する
ことを要求する。

条件3は窓のいずれかの月に実課金があれば候補から外す（Premiumでの実課金は、需要が
Premiumの込み枠を超えた観測）。credit上限への到達は実課金の発生を伴うため、この検査に
含まれる。

条件2は費用の軸で、窓の全月で全product需要が方針線（`premium_justification_usd`）未満で
あることを要求する。境界（ちょうど同額）は方針線以上として扱い、降格の候補にしない。
成立した降格の理由コードは`SUSTAINED_LOW_TOTAL_DEMAND`と`CAPACITY_SIGNAL_UNAVAILABLE`。

需要の中身（Codeか補助productか）は降格の条件にしない（§3.5・§9.3）。Code需要の大きい利用者も、
全product需要が方針線未満の月が窓に続けば降格の候補になる。方針線はシート差額の数倍の水準に
置いてあり、その未満の需要はStandardの枠で足りるとみなす。

### 12.6 Credit

κが無効な組織で使える信号:

- 全product需要と方針線の比較（§12.3）

継続性:

- 1か月だけ方針線以上
- 2か月以上連続で方針線以上
- 週次snapshotで当月消費が継続上昇

一時的に方針線以上なら`ENABLE_WITH_CAP`、継続していれば`UPGRADE_TO_PREMIUM`とする。

credit設定が不明なら金額を断定せず`CreditAction.REVIEW`とする。

継続性のゲートを適用するのはcreditが無効な組織（上限κ=0）の昇格だけとし、creditが有効な
組織は§12.4どおり1完全月で判定する。実課金という観測がある組織では観測を優先し、観測が
構造的に得られない組織（κ=0では実課金が常に0）でだけ継続性で代替する。

κ=0で需要が方針線以上になったのが1か月だけの場合は、席を変えず`ENABLE_WITH_CAP`とする。
上限つきcreditの付与は可逆な計測手段で、1か月の課金実測を取ることが「実課金が構造的に0の
ためPremium昇格の根拠が永久に出ない」行き詰まりへの回答になる。

週次snapshotの継続上昇は2完全月連続と同等の継続性として扱い、対象月内の当月消費の観測点が
3点以上あって取得日順に狭義単調増加していることを要求する。当月消費は月次でリセットされる
ため比較は対象月の点だけで行い、横ばいは上昇とみなさない（消費が止まった状態と区別できない）。

継続性はいずれの形（2完全月連続・月内の継続上昇）でも、対象月で需要が方針線以上であること
を前提とする（継続性は「その状態が続いているか」を見る条件で、それ自体は候補化の根拠に
ならない）。κ=0の結論は次の3つに分かれる: 方針線以上で継続性もあれば`UPGRADE_TO_PREMIUM`。
方針線以上だが継続性が無ければ`ENABLE_WITH_CAP`に留まる。方針線に届かなければ現状維持で、継続
上昇だけでは候補にならない。

credit設定が不明な場合、席を動かす判断は実課金だけで行う（§12.4）。credit側は上限も有効・
無効も分からず金額を断定できないため、`ENABLE_WITH_CAP`ではなく`REVIEW`を重ねる。`REVIEW`を
出すのは実課金または方針線で候補になったユーザだけで、どちらも成立しないユーザには出さない
（設定の不明だけで人へ回すと、確認すべき相手が組織の全員になる）。

statusは人が取るべきアクションがあるかで決まる。seat actionが`UPGRADE_TO_PREMIUM`・
`DOWNGRADE_TO_STANDARD`のとき、またはcredit actionが`ENABLE_WITH_CAP`・`REVIEW`のときは
`RECOMMENDED`とする。seat側が`OBSERVE`でもcredit側に作業があれば`RECOMMENDED`になり、
どちらの側の作業かはseat actionとcredit actionが示す。

理由コードは、方針線以上の需要に`TOTAL_DEMAND_ABOVE_PREMIUM_LINE`、それが2完全月連続で
続いている場合に`SUSTAINED_TOTAL_DEMAND_ABOVE_PREMIUM_LINE`、月内の消費上昇に
`CREDIT_CONSUMPTION_RISING`を使う。実課金がシート差額を上回って成立した候補には
`STANDARD_BILLING_ABOVE_SEAT_GAP`を、それが2完全月連続で続いている場合に
`SUSTAINED_OVERAGE`を添える。usage credit上限への到達だけで候補になった場合は実課金と
シート差額の関係を立証していないため`STANDARD_BILLING_ABOVE_SEAT_GAP`を添えない（§12.4）。
`TOTAL_DEMAND_ABOVE_PREMIUM_LINE`は方針線が候補化の信号になった場合（κ無効、またはκ不明で
実課金が条件を満たさない場合）にだけ添える。

### 12.7 Confidence

次元:

- usage coverage
- identity quality
- seat history coverage
- credit setting coverage
- policy stability

policy stabilityは、方針線（`premium_justification_usd`）を±$100ずらした3通り（−・基準・
+の順）でseat actionを評価し、基準と同じseat actionになった数（基準を含むので1〜3）とする。
3は「線の置き場所に結論が依存していない」、1は「線をどこへ引くかで結論が変わる」を表す。
判定が費用の軸に到達しない場合（hard blocker、またはrecent窓で`OBSERVE`へ倒れた場合）は
値を持たない（None）。「3/3で安定」と「方針線が判定に関与しなかった」を区別するため。

Billing契約情報やOTelをupgrade/downgradeの必須条件にしない。

hard blocker:

- partial month
- identity conflict
- current seat unknown
- 必要履歴不足

current seat unknownは`CURRENT_SEAT_UNKNOWN`を理由に`NO_DECISION`とする（membersに行が
無い利用者。黙って除外すると判定から漏れたことが出力に残らない）。

recent memberとrecent seat changeは既定で`OBSERVE`へ落とす。

recent seat changeの判定材料はSeatChangeEvent（§10）とする。eventの区間
`changed_after..changed_before`が、分析月末から遡って`recent_seat_change_days`の
窓と重なる場合をrecentとみなす。スナップショット間隔が広く変更時点を絞れない場合は
区間が広がり、この重なり判定によって保留側（`OBSERVE`）へ倒れる。スナップショット
ペアが無くeventを検出できない場合は`RECENT_SEAT_CHANGE`を発火させずに判定を進め、
seat history coverageとして確度へ反映する（hard blockerにしない）。分類できない
観測（§10.4）がrecent窓と重なるsubjectは、eventが無くても`OBSERVE`側へ倒す。
したがってV2判定はTrack 3のStep 10〜12を前提にしないが、Step 9には依存する。

## 13. Decision audit

### 13.1 推奨snapshot

各正式分析で、既存レポートとは別に変更しないsnapshotを保存する。

```text
reports/<org>/<month>/history/<snapshot-id>/decision-evidence.csv
```

`snapshot-id`:

```text
<source-end-date>-<short-source-hash>
```

同じsource hashなら上書きしてよい。異なるsourceなら別snapshotとする。

### 13.2 実変更との照合

シート変更eventごとに、`changed_after`以前で最も新しいdecision snapshotを選ぶ。

分類:

- `matched`
- `recommended_not_changed`
- `changed_without_recommendation`
- `changed_opposite`
- `not_evaluable`

### 13.3 2/4/8週

起点は`changed_before`とする。

評価窓:

- 2w: 1〜14日
- 4w: 1〜28日
- 8w: 1〜56日

各窓の必要coverage:

```yaml
follow_up:
  min_coverage_ratio: 0.60
```

coverage未満は`not_evaluable`。

比較する主指標:

- Code demandの日次rate
- total demandの日次rate
- billed extraの日次rate
- Code requestsの日次rate

参考:

- merged PR count
- PR lead time

### 13.4 結果ラベル

Premium化:

- `usage_increased`
- `usage_maintained`
- `usage_not_increased`
- `not_evaluable`

Standard化:

- `usage_maintained`
- `credit_increased`
- `usage_dropped`
- `reupgraded`
- `not_evaluable`

閾値はconfig管理とし、最初はラベルより数値を主表示する。

## 14. Browser-assisted collection

入力のCSV 3種（members・支出レポート・Claude Code analytics）を、管理画面から人がダウンロード
しているのと同じ公式のエクスポートとして`collect --source claude`で取得する。既定では不活性で、
ワークスペースの`config.yaml`に`claude_export`（`profile`と`org_id`）を書いた組織／workspace
だけが対象になる（§21。mainへマージするコードは既定で不活性という運用に合わせる）。
モジュールは`claude_export.py`（層15）と、非.pyの同梱物`browser_extension/`。

### 14.1 方式

- ツール専用のプロファイル（アカウントごとに1つ。`~/.seat-analyzer/profiles/<profile>`）で、
  自動操作の無い素のGoogle Chromeを起動する。同梱の拡張機能（unpacked・MV3）はそのプロファイルへ
  一度だけ読み込む
- CLIはChromeの実行ファイルにトリガーURL（`https://claude.ai/#seat-analyzer-run=<URLエンコードした
  JSON>`）を渡して起動し、stagingディレクトリ（`~/.seat-analyzer/exports/<run_id>/`）に
  manifest.jsonが出るのを待つ。実行内容はURLのフラグメントに載せるので、claude.aiのサーバーへは
  送られない
- 拡張機能は、組織の切替（Cookie）、3ページのエクスポート操作、ダウンロードのstagingへの振り分け、
  manifestの出力を行う
- 取得の操作が一定の間隔で並ばないよう、遷移の完了から操作までの待ち（2〜4秒）と、ボタンや
  選択肢を押す前の待ち（0.5〜1.5秒）は毎回一様乱数で選ぶ
- ログインの確認（`--check-login`）では、拡張機能がclaude.aiを開いてページの状態・API
  （`/api/organizations`）の読み取り・セッションのCookie（`sessionKey`）の期限を`session.json`に
  書き、CLIがプロファイルごとに有効・期限間近・無効・不明を判定する。人の操作は待たない。
  取得の最後にもCookieの期限だけを書き、CLIが1行添える
- 再ログイン（`--login`）では、拡張機能がセッションのCookie（名前が`sessionKey`で始まるもの）を
  消してからログイン画面を開く。有効なセッションのままログイン画面を開くとアプリへ戻されて
  再認証にならないため、期限の前に更新するには先に消す
- 対象月は2モードだけ: 当月（支出は「今月」・旧表記の「月累計」と英語の「Month to date」も認識・
  Claude Codeは表示中の月）と前月（支出は「先月」・
  Claude Codeは月送り1回）。ツールが対応するのは当月と前月のみで、それより前の月は
  従来どおり手動のダウンロードで置く
- 取得できる時期はUTCの日付で決める（Step 55）。claude.aiのエクスポートの期間はUTC基準で、支出
  レポートの「今月」はUTCで完了した日まで、membersとClaude Code analyticsの取得日はUTCの日付に
  なる。ツールの当月・前月は実行機のローカル日付で決まる（`resolve_mode`）ので、UTCの今日が
  ローカルの月に追いつく前に取ると別の月の期間で届く（支出レポートとmembersは§14.3の検証で止まり、
  Claude Code analyticsは画面の表示月の名前で検証を通り、取得日が期間の外なので終了日の置き換えが
  働かず月末日の名前のまま当月に置かれうる）。当月モードはUTCの今日の年月がローカルの年月と同じで
  日が2以上のとき（当月にUTCで完了した日がある）、前月モードはUTCの今日の年月がローカルの年月と
  同じとき（UTCでも前月が終わっている）だけ取得し、それ以外はChromeを起動せずに理由を表示する。
  理由は3通り: 必要なUTCの時点（当月モードは対象月の2日0:00 UTC、前月モードは対象月の翌月の
  1日0:00 UTC）にまだ達していないときは「取得できる時刻」（その時点をローカル時刻に直したもの。
  UTCより東の1日・2日の朝のほか、UTCとローカルが同じ年月の1日も含む）、UTCが先に翌月へ進んで
  いるとき（UTCより西のローカルの月末の夜）は、当月モードなら「ローカルが翌月になってから前月
  モードで取る」、前月モードなら「claude.aiの先月はもう別の月なので手動で置く」（待っても取れる
  ようにならない）。`--month`を省略した実行は終了コード0（取るものが無いだけで失敗ではなく、
  定期実行が誤って通知しない）、`--month`を明示した実行は1（頼まれた月を取れていない）。`--dry-run`も
  同じ表示で、`--import`はrun.jsonの月で検証するので対象外。プロファイルは順に起動するので、
  各プロファイルの起動直前にも判定し直す: いまのローカル日付で`resolve_mode`を引き直して計画と
  同じモード・月になり、かつ時期の規則を満たすときだけ起動し、そうでなければ対象月を変えずに
  そのプロファイルを失敗にする（取得の途中でUTCの月が変わる、UTCとローカルが同時に翌月へ進む、
  など月末の0:00（UTC）の直前に始めた実行のため）。日本時間では当月は2日9:00以降、前月は1日
  9:00以降になる
- Playwright・Chrome remote debugging（CDP）・headlessは使わない。自動操作下では外部セキュリティ
  検証が反復して管理画面へ安定して到達できず、ダウンロード時にブラウザが異常終了することもある
  ため、素のChromeと拡張機能の組み合わせだけを使う
- OS依存はChromeの場所（OSごとの既定の場所か`claude_export.chrome_path`）、プロセスの列挙・
  終了、プロファイルのロック（Unixはflock、Windowsはmsvcrt.locking）の3箇所に閉じる。macOSで
  実機検証し、Linux・Windowsは設計上動く想定（実機は未検証。ロックとプロセスの一覧の解析は
  CIの3 OSでテストする）

### 14.2 守ること

- ID・パスワードを受け取らない。ログインと外部セキュリティ検証は人が行い、自動化・回避しない
  （画面に要操作を表示して待つ）
- 利用者の通常のブラウザプロファイルには触れず、ツールが作った専用プロファイルだけを使う。
  専用プロファイルの設定（Preferences）に書くのは、claude.aiからの自動ダウンロードの許可と、
  ダウンロードの確認を出さないこと・ダウンロード先（staging）だけで、他の項目は保つ
- claude.ai上で押すのはエクスポート系のボタンだけで、シート・creditの設定を変更する操作を持たない
- セッションのCookieは期限だけを見て、値はファイルにもログにも書き出さない。Cookieを
  書き換えるのは組織の切替（`lastActiveOrg`）と、`--login`の前のセッションのCookieの削除だけ
- ダウンロード先は専用のstagingで、利用者のDownloadsや既存ファイルを変更・削除しない
- 別組織のデータを保存しないため、組織切替の確認が取れない組織は丸ごと飛ばす。切替の直後
  だけでなく支出レポートとClaude Codeのページを開くたびにも確かめ、取れなければその種別を
  飛ばす（ログインし直すと表示する組織が変わることがあるため）。配置の前にはファイル名の
  組織UUIDも設定の`org_id`と照合する（§14.3の検証）
- 同じプロファイルを2つの実行が同時に使わない。CLIは取得・`--list-orgs`・`--check-login`・
  `--finish-setup`の間、`<staging_dir>/<profile>.lock`にOSのファイルロック（Unixはflock、Windowsは
  msvcrt.locking）を取り、別の実行が持っていれば止める。ロックは実行が終わると（異常終了を
  含めて）OSが外すので、取り残されたロックという状態は無く、ファイルは消さず空のまま残る。
  ロックを掛けられない環境（ネットワークのファイルシステム等）ではロック無しで進めずに止める。
  拡張機能もsessionストレージの`activeRun`で、同じChromeで進行中の別の実行があれば新しい
  実行を始めない（開始から1時間を超えた記録は無視する。何も書かない`--login`は対象外）
- 終了させるのは、プロファイル（`--user-data-dir`）が一致し、実行ファイルが起動に使ったものと
  一致するか名前にchrome / chromiumを含むプロセスだけ（Linuxの`google-chrome`はラッパー
  スクリプトで、プロセスには実体のパスが見えるため）。強制終了の前に列挙し直し、まだ一致して
  いるものだけを対象にする（待つ間に終わったpidが別のプロセスに再利用されていても終了させない）

### 14.3 manifestと配置

stagingの配置（run.jsonはCLI、それ以外は拡張機能が書く）:

```text
<staging_dir>/<run_id>/run.json                   起動の前に書くその実行の計画
<staging_dir>/<run_id>/progress.json              各手順の後に上書きする途中経過
<staging_dir>/<run_id>/session.json               ログインセッションの観測（--check-login と、取得のmanifestの前）
<staging_dir>/<run_id>/manifest.json              最後に書く結果（CLIはこれを待つ）
<staging_dir>/<run_id>/<dir>/<kind_dir>/<元ファイル名>
<staging_dir>/<run_id>/orgs.json                  組織一覧（--list-orgs）のとき
<staging_dir>/<profile>.lock                      OSのファイルロックの対象（空のまま残る）
```

`dir`は`<org>`か`<org>/<workspace>`（区切りはOSによらず`/`）、`kind_dir`は種別の入力
サブディレクトリ（members→`members`、spend→`spend`、code→`code-analytics`）。`run_id`は
`<profile>-<mode>-<YYYYMMDD-HHMMSS>`（ローカル時刻）。stagingを既定のダウンロード先にし、
manifest.json・progress.json・orgs.json・session.jsonは
`download`に`<run_id>/<固定名>`を直接指定し、返却IDだけを追跡する。listener登録後の検索で、
ID返却前に保存が完了した場合も拾う。JSONは共有routingを操作せず、開始前に
`jsonDownload:<Blob URL>`を独立したstorage.sessionキーとして登録し、
自拡張IDとURLが完全一致する場合だけ登録済みの相対名とoverwriteをbackgroundで指定する。
完了後は期待パスとの終端一致を確認し、登録削除とBlob URL解放を行う。登録・削除失敗でもURLを解放し、
後片付け失敗で元の例外を隠さない。CSV取得中はroutingのprefixで自拡張以外の取得を元のファイル名のまま
振り分ける。CSVの先着採用・遅着による取り違えは残課題で、対象操作との一意な対応付けは別途調べる。

```json
{
  "run_id": "corp-previous-20261007-110009",
  "mode": "previous",
  "finished_at": "2026-10-07T02:00:49.000Z",
  "results": [
    {"dir": "example/main", "kind": "members", "ok": true, "filename": "members-...-2026-10-07.csv"},
    {"dir": "example/main", "kind": "spend", "ok": true, "filename": "spend-report-...-2026-09-01-to-2026-09-30.csv", "range": "2026-09-01 to 2026-09-30"},
    {"dir": "example/main", "kind": "code", "ok": false, "reason": "export button not present after 60s"}
  ],
  "log": ["..."]
}
```

- 壊れたJSON・`results`の欠落・オブジェクトでない要素は読み込みを止める。`ok`でもファイル名が
  無い（またはディレクトリ・ドライブ・`:`を含む）結果は失敗として扱う
- manifestの`dir`は計画の (配置先, 種別) と突き合わせるだけで、パスは常に計画の側から組む。
  計画にあってmanifestに無い組み合わせは「結果なし」の失敗、計画に無い結果は捨てる
- 拡張機能は`results`をspecの全 (org, kind) について1件ずつ書く。組織の切替を確認できずに
  飛ばした組織は、そのすべてのkindを`organization switch not confirmed`の失敗にする。支出
  レポートやClaude Codeのページで組織を確認できなければ、そのkindだけを同じ理由の失敗にする。
  `filename`はChromeが保存したファイルのbasename
- progress.jsonはmanifestと同じ形に`"status": "running"`を付けたもの。CLIは待機中に読めた
  ものから、増えた結果を1行ずつ表示する（書きかけで読めないmanifestは待ち続ける）
- run.jsonは`{run_id, profile, mode, month, created_at, spec}`。`--import <run_id>`はこれから
  計画を組み直し、Chromeに触れずに検証・配置だけを行う。対象はspecの (dir, uuid) を設定の
  対象と突き合わせて決め（設定に無いdirや、設定とUUIDが違うdirがあれば止める）、配置先は
  設定から組む。検証はその実行の対象月で行う（取り込む日の当月ではない）
- 組織一覧（`--list-orgs`）のrun_idは`<profile>-list-orgs-<YYYYMMDD-HHMMSS>`、specは
  `{run_id, action: "list-orgs"}`。拡張機能はclaude.aiのタブで`/api/organizations`を読み、
  orgs.jsonに`[{uuid, name, rate_limit_tier, plan}]`（planは`plan_display_name`。無い値は
  null）を書く。取得できなければ`{"error": 理由}`を書く。APIの読み取りには10秒の上限を付ける
- ログインの確認（`--check-login`）のrun_idは`<profile>-check-login-<YYYYMMDD-HHMMSS>`、specは
  `{run_id, action: "check-login"}`。拡張機能は`session.json`に`{run_id, checked_at,
  cookie_found, cookie_expires_at, page, api, log}`を書く。`page`は`app`・`login`・`challenge`・
  `unknown`、`api`は`{ok: true, status, organizations}`（件数だけ）・`{ok: false, status, reason}`・
  null。取得でもmanifest.jsonの前に、Cookieの期限だけを同じ形（`page`・`api`はnull）で書く
  （補助の情報なので、その失敗では取得を落とさない）
- 再ログイン（`--login`）のrun_idは`<profile>-login-<YYYYMMDD-HHMMSS>`、specは
  `{run_id, action: "login"}`。CLIは実行ディレクトリを作らず、ロックも取らない。拡張機能は
  何も書かず、進行中の実行の記録（`activeRun`）による拒否と登録の対象にもしない（ログイン
  待ちで止まった取得が同じChromeに残っていても再ログインできる）

検証（最初に外れた理由を表示する）:

1. ファイルが存在し、空でない
2. 先頭行のヘッダに、種別ごとの必須の正準列がすべてある（照合は`columns.<種別>`のエイリアスと、
   分析の読み込みと同じ正規化で行い、欠けていれば最初の1列を理由にする）。先頭行は64 KiBまで
   読み、それを超えるものは途中で切れたヘッダとして失敗にする
   - members・支出レポート: 分析の読み込みが必須にする列（`ingest.REQUIRED_COLUMNS`。membersは
     email・seat_type、支出レポートはemail・model・prompt_tokens・completion_tokens）
   - Claude Code analytics: emailと月間のLoC（`loc_with_cc`）。エクスポートはこの2列で、
     支出レポートとの取り違えをLoCの列の有無で止める
3. membersと支出レポートは、ファイル名に組織UUIDの形が含まれていれば設定の`org_id`と同じ
   （大文字小文字は区別しない）。Claude Code analyticsのファイル名にはUUIDが入らないので見ない
4. ファイル名の期間が対象月に合う
   - 支出レポート: 期間付きで、開始が対象月の1日・終了が対象月の末日以前
   - Claude Code analytics: 期間付きで、開始が対象月の1日（終了日は部分月でも月末日になるので見ない）
   - members: スナップショットの日付が対象月の1日以降（前月モードでは当日＝翌月の日付になる）

配置:

- 配置先は`input/<org>/[<workspace>/]<kind_dir>/<元ファイル名>`。元のファイル名のまま置くので、
  分析側の期間の解釈（`ingest.file_period`）がそのまま効く
  - 例外: 当月モードのClaude Code analyticsは、ファイル名の期間の終了日を取得日に置き換えた名前で
    置く（Step 54。`claude_export.placement_name`）。エクスポートは部分月でも終了日が月末日で届くが、
    分析は終了日をスナップショットの時点に使う（`ingest.code_snapshots`）ため。取得日はUTCの日付
    （同じ実行のmembersのファイル名の日付と揃う）で、取得ではrun.jsonの`created_at`と同じ時刻から、
    `--import`では`created_at`から求める。取得日が期間の外か終了日と同じとき、前月モード、他の
    種別は元の名前のまま。検証はstagingの元の名前のファイルに対して行い、stagingの名前は変えない
- 組織ディレクトリ（入れ子レイアウトならworkspaceのディレクトリ）が無ければ配置しない
  （`init-org`で作ってから）。設定の綴り違いで新しい組織ができるのを防ぐ。種別のディレクトリは
  無ければ作る
- バイトのままコピーし、同じディレクトリの一時名から置き換える。同名は上書きする（同じ日の
  再取得は新しいスナップショット）
- 検証に通らないファイルは配置しない

### 14.4 フォールバック

- 失敗した種別は理由を表示し、ダウンロードしたファイルはstagingに残す
- 手動でダウンロードして従来どおり`input/`へ置けば分析は動く。分析そのものをブラウザに依存させない
- タイムアウト時はChromeを終了させず、ブラウザに出ている要操作の表示を確認するよう案内する。
  人の操作で取得が続いてmanifestが書かれたら、`--import <run_id>`で検証・配置だけをやり直す

### 14.5 定期実行の雛形（macOS launchd）

定期実行と通知はツールの機能にせず、`examples/launchd/`に置く雛形（Step 56）で行う。wheelには
入れない（利用者はリポジトリから取る）。CLIにインストールのサブコマンドも持たせない（OS固有の
インストール処理をツールに持ち込まないため）。

- 構成は4ファイル。`collect.zsh`（本体）、`seat-analyzer.collect-weekly.plist`・
  `seat-analyzer.collect-monthly.plist`（launchdのジョブ定義。パスは`__HOME__`・`__WORKSPACE__`の
  プレースホルダ。launchdは`~`や環境変数をパスに展開しない）、`install.zsh`（プレースホルダを埋めて
  `~/Library/LaunchAgents/`に置き`launchctl bootstrap`する。再実行は入れ替え、`--uninstall`は
  bootoutとplistの削除。ワークスペースに`config.yaml`が無ければ止める。
  埋める値をXMLとしてエスケープし、`plutil -lint`を通してからplistを置く。
  本体は`~/.seat-analyzer/scheduled/`へ写すので、実行時にリポジトリは要らない）
- 本体の動き: 引数はweekly / monthly。作業ディレクトリはplistの`WorkingDirectory`（ワークスペース）を
  そのまま使う。zshは`-f`で起動し、`.zshenv`も読まない。launchdが起動するzshは
  `.zprofile`・`.zshrc`を読まないので、コマンドの場所はplistの
  `EnvironmentVariables`で与える: PATHは`__HOME__/.local/bin`（`uv tool install`の置き場所）・
  Homebrew・OS標準の順、コマンドは`SEAT_ANALYZER_CMD`（既定`seat-analyzer`。開発機のように
  `uv run seat-analyzer`にもできる。本体はzshの`${=SEAT_ANALYZER_CMD}`で空白で単語に分けて
  実行する。引用符は解釈せず、空白を含むパスは非対応＝`uv tool install`の置き場所に空白は無く、
  必要ならsymlinkで避ける）。`PYTHONUNBUFFERED=1`で標準出力・標準エラーのログの行の順序を保つ。
  weeklyは`collect --source claude`（当月）、monthlyは`--month <前月>`（前月はzshの算術で求め、
  macOS固有の`date -v`は使わない）。どちらもそのあと`--check-login`。取得か`--check-login`の
  終了コードが0でなければmacOSの通知（`osascript`の`display notification`）を出し、全体の終了コードを
  1にする（出力の文字列は解釈しない。`--check-login`の1は期限間近・無効・不明・時間切れ・設定未完了・
  使用中を含み、通知の文はログを見るよう促すだけにする）。ログはlaunchdが`~/.seat-analyzer/logs/`へ
  追記する。`SEAT_ANALYZER_INPUT_DIR`があれば`--input-dir`に渡す（一時ディレクトリへ配置して試す）
- 既定の時刻は週次が日曜2:00、月次が毎月2日2:00（ローカル時刻）。どのタイムゾーンでも、月次の
  2日2:00はUTCで当月の1日か2日に入るので前月を取れる。週次が1日か2日に当たる日曜は、タイム
  ゾーンによってはツールの時期の判定（§14.1・Step 55）が「まだ」と表示して取得せずに終わる
  （UTC+14では2日2:00がUTCの1日12:00、UTC-12では2日14:00）。雛形は日付の規則を持たない
- plistは`caffeinate -is`（実行中の再スリープを止める。`-s`はAC電源のとき有効）→`lockf -k`（週次と
  月次が同じ時刻に重なっても順番に実行する。既定でロックが外れるまで待つ）→`/bin/zsh -f`の順に包む
- 起床の予約（`pmset`）は持たない。無人で予約するにはsudoersの変更が要るため。スリープするMacの
  扱いはdocs/setup.mdで案内する（電源接続時にスリープしない設定にするか、`sudo pmset repeat
  wakeorpoweron`を1回手で登録する。`pmset repeat`は1件しか持てない）。あわせてlaunchdの前提も
  書く: LaunchAgentsはGUIにログインした状態でだけ動く（電源投入後にログイン画面で止まっていれば
  動かない）、スリープ中に過ぎた予定は起床後に1回だけまとめて実行される、対象月は実行した時点の
  日付で決まる（長く止まっていた分の遡及はしない。予定した月が過ぎていれば手動で置く）
- 雛形に個人のパス・ラベル・組織名を含めない。ラベルは`seat-analyzer.collect-weekly`・
  `seat-analyzer.collect-monthly`
- テスト: plistの雛形を`plistlib`で読めること・必要なキーとプレースホルダだけを含むことを全OSで見る。
  `zsh`がある環境では`zsh -n`で構文を見る。Windows以外でzshがあれば偽の`seat-analyzer`をPATHに
  置いて、weekly / monthlyの呼び出し列と終了コード（取得か`--check-login`の失敗で1）を見る。
  偽のコマンドは`SEAT_ANALYZER_CMD`の単語分割（複数語）も確かめる。macOSでは偽の`launchctl`と
  一時的なHOMEで、`install.zsh`の配置・置換・再実行・`--uninstall`を見る

## 15. GitHub collection

### 15.1 Config

```yaml
organizations:
  example:
    github_org: example-org
```

GitHub分析は組織ごとのopt-inとする。`organizations.<組織名>.github_org`を設定した組織だけが
対象で、収集・doctor検査・指標・出力のすべてが対象組織に限られる。未設定の組織はGitHub関連の
処理と警告の一切から除外される（対応表が無いこと自体を問題として報告しない）。

このゲートを持つのは消費側（doctor・指標）とし、対応表のloaderはゲートを持たない。loaderは
純粋な読み取りに徹し、「GitHub分析をするかどうか」の判断を呼び出し側へ残す。

### 15.2 認証

- `gh auth status`をread-onlyで確認
- private repository可視性をsmoke test
- SSO不足・scope不足はData Doctor issue
- tokenをPythonへ取り出さない

### 15.3 自動発見

- Organization内の全参照可能repository
- Archive除外
- Fork除外
- Template除外
- 手動allowlist不要
- optional denylistだけ許容

### 15.4 PR取得

- Organization単位
- merge date range単位
- 初回90日
- 以後は直近月を再取得してupsert
- 件数が多い場合は期間を週単位へ分割
- pagination
- 直列処理
- rate limit時は停止・再開

### 15.5 指標

```text
merged_pr_count:
  merge月に帰属

lead_time_hours:
  mergedAt - createdAt

summary:
  median
  P75
  P90
```

Draft期間は初期実装では含める。

対象者（母集団）は、その月にこの組織でシートを持つ人とする（2026-10-11 追加）。分析が採用した
メンバー一覧で`standard`か`premium`と確認できた人で、複数workspaceの組織は分析したworkspaceの
どれかで持てば対象になる。含めないのは`unassigned`、`unknown`（spendにいてmembersにいない人と、
シート種別を読めない人）、メンバー一覧に居ない人、未開始で分析から飛ばしたworkspaceだけで持つ人。
月内の在籍期間は復元しない（採用したメンバー一覧の時点で決まる）。

- 人ごとの行: 対象者のうち対応表にloginを持つ人（PR 0件の人も行を持つ）
- 組織全体の件数とlead time: 対象者のPRだけ（人ごとの行の合計）。対象者のloginに当たらない
  作成者（対応表に無い人と、対応表にあっても対象者でない人）と削除済みアカウントのPRは、
  件数の列にだけ残す
- 対象者が0人のときに対応表全体へ戻す代替は持たない
- 別のOrganizationでのPRは集計しない（そこで開発する人は対応表に`なし`と書く）

doctorの対応表の検査も同じ対象者だけを見る（入れ子の組織は開始済みworkspaceのメンバー一覧の
和集合）。

### 15.6 GitHubは参考情報

- V2 seat actionの必須条件にしない
- GitHub dataがなくても分析を成功させる
- 因果関係を断定しない
- 個人ランキングを作らない

### 15.7 コミット単位の帰属（行数）

PR の件数と lead time は PR の作成者に帰属させたまま（§15.5）、行数はコミットの作者に帰属
させる（Step 58〜59）。PR の作成者と中のコミットの作者は別の人であることがあり（引き継ぎ・
共同作業）、PR の additions / deletions を作成者に足すと別の人の成果が混ざる。

- 取得: 検索の GraphQL に`commits`を足し、PR ごとに oid・作者の login とメール・additions・
  deletions・committedDate を取る（§7.6）。1 回で取り切れない PR（総数が 1 回の上限を超える）は
  PR ごとに続きを辿る。コミットの親の数（マージコミットの判定）と、PR の mergeCommit の oid も
  取る。GitHub は PR のコミットを250件までしか返さない。コミットのメッセージ・ファイル一覧は
  問い合わせそのものに書かない。GitHub の GraphQL は約10秒で打ち切られるため、通常は検索の
  PR を10件・ネストしたコミットを50件にする。一時的な失敗（HTTP 502・503・504、応答待ちの
  打ち切り、本文が JSON でない200、時間切れを示す GraphQL のエラー）では、同じ位置から
  5件×20件、1件×10件と小さくして読み直す。PR 単位の続きは100・50・20件と縮める。
  最大3回・待ちは2秒と5秒で、成功した次のページは通常の大きさへ戻す。利用上限は時間切れより
  優先して止める。上限はページ数でなく受信件数の累計で見て、検索は1000件、コミットは250件を
  超えたら窓全体を未完了にする。次があるのに0件、または cursor が無いページでも止める
- 作者の解決: login が GitHub ID の対応表に当たればその人。login が無い（GitHub アカウントに
  結び付かない）コミットは、作者のメールアドレスを対象者のメールアドレス（members）と小文字で
  突き合わせる。どちらにも当たらなければ`unresolved`として件数だけ残す
- 上限つき合計: 1 コミットの追加行が`github.large_commit_lines`（既定 5,000）を超える
  コミットは「大きな変更」として合計から外し、件数を別に数える（lock ファイル・生成物・
  外部コードの取り込みが行数の合計を決めてしまうため）。ファイル単位の除外はしない（PR ごとの
  追加の問い合わせが要り、「files を保存しない」を変えることになる）
- 母集団は §15.5 と同じ（その月にシートを持つ人）。Bot・draft の PR のコミットは数えない
- github-summary に列を足す（`commit_count`・`lines_added`・`lines_removed`・
  `lines_added_capped`・`large_commits`。組織全体行に`unresolved_author_commits`）。既存の列は
  変えない
- コミットを持たないキャッシュ（schema 1）では新しい列を空欄にして、`collect`の再実行を案内する
  （`GH_COMMITS_MISSING`）

## 16. CLI

### 16.1 既存

```sh
uv run seat-analyzer analyze --month YYYY-MM
uv run seat-analyzer analyze --preview --days N
uv run seat-analyzer init-org <org>
```

### 16.2 段階追加

```sh
uv run seat-analyzer doctor --org <org> --month YYYY-MM --format text|json

uv run seat-analyzer analyze \
  --org <org> \
  --month YYYY-MM \
  --decision-version v1|v2

uv run seat-analyzer review \
  --org <org> \
  --month YYYY-MM

uv run seat-analyzer collect \
  --org <org> \
  --source admin

uv run seat-analyzer collect \
  --source claude \
  [--month YYYY-MM] \
  [--org <org> ...] \
  [--profile <name>] \
  [--dry-run] \
  [--keep-browser] \
  [--timeout <分>]

uv run seat-analyzer collect \
  --source claude \
  --import <run_id>

uv run seat-analyzer collect \
  --source claude \
  --setup|--finish-setup|--login|--list-orgs <profile>

uv run seat-analyzer collect \
  --source claude \
  --check-login [<profile>] \
  [--timeout <分>] [--keep-browser]

uv run seat-analyzer collect \
  --org <org> \
  --source github \
  --month YYYY-MM

uv run seat-analyzer collect \
  --org <org> \
  --source otel \
  --month YYYY-MM \
  [--dry-run]
```

`--source github`は、問い合わせを始める窓ごとに対象期間と今回の何番目か・窓の総数を1行で表示する。

`--source claude`の`--month`は当月か前月で、省略時は当月（§14.1）。取得できる時期（UTCの今日が
ローカルの月に追いついているか。§14.1・Step 55）を満たさなければChromeを起動せずに理由を表示し、
`--month`の省略時は終了コード0、明示時は1。`--org`は複数指定でき、
省略時は`claude_export`を設定した全組織。`--dry-run`は取得の計画（プロファイルごとのモードと、
対象・UUID・種別・配置先）を表示して終了する。`--keep-browser`は取得の後もChromeを終了させず、
`--timeout`は待機の上限（分。省略時は`claude_export.timeout_minutes`）を決める。`--import`は
stagingに残った実行の検証・配置だけをやり直す（§14.3）。`--setup`はプロファイルを作って
Chromeをログイン画面で起動し、人が行う手順（ログイン・拡張機能の読み込み）を表示して待たずに
終わる。人の操作が終わったら`--finish-setup`がChromeを終了させ、拡張機能が同梱の場所から
読み込まれていることを確かめてからPreferencesを書く（ウィンドウを閉じてもChrome本体が残る
環境があり、人による終了を待つと完了を判定できないため、締めの操作をコマンドに分ける）。
`--login`はセッションのCookieを消してからログイン画面を開き、`--check-login`はログイン
セッションの状態をプロファイルごとに表示する（§18 Step 52）。
`--setup`・`--finish-setup`・`--login`・`--list-orgs`・`--check-login`・`--import`は単独で使う
（`--list-orgs`と`--check-login`だけは`--timeout`・`--keep-browser`を併用できる）。claude専用の
オプションを`--source github`に付けると終了コード2。

`--source otel`は`organizations.<組織名>.otel`を設定した組織だけで、BigQuery を`bq` CLI で読んで
月の集計を`otel-cache/`へ保存する（§27.3）。`--dry-run`は読み取り量の見積もりだけを表示して
終わる。見積もりが`otel.maximum_bytes_billed`を超えるときは実行しない。定期実行（§14.5）には
含めない（サブスクリプションの外の課金が生じるため、人が都度実行する）。

判定の版は「`--decision-version`の明示指定 > `config.yaml`の`decision.version` > `v2`」で決まる
（Track 11で既定を`v2`にした。それまでは`decision_v2.enabled`による併記のopt-inで、既定は`v1`）。
`v2`は主判定がV2で、report・details・dashboard・recommendationsの判定がV2の結論になり、
decision-evidenceを常に併記する。`v1`は従来の判定（allowanceモデル）で、decision-evidenceを
書かない。`v1`はV2の既定化から最低3か月は選べる（§22.1）。速報モード（`--preview`）は
どちらの版でもV1の一次判断のままで、このオプションと併用できない。

## 17. 構造化issue

最低限のcode:

### 入力

- `MISSING_SPEND`
- `MISSING_MEMBERS`
- `PARTIAL_MONTH`
- `MISSING_HISTORY_MONTH`
- `UNKNOWN_MODEL`
- `NUMERIC_PARSE_FAILED`
- `MEMBER_ROW_MISSING`
- `MEMBERS_INFO_UNREGISTERED`
- `MEMBERS_INFO_UNREADABLE`

### Workspace

- `WORKSPACE_LAYOUT_MIXED`
- `WORKSPACE_CONFIG_MISMATCH`
- `WORKSPACE_PRIMARY_INVALID`
- `SECONDARY_ONLY_ACCOUNT`

複数のTeamスペースを運用する組織（§26）の構造と人の検査。`MEMBERS_INFO_UNREGISTERED`と
`MEMBERS_INFO_UNREADABLE`はレイアウトによらず持つ検査なので入力に置く（§26.8）。

### Identity

- `IDENTITY_EMAIL_FALLBACK`
- `IDENTITY_CONFLICT`
- `GITHUB_MAPPING_MISSING`
- `GITHUB_MAPPING_DUPLICATE`

### Seat/credit

- `SEAT_TYPE_UNKNOWN`
- `UNASSIGNED_WITH_USAGE`
- `SEAT_CHANGE_DETECTED`
- `RECENT_SEAT_CHANGE`
- `CREDIT_SETTING_UNKNOWN`
- `ADMIN_SNAPSHOT_STALE`

### Browser issue

- `BROWSER_LOGIN_REQUIRED`
- `ADMIN_PAGE_CHANGED`
- `DOWNLOAD_FAILED`
- `DUPLICATE_DOWNLOAD`

語彙は`domain.IssueCode`に定義済みで、まだどこにも結線していない。`collect --source claude`
（§14）は取得の失敗を、拡張機能と配置前の検証が返す理由文のまま表示する。
`DUPLICATE_DOWNLOAD`は旧方式（download watcher）で定めた語彙。

### GitHub issue

- `GH_NOT_AUTHENTICATED`
- `GH_ORG_NOT_ACCESSIBLE`
- `GH_PERMISSION_INCOMPLETE`
- `GH_RATE_LIMITED`
- `GH_PARTIAL_RESULT`
- `GH_COMMITS_MISSING`
- `GITHUB_CONFIG_UNMATCHED`

`GITHUB_CONFIG_UNMATCHED`は、`organizations`のキーが入力の組織ディレクトリのどれとも
一致しない場合のwarning。綴り違いはGitHubの検査がまるごと飛ぶ形で表に出ないため、
有効にしたつもりの組織が黙って対象外になる。組織に属さないissueなのでscopeはorgではなく
`config_org`を持つ。

### OpenTelemetry issue

- `OTEL_BQ_NOT_AUTHENTICATED`
- `OTEL_BQ_NOT_ACCESSIBLE`
- `OTEL_QUERY_TOO_LARGE`
- `OTEL_PARTIAL_RESULT`
- `OTEL_ATTRIBUTE_MISSING`
- `OTEL_COVERAGE_LOW`
- `OTEL_USER_MISSING`

`OTEL_COVERAGE_LOW`と`OTEL_USER_MISSING`は §27.4 の網羅率の検査。テレメトリが届いていない人を
「使っていない人」と読ませないための issue で、パネルではその人の OTel 由来の列を空欄にする。
`otel`を設定していない組織では OTel の issue は一切出ない。

### Policy

- `PROHIBITED_PRODUCT_OBSERVED`
- `CAPACITY_SIGNAL_UNAVAILABLE`

## 18. マイクロステップ実装順序

以下は原則として1 Step = 1 PRとする。Stepの途中で別Stepの実装を混ぜない。

### Track 0: 実機Feasibility

#### Step 0A: GitHub認証の手動smoke test

目的:

- 実装前に`gh`で対象Organizationを読めるか確認する

変更:

- コード変更なし
- 検証結果だけをローカルメモへ記録

確認:

- `gh auth status`
- Organization参照
- repository件数
- 直近1か月のmerged PR件数

受け入れ条件:

- private repositoryを少なくとも1件参照できる
- PR createdAt/mergedAtを取得できる
- scope不足なら不足内容が分かる

今回は行わない:

- collector実装
- cache保存

#### Step 0B: Playwright管理画面smoke test

目的:

- 公式CSVを1種類ダウンロードできるか確認する

対象:

- `scripts/spike_admin_download.py`

受け入れ条件:

- headed browserが開く
- 手動ログインできる
- 1組織のSpend CSVを一時ディレクトリへ取得できる
- シート・creditを変更しない

今回は行わない:

- production CLI
- Members/Code Analytics
- 自動ログイン

不採用時:

- scriptをproductionへ残さず、手動取得を継続する

実機検証結果:

- headed browserは起動できた
- 一時browser profileでは外部セキュリティ検証が反復した
- セキュリティ検証を回避する変更は行わない
- Playwright方式は不採用とし、検証用scriptは残さない

#### Step 0C: 通常ブラウザ＋download watcher smoke test

目的:

- 普段のブラウザセッションを使い、外部セキュリティ検証を回避せずに取得作業を
  補助できるか確認する

対象:

- `scripts/spike_download_watcher.py`

方式:

- OSの通常ブラウザを開く
- ログイン、Organization選択、Spend Exportは利用者が手動で行う
- scriptはdownload directoryを監視する
- 新規CSVのheaderだけを読み、Spend reportであることを確認する
- 検出したCSVを一時ディレクトリへコピーする

受け入れ条件:

- 既存のログイン済みブラウザを利用できる
- 1組織のSpend CSVを5分以内に検出できる
- 認証情報、Cookie、CSV本文をログ出力しない
- シート・creditを変更しない

今回は行わない:

- CSVの`input/`への自動配置
- Members/Code Analytics
- 画面操作の自動化
- PlaywrightやChrome remote debuggingの利用

### Track 1: 入力と品質

#### Step 1: Spend任意カラム

依存:

- なし

対象:

- `config.yaml`
- `src/seat_analyzer/ingest.py`
- `tests/test_ingest.py`

実装:

- account UUID
- user ID
- gross spend
- Web Search

受け入れ条件:

- 新旧CSVを読める
- 任意列なしは`NA`
- V1結果不変

今回は行わない:

- subject_id
- report追加

#### Step 2: Members任意カラム

依存:

- Step 1なしでも実施可能

対象:

- `config.yaml`
- `src/seat_analyzer/ingest.py`
- `tests/test_ingest.py`

実装:

- account UUID
- user ID
- member status

受け入れ条件:

- 既存Members CSV互換
- 未知statusを保持
- V1 seat判定不変

今回は行わない:

- ID join変更

#### Step 3: subject_id

依存:

- Step 1
- Step 2

対象:

- `src/seat_analyzer/identity.py`
- `tests/test_identity.py`

実装:

- subject_id
- quality
- conflict

受け入れ条件:

- email変更をstable IDで結合
- conflict検出
- V1はemail joinのまま

今回は行わない:

- V1 join置換

#### Step 4: QualityIssue

依存:

- なし

対象:

- `src/seat_analyzer/domain.py`
- `src/seat_analyzer/data_quality.py`
- `tests/test_data_quality.py`

実装:

- severity
- code
- scope
- JSON serializer

受け入れ条件:

- 決定的なcode
- JSON化
- 既存warning不変

今回は行わない:

- CLI

#### Step 5: doctorの既存入力検査

依存:

- Step 4

対象:

- `src/seat_analyzer/cli.py`
- `src/seat_analyzer/data_quality.py`
- `tests/test_cli.py`

実装:

- Spend/Membersの欠損・部分月・不整合
- text/json

受け入れ条件:

- errorでexit 1
- warningだけならexit 0
- `analyze`不変

今回は行わない:

- GitHub
- browser
- admin settings

### Track 2: Code中心の利用可視化

#### Step 6: product policy config

依存:

- なし

対象:

- `src/seat_analyzer/default-config.yaml`
- `src/seat_analyzer/config.py`
- `tests/test_hardening.py`

実装:

- primary
- supplementary
- prohibited
- `supplementary_high`の閾値

受け入れ条件:

- 省略時default（既定は`default-config.yaml`が持ち、ワークスペースの`config.yaml`は差分だけで上書きできる）
- 空primaryはerror
- 閾値の型・範囲を検証する
- 現行分析不変

今回は行わない:

- 集計

#### Step 7: Code/全product特徴量

依存:

- Step 6

対象:

- `src/seat_analyzer/product_usage.py`
- `src/seat_analyzer/analyze/__init__.py`
- `tests/test_product_usage.py`
- `tests/test_module_deps.py`

実装:

- §9.2の特徴量を過不足なく実装する（`total_demand_usd`・`code_demand_usd`・
  `code_demand_share`・`total_requests`・`code_requests`・`product_breadth`・
  `supplementary_high`・`prohibited_observed`）

受け入れ条件:

- Code alias対応
- 分母0は`NA`
- 全product費用とCode活用を分離
- `LAYERS`へ層15を割り当てる。`analyze`(20)と`report`(30)の双方から呼べる必要があり、
  同層importが禁止されているため20は使えない
- policyは引数で受け取り、`config`をimportしない。特徴量の計算を「DataFrameとpolicyを
  受けて返す純粋関数」に保つ
- 価格適用済みの明細から一度だけ呼び、結果を`AnalysisResult`へ保持する。Step 8以降は
  保持済みの値を出力するだけとし、Spendの再読込・再価格計算をしない（cost basisと
  採用snapshotが分析本体とCSVで食い違うのを防ぐ）
- 観測できない値を作らない。`product`名が空の行・`requests`が欠けた行・列そのものが無い
  入力は、どれも「その行の値が分からない」として同じ規則で扱う。分からない行の寄与を
  最小・最大に見積もった範囲を出し、結論が動かないときだけ値を確定させ、それ以外は`NA`に
  する。明細行数での代替はしない
  - 当初は「`product`列が無ければCode系の特徴量は`NA`」のように列の有無で書いていたが、
    それだと証明できる値まで`NA`になる（分からない行の正の寄与をすべて含めた上限が
    閾値未満なら`supplementary_high`は偽が確定する、primary行が1つも無ければCode回数は
    0が確定する、など。負の`cost_usd`がありうるため総需要では判定できず、上限で見る）。
    守りたいのは「証明できない値を出さない」ことなので、条件をそちらへ書き換えた
- `CAPACITY_SIGNAL_UNAVAILABLE`は`product`の列欠落またはセル欠損（product名が空の行）の
  ときに出す。`requests`の欠落にはissueも警告も出ない（列欠落はingestが警告せず、
  セルの空欄・数値変換失敗も黙って欠損になる）。回数由来の特徴量が`NA`になることで
  出力側から見える。
  `product`は`REQUIRED_COLUMNS["spend"]`に含まれておらず、旧CSVは有効な入力として
  受け付けたままにする
- prohibitedに一致するproductの行があれば`PROHIBITED_PRODUCT_OBSERVED`を出す。
  seat判定へは影響させない

今回は行わない:

- seat判定
- report

#### Step 8: usage-summary.csv

依存:

- Step 7

対象:

- `src/seat_analyzer/report/usage_csv.py`
- `src/seat_analyzer/report/__init__.py`
- `src/seat_analyzer/cli.py`
- `tests/test_cli.py`
- `tests/golden/`

実装:

- 通常の`analyze`の成果物として`usage-summary.csv`を生成する

受け入れ条件:

- 常に生成する。opt-inのフラグは設けず、goldenのファイル集合へ追加する
- 既存出力不変（`report.md`・`recommendations.csv`・`dashboard.html`・`preview.md`・
  `preview-dashboard.html`・`summary/<month>.md`がバイト一致で変わらない）
- Code/totalを表示
- prohibited product warning

今回は行わない:

- dashboard

#### Step 8F: 統計参考値の掲載

v1.1.0で追加したStep。原設計には無い。個々のユーザの数値が組織の中でどの位置にあるかを
読み手が判断できるようにする。

Step 8D・8E・8Gとの実行順は`8F → 8G → 8E → 8D`とする。番号はPRの記録と対応しているため
振り直さず、順序だけをここで定める。デザイン（8D）は載せる中身が確定してから当てる。
8E・8Gを8Dの後に回すと、後から入ったsectionだけがデザインから浮くため。

依存:

- Step 8

対象:

- `src/seat_analyzer/report/stats.py`（新設）
- `src/seat_analyzer/report/format.py`
- `src/seat_analyzer/report/text.py`
- `src/seat_analyzer/report/markdown.py`
- `src/seat_analyzer/report/html.py`
- `src/seat_analyzer/templates/dashboard.html.j2`
- `src/seat_analyzer/templates/dashboard.css`
- `src/seat_analyzer/templates/partials/stats.html.j2`（新設）
- `src/seat_analyzer/prompts/aspects-full.md`
- `examples/generate_sample_data.py`
- `tests/golden/`

実装:

分布の計算（`report/stats.py`）

- 母集団は`current_seat == "unassigned"`を除いた分析対象ユーザ。利用ゼロのユーザは含める
  （除くと中央値が上振れし、遊休の存在が統計から消える）。`unknown`は含める（membersの
  更新漏れ疑いであって、シートが割り当てられていないとは限らない）。組織サービス行は
  analyzeの段階で分離済みのため元から対象外
- 指標ごとに欠損の扱いが違うため`n`が揃わない。`n`は指標ごとに必ず表示する

  | 指標 | 取得元 | 欠損の扱い |
  |---|---|---|
  | API換算需要 | `api_cost_usd` | `NaN`は0 |
  | input | `prompt_tokens` | `NaN`は0 |
  | output | `completion_tokens` | `NaN`は0 |
  | LoC | `loc_with_cc` | 列が無ければ行ごと省略。値が0のユーザは母集団から除く |
  | 実課金 | `billed_extra_usd` | `NaN`は0 |
  | リクエスト数 | `product_usage.features.total_requests` | 下記 |

- 統計量は`n`・平均・中央値・標準偏差・p25・p75・p90・最大。標準偏差は母標準偏差
  （`ddof=0`）とする。全数調査であり、`n=1`で未定義にならない
- 分位点はpandasの`Series.quantile`既定（線形補間）に固定し、docstringへ定義を書く。
  標準ライブラリの`statistics.quantiles`とは値が異なるため、どちらを使うかを決めておく
- 判定・推奨には一切使わない。表示専用の値であり、`analyze`には手を入れない

LoCの母集団

- `analyze`はcode-analyticsに行が無いユーザを`fillna(0)`で0にするため、`users`の段階では
  「行が無い」と「0行」を区別できない。LoCの欠落は「コードを書いていない」を意味しない
  ので、0を母集団から除いて`n`を併記する

リクエスト数

- `users`には無い。Step 8の`product_usage.features`（index=email）を左結合して得る
- `users`へ列を足さない。`recommendations.csv`は`result.users`の全列をそのまま書き出す
  ため、列を足すとこのCSVの列構成が変わる
- spendに行が無いユーザは0（回数ゼロが確定する）
- `total_requests`が`NA`のユーザは母集団から除く（`requests`列が無い・値が欠けている＝
  回数が分からない。0と区別する）
- spendに現れたユーザのうち確定値を持つ人が1人もいなければ、リクエスト数の行ごと省略する
  （残るのは利用ゼロのメンバーだけになり、全員0の退化した行になるため）

report.mdへの掲載

- 「## 組織内の分布（参考値）」を「詳細利用状況」の直後・「感度分析」の前に置く
- `discuss`へは追加の配線なしで渡る。`collect_materials`の資料1が`report.md`本体を
  そのまま渡すため
- `prompts/aspects-full.md`へ観点を1つ足す。個人の数値を分布上の位置として解釈すること、
  および歪みと打ち切りの注意を踏まえること

dashboard.htmlへの掲載

- 分布表を「詳細利用状況」の直後に置く（`partials/stats.html.j2`）
- 「ユーザ別 API 換算コスト」の棒に中央値・平均のガイド線を引く（`.track`内に絶対配置し、
  位置は`値 / max_cost`）
- 各棒の金額の右に順位を小さく添える（`api_cost_usd`の降順順位）。棒の並びは判定ステータス
  順（`_sorted_by_status`）のままなので、行番号ではなく値から計算した順位を出す
- 列を増やさない。推奨一覧・詳細利用状況テーブルの列構成は変えない

書式

- 金額はreport.mdが`_fmt_usd`、dashboardが`_fmt_compact`（既存の使い分けに従う）
- トークン・LoC・回数は統計表用の短縮表記を新設し、`format.py`へ置いて両形式で共有する
  （単位の刻みは詳細利用状況の`_fmt_tokens`と揃えて1e9以上はB・1e6以上はM・小数2桁、
  1e4以上はK・小数1桁、それ未満は桁区切り整数）。詳細利用状況の桁区切り整数はそのまま

注記（表の直下・固定文言は`text.py`で両形式が共有する）

- 平均は少数の大口利用に引かれやすいため「平均以下＝低活用」ではない（歪みの程度はデータ
  次第で、上限到達者が多い組織では平均と中央値が近いこともある。断定は書かない）
- 追加クレジット上限に到達したユーザは需要そのものが上限で止まっており、分布の右裾は
  実態より低い（既存の注意事項のセンサリングと同じ話を、分布の読み方として書く）
- LoCとspendは網羅範囲が一致せず、LoCの行が無いことは「書いていない」を意味しない
- 比較の母集団は当該組織内に閉じる

examplesの全部入りサンプル

- 条件つきsectionがすべて出る合成データを用意する。Step 8Dのデザイン作業を実データ抜きで
  行えるようにし、かつ欠けたsectionのまま作られたデザインが他組織で崩れるのを防ぐ
- 出る状態にする対象: 追加クレジット構成・前月からの変化・月中の利用推移・月中のメンバー
  変動・月中のClaude Code活動・込み枠の実測・追加クレジット付与候補・部署別/チーム別
  サマリ・LoC列・分布（リクエスト数の行を含む）
- 出力先は`examples/reports`（`reports/`へ出すと`org-a`・`org-b`が禁止語収集の対象になる）

受け入れ条件:

- `recommendations.csv`・`usage-summary.csv`・`preview.md`・`summary/<month>.md`が
  バイト一致で不変。数値が変わればこれらのどれかに必ず現れるので、これをもって
  「統計の追加がデータを壊していない」ことの検査とする
- `preview-dashboard.html`の差分は共有CSSの追加だけに限る。CSSは正式・速報で共有して
  いるため（Step 8Dでも共有を維持すると決めている）バイト一致にはできない。追加した
  セレクタに一致する要素が速報側に無いことをもって、表示が変わらないことを担保する
- 速報は対象外とする（`PreviewResult`は`product_usage`を持たない）
- 判定・推奨・警告の内容が変わらない
- 母集団の需要合計が、サマリの「全体のAPI換算需要」から未割当ユーザ分を引いた値と一致する
  ことをテストで突合する（統計が別経路の再計算になっていないことの検査）
- 既定の列構成で横スクロールが出ない
- 統計量の単体テスト（既知の小配列で中央値・母標準偏差・分位点を検証する）
- 境界: `n=0`・`n=1`、全員ゼロ、LoC列なし、`requests`列なし、実課金が全員ゼロ、
  未割当のみの組織

今回は行わない:

- dashboardの再設計（Step 8D）
- product軸の掲載（Step 8E）
- 個人の位置をテーブルの列として出すこと。詳細利用状況はトークン降順に並んでいるので順位列
  は行番号と同じになり、推奨一覧は8列の上限にある。棒グラフのガイド線と順位で代える

#### Step 8G: report.mdの再構成（考察メイン化とdetails.mdの分離）

v1.1.0で追加したStep。原設計には無い。dashboardが数値を担い、report.mdはアクションと
考察を中心にした短い文書にする。
ユーザ単位の数値表はdashboardと重複しており、report.mdの読者には過剰なため。

report.mdには読者向けでない役割がもう1つある。`discussion.collect_materials()`が
report.md本体を資料1としてモデルへ渡しており、表を削るとそのぶん考察の材料が消える。
このStepの中心は「読み手向けの文書」と「モデルへ渡す資料」の分離で、削った表の受け皿として
機械生成の`details.md`を新設する。

依存:

- Step 8F

対象:

- `src/seat_analyzer/report/markdown.py`
- `src/seat_analyzer/report/details.py`（新設）
- `src/seat_analyzer/report/__init__.py`
- `src/seat_analyzer/report/html.py`
- `src/seat_analyzer/templates/dashboard.html.j2`
- `src/seat_analyzer/templates/partials/e-dist.html.j2`（削除）
- `src/seat_analyzer/discussion.py`
- `src/seat_analyzer/cli.py`
- `tests/golden/`

実装:

report.mdの再構成

- 残すsection（この順）: サマリ / 前月からの変化 / 追加クレジット付与候補 /
  シート変更推奨 / 注意事項 / データ検証・警告 / 考察
- 「前月からの変化」は、月次の増減をレポート本文でも示すために残す
- シート変更推奨の表の凡例（列の読み方）は、表が空でない場合のみ表の直下に残す
- `details.md`へ移すsection: 全ユーザ / 凡例と備考 / 部署別サマリ / チーム別サマリ /
  詳細利用状況 / 組織内の分布（参考値） / 月中の利用推移 / 月中のClaude Code活動 /
  月中のメンバー変動 / 込み枠の実測 / 感度分析
- `_preserve_discussion()`の対象はreport.mdのまま（details.mdに考察sectionは無い）

details.md（新設・正式分析で常に生成）

- 表題は「分析詳細資料 — <組織> — <月>」。冒頭に「機械生成の詳細資料。dashboardと同じ
  数値のMarkdown版で、考察執筆（discuss）の資料を兼ねる」旨の1行を置く
- 中身は移したsectionをそのまま出す（数値・表の形式は変えない。移動のみ）。データが無い
  sectionは従来どおり省略する
- 対象組織のデータのみを含む（レポート成果物の組織分離ルールをそのまま適用）
- 実装は`report/details.py`。sectionの組み立て関数は`markdown.py`の既存関数を再利用する
  （同一パッケージ内のためモジュール層の変更なし）

discussの資料構成

- `collect_materials()`: 資料1 = report.md本体（slim） / 資料2 = details.md /
  資料3 = recommendations.csv。混入チェックの照合元（source_text）にdetails.md本文を
  加える（機械生成された当月の資料のみ、という前提は保たれる）
- details.mdが無い月（このStep以前に生成した旧レポート）は資料2を省略して従来どおり
  動く（後方互換。エラーにしない）
- 速報（preview）の資料構成は変えない
- `prompts/aspects-*.md`は変えない（「資料に〜があれば」の書き方なので、渡り先が
  details.mdに変わっても観点はそのまま機能する）

dashboardの変更（このStepで唯一のHTML変更）

- 「込み枠の実測（E = API換算需要 − 実課金）」sectionをdashboardから削除する。
  E行はユーザ単位では推奨一覧の2列の引き算にすぎず、集計値は運用者向けの
  キャリブレーション材料（allowance推定の検証）であって、dashboardの読者は
  行動につなげられないため。実測の記録はdetails.mdが引き継ぐ
- `_DASHBOARD_SECTIONS`から`_E_DIST_HTML`を外し、partial `e-dist.html.j2`と
  `_e_distribution_view()`を削除する。`_compute_e_distribution()`（analyze側）は
  details.mdが使うため残す

CLI・ドキュメント

- `cli.py`の出力一覧にdetails.mdのパスを足す
- `docs/usage.md`の成果物リストとsection説明を更新する。`README.md`・
  `.claude/commands/seat-analysis.md`にreport.mdのsection構成へ言及している箇所が
  あれば追従させる

受け入れ条件:

- 変わる出力は`report.md`と`dashboard.html`、新規の`details.md`だけ。
  `recommendations.csv`・`usage-summary.csv`・`preview.md`・`preview-dashboard.html`・
  `summary/<month>.md`はバイト一致で不変
- dashboard.htmlの差分は「込み枠の実測sectionの削除」のみ
- report.mdとdetails.mdを合わせると、移動対象sectionの内容が過不足なく存在する
  （数値の変更・欠落が無い。移動のみであることの検査）
- 判定・推奨・警告が変わらない
- `discuss --dry-run`のプロンプトにdetails.mdの内容が資料として入る。details.mdが
  無い月ディレクトリでも資料2を省略して動く
- 考察の保全（`_preserve_discussion`）が引き続き機能する
- golden（full系ケース）にdetails.mdを追加する

今回は行わない:

- preview.md / preview-dashboard.htmlの再構成（速報は現状のまま）
- dashboardの再設計（Step 8D）・product軸（Step 8E）
- 考察プロンプト（aspects）の観点変更

#### Step 8D: dashboardの再設計

v1.1.0で追加したStep。原設計には無い。§2.2は「既存のHTML全体を書き換えない」としているが、
これは機能追加のついでに書き換えることを禁じる規則であり、再設計そのものを独立したStepに
する限りは趣旨に反しない。product軸の掲載はStep 8Eへ分ける。

デザイン仕様をtemplatesへ書き起こし、全組織・全データ状況で成立させる。

デザイン成果物は**そのまま流用できない**。Claude Designのランタイム前提の形式で、
描画ロジックはコンポーネントとして書かれ、スタイルの大半はインライン指定である。
成果物のREADME（配色・余白・タイポグラフィ・挙動を数値で確定させたもの）を仕様として、
Jinjaテンプレート + CSS + 素のJSへ書き起こす。

**作業量が大きいため2つのPRに分ける**（8D-1 = 見た目、8D-2 = 対話機能）。1つに畳むと
レビューが機能しない規模になる。

依存:

- Step 8E

##### 決定事項

- **フォントはシステムフォントのみ**（追加0KB）。デザインはIBM Plex Sans JP / IBM Plex Mono
  を指定しているが、`dashboard.html`は共有される自己完結HTMLなので外部参照を持てず、
  埋め込みは日本語フォントだけで5.4MBに達する。3案（全埋め込み+5.5MB / システムのみ+0KB /
  Mono欧文のみ+147KB）を実データで描画比較した結果、日本語の見た目はシステムフォントと
  ほとんど差が無く、差が出るのは等幅部分だけだったため、システムフォントで実装する。
  **字送り・ウェイト・サイズスケールはデザイン仕様どおりに再現し、書体だけを差し替える**
- **速報ダッシュボードも同じテイストで作る**。トークン・カード・表・バーの体裁を共有する。
  速報はsectionが少ないためタブ分割はせず、単一ページに積む
- タブ・テーマ切替のためにJSを導入する（現行はJSゼロ）。ただし**JSが無効でも内容が
  読めること**を条件にする（下記）

##### Step 8D-1: 見た目

対象:

- `src/seat_analyzer/templates/dashboard.css`
- `src/seat_analyzer/templates/dashboard.html.j2`
- `src/seat_analyzer/templates/preview-dashboard.html.j2`
- `src/seat_analyzer/templates/partials/*.html.j2`
- `src/seat_analyzer/templates/dashboard.js`（新規・タブとテーマのみ）
- `src/seat_analyzer/report/html.py`
- `tests/golden/`

実装:

- デザイントークン（`--bg` / `--surface` / `--ink` / `--accent` / `--std` / `--prem` /
  `--warn` / `--amber` / `--track` / `--dim` 等）をLight/Darkの2組でCSSカスタム
  プロパティとして定義する。Darkは`html[data-theme="dark"]`と
  `@media (prefers-color-scheme: dark)`の両方で効かせる
- テーマ切替（Light / Dark / Auto）。`html[data-theme]`の付け外しで表現し、選択値は
  localStorageに保存する。Autoは属性を外してOS設定に委ねる
- 5タブ構成（概要 / 推奨アクション / メンバー別 / 組織 / 前提と注意）。既存sectionの
  割り当ては次のとおりで、**sectionの中身は移動のみ**とする:
  - 概要: KPIカード4枚 / 月次推移 / 前月からの変化 / 追加クレジットの状態 / 月中のメンバー変動 / 主な増減
  - 推奨アクション: 判定サマリ / 追加クレジット付与候補 / 推奨一覧（この順。要約を先に置き、一覧は全幅で下段）
  - メンバー別: ユーザ別API換算コスト / 詳細利用状況 / 月中の利用推移 / 月中のClaude Code活動 / Codeと他プロダクトの需要 / Code・他product需要の内訳
  - 組織: 部署別サマリ / チーム別サマリ / 組織内の分布
  - 前提と注意: 前提と注意（件数を持たないタブ）
- カード・表・バー・バッジの体裁をデザイン仕様の数値どおりに実装する
- **JSが無効でも全section が読めるようにする**。タブの出し分けはJSが付けたクラスの下でのみ
  効かせ、JSが動かない環境では全sectionが縦に積まれた状態で表示される。共有先の閲覧環境を
  こちらで選べないため、JSに依存して内容が消える作りにはしない

受け入れ条件:

- `dashboard.html`と`preview-dashboard.html`以外の出力がバイト一致で不変
  （`report.md`・`details.md`・`recommendations.csv`・`usage-summary.csv`・`preview.md`・
  `summary/<month>.md`）。数値が変われば必ずこれらにも現れるので、これをもって
  「再設計がデータを壊していない」ことの検査とする
- 表示する数値を増やさない。既存の数値からその場で導ける表現（判定サマリ・追加クレジットの
  状態の帯・月次推移のバー・タブの件数バッジ）はデザインの一部として認めるが、新しい集計・
  新しい入力は足さない。件数バッジは**そのタブに実際に描画されている中身から数える**こと。
  組織タブは先頭に描画される集計軸（部署があれば部署、無ければチーム）の行数とし、軸が
  1つも無ければバッジを出さない（描画されない軸を数えると、部署が無くチームだけある組織で
  0 と出る）
- 既存sectionの中身は移動のみとする。ただしユーザ表記だけは例外で、**dashboard上のすべての
  ユーザ表示をローカル部 + `title`（フルアドレス）に統一する**。**Markdown側（`report.md`・
  `details.md`・`preview.md`）は完全なメールアドレスのまま**にする
  - 印刷やタッチ環境では`title`が読めず、異なるドメインで同じローカル部の場合、画面上の
    短縮表記だけでは区別できない。識別が必要な場面ではMarkdown側の完全なアドレスを確認する
- Light・Darkとも、文字として使う色が実際に載る背景に対して4.5:1以上であること（大きい文字
  は3:1）。デザインのトークンをそのまま採ると**両テーマとも**基準を下回る組み合わせがある
  （Lightは最小2.63、Darkは最小4.19）。トークンを動かすときは色相を保ったまま明暗だけを
  調整し、DOMに実在する組み合わせを実測して確かめる
- 条件つきsectionが出ない組織・LoC列が無い組織・実課金が全員ゼロの組織でも崩れない
- 外部参照（フォント・スクリプト・画像）を持たない。自己完結HTMLであることを維持する
- Jinjaのautoescapeを維持する。CSV由来の値をJSのコンテキストへ渡さない
- 判定・推奨の内容を変更しない（表示のみ）
- CSSとpartialは正式・速報で共有したままにする。速報側を旧デザインで固定するために
  テンプレートを二重化しない
- 既定の列構成で横スクロールが出ないことを確認する

##### Step 8D-2: 対話機能

対象:

- `src/seat_analyzer/templates/dashboard.js`
- `src/seat_analyzer/templates/dashboard.css`
- `tests/golden/`

実装:

以下をこのStepの仕様とする。

- **列ソート**: ヘッダクリックでトグル。初回は数値列が降順・文字列列が昇順。ソート中の列に
  `↑` / `↓` を表示する。数値は表示文字列を解釈して比較する（`$` `,` `+` `%` を除去し、
  `B` / `M` / `K` の接尾辞を 1e9 / 1e6 / 1e3 に展開）。解釈できない値（`—`・空）は最小に倒す。
  同値の行はもとの並び順を保つ（安定ソート）
- **検索**: ユーザ名＋メールアドレスの部分一致（大文字小文字を無視）。入力欄は幅200px・
  `padding: 7px 11px`・背景`--surface-2`・`border-radius: 8px`、フォーカス時に
  `border-color: --accent`。絞り込み中はカードの件数表示にその旨を添える
- **判定フィルタ**: 推奨一覧のみ。選択肢は実データに現れた判定から作る（固定リストにしない。
  出ない判定を選べると空表になる）
- **行数**: 表もバーも一覧の全行を出す。行数による折りたたみは持たない（表は下のスクロール
  領域と高さ変更で読む量を調整でき、気づかれない折りたたみは全体を確認する妨げになる）
- **ヘッダ固定**: スクロールコンテナ内で`th { position: sticky; top: 0; z-index: 2 }`
- **スクロール領域の高さ変更**: 各スクロールコンテナ直下に自前のグラブバーを置く
  （高さ18px・背景`--surface-2`・上線`--line`・中央に`28px × 3px`のグリップ2本`--dim`・
  `cursor: ns-resize`・hoverで`--accent-soft`）。`pointerdown` → `pointermove`で高さを増減
  （**縦のみ**、最小180px）、ドラッグ中は`body { user-select: none }`。ブラウザ標準の
  `resize`コーナーは使わない（ダークテーマで視認性が低いため）
- **初期高さ**: 推奨一覧・詳細利用状況は620px、その他の表は540px
- **hover**: 表の行は背景`--hover`、ソート可能なヘッダは文字色`--ink`
- **transition**: 色・背景のみ`.15s`。それ以外のアニメーションは持たない
- **触れる部品の輪郭**: 検索入力欄・判定フィルタ・テーマ切替の選択中セグメントは
  枠線に`--dim`を使う。デザインが指定する`--line`はカード面との比が
  Light 1.26 / Dark 1.25 で、UI部品の境界の目安3:1を大きく下回り「触れる部品だと分からない」
  ため。`--dim`ならLight 4.81 / Dark 4.95 で、両テーマの差も0.3以内に収まる。
  **グラブバーの上線は`--line`のまま据え置く**（掴めることはグリップ＝`--dim`が伝えており、
  上線を濃くすると表とカード脚注の間で罫線が情報より目立つため）。この据え置きは意図であって
  直し忘れではない

受け入れ条件:

- 8D-1と同じ出力不変条件を満たす
- JSが無効な環境では、ソート・検索・高さ変更のUIが現れず、全行が表示されたままになる。
  内容が欠けたり操作できないUIが残ったりしない
- ソート・検索・フィルタは表示の並べ替えと絞り込みに限る。数値を再計算しない
- 8D-1と同じく、CSV由来の値をJSのコンテキストへ渡さない（JSはDOM操作だけを行う）

#### Step 8E: dashboardへのproduct軸の掲載

v1.1.0で追加したStep。原設計には無い。Step 8Dより先に行う（§Step 8Fの実行順を参照）。
提案書§5.3「Codeと他プロダクトを分離して表示する」の掲載面。既存の詳細利用状況の
product構成は利用回数基準で金額とはズレるため、金額（API換算需要）基準の分離を
dashboardで読めるようにする。

依存:

- Step 8G

対象:

- `src/seat_analyzer/templates/dashboard.html.j2`（placeholder `<!--PRODUCT_SECTION-->`）
- `src/seat_analyzer/templates/partials/product.html.j2`（新規）
- `src/seat_analyzer/report/html.py`
- `src/seat_analyzer/analyze/__init__.py`（summaryへ表示用しきい値を1キー追加）
- `tests/golden/`

実装:

- 正式ダッシュボードへセクション「Codeと他プロダクトの需要（API換算）」を追加する。
  位置は「詳細利用状況」の直後・「組織内の分布（参考値）」の前。速報は対象外
  （PreviewResultはproduct_usageを持たない）
- データ源は`AnalysisResult.product_usage.features`のみで、再計算をしない。行の範囲は
  featuresの行（対象月のスペンドに明細のあるユーザ）で、利用ゼロのメンバーと
  組織サービス利用は含まれない（注記に明記する）
- 並びはtotal_demand_usdの降順・emailタイブレーク・欠損値は末尾
- 冒頭に組織サマリ1行:「Code需要 $X / 全需要 $Y（Z%）・対象 n名」。X・Yはcodeとtotalの
  両方が確定した行（m名）だけの合計とし、m < nなら確定分m名であることを併記、m = 0なら
  この行を出さない。比率はY > 0のときだけ出す
- 積み上げバー: ユーザ別API換算コストと同型（.bar / .track / .fill）。1本の棒を
  Code（var(--ok)）と他プロダクト（#9aa3ad）の2セグメントにinline styleで塗り分ける。
  code_demand_usdが欠損のユーザは全幅を斜線ハッチ（内訳不明）にし、total_demand_usdが
  欠損なら塗らない。値ラベルは「$X (Code Y%)」（欠損は—）
- テーブル6列: ユーザ / 需要（計） / Code需要 / Code比率 / 他product需要 / product数。
  他product需要 = total − code（どちらか欠損なら—）。Code比率は整数%。確定できない値は
  すべて—で、0や空文字で埋めない（usage-summary.csvと同じ規則）
- supplementary_highがTrueの行は他product需要セルに⚑を付ける（False・欠損は無印）。
  凡例に「⚑ = 補助プロダクトの需要がしきい値以上。Codeが低く他が高いユーザは自動変更
  ではなくレビュー対象（判定・推奨には未反映）」の趣旨を書く。しきい値の金額は
  `summary["supplementary_high_usd"]`（analyzeがconfigのproduct_policyから詰める。
  grant_suggested_cap_usdと同じ流儀）から表示し、テンプレートへ直書きしない
- prohibited_observedは載せない（禁止指定の報告はCLIのみ・§Step 8）
- セクションの表示条件: product_usageがあり、featuresが非空で、code_demand_usdに確定値が
  1つ以上あること。満たさなければセクションごと省略する（product列が無い入力の理由説明は
  CLIのCAPACITY_SIGNAL_UNAVAILABLE警告が担う）
- CSSは変更しない: dashboard.cssは速報と共有され、変更するとpreview-dashboard.htmlの
  バイト一致が壊れる。既存クラスの再利用とpartial内のinline styleで作る

受け入れ条件:

- `dashboard.html`以外の出力がバイト一致で不変（`preview-dashboard.html`を含む）
- 差分が「productのセクションが増えただけ」であること
- 既存8列を増やさない
- 任意入力なしでも崩れない

今回は行わない:

- 判定への反映（Track 4）
- 速報ダッシュボードへの掲載
- report.md / details.md への掲載


### Track 3: シート変更履歴

#### Step 9: SeatChangeEvent

依存:

- Step 3

対象:

- `src/seat_analyzer/seat_changes.py`
- `tests/test_seat_changes.py`

実装:

- Members snapshot pairから正準event

受け入れ条件:

- upgrade/downgrade
- added/removed
- unassigned
- 重複なし

今回は行わない:

- report
- 推奨照合

#### Step 10: seat-change-events.csv

依存:

- Step 9

対象:

- `src/seat_analyzer/report_v2.py`
- `tests/test_cli.py`

受け入れ条件:

- changed_after/before
- from/to
- source
- 同入力で決定的

今回は行わない:

- 評価

#### Step 11: decision snapshot保存

依存:

- Step 3

対象:

- `src/seat_analyzer/report_v2.py`
- `tests/test_cli.py`

実装:

- V1 recommendationをhistoryへ保存
- source manifest

受け入れ条件:

- source hashが同じなら同じpath
- 異なるsourceなら別snapshot
- 既存report不変

今回は行わない:

- V2
- change matching

#### Step 12: 推奨と実変更の照合

依存:

- Step 10
- Step 11

対象:

- `src/seat_analyzer/decision_audit.py`
- `tests/test_decision_audit.py`

実装:

- latest pre-change snapshotを選択
- matched/without/opposite/not-evaluable

受け入れ条件:

- 未来snapshotを使わない
- 推奨なしを明示
- exact change timeを捏造しない

今回は行わない:

- follow-up

### Track 4: V2判定

#### Step 13: V2 domain

依存:

- Step 4

対象:

- `src/seat_analyzer/domain.py`
- `tests/test_decision_v2.py`

実装:

- status
- seat action
- credit action
- reason code

受け入れ条件:

- serialization
- stable value

今回は行わない:

- 判定関数

#### Step 14: asymmetric history config

依存:

- なし

対象:

- `config.yaml`
- `src/seat_analyzer/config.py`
- `tests/test_hardening.py`

実装:

- upgrade=1
- downgrade=2
- recent change期間

受け入れ条件:

- defaultあり
- V1 hysteresis不変

今回は行わない:

- V2判定

#### Step 15: Upgrade rule

依存:

- Step 7
- Step 9
- Step 13
- Step 14

対象:

- `src/seat_analyzer/decision_v2.py`
- `tests/test_decision_v2.py`

実装:

- Standard→Premiumだけ

受け入れ条件:

- 1完全月で候補化可能
- Code低・他product高はreview
- partial monthはno decision
- V1不変

今回は行わない:

- downgrade
- credit提案

#### Step 16: Downgrade rule

依存:

- Step 9
- Step 15

対象:

- `src/seat_analyzer/decision_v2.py`
- `tests/test_decision_v2.py`

実装:

- Premium→Standardだけ

受け入れ条件:

- 2完全月必須
- Code/total両方低い
- recent changeはobserve
- supplementary高はreview

今回は行わない:

- credit提案

#### Step 17: Admin credit loader

依存:

- Step 2

対象:

- `src/seat_analyzer/admin_inputs.py`
- `config.yaml`
- `tests/test_admin_inputs.py`

実装:

- organization snapshot
- user credit snapshot
- as-of選択

受け入れ条件:

- 入力なしで空result
- 未知値は`NA`
- 同日重複はerror

今回は行わない:

- browser
- decision

#### Step 18: Credit comparator

依存:

- Step 15
- Step 17

対象:

- `src/seat_analyzer/decision_v2.py`
- `tests/test_decision_v2.py`

実装:

- κの3状態に応じた候補化の信号（§12.4の表）
- 継続性

受け入れ条件:

- 設定不明はreview
- 一時需要はcredit候補
- 継続需要はPremium候補

今回は行わない:

- UI

#### Step 19: decision-evidence.csv

依存:

- Step 15
- Step 16
- Step 18

対象:

- `src/seat_analyzer/decision_evidence.py`
- `src/seat_analyzer/report/evidence_csv.py`
- `src/seat_analyzer/cli.py`
- `tests/test_cli.py`

実装:

- `--decision-version v2`
- evidence CSV

受け入れ条件:

- reason codes
- Code/total分離
- V1選択可能
- V2をdefaultにしない

今回は行わない:

- dashboard

### Track 5: 変更後評価

#### Step 20: UsageInterval

依存:

- Step 7

対象:

- `src/seat_analyzer/usage_intervals.py`
- `tests/test_usage_intervals.py`

実装:

- 累積snapshotの差分
- 月替わり
- daily rate

受け入れ条件:

- 同月差分
- 月替わり非差分
- 累積減少warning
- mixed seat flag

今回は行わない:

- follow-up

#### Step 21: 2週間評価

依存:

- Step 9
- Step 20

対象:

- `src/seat_analyzer/decision_audit.py`
- `tests/test_decision_audit.py`

実装:

- 14日窓だけ
- coverage

受け入れ条件:

- coverage 60%未満はnot evaluable
- mixed interval除外
- Code/total/billed rate

今回は行わない:

- 4/8週
- GitHub

#### Step 22: 4週間評価

依存:

- Step 21

対象:

- `src/seat_analyzer/decision_audit.py`
- `tests/test_decision_audit.py`

実装:

- 28日窓

受け入れ条件:

- 2週結果不変
- 28日coverage

今回は行わない:

- 8週

#### Step 23: 8週間評価

依存:

- Step 22

対象:

- `src/seat_analyzer/decision_audit.py`
- `tests/test_decision_audit.py`

実装:

- 56日窓

受け入れ条件:

- 2/4週結果不変
- 56日coverage

#### Step 24: review.md

依存:

- Step 12
- Step 19
- Step 23

対象:

- `src/seat_analyzer/report_v2.py`
- `tests/test_cli.py`

実装:

- 推奨
- 実変更
- 一致・不一致
- 2/4/8週

受け入れ条件:

- 事実と解釈を分離
- 担当者個人を採点しない
- データ不足を表示

ここまでを最初の実用リリースとする。

### Track 6: Browser-assisted取得

#### Step 25: Download watcher基盤

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 0C成功

対象:

- `src/seat_analyzer/browser_collect.py`
- `tests/test_browser_collect.py`

実装:

- download directory検証
- 開始時snapshotとの差分検出
- download完了までのsize安定待ち
- Spend header判定

受け入れ条件:

- 通常分析はbrowserなしで動く
- 既存ファイルを誤検出しない
- 不明なCSVを無視する
- 追加dependencyなし

今回は行わない:

- browser起動
- `input/`への配置

#### Step 26: Spend検出・配置

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 25

対象:

- `src/seat_analyzer/browser_collect.py`
- `tests/test_browser_collect.py`

実装:

- 1組織
- Spendだけ
- 通常ブラウザを開く
- 検出したSpend CSVを対象Organizationへコピー

受け入れ条件:

- download path
- filename validation
- timeout時の手動fallback案内
- シート変更操作なし

今回は行わない:

- Members
- Code Analytics

#### Step 27: Members検出・配置

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 26

実装:

- Membersだけ追加

受け入れ条件:

- Spend動作不変
- snapshot filename

#### Step 28: Code Analytics検出・配置

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 27

実装:

- Code Analyticsだけ追加

受け入れ条件:

- Spend/Members動作不変
- export不可ならwarning

#### Step 29: Collection manifest

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 28

実装:

- SHA-256
- idempotence
- manifest

受け入れ条件:

- 重複配置なし
- 部分失敗を記録

#### Step 30: collect CLI

見送り（2026-10-07）: 方式変更のため実装しない。代替は Step 50〜51

依存:

- Step 29

対象:

- `src/seat_analyzer/cli.py`
- `tests/test_cli.py`

実装:

- `collect --source browser`

受け入れ条件:

- browser失敗で分析を壊さない
- 手動fallback案内

#### Step 31: Admin credit入力補助

依存:

- Step 17
- Step 51

実装:

- Membersから入力用templateを生成する
- 利用者が明示指定したCSVをcanonical admin users CSVへ変換する
- credit値以外の管理画面情報を要求しない

受け入れ条件:

- read-only
- 未入力値を不明として保持
- 不明値を0にしない
- 月次の追加手作業を5分以内に収める

今回は行わない:

- seat変更
- credit変更
- DOM取得
- clipboardの自動読取

#### Step 50: claude.aiエクスポート取得の土台（config・計画・検証・配置・--dry-run）

依存:

- §14の方式（2026-10-07の実機検証で確定）

対象:

- `src/seat_analyzer/claude_export.py`（新規・層15。importは`ingest`だけ）
- `src/seat_analyzer/config.py`・`src/seat_analyzer/default-config.yaml`
- `src/seat_analyzer/cli.py`
- `tests/test_claude_export.py`・`tests/test_config.py`・`tests/test_cli.py`・
  `tests/test_module_deps.py`

実装:

- config: トップレベルの`claude_export`（`chrome_path`・`profiles_dir`・`staging_dir`・
  `timeout_minutes`）と、`organizations.<組織名>.claude_export`・
  `organizations.<組織名>.workspaces.<workspace名>.claude_export`（`profile`・`org_id`・`kinds`）。
  ロード時に検証する（UUIDの形式・プロファイル名・`kinds`（不活性の区画でも）・`org_id`の無い
  区画に書いた`profile`や既定と違う`kinds`・`workspaces`を持つ組織の直下の区画・同じ`org_id`の
  重複・未知のキー）。計画を作る時点で、大文字小文字や文字の合成の違いだけの組織名・
  workspace名も止める
- `claude_export.py`: 対象の列挙（`gated_targets`）、モードの決定と計画（`resolve_mode`・
  `plan_runs`）、拡張機能へ渡す実行内容とトリガーURL、manifestの読み取りと計画との突き合わせ、
  検証（`verify_export`）と配置（`place_export`）、Preferencesの更新、Chromeの場所・起動
  コマンド・プロセスの列挙と終了コマンドの組み立て（ここまで純粋関数）と、プロセスを起動・
  列挙・終了させる薄いラッパ
- CLI: `collect --source claude --dry-run`。`--org`を複数指定可にし、`--month`とともに必須を
  外す（githubは従来どおり`--org`1つと`--month`が必須で、欠ければ終了コード2）。`--profile`・
  `--dry-run`はclaude専用

受け入れ条件:

- 既定で不活性（`claude_export`未設定なら従来と同じ動き）
- goldenが不変
- `--dry-run`が計画を表示する
- 設定の誤りがロード時に止まる
- 検証で期間と種別の合わないCSVを配置しない
- 追加dependencyなし

今回は行わない:

- 拡張機能（`browser_extension/`）
- ブラウザの起動・待機・終了と配置のCLIへの結線（`--dry-run`の無い`--source claude`は使えない）
- `--setup`・`--login`・`--list-orgs`・`--keep-browser`・`--timeout`
- 利用者向けdocs（usage・setup・reference・README・CHANGELOG）

#### Step 51: 拡張機能とcollect --source claudeの結線

依存:

- Step 50

対象:

- `src/seat_analyzer/browser_extension/`（新規・非.pyの同梱物。wheelに入ることを確かめる）
- `src/seat_analyzer/claude_export.py`・`src/seat_analyzer/cli.py`・`src/seat_analyzer/config.py`・
  `src/seat_analyzer/default-config.yaml`・`src/seat_analyzer/templates/workspace-config.yaml`
- `docs/usage.md`・`docs/setup.md`・`docs/reference.md`・`docs/README.md`・`README.md`・
  `CHANGELOG.md`
- `tests/`

実装:

- 拡張機能（manifestの`key`に公開鍵を入れてIDを固定する。秘密鍵は置かない）: トリガーURLの
  受け取り（コンテンツスクリプトとタブのURL更新の2経路。同じタブは1回だけ起動し、同じ
  run_idは2度実行しない。同じChromeで進行中の別の実行があれば始めない）、組織の切替（Cookie）と
  切替後の確認（組織のAPIの直近の呼び出しが対象のUUIDに揃うこと。支出レポートとClaude Codeの
  ページでも確かめる）、3ページのエクスポート操作（種別ごとに独立。支出レポートは期間の
  開始が1日であることを確かめてからダウンロードし、Claude Codeはダウンロードが始まらなければ
  押し直す）、ダウンロードのstagingへの振り分け（固定名は拡張機能自身のダウンロードにだけ
  付ける）、progress.json・manifest.json・orgs.jsonの出力、ログインや外部セキュリティ検証が
  要るときの要操作の表示と待機（最大10分）
- CLI: Preferencesの確認（ダウンロード先がstaging）→ プロファイルのロック（OSのファイルロック）→ run.jsonの
  書き出し → Chromeの起動 → manifestの待機（途中経過の表示。進捗が60秒止まったらブラウザの
  要操作の確認を促し、時間切れではChromeを残して`--import`を案内）→ 検証（ファイル名の組織
  UUIDの照合を含む）・配置 → Chromeの終了（プロファイルが一致し、実行ファイルが起動に使った
  ものと一致するか名前にchrome / chromiumを含むプロセスだけ。見つからなければ警告だけ）→ ロックの解放。`--setup`（プロファイルを作ってChromeを起動し、手順を表示して待たずに終わる）・
  `--finish-setup`（Chromeを終了させ、拡張機能が同梱の場所から読み込まれていることを
  Secure Preferences・Preferencesで確かめてからPreferencesを書く）・`--login`・`--list-orgs`・
  `--import`・`--keep-browser`・`--timeout`
- config: `claude_export.profiles_dir`・`staging_dir`の相対パスの基準を設定ファイルの置き場所に
  する（`paths`と同じ）。`chrome_path`は絶対パスか空文字に限る（§21）
- 利用者向けの手順（初回のセットアップ・月次の取得・手動のフォールバック）

受け入れ条件:

- 実機で複数プロファイルの当月／前月の3種を設定した全スペースから無操作で取得
- ログイン期限切れを安全に扱う（要操作の表示と待機）
- シート／creditの変更操作が存在しない
- 手動フォールバックが動く

今回は行わない:

- 当月・前月以外の月の取得
- 管理画面のcredit設定の取得（Step 31）

#### Step 52: ログインセッションの期限の確認

依存:

- Step 51

対象:

- `src/seat_analyzer/browser_extension/run.js`・`src/seat_analyzer/claude_export.py`・
  `src/seat_analyzer/cli.py`・`src/seat_analyzer/config.py`・`src/seat_analyzer/default-config.yaml`・
  `src/seat_analyzer/templates/workspace-config.yaml`
- `docs/usage.md`・`docs/setup.md`・`docs/reference.md`・`CHANGELOG.md`
- `tests/`

目的:

- 取得の定期実行（日次・週次）を見据え、専用プロファイルのログインセッションがあと何日で
  切れるかを確かめ、期限が近い・切れているときに終了コード1で知らせる。再ログインは
  `--login <profile>`を使う

実装:

- 拡張機能: `action: "check-login"`を足す。claude.aiを開き（遷移の完了と2〜4秒の待ちの後）、
  ページの状態を1秒ごとに最大20秒見る。アプリかログイン画面に定まったら打ち切り、外部
  セキュリティ検証は自動で解けることがあるので上限まで見直す（上限まで検証のままなら
  `challenge`、一度も読めなければ`unknown`）。人の操作は待たない（要操作の表示と待機を使わない）。
  アプリが表示されたときだけ`/api/organizations`を1回読む（上限10秒。組織一覧と同じ関数で、
  HTTPのステータスも返す）。セッションのCookie（`sessionKey`）の有無と期限（期限の無いものが
  混じればnull、複数あれば最も早いもの）と合わせて`session.json`に書く。途中の失敗はできる
  限り観測として書く。Cookieの値は書き出さない
- 拡張機能: 取得の最後にも、manifest.jsonの前にCookieの期限だけを同じ形（`page`・`api`はnull）で
  `session.json`に書く。その失敗は記録だけしてmanifestの保存へ進む
- 拡張機能: `action: "login"`を足す。名前が`sessionKey`で始まるCookieだけを消してから、作業
  タブでログイン画面を開く（待たない・何も書かない）。他のCookie（`lastActiveOrg`等）には
  触れない
- `session.json`は取得のmanifestと独立したファイルにする（取得の結果の読み取りを変えない）
- 判定（`claude_export.session_status`）の優先順位は固定で、上から最初に当たったものを採る:
  1. ログイン画面が表示された → 無効
  2. APIの読み取りがHTTP 401 → 無効
  3. 外部セキュリティ検証のまま・ページの状態を読めなかった・APIの読み取りに失敗（403を
     含む401以外と応答なし。403は権限の問題でもありうるので無効にしない）・アプリなのに
     APIの結果が無い → 不明
  4. Cookieが無い・期限が無い → 不明
  5. 残り日数（期限までの時間を1日単位で切り捨て。表示と比較に同じ整数を使う）が
     `claude_export.login_warning_days`（既定3・0以上の整数）を下回る → 期限間近、それ以外 → 有効
- CLI: `collect --source claude --check-login [<profile>]`。省略時は`claude_export`を設定した
  プロファイルを初出順に重複なく、指定時はそのプロファイルだけ（設定に無くてもよい）を順に
  確かめる。プロファイルごとに設定の確認 → ロック → 起動 → `session.json`の待機（要操作の
  案内は出さない。時間切れはChromeを残してブラウザの確認を案内する。不明のときも`--login`
  ではなく`--check-login <profile> --keep-browser`での再確認を案内する）→ run_idの照合 → 1行の表示 →
  Chromeの終了。確かめた全プロファイルが有効のときだけ終了コード0。`--timeout`・
  `--keep-browser`以外とは併用できない。`claude_export`を設定した組織が無ければ何もせず1
- CLI: 通常の取得の配置の後に`session.json`があれば、Cookieの期限を1行添える（期限間近なら
  `--login`の案内も）。無ければ何も出さず、読めなければ警告1行だけ。取得の成否は変えない
- CLI: `--login`は拡張機能の`action: "login"`のトリガーURLで起動する（設定の済んでいない
  プロファイルでは起動しない）。`--setup`は従来どおりログイン画面を直接開く

注意:

- Cookieの有効期限は上限の目安で、サーバー側のセッションが先に切れることがある。そのため
  APIを実際に読めたかを先に見る
- 通知の手段（OSの通知・チャット）はこのStepの範囲外で、終了コードと表示までにする

受け入れ条件:

- 実機で残り日数が表示される
- 期限切れのプロファイルで「無効」と`--login`の案内が出る
- 有効だが期限間近のプロファイルで`--login` → 再ログイン → 再チェックで期限が更新される
- 既定で不活性（`claude_export`を書いた組織が無ければ何もしない）

今回は行わない:

- 定期実行の仕組みそのもの（launchd・cronの設定）
- 通知の送信

#### Step 53: ページ遷移の間の待ち時間に揺らぎを入れる

依存:

- Step 51

対象:

- `src/seat_analyzer/browser_extension/run.js`
- `tests/test_browser_extension.py`

目的:

- 取得の操作が一定の間隔で並ばないよう、ページ遷移の間に2〜4秒の揺らぎを入れる。現状は
  ページの読み込みが終わってから固定の待ち（2秒）を置いて操作に移るので、遷移の間隔が
  ほぼ一定になる

実装:

- 遷移の完了から操作までの固定の待ち（2秒）を、2〜4秒の一様乱数に置き換える。遷移の前に
  2〜4秒を足す案は、揺らぎの幅が同じで時間だけが延びるため採らない。選んだ待ちは遷移ごとに
  実行ページのログへ1行出す
- ボタンや選択肢を押す前（エクスポート・支出レポートの期間の選択とダウンロード・前月モードの
  Claude Codeの月送り）にも、0.5〜1.5秒の一様乱数の待ちを入れる。待ちはタブ内でボタンを
  探して押す処理を呼ぶ手前に置き、探してから押すまでの間は従来どおり空けない

影響:

- 遷移は組織ごとに3回（メンバー一覧・支出レポート・Claude Code）、押す操作は5回（前月
  モードは6回）なので、当月モードでは1組織あたり取得が平均8秒ほど（遷移の分が0〜6秒、
  押す前の分が2.5〜7.5秒）延び、プロファイルが持つ組織の数に比例する。待機の上限
  （`claude_export.timeout_minutes`、既定15分）に比べて小さい

受け入れ条件:

- 実機で、実行ページのログの時刻から遷移の間隔がばらついていることを確かめる
- 全スペースの取得が従来どおり通る

今回は行わない:

- 操作の順序の入れ替え
- マウス操作の模倣

#### Step 54: 当月モードのClaude Code analyticsの終了日を取得日にする

依存:

- Step 51

対象:

- `src/seat_analyzer/claude_export.py`・`src/seat_analyzer/cli.py`・`src/seat_analyzer/ingest.py`
- `docs/usage.md`・`docs/reference.md`・`CHANGELOG.md`
- `tests/test_claude_export.py`・`tests/test_cli.py`・`tests/test_filenames.py`

目的:

- 当月モードで取得したClaude Code analyticsを、ファイル名の終了日を取得日にした名前で置く。
  claude.aiのエクスポートは部分月でも終了日が月末日の名前で届き、分析はこの終了日をスナップ
  ショットの時点に使う（`ingest.code_snapshots`。速報の「月中のClaude Code活動」の見出しと最新
  区間の増分）。月末日のままでは見出しが月末日になり、週次の取得が同じ名前で上書きして時点が
  残らない。取得日時点のスナップショットとして扱える配置名にする

実装:

- `claude_export.placement_name(filename, kind, mode, fetched_on)`: 種別がcodeかつ当月モードの
  ときだけ、ファイル名の期間の終了日を取得日にする（置き換えるのは年月日の数字だけで、区切りと
  それ以外の文字は保つ。`ingest.replace_range_end`）。他の種別・前月モード・期間を読めない名前・
  取得日が期間の外か終了日と同じときは元の名前
- 取得日はUTCの日付（同じ実行でclaude.aiがmembersのファイル名に付ける日付と揃える）。取得では
  run.jsonの`created_at`と同じ時刻から、`--import`では`created_at`から求める
  （`claude_export.fetched_on_from_record`。無い・ISO 8601でない・タイムゾーンが無ければ止める）
- `place_export`に`dest_name`を足す（単一のファイル名でなければValueError）。検証はstagingの元の
  名前のファイルに対して従来どおり行い、stagingの名前は変えない
- CLI: 名前を変えたときは「配置:」の行に`（終了日を取得日 YYYY-MM-DD に変更）`を添える

受け入れ条件:

- 当月モードで取得したClaude Code analyticsが`…_to_<取得日>.csv`で置かれ、速報の「月中の
  Claude Code活動」の見出しが取得日になる
- 前月モードは元の名前のまま
- `--import`も同じ名前で置く（取得日はrun.jsonの`created_at`）
- stagingのファイルは元の名前のまま
- goldenが不変

#### Step 55: 取得できる時期をUTCの日付で判定する

依存:

- Step 54

対象:

- `src/seat_analyzer/claude_export.py`・`src/seat_analyzer/cli.py`
- `docs/usage.md`・`docs/reference.md`・`CHANGELOG.md`
- `tests/test_claude_export.py`・`tests/test_cli.py`

目的:

- claude.aiのエクスポートの期間はUTC基準で、ツールの当月・前月は実行機のローカル日付で決まる。UTCの
  今日がローカルの月に追いつく前に取ると、支出レポートとmembersは別の月の期間で届いて検証で止まり
  （終了コード1）、Claude Code analyticsは画面の表示月の名前で検証を通り、取得日が期間の外なので
  終了日の置き換えが働かず月末日の名前のまま当月に置かれうる。定期実行では1日・2日に当たる日に
  これが起き、誤った通知と誤った名前のファイルになる。日本時間に固有の「1日・2日は取らない」規則を
  定期実行のスクリプトに持たせる代わりに、タイムゾーンに依らない判定をツール側に置く（§14.1）

実装:

- `claude_export`に時期の判定を足す（例: `not_ready_reason(mode, month, local_today, utc_today)`。
  名前は実装時に決めてよい）。当月モードはUTCの今日の年月がローカルの年月と同じで日が2以上、
  前月モードはUTCの今日の年月がローカルの年月と同じなら`None`。満たさなければ理由の文。理由は
  §14.1の3通り: 必要なUTCの時点（当月は対象月の2日0:00 UTC、前月は翌月の1日0:00 UTC）に達して
  いなければ取得できるようになるローカルの時刻（その時点を`local_timezone()`で直したもの）、UTCが
  先に翌月へ進んでいれば、当月モードは「ローカルが翌月になってから前月モードで」、前月モードは
  「期間を過ぎたので手動で置く」
- CLI: `plan_runs`の後、Chromeを起動する前に判定する。満たさなければ理由を表示して、`--month`の
  省略時は終了コード0、明示時は1。`--dry-run`でも同じ表示（計画は出さない）。各プロファイルの
  起動直前にも判定し直す: いまのローカル日付で`resolve_mode`を引き直して計画と同じモード・月に
  なり、かつ時期の規則を満たすときだけ起動し、そうでなければ対象月を変えずにそのプロファイルを
  理由つきで失敗にする（残りのプロファイルも同じ）。`--import`・`--check-login`・`--list-orgs`・
  `--setup`・`--finish-setup`・`--login`は対象外
- 現在の日時は`claude_export.local_today()`と`claude_export.utc_now()`から取り、テストから差し替える。
  tests/test_cli.pyの`_dry_run`は`local_today`だけを固定しているので、`claude_env`と同じ瞬間に
  `utc_now`・`local_timezone`も固定する（固定しないと実行日によって落ちる）
- docs/usage.mdの「当月は実行機のローカル日付で決まる」に、取得できる時期（日本時間では当月は
  2日9:00以降、前月は1日9:00以降）・3通りの理由・終了コードを添える。docs/reference.mdの
  `--dry-run`（計画を出さない場合）と終了コード（省略時の0の例外）の説明を揃える

受け入れ条件:

- ローカルが日本時間のとき、当月モードは2日9:00以降、前月モードは1日9:00以降だけChromeを起動する。
  それより前は起動せず、理由と取得できる時刻を表示する
- UTCより西のタイムゾーン（例: UTC-8）でローカルの月末の夜に動かすと、UTCの今日が翌月なので当月・
  前月モードとも止まり、当月モードは翌月に前月モードで取るよう、前月モードは手動で置くよう案内する
- `--month`の省略時は終了コード0、明示時は1。`--dry-run`も同じ
- 2つ目以降のプロファイルの起動直前に、UTCの月が変わっている・UTCとローカルが同時に翌月へ進んで
  計画のモード・月と合わなくなった、のどちらでも、そのプロファイルは起動せずに失敗になる
- UTCとローカルが同じ年月の1日に当月モードを動かすと、2日0:00 UTCに当たるローカルの時刻を案内する
- テストはUTC+14とUTC-12、年末年始、2日0:00（UTC）の直前と直後を含み、既存のdry-runのテストが
  実行日によらず通る
- それ以外の日時では計画・取得・配置・表示が変わらない。goldenが不変

#### Step 56: 定期実行の雛形（macOS launchd）

依存:

- Step 55

対象:

- `examples/launchd/collect.zsh`・`examples/launchd/seat-analyzer.collect-weekly.plist`・
  `examples/launchd/seat-analyzer.collect-monthly.plist`・`examples/launchd/install.zsh`（新規）
- `docs/setup.md`・`docs/usage.md`・`docs/reference.md`・`README.md`・`CHANGELOG.md`
- `tests/test_examples_launchd.py`（新規）

目的:

- 週次（当月）と月次（前月）の取得とログインの確認を無人で回し、失敗と期限間近だけ通知する雛形を
  公開する（§14.5）。ツールの機能にはしない

実装:

- §14.5の構成と動き
- docs/setup.mdに「定期実行を設定する（macOS・任意）」の節: 4ファイルの取り方（リポジトリから）、
  `install.zsh <ワークスペース>`の実行と確認（`launchctl print`）、試し方（一時ディレクトリに
  `seat-analyzer init-org --input-dir <一時> <組織名> [--workspaces ...]`で配置先の組織ディレクトリを
  作ってから、`SEAT_ANALYZER_INPUT_DIR`でそこへ配置して本体を直接実行し、登録後は
  `launchctl kickstart`で1回動かす）、ログと通知、解除（`install.zsh --uninstall`）、スリープするMacの
  扱いとlaunchdの前提（§14.5の4点: GUIログイン・起床後の1回の実行・対象月は実行時点・遡及なし）、
  `login_warning_days`を10にする理由（週1回の確認と対応の猶予3日）
- docs/usage.mdの運用例と「定期実行と通知の仕組みはこのツールに含まない」の文を雛形への案内に
  変える。docs/reference.mdの`login_warning_days`の例に週次の値を添える。READMEに1行

受け入れ条件:

- `install.zsh <ワークスペース>`で2つのジョブが登録され、`launchctl print`で見える。再実行で
  入れ替わり、`--uninstall`で消える
- 一時ディレクトリへの配置で、weeklyが当月、monthlyが前月を取得し、どちらも最後に`--check-login`を
  行う。取得か`--check-login`の終了コードが0でないときだけ通知が出て終了コード1
- Step 55が「まだ」と判定する日時の週次は、取得せずにログイン確認へ進み、それが成功すれば正常終了する
  （Step 55の表示がログに残る。偽のコマンドで、取得が終了コード0で何も置かない場合を含める）
- テストが全OSで通る（zsh・macOS固有の検査はその環境でだけ行う）。雛形に個人のパス・組織名が無く、
  `check-text --diff`が0件

### Track 7: GitHub

#### Step 32: GitHub mapping loader

依存:

- Step 3

対象:

- `src/seat_analyzer/github_collect.py`
- `tests/test_github_collect.py`

実装:

- github-members.csv

受け入れ条件:

- duplicate error
- missing warning

今回は行わない:

- gh実行

#### Step 33: GitHub doctor

依存:

- Step 5
- Step 0A成功

対象:

- `src/seat_analyzer/data_quality.py`
- `src/seat_analyzer/github_collect.py`
- `tests/test_data_quality.py`

実装:

- auth
- org access
- rate status

受け入れ条件:

- token非表示
- scope不足をissue化

今回は行わない:

- PR保存

#### Step 34: Repository自動発見

依存:

- Step 33

対象:

- `src/seat_analyzer/github_collect.py`
- `tests/test_github_collect.py`

実装:

- 全repository
- Archive/Fork/Template除外

受け入れ条件:

- 手動allowlist不要
- pagination
- fixture gh output

今回は行わない:

- PR

#### Step 35: PR検索とraw cache

依存:

- Step 34

対象:

- `src/seat_analyzer/github_collect.py`
- `src/seat_analyzer/cli.py`
- `tests/test_github_collect.py`
- `tests/test_cli.py`

実装:

- merge date range
- metadataだけ
- upsert
- `collect --source github`（§16.2）

受け入れ条件:

- title/body/codeを保存しない
- repo + numberで一意
- partial結果を明示

今回は行わない:

- 指標計算

#### Step 36: マージPR数

依存:

- Step 32
- Step 35

対象:

- `src/seat_analyzer/github_metrics.py`
- `tests/test_github_metrics.py`

実装:

- user/month merged count
- Bot除外

受け入れ条件:

- merge月帰属
- mapping不足は除外・warning

今回は行わない:

- lead time

#### Step 37: PR lead time

依存:

- Step 36

対象:

- `src/seat_analyzer/github_metrics.py`
- `tests/test_github_metrics.py`

実装:

- created→merged
- median/P75/P90

受け入れ条件:

- timezone正規化
- open/closed unmerged除外
- Draft期間含む

#### Step 38: github-summary.csv

依存:

- Step 37

対象:

- `src/seat_analyzer/report/github_csv.py`
- `tests/test_cli.py`

受け入れ条件:

- referenceラベル
- seat判定へ影響しない
- GitHubなしでもreview成功

#### Step 39: GitHub follow-up参考表示

依存:

- Step 23
- Step 38

対象:

- `src/seat_analyzer/decision_audit.py`
- `src/seat_analyzer/report_v2.py`
- `tests/test_decision_audit.py`

実装:

- 変更前後PR数
- lead time

受け入れ条件:

- 因果表現をしない
- データ不足を明示

### Track 8: Billingと表示

#### Step 40: 購入席loader

依存:

- Step 17

対象:

- `src/seat_analyzer/admin_inputs.py`
- `tests/test_admin_inputs.py`

実装:

- purchased seats
- contract terms

受け入れ条件:

- assigned > purchased warning/error
- Billingなしで分析継続

#### Step 41: 割当と購入の分離

依存:

- Step 40

対象:

- `src/seat_analyzer/report_v2.py`
- `tests/test_cli.py`

実装:

- assigned
- purchased
- unassigned purchased

受け入れ条件:

- 再配置をcash savingと表示しない
- 不明は`NA`

#### Step 42: Dashboard統合

依存:

- Step 8D
- Step 8E
- Step 24
- Step 38
- Step 41

対象:

- `src/seat_analyzer/report.py`
- `tests/test_cli.py`

実装:

- review summary
- Code usage（Step 8Eで追加済みの表示を維持する。ここで作り直さない）
- GitHub reference
- Billing

受け入れ条件:

- 既存8列を増やさない
- 別section/tab
- 横スクロールなし
- 任意入力なしでも崩れない

### Track 9: 複数workspace

仕様は§26。Phase 1（Step 43〜48）で運用に必要な全部を出し、Phase 2（Step 49）は
週次スナップショットが両workspaceで揃ってから着手する。

#### Step 43: workspaceの発見と設定

依存:

- なし（既存レイアウトに対する追加）

対象:

- `src/seat_analyzer/ingest.py`（`discover_workspaces`・混在レイアウトの検出）
- `src/seat_analyzer/config.py`（`organizations.<組織>.workspaces`の検証）
- `src/seat_analyzer/leakcheck.py`（入れ子レイアウトを組織ディレクトリとみなす構造判定）
- `src/seat_analyzer/cli.py`（`init-org --workspaces`・doctorの構造検査の結線）
- `src/seat_analyzer/data_quality.py`（§26.8の構造検査）
- `tests/test_ingest.py`・`tests/test_config.py`・`tests/test_leakcheck.py`・`tests/test_data_quality.py`

実装:

- 組織ディレクトリの子で`spend/`を持つものをworkspaceとして列挙する。名前の規則は
  `validate_org_name`と同じ
- 従来レイアウト（組織直下に`spend/`）との混在はエラー
- configの`workspaces`と列挙結果の不一致・主の不在・主の重複はそれぞれ別のメッセージで
  エラー（黙って片方を採らない）
- 混入チェックの組織判定は入れ子レイアウトも組織とみなす。禁止語の収集は再帰なので変えない。
  workspace名は禁止語に加えない
- `init-org <組織名> --workspaces main,second`で入れ子の雛形を作る

受け入れ条件:

- 従来レイアウトの組織の挙動が不変（全既存テスト成功）
- 混在・不一致・主の不在がそれぞれ止まる
- 検査のmessageに絶対パスを含めない

#### Step 44: workspace別の分析と結合

依存:

- Step 43

対象:

- `src/seat_analyzer/analyze/workspaces.py`（新設。analyzeパッケージ内なので層は変えない）
- `src/seat_analyzer/analyze/__init__.py`・`src/seat_analyzer/analyze/pipeline.py`
- `src/seat_analyzer/ingest.py`（発見名とconfigの突き合わせの純粋関数。doctorと共用）
- `tests/test_analyze.py`

実装:

- workspaceごとに`analyze()`を呼び、組織単位の容れ物（workspace→`AnalysisResult`・主の名前・
  configのlabel・飛ばしたworkspace）へ束ねる。単一workspaceの組織は今の`AnalysisResult`が
  そのまま流れる。CLIの結線と`--allow-missing-workspace`はStep 47（出力が揃ってから開く）
- 入れ子レイアウトでは`members-info.csv`を組織直下から読む（workspaceのディレクトリではない）
- κの解決を「アカウント→κ」の1関数に閉じる（主はmembers-infoの列、副はworkspaceの既定値。
  §26.3）
- `fixed_seat`を書いたworkspaceのアカウントはV1判定の対象外ステータスにする
- 対象月にデータが無いworkspaceの扱い（§26.6。まだ始まっていないworkspaceは飛ばす。始まって
  いるのに無ければエラー。許可されたworkspaceは需要0として分析する）
- configと発見名の不一致・主の不在は分析の経路でもエラーにする（doctorだけに頼らない）

受け入れ条件:

- 従来レイアウトと、`workspaces`を1つだけ書いた組織の成果物がバイト一致
- 2 workspaceの各アカウント行の集計値（需要・実課金・トークン・product構成）が、その
  workspaceを単独の組織として実行した結果と一致（判定の合算はStep 45で入れる）
- 副が始まる前の月の再生成が止まらない。始まった後の欠月はフラグなしで止まる

#### Step 45: 人の層と判定（合算・払い出し・継続）

依存:

- Step 44

対象:

- `src/seat_analyzer/analyze/persons.py`（新設・純粋関数）
- `src/seat_analyzer/analyze/pipeline.py`・`src/seat_analyzer/analyze/workspaces.py`
  （合算需要の結線と、払い出した月の解決）
- `src/seat_analyzer/analyze/credits.py`（上限到達の述語）
- `src/seat_analyzer/report/format.py`（部署別・チーム別を人単位で数える）
- `tests/test_analyze.py`・`tests/test_persons.py`

実装:

- §26.4の人の行
- 複数アカウント保有者の主の行を合算需要で判定し直す（§26.4の両軸の規則。履歴の各月も合算）
- §26.5の払い出し判定・継続判定・複数アカウント保有者の実課金
- 部署別・チーム別サマリを人の層から計算する（アカウント数を人数として数えない）。集計の実体は
  `report/format._group_summary_rows`で、人の表を渡せば人単位になる（`report/stats.py`の
  分布はアカウント単位の指標なので触らない）

受け入れ条件:

- 主が$100・副が$300の合成データで、主の行の推奨が合算$400で決まる（単独$100なら降格に
  なる例）
- 各判定が定義どおり（合成データで閾値の境界・上限到達の1か月成立・払い出し月の除外・
  「データ蓄積待ち」・κ無効の「判断材料なし」を含む）
- 同じ入力から常に同じ行順（emailをタイブレークにする）
- 単一workspaceの部署別・チーム別が不変

#### Step 46: V2の合算（decision-evidence）

依存:

- Step 45

対象:

- `src/seat_analyzer/decision_evidence.py`（`evaluate_org`・人ごとの履歴と完全性）
- `src/seat_analyzer/analyze/pipeline.py`（V2の材料に明細を持たせる・`add_demand`の公開）
- `src/seat_analyzer/analyze/workspaces.py`（払い出した月の保持・`has_multiple_workspaces`）
- `src/seat_analyzer/report/evidence_csv.py`
- `tests/test_decision_evidence.py`・`tests/test_analyze.py`

実装:

- §26.9。複数アカウント保有者の`SubjectHistory`を月別合算で組み、主の行1本にする。副の行は
  作らない
- Identity解決はworkspaceごと（§26.4）。合算はemailで結ぶ
- 履歴と完全性は人ごと（副の部分月は不完全月・副の欠月は打ち切り）
- `workspace`列に主の名前を書く（configの`workspaces`が2つ以上の組織だけ）

受け入れ条件:

- 主$300・副$300の合成データで、evidenceの行が1本で需要$600になる
- 副が始まる前の月は主だけの値で、合算の月と暦で連続する
- 副の欠月で降格の評価窓がずれない。副にアカウントの無い人は副の部分月・欠月の影響を受けない
- 単一workspaceのdecision-evidenceがバイト一致
- 語彙（`SeatAction`・`ReasonCode`）を増やさない

#### Step 47: 複数workspaceの出力

依存:

- Step 46

対象:

- `src/seat_analyzer/ingest.py`（`discover_org_months`）・`src/seat_analyzer/config.py`
  （`label`の重複検査）
- `src/seat_analyzer/analyze/`（`OrgAnalysisResult.nested`・`single_org_result`・
  `summarize_org`・`own_demand_users`の公開・固定シートと違う種別の警告・
  `PersonLayer.payout_workspace`・`ContinuationJudgment.evaluation_months`・種別不一致の理由の文言）
- `src/seat_analyzer/report/`（markdown・details・html・csv_out・usage_csv・format・新設の
  `spaces.py`）
- `src/seat_analyzer/templates/`（partialの見出しに表示名・新設の`workspaces.html.j2`と
  `spaces.html.j2`）・`src/seat_analyzer/prompts/`（固定シートと複数スペースの観点）
- `src/seat_analyzer/cli.py`（入れ子レイアウトの結線・`--allow-missing-workspace`・速報の扱い・
  V2を`evaluate_org`に一本化）
- `examples/`（2 workspaceの合成組織org-cと`examples/config.yaml`）・`README.md`・
  `docs/setup.md`・`.github/workflows/ci.yml`（E2Eに`--config examples/config.yaml`）・
  `CHANGELOG.md`
- `tests/golden/`・`tests/test_golden.py`・`tests/test_spaces.py`（新設）・`tests/test_cli.py`・
  `tests/test_report_split.py`・`tests/test_dashboard_shell.py`・`tests/test_config.py`・
  `tests/test_persons.py`・`tests/test_analyze.py`

実装:

- §26.6（`--allow-missing-workspace`の検証）・§26.7
- `cli._reject_nested_layout`を廃止し、`analyze`（正式）・`discuss`・`collect`は入れ子レイアウトを
  通す。対象月の決定と欠月のスキップは組織単位（入れ子は全workspaceの月の和集合）
- 出力関数は`OrgAnalysisResult`を受け取り、複数workspaceの組織でなければ唯一のworkspaceの結果で
  従来の経路を通す（従来の`AnalysisResult`もそのまま受け付ける）

受け入れ条件:

- 単一workspaceのgolden 5ケースがバイト一致（`full/summary/2026-06.md`・速報2ケースを含む）
- `full-all`にorg-cの6種（report / details / dashboard / recommendations / usage-summary /
  decision-evidence）と`summary/2026-08.md`が追加され、org-bの2026-08の成果物は不変
- org-cの成果物で合成データの期待判定（払い出し・継続・主での実課金・V1の変更推奨）が出る
- recommendations / usage-summary / decision-evidenceの`workspace`列がemailの次にあり、副が
  始まっていない月でも列がある。連結で欠けた任意列は空欄／「—」で、生成が落ちない
- dashboardの3不変条件（メールはローカル部のみ・$100以上は整数・外部通信なし）がorg-cでも成立し、
  タブ並びはoverview / actions / members / org / spaces / notes、単一workspaceでは従来の5つ。
  `dashboard.css`・`dashboard.js`は不変
- `analyze --preview`は入れ子の組織を複数組織の実行では通知して飛ばし、単一対象では止まる。
  `--allow-missing-workspace`は未知の名前・`--preview`併用で止まり、指定したworkspaceの欠月は
  需要0で完走して警告がレポートに残る。全workspaceを指定した欠月も完走する。`discuss`・`collect`は
  入れ子レイアウトを受け付ける
- 固定シートのworkspaceでは感度分析・付与候補が出ず、`label`の重複はconfigのロードで止まる
- 追加した文言は`check-text`を通し、差分は`check-text --diff`で0件
- `uv run pytest`と`uv run ruff check .`が通り、CIの全ジョブが緑

#### Step 48: 速報・doctorの人の検査・docs（v1.3.0）

依存:

- Step 47

対象:

- `src/seat_analyzer/analyze/preview.py`（任意引数`workspace`・`members_info_dir`・
  `extra_demand`と、未丸めの観測需要`own_demand`）・`src/seat_analyzer/analyze/workspaces.py`
  （`OrgPreviewResult`・`preview_org`・`preview_days`。発見と設定の突き合わせを正式分析と共用）・
  `src/seat_analyzer/analyze/persons.py`（`preview_persons`）
- `src/seat_analyzer/report/`（markdown・html・spaces・text）・`src/seat_analyzer/templates/`
  （preview-dashboardのworkspaceごとの節・credit-reachの見出しに表示名・新設の
  `preview-workspaces.html.j2`と`preview-persons.html.j2`）・
  `src/seat_analyzer/prompts/aspects-preview.md`
- `src/seat_analyzer/cli.py`（速報の入れ子レイアウトの拒否を外す・観測日数の決定・
  `discuss --preview`）
- `src/seat_analyzer/data_quality.py`・`src/seat_analyzer/domain.py`（§26.8の人の検査と
  `MISSING_HISTORY_MONTH`の規則）
- `docs/usage.md`・`docs/reference.md`・`docs/setup.md`・`docs/README.md`・`README.md`・
  `CHANGELOG.md`
- `tests/golden/`（新ケース`preview-multi`）・`tests/test_golden.py`・`tests/test_preview.py`・
  `tests/test_cli.py`・`tests/test_data_quality.py`

実装:

- §26.7の速報。`preview()`は1 workspace分のまま任意引数を足し、組織単位は`preview_org`が束ねる
  （正式分析の`analyze`と`analyze_org`と同じ分担）。出力関数は複数workspaceの組織でなければ
  唯一のworkspaceの結果で従来の経路を通す
- §26.8の人の検査と`MISSING_HISTORY_MONTH`の規則と、まだ始まっていないworkspaceの対象月の
  検査（§26.8の「対象月」）
- 入力レイアウト・config・レポートの節・CSVの列・移行手順（既存の3ディレクトリを主workspaceへ
  移す。`members-info.csv`と`github-cache/`は動かさない）を文書化する。`fixed_seat`は副スペース
  （複数workspaceの組織）のための設定と書く

受け入れ条件:

- 単一workspaceの速報・正式分析のgolden 5ケースがバイト一致
- 新ケース`preview-multi`（org-cの2026-08）で、スペース別・連結した一次判断テーブル・人別の
  需要・workspaceごとの節がMarkdownとHTMLの両方に出て、主の行の需要は合算値・観測系は各
  アカウント自身の需要
- 固定シートのworkspaceのStandard / Premiumは「対象外（固定シート）」、観測需要がほぼゼロなら
  「遊休候補」、付与候補は出ない
- 移行手順どおりに動かした組織（`workspaces`が1つ）の速報・正式分析の出力が移行前とバイト一致
- 観測日数の食い違い・始まったworkspaceの欠月・`--allow-missing-workspace`との併用で止まり、
  未開始のworkspaceは警告して飛ぶ。`discuss --preview`が入れ子レイアウトを受け付ける
- doctorの3つの検査が定義どおりに出て、`MISSING_HISTORY_MONTH`が最古のspendの月より前の月に
  出ない。読めないmembers-infoがあっても`--format json`が出力される。まだ始まっていない
  workspaceは過去の月の検査でerrorにならない
- 追加した文言は`check-text`を通し、差分は`check-text --diff`で0件
- `uv run pytest`と`uv run ruff check .`が通り、CIの全ジョブが緑

#### Step 49: 切替の観測（Phase 2）

依存:

- Step 48
- 両workspaceの週次エクスポートが同じ日に揃っていること

対象:

- `src/seat_analyzer/analyze/midmonth.py`
- `src/seat_analyzer/analyze/persons.py`・`src/seat_analyzer/analyze/__init__.py`
- `src/seat_analyzer/report/spaces.py`・`src/seat_analyzer/report/details.py`
- `src/seat_analyzer/prompts/aspects-full.md`・`tests/test_switch_observation.py`
- `docs/reference.md`・`docs/roadmap/implementation-design.md`・
  `docs/roadmap/implementation-status.md`・`CHANGELOG.md`

実装:

- 複数workspaceの組織のdetailsに「主と副の切り替え（副の表示名）」を副ごとに出す。
  並びは他のworkspaceごとの節と同じく、主の次は名前の昇順。
  対象は主にアカウントがあり、その副にシートのあるアカウント（`holds_seat`）を持つ人。
  主のシート種別は問わず、対象月の利用がゼロでも行を出す。副で未割当だけの人は除く
- 対象月の月初開始の累積スペンドについて、主と副の両方で1本ずつある終了日だけを使う。
  同じ終了日のファイルが複数ある日付は、そのworkspaceでは使わない。共通日が2つ未満、
  または対象者がいない副の節は出さない
- 最初は月初から最初の共通日まで、以後は前の共通日から次の共通日までを区間とする。
  表記は`月初→〜MM-DD`・`〜MM-DD→〜MM-DD`。日数は最初が`FilePeriod.days`、以後が終了日の差
- 列はユーザ・区間・日数・主のAPI換算需要の増分・副のAPI換算需要の増分・主の実課金の増分。
  需要は`computed_cost_usd`（`cost_basis`に依らない）、実課金は`net_spend`のemail別合計。
  emailの行が無い時点と実課金列が無い場合は0とし、丸め前の累積の差を取ってから小数2桁に
  丸め、既存の`_fmt_delta`で表示する。行はemail昇順・区間の古い順
- `_compute_snapshot_diff`の追加キーで終了日・日数・丸め前の累積を渡し、`persons.py`の
  純粋関数で突き合わせて`PersonLayer`に持たせる。既存キー・描画・警告は変えない
- detailsではworkspaceごとの月中推移の直後、Claude Code活動の前に置く。使った時点と、
  使わなかった時点の表示名・理由を表の前に示し、読み方を表の後に置く。
  見出し・描画・読み方は`spaces.py`にまとめる
- 節のある月だけreportの「複数スペースの利用」にdetailsへの案内を1行足す。
  考察プロンプトには継続判定の補助材料として読む指示を足す。dashboard・速報・CSVには出さず、
  新しいconfigキーや閾値は作らない

受け入れ条件:

- 終端日が一致しない区間は出さない（近い日付で無理に合わせない）
- 単一workspaceの出力が不変
- 同じ両端日の区間の増分は既存の月中の利用推移と一致する
- 同じ終了日のファイルが複数ある日付は使わない

### Track 10: Claude Code の習熟度の時系列

仕様は§27。読者は CTO と開発責任者で、組織の担当者へ共有する成果物とは別に出す。Step 57 は
調査（コードを変えない）。Step 58〜59 は GitHub のコミット単位の帰属で OpenTelemetry に依存せず、
Step 60〜61 で OpenTelemetry の取得と網羅率、Step 62〜65 でパネル・段階・検知・dashboard。
どの Step で止めても手前までが動く。後続候補（番号は着手時に振る）: コミット SHA による PR と
セッションの結合、週次の粒度（§27.10）。

#### Step 57: BigQuery の実態確認（調査）

依存:

- なし

対象:

- `docs/roadmap/implementation-status.md`（検証記録）
- ワークスペースの`config.yaml`（project・dataset の名前。公開しない）

実装:

- `bq` CLI で`INFORMATION_SCHEMA`と`--dry_run`だけを使い、テーブルの形（collector から BigQuery
  までの経路）、信号（メトリクス・イベント）、属性（`user.email`・`organization.id`・`vcs.*`・
  `app.version`）、content logging の有無、蓄積の開始日と保持期間、導入している組織、1 か月の
  読み取り量と費用を確かめる
- 読む前に dry-run の読み取り量を提示して許可を得る。本文・引数の列は SELECT しない

受け入れ条件:

- §27.3 の SQL テンプレートを書くのに要る事実（テーブル名・列名・属性の有無・パーティション列）が
  検証記録にある（一般化して書く。project・dataset・組織名は書かない）
- 網羅率（§27.4）の初期値の見当（導入した組織で、spend の Claude Code 需要に対する OpenTelemetry の
  費用の比）
- `otel.maximum_bytes_billed`の既定の根拠

今回は行わない:

- コードの変更

#### Step 58: PR キャッシュにコミットを保存

依存:

- Step 35

対象:

- `src/seat_analyzer/github_collect.py`
- `tests/test_github_collect.py`
- `docs/reference.md`

実装:

- 検索の GraphQL に`commits`を足す（§7.6・§15.7）。総数が 1 回の上限を超える PR は PR ごとに続きを
  辿る
- schema 2。schema 1 のキャッシュは読め、`collect`は全窓を取り直して schema 2 で書く

受け入れ条件:

- コミットのメッセージ・ファイル一覧・作者の名前を問い合わせに書かない（禁止フィールドのテスト）
- 1回で取り切れない PR は PR 単位で続きを辿る（GitHub の上限の250件まで）
- 読めない要素があればページ全体を読めなかった扱い（既存の規則）
- schema 1 のキャッシュを読んだ`collect`は全窓を取り直し、schema 2 の完了済みの窓は問い合わせない

今回は行わない:

- 集計・出力

#### Step 59: コミットの作者で行数を集計

依存:

- Step 58

対象:

- `src/seat_analyzer/github_metrics.py`
- `src/seat_analyzer/report/github_csv.py`
- `src/seat_analyzer/default-config.yaml`（`github.large_commit_lines`）
- `tests/test_github_metrics.py`・`tests/test_github_csv.py`
- `docs/reference.md`

実装:

- §15.7

受け入れ条件:

- 既存の列（PR 数・lead time・内訳）はバイト一致
- 作者の解決は login → メール → `unresolved`の順で、メールは小文字で突き合わせる
- 上限を超えるコミットは合計から外れ、件数に残る
- schema 1 のキャッシュでは新しい列が空欄で`GH_COMMITS_MISSING`

今回は行わない:

- パネル

#### Step 60: collect --source otel

依存:

- Step 57

対象:

- `src/seat_analyzer/otel_collect.py`（新規・層 15）
- `src/seat_analyzer/sql/`（SQL テンプレート。`.py`でない同梱物）
- `src/seat_analyzer/cli.py`
- `src/seat_analyzer/config.py`・`src/seat_analyzer/default-config.yaml`
- `tests/test_otel_collect.py`
- `docs/setup.md`・`docs/usage.md`・`docs/reference.md`

実装:

- §27.2・§27.3・§7.7

受け入れ条件:

- 本文・引数・パスの列をテンプレートに書かない（テストがテンプレートを検査する）
- dry-run の見積もりが上限を超えたら実行しない
- `bq`の出力は fixture で検証し、unit test はネットワークに出ない
- 同じ入力から同じ JSON（行の順序を固定）
- 認証なし・権限なし・途中失敗で部分結果を完全な結果として保存しない
- wheel に`sql/`が入る

今回は行わない:

- doctor・指標

#### Step 61: OpenTelemetry の網羅率

依存:

- Step 60

対象:

- `src/seat_analyzer/data_quality.py`
- `src/seat_analyzer/cli.py`
- `tests/test_data_quality.py`
- `docs/reference.md`

実装:

- §27.4

受け入れ条件:

- 比率の閾値は config
- `otel`の無い組織では issue が出ない
- 網羅率の低い人 × 月を後続が参照できる形（純粋関数の戻り値）で返す

今回は行わない:

- パネル

#### Step 62: 人 × 月パネル

依存:

- Step 59
- Step 61

対象:

- `src/seat_analyzer/maturity.py`（新規・層 25）
- `src/seat_analyzer/report/maturity_csv.py`（新規）
- `src/seat_analyzer/report/naming.py`
- `src/seat_analyzer/cli.py`
- `src/seat_analyzer/default-config.yaml`（`maturity.enabled`）
- `tests/test_maturity.py`・`tests/test_maturity_csv.py`
- `docs/reference.md`

実装:

- §27.5・§27.8（段階の列はまだ出さない）

受け入れ条件:

- `maturity.enabled: false`で既存の成果物がバイト一致
- GitHub・OpenTelemetry の無い組織でも書ける（該当の列が空欄）
- 網羅率の低い人 × 月は OpenTelemetry 由来の列が空欄
- 行はメール昇順・月昇順で、件数の多い順に並べない
- 他組織の禁止語の収集（leakcheck）の対象になる

今回は行わない:

- 段階・dashboard

#### Step 63: 習熟の段階

依存:

- Step 62

対象:

- `src/seat_analyzer/maturity.py`
- `src/seat_analyzer/default-config.yaml`（`maturity.stages`）
- `src/seat_analyzer/report/maturity_csv.py`
- `tests/test_maturity.py`
- `docs/reference.md`

実装:

- §27.6。実データの分布を見てから既定の閾値を決める（決め方は検証記録へ一般化して残す）

受け入れ条件:

- 段階はルールだけで再現できる（合成スコアを作らない）
- 網羅率の低い人 × 月は`不明`
- 組織行に段階ごとの人数と前月からの遷移

今回は行わない:

- dashboard

#### Step 64: 管轄外の検知

依存:

- Step 62

対象:

- `src/seat_analyzer/maturity.py`
- `src/seat_analyzer/otel_collect.py`（リポジトリ属性の集計）
- `tests/test_maturity.py`・`tests/test_otel_collect.py`
- `docs/reference.md`

実装:

- §27.7

受け入れ条件:

- 母集団から外さない
- owner 名・リポジトリ名を成果物に書かない
- 属性の無い組織では件数の差の方式に落ちる

今回は行わない:

- SHA 結合

#### Step 65: 管理職向け dashboard

依存:

- Step 63
- Step 64

対象:

- `src/seat_analyzer/report/maturity_html.py`（新規）
- `src/seat_analyzer/templates/maturity-dashboard.html.j2`（CSS・JS を含む。新規）
- `src/seat_analyzer/report/naming.py`
- `tests/test_maturity_html.py`
- `docs/reference.md`

実装:

- §27.8 の 4 つの図

受け入れ条件:

- 既存 dashboard が不変
- 外部通信なし・JS 無効でも全内容が読める・メールはローカル部のみ
- 出力に HTML コメントを残さない

今回は行わない:

- 週次の粒度・SHA 結合

### Track 11: V2判定の既定化

仕様は§12（分類軸を外した形で書いてある）・§16.2・§21・§22・§26.9。シートの判定を全productの
需要で行い（§3.5・§9.3）、V2の結論をreport・details・dashboard・recommendationsに出す。
Step 66〜69は既定を`v1`のままmainへ入れ（Step 69まではV2の結論を主成果物へ出す経路がCLIに
無く、Step 69でも`v2`を選んだ実行だけが変わる）、Step 70で既定を`v2`へ切り替える。V1は
`--decision-version v1`で最低3か月選べる（§22.1）。速報（`--preview`）はこのTrackでは触らない
（一次判断はallowanceモデルのまま）。後続候補（番号は着手時に振る）: 速報の一次判断を方針線へ
置き換える、V1を撤去する（§22.1）。

#### Step 66: シート判定から分類軸（Code）を外す

依存:

- Step 19

対象:

- `src/seat_analyzer/domain.py`（`SeatAction`・`ReasonCode`）
- `src/seat_analyzer/decision_v2.py`
- `src/seat_analyzer/default-config.yaml`・`src/seat_analyzer/config.py`（削除するキーの検証）
- `src/seat_analyzer/decision_evidence.py`・`src/seat_analyzer/report/evidence_csv.py`（列）
- `src/seat_analyzer/analyze/pipeline.py`（使われなくなるV2の材料の削除）
- `src/seat_analyzer/cli.py`（V2の内訳の表示ラベル表から`REVIEW_ASSIGNMENT`を外す）
- `tests/test_decision_v2.py`・`tests/test_decision_evidence.py`・`tests/test_hardening.py`・
  `tests/test_analyze.py`・`tests/test_cli.py`・`tests/golden/full-all/`（decision-evidenceの再生成）
- `docs/reference.md`（decision-evidenceの列）

`cli.py`・`config.py`の変更は語彙と設定キーの削除に伴う参照側の数行で、語彙と同じStepで行う
（Enumの参照が残るとモジュールの読み込みで失敗し、検証が残ると既定設定のロードで失敗する）。

実装:

- 昇格（§12.4）: 費用の軸が成立した候補をそのまま`UPGRADE_TO_PREMIUM`にする。分類軸と
  `REVIEW_ASSIGNMENT`を削除する
- 降格（§12.5）: Code需要のゲートとsupplementary高の分岐を削除する。窓の全月で実課金なし・
  全product需要が方針線未満なら`DOWNGRADE_TO_STANDARD`
- 語彙: `SeatAction.REVIEW_ASSIGNMENT`と、`ReasonCode`の`ONE_MONTH_STRONG_CODE_DEMAND`・
  `SUSTAINED_LOW_CODE_DEMAND`・`HIGH_SUPPLEMENTARY_USAGE`・`REVIEW_NON_CODE_USAGE`を削除する
  （§12.2）。`DATA_CONFIDENCE_LOW`は残す（分類できない観測がrecent窓に重なるときの保留理由。
  §10.4・§12.7）
- config: `decision_v2.upgrade.min_code_demand_usd`・`decision_v2.downgrade.max_code_demand_usd`を
  既定設定と検証から削除する（利用者の設定に書いてあれば未知キーとしてロード時エラー）
- decision-evidence: `code_demand_usd`・`supplementary_high`の列を外す（判定に使わない値を材料
  として出さない。Codeの内訳はusage-summaryにある）

受け入れ条件:

- 合成データで、Code需要$0・全product需要が方針線以上のStandard（κ=0・2完全月連続）が
  `UPGRADE_TO_PREMIUM`になる
- 合成データで、全product需要が窓の全月で方針線未満のPremiumは、Code需要の大小によらず
  `DOWNGRADE_TO_STANDARD`になる
- `review_assignment`がどの経路でも出ない（語彙にも無い）
- 分類できない観測（§10.4）がrecent窓に重なるとき、従来どおり`DATA_CONFIDENCE_LOW`で`OBSERVE`になる
- `policy_stability`は経済軸だけで決まり、Code需要の値を変えても変わらない
- 既定（`v1`）と`--preview`の成果物がバイト一致（golden）。`full-all`のdecision-evidenceだけが
  列と理由コードの変更で変わり、意図どおりか目視して再生成する

今回は行わない:

- V2の結論を主成果物へ出すこと（Step 67〜69）
- 速報

#### Step 67: V2の結論を写す処理（apply）と設定キー

依存:

- Step 66

対象:

- `src/seat_analyzer/config.py`・`src/seat_analyzer/default-config.yaml`（`decision.version`の追加と
  `decision_v2.enabled`の廃止）
- `src/seat_analyzer/decision_evidence.py`（`apply`: V2の結論をusers・summary・付与候補・人の層へ
  写す純粋な関数）
- `src/seat_analyzer/analyze/pipeline.py`（`analyze(..., decision_version=)`: 判定窓に基づく警告の
  抑止・summaryの`decision_version`・新しい判定ラベル「対象外（副スペース）」の定数）
- `src/seat_analyzer/analyze/workspaces.py`（`analyze_org` の引数の受け渡し）・
  `src/seat_analyzer/analyze/__init__.py`（公開名）・`src/seat_analyzer/decision_v2.py`（docstring）
- `src/seat_analyzer/cli.py`（decision-evidenceを併記するかの判断を`decision_v2.enabled`から
  `decision.version`へ移す。挙動は従来と同じ）
- `src/seat_analyzer/data_quality.py`（`doctor`の部分月・欠月の説明を`decision.version`で分ける）
- `tests/test_decision_evidence.py`・`tests/test_analyze.py`・`tests/test_config.py`・`tests/test_cli.py`・
  `tests/test_data_quality.py`・`tests/test_decision_v2.py`・`tests/test_hardening.py`
- `README.md`・`docs/usage.md`・`docs/reference.md`・`CHANGELOG.md`

実装:

- 設定キー`decision.version: v1 | v2`を追加する。このStepの既定は`v1`。`decision_v2.enabled`は
  未知キーとしてロード時エラーにし、メッセージで`decision.version`へ案内する。CLIは
  `--decision-version` > `decision.version` > 既定の順で版を決め、このStepでは`v2`のときに
  decision-evidenceを併記するだけ（従来の`enabled: true`と同じ挙動。主成果物をV2にする結線は
  Step 69）
- `apply(org_result, rows, cfg) -> OrgAnalysisResult`: V2の判定行（`evaluate_org`の出力）を
  アカウントの行へ写し、summary・付与候補・人の層を組み直して返す。CLIからはまだ呼ばない。
  `analyze_org(..., decision_version="v2")`で分析した結果だけを受け付ける
  - `status`: 昇格・降格 → 変更推奨、`OBSERVE` → 要観察、`NO_DECISION`のうち`PARTIAL_MONTH`・
    `INSUFFICIENT_HISTORY` → 要観察（データ蓄積待ち）、`IDENTITY_CONFLICT` → 要観察、
    `CURRENT_SEAT_UNKNOWN` → シート不明、`EXCLUDED` → 対象外（シート未割当／固定シート）。
    credit側だけに作業がある人（`ENABLE_WITH_CAP`・`REVIEW`）のシートの判定は現状維持
  - `recommended_seat`: 昇格→`premium`、降格→`standard`、それ以外は現シート
  - `monthly_saving_usd`: シート差額ベース。降格は+差額、昇格は実課金−差額（実課金ゼロなら
    −差額。κの記入によらずこの式）、現状維持は0、判定しない行は空。変更先の従量課金は
    見積もらない（§12.3）
  - `confidence`: `policy_stability`の3/2/1を高/中/低、Noneは`—`
  - `reason`: 理由コードの日本語ラベルを`; `で連結した新しい列。ラベルの表はapplyが唯一の源で、
    decision-evidenceの`reason_codes`と順も対応する
  - V1だけの列（`cost_if_standard_usd`・`cost_if_premium_usd`・`rec_low`・`rec_high`・
    `cap_suspected`）はusersから落とす
  - summary: `n_change_recommended`・`n_watching`・`est_monthly_saving_usd`を再計算し、
    `decision_version`を持たせる。`n_cap_suspected`は持たない
  - 付与候補: `credit_action`が`ENABLE_WITH_CAP`の人（需要・推奨初期上限）。`REVIEW`の人は
    別の一覧（設定確認。`credit_reviews`）へ載せる。allowanceの超過見込みは使わない
  - 人の層（§26.5）はapplyの後に組み直す（人の`status`はアカウントの新しい`status`から決まる）
- 複数workspace（§26.9）: 主のアカウントは`evaluate_org`（合算）の行を写す。副のStandard /
  Premiumのアカウントは V2の判定を持たないので、`fixed_seat`があれば従来どおり「対象外（固定
  シート）」、無ければ新しいラベル「対象外（副スペース）」にする（種別の判定を行わず、続けるか
  戻すかは§26.5の継続判定が担う）。副の`unknown`（membersに居ないのにspendに現れた）は
  「シート不明」、`unassigned`は「対象外（シート未割当）」のまま。副の行はdecision-evidenceに
  足さない
- `analyze(..., decision_version=)`（既定`v1`）: `v2`では判定窓に基づく3つの警告（主の欠月・
  副の欠け・判定窓のあいだの加入とシート変更）を出さず、部分月の警告は観測の事実だけにする
  （「変更推奨を出していません」はV1の規則の説明なので、V2の結論と並べない）。summaryには
  `decision_version`と、昇格・降格の`min_complete_months`（表示の注記が設定値を使うため）を
  持たせる
- `doctor`（`data_quality.inspect_input`）の部分月・欠月の説明は設定の`decision.version`で分ける
  （`doctor`には`--decision-version`が無い）。`v2`では「対象月が部分月なら判定を保留し、過去の
  部分月は完全月に数えない」「欠月より前の月は履歴に入らない」とし、V1の判定窓の説明は`v1`の
  ときだけ出す。検出する月は版によらず同じで、説明の文だけを分ける

受け入れ条件:

- 既定（`v1`）と`--decision-version v2`の成果物がどちらもバイト一致（golden。applyはまだ結線
  しないので`full-all`も変わらない）
- applyの単体テストで、V2の判定行ごとに`status`・`recommended_seat`・`monthly_saving_usd`・
  `confidence`・`reason`が上の対応表どおりになる（昇格→変更推奨+`premium`、`OBSERVE`→要観察、
  `PARTIAL_MONTH`→要観察（データ蓄積待ち）、κ=0で実課金$150の昇格→+$50 など）
- applyの出力のsummary（変更推奨の人数・削減見込み・要観察）がusersの行から数えた値と一致し、
  `n_cap_suspected`を持たない
- 付与候補に`ENABLE_WITH_CAP`の人だけが載り、推奨初期上限が`suggested_credit_cap_usd`と同じ
- 複数workspaceの合成データで、人の層の`status`が主の行のV2の結論から決まり、`fixed_seat`の副の
  Standard / Premiumは対象外（固定シート）、`fixed_seat`の無い副のStandard / Premiumは対象外
  （副スペース）になる。副にだけ現れる`unknown`はシート不明、副の`unassigned`は対象外（シート
  未割当）のまま
- `decision_version="v2"`の`analyze`が判定窓に基づく警告を出さず、`v1`の警告は従来どおり
- `decision.version: v2`の設定で`doctor`の部分月・欠月の説明がV2の文になり、`v1`では従来の文のまま
- `decision_v2.enabled`を書いた設定はロード時エラー（移行の案内つき）。`decision.version: v2`は
  従来の`enabled: true`と同じ成果物を書く

今回は行わない:

- 表示の版対応（Step 68）
- CLIの結線（Step 69）

#### Step 68: 表示をV2に合わせる

依存:

- Step 67

対象:

- `src/seat_analyzer/report/text.py`・`markdown.py`・`details.py`・`html.py`・`spaces.py`
- `src/seat_analyzer/templates/dashboard.html.j2`・`partials/grant.html.j2`・`partials/snapshot.html.j2`
- `src/seat_analyzer/report/format.py`（版の判定と apply 済みの検査）・
  `src/seat_analyzer/report/__init__.py`（書き込み前の検査）
- `src/seat_analyzer/analyze/pipeline.py`（V2 の summary の方針線・月中の警告への版の受け渡し）・
  `src/seat_analyzer/analyze/midmonth.py`（月中のシート変更の警告の版分け）
- `src/seat_analyzer/decision_v2.py`（確度の幅の定数の公開名）・
  `src/seat_analyzer/decision_evidence.py`（連続月の関数の公開名）
- `tests/test_report_v2.py`・`tests/test_partials.py`
- `src/seat_analyzer/prompts/aspects-full.md`
- 表示のテスト（applyの出力を`report.write_all`に通して確かめる。CLIの結線はまだ無い）

実装:

- summaryの`decision_version`で分岐する。`v2`のとき、ユーザ表（report・details・dashboardの
  推奨一覧とユーザ別）にStandard時・Premium時・⚠上限フラグ・感度分析を出さず、確度の次に理由の
  列を足す（dashboard は判定のセルの次）
- 凡例・注意事項・「前提と注意」を版ごとの文にする（確度＝方針線を±$100ずらしても結論が同じか、
  判定の条件）。固定文言は`text.py`の`_TEXT`に版ごとのキーで持つ
- 削減見込み（`monthly_saving_usd`・サマリのKPI・人別の表・組織横断サマリ）は列名を変えず、`v2`の
  凡例と注記に「シート差額ベースの比較額で、変更後の従量課金を含まない。負は費用増」と書く
- サマリの「上限到達疑い（Standard）」行は`v2`で出さない。「判定に使用した月」は履歴の月と
  「判定に要る完全月数の下限: 昇格 N・降格 M」の注記にする（N・Mはsummaryに載せた設定値。
  既定は1と2）。履歴は組織に共通のもので、加入より前の月などは人ごとに除く
- 付与候補の節を、上限つき付与の一覧（需要・推奨初期上限）と設定確認の一覧の2段にする。
  dashboard のカードは V2 では固定シートでない主 workspace にだけ出す
- apply を通していない V2 の結果は、`write_all`・`write_org_summary` がディレクトリ作成と
  ファイル書き込みの前に止める
- 月中のシート変更の警告は V2 では検出した事実だけを伝える
- 月中の利用推移（§8）の停止疑いは観測なので残し、`v2`では「実効込み量の実測候補」の語を出さない
  （上限で止まった可能性に本人確認が要る旨は従来どおり）
- 複数workspaceの人別の表（`spaces.py`）の凡例を版ごとにする（「アカウントのV1判定」「上限到達
  疑い」の文言）
- 考察の観点（`aspects-full.md`）は版を受け取らないので、両方の版に通る書き方にする: 上限到達疑いの
  観点に「資料にあれば」を付け、追加クレジットの付与候補・設定確認の観点と、理由の列を根拠に
  変更推奨の妥当性を評価する観点を「資料にあれば」で足す
- HTML出力にコメントを残さない・外部通信なし・メールはローカル部のみ・JS無効でも読める、は従来
  どおり

受け入れ条件:

- applyの出力を`write_all`に通したreport・details・dashboardに、込み利用量の推定値（allowanceの
  3シナリオ）に基づく表示（Standard時・Premium時・⚠上限?・3シナリオの確度・超過見込み・
  感度分析）が現れない
- 理由の列の文言が、decision-evidenceの`reason_codes`の順と1対1で対応する
- 複数workspaceの合成データで、人別の表の凡例が`v2`の文になり、「対象外（副スペース）」の行が
  表示順・バッジを持つ
- 既定（`v1`）の成果物がバイト一致（golden不変）

今回は行わない:

- 速報の表示
- CLIの結線（Step 69）

#### Step 69: CLIの結線

依存:

- Step 68

対象:

- `src/seat_analyzer/cli.py`（実行順・残存evidenceの通知・結果の表示）
- `src/seat_analyzer/analyze/workspaces.py`（`summarize_org`が`decision_version`を運び、
  `n_cap_suspected`の合計は`v1`のときだけ持つ）
- `tests/test_cli.py`・`tests/test_spaces.py`・`tests/test_golden.py`・`tests/golden/`
  （`full-all-v1`と`full-v2`の新設・`full-all`の再生成）
- `docs/reference.md`・`docs/usage.md`（`--decision-version v2`の意味の更新）
- `src/seat_analyzer/default-config.yaml`（版の説明）・`README.md`・`CHANGELOG.md`
- `docs/roadmap/implementation-design.md`・`docs/roadmap/implementation-status.md`

実装:

- 先に`full-all-v1`（`full-all`と同じ月・組織で`--decision-version v1`を明示。decision-evidenceを
  含まない）をgoldenに加える。この時点の`v1`の主成果物はTrack 11の前と同じなので、複数workspaceの
  V1の基準はここで固定する（Step 70で作ると、`full-all`の主成果物がすでにV2になっていて基準が
  残らない）
- `analyze_org`に判定の版を渡す。`v2`のとき、analyze → SeatChanges → `evaluate_org` → `apply` →
  主成果物（report・details・dashboard・recommendations・usage-summary）→ decision-evidence の
  順で実行する。`v1`は従来どおり
  （applyを通らず、decision-evidenceを書かない）。既定は`v1`のまま
- `_stale_evidence_notices`（`v1`の実行で前回のdecision-evidenceが残っているとき）の文言を、
  「主成果物とdecision-evidenceの版が違う。`--decision-version v2`で再実行すると主成果物もV2に
  なる。比較用は別の`--output-dir`へ」に変える
- `summarize_org`は`decision_version`を返し、`v2`ではworkspaceの行・合計ともに
  `n_cap_suspected`を持たない（分析を飛ばしたworkspaceの行も同じ）
- 実行時の「上限到達疑い」は`v2`では単一・複数workspaceともに出さず、V2の内訳
  （既存の`_print_decision_evidence`）を出す

受け入れ条件:

- 既定（`v1`）の成果物がバイト一致（`full`・`full-snapshots`・`preview*`）
- `--decision-version v2`で、recommendationsの`status`・`recommended_seat`・`monthly_saving_usd`・
  `confidence`・`reason`がdecision-evidenceの同じ行と整合する
- `v2`のreportのサマリ（変更推奨の人数・削減見込み・要観察）がrecommendationsの行から数えた
  値と一致し、組織横断サマリの件数とも一致する
- `full-all-v1`の主成果物が、結線前の`full-all`の主成果物（decision-evidenceを除く）とバイト一致する
- `--decision-version v2`のgolden（`full-v2`。org-a / org-bの2026-06）を新設し、`full-all`（複数
  workspaceを含む`v2`）を再生成して固定する
- 同じ出力先で`v2`の後に`v1`を実行すると、残存evidenceの新しい通知が出て、主成果物は`v1`になる

今回は行わない:

- 既定の切り替え（Step 70）

#### Step 70: 既定をV2へ切り替える

依存:

- Step 69

対象:

- `src/seat_analyzer/default-config.yaml`（`decision.version: v2`）
- `tests/golden/`（`full`・`full-snapshots`を`v2`で再生成し、`full-v2`は`full`へ統合する。
  切り替え前の`full`・`full-snapshots`を`full-v1`・`full-snapshots-v1`として残す。`full-all-v1`は
  Step 69で作ってある）
- `README.md`・`docs/usage.md`・`docs/reference.md`・`CHANGELOG.md`
- `docs/roadmap/implementation-design.md`（§22の状態）

実装:

- 既定を`v2`にする。`--decision-version v1`と`decision.version: v1`で従来の判定に戻せる
- V1の不変を固定するgoldenを3つ持つ（`--decision-version v1`を明示。単一workspace・月中差分・
  複数workspaceの3つの形）。`full-v1`・`full-snapshots-v1`は切り替え前の`full`・`full-snapshots`を
  名前を変えて残したもので、再生成しない
- docs: 判定ロジック概要をV2を主にして書き直し、V1は「`--decision-version v1`」の節に移す。
  recommendations・decision-evidenceの列、成果物の一覧、usageの手順。CHANGELOGと
  reference.mdに移行の節を書く（削除した設定キー3つ・`decision.version`の選び方・
  decision-evidenceから外した2列・既存の月を再生成すると考察は引き継がれるので判定が変わった月は
  `--force-discussion`か手で書き直すこと）
- 版はMINOR（判定と成果物が変わる）

受け入れ条件:

- 引数を省略した`analyze`がV2の判定で成果物を書き、decision-evidenceを併記する
- `--decision-version v1`の成果物が切り替え前とバイト一致（`full-v1`・`full-snapshots-v1`・
  `full-all-v1`）
- `--preview`の成果物が不変
- 設定例・手順に`decision_v2.enabled`・`min_code_demand_usd`・`max_code_demand_usd`が残らず、
  移行の節にその3つが削除したキーとして書いてある

今回は行わない:

- V1の撤去（既定化から最低3か月後。allowance・上限フラグ・付与候補の旧実装の削除と、速報の
  一次判断の置き換えを含む）

## 19. Milestone

### Milestone A: Core data

Step 1〜8（8F・8G・8E・8Dを含む。この4つはこの順で行う）

- stable IDの準備
- Data Doctor
- Code/全product分離
- 統計参考値の掲載
- report.mdの考察メイン化とdetails.mdの分離
- dashboardの再設計とproduct軸の掲載

### Milestone B: Independent audit

Step 9〜24

- シート変更履歴
- V2非対称判定
- credit比較
- 2/4/8週評価
- review.md

ここで実用開始する。

### Milestone C: Collection automation

Step 31・50〜54

- 専用プロファイルのChromeと同梱拡張機能
- 公式のエクスポートCSV
- 2モード（当月・前月）
- ログインセッションの確認（`--check-login`）
- credit入力補助

### Milestone D: GitHub reference

Step 32〜39

- Organization自動発見
- PR数
- lead time

### Milestone E: Billing and UI

Step 40〜42

- 購入席
- 割当との分離
- Dashboard

### Milestone F: Multi-workspace

Step 43〜49

- workspaceの入れ子レイアウト
- アカウント層と人の層の2層集計（利活用は合算・副の活用は分けて見る）
- 副の払い出し判定・継続判定・複数アカウント保有者の実課金
- V2の合算
- 切替の観測（Phase 2）

### Milestone G: Maturity

Step 57〜65

- GitHub のコミット単位の帰属
- OpenTelemetry（BigQuery）の取得と網羅率
- 人 × 月パネル・習熟の段階・管轄外の検知
- 管理職向け dashboard

### Milestone H: V2 default

Step 66〜70

- シート判定から分類軸（Code）を外す
- V2 の結論を写す処理（apply）・表示の版対応・CLI の結線
- 既定を V2 へ（V1 は `--decision-version v1` で最低 3 か月）

### Future

- OpenTelemetry のコミット SHA による結合と週次の粒度（§27.10）
- Jira
- CI/CD
- DORA

## 20. テスト戦略

### 20.1 全Step共通

```sh
uv run ruff check .
uv run pytest
```

- 既存テストをすべて通す
- 実データをfixtureへ含めない
- 外部ネットワークなしでunit testを実行する
- `gh`とbrowserはfixture/mockで検証する
- live smoke testは手動・明示実行だけ

### 20.2 必須不変条件

- `--decision-version v1`の判定と成果物は、V2導入（Track 4）・既定化（Track 11）の前後で
  変わらない。判定窓による保留（§26.4）は別
- partial monthから変更推奨を出さない
- upgradeは1完全月で候補になり得る
- downgradeは2完全月未満で候補にならない
- 需要の中身（product）をシート判定の条件にしない（Track 11以降）
- シート変更eventは重複しない
- 未来の推奨を過去変更へ照合しない
- follow-up coverage不足はnot evaluable
- GitHubなしでも分析できる
- browserなしでも分析できる
- GitHub本文・コードを保存しない
- browser-assisted取得はエクスポート以外の管理画面の操作をしない（シート・creditの設定変更を行わない）
- 不明値を0として扱わない
- OpenTelemetryなしでも分析できる
- テレメトリが届いていない人を未使用と扱わない（網羅率で区別する）
- 習熟の段階は合成スコアではなく、configのルールで再現できる
- 行数の帰属はPRの作成者ではなくコミットの作者

### 20.3 Security

- CSV Formula Injection
- org path traversal
- Cookie・token非表示
- GitHub cache禁止フィールド
- OpenTelemetry cache禁止フィールド（プロンプト・ツール引数・本文・パス・URL）
- BigQueryの読み取り量の上限（dry-runの見積もりが上限を超えたら実行しない）
- download元ファイルを変更・削除しない

## 21. Config

追加例:

```yaml
organizations:
  example:
    github_org: example-org
    # Claude CodeのOpenTelemetryをBigQueryに集めている組織だけ書く（組織ごとのopt-in。§27.2）
    otel:
      bigquery:
        project: example-project
        dataset: claude_code_otel
        metrics_table: metrics
        logs_table: logs          # イベントを集めていなければ省略
      # 絞り込みに使うclaude.aiの組織UUID。省略時はclaude_export.org_id（複数スペースは各workspace）
      organization_ids: []
    # 複数のTeamスペースを運用する組織だけ書く（§26.3）
    workspaces:
      main:
        primary: true
        label: 主スペース
        # claude.aiからのCSV取得（§14）。複数スペースの組織はworkspaceごとに書く
        claude_export:
          profile: corp
          org_id: 00000000-0000-4000-8000-000000000002
      second:
        label: 副スペース
        fixed_seat: premium
        credit_limit_default_usd: 0
        evaluation_months: 2
        claude_export:
          profile: corp
          org_id: 00000000-0000-4000-8000-000000000003
    secondary_breakeven_usd: 125.0
  example-single:
    # 単一スペースの組織は組織の直下に書く
    claude_export:
      profile: corp
      org_id: 00000000-0000-4000-8000-000000000001
      kinds: [members, spend, code]

product_policy:
  primary: ["Claude Code"]
  supplementary:
    - "Chat"
    - "Cowork"
    - "Design"
    - "Research"
    - "Code Review"
    - "Claude in Slack"
  prohibited: []

decision:
  version: v2                 # 主判定の版（v1 | v2）。CLIの--decision-versionが優先する（§16.2）。
                              # hysteresis_months・buffer_ratio・censoring_marginは既存のキーで、
                              # V1の判定と速報の注記が使う

decision_v2:
  premium_justification_usd: 450.0
  upgrade:
    min_complete_months: 1
  downgrade:
    min_complete_months: 2
  recent_seat_change_days: 28
  observed_billing_margin_usd: 20.0

follow_up:
  horizons_days: [14, 28, 56]
  min_coverage_ratio: 0.60

github:
  initial_lookback_days: 90
  exclude_archived: true
  exclude_forks: true
  exclude_templates: true
  repository_denylist: []
  large_commit_lines: 5000                  # これを超える追加行のコミットは上限つき合計から外す（§15.7）

claude_export:
  chrome_path: ""                           # 空文字 = OSごとの既定の場所を自動検出
  profiles_dir: ~/.seat-analyzer/profiles   # 専用プロファイルの置き場
  staging_dir: ~/.seat-analyzer/exports     # ブラウザがダウンロードする一時置き場
  timeout_minutes: 15                       # manifestを待つ上限
  login_warning_days: 3                     # --check-loginで「期限間近」にする残り日数

otel:
  bq_path: ""                               # 空文字 = PATHのbq
  maximum_bytes_billed: 2000000000          # 1回の問い合わせで読む量の上限（byte）。見積もりが超えたら実行しない
  coverage_warning_ratio: 0.8               # OTelの費用 ÷ spendのClaude Code需要がこれを下回る人×月を警告（§27.4）

maturity:
  enabled: false                            # 人×月パネルと段階（§27.8）。既定は不活性
  stages: {}                                # 段階のルール（§27.6）。既定はStep 63で分布を見てから決める
```

新しいセクションはすべて省略可能とする。`decision.version`の既定は`v2`（Track 11以降。
`decision_v2.enabled`はTrack 11で廃止し、書いてあればロード時エラー）。それ以外の新しい
セクションの既定は不活性。

`claude_export.profiles_dir`・`staging_dir`の相対パスは、`paths`と同じくそれを書いた設定
ファイルの置き場所が基準になる（`~`は展開し、絶対パスはそのまま使う）。パッケージの既定
（`~/.seat-analyzer/...`）は使う時点で`~`を展開する。`chrome_path`は実行ファイルの場所なので
設定ファイルの置き場所で解決せず、絶対パスか空文字（自動検出）に限る（相対パスはロード時に
エラー）。

## 22. Migration

### 22.1 V1/V2並行

1. V1をdefaultのまま維持
2. V2 evidenceだけ追加（Step 19）
3. 複数月並行出力
4. 差異をreview
5. V2 ruleを調整（Step 66: 分類軸を外す）
6. 明示承認後にdefaultをV2へ変更（Step 67〜70）
7. V1を最低3か月残す（`--decision-version v1`・`decision.version: v1`）

V1のallowanceモデルに依存する出力（込み枠推定の3scenario、⚠️上限到達疑い、追加クレジットの
付与候補、感度分析）は、`v2`では出さず、方針線に基づく判定・理由・`ENABLE_WITH_CAP`の付与候補に
置き換わる（§18 Track 11）。`v1`を指定した実行は従来の成果物だけを出し、decision-evidenceを
書かない。

V1の撤去は既定化から最低3か月後の別Step（番号は着手時に振る）。allowanceの設定・上限フラグ・
付与候補の旧実装の削除と、速報の一次判断（allowanceモデル）の方針線への置き換えを含む。

### 22.2 Rollback

- `--decision-version v1`
- `decision.version: v1`
- browser機能を無効化
- GitHub入力を無視
- 新出力を削除しても既存分析は動く

### 22.3 Read-only

通常の`analyze`と`review`は`input/`を変更しない。

入力を変更するのは明示的な`collect`だけとする。

## 23. リリース条件

### Core review

- Step 1〜24完了
- 全既存テスト成功
- V1回帰成功
- upgrade/downgradeの非対称条件成功
- シート変更の自動検出成功
- 2/4/8週coverage成功
- reviewが担当者個人を採点しない

### Browser-assisted collection release

- Step 50〜54完了
- 3回連続で全スペースのCSV取得に成功
- ログイン期限切れを安全に扱える
- シート・credit変更操作が存在しない
- 手動フォールバックが動く

### GitHub integration release

- Step 0A成功
- Organization全体を自動発見
- permission coverage表示
- metadataだけを保存
- rate limit時に部分結果を明示

### Multi-workspace release（v1.3.0）

- Step 43〜48完了
- 単一workspaceの全goldenが不変
- `workspaces`を1つだけ書いた組織が従来レイアウトとバイト一致
- 2 workspaceの合成組織で`init-org` → `analyze` → `doctor`が通る
- 実データで主workspaceの各アカウント行が単独実行と一致

### Maturity release

- Step 57〜65完了
- `otel`も`maturity.enabled`も無い組織の全goldenが不変
- OpenTelemetry cacheとmaturityの成果物に禁止フィールド（§7.7・§27.9）が無い
- 網羅率の低い人×月が段階`不明`になる
- maturityの成果物にリポジトリ名・owner名・他組織の情報が無い
- `bq`の実行がdry-runの見積もりと上限の確認を経ないと起きない

## 24. 実装依頼テンプレート

各実装は次の形式で依頼する。

```text
docs/roadmap/implementation-design.md の「Step N: <名称>」だけを実装してください。

対象:
- 設計書記載のファイル

受け入れ条件:
- 設計書記載の条件

制約:
- 次のStepを先行実装しない
- production codeは原則1〜3ファイル
- V1の出力・判定を変更しない
- 実データを参照しない
- コミットしない

検証:
- uv run ruff check .
- uv run pytest
- 対象テスト
```

設計書だけでは判断できない場合は、実装者が独自にスコープを広げず、次を報告する。

- 不明点
- 影響するStep
- 選択肢
- 推奨する最小変更

## 25. 将来拡張

### OpenTelemetry

Track 10（§27）で着手した。前提（端末設定の配布・収集endpoint・セキュリティ合意・小規模pilot）は
利用側の組織で整っていることをStep 57で確かめる。残る拡張は§27.10。

### Jira・CI/CD

使用基盤、権限、正準IDを確認してから別Trackを追加する。

### Enterprise

Teamの公式CSV・管理画面で不足が明確になった場合にのみ検討する。

### 取得の定期実行

`collect --source claude`の定期実行（日次・週次）と、その前提になるログインセッションの期限の
確認（Step 52）。

## 26. 複数workspace

同一の組織が複数のTeamスペース（claude.ai上では別のorganization）を運用する場合の仕様。
実装順はTrack 9（§18）。

### 26.1 背景と前提

- 用途: 同じ人が各スペースに1アカウントずつ持ち、片方の利用上限（5時間・1週）に当たったら
  もう片方を使う構成で、副スペースのシートの需要と追加費用を比較し、払い出し・継続の判断を
  支援する。承認者は導入先の運用に委ねる
- 公式の前提: 1つのClaudeアカウント（同じメール）は複数のTeam組織に所属でき、利用上限は
  メンバー単位でorganizationごとに持つ。利用量はStandardがProの1.25倍、Premiumが6.25倍で、
  容量あたりの単価は両シートで同じ。したがってこの運用の経済的な意味は「容量を$125刻みで
  買い足す手段を得ること」で、追加クレジットと比べると上限κで打ち切られず、消費した分の
  課金でもない固定費になる
- 用語: 組織（org）は`input/<組織名>/`＝レポート1セットの単位。workspaceはTeamスペース1つ。
  従来は両者が一致していた。レポートの読者は組織の担当者で変わらないので、組織分離の
  規則（他組織の情報を書かない）はそのまま
- 対象外: PremiumをStandard×2へ置き換える推奨は出さない。副スペースのシート種別は運用方針で
  決まる（`fixed_seat`）

### 26.2 入力レイアウト

```
input/<組織名>/
  members-info.csv          人単位（全workspace共通）。追加クレジット上限の列は主workspaceの設定
  github-cache/             組織単位（GitHubは人に紐づくので動かさない）
  <workspace名>/
    spend/
    members/
    code-analytics/
    admin/                  管理画面の写しはworkspaceごと
```

- 従来レイアウト（組織直下に`spend/`）は「workspaceが1つの組織」として今までどおり読む
- 2形式の混在（組織直下に`spend/`があり、子ディレクトリにも`spend/`がある）はエラー
- workspaceの発見は構造判定（`spend/`を持つ子ディレクトリ）。名前の規則は組織名と同じ
- configの`organizations.<組織>.workspaces`に発見した名前が全部書かれ、主が1つに決まること。
  食い違い（configにあるがディレクトリが無い・ディレクトリがあるがconfigに無い・主が0または
  2以上）はエラーにする。ディレクトリの置き忘れや綴りの違いを黙って無視しない
- `workspaces`に1つだけ書いた組織の出力は従来レイアウトとバイト一致させる。移行の検証手段に
  する

### 26.3 Config

`organizations.<組織>.workspaces.<名前>`の下に書けるキー:

- `primary`: 主workspace。ちょうど1つを`true`にする
- `label`: レポートの表示名。省略時はディレクトリ名
- `fixed_seat`: このworkspaceで払い出すシート種別が運用方針として固定されている場合に書く
  （`standard` / `premium`）。書いたworkspaceのアカウントはV1のシート損益分岐判定の対象外
  （ステータス「対象外（固定シート）」）になり、人の層の継続判定（§26.5）だけが働く。
  書かなければ従来どおりV1判定
- `credit_limit_default_usd`: そのworkspaceのアカウントの追加クレジット上限κの既定。主は
  従来どおりmembers-infoの列を読み、既定値は列が空欄のときだけ使う。副は列を読まず既定値を
  使う（列は主の設定だから）。無ければ不明
- `evaluation_months`: §26.5の判定に必要な連続月数。省略時は`decision.hysteresis_months`

`organizations.<組織>`直下に`secondary_breakeven_usd`（§26.5の損益分岐。払い出し判定の
実課金閾値と継続判定の需要閾値の両方）を置ける。省略時は副の`fixed_seat`の価格（無ければ
Premiumの価格）。

κの解決は「アカウント→κ」の1関数に閉じる。副スペースでも人ごとに上限が違う運用になったら、
`<workspace>/members-info.csv`（emailと追加クレジット上限の列だけ）で上書きできるようにする。
この拡張で呼び出し側が変わらない形にしておくが、今回は実装しない。

### 26.4 集計の2層

- アカウント層＝(email, workspace)。V1の集計・判定・ヒステリシス・上限フラグ・κ・E分布・
  月中差分・前月比・メンバー変動・Claude Code活動をworkspaceごとに従来どおり計算する。
  ヒステリシスは対象月から暦で遡る判定窓で見て、判定窓に欠月・部分月があれば、推奨が現シートと
  違う人だけを「要観察（データ蓄積待ち）」にする。主の判定に需要を足す副は、副のspendがある
  最初の月以降の判定窓の月が欠け・部分月なら、副にアカウントがあるかを見分けず組織単位で主の
  全員にこの保留が効く（副が始まる前の月は需要0としても欠けとしても数えない）。判定窓のあいだの
  加入・シート種別の変更もmembersで確かめて保留する（単一スペースの組織にも効く。固定シートの
  workspaceは対象外）。窓の始まり以前のmembersが無くても採用ファイル以前の全ファイルを見るが、
  最初に読めるmembersより前の加入・シート変更は見つけられない。条件の詳細は reference.md の
  [判定ロジック概要](../reference.md#判定ロジック概要)を参照
- 人の層＝email。全workspaceのアカウント行を束ねる。members-infoの部署・チーム・職種・備考は
  人の属性なので全アカウントに同じ値を付ける。部署別・チーム別サマリは人の層で数える
  （アカウント数を人数として数えない）
- 人の行が持つ値: 保有シート（workspace→シート種別）・シート費合計・需要合計・実課金合計・
  現状費用合計・主と副それぞれの需要と実課金・副の需要比率
- 判定に入れる需要の規則（両軸）: どのくらい利活用しているかは人の合算で見る。副を活用して
  いるかは主と副を分けて見る
  - 複数アカウント保有者の主の行は、V1判定（推奨・ヒステリシス・確度・上限フラグ・付与候補の
    方向）の需要を全workspaceの合算に置き換えて判定する。履歴の各月も合算する。実課金・κ・
    現シート・シート変更eventは主のもの（主シートの費用と設定だから）。推奨表の需要列は
    合算値で、主のみの需要は人の表と details に出す
  - 副の行は`fixed_seat`があれば対象外。無ければ従来どおり副単独の需要で判定する
  - E分布・上限到達・月中差分・メンバー変動・Claude Code活動・実課金の整合性警告は
    アカウント固有の量なのでアカウントごと
  - 1アカウントの人は合算＝単独なので、単一workspaceの組織は何も変わらない
- Identity解決（stable ID）はworkspaceごとに行う。spendの`user_id`はorganizationごとに
  振られうるので、workspaceをまたいで比較すると同一人物がconflictになる。workspace間の
  同一人物の結合はemailで行う（同じアカウントを両スペースへ招待する運用が前提）
- 実装形: 既存の`analyze()`をworkspaceごとに呼び、組織単位の容れ物へ束ねる。単一workspaceの
  組織は今の`AnalysisResult`がそのまま流れ、出力はバイト一致

### 26.5 人の層の判定

参考セクションとして出し、V1の主判定（変更推奨）は変えない。判断したい仮説は2つ。

- (1) 副を持たない人に副を払い出すべきか（主のシートを使い切っていて、副を足せばさらに
  仕事が進むか）
- (2) 副を持つ人が副を十分に活用しておらず、1アカウント運用へ戻すべきか

副を持つ＝副workspaceにシートのあるアカウント（Standard / Premium、spendにだけ現れる不明を含む）を
持つこと。未割当（管理者）は副を持たない扱いにする（需要の合算には含める）。

共通の物差し: 追加クレジットはAPI等価単価で課金される（実課金行がtokens×単価と一致する
ことは確認済み）。したがって副アカウントの需要（API換算USD）は、同じ利用を副なしで追加
クレジットで賄った場合の課金額に等しい。副のシート料との比較がそのまま損益分岐になり、
両方向の判定に同じ閾値`secondary_breakeven_usd`（既定＝副の`fixed_seat`の価格）を使う。
Premiumシートの込み容量は非公開で「容量の何%を使ったか」は測れないので、活用の度合いは
この金額で測る。
連続月数・完全月数は、対象月から暦で連続し、その workspace で全月そろった月だけを数える
（欠月・部分月で途切れる）。

1. 払い出し判定（副を持たない人。主のシートが副の`fixed_seat`と同じ種別で、主のκが有効な
   場合）
   - 候補: 対象月に主の追加クレジット上限へ到達している（容量不足が確定しているので1か月で
     足りる）、または主の実課金がbreakeven以上の月が`evaluation_months`連続（副の固定費より
     多くをクレジットに払っている）
   - 観察: 候補に当たらず主の実課金が0より大きい（損益分岐未満の月がある、または損益分岐以上の
     月がまだ必要な連続月数に達していない）
   - 不要: 主の実課金が0（込み枠で足りている。需要の大小は問わない）
   - 判断材料なし: 主の追加クレジットが無効、または有効かどうか分からない（κが未記入で実課金も
     観測されていない。κが未記入でも実課金が観測されていれば有効として判定する）。どちらも実課金が
     上限到達を語らない。主がStandardの人はV1の昇格判定が先
   - 併記する材料: 月別の実課金・上限到達の有無・需要・Code比率・LoC（あれば）。「さらに
     仕事が進むか」は、上限到達（作業が止まった事実）とCode主体の利用で読む
2. 継続判定（副にシートを持つ人）
   - 継続: 副の需要がbreakeven以上（クレジットで賄うより安い）。副の需要が主のκを超える月は
     「クレジットでは賄えなかった量」として明示する
   - 戻す候補: 副の需要がbreakeven未満の完全月が`evaluation_months`連続。削減見込み＝副の
     シート料−副の需要（戻した場合にクレジットへ回る分を差し引く）。副の需要が
     `trend.idle_usd`未満なら「遊休」と併記する
   - データ蓄積待ち: 副を持ってからの完全月が`evaluation_months`に満たない（払い出した月は
     不完全月として数えない）
   - 観察: 上のどれにも当たらない（直近の月が損益分岐未満でも、未満の完全月が
     `evaluation_months`連続していない）
   - 払い出した月は、その workspaceでspendに行がある月とmembersスナップショットに載った月の
     早い方とする。membersは対象月末に最も近いファイルが採用されるので、月末から離れた
     スナップショットは在籍の証拠にしない（完全月を少なく数える側に倒す）
3. 複数アカウント保有者の実課金（事実の一覧）: 副を持ちながら主で実課金が発生した人。副に
   切り替えずクレジットを使っている運用の逸脱か、両方の枠を使い切っている（さらに容量が要る）
   かのどちらかで、月別の副の需要と並べて読む

判定は人ごとに月次で行い、ステータス（候補 / 観察 / 不要 / 判断材料なし / 継続 / 戻す候補 /
データ蓄積待ち）と連続月数を出す。閾値をそのworkspaceのシート料から導くので、副が
Standardの運用でも同じ規則で動く。

### 26.6 対象月にworkspaceのデータが無いとき

- まだ始まっていないworkspace（対象月以前にspendが1つも無い）は警告して飛ばす。過去月の
  再生成で止まらないようにする
- 始まっているのに対象月のspendが無いworkspaceはエラー。`analyze --allow-missing-workspace <名前>`
  を付けると需要0として続行し、その旨をレポートの警告に残す（利用が無くエクスポートしなかった
  月のための逃げ道）。membersは従来の「月末に最も近いスナップショット」で解決する
- `--allow-missing-workspace`は、`--preview`との同時指定・対象組織のどのconfigの`workspaces`にも
  無い名前（綴り違い）・2つ以上の対象組織のworkspace名に一致する名前（一般名なので別の組織の
  欠月まで需要0にしうる。`--org`で1つに絞る）をエラーにする。指定のある組織は、どのworkspaceにも
  対象月のspendが無くても欠月のスキップをせず分析へ回す（全workspaceを指定した月も完走する）。
  分析できたworkspaceが1つも無い組織は「対象月のデータが無い組織」と同じ扱い

### 26.7 出力

組織ごとに1セットのまま。複数workspaceの組織（configの`workspaces`が2つ以上。副が未開始の
月・`--allow-missing-workspace`の月も同じ形）だけ形が変わり、それ以外の組織の成果物は従来と
バイト一致する。

- 表の需要は2系統に分ける。判定系（全ユーザ・シート変更推奨・推奨一覧と判定サマリ・
  recommendations）は全アカウントを主→副の順に連結し、複数アカウント保有者の主の行の需要は
  合算値のまま出す（凡例に「縦に足すと副の分が二重になる」と書く）。観測系（詳細利用状況・
  ユーザ別の棒と順位・組織内の分布・Codeと他プロダクトの需要・usage-summary）は各アカウント
  自身の需要で出す
- workspaceごとの節は、見出しの末尾に表示名を添えた同じ階層の節（例: 「前月からの変化
  （主スペース）」）を主→副の順に並べる。入れ子の小見出しにはしない
- report: サマリに人数とアカウント数の両方・組織合計・workspaceごとの追加クレジット構成と
  判定に使用した月。サマリ直下に「スペース別」表（workspace別の人数・シート内訳・シート費・
  需要・実課金・変更推奨と合計。未開始と需要0として扱ったworkspaceは注記）。前月からの変化・
  追加クレジット付与候補はworkspaceごと。シート変更推奨にスペース列。「複数スペースの利用」
  節を新設し、スペースの一覧と閾値・払い出し判定・継続判定・副を持ちながら主で実課金が発生した
  人（§26.5）と判定の読み方を置く。人の表（全員1人1行）はdetailsに置く（reportは短い実行判断の
  文書・表はdetailsという分担）
- details: 全ユーザ（判定系・スペース列）、備考と部署別・チーム別サマリは人の層から数える、
  「人別の利用」（人の表。全アカウントのinput / outputトークンの合計を含む）を新設、
  「主と副の切り替え」（副ごと。主と副で終了日が2つ以上一致する月で対象者がいる場合だけ）、
  詳細利用状況（観測系・スペース列）。分布・月中の利用推移・
  Claude Code活動・メンバー変動・Eの実測・感度分析はworkspaceごと
- dashboard: 概要のKPIは人数（添え書きにアカウント数）、KPIの直下に「スペース別」の表、
  月次推移・前月からの変化・追加クレジット構成・メンバー変動・主な増減・付与候補・ユーザ別の
  棒・月中の推移・Claude Code活動・Codeと他プロダクト・分布はworkspaceごとのカード。推奨一覧と
  詳細利用状況にスペース列。新タブ「複数スペース」（組織タブの次。人別の利用・払い出し判定・
  継続判定・主での実課金）。前提と注意に判定の読み方。CSS・JSは単一workspaceと同じものを使う
- `fixed_seat`のworkspaceでは、V1の損益分岐判定に由来する感度分析と追加クレジット付与候補を
  出さない（判定していない状態を「全員一致」「該当者なし」と見せない）。追加クレジット構成は
  事実なので出す
- recommendations / usage-summary / decision-evidence: `workspace`列（ディレクトリ名）をemailの
  次に足す（まだ始まっていないworkspaceがある月も同じ形にして、月をまたいだ突き合わせで列の形が
  変わらないようにする）。連結で欠けた任意列（code-analytics等）は空欄、Markdown・HTMLでは「—」
- 表示名（`label`。省略時はディレクトリ名）は組織内で一意（configのロードで検査）。表・見出しは
  表示名だけで示し、CSVはディレクトリ名
- 横断サマリ（`reports/summary/`）: 複数workspaceの組織のメンバー列を「人数（アカウント数）」に
  する。合計行は人数の和で、複数workspaceの組織があればアカウント数の和を添える。変更推奨の
  列はアカウント単位のまま
- 人数以外の件数（変更推奨・要観察・上限到達疑い・一覧の件数）はアカウント単位で、その旨を
  注意事項に書く
- 速報（preview / preview-dashboard）: 複数workspaceの組織は、workspaceごとに従来の一次判断を
  出して組織1セットにまとめる（それ以外の組織の速報は従来とバイト一致）。§26.4の両軸に従い、
  副を先に計算して、主の行の一次判断を全workspaceの合算需要（未丸めの観測値の和を月末ペースに
  換算したもの）で決める。主の行の集合は主のmembersと主自身のspendのままで、副にだけ居る人の
  主の行は作らない。判定系（一次判断テーブル・一次判断の内訳・付与候補・追加クレジット残額）は
  全アカウントを主→副の順に連結し、観測系（月末ペース換算需要の棒・詳細利用状況・スペース別・
  サマリの観測需要の合計）は各アカウント自身の需要で出す
  - 新設の表: サマリ直下の「スペース別」（workspace別の人数・シート内訳・シート費・観測需要・
    月末ペース換算・実課金）、一次判断テーブルのスペース列、「人別の需要（スペース合算）」
    （1人1行。保有シート・シート費・観測需要と月末ペース換算の合計・主と副の換算需要・副の比率・
    実課金・主の一次判断）。副の比率はシートのある副アカウントが無い人と、換算需要の合計が0の人は
    「—」。払い出し判定・継続判定は完全月で行うので速報には置かない
  - workspaceごとの節（見出しに表示名）: 追加クレジットの状態・追加クレジット残額・月中の利用
    推移・月中のClaude Code活動・月中のメンバー変動・付与候補。LoCの観測時点はworkspaceごとに示す
  - `fixed_seat`のworkspaceのStandard / Premiumは「対象外（固定シート）」（観測需要がほぼゼロなら
    観測として「遊休候補」のまま）。そのworkspaceでは付与候補を出さない
  - 観測日数は全workspaceで同じにする。入れ子の組織は`--days`の有無によらず、日数の分かる
    workspaceどうしが食い違えば止める（`--days`では直らないので、同じ期間のspendをそろえる）。
    `--days`を省略したときはファイル名の期間から決め、期間を読めなければ`--days`を求める
  - まだ始まっていないworkspaceは警告して飛ばす。始まっているのに対象月のspendが無いworkspaceは
    止める（速報は`--allow-missing-workspace`を受け付けない。§26.6）。どのworkspaceにも対象月の
    spendが無い組織は、正式分析と同じく「対象月のデータが無い組織」として扱う
- workspace名は他組織の禁止語に加えない（一般名を推奨する。Teamの表示名を使うなら`label`に書く）

### 26.8 doctor

- 構造: 混在レイアウト・configとの不一致・主の不在と重複
- 対象月: workspace別のspend/membersの有無（§26.6の規則でerror/warning。まだ始まっていない
  workspaceは入力の検査をせずwarningを1件出し、始まっているのに対象月のspendが無いworkspaceは
  error）。ヒステリシス窓の
  欠月（`MISSING_HISTORY_MONTH`）は、その入力の最古のspendの月より前の月を数えない（まだ
  始まっていない月。従来レイアウトも同じ規則）
- 人: 対象月に始まっているworkspaceだけを見る（membersは月末に最も近いファイルを後の月からも
  採るので、未開始のworkspaceを含めると将来のメンバーと比べてしまう）
  - 副にだけアカウントがある人（副のStandard / Premiumのアカウントが主のmembersに居ない）を
    警告する（`SECONDARY_ONLY_ACCOUNT`）。許容するが、主の払い出し漏れかメールの相違の可能性が
    ある。主が未開始なら比べない
  - members-infoの未登録は、全workspaceのmembersと対象月のspendのメールの和集合で見る
    （`MEMBERS_INFO_UNREGISTERED`・組織単位で1件。従来レイアウトも同じ検査を持つ）。
    ファイルがあるのに読めないときはerror（`MEMBERS_INFO_UNREADABLE`。分析も同じ理由で止まる）
    にし、他の検査と`--format json`の出力は続ける

### 26.9 V2との関係

V2（decision-evidence）も§26.4の両軸に従う。複数アカウント保有者は主の行1本にし、
`SubjectHistory`の需要（全product）を全workspaceの月別合算で組む。
現シート・κ・実課金・シート変更event・加入の判定は主のもの。副の行はV2に持たない（副を
どうするかは§26.5の継続判定が担う）。V2の昇格・降格は主シートに対する結論と読む約束に
するので、語彙（`SeatAction`・`ReasonCode`）は増やさない。`workspace`列は主の名前を書く。
主判定がV2のとき（Track 11）、副のStandard / Premiumのアカウントの判定ラベルは、`fixed_seat`が
あれば従来どおり「対象外（固定シート）」、無ければ「対象外（副スペース）」とし、どちらも種別の
判定・削減額・付与候補の対象にしない。

- 履歴と完全性は人ごとに決める。副に払い出した月（§26.5）以降、副の部分月はその月を不完全月に
  し、副のspendが無い月（欠月）はその月以前を履歴から外す（主の欠月と同じ打ち切り。不完全月と
  して飛ばすと、降格の評価窓が古い月で補われて判定が変わる）。副にシートの無い人（未割当を
  含む）は主の履歴のまま
- Identityはworkspaceごとに解き、副でそのemailが衝突していれば`IDENTITY_CONFLICT`にする
  （workspace間でIDが違うこと自体は衝突にしない）
- 主が飛ばされた月（§26.6）は行を作らない。主に`fixed_seat`を書いた組織では、主のアカウントを
  V1と同じく判定しない（`excluded`）

### 26.10 段階

- Phase 1（Step 43〜48）: §26.2〜§26.9の全部。v1.3.0として出す
- Phase 2（Step 49）: 切替の観測。両workspaceの週次エクスポートが同じ日に揃うことが前提

## 27. Claude Code の習熟度の時系列

Claude Code の活用度と習熟度を人 × 月で追い、GitHub にマージされた成果と並べて見るための仕様。
実装順は Track 10（§18）。読者は CTO と開発責任者で、シートの判定には使わない。

### 27.1 背景と前提

- claude.ai のエクスポート（spend・code-analytics）だけでは「どれだけ使ったか」までしか分からず、
  「どう使ったか」（受け入れ・却下、自律の度合い、手戻り）は見えない。Claude Code の
  OpenTelemetry（以下 OTel）にはそれがある
- OTel は組織ごとに導入の有無が違う。導入した組織だけが`otel`を設定し（§27.2）、未設定の組織は
  OTel 関連の処理と警告から一切除外される（GitHub の`github_org`と同じゲート）
- OTel は利用側の収集基盤（collector → BigQuery）に蓄積されている前提で、このツールは BigQuery を
  読むだけ。collector の設定・配布・保持はこのツールの範囲外
- BigQuery のテーブルの形は収集基盤に依存する。Step 57 で実物を確かめてから SQL テンプレートを
  その形に合わせて書く。別の形に合わせるときはテンプレートを差し替える
- 測るのは参考値。因果を断定せず、個人ランキングを作らない（§15.6 と同じ）。評価の根拠には
  使わない

### 27.2 Config

```yaml
organizations:
  example:
    otel:
      bigquery:
        project: example-project
        dataset: claude_code_otel
        metrics_table: metrics
        logs_table: logs
      organization_ids: []
otel:
  bq_path: ""
  maximum_bytes_billed: 2000000000
  coverage_warning_ratio: 0.8
maturity:
  enabled: false
  stages: {}
```

- `organizations.<組織名>.otel`を書いた組織だけが対象
- `organization_ids`は OTel の`organization.id`で絞る claude.ai の組織 UUID。省略時は
  `claude_export.org_id`（複数スペースの組織は各 workspace のもの）を使う。両方無ければロード時に
  エラー
- `logs_table`を省略した組織はメトリクスだけで集計し、イベント由来の集計（§27.5 の表で元データが
  「イベント」の行）は空欄になる
- project・dataset の名前は workspace の`config.yaml`にだけ書く（公開リポジトリの docs・既定設定には
  書かない）

### 27.3 取得（`collect --source otel`）

- `bq` CLI を subprocess で呼ぶ（`gh`と同じ流儀。認証は gcloud に任せ、token を Python へ取り出さ
  ない）
- 読む前に`--dry_run`で読み取り量（byte）を出し、`otel.maximum_bytes_billed`を超えるときは実行
  しない。実行時も同じ値を`--maximum_bytes_billed`に渡す
- SQL は同梱のテンプレート（`src/seat_analyzer/sql/`。`.py`でない同梱物なので wheel に入ることを
  確かめる）に、月の範囲（UTC）・`organization.id`・テーブル名を埋める。日付のパーティション列で
  絞る
- SELECT する列は §27.5 の集計に要るものだけ。本文・引数・パスの列（§7.7 の「保存しない」）は
  テンプレートに書かない（テストがテンプレートを検査する）
- 集計は SQL 側で人 × 日 × 次元まで行い、行の単位のデータをローカルへ持ち込まない
- 結果は`otel-cache/otel-YYYY-MM.json`（§7.7）。同じ月の再実行は置き換え
- 認証なし・権限なし・上限超過・途中失敗はそれぞれ issue（§17）にし、部分結果を完全な結果として
  保存しない
- 定期実行（§14.5）には含めない。サブスクリプションの外の課金が生じるため、人が都度実行する

### 27.4 網羅率

テレメトリが届いていない人を「使っていない人」と読む誤りを防ぐため、OTel の値を使う前に網羅率を
確かめる。

- 人 × 月で、OTel の`cost.usage`の合計と spend の Claude Code product の API 換算需要
  （`computed_cost_usd`）を比べる。比が`otel.coverage_warning_ratio`を下回る人 × 月は
  `OTEL_COVERAGE_LOW`、spend に Claude Code の需要があって OTel に行が無い人は`OTEL_USER_MISSING`
- 組織の網羅率（需要の合計に対する OTel で説明できる割合）を doctor と analyze の警告に出す
- 網羅率の低い人 × 月は、パネルで OTel 由来の列を空欄にし、段階を`不明`にする

### 27.5 指標

人 × 月で次を持つ。元データの名前は Claude Code の OTel リファレンスのもの。

| 軸 | 指標 | 元データ |
|---|---|---|
| 活用 | 稼働日数・セッション数（新規・再開の別） | `session.count`（`start_type`） |
| 活用 | 利用者の操作時間・CLI の稼働時間 | `active_time.total`（`type`） |
| 活用 | プロンプト数 | イベント`user_prompt`（件数のみ） |
| 活用 | Claude Code が加えた行・消した行 | `lines_of_code.count`（`type`） |
| 活用 | トークン・費用（モデル別） | `token.usage`・`cost.usage`（`model`・`type`） |
| 自律 | 編集の受け入れ・却下と判断の出どころ（自動承認か都度か） | `code_edit_tool.decision`（`tool_name`・`decision`・`source`） |
| 自律 | 権限モードの構成 | イベント`tool_decision`（`permission_mode`） |
| 自律 | サブエージェント・補助クエリの割合 | `cost.usage`・`token.usage`（`query_source`） |
| 自律 | スキル・エージェント・MCP の利用 | `cost.usage`（`skill.name`・`agent.name`・`mcp_server.name`の有無。名前は件数に畳む） |
| 自律 | スラッシュコマンドの利用（組込み・カスタム・MCP の別） | イベント`user_prompt`（`command_source`） |
| 自律 | API エラー率 | イベント`api_error`（件数のみ） |
| 成果 | Claude Code が作ったコミット・PR | `commit.count`・`pull_request.count` |
| 成果 | コミット単位のマージ行（上限つき）・コミット数・lead time | GitHub（§15.5・§15.7） |
| 効率 | マージ行あたりの費用、消した行 ÷ 加えた行 | 上の組み合わせ |
| 検知 | リポジトリの owner 別のセッション・費用 | `vcs.owner.name`（§27.7。属性が有効な組織のみ） |

- 比率は分子・分母を列に持ち、比率そのものはパネルで計算して列にする（元の値を失わない）
- 中央値のように日次から月次へ畳めない統計は持たない（件数・合計・秒・USD・トークンに閉じる）
- スキル・エージェント・MCP・コマンドの名前は保存しない（有無と件数だけ）

### 27.6 段階

1 人の 1 か月を次のどれかに分ける。ルールは`maturity.stages`に置き、既定は Step 63 で実データの
分布を見てから決める。1 つの合成スコアにはしない（何が効いて動いたかが読めず、閾値の置き方に
結論が引きずられる）。

- `未使用`: セッションが無い
- `試用`: 稼働日数・セッション数が少ない
- `定着`: 継続して使い、Claude Code が加えた行がある
- `自律`: 自動承認・サブエージェント・スキルを使い、Claude Code がコミットや PR を作っている
- `高度`: `自律`に加えて、カスタムコマンド・MCP を使い、効率の指標が組織の上位にある
- `不明`: 網羅率が低い（§27.4）

組織の見せ方は、段階ごとの人数の月次推移と、前月からの遷移（上がった人・下がった人の数）。

### 27.7 管轄外の検知

OTel の利用のうち、この組織の GitHub Organization の外で行われた分を人ごとに数える。母集団から
外さない（§15.5 の`なし`は GitHub 側の数え方で、ここでは使わない）。

- リポジトリ属性（`vcs.owner.name`・`vcs.repository.name`。`OTEL_METRICS_INCLUDE_REPOSITORY`が
  有効な端末だけが送る）がある組織: owner が`github_org`と一致しないセッション・費用の割合を
  `outside_org_share`に出す。owner 名・リポジトリ名は成果物に書かない
- 属性が無い組織: OTel の`commit.count`と GitHub のその人のコミット数（§15.7）の差を
  `unexplained_commits`に出す（Claude Code が作ったコミットのうち、この Organization に現れなかった
  分）
- どちらも参考値。ローカルだけの作業・未マージ・別 org の正当な業務を区別できないので、人に
  確かめるきっかけとして扱う

### 27.8 パネルと管理職向け成果物

- `maturity-panel-<YYYYMM>-<org>.csv`: 1 行が 1 人の 1 か月。その月までの全月（入力にある月）を
  含む。列は活用・自律・成果と効率・検知・段階・文脈（シート種別・部署・チーム・職種・
  code-analytics の有無・GitHub と OTel の網羅）。行はメール昇順・月昇順
- `maturity-dashboard-<YYYYMM>-<org>.html`: 散布図（LoC × マージ行。両対数）、個人の推移、段階
  ごとの人数の推移、コホート（利用開始月で束ねて経過月数ごとの中央値）。外部通信なし・JS 無効でも
  全内容が読める・メールはローカル部のみ（既存 dashboard と同じ 3 点）
- 読者は CTO と開発責任者。組織の担当者へ共有する成果物（report・details・dashboard・
  recommendations・usage-summary・preview・preview-dashboard と opt-in の decision-evidence・
  github-summary）の集合には含めない。docs にその旨を書く
- `maturity.enabled: false`が既定。有効でも、GitHub・OTel の無い組織では該当の列が空欄になるだけで
  書ける
- 組織横断の比較は`reports/summary/<month>.md`にだけ書く
- 複数スペースの組織は人の層（§26.4）で合算する
- 生データ（月次ファイル・週次累積・github-cache・otel-cache）が正で、パネルは毎回それから作り直す
  （追記型の履歴ファイルを持たない）

### 27.9 保存しないもの

§7.7 の「保存しない」に加えて、成果物にも次を書かない。

- プロンプト・応答・ツールの引数・コマンド
- リポジトリ名・owner 名・ファイルパス
- スキル・エージェント・MCP・コマンドの名前
- 他組織の情報

### 27.10 後続

- コミット SHA による結合: イベント`tool_result`の`vcs.ref.head.revision`と PR のコミットの oid を
  突き合わせ、「Claude Code のセッションを経た PR の割合」を出す
- 週次の粒度: 週次の累積スナップショットと OTel の日次から、月内の推移を出す
