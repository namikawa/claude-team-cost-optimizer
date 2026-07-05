# claude-team-cost-optimizer

Claude Team プラン（Standard / Premium シート）のシート最適化分析ツール。

メンバーごとの利用実績（スペンドレポート）から API 換算コストを集計し、

- Premium なのに使っていない → Standard へダウングレード推奨
- Standard + 従量課金が Premium を超えそう → Premium へアップグレード推奨

を月次で判定・レポートします。ローカルマシン上で Claude Code から実行する想定です。

> 免責: 本ツールは Anthropic 非公式のコミュニティツールです。シート料金・モデル単価・
> スペンドレポートの仕様は変更される可能性があるため、利用前に `config.yaml` の単価
> （2026-07 時点の値）を最新の公式情報と照合してください。本ツールの分析結果に基づく
> 判断は利用者の責任で行ってください。

## セットアップ

```sh
uv sync
```

## 入力データの構成（複数組織対応）

組織（Team プランの workspace）ごとに `input/<組織名>/` を作り、その配下に
CSV を配置する。組織名はディレクトリ名がそのまま識別子になる（レポートの
タイトル・出力先に使われる）。

```
input/
  <組織名A>/
    spend/            spend_YYYY-MM.csv        （必須）
    members/          members_YYYY-MM.csv      （必須）
    code-analytics/   cc_YYYY-MM.csv           （任意）
    members-info.csv                           （任意）
  <組織名B>/
    ...
```

`members-info.csv` は部署・チーム・職種・備考をメールアドレスに紐づける任意の
マッピングファイル。組織ディレクトリ直下に固定ファイル名で置く（サブディレクトリ
ではなく、月情報も持たない手動メンテのファイル）。カラムは email（必須）・部署・
チーム・職種・備考で、`email` 以外はすべて空欄でよい。組織階層は部署 > チームだが、
部署とチームは別軸として扱うためどちらか一方だけの記入でもよい。日本語ヘッダ
（email,部署,チーム,職種,備考）と英語ヘッダ（email,department,team,role,note）の
どちらも使える。置くとレポートに部署列・チーム列・部署別サマリ・チーム別サマリ・
備考が追加され（データがある軸のみ）、無ければ従来どおり動作する。
兼務は部署・チームのセルを `;`（半角セミコロン）で区切って複数記載できる（例:
`基盤チーム; SREチーム`）。部署別・チーム別サマリでは兼務者を所属数で均等按分（1/n）
して計上するため、各サマリの縦合計は常に全体と一致する。

雛形は以下のコマンドで作成できる（`input/<組織名>/{spend,members,code-analytics}/` と
`reports/<組織名>/` をまとめて作る。複数指定可）:

```sh
uv run seat-analyzer init-org <組織名>
```

組織が1つだけの場合も同じ構成を推奨。旧レイアウト（`input/spend/` 直下）も
単一組織として引き続き動作する。

## 月次運用手順（毎月月初・組織ごとに実施）

> ⚠️ スペンドレポートは90日より前に遡れません。毎月必ずエクスポートしてください。

1. スペンドレポート（必須） — Owner / Primary Owner のみ
   - claude.ai 左下のイニシャル → Settings > Analytics（対象組織の workspace で）
   - 「How much is Claude costing?」セクション → Export spend report
   - 期間は Custom で前月1日〜末日 を指定
   - ダウンロードした CSV をそのままのファイル名で `input/<組織名>/spend/` に置く
2. メンバー一覧（必須）
   - 管理画面のメンバー管理からエクスポート（email とシート種別を含むもの）
   - そのまま `input/<組織名>/members/` に置く
   - エクスポートが無い場合は `email,seat_type` の2列 CSV（ファイル名に YYYY-MM を含める）を手動作成でも可
3. Claude Code 分析（任意・活用度分析用）
   - https://claude.ai/analytics/claude-code → Leaderboard → Export all users
   - そのまま `input/<組織名>/code-analytics/` に置く

ファイル名の解釈ルール（リネーム不要）:

- 期間付き（`...-2026-06-01-to-2026-06-30.csv`、アンダースコア区切りも可）は開始月を対象月とする。
  月をまたぐ期間のエクスポートはエラーになるため、月単位でエクスポートすること
- 日付のみ（`members-...-2026-07-05.csv`）はエクスポート日の月のスナップショットとして扱う
- 同一月にファイルが複数ある場合、期間が包含関係なら広い方を自動採用（members の
  スナップショットは最新日付を採用）し、警告を表示する。判断できない場合はエラー
- 期間が1ヶ月に満たないスペンドレポートを通常分析に使うと警告が出る（速報モードを案内）
4. 分析実行
   - Claude Code で `/seat-analysis` を実行（推奨。数値検証と考察執筆まで行う）
   - または CLI 直接実行:

   ```sh
   uv run seat-analyzer analyze                          # 全組織を一括分析（最新月）
   uv run seat-analyzer analyze --month YYYY-MM          # 月を指定
   uv run seat-analyzer analyze --org <組織名>           # 特定組織のみ（複数指定可）
   ```

5. 組織ごとに `reports/<組織名>/YYYY-MM/` に以下が生成される
   - `report.md` — 推奨テーブル + 感度分析 + 考察
   - `dashboard.html` — 経営層共有用ダッシュボード（自己完結 HTML）
   - `recommendations.csv` — スプレッドシート二次加工用

   複数組織を一括分析した場合は `reports/summary/YYYY-MM.md` に組織横断サマリ
   （組織別のシート費用・削減見込みと合計）も生成される

## 速報モード（部分月データでの一次判断）

導入直後の組織などで月初の正式分析を待たずにシート構成を確認したい場合、
月の途中までのスペンドレポート（例: 1日〜10日）を通常どおり
`input/<組織名>/spend/` に配置して実行する:

```sh
uv run seat-analyzer analyze --preview [--org <組織名>] [--days 10]
```

観測日数はファイル名の期間（`...-2026-07-01-to-2026-07-10.csv` なら10日）から
自動判別される。期間の無いファイル名の場合のみ `--days` で指定する。

- 出力は `reports/<組織名>/<月>/preview.md` のみ。変更推奨・ヒステリシス判定・
  正式レポート（report.md 等）には影響しない
- 需要を月末ペースに日割り換算し、遊休候補 / Standard候補 / Premium妥当 /
  判断保留 などの一次判断ラベルを付ける。境界付近は判断保留に倒す
- 日割り換算は利用の偏りを補正しない参考値。シート変更の確定判断は
  全月データ2ヶ月分の正式分析で行うこと
- 月初に全月分のエクスポートで同じファイルを上書きすれば、そのまま正式分析に移行できる

## 判定ロジック概要

ユーザ×月ごとに API 換算コスト `api_cost` を集計し、

```
cost_if_standard = $25  + max(0, api_cost − S_allowance)
cost_if_premium  = $125 + max(0, api_cost − P_allowance)
```

の安い方を推奨。ただし:

- allowance（シート込み利用量の USD 換算）は Anthropic 非公開のため、
  `config.yaml` の low / mid / high 3 シナリオで感度分析する（判定の主系は mid）
- ヒステリシス: 直近 2 ヶ月連続（`decision.hysteresis_months`）で同じ推奨、
  かつ削減見込みが差額 $100 の 20% 以上（`decision.buffer_ratio`）のときのみ「変更推奨」
- センサリング警告: 従量課金が無効な場合、Standard ユーザの観測利用量は上限で
  頭打ちになり真の需要を過小評価する。上限到達が疑われるユーザにはフラグを付ける
- シート未割当（Seat Tier: Unassigned）のメンバーは、意図的な未割当（別組織で
  アサイン済み・管理者等）として判定対象外にする。利用実績がある場合のみ警告

## allowance のキャリブレーション

数ヶ月分の実データが溜まったら:

- Standard ユーザの月次 `api_cost` の分布を確認し、上限到達（頭打ち）している
  ユーザの観測最大値 ≒ `S_allowance` として `config.yaml` を更新
- Premium は Standard の 5 倍程度（セッション倍率 1.25x vs 6.25x）を目安に設定

## 開発

```sh
uv run pytest              # テスト
uv run seat-analyzer analyze --input-dir examples/input --month 2026-06   # サンプル2組織でE2E
uv run seat-analyzer analyze --input-dir examples/input --org org-b       # 特定組織のみ
```
