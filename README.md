# claude-team-cost-optimizer

Claude Team プラン（Standard / Premium シート）のシート最適化分析ツール。

メンバーごとの利用実績（スペンドレポート）から API 換算コストを集計し、

- Premium なのに使っていない → Standard へダウングレード推奨
- Standard + 従量課金が Premium を超えそう → Premium へアップグレード推奨

を月次で判定・レポートします。ローカルマシン上で実行し、分析（`analyze`）は手元で完結します。
考察の自動執筆（`discuss`）を使う場合のみ、レポートの内容が Claude（Anthropic）へ
送信されます。

> 免責: 本ツールは Anthropic 非公式のコミュニティツールです。シート料金・モデル単価・
> スペンドレポートの仕様は変更される可能性があるため、利用前に既定設定の単価
> （2026-07 時点の値）を最新の公式情報と照合してください。本ツールの分析結果に基づく
> 判断は利用者の責任で行ってください。

## 動作環境

macOS / Windows / Linux で動作確認済み。必須なのは [uv](https://docs.astral.sh/uv/) のみで、
Python（3.11 以上）は uv が用意します。レポートの考察を自動執筆する機能を使う場合は
ローカルの Claude Code CLI も必要です。

## インストール

[Releases](https://github.com/namikawa/claude-team-cost-optimizer/releases) で最新リリースに
添付された wheel（`seat_analyzer-X.Y.Z-py3-none-any.whl`）の URL を確認し、それを指定して
インストールします。git の入っていないマシンでも導入できます。

```sh
uv tool install "seat-analyzer @ <wheel の URL>"
seat-analyzer --version
```

アップデートは新しいリリースの wheel の URL で同じコマンドを実行し直すだけです。ゼロから
環境を作る手順は [docs/setup.md](docs/setup.md) にあります（Claude Code に読ませて実行させる
形式）。

## 使い方

1. データと設定を置くワークスペースを任意の場所に作る

   ```sh
   mkdir ~/claude-seat-analysis && cd ~/claude-seat-analysis
   seat-analyzer init
   ```

   `input/`・設定の上書きファイル `config.yaml`・`.gitignore` が作られます。設定は
   パッケージ同梱の既定に対して差分だけを書く形式なので、`config.yaml` は空のままでも
   動きます。以降のコマンドはこのディレクトリで実行します。CSV とレポートを別の場所に
   置く場合は `config.yaml` の `paths.input` / `paths.output` に書きます（コマンドごとに
   `--input-dir` / `--output-dir` を付けても指定できます）。

2. 組織（Team プランの workspace）ごとにディレクトリの雛形を作る

   ```sh
   seat-analyzer init-org <組織名>
   ```

   組織名はディレクトリ名がそのまま識別子になります。詳しくは
   [docs/usage.md](docs/usage.md) の入力データの構成。

3. claude.ai からエクスポートした CSV を `input/<組織名>/` 配下に置く。
   スペンドレポートとメンバー一覧が必須、Claude Code 分析は任意。エクスポート手順と
   ファイル名の解釈ルールは [docs/usage.md](docs/usage.md) の月次運用手順。

4. 分析を実行する

   ```sh
   seat-analyzer analyze --month YYYY-MM                    # 分析のみ
   seat-analyzer analyze --month YYYY-MM --with-discussion  # 考察の執筆まで
   ```

   `analyze` 単体ではレポートの「## 考察」は未記入のまま出力されます。執筆には
   ローカルの Claude Code CLI を使います（[docs/tooling.md](docs/tooling.md)）。

## 生成されるもの

組織ごとに `reports/<組織名>/YYYY-MM/` へ出力されます。ファイル名は
`{種別}-{YYYYMM}-{組織名}.{拡張子}` で、共有でフォルダの外へ出しても
どの組織のいつの分析かが分かるようになっています。

- `report-YYYYMM-<組織名>.md` — サマリ + 前月からの変化 + 追加クレジット付与候補 + シート変更推奨 + 注意事項 + 警告 + 考察
- `details-YYYYMM-<組織名>.md` — 全ユーザ + 部署別/チーム別サマリ + 詳細利用状況 + 組織内の分布 + 月中の推移 + 感度分析（機械生成の詳細資料）
- `dashboard-YYYYMM-<組織名>.html` — 経営層共有用ダッシュボード（概要 / 推奨アクション / メンバー別 / 組織 / 前提と注意 の5タブ。ソート・検索・テーマ切替つきの自己完結 HTML）
- `recommendations-YYYYMM-<組織名>.csv` — スプレッドシート二次加工用
- `usage-summary-YYYYMM-<組織名>.csv` — ユーザ単位の product 利用特徴量（全 product と Claude Code の需要・リクエスト数など。確定できない値は空欄）
- `decision-evidence-YYYYMM-<組織名>.csv` — V2 判定の根拠（`--decision-version v2` のときだけ出力。V1 の判定・成果物は変わりません）
- `reports/summary/YYYY-MM.md` — 複数組織を一括分析した場合の組織横断サマリ（この名前は変わりません）

速報モード（`--preview`）は `preview-YYYYMM-<組織名>.md` と
`preview-dashboard-YYYYMM-<組織名>.html` を出します。

各セクションの読み方は [docs/reference.md](docs/reference.md) を参照。

## ドキュメント

| ドキュメント | 内容 |
| --- | --- |
| [docs/setup.md](docs/setup.md) | ゼロから動く状態にするまでのセットアップ手順（Claude Code に実行させる形式） |
| [docs/usage.md](docs/usage.md) | 入力データの構成、CSV のエクスポート手順、月次運用、速報モード |
| [docs/reference.md](docs/reference.md) | レポート各セクションの読み方、追加クレジットの上限、判定ロジックの前提 |
| [docs/tooling.md](docs/tooling.md) | 考察の自動執筆（`discuss`）と公開テキストの検査（`check-text`） |
| [CHANGELOG.md](CHANGELOG.md) | バージョンごとの変更履歴 |

一覧は [docs/README.md](docs/README.md) にある。

## 開発

リポジトリを clone して開発環境を作る。

```sh
uv sync
uv run pytest              # テスト
uv run ruff check .        # lint
```

開発時は `uv run seat-analyzer ...` で実行する（`uv tool install` で入れたものとは別の環境）。
サンプル 2 組織での E2E は出力先を分ける。

```sh
uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --month 2026-06   # サンプル2組織でE2E
uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --org org-b       # 特定組織のみ
uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --org org-b --month 2026-08   # 条件つきセクションが全部出る月
```

設定の既定値は `src/seat_analyzer/default-config.yaml`（単価・カラムエイリアス・閾値）。
リリース手順は [docs/release.md](docs/release.md)。
