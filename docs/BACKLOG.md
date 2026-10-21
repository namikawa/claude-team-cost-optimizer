# 実施検討候補

最終更新: 2026-10-22

実施の検討から始めるべき項目を記録する。未採用の項目は実施要否の検討待ちであり、実装や納期を確約しない。優先度は調査順の仮置きとし、検証結果に応じて見直す。

リリース計画の正典は [実装ステータス](./roadmap/implementation-status.md) とする。この BACKLOG は実装検討前の候補と採否判断の記録を管理し、採用した項目の実装計画や進捗は正典へ反映する。採否判断では、実施または見送りとその理由を残し、実施する場合は最小範囲と受入条件も定める。

## 1. P1候補: ブラウザー拡張のダウンロード取り違え対策の要否

- 状態: 対策を採用（2026-10-21）。JSON保存は合成環境の実機受入済み（2026-10-22）。CSVの対応方式は別段階で調査する
- 対象: [run.js](../src/seat_analyzer/browser_extension/run.js) の `trackDownload`、[background.js](../src/seat_analyzer/browser_extension/background.js) の `onDeterminingFilename`、[claude_export.py](../src/seat_analyzer/claude_export.py)
- 採用理由: 専用Chromeと合成データでCSVの先着取り違え・3秒の後処理を超えた遅着の取り違えを再現し、JSON保存中も別件CSVの保存先が変わった。誤採用されたcode CSVを現行CLIは拒否できなかった。証拠と未確認点は[検証記録](./download-probe.md)に残す。実サービスでの発生条件・頻度は未確認。
- 最小範囲: JSONの保存先を直接指定し、Blob URLごとの独立した登録と自拡張ID・URLの完全一致で固定名を決め、返却IDだけを追跡する。保存完了時に期待パスとの一致を確認し、登録とBlob URLを片付ける。JSON用の共有routingとadoptによるID変更を廃止する。CSVの先着採用・再試行方式は今回の対象外。
- 受入条件: 別件先着や検索応答順に影響されずJSONの返却IDだけで完了すること、通常のパス・内容・同名上書きを維持すること、JSON保存が並行CSVの保存先を変えないこと、成功・失敗時にlistener・timer・Blob URLを片付けること。進捗と残確認は[実装ステータス](./roadmap/implementation-status.md)で管理する。
- CSV後続: 対象操作とダウンロードを一意に対応付ける方法を実サービスで調べてから方式を決める。調査と本番修正は別段階とする。

## 2. P1候補: URL で起動する拡張機能操作の信頼境界の確認

- 状態: 実施要否の検討待ち
- 対象: [manifest.json](../src/seat_analyzer/browser_extension/manifest.json) の `run.html` 公開設定、[background.js](../src/seat_analyzer/browser_extension/background.js) の `startRun`、[run.js](../src/seat_analyzer/browser_extension/run.js) の `runLogin`・`main`、[trigger.js](../src/seat_analyzer/browser_extension/trigger.js)
- 根拠と未確認点: URL 由来の指示から、ログイン用 Cookie の削除を含む操作へ到達する構造がある。実ブラウザーで外部ページから起動できる条件と利用者への影響は未確認。
- 最初の検討・検証: 合成データと専用プロファイルで到達経路、必要な利用者操作、CLI 連携を確認し、対策が必要なら実行前の明示確認などを比較する。公開範囲の制限だけでなく、`trigger.js` を経由する起動も含めて検証する。
- 採否判断時に残す結論: 対策の実施または見送りと理由。実施する場合は最小範囲を定め、想定する利用者の意思確認と既存の CLI 連携を受入条件に含める。

## 3. P3候補: 混入チェックの収集結果再利用の要否

- 状態: 実施要否の検討待ち
- 対象: [leakcheck.py](../src/seat_analyzer/leakcheck.py) の `forbidden_terms`・`_csv_paths`・`_group_terms`
- 根拠と未確認点: `forbidden_terms` の CSV 列挙と `_group_terms` の内部で同じ入力ツリーを再走査している。実行時間に占める I/O 負荷と、収集結果の再利用による改善効果は未計測。
- 最初の検討・検証: 合成データでファイル数や階層を変えて走査の負荷を実測し、再利用の効果と変更コストから必要性を判断する。
- 採否判断時に残す結論: 再利用の実施または見送りと理由。実施する場合は最小範囲を定め、収集結果の順序、例外処理、収集不能時に安全側へ停止する性質の維持を受入条件に含める。

## 4. P3候補: ブラウザー拡張の動作テスト追加の要否

- 状態: 実施要否の検討待ち
- 対象: [tests/test_browser_extension.py](../tests/test_browser_extension.py)、[tests/test_claude_export.py](../tests/test_claude_export.py)、[browser_extension/](../src/seat_analyzer/browser_extension/)
- 根拠と未確認点: 既存の静的検査やモックだけでは、実ブラウザーのイベントや権限を伴う重要操作を十分に保護できるか未確認。追加すべき最小範囲と CI・保守コストは未評価。
- 最初の検討・検証: 既存テストが保護する範囲を整理し、合成データで実アカウントを使わず検証できる重要操作と最小のブラウザーテスト範囲を検討する。実行環境、CI への組み込み、保守コストも比較する。
- 採否判断時に残す結論: テスト追加の実施または見送りと理由。実施する場合は対象操作と最小範囲を定め、実アカウント不要での再現性と継続運用できる実行コストを受入条件に含める。

## 5. P3候補: 静的型チェックの段階導入の要否

- 状態: 実施要否の検討待ち
- 対象: [pyproject.toml](../pyproject.toml)、入出力境界の例として [ingest.py](../src/seat_analyzer/ingest.py)・[claude_export.py](../src/seat_analyzer/claude_export.py)・[report/](../src/seat_analyzer/report/)
- 根拠と未確認点: 型チェッカーは現状未設定。対象を絞った適用で得られる利益、既存の型エラー、維持コストは未調査。
- 最初の検討・検証: 入出力境界の限定した範囲で検出内容と対応量を調べ、導入の利益と維持コストを比較する。依存追加や DataFrame の全面的な型変更は、この候補の記録では確定しない。
- 採否判断時に残す結論: 段階導入の実施または見送りと理由。実施する場合は対象と最小範囲を定め、検出したい誤り、既存エラーの扱い、継続確認の方法を受入条件に含める。
