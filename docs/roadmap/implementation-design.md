# Claude利活用・シート適正化機能 実装設計書

- ステータス: Draft
- 最終更新: 2026-08-14
- 対象: `seat-analyzer` 0.1系からの段階的拡張
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
- Spendだけを通常ブラウザとdownload watcherで取得補助できる

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
- 管理画面CSVは通常ブラウザとdownload watcherによる取得補助を許容
- CSVがない管理画面表は小さな手入力CSVを使用する
- GitHubはローカルの認証済み`gh`を使用
- GitHubリポジトリはOrganization単位で自動発見
- ソースコード、diff、PR本文は取得しない

### 3.5 プロダクト

- Claude Codeをprimaryとする
- Chat、Cowork、Design等はsupplementaryとする
- 費用には全プロダクトを含める
- 活用評価はCodeを主軸とする
- Cowork、Design等は利用可視化まで
- 禁止機能はpolicy warningとする

### 3.6 分析単位

- 新機能はClaude組織ごとに独立して分析する
- GitHub OrganizationもClaude組織ごとに1つ設定する
- 組織を跨いだユーザー結合・最適化を行わない
- 既存の複数組織一括実行機能は維持してよい

## 4. ゴールと非ゴール

### 4.1 ゴール

- stable IDを利用できる
- シート変更をMembers差分から検出できる
- upgradeとdowngradeで異なる履歴条件を使える
- Standard/Premium/creditを比較できる
- 推奨と実変更を照合できる
- 変更後2/4/8週を評価できる
- GitHub PR数・リードタイムを参考表示できる
- 管理画面CSV取得を通常ブラウザとdownload watcherで補助できる
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
  browser_collect.py    # 通常ブラウザとdownload watcherによる取得補助
  github_collect.py     # ghによるPR metadata
  github_metrics.py     # PR数・lead time
  report_v2.py          # 新規CSV/Markdown
```

上の2つのブロックは設計時点の想定で、現状とは次の点が異なる（v1.0.0 時点）。

- `report.py`は`report/`パッケージになっている。`__init__.py`が公開APIとオーケストレーション、出力形式ごとに`markdown.py`/`html.py`/`csv_out.py`、共通処理が`document.py`/`format.py`/`text.py`という構成
- `analyze.py`も`analyze/`パッケージになっている（`__init__.py`/`credits.py`/`midmonth.py`）
- 段階的に追加する一覧のうち`domain.py`/`data_quality.py`/`identity.py`は追加済み。一覧に無いものとして考察執筆まわりの`discussion.py`/`leakcheck.py`/`public_text.py`がある
- `report_v2.py`という単一モジュールは作らない。新しい出力は`report/`パッケージの中へ、出力形式ごとに足す（CSVなら`report/*_csv.py`）。以降のStepで対象として`report_v2.py`または`report.py`と書かれている箇所は、この方針に読み替える。責務ごとの実ファイル名は各Trackの着手時に個別に確定させる（着手しないTrackを先回りして書き換えない。§2.4）
- 設定の既定は`src/seat_analyzer/default-config.yaml`が唯一の源。リポジトリ直下・ワークスペースの`config.yaml`は差分だけを書く任意の上書きファイルで、gitignore済み。以降のStepで対象として`config.yaml`と書かれている箇所は`default-config.yaml`に読み替える
- モジュールを増やすときは`tests/test_module_deps.py`の`LAYERS`へ層を割り当てる。パッケージ内importは自分より厳密に下の層だけを指してよく、同層どうしのimportも認めない。層に無いモジュールを足すとテストが落ちる

## 6. ディレクトリ

### 6.1 入力

```text
input/<org>/
  spend/
  members/
  code-analytics/
  members-info.csv
  github-members.csv
  admin/
    organization-YYYY-MM-DD.csv
    users-YYYY-MM-DD.csv
  github-cache/
    prs-YYYY-MM.json
  collection/
    manifest-YYYY-MM-DDTHHMMSS.json
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
  review.md
  history/
    <snapshot-id>/
      decision-evidence.csv
      source-manifest.json
```

### 6.3 Download監視

```text
OSの通常download directory
```

- 監視先はconfigまたはCLIで変更可能にする
- コマンド開始後に新規作成・更新されたCSVだけを候補にする
- CSV全体を走査せず、種別判定に必要なheaderだけを読む
- 元ファイルを変更・削除しない
- browser profile、Cookie、認証情報へアクセスしない

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

`input/<org>/github-members.csv`

```csv
email,github_login
user@example.com,example-user
```

制約:

- emailは一意
- github_loginは組織内で一意
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

保存しない:

- title
- body
- comments
- review本文
- files
- diff
- commit message
- code

一意キー:

```text
repository + "#" + PR number
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

- 費用比較は`total_demand_usd`
- 活用判定は`code_demand_usd`を主にする
- Code低・supplementary高は自動downgradeせず`REVIEW_NON_CODE_USAGE`
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
    REVIEW_ASSIGNMENT = "review_assignment"
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

- `ONE_MONTH_STRONG_CODE_DEMAND`
- `SUSTAINED_LOW_CODE_DEMAND`
- `SUSTAINED_LOW_TOTAL_DEMAND`
- `SUSTAINED_OVERAGE`
- `CREDIT_LIMIT_REACHED`
- `CREDIT_SETTING_UNKNOWN`
- `PREMIUM_CHEAPER_THAN_STANDARD_WITH_CREDIT`
- `STANDARD_WITH_CREDIT_CHEAPER`
- `HIGH_SUPPLEMENTARY_USAGE`
- `REVIEW_NON_CODE_USAGE`
- `RECENT_MEMBER`
- `RECENT_SEAT_CHANGE`
- `PARTIAL_MONTH`
- `INSUFFICIENT_HISTORY`
- `IDENTITY_CONFLICT`
- `CAPACITY_SIGNAL_UNAVAILABLE`
- `DATA_CONFIDENCE_LOW`

### 12.3 Cost

```text
standard_cost =
  standard_unit_price
  + expected_standard_credit

premium_cost =
  premium_unit_price
  + expected_premium_credit
```

現在シートの実課金を観測値として優先し、変更先は現行allowanceモデルを使用する。
low/mid/highは引き続きscenario stabilityとして出力する。

5時間枠・週次上限を観測できないため、`CAPACITY_SIGNAL_UNAVAILABLE`を情報として付ける。
この理由だけで全候補を保留にはしない。

### 12.4 Upgrade

必要履歴:

```yaml
decision_v2:
  upgrade:
    min_complete_months: 1
    min_code_demand_usd: 200.0
```

候補条件:

1. current seatがStandard
2. 完全月が1か月以上
3. Code需要が設定閾値以上
4. 次のいずれか
   - Standardの実課金を含む費用がPremiumより高い
   - usage credit上限へ到達
   - 純モデル判定でPremiumが複数scenarioにおいて有利
5. partial month、identity conflictではない

条件3の閾値は`min_code_demand_usd`（既定はシート差額の2倍）。シート差額程度の需要は
軽度の利用でも生じうるため、Code主体であることの証明には差額の2倍を要求する。Code需要を
確定できない場合は、Code主体であることを証明できないまま自動で推奨しないため
`OBSERVE`とする。

条件4は費用の軸で、シートの込み枠がproduct共通であることから全product合算の需要と
実課金で評価する。実課金を含む経路と純モデル経路はいずれも`min_assignment_saving_usd`
以上の月あたり削減見込みを要求する。削減見込みは正であることも要求する
（`min_assignment_saving_usd: 0`の設定でも、StandardとPremiumが同額の状態は「Standardの
費用がPremiumより高い」を満たさない）。usage credit上限への到達だけは金額差ではなく上限
そのものを根拠にし、到達には実課金の発生を伴う（実課金ゼロを到達とみなさない）。

条件3は費用の軸を満たした候補を振り分けるゲートとして働く。Code需要が低く全product
需要だけ高い場合は`REVIEW_ASSIGNMENT`とし、statusは`RECOMMENDED`とする。シートを変える
のではなくアサインを人が見直す作業として出すためで、保留（`OBSERVE`）ではない。

### 12.5 Downgrade

必要履歴:

```yaml
decision_v2:
  downgrade:
    min_complete_months: 2
    max_code_demand_usd: 200.0
```

候補条件:

1. current seatがPremium
2. 直近2完全月でCode需要が低い
3. 直近2完全月で全product需要も低い
4. 実課金・credit上限到達がない
5. 直近のシート変更ではない
6. partial month、identity conflictではない

条件2〜4の評価窓は直近の完全月`min_complete_months`ヶ月で、間に部分月が挟まる場合は
飛ばして完全月だけを採る。誤ったdowngradeは業務を止めるため、条件は窓の全月で成立する
ことを要求する。

条件2の閾値は`max_code_demand_usd`（既定は昇格側の`min_code_demand_usd`と同額）。この値
以上のCode需要が窓のいずれかの月にあるユーザは自動downgradeの対象にしない。Code需要を
確定できない月がある場合は、低いことを証明できないまま自動でdowngradeしないため
`OBSERVE`とする（upgradeのNA扱いと同じ）。

条件4は窓のいずれかの月に実課金があれば候補から外す（Premiumでの実課金は、需要が
Premiumの込み枠を超えた観測）。credit上限への到達は実課金の発生を伴うため、この検査に
含まれる。

条件3は費用の軸で、窓の実課金が0と確定しているため観測実課金による拘束は働かず、需要
だけの純モデル判定になる。`min_assignment_saving_usd`以上のmid削減見込みと複数scenarioの
一致を要求する点はupgradeと同じ。

Code需要が低くsupplementaryが高い場合は、自動downgradeせず`REVIEW_ASSIGNMENT`とし、
statusは`RECOMMENDED`とする（upgradeの条件3と同じ扱い）。この振り分けは条件3より先に
行う。全product需要が大きく条件3が成立しない場合に現状維持で終わらせると、Premiumの枠を
非Code利用で使っている状態が誰の作業としても残らないため。

### 12.6 Credit

比較:

- Standard + observed/estimated credit
- Premium + observed/estimated credit

継続性:

- 1か月だけ高い
- 2か月以上高い
- 週次snapshotで継続上昇

一時的な高利用でStandard + creditが安い場合は`ENABLE_WITH_CAP`、継続的でPremiumが
安い場合は`UPGRADE_TO_PREMIUM`とする。

credit設定が不明なら金額を断定せず`CreditAction.REVIEW`とする。

### 12.7 Confidence

次元:

- usage coverage
- identity quality
- seat history coverage
- credit setting coverage
- scenario stability

Billing契約情報やOTelをupgrade/downgradeの必須条件にしない。

hard blocker:

- partial month
- identity conflict
- current seat unknown
- 必要履歴不足

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

### 14.1 実装方式

- OSの通常ブラウザを開く
- 既存のログイン状態をそのまま利用する
- Organization選択、画面遷移、Exportは利用者が手動で行う
- download directoryでコマンド開始後の新規CSVを監視する
- headerからSpend、Members、Code Analyticsを判別する
- 安定したファイルだけを対象Organizationの入力へコピーする
- ブラウザ画面の操作やDOM取得は行わない
- Playwright、Chrome remote debugging、非公開APIを利用しない

### 14.2 セキュリティ

- ID、パスワードを受け取らない
- MFAを自動化・回避しない
- Cookieを表示しない
- browser profileを読み取らない
- internal APIをreverse engineerしない
- シート・credit設定を変更しない
- 既存download fileを変更・削除しない
- 検出した実データの内容や元のfilenameをログ出力しない

### 14.3 収集manifest

```json
{
  "collected_at": "2026-07-29T10:00:00+09:00",
  "organization": "example",
  "sources": [
    {
      "kind": "spend",
      "path": "input/example/spend/...",
      "sha256": "...",
      "status": "downloaded"
    }
  ]
}
```

同一sha256のファイルを重複配置しない。

### 14.4 フォールバック

Browser-assisted collection失敗時:

1. 失敗理由を表示
2. 手動ダウンロード先を案内
3. 既存の`analyze`を利用可能にする
4. 分析自体をbrowser依存にしない

## 15. GitHub collection

### 15.1 Config

```yaml
organizations:
  example:
    github_org: example-org
```

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

### 15.6 GitHubは参考情報

- V2 seat actionの必須条件にしない
- GitHub dataがなくても分析を成功させる
- 因果関係を断定しない
- 個人ランキングを作らない

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
  --org <org> \
  --source github \
  --month YYYY-MM
```

V2が安定するまで既定値は`v1`。

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

### GitHub issue

- `GH_NOT_AUTHENTICATED`
- `GH_ORG_NOT_ACCESSIBLE`
- `GH_PERMISSION_INCOMPLETE`
- `GH_RATE_LIMITED`
- `GH_PARTIAL_RESULT`

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

v1.1.0で追加したStep。原設計には無い。実データでの目視レビュー（2026-08-16）を受けた
ユーザ判断: dashboardが数値を担い、report.mdはアクションと考察を中心にした短い文書にする。
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
- 「前月からの変化」を残すのはユーザ判断（月次の増減はレポート本文でも語らせたい）
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

デザイン自体はユーザがClaude Designで作った（2026-08-17受領）。このStepの作業は、
デザイン仕様をtemplatesへ書き起こし、全組織・全データ状況で成立させることになる。

デザイン成果物は**そのまま流用できない**。Claude Designのランタイム前提の形式で、
描画ロジックはコンポーネントとして書かれ、スタイルの大半はインライン指定である。
成果物のREADME（配色・余白・タイポグラフィ・挙動を数値で確定させたもの）を仕様として、
Jinjaテンプレート + CSS + 素のJSへ書き起こす。

**作業量が大きいため2つのPRに分ける**（8D-1 = 見た目、8D-2 = 対話機能）。1つに畳むと
レビューが機能しない規模になる。

依存:

- Step 8E

##### 決定事項（ユーザ確認済み・2026-08-17）

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
  ユーザ表示をローカル部 + `title`（フルアドレス）に統一する**（2026-08-17 ユーザ裁定。
  「前月からの変化」を8D-1で変え、残る箇条書き7箇所＝月中のメンバー変動・追加クレジット
  付与候補・月中の利用推移の注記を8D-2で揃えた）。表とバーが元からこの形で、箇条書きだけが
  完全なメールアドレスだったため、揃える側に寄せた判断。**Markdown側（`report.md`・
  `details.md`・`preview.md`）は完全なメールアドレスのまま**で、こちらは変えない
  - 外部レビューは、印刷やタッチ環境では`title`が読めず、異なるドメインに同じローカル部が
    あると画面上で区別できないことを指摘した（特に付与候補は操作対象の特定に使う）。
    運用中の組織はいずれも単一ドメインで取り違えが起きず、識別が要る場面ではMarkdown側の
    レポートが完全なアドレスを持つため許容する
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

デザイン成果物（Claude Design のREADME）から、実装に必要な数値をここへ書き写した。
成果物そのものは実データを含むため作業ツリーには残さない。以下がこのStepの仕様の正。

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
  枠線に`--dim`を使う（2026-08-17 ユーザ裁定）。デザインが指定する`--line`はカード面との比が
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

- Standard + credit対Premium
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

- `src/seat_analyzer/report_v2.py`
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

依存:

- Step 26

実装:

- Membersだけ追加

受け入れ条件:

- Spend動作不変
- snapshot filename

#### Step 28: Code Analytics検出・配置

依存:

- Step 27

実装:

- Code Analyticsだけ追加

受け入れ条件:

- Spend/Members動作不変
- export不可ならwarning

#### Step 29: Collection manifest

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
- Step 30

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
- `tests/test_github_collect.py`

実装:

- merge date range
- metadataだけ
- upsert

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

- `src/seat_analyzer/report_v2.py`
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

Step 25〜31

- 通常ブラウザ
- download watcher
- 公式CSV
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

### Future

- OpenTelemetry
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

- V1指定時の判定は変更前と同じ
- partial monthから変更推奨を出さない
- upgradeは1完全月で候補になり得る
- downgradeは2完全月未満で候補にならない
- Code低・supplementary高を自動downgradeしない
- シート変更eventは重複しない
- 未来の推奨を過去変更へ照合しない
- follow-up coverage不足はnot evaluable
- GitHubなしでも分析できる
- browserなしでも分析できる
- GitHub本文・コードを保存しない
- browser-assisted取得は管理画面を操作しない
- 不明値を0として扱わない

### 20.3 Security

- CSV Formula Injection
- org path traversal
- Cookie・token非表示
- GitHub cache禁止フィールド
- download元ファイルを変更・削除しない

## 21. Config

追加例:

```yaml
organizations:
  example:
    github_org: example-org

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

decision_v2:
  enabled: false
  upgrade:
    min_complete_months: 1
    min_code_demand_usd: 200.0
  downgrade:
    min_complete_months: 2
    max_code_demand_usd: 200.0
  recent_seat_change_days: 28
  min_assignment_saving_usd: 20.0

follow_up:
  horizons_days: [14, 28, 56]
  min_coverage_ratio: 0.60

github:
  initial_lookback_days: 90
  exclude_archived: true
  exclude_forks: true
  exclude_templates: true
  repository_denylist: []

browser:
  enabled: false
  open_url: "https://claude.ai/"
  download_dir: null
  timeout_seconds: 300
```

新しいセクションはすべて省略可能とする。`decision_v2.enabled=false`が既定。

## 22. Migration

### 22.1 V1/V2並行

1. V1をdefaultのまま維持
2. V2 evidenceだけ追加
3. 複数月並行出力
4. 差異をreview
5. V2 ruleを調整
6. 明示承認後にdefaultをV2へ変更
7. V1を最低3か月残す

### 22.2 Rollback

- `--decision-version v1`
- `decision_v2.enabled=false`
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

- Step 0C成功
- 3回連続でCSV取得成功
- login期限切れを安全に扱える
- browser画面操作とシート・credit変更操作が存在しない
- 手動fallbackが動く

### GitHub integration release

- Step 0A成功
- Organization全体を自動発見
- permission coverage表示
- metadataだけを保存
- rate limit時に部分結果を明示

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

次が整ってから別設計として追加する。

- 端末設定配布
- 収集endpoint
- セキュリティ合意
- 小規模pilot

現在の設計へplaceholderや未使用抽象化を追加しない。

### Jira・CI/CD

使用基盤、権限、正準IDを確認してから別Trackを追加する。

### Enterprise

Teamの公式CSV・管理画面で不足が明確になった場合にのみ検討する。
