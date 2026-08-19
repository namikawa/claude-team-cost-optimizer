# Claude Team シート速報プレビュー — org-a — 2026-06

10日間の観測データ（2026-06、暦30日、月末ペース換算 ×3.0）に基づく一次判断です。
シート変更の確定判断には使わず、ヒアリング・観察対象の絞り込みに使ってください。

## サマリ

| 指標 | 値 |
|---|---|
| 対象メンバー数 | 12 名（Standard 5 / Premium 6 / 未割当 0 / 不明 1） |
| 現在のシート費用 | $875.00 /月 |
| 観測需要 → 月末ペース換算 | $1,545.50 → $4,636.50 |
| 一次判断の内訳 | 遊休候補 1 名 / Standard候補 3 名 / Premium検討 1 名 / シート不明 1 名 / Premium妥当 2 名 / Standard妥当 4 名 |
| 実課金発生 | 11 名 |

## 一次判断テーブル

| ユーザ | 現シート | 部署 | チーム | 観測需要(10日) | 月末ペース換算 | 実課金(観測) | 一次判断 | 確度 |
|---|---|---|---|---|---|---|---|---|
| ito@example.co.jp | Premium | コーポレート | 情シスチーム | $0.00 | $0.00 | $0.00 | 遊休候補 | — |
| yamamoto@example.co.jp | Premium | プラットフォーム開発部 | 基盤チーム; SREチーム | $30.00 | $90.00 | $30.00 ⚠️超過済 | Standard候補 | 高 |
| sato@example.co.jp | Premium | プロダクト開発部 | Webチーム | $24.00 | $72.00 | $24.00 ⚠️超過済 | Standard候補 | 高 |
| watanabe@example.co.jp | Premium | プロダクト開発部 | Webチーム | $12.00 | $36.00 | $12.00 ⚠️超過済 | Standard候補 | 高 |
| nakamura@example.co.jp | Standard | プロダクト開発部 | モバイルチーム | $335.00 | $1,005.00 | $335.00 ⚠️従量あり | Premium検討 | 高 |
| guest@example.co.jp | 不明 |  |  | $15.00 | $45.00 | $15.00 ⚠️従量あり | シート不明 | — |
| tanaka@example.co.jp | Premium | プラットフォーム開発部 | 基盤チーム | $610.00 | $1,830.00 | $610.00 ⚠️超過済 | Premium妥当 | 高 |
| suzuki@example.co.jp | Premium | プラットフォーム開発部 | 基盤チーム | $415.00 | $1,245.00 | $415.00 ⚠️超過済 | Premium妥当 | 高 |
| kobayashi@example.co.jp | Standard | プロダクト開発部 | モバイルチーム | $48.50 | $145.50 | $48.50 ⚠️従量あり | Standard妥当 | 中 |
| yamada@example.co.jp | Standard | プラットフォーム開発部 | SREチーム | $27.00 | $81.00 | $27.00 ⚠️従量あり | Standard妥当 | 高 |
| kato@example.co.jp | Standard | コーポレート | 情シスチーム | $18.00 | $54.00 | $18.00 ⚠️従量あり | Standard妥当 | 高 |
| yoshida@example.co.jp | Standard | コーポレート | デザインチーム | $11.00 | $33.00 | $11.00 ⚠️従量あり | Standard妥当 | 高 |

### 備考

- ito@example.co.jp: 2026-06 休職中・9月復帰予定
- yamamoto@example.co.jp: 2チーム兼務（兼務按分のデモ）
- sato@example.co.jp: 2026-06 ヒアリング済み: 7月からPJ利用予定

- 一次判断: 月末ペース換算需要を損益分岐モデル（allowance 3シナリオ）にかけた参考判定。
  境界付近（3シナリオ不一致 or 削減見込みがバッファ未満）は「判断保留」に倒しています
- 遊休候補: 観測期間中の利用がほぼゼロ。解約前にオンボーディング状況のヒアリングを推奨
- ⚠️超過済: Premium の込み量を観測期間中にすでに超過し実課金が発生（明確なヘビー層）
- ⚠️従量あり: Standard 等で従量課金が発生（Premium 検討の重要シグナル）
- 対象外（未割当）: 意図的にシートを割り当てていないメンバー（別組織でアサイン済み・管理者等）

## 注意事項

- 日割り換算（×3.0）は利用の偏り（曜日・導入直後の立ち上がり・プロジェクト山谷）を補正しません
- 実課金は込み量を使い切ってから発生する非線形な値のため、月末ペース換算していません
- 変更推奨・ヒステリシス判定は行いません。確定判断は全月データ2ヶ月分での正式分析（`analyze`）で行ってください

## データ検証・警告

- spend_2026-06.csv: 任意カラムなし: ['uncached_input_tokens', 'cache_read_tokens', 'cache_write_5m_tokens', 'cache_write_1h_tokens']
- members-info.csv に未登録のユーザ 1 名: ['guest@example.co.jp']（部署・チーム・職種が空欄で集計されるため追記を推奨）
- members に存在しない利用ユーザ 1 名（シート不明として集計）: ['guest@example.co.jp']

## 考察

<!-- /seat-analysis または seat-analyzer discuss --preview 実行時に Claude が記入するセクション -->
（未記入 — `/seat-analysis preview <日数>` または `seat-analyzer discuss --preview` を実行すると考察が追記されます）
