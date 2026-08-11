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

## クイックスタート

Python 3.11 以上と [uv](https://docs.astral.sh/uv/) が必要。考察の自動執筆には
ローカルの Claude Code CLI も使う。ゼロから環境を作る手順は
[docs/setup.md](docs/setup.md) にある（Claude Code に読ませて実行させる形式）。

1. 依存をインストールする

   ```sh
   uv sync
   ```

2. 組織（Team プランの workspace）ごとにディレクトリの雛形を作る

   ```sh
   uv run seat-analyzer init-org <組織名>
   ```

   組織名はディレクトリ名がそのまま識別子になる。詳しくは
   [docs/usage.md](docs/usage.md) の入力データの構成。

3. claude.ai からエクスポートした CSV を `input/<組織名>/` 配下に置く。
   スペンドレポートとメンバー一覧が必須、Claude Code 分析は任意。エクスポート手順と
   ファイル名の解釈ルールは [docs/usage.md](docs/usage.md) の月次運用手順。

4. 分析を実行する

   ```sh
   uv run seat-analyzer analyze --month YYYY-MM                    # 分析のみ
   uv run seat-analyzer analyze --month YYYY-MM --with-discussion  # 考察の執筆まで
   ```

   `analyze` 単体では report.md の「## 考察」は未記入のまま出力される。執筆には
   ローカルの Claude Code CLI を使う（[docs/tooling.md](docs/tooling.md)）。
   Claude Code から `/seat-analysis` を実行すると、分析に加えて警告の検証と考察の執筆までを
   対話的に行える。

## 生成されるもの

組織ごとに `reports/<組織名>/YYYY-MM/` へ出力される。

- `report.md` — 前月からの変化 + 推奨テーブル + 部署別/チーム別サマリ + 詳細利用状況 + 感度分析 + 考察
- `dashboard.html` — 経営層共有用ダッシュボード（自己完結 HTML）
- `recommendations.csv` — スプレッドシート二次加工用
- `reports/summary/YYYY-MM.md` — 複数組織を一括分析した場合の組織横断サマリ

各セクションの読み方は [docs/reference.md](docs/reference.md) を参照。

## ドキュメント

| ドキュメント | 内容 |
| --- | --- |
| [docs/setup.md](docs/setup.md) | ゼロから動く状態にするまでのセットアップ手順（Claude Code に実行させる形式） |
| [docs/usage.md](docs/usage.md) | 入力データの構成、CSV のエクスポート手順、月次運用、速報モード |
| [docs/reference.md](docs/reference.md) | レポート各セクションの読み方、追加クレジットの上限、判定ロジックの前提 |
| [docs/tooling.md](docs/tooling.md) | 考察の自動執筆（`discuss`）と公開テキストの検査（`check-text`） |

一覧は [docs/README.md](docs/README.md) にある。

## 開発

```sh
uv run pytest              # テスト
uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --month 2026-06   # サンプル2組織でE2E
uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --org org-b       # 特定組織のみ
```
