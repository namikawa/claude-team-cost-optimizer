# Claude利活用・シート適正化機能 実装設計書

- ステータス: Draft
- 最終更新: 2026-07-30
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
  → report.md / dashboard.html / recommendations.csv
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

`analyze.py`と`report.py`は当面分割しない。既存リファクタリングは別タスクとする。

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
  report.md
  dashboard.html
  recommendations.csv
```

追加:

```text
reports/<org>/<month>/
  data-quality.json
  usage-summary.csv
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
```

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

Code需要が低く全product需要だけ高い場合は`REVIEW_ASSIGNMENT`とする。

### 12.5 Downgrade

必要履歴:

```yaml
decision_v2:
  downgrade:
    min_complete_months: 2
```

候補条件:

1. current seatがPremium
2. 直近2完全月でCode需要が低い
3. 直近2完全月で全product需要も低い
4. 実課金・credit上限到達がない
5. 直近のシート変更ではない
6. partial month、identity conflictではない

Code需要が低くsupplementaryが高い場合は、自動downgradeせず`REVIEW_ASSIGNMENT`とする。

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
uv run seat-analyzer doctor --org <org> --month YYYY-MM

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

### Identity

- `IDENTITY_EMAIL_FALLBACK`
- `IDENTITY_CONFLICT`
- `GITHUB_MAPPING_MISSING`
- `GITHUB_MAPPING_DUPLICATE`

### Seat/credit

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

- `config.yaml`
- `src/seat_analyzer/config.py`
- `tests/test_hardening.py`

実装:

- primary
- supplementary
- prohibited

受け入れ条件:

- 省略時default
- 空primaryはerror
- 現行分析不変

今回は行わない:

- 集計

#### Step 7: Code/全product特徴量

依存:

- Step 6

対象:

- `src/seat_analyzer/product_usage.py`
- `tests/test_product_usage.py`

実装:

- total demand
- Code demand
- Code share
- product breadth

受け入れ条件:

- Code alias対応
- 分母0は`NA`
- 全product費用とCode活用を分離

今回は行わない:

- seat判定
- report

#### Step 8: usage-summary.csv

依存:

- Step 7

対象:

- `src/seat_analyzer/report_v2.py`
- `src/seat_analyzer/cli.py`
- `tests/test_cli.py`

実装:

- 既存analyze時に任意の追加CSV

受け入れ条件:

- 既存出力不変
- Code/totalを表示
- prohibited product warning

今回は行わない:

- dashboard

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

- Step 24
- Step 38
- Step 41

対象:

- `src/seat_analyzer/report.py`
- `tests/test_cli.py`

実装:

- review summary
- Code usage
- GitHub reference
- Billing

受け入れ条件:

- 既存8列を増やさない
- 別section/tab
- 横スクロールなし
- 任意入力なしでも崩れない

## 19. Milestone

### Milestone A: Core data

Step 1〜8

- stable IDの準備
- Data Doctor
- Code/全product分離

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
  downgrade:
    min_complete_months: 2
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
docs/implementation-design.md の「Step N: <名称>」だけを実装してください。

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
