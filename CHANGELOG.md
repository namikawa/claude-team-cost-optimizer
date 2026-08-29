# 変更履歴

## [未リリース]

### 変更

- dashboard / preview-dashboard の出力 HTML から CSS・JS のコメントを除去した

### 修正

- 速報の追加クレジットの上限到達見込みで、到達日を数値として確定できないと処理が失敗した

## [1.1.1] - 2026-08-22

### 追加

- 入出力ディレクトリの既定を `config.yaml` の `paths.input` / `paths.output` で指定可能にした
- 速報ダッシュボードに「詳細利用状況（観測値）」を追加した

### 変更

- 組織×月ディレクトリの成果物のファイル名に対象月と組織名を含める形へ変えた
  （`report.md` → `report-YYYYMM-<組織名>.md`。7 種すべてが対象）
- 旧名の成果物は自動で改名も削除もしない。記入済みの「## 考察」は旧名からも引き継ぐ
- dashboard のタブ構成を見直した（5 タブ・推奨アクションを要約先行に・「前提と注意」を専用タブへ）
- dashboard の一覧は最初から全行を表示する（行の折りたたみを廃止）

## [1.1.0] - 2026-08-18

### 追加

- 詳細資料 `details.md` を正式分析で常に生成する。`report.md` はサマリ・シート変更推奨・
  考察を読むための短い文書になった
- `usage-summary.csv` を正式分析で常に生成する
- product の分類設定 `config.yaml > product_policy` を追加した
- dashboard に「Codeと他プロダクトの需要（API換算）」と「組織内の分布（参考値）」を追加した

### 変更

- dashboard を刷新した（4 タブ構成・テーマ切替・列ソート・検索・判定フィルタ）
- メンバー一覧の採用規則を「対象月末に最も近いスナップショット」に変えた。過去の月を
  再実行すると採用ファイルが変わり、判定が変わることがある
- `doctor` は、対象月末の直後（7 日以内）のメンバー一覧を採用した場合に警告を出さなくなった

### 修正

- メンバー一覧が対象月に無い場合、対象月から最も遠いスナップショットが選ばれていた

## [1.0.0] - 2026-08-11

初回リリース。

### 追加

- シートコストの損益分岐分析（`analyze`）。「Standard + 従量課金」と「Premium」のコストを
  比較してシート変更を推奨する
- 出力は組織ごとに `reports/<組織名>/YYYY-MM/` へ 3 種類（`report.md`・`dashboard.html`・
  `recommendations.csv`）
- 速報モード（`analyze --preview`）。月の途中までのデータを月末ペースに換算して一次判断の
  ラベルを付ける
- 入力データの事前検査（`doctor`）
- 考察の自動執筆（`discuss` / `analyze --with-discussion`）
- 組織別レポートに他組織の情報が混ざらないことの機械的な検査
- 公開テキストの検査（`check-text`）
- マルチ組織。組織ごとに独立して分析し、複数組織を一括実行すると組織横断サマリも出力する
- 任意の `members-info.csv`（部署・チーム・職種・追加クレジット上限・備考）
- 前月からの変化のセクション
- スナップショット差分。月中の利用の伸び・停止と込み量の消化を検出する
- ワークスペースとプログラムの分離（`uv tool install` と `init` / `init-org`）
- macOS / Windows / Linux で動作確認済み

[1.1.1]: https://github.com/namikawa/claude-team-cost-optimizer/releases/tag/v1.1.1
[1.1.0]: https://github.com/namikawa/claude-team-cost-optimizer/releases/tag/v1.1.0
[1.0.0]: https://github.com/namikawa/claude-team-cost-optimizer/releases/tag/v1.0.0
