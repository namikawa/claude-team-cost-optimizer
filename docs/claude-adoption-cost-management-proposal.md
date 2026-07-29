# Claude利活用・シート適正化機能提案書

- ステータス: Proposal
- 最終更新: 2026-07-30
- 対象: `claude-team-cost-optimizer`
- 関連文書: [実装設計書](./implementation-design.md)
- 進捗管理: [実装ステータス](./implementation-status.md)

## 1. エグゼクティブサマリ

本システムは、Claude Teamプランを利用するエンジニア組織において、Claude Codeを中心と
した利用状況をローカルで分析し、Standard、Premium、usage creditの適正な割当を支援する
ツールである。

目的は単純なコスト削減ではない。低利用のPremiumをStandardへ変更する一方で、強い需要が
観測されたStandardを積極的にPremiumへ変更し、組織全体のClaude Code活用機会を最大化する。

確定した運用方針は次のとおりである。

- 分析はClaude組織ごとに独立して行う
- 週次は利用変化とシート変更の検出、月次はシート判断と効果検証を行う
- StandardからPremiumは、1か月でも十分に強い需要があれば候補化する
- PremiumからStandardは、2か月連続の低利用を確認する
- 一時的な高利用は、Premiumとusage creditの費用・継続性を比較する
- シート変更は担当者が管理画面で実施し、システムは変更しない
- 実際の変更はMembersスナップショット差分から独立して検出する
- 変更後2週間、4週間、8週間の利用変化を検証する
- GitHubのPR数とPRリードタイムは参考情報として扱う
- Code以外のClaude利用も費用には含めるが、活用評価はCodeを主軸にする
- 追加の月次手作業は5分以内を目標とする

コア機能の実現性は高い。一方、OpenTelemetryを必要とするeffort、subagent、セッション、
retry、compaction等の分析は、現在の端末管理・収集基盤を前提にすると実装対象外とし、
将来候補として残す。

## 2. 目的

### 2.1 解決する問い

1. Premiumを十分に活用していないメンバーは誰か
2. Standardでは潜在需要を満たせていない可能性があるメンバーは誰か
3. 一時的な高需要にはPremiumとusage creditのどちらが適切か
4. シート変更後に利用が増えたか、制約が軽減したか
5. 変更担当者の判断とデータ上の推奨は一致していたか
6. 判断が一致しなかった場合、変更後の結果はどうだったか
7. Claude Codeの利用とGitHub上のPR活動にどのような傾向があるか
8. Code以外のClaudeプロダクトはどの程度利用されているか

### 2.2 本システムの位置づけ

本システムは次の役割を持つ。

- シート割当の意思決定支援
- usage creditの意思決定支援
- 変更判断の独立レビュー
- 変更後の効果検証
- 利用促進対象の発見
- 予算と需要の可視化

人事評価、査定、個人の生産性順位付けには使用しない。トークン、LoC、PR数は活動量で
あり、個人の能力や価値を直接表すものではない。

## 3. 確定した運用前提

### 3.1 データ取得

- Spend、Members、Code Analyticsは管理画面から週1回程度取得する
- 取得曜日・時刻は厳密に固定しない
- ファイルの期間と取得日をシステムが解釈する
- 管理画面の公式CSV取得は、通常ブラウザとdownload watcherで配置まで補助する
- 取得補助に失敗した場合は、現在の手動ダウンロードへフォールバックする
- CSVがない管理画面数値は、月1回の小さなCSV入力を許容する
- スクリーンショットは証跡とし、原則として数値入力元にしない
- 非公開APIや画面内部APIへ依存しない

### 3.2 実行環境

- サーバは設けず、特定のローカルマシンで実行する
- 開発者端末への設定一括配布を前提にしない
- 外部Telemetry endpointを前提にしない
- OpenTelemetryは現行スコープに含めない
- 実データとレポートはGitへ含めない

### 3.3 シート変更

- システムは候補と根拠を提示する
- シート変更は権限を持つ担当者が管理画面で行う
- システムから管理画面のシートを自動変更しない
- 承認・通知は本システムの対象外とし、導入組織の運用に委ねる
- 変更記録の手入力を担当者へ要求しない
- Members CSVの前回差分から変更を機械的に検出する

### 3.4 レビュー

分析は変更担当者自身の操作記録に依存せず、別のレビュー担当者が独立して確認できる形に
する。

評価対象は担当者個人ではなく、シート変更という意思決定である。

## 4. 実現性

| 機能 | 実現性 | 現行スコープ |
|---|---|---|
| Standard/Premium候補 | 高い | 実装対象 |
| usage credit比較 | 中〜高 | 実装対象 |
| シート変更の自動検出 | 高い | 実装対象 |
| 変更後2/4/8週評価 | 高い | 実装対象 |
| GitHub PR数・リードタイム | 高い | 実装対象 |
| Code中心のプロダクト分析 | 高い | 実装対象 |
| 管理画面CSV取得補助 | 高い | 通常ブラウザ＋download watcher |
| 購入席・請求情報 | 高い | 補助機能として実装 |
| Cowork/Designの利用可視化 | 高い | Spend・管理画面の範囲 |
| Cowork/Designの成果分析 | 不要 | 対象外 |
| effort・speed | 現状困難 | 将来候補 |
| main/subagent | 現状困難 | 将来候補 |
| error・retry・compaction | 現状困難 | 将来候補 |
| 5時間枠・週次上限の直接観測 | 現状困難 | 間接シグナルで代替 |
| Jira・CI/CD・DORA | 未確認 | 将来候補 |

## 5. 判断原則

### 5.1 活用を優先し、削減を目的化しない

最適化の優先順位は次のとおりとする。

1. Code利用機会を必要なメンバーへ提供する
2. Standard、Premium、usage creditを需要に合わせる
3. 変更後の利用変化を観測する
4. 使われていないPremiumを別の需要へ再配置する
5. 結果として不要な支出を抑える

### 5.2 アップグレードとダウングレードを非対称にする

活用促進フェーズでは、アップグレードは試しやすく、ダウングレードは慎重にする。

```text
Standard → Premium:
  1か月でも十分に強い需要があれば候補化

Premium → Standard:
  2か月連続の低利用で候補化
```

### 5.3 Codeと他プロダクトを分離して表示する

```text
費用・利用枠の需要:
  Codeを含む全プロダクト

エンジニア組織としての活用:
  Claude Codeを主指標

Chat/Cowork/Design等:
  補足指標
```

代表的な解釈:

| Code | その他 | 解釈 |
|---|---|---|
| 高い | 任意 | Code高活用 |
| 低い | 高い | 非Code高活用。自動変更せずレビュー |
| 低い | 低い | 低活用・ダウングレード候補 |
| 高い | 高い | Claude全体の高活用 |

### 5.4 GitHub成果は参考情報にする

マージPR数とPRリードタイムを取得するが、Claude利用との因果関係は断定しない。

- シート変更前後の参考比較
- 同一人物・同一チーム内での傾向
- Claude利用量とPR活動の相関

個人ランキングやシート判定のhard gateには使用しない。

## 6. P0: コア機能

### 6.1 入力品質とstable ID

- Spendのaccount UUID、user ID、gross/net spend等を正準化する
- email変更時も同一人物を追跡できるようにする
- stable IDがない場合はemailへフォールバックする
- 欠月、部分月、重複、未知モデル、members不整合をData Doctorで検出する

### 6.2 シート変更履歴の自動生成

前後のMembersスナップショットを比較し、次を検出する。

- Standard → Premium
- Premium → Standard
- Assigned → Unassigned
- Unassigned → Assigned
- メンバー追加・削除

正確な変更日時は取得できないため、次の区間として記録する。

```text
changed_after  = 前回スナップショット日時
changed_before = 今回スナップショット日時
detected_at    = 今回スナップショット日時
```

### 6.3 Decision Engine v2

ユーザーごとに次を別々に出力する。

- `seat_action`
- `credit_action`
- `enablement_signal`
- `data_confidence`
- `scenario_stability`
- `reason_codes`

単一の活用スコアは作らない。

### 6.4 Standard、Premium、usage credit比較

比較対象:

- Standard
- Standard + usage credit
- Premium
- Premium + usage credit

主な入力:

- 全プロダクトのAPI等価需要
- Claude Code需要
- 実課金
- credit設定・上限
- 需要の継続月数
- 週次スナップショットの増加傾向

5時間枠・週次上限を直接取得できないため、API等価需要だけで上限到達を断定しない。

### 6.5 Recommendation Evidence

推奨ごとに以下を出力する。

- 現在のシート
- 推奨シート
- credit提案
- 判定期間
- reason codes
- 全プロダクト需要
- Code需要
- 実課金
- 予測費用
- シナリオ安定性
- データ確度
- 変更後に確認すべき指標

## 7. P0: 変更判断の独立レビュー

### 7.1 推奨と変更の照合

```text
システム推奨
  ↓
Members差分で実際の変更を検出
  ↓
変更直前の推奨と照合
  ↓
2/4/8週後の結果を評価
```

### 7.2 分類

| 分類 | 意味 |
|---|---|
| `matched` | 推奨と実変更が一致 |
| `recommended_not_changed` | 推奨したが変更未検出 |
| `changed_without_recommendation` | 推奨なしで変更 |
| `changed_opposite` | 推奨と逆方向へ変更 |
| `not_evaluable` | 変更前の推奨または変更後データが不足 |

### 7.3 変更後の評価

#### Premium化

主指標:

- Claude Code需要・利用頻度が増えたか
- 全プロダクト需要が増えたか
- usage creditの問題が軽減したか
- 高利用が継続したか

参考指標:

- マージPR数
- PRリードタイム

#### Standard化

主指標:

- 利用が不自然に低下していないか
- usage creditが急増していないか
- 短期間でPremiumへ戻っていないか
- Code利用を維持できているか

参考指標:

- マージPR数
- PRリードタイム

### 7.4 評価時点

- 2週間: 初期変化
- 4週間: 1か月相当
- 8週間: 継続性

変更日は区間でしか分からないため、原則として`changed_before`を起点にする。

## 8. P1: 収集補助

### 8.1 通常ブラウザ＋download watcher

通常ブラウザで公式画面を開き、手動Export後のCSV検出・検証・配置をローカルscriptで
補助する。Playwrightの実機検証では外部セキュリティ検証が反復したため、その検証を
回避する変更は行わず、この方式は不採用とした。

対象:

- Spend
- Members
- Code Analytics
- CSVとして提供される管理画面Analytics

条件:

- 普段使用する通常ブラウザを使用する
- Organization選択、画面遷移、Exportは利用者が手動で行う
- scriptはdownload directoryだけを監視する
- CSVのheaderを検証してから対象Organizationへ配置する
- ID、パスワード、Cookieをリポジトリへ保存しない
- MFAを回避しない
- 非公開APIを直接呼ばない
- シート変更やcredit変更は行わない
- 失敗時は手動ダウンロードへフォールバックする

### 8.2 管理画面数値

CSVがない場合の優先順位:

1. 表をコピーしてCSV保存する
2. 月1回、小さなCSVへ手入力する
3. スクリーンショットは証跡として保存する

スクリーンショットのOCRを主要な数値入力経路にしない。

### 8.3 5分以内の運用

定常運用では、次のコマンド実行と結果確認だけを目標にする。

```sh
uv run seat-analyzer collect --org <org>
uv run seat-analyzer review --org <org> --month YYYY-MM
```

一度だけ必要:

- 管理画面へのログイン
- Claude組織とGitHub Organizationの対応設定
- Claude emailとGitHub loginの対応表
- 契約情報

変更時だけ必要:

- 契約条件
- 自動取得できないcredit設定
- 新規メンバーのGitHub login

## 9. P1: GitHub PR分析

### 9.1 対象

Claude組織ごとに1つのGitHub Organizationを設定する。

```yaml
organizations:
  example:
    github_org: example-org
```

リポジトリを手動列挙しない。

### 9.2 収集

- Organization内の参照可能な全リポジトリ
- Archive、Fork、Templateを除外
- Bot・GitHub App作成PRを除外
- 初回は直近90日
- 以後は増分取得
- ソースコード本文、diff本文、PR本文は取得しない
- `repository + PR number`を一意キーにする
- 新規リポジトリを次回実行時に自動検出する
- 権限不足と取得件数をData Doctorへ表示する

### 9.3 指標

| 指標 | 定義 |
|---|---|
| マージPR数 | 対象月にマージされたBot以外のPR |
| PRリードタイム | PR作成からマージまで |
| 代表値 | median、P75、P90 |
| 月への帰属 | マージ月 |

Draft期間は初期実装では除外しない。

### 9.4 利用者対応

`email,github_login`対応表を一度作成する。未対応ユーザーはGitHub集計から除外し、
Data Doctorで件数を表示する。

## 10. P1: Code中心のプロダクト分析

### 10.1 プロダクトポリシー

```yaml
product_policy:
  primary:
    - Claude Code
  supplementary:
    - Chat
    - Cowork
    - Design
    - Research
    - Code Review
    - Claude in Slack
  prohibited: []
```

`prohibited`は導入組織のポリシーに応じて設定する。この公開例では特定組織の
セキュリティ方針を含めず、空配列とする。実際のproduct名との差異はaliasで吸収する。

### 10.2 指標

- Code利用ユーザー数
- Code需要・トークン・リクエスト
- Code利用率
- Code低利用者
- 全プロダクト需要
- プロダクト別アクティブユーザー
- プロダクト別リクエスト・トークン・費用
- 複数プロダクト利用率
- 禁止機能の観測警告

Cowork、Design等は利用可視化までとし、成果分析を行わない。

## 11. P2: Billingと購入席

目的は削減額の最大化ではなく、割当と契約を混同しないことである。

取得する情報:

- Standard/Premium購入席数
- Standard/Premium割当席数
- 未割当購入席数
- 月払い・年払い
- 更新日
- 契約単価
- 請求PDF

表示を分離する。

- 割当上のシート変更
- Premiumの再配置余地
- 追加購入の必要性
- 購入数変更時の請求影響

Billing情報がない場合もシート候補は出せるが、実請求への影響は`unknown`とする。

## 12. 将来候補

### 12.1 OpenTelemetry

現在は次の理由で実装しない。

- 開発者端末の設定一括配布を前提にできない
- 組織内に収集endpointがない
- 外部endpointへの送信を現時点で前提にしない
- 小規模パイロットも現時点では行わない

将来、収集基盤と運用合意が整った場合に以下を検討する。

- effort
- speed
- main/subagent
- session
- active time
- edit accept/reject
- error/retry/compaction
- skill/plugin/MCP
- 5時間窓・週次需要

### 12.2 開発成果の拡張

- Jira
- GitHub Actions
- CI/CD
- deployment
- DORA
- Incident

担当基盤と取得権限が確認できるまで実装しない。

### 12.3 Enterprise Analytics API

Team運用で公式に取得できるCSV・管理画面情報を優先する。横断APIが必要になるまでは
Enterprise前提の実装を行わない。

## 13. 追加データ

### 現行スコープ

| データ | 取得 | 用途 |
|---|---|---|
| Spend CSV | 週次 | 需要、モデル、product、実課金 |
| Members CSV | 週次 | シート、変更履歴、メンバー変化 |
| Code Analytics CSV | 週次 | Code利用、LoC等 |
| credit設定 | 月次または変更時 | Standard/Premium/credit比較 |
| 管理画面Analytics | 月次、可能な範囲 | 補足的な利用可視化 |
| GitHub PR metadata | 自動 | PR数、PRリードタイム |
| email/GitHub login対応 | 初回・変更時 | ユーザー結合 |
| 契約・購入席 | 初回・変更時 | 割当と購入の分離 |

### 将来

| データ | 用途 |
|---|---|
| OpenTelemetry | effort、session、agent、摩擦 |
| Jira | チケットリードタイム |
| CI/CD | 品質、デプロイ |
| Incident | 変更失敗・回復 |

## 14. 週次・月次オペレーション

### 週次

```text
公式CSVを取得・配置
→ 入力品質検査
→ 利用急増・停止・credit接近を確認
→ Members差分からシート変更を検出
→ 前回推奨との一致・不一致を表示
```

### 月次

```text
対象月を確定
→ GitHub PR metadataを増分取得
→ Standard/Premium/credit候補を生成
→ 変更後2/4/8週を評価
→ Code中心の活用状況を確認
→ 変更担当者が管理画面で実施
```

## 15. 実装ロードマップ

### Phase 0: 実機検証

- 通常ブラウザとdownload watcherで公式CSVを取得補助できるか
- seat、creditの小さな手入力CSVを5分以内で作成できるか
- `gh`で対象Organizationの全PRを取得できるか
- 権限・件数・レート制限を確認できるか

### Phase 1: シート判断の基盤

- Spend/Membersのstable ID・追加列
- Data Doctor
- product policy
- シート変更履歴
- アップグレード1か月、ダウングレード2か月
- Standard/Premium/credit比較

### Phase 2: 独立レビュー

- Recommendation Evidence
- 変更直前の推奨との照合
- 変更後2/4/8週評価
- 週次・月次reviewレポート

ここまでを最初の実用リリースとする。

### Phase 3: 収集補助

- 通常ブラウザ＋download watcher
- 手動フォールバック
- 取得manifest・checksum
- 管理画面補足値

### Phase 4: GitHub

- Organization全体の自動取得
- email/login対応
- マージPR数
- PRリードタイム
- 変更前後の参考表示

### Phase 5: Billing・表示改善

- 購入席・契約
- 請求影響
- 経営向けダッシュボード
- Code中心のプロダクト可視化

### Future

- OpenTelemetry
- Jira
- CI/CD
- DORA
- Enterprise Analytics API

## 16. 成功指標

### 活用

- Code利用率
- Code需要・利用頻度
- Standard→Premium後の利用増加率
- Premium→Standard後の利用維持率
- usage credit問題の軽減率
- 低利用Premium比率
- 高需要Standard比率

### 意思決定

- 推奨と実変更の一致率
- 推奨なし変更の割合
- 変更後に期待方向へ動いた割合
- ダウングレード後の再アップグレード率
- アップグレード後も低利用だった割合
- データ不足で評価不能だった割合

### 運用

- 週次収集の成功率
- 取得補助成功率
- 手動フォールバック率
- 月次追加作業時間
- GitHub取得カバレッジ
- email/login対応率

### 参考

- マージPR数
- PRリードタイムmedian/P75/P90

PR指標をシート判断の正解ラベルにはしない。

## 17. ガバナンス

- システムはread-onlyとし、管理画面を変更しない
- 判断者個人ではなく、意思決定を評価する
- プロンプト、レスポンス、ソースコード本文、diff本文を取得しない
- 個人データはローカルで限定された利用者だけが閲覧する
- 生データとレポートをGitに含めない
- browser profile、Cookie、認証情報をGitに含めない
- GitHubはmetadataだけを取得する
- 取得できない値を0として扱わない
- データ不足時は判定保留にする

## 18. 避けるべき実装

- リポジトリを手動で列挙・維持する
- シート変更履歴を担当者の手入力に依存する
- browser helperから管理画面を操作する
- スクリーンショットOCRを主要な数値入力にする
- 月次API等価需要を5時間・週次上限の真値とする
- 非Code利用だけでCode高活用と判定する
- PR数を個人生産性や判定の正解とする
- OTelを現行ロードマップの前提にする
- 非公開APIへ依存する
- 追加の月次手作業を常態化する

## 19. 公式参考情報

- [Claude pricing](https://claude.com/pricing)
- [What is the Team plan?](https://support.claude.com/en/articles/9266767-what-is-the-team-plan)
- [Purchase and manage seats on Team plans](https://support.claude.com/en/articles/12004354-purchase-and-manage-seats-on-team-plans)
- [How is my Team plan bill calculated?](https://support.claude.com/en/articles/9267289-how-is-my-team-plan-bill-calculated)
- [Manage usage credits for Team and seat-based Enterprise plans](https://support.claude.com/en/articles/12005970-manage-usage-credits-for-team-and-seat-based-enterprise-plans)
- [View usage analytics for Team and Enterprise plans](https://support.claude.com/en/articles/12883420-view-usage-analytics-for-team-and-enterprise-plans)
- [Claude Code Analytics](https://code.claude.com/docs/en/analytics)
- [Claude Code OpenTelemetry](https://code.claude.com/docs/en/monitoring-usage)
- [GitHub CLI: search pull requests](https://cli.github.com/manual/gh_search_prs)
- [GitHub CLI: list repositories](https://cli.github.com/manual/gh_repo_list)
- [GitHub API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
