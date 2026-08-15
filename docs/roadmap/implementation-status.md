# 実装ステータス

- 最終更新: 2026-08-15
- 対象設計: [Claude利活用・シート適正化機能 実装設計書](./implementation-design.md)
- 次のタスク: Step 8D dashboardの再設計（着手前にデザイン方針の確認が必要）
- 現在のリリーススコープ: v1.1.0 = Track 2（Step 6〜8E）。設計書のMilestone Aがここで完了する

## 1. この文書の目的

実装設計書の仕様と、日々変わる進捗を分離して管理する。

- 実装内容・受け入れ条件: `implementation-design.md`
- 着手・完了・ブロック状況: 本文書
- 機能の背景・優先順位: `claude-adoption-cost-management-proposal.md`

実装設計を変更せずに、各Stepの進捗と検証結果を更新できるようにする。

## 2. ステータス

| ステータス | 意味 |
|---|---|
| `未着手` | 作業を開始していない |
| `進行中` | 現在作業中 |
| `ブロック` | 外部条件・判断待ちで進められない |
| `完了` | 受け入れ条件と必要な検証を満たした |
| `見送り` | 検証結果または方針により実装しない |

`条件付き完了`は使用しない。制約を発見すること自体が目的のFeasibility Stepは、
制約と対応方針を検証記録へ残したうえで`完了`とする。

## 3. 更新ルール

### 着手時

1. 対象Stepを`進行中`へ変更する
2. 同時に複数Stepを`進行中`にしない
3. 依存Stepが`完了`していることを確認する

### 完了時

1. 設計書の受け入れ条件をすべて確認する
2. 必要なテスト・smoke testを実行する
3. 本文書を`完了`へ更新する
4. 完了日と検証結果を「検証記録」へ追記する
5. 次のタスクを更新する

### ブロック時

1. ステータスを`ブロック`へ変更する
2. ブロック理由を検証記録へ記載する
3. 解消条件を明記する
4. 独立して進められる次Stepがなければ作業を止める

### 公開情報

本文書には次を記載しない。

- 実在するOrganization名
- 実在するrepository名
- メンバー名・メール
- 実利用金額
- 組織ごとの実人数・実件数
- token、Cookie、認証情報

検証結果は、実装判断に必要な内容へ一般化して記載する。

## 4. 進捗サマリ

| Track | 内容 | 完了 | 進行中 | ブロック | 未着手 | 見送り |
|---|---|---:|---:|---:|---:|---:|
| 0 | 実機Feasibility | 3 | 0 | 0 | 0 | 0 |
| 1 | 入力と品質 | 5 | 0 | 0 | 0 | 0 |
| 2 | Code中心の利用可視化 | 3 | 0 | 0 | 2 | 0 |
| 3 | シート変更履歴 | 0 | 0 | 0 | 4 | 0 |
| 4 | V2判定 | 0 | 0 | 0 | 7 | 0 |
| 5 | 変更後評価 | 0 | 0 | 0 | 5 | 0 |
| 6 | Browser-assisted取得 | 0 | 0 | 0 | 7 | 0 |
| 7 | GitHub | 0 | 0 | 0 | 8 | 0 |
| 8 | Billingと表示 | 0 | 0 | 0 | 3 | 0 |
| **合計** |  | **11** | **0** | **0** | **36** | **0** |

## 5. Step一覧

### Track 0: 実機Feasibility

| Step | タスク | ステータス | 完了日 | 備考 |
|---|---|---|---|---|
| 0A | GitHub認証の手動smoke test | `完了` | 2026-07-30 | 大規模Organization向けの分割取得要件を確認 |
| 0B | Playwright管理画面smoke test | `完了` | 2026-07-30 | 外部セキュリティ検証が反復するため不採用 |
| 0C | 通常ブラウザ＋download watcher smoke test | `完了` | 2026-07-30 | 実機でSpend CSVの検出・一時コピーに成功 |

### Track 1: 入力と品質

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 1 | Spend任意カラム | `完了` | 2026-07-30 |
| 2 | Members任意カラム | `完了` | 2026-07-30 |
| 3 | subject_id | `完了` | 2026-07-30 |
| 4 | QualityIssue | `完了` | 2026-08-01 |
| 5 | doctorの既存入力検査 | `完了` | 2026-08-04 |

### Track 2: Code中心の利用可視化

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 6 | product policy config | `完了` | 2026-08-14 |
| 7 | Code/全product特徴量 | `完了` | 2026-08-15 |
| 8 | usage-summary.csv | `完了` | 2026-08-15 |
| 8D | dashboardの再設計 | `未着手` |  |
| 8E | dashboardへのproduct軸の掲載 | `未着手` |  |

### Track 3: シート変更履歴

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 9 | SeatChangeEvent | `未着手` |  |
| 10 | seat-change-events.csv | `未着手` |  |
| 11 | decision snapshot保存 | `未着手` |  |
| 12 | 推奨と実変更の照合 | `未着手` |  |

### Track 4: V2判定

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 13 | V2 domain | `未着手` |  |
| 14 | asymmetric history config | `未着手` |  |
| 15 | Upgrade rule | `未着手` |  |
| 16 | Downgrade rule | `未着手` |  |
| 17 | Admin credit loader | `未着手` |  |
| 18 | Credit comparator | `未着手` |  |
| 19 | decision-evidence.csv | `未着手` |  |

### Track 5: 変更後評価

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 20 | UsageInterval | `未着手` |  |
| 21 | 2週間評価 | `未着手` |  |
| 22 | 4週間評価 | `未着手` |  |
| 23 | 8週間評価 | `未着手` |  |
| 24 | review.md | `未着手` |  |

### Track 6: Browser-assisted取得

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 25 | Download watcher基盤 | `未着手` |  |
| 26 | Spend検出・配置 | `未着手` |  |
| 27 | Members検出・配置 | `未着手` |  |
| 28 | Code Analytics検出・配置 | `未着手` |  |
| 29 | Collection manifest | `未着手` |  |
| 30 | collect CLI | `未着手` |  |
| 31 | Admin credit入力補助 | `未着手` |  |

### Track 7: GitHub

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 32 | GitHub mapping loader | `未着手` |  |
| 33 | GitHub doctor | `未着手` |  |
| 34 | Repository自動発見 | `未着手` |  |
| 35 | PR検索とraw cache | `未着手` |  |
| 36 | マージPR数 | `未着手` |  |
| 37 | PR lead time | `未着手` |  |
| 38 | github-summary.csv | `未着手` |  |
| 39 | GitHub follow-up参考表示 | `未着手` |  |

### Track 8: Billingと表示

| Step | タスク | ステータス | 完了日 |
|---|---|---|---|
| 40 | 購入席loader | `未着手` |  |
| 41 | 割当と購入の分離 | `未着手` |  |
| 42 | Dashboard統合 | `未着手` |  |

## 6. 検証記録

### 2026-08-15 — Step 8 usage-summary.csv

ステータス: `完了`

実装したこと:

- `src/seat_analyzer/report/usage_csv.py` を新設し（`report/` 配下なので層30の単位内・
  `LAYERS` の変更なし）、通常の `analyze` の成果物として `usage-summary.csv` を常に生成する。
  opt-in のフラグは設けず、`write_all` が recommendations.csv と同じディレクトリへ書く
- 内容は `AnalysisResult.product_usage.features` に保持済みの値そのもの。行の追加・削除・
  再計算をしない（Step 7 の受け入れ条件のとおり、Spend の再読込・再価格計算をしない）。
  行の範囲は対象月のスペンド明細に現れたユーザで、利用ゼロのメンバーは行を持たない。
  この対象範囲はモジュールの docstring に明記した
- 列は `email` + `FEATURE_COLUMNS`（列順の唯一の源は `product_usage`）。書式は金額が小数2桁、
  構成比が小数4桁、回数は整数で表せる値は整数表記・表せない値は repr で桁を落とさない、
  真偽値は recommendations.csv と同じ `True`/`False`。確定できなかった値（欠損）は空欄にする。
  0 や `False` で埋めると「観測した結果が 0 だった」と区別できなくなるため
- 式のエスケープ（formula injection 対策）と改行の正規化は email 列にだけ適用する。数値から
  自前で組み立てた文字列に掛けると、負の金額の `-` が式の先頭文字と一致して引用符が付き
  値が壊れる。ヘルパは csv_out の公開名へ昇格して2つの CSV 出力で共有した
- `product_usage` を持たない分析結果は `ValueError` にする（「常に生成する」の fail-loud。
  黙ってスキップしない）
- prohibited product warning: `analyze` 実行時の警告ブロックに `PROHIBITED_PRODUCT_OBSERVED`
  の message を表示する。宛先は分析の実行者なので、共有物であるレポートには載せない
  （レポートの内容が判定に使う値だけで決まることを保ち、既存出力のバイト一致を構造的に守る）。
  `CAPACITY_SIGNAL_UNAVAILABLE` は表示しない（Step 7 の裁定どおり、特徴量が空欄になることで
  出力側から見える）
- 速報モード（`write_preview`）は変更しない。`product_usage.py`・`analyze/`・markdown・html の
  各出力も変更なし

確認したこと:

- 既存出力がバイト一致で不変（golden の追跡ファイルに diff ゼロ。追加は正式分析 3 ケースの
  usage-summary.csv のみで、preview 系ツリーには増減なし）
- 列構成と書式（金額・構成比・整数/非整数の回数・真偽値）、欠損の空欄、email の式エスケープ、
  負の金額がエスケープで壊れないこと、BOM + LF、明細に誰も現れない月のヘッダのみのファイル
- product 列の無い入力では、product 名に依らず確定する列（全 product の需要・回数）だけが
  値を持ち、Code 系の列は空欄になる
- 禁止 product の利用行があると実行時に警告が出て、同じ設定でも観測が無ければ出ないこと
- テストが組み立てる features の dtype が `compute` の出力と一致すること（型の表を private に
  依存せず持ち、ズレたら落ちる）
- 生成した usage-summary.csv が CLI の出力一覧に載り、内容が分析結果の特徴量と一致すること

コード・ファイル変更:

- `src/seat_analyzer/report/usage_csv.py`（新規）
- `src/seat_analyzer/report/__init__.py`
- `src/seat_analyzer/report/csv_out.py`
- `src/seat_analyzer/cli.py`
- `README.md`・`docs/usage.md`・`docs/setup.md`（成果物リストへ追加）
- `tests/test_usage_csv.py`（新規）
- `tests/test_cli.py`
- `tests/golden/`（usage-summary.csv を 3 ケースへ追加）

テスト:

- `uv run ruff check .`
- `uv run pytest`（601件成功。追加前は583件）
- 追跡ファイルの差分と未追跡の新規ファイルをあわせて `check-text --diff` に通し、
  業務情報の混入なしを確認

### 2026-08-15 — Step 7 Code/全product特徴量

ステータス: `完了`

実装したこと:

- `src/seat_analyzer/product_usage.py`（層15）を新設し、設計書§9.2の8特徴量を計算する
  `compute(spend_df, policy) -> ProductUsage` を実装した。`ProductUsage`は
  `features`（index=email・8列）と`issues`（構造化品質issue）を持つ
- policyは引数で受け取り、`config`をimportしない。DataFrameとpolicyだけで結果が決まる
  純粋な計算に保った
- product名の照合は正規化（前後空白の除去 → NFC → casefold）後の完全一致にした。
  部分一致・あいまい一致は実装しない。表記ゆれはpolicyに名前を並べて吸収する設計とし、
  その判断をモジュールのdocstringへ書いた
- `prohibited`は分類と直交する属性なので、禁止指定されたsupplementary productの需要も
  `supplementary_high`の集計に含める。禁止productの行があれば`prohibited_observed`を真に
  して`PROHIBITED_PRODUCT_OBSERVED`を返す（seat判定へは影響させない）
- 明細行数での代替はしない。表示用の構成比（`analyze.aggregate_month`）は行数で
  代替しているが、目安の表示と判定に効きうる数値では件数を作ってよいかの基準が違うため、
  揃えずに理由をコードへ残した。issueは`product`の列欠落（`reason: column_missing`）または
  product名のセル欠損（`reason: value_missing`）のときに`CAPACITY_SIGNAL_UNAVAILABLE`を
  出す。`requests`の欠落にはissueも警告も出ず、回数由来の特徴量が`NA`になることで
  出力側から見える
- `product`名が空の行・`requests`が欠けた行・列そのものが無い入力を、どれも「その行の値が
  分からない」として同じ規則で扱う。分からない行の寄与を最小・最大に見積もった範囲を出し、
  結論が動かないときだけ値を確定させる。伝播はユーザ単位に閉じ、欠損の無いユーザには
  波及しない
- 合計は範囲の下限と上限が一致するとき（分からない行の寄与が0のとき）だけ確定する。
  閾値判定は範囲が閾値のどちら側に収まるかで確定する。存在（`prohibited_observed`）は
  1行でも観測していれば真が確定し、偽の側は確定しない。policyにその分類の名前が1つも
  無いときは、分からない行が一致しようがないので確定する
- 欠損は0で埋めず、pandasの欠損表現が使える型（`Float64`・`Int64`・`boolean`）で保持する。
  真偽値に`bool`を使うと「算出できなかった」が偽と同じ値になるため`boolean`にした
- 金額・回数の合計は浮動小数点の加算のため、入力行の順序で最下位ビットが変わりうる。
  閾値ちょうどの比較はその粒度まで保証しない旨をモジュールのdocstringへ書いた
  （集計方法は`analyze.aggregate_month`と揃えたままにする。`math.fsum`へ変えると
  `api_cost`と`total_demand_usd`が最下位ビットで食い違う）
- `analyze()`が価格適用済み明細から対象月に1度だけ呼び、`AnalysisResult.product_usage`
  へ保持する。`users`・`warnings`・速報モードには手を入れていない
- `tests/test_module_deps.py`の`LAYERS`へ層15を割り当てた。`analyze`(20)と`report`(30)の
  双方から呼べる位置が要るため、同層importが禁止されている20は使えない

確認したこと:

- 8特徴量が定義どおりに計算される（primary・supplementary・分類外・禁止を含む合成データで
  期待値を手計算して固定）
- primaryに複数の名前を並べると両方がCodeとして数えられる
- "Code Review"がprimaryの"Claude Code"に一致しない（部分一致しない）
- 前後空白・大小文字・Unicode正規化形式（NFD/NFC）の違いを跨いで一致する
- `product`列なしで該当特徴量が欠損になり、issueが1件返る
- `requests`列なしで回数系が欠損になり、明細行数（行数2）で代替されない
- 需要合計0のとき`code_demand_share`が欠損になる
- 禁止かつsupplementaryのproductの需要が`supplementary_high`に含まれる
- 既定設定（`prohibited`が空）では`prohibited_observed`が偽でissueが出ない
- 同じ入力を繰り返し計算した結果が一致する（同一入力に対する実行間の決定性）
- レポート成果物（golden）が1つも変わらない。`ingest`・`pricing`・`report`は変更なし

コード・ファイル変更:

- `src/seat_analyzer/product_usage.py`（新規）
- `src/seat_analyzer/analyze/__init__.py`
- `tests/test_product_usage.py`（新規）
- `tests/test_module_deps.py`

テスト:

- `uv run ruff check .`
- `uv run pytest`（552件成功。追加前は538件）
- `git diff HEAD`を`check-text --diff`に通し、業務情報の混入なしを確認

外部レビュー Round 1（指摘5件・全件を再現確認のうえ対応。high severityなし）:

1. `product`名が空の行を「分類外の product」として確定扱いしていた。空の行の需要は
   `total_demand_usd`に入るのに`code_demand_usd`からは確実に除外され、`code_demand_share`が
   確定値として出ていた（実際は不確定）。過小評価の方向で、Code活用が低いことを根拠に
   ダウングレードを出す経路につながる。ユーザ単位のマスクで product 由来の特徴量へ伝播させ、
   確定できるものだけを算出するようにした
2. `requests`列があってもセルが欠損していると、合計が欠損を読み飛ばして0や部分合計になって
   いた。列ごと無い場合は欠損にしているのに、セル欠けでは「観測していない件数」が作られる
   非対称だった。そのユーザに欠損が1つでもあれば requests 由来の特徴量をすべて欠損にした。
   分類できない行の requests が primary だった可能性を排除できないため、依存範囲ごとの
   切り分けは採らない
3. `requests`合計が0のユーザの`product_breadth`が0になっており、受け入れ条件「分母0は`NA`」を
   満たしていなかった。0は「比を計算した結果、下限を超えるproductが無かった」、`NA`は
   「比を定義できない」で意味が違う
4. 浮動小数点の合計が入力行の順序に依存する（300試行中8試行で最下位ビットが変わることを確認）。
   ただし集計方法は変えない。`math.fsum`にすると`analyze.aggregate_month`が素の合計で計算する
   `api_cost`と`total_demand_usd`が食い違うため。主張の側を実態に合わせ、行順の入れ替えを
   検証していたテストを実行間の決定性の検証へ改め、順序依存をモジュールのdocstringに明記した
5. 上記の結果、本記録の「欠損は0で埋めない」「行の順序を入れ替えても一致する」という記述が
   実挙動と食い違っていた。1〜4の修正後に記述を実態へ揃えた

テスト（Round 1 修正後）:

- `uv run ruff check .`
- `uv run pytest`（561件成功）
- 指摘1〜3の再現コードを実行し、修正後の値が意図どおりになることを確認

外部レビュー Round 2（指摘4件・全件を再現確認のうえ修正。high severityなし）:

Round 1 で「誤って確定していた」を直した結果、今度は「確定できるのに不明にしている」が
4箇所できていた。個別にマスクを足すのではなく、「分からない行が結論を変えうるか」を
範囲で計算する仕組みへ一本化した。

1. `supplementary_high`を存在の主張として扱っていたが、これは合計に対する閾値判定であり
   同じ扱いにはできない。分からない行の需要が閾値に届かないときは偽が確定するのに欠損に
   なっていた（精度）。また`cost_usd`の非負性は保証されておらず（`cost_basis: net_spend`は
   返金等で負値がありうる）、負値があると真を残すのが誤りになる場合があった（正しさ）。
   範囲の下限が閾値以上なら真、上限が閾値未満なら偽、またぐなら欠損に変更した
2. 分からない行の寄与が0（cost 0・requests 0）でもCode系が一律に欠損になっていた。
   範囲の下限と上限が一致するので確定できる
3. `requests`欠損の伝播が`code_requests`に効きすぎていた。productが`Chat`と分かっている行の
   欠損はprimaryになりようがないので`code_requests`を動かさない。伝播元をprimary行と
   product不明行に限定した
4. `product`列ごと無い経路に、セル欠損側で入れた空カテゴリの確定扱いが適用されておらず
   自己矛盾していた。列欠落の早期returnをやめ、全行を「分からない行」として同じ規則へ
   流すことで、2経路の食い違いが構造的に起きないようにした

この一本化により、設計書の受け入れ条件を「列の有無で`NA`にする」から「証明できない値を
出さない」へ書き換えた（`docs/roadmap/implementation-design.md`のStep 7）。列が無くても
分からない行の正の寄与を含めた上限が閾値未満なら`supplementary_high`は偽が確定し、
primary行が1つも無ければCode回数は0が確定する（負の`cost_usd`がありうるため総需要ではなく
上限で判定する）。観測していない値を作る方向ではなく、証明できる値だけを出す側の変更。

テスト（Round 2 修正後）:

- `uv run ruff check .`
- `uv run pytest`（572件成功）
- 指摘の再現コード6件を実行し、修正後の値が意図どおりになることを確認

外部レビュー Round 3（指摘5件・全件を検証のうえ対応。high severityなし。うち3件は文書の誤り）:

1. `product_breadth`が確定できる場合まで欠損にしていた（既知99リクエスト+不明1リクエストなら、
   どの割り当てでも顔ぶれは変わらず1が確定する）。分母が全requestsで固定されているため
   不明行の割り当ては既知productを増やす方向にしか働かない、という単調性を使い、
   「不明行を全部まとめて新規productにしても5%に届かない」かつ「下限未満の既知productの
   最大へ全部注いでも届かない」とき確定させる正確な判定を入れた。不明行に負のrequestsが
   ある場合は単調性が崩れるため欠損に倒す。比較は既知productの判定と同じ除算形に揃え、
   不明行が無いユーザで丸めのずれによる不確定が出ないようにした
2. boundsの下限・上限を「確定分の合計+clip合計」の2段加算で出しており、直接合計と
   最下位ビットで食い違って誤確定しうる並びがあった（旧実装が1.0を確定する反例を確認）。
   行をマスクで選び元の行順のまま各1回の合計にして、下限・上限を「実際にありうるシナリオの
   合計そのもの」にした。`math.fsum`は使わない（Round 1の裁定どおり）
3. 設計書の例「総需要が閾値未満なら`supplementary_high`は偽が確定」が負値を考慮すると
   不成立だった（総需要50でも正値寄与の上限が150ならNA。実装が正しく例が誤り）。
   「正の寄与を含めた上限が閾値未満なら偽が確定」へ修正した
4. 設計書の「確定できなかった特徴量があるときはissue」が、requests欠落にはissueを出さない
   Round 1の合意と矛盾していた。issueの条件を「productの列欠落またはセル欠損」に限定し、
   requests欠落はissueも警告も出ない（特徴量の`NA`として見える）と明記した。
   なおRound 3時点では「既存のingest警告に委ねる」と書いたが、ingestはセル欠損を
   警告しない（`errors="coerce"`で黙って欠損化する）ためRound 4で訂正した
5. 本記録の「実装したこと」にRound 2修正前の挙動が現在形で残っていた。最終挙動へ書き直した

テスト（Round 3 修正後）:

- `uv run ruff check .`
- `uv run pytest`（578件成功）
- 指摘1・2の再現コードと、Round 1〜2の再現ケース5件の維持を確認

外部レビュー Round 4（指摘3件・全件を検証のうえ対応。high severityなし。うち1件は文書の誤り）:

1. boundsの合計で除外行を0に置き換えていたため、挿入された0が加算のブロック割りを変え、
   「選んだ行だけの直接合計」にならなかった。複数の部分集合の合計が同じ丸め先へ寄り、
   不明な寄与が非ゼロなのに下限と上限が一致して確定する並びを確認（Round 3の修正が目的を
   達成できていなかった）。除外行を0で埋めるのをやめ、実際に選んだ行だけをgroupby合計する
   `_subset_sum`へ置き換えた。行の無いユーザは空和の0で補う。実データでは不明行の寄与が
   確定分の1e-16倍未満になることは起きないため実害は無いが、機構の保証として修正した
2. breadthの確定条件のdocstringが「条件が崩れれば結論を変える割り当てが実在する」と
   過大な主張をしていた（反例: カウント済みのproductが無く、既知が需要0の未達productだけの
   ユーザでは、不明行がどこへ帰属しても同じ1つのproductが下限を超えるため、条件が崩れても
   結論は変わらない）。
   厳密な判定は「不明行をどう束ねると下限以上のproductをいくつ作れるか」という組合せ問題に
   なるため追わず、2条件を確定の十分条件と位置づけて保守的に欠損へ倒す、と実態に合わせた
   （実装は不変。確定と言った値が誤らない側の保証は保たれている）。保守的NAになる既知の
   ケースと、本当に不確定なケース（不明2行の束ね方で1にも2にもなる）を対で
   テストへ文書化した
3. Round 3で書いた「requestsの欠落は既存のingest警告に委ねる」が実装と不一致だった。
   ingestが警告するのは列そのものの欠落候補だけで、セルの空欄・数値変換失敗は
   `errors="coerce"`で黙って欠損になる。「issueも警告も出ず、回数由来の特徴量が`NA`になる
   ことで出力側から見える」へ両文書を訂正した

テスト（Round 4 修正後）:

- `uv run ruff check .`
- `uv run pytest`（581件成功）
- 指摘1の再現コード（ゼロ挿入で誤確定していた10行の並び）でNAになることと、
  Round 1〜3の再現ケース7件の維持を確認

外部レビュー Round 5（指摘3件・全件を再現確認のうえ対応。high severityなし。
2件は実データでは到達しない敵対的入力のコーナーで、機構の保証を守るための修正）:

1. 負のrequests値の桁落ち（1e16と-1e16の相殺）を使うと、breadthの確定条件の
   `largest_short + unknown_total`が「別々に集計した値の加算」であるためにシナリオ直接合計と
   食い違い、誤確定する並びがあった。厳密なシナリオ合計は追わず、不明行を持つユーザに
   負のrequests値が1つでもあれば確定しない保守的な条件を加えた。不明行の無いユーザには
   適用しない（割り当ての自由度が無く結論は1つに決まる）。requestsは実データでは非負の
   カウントなので実運用の出力は変わらない
2. Round 4の`_subset_sum`の`fillna(0.0)`が、「選択行が無いユーザの空和0」だけでなく
   「集計結果が本当にNaNになったユーザ（infの相殺等）」まで0で確定していた
   （Round 4が意図せず変えた挙動）。`reindex(index, fill_value=0.0)`へ変更し、
   新しく追加されるラベルだけを0にして既存グループのNaNを保持する（旧実装の挙動に復帰）
3. docstringの反例説明「下限を超える不明行が1行だけなら結論は変わらない」が一般には
   偽だった（既知Code90+不明10では帰属先でbreadthが1にも2にもなる。実装は正しくNAを返す）。
   反例が成立する正確な条件（カウント済みproductが無く、既知が需要0の未達productだけ）へ
   docstring・テスト・本記録を書き換えた

テスト（Round 5 修正後）:

- `uv run ruff check .`
- `uv run pytest`（583件成功）
- 指摘1・2の再現コードでNAになることと、負値があっても不明行が無ければ従来どおり確定する
  こと、Round 1〜4の再現ケースの維持を確認

外部レビュー Round 6（指摘2件・ともにlow。挙動への指摘なし。テストの分離検証の補強と
docstringの前提の明記のみ）:

1. Round 5のテストが「不明行はあるが負値は別ユーザだけ」の経路を検証しておらず、
   負値を全ユーザ共通で判定する誤実装でも通る構成だった。不明行を持つが負値を持たない
   ユーザを同じ入力へ加え、別ユーザの負値に巻き込まれないことを固定した
2. モジュールdocstringの単調性の説明に「requestsがすべて非負なら」という前提が
   抜けていた。前提を明記し、負値がある場合は保守的に欠損へ倒す旨を追記した

挙動を変える指摘が出なくなったため、レビューは6巡で収束と判断した。

レビュー全体の記録（累計22件・すべて検証のうえ対応）:

| 巡 | 指摘 | high | mid | low | 前巡の修正由来 |
|---|---:|---:|---:|---:|---:|
| 1 | 5 | 0 | 4 | 1 | — |
| 2 | 4 | 0 | 3 | 1 | 4 |
| 3 | 5 | 0 | 1 | 4 | 2 |
| 4 | 3 | 0 | 2 | 1 | 2 |
| 5 | 3 | 0 | 2 | 1 | 2 |
| 6 | 2 | 0 | 0 | 2 | 2 |

このStepで得た教訓:

- 「観測していない値を作らない」のような不変条件は、代理条件（列の有無・一律の欠損伝播）で
  実装すると両方向に破れる。不変条件そのもの（分からない行の寄与の範囲）を計算する形に
  一本化してから指摘が収束へ向かった
- 浮動小数点の合計は「同じ行集合の直接合計」以外の計算経路（別々に足してから加算・
  除外行の0埋め）を作ると、丸めの食い違いが確定判定の保証を破る。部分集合を選んで
  1回で合計する
- Round 3以降の指摘はすべて実データでは到達しない敵対的入力（負のrequests・±inf・
  1e-16スケールの桁落ち）のコーナーだったが、「確定と言った値が誤らない」という機構の
  保証を守るため修正した。保証を諦めて入力を制約する案（非負の検証で拒否）は、
  cost_basis=net_spendの負値が正当であるため採れなかった

### 2026-08-14 — Step 6 product policy config

ステータス: `完了`

実装したこと:

- `product_policy`セクションを既定設定へ追加（`primary`・`supplementary`・`prohibited`・
  `supplementary_high_usd`）。設計書§9.1の内容に合わせ、`prohibited`の既定は空にした
- ロード時の検証を追加。3つのリストが「空でない文字列のリスト」であること、`primary`が
  空でないこと、閾値が0以上の有限な数値であることを検査する
- 同じproduct名が`primary`と`supplementary`の両方に書かれた場合をerrorにした。互いに
  排他な分類で、どちらとして数えるかが設定の書き方次第で決まってしまい、後続Stepの特徴量が
  黙って変わるため。同一リスト内の重複も書き間違いとして弾く
- `prohibited`は分類と直交する指定なので、`primary`・`supplementary`と重ねて書ける。
  禁止に指定したproductも元の分類に残り、別途policy warningの対象になる（§9.3）
- 重複の照合は前後空白を除去し、NFC正規化してから大小文字を無視して行う。設定ミスを
  拾うのが目的なので、取りこぼしより誤検出に倒す
- `product_policy`を必須セクション一覧へ追加し、欠落時のメッセージを他セクションと揃えた
- ワークスペース設定の雛形へ`prohibited`のコメント例を追加。導入組織ごとに設定する項目で、
  雛形の趣旨（この環境・この組織に固有で他の利用者と共有しない設定）に合致するため

確認したこと:

- 既定設定がそのままロードでき、各キーが期待どおりの型で読める
- `primary`が空・空文字・空白のみ・非文字列・リストでない場合にerrorになる
- 閾値が負・非数値・真偽値・NaN・±Infinityの場合にerrorになる。0は正当な設定として通る
- 同じproduct名の重複を、大小文字違い・前後空白違いでも検出し、messageにproduct名が入る
- `prohibited`は空でも値入りでもロードできる
- 重複の報告順が集合の反復順に依らず、設定の記述順になる
- 集計・レポート・product列の照合ロジックは実装していない（Step 7以降の担当）
- `ingest`・`pricing`・`analyze`・`report`を変更していない。新規モジュールなし

コード・ファイル変更:

- `src/seat_analyzer/default-config.yaml`
- `src/seat_analyzer/config.py`
- `src/seat_analyzer/templates/workspace-config.yaml`
- `tests/test_hardening.py`

テスト:

- `uv run ruff check .`
- `uv run pytest`（536件成功。追加前は516件）
- `git diff HEAD`を`check-text --diff`に通し、業務情報の混入なしを確認

外部レビュー Round 1（指摘3件・全件をコードで再現確認のうえ修正。high severityなし）:

1. `prohibited`と`primary`・`supplementary`の重複まで拒否していた。既定の`supplementary`に
   ある product を禁止指定できず、ワークスペース設定の雛形が案内する
   「`prohibited`だけを上書きする」書き方がロードエラーになっていた（一時ディレクトリで
   再現）。回避のため`supplementary`から消すと`supplementary_high`の集計対象が変わる。
   §9.2の`prohibited_observed`は独立した特徴量、§9.3は「禁止productはseat判定へ影響させず
   policy warning」であり、`prohibited`は分類と直交する属性だった。排他なのは
   `primary`と`supplementary`だけなので、重複の検査を「同一リスト内」と
   「primaryとsupplementaryの重なり」に限定した。エラーメッセージもどのリストで落ちたかが
   分かる文面へ分けた
2. 閾値0のテストのdocstringが「supplementaryの利用があれば真」となっていたが、
   判定は「閾値以上」なので需要ゼロでも真になる。Step 7の実装者が比較演算子を誤らないよう
   実挙動に合わせて書き直した（閾値0は常に真になる境界値として許可のまま）
3. 重複判定にUnicode正規化がなく、合成済みと分解済みの同じ名前を別名として扱っていた。
   組織名の衝突判定（`ingest.check_org_name_collisions`）が`NFC`正規化を使っており規則が
   不整合だったため、`NFC`正規化してから`casefold`する比較に揃えた。組織名は前後空白を
   含むこと自体を不正にしているのに対し、product名は前後空白を落としてから比較する点だけが
   異なる

テスト（Round 1 修正後）:

- `uv run ruff check .`
- `uv run pytest`（538件成功）
- 一時ディレクトリに`prohibited`だけを書いた上書き設定を置いてロードし、成功すること・
  `supplementary`が既定のまま変わらないことを確認

### 2026-07-30 — Step 0A GitHub認証の手動smoke test

ステータス: `完了`

確認したこと:

- 対象範囲のrepository metadataに必要なread-only権限を確認
- Organization単位でrepositoryを自動列挙可能
- PR本文・diff・codeを取得せず、PR作成日時・マージ日時を取得可能
- GitHub上のissue、PR、repository、設定を変更していない

発見した制約:

- 大規模Organizationを1か月単位で一括検索するとsecondary rate limitへ到達する
- primary rate limitに余裕があってもsecondary rate limitは別に発生し得る

設計へ反映済みの対応:

- 期間を週単位へ分割する
- 直列で取得する
- ローカルcacheへupsertする
- rate limit時に中断・再開する
- 部分取得を確定値として扱わない

コード・ファイル変更:

- なし

テスト:

- コード変更がないため未実施

### 2026-07-30 — Step 0B Playwright管理画面smoke test

ステータス: `完了`

確認したこと:

- headed browserを一時profileで起動できた
- scriptに管理画面のclick、入力、設定変更処理がない
- 対話的な認証フローを試行した

発見した制約:

- 外部セキュリティ検証が反復し、管理画面へ安定して到達できない
- Spend CSVの取得受け入れ条件は満たせなかった

対応方針:

- セキュリティ検証の回避は行わない
- Playwright方式は不採用
- Playwright検証用scriptは削除
- 通常ブラウザとdownload watcherを組み合わせるStep 0Cへ切り替える

コード・ファイル変更:

- Playwright検証用scriptはproductionへ残していない

テスト:

- headed browserの起動と外部セキュリティ検証の制約を確認

### 2026-07-30 — Step 0C 通常ブラウザ＋download watcher smoke test

ステータス: `完了`

確認したこと:

- 通常ブラウザのセッションを利用できた
- 利用者の手動Export後、新しいSpend CSVを5分以内に検出できた
- headerからSpend CSVであることを判定できた
- Spend CSVをGit管理外の一時ディレクトリへコピーできた
- シート・creditを変更していない

セキュリティ確認:

- browser profile、Cookie、認証情報へアクセスしていない
- CSV本文と元filenameをログ出力していない
- 検証用の実データコピーは検証後に削除した
- Downloads内の元ファイルは変更・削除していない

設計へ反映済みの対応:

- Playwright自動取得を通常ブラウザ＋download watcherへ変更
- 管理画面の画面操作とDOM取得を実装対象外へ変更
- CSVがないcredit値は小さな手入力CSVで補完する

コード・ファイル変更:

- `scripts/spike_download_watcher.py`を追加

テスト:

- 合成Spend CSVによる監視・header判定・一時コピー
- 通常ブラウザからの実CSVダウンロードsmoke test
- `uv run ruff check .`
- `uv run pytest`（167件成功）

### 2026-07-30 — Step 1 Spend任意カラム

ステータス: `完了`

実装したこと:

- Spendの`account_uuid`、`user_id`、`gross_spend`、`web_search_count`を正準化
- 現行形式のunderscore区切りと従来形式のspace区切りを同じaliasで処理
- 4列が存在しない旧CSVでは列を`NA`で追加
- Spend CSVを文字列として読み、ID列の先頭ゼロを保持したまま前後空白を除去
- 金額・件数列を数値化
- 現行形式を示す列がある場合、4列の部分欠損を警告
- 4列のalias設定欠損を起動時に検出
- キャッシュ内訳列を誤って`NA`補完対象へ追加しない不変条件テストを追加

確認したこと:

- 新形式CSVを読み込める
- 4つの任意列がない旧形式CSVを読み込める
- ID列に空セルが混在しても、数値形式IDの先頭ゼロと欠損を保持できる
- 空白のみのIDセルを欠損として扱う
- 旧形式と新形式でV1が参照する列の値・型が一致する
- 旧CSVの既存警告へ新しい任意列名を追加しない
- 現行形式の4列が部分的に欠けた場合は、欠損列名を警告する
- report追加、`subject_id`生成、既存判定の変更を行っていない

コード・ファイル変更:

- `config.yaml`
- `src/seat_analyzer/config.py`
- `src/seat_analyzer/ingest.py`
- `tests/test_hardening.py`
- `tests/test_ingest.py`
- `tests/test_pricing.py`

テスト:

- `uv run pytest tests/test_ingest.py tests/test_hardening.py tests/test_pricing.py`（31件成功）
- `uv run ruff check .`
- `uv run pytest`（174件成功）

### 2026-07-30 — Step 2 Members任意カラム

ステータス: `完了`

実装したこと:

- Membersの`account_uuid`、`user_id`、`member_status`を正準化
- 3列が存在しない既存CSVでは列を`NA`で追加
- Members CSVを文字列として読み、数値形式IDの先頭ゼロを保持
- IDとstatusの前後空白を除去し、空白のみの値を`NA`へ統一
- 未知のstatus値は大小文字を含めて変換せず保持
- 3列のalias設定欠損を起動時に検出

確認したこと:

- 新形式と既存形式のMembers CSVを読み込める
- ID列に空セル・空白のみセルが混在しても、数値形式IDの先頭ゼロと欠損を保持できる
- 空白のみのmember statusを欠損として扱う
- 未知のmember statusを入力値のまま保持できる
- 既存CSVの警告へ新しい任意列名を追加しない
- 任意列の有無によらずV1のemailとseat判定の値・型が一致する
- ID join変更、report追加を行っていない

コード・ファイル変更:

- `config.yaml`
- `docs/implementation-design.md`
- `src/seat_analyzer/config.py`
- `src/seat_analyzer/ingest.py`
- `tests/test_hardening.py`
- `tests/test_ingest.py`

テスト:

- `uv run pytest tests/test_ingest.py tests/test_hardening.py tests/test_member_changes.py tests/test_members_info_snapshots.py`（44件成功）
- `uv run ruff check .`
- `uv run pytest`（178件成功）

### 2026-07-30 — Step 3 subject_id

ステータス: `完了`

実装したこと:

- Identity証拠をemail・`account_uuid`・`user_id`で連結する独立モジュールを追加
- `account:<account_uuid>`、`user:<user_id>`、`email:<normalized_email>`の優先順位で
  `subject_id`を生成
- stable IDを共有する複数emailを同一subjectとして解決
- 同じemailだけがstable IDを持つ別入力へIDを伝播
- `stable`、`email_consistent`、`email_fallback`、`conflict`、`unresolved`の品質を実装
- 同一identity内で同種stable IDが複数に分岐した場合をconflictとして検出

確認したこと:

- `account_uuid`と`user_id`が1つずつ併存してもconflictにしない
- email変更があっても同じstable IDなら同じsubjectになる
- stable IDがないemailの履歴十分性を証拠行数から推測しない
- conflict時は`subject_id`を確定しない
- conflictの影響範囲を成分内のemail・stable IDから確認できる
- 空白・欠損だけの証拠は`unresolved`になる
- unresolvedを含めても入力証拠の先頭位置に基づく決定的な順序になる
- 非スカラーのIdentity証拠を入力元名・問題値付きの明確なエラーで拒否する
- V1のemail join、report、シート判定を変更していない

コード・ファイル変更:

- `docs/implementation-design.md`
- `src/seat_analyzer/identity.py`
- `tests/test_identity.py`

テスト:

- `uv run pytest tests/test_identity.py`（14件成功）
- `uv run ruff check .`
- `uv run pytest`（192件成功）

### 2026-08-01 — Step 4 QualityIssue

ステータス: `完了`

実装したこと:

- 構造化品質issueの語彙を独立モジュールとして追加（severity・code・scope・JSON serializer）
- Severityはerror/warningの2値のみ（doctorのexit code意味論から確定。info等は設けない）
- IssueCodeは設計書§17の25 codeをvalue == nameで過不足なく定義
- QualityIssueはfrozen dataclass。scopeは構築時に検証・正準化し読み取り専用で保持
  （スカラーとスカラー列のみ許可、list→tuple化、非スカラー・非strキー・NaN/infは
  キー名・型名入りエラーで拒否）
- 決定的なJSON直列化（ensure_ascii=False・allow_nan=False・キー順固定）と
  全順序整列を追加
- messageの決定性制約（タイムスタンプ・乱数・実行環境依存値の禁止）をdocstringへ明記

確認したこと:

- `sort_issues`を通せば、同一issue集合から構築順・scopeキー挿入順によらずバイト一致のJSONに
  なる（`issues_to_json`単体は渡された順を保持する。整列は呼び出し側の責務）
- 日本語messageがエスケープされず出力される
- 入力Mappingの後続変更がissueへ波及しない
- 等価なissueのhashが一致し、set/dictキーとして使える
- 既存ファイルの変更ゼロで「既存warning不変」を構造的に担保
- CLI配線・既存warningからの変換は行っていない（Step 5の担当）

コード・ファイル変更:

- `src/seat_analyzer/domain.py`
- `src/seat_analyzer/data_quality.py`
- `tests/test_data_quality.py`

テスト:

- `uv run pytest tests/test_data_quality.py`（23件成功）
- `uv run ruff check .`
- `uv run pytest`（215件成功）

### 2026-08-04 — Step 5 doctorの既存入力検査

ステータス: `完了`

実装したこと:

- `doctor`サブコマンドを追加（`--org`複数指定・`--month`省略時は最新月・`--format text|json`）
- 検査本体を`data_quality.inspect_input`として追加（1組織・1対象月で読み取り専用）
- Spendの検査: 対象月の欠損、ファイル名から採用ファイルを決められない、読めない（必須カラム
  欠落・文字コード）、部分月、ヒステリシス窓の欠月、単価表に無いモデル、必須数値列の解釈失敗
- Membersの検査: 1件も無い、読めない、対象月が無く別月へフォールバック、シート種別の判別不能
- Spend×Membersの突き合わせ: spendに行があるがmembersに居ない、未割当なのに利用実績がある
- errorが1件でもあればexit 1、warningのみ・問題なしはexit 0
- json出力はstdoutをJSONのみに保ち、対象月の通知はstderrへ出す
- IssueCodeへ3 code追加（`MEMBER_ROW_MISSING`・`SEAT_TYPE_UNKNOWN`・`UNASSIGNED_WITH_USAGE`）。
  設計書§17は「最低限のcode」の規定であり、Step 5で検出対象が確定したため追記した

確認したこと:

- 問題の無い入力では issue ゼロ・exit 0 になる
- 警告だけなら exit 0、errorがあれば exit 1 になる
- ingestの例外メッセージに含まれる入力ディレクトリのパスをmessageへ持ち込まない
  （入力ディレクトリからの相対表記へ落とし、同一入力で常に同じ文字列になる）
- 同じ入力を2回検査するとJSON出力がバイト一致する
- 複数組織で組織ごとに検査し、scopeのorgで識別できる。旧レイアウト（単一組織）では
  scopeにorgを入れない
- doctorはファイルを作成・変更しない（実行前後でツリーが一致）
- `analyze`・`report`・`ingest`・`pricing`のコードを変更していない
- GitHub・browser・admin設定・code-analytics・members-infoは検査していない

コード・ファイル変更:

- `README.md`
- `docs/implementation-design.md`
- `src/seat_analyzer/cli.py`
- `src/seat_analyzer/data_quality.py`
- `src/seat_analyzer/domain.py`
- `tests/test_cli.py`
- `tests/test_data_quality.py`

テスト:

- `uv run pytest tests/test_cli.py tests/test_data_quality.py`（58件成功）
- `uv run ruff check .`
- `uv run pytest`（243件成功）
- 実データ3組織でのsmoke test（exit 0）。既知の状態（観測月が1ヶ月のみの組織で履歴月欠落、
  対象月当時のメンバー一覧が無い組織でフォールバック）をwarningとして検出し、
  それ以外の組織ではissueが出ないことを確認

外部レビュー Round 1（指摘9件・全件をコードで再現確認のうえ修正）:

1. 欠月時のmessageが`analyze`の実挙動と不一致だった。`analyze`は暦上の連続性ではなく
   存在する過去月だけで連続同推奨を判定するため、欠月があっても「変更推奨」は出る。
   「要観察に留まる」という誤案内を、判定の質が下がる旨の文言へ修正
2. `spend/`を持たない組織を検査対象にできなかった（全組織モードでは黙って除外、
   `--org`指定ではJSONを出さずにエラー終了）。doctorの組織発見条件を「既知の入力
   サブディレクトリかmembers-info.csvを持つディレクトリ」へ広げた
3. データ行が無いメンバー一覧（ヘッダのみ）がerrorにならなかった。全ユーザがシート不明に
   なり判定が成立しないためerrorとし、突き合わせ検査は行わないようにした
4. `PermissionError`等の読み取り失敗が構造化issueにならず、traceback で終了して
   `--format json`のstdoutがJSONにならなかった。読み取り経路の`OSError`を
   `MISSING_SPEND`/`MISSING_MEMBERS`のerrorへ変換した
5. 対象月を決められない場合にMembersの検査まで打ち切っていた。Spendの解決可否とMembersの
   有無を独立に検査し、「ファイル名から解決できない」と「1件も無い」を区別した
6. 複数組織のJSONを全体整列していなかったため、`--org`の指定順で同一issue集合でも
   バイト列が変わり、warningがerrorより前に出ていた。JSON化の直前に全体を整列した
7. modelセルが空の行はdefault単価が適用されるのに`UNKNOWN_MODEL`を出せなかった
   （`unmatched_models`が欠損を除外するため）。空セルの行数を別に数えて警告へ含めた
8. 単日日付で命名されたスペンドを部分月として検出できなかった。ただし実際の集計期間は
   ファイル名から分からないため、日数を断定せず「全月と確認できない」警告にした
9. `NUMERIC_PARSE_FAILED`の`scope.rows`が失敗セル数になっていた。影響行数へ修正し、
   セル数は別キーへ分離した

テスト（Round 1 修正後）:

- `uv run ruff check .`
- `uv run pytest`（253件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一（新たな誤検出なし）

外部レビュー Round 2（指摘7件・全件をコードで再現確認。6件を修正、1件は文書を修正）:

1. 組織名が`members`・`code-analytics`のとき、その組織ディレクトリを旧レイアウトの目印と
   誤認して「混在」エラーになっていた（analyzeでは組織として扱える）。組織候補として
   発見したディレクトリは旧レイアウト判定から除外した
2. 対象月を確定できない経路では、メンバー一覧の有無しか見ずデータ行の有無・可読性を
   検査していなかった（Round 1 の指摘がこの経路で再発）。最新候補を実際にロードして
   「読めない」「データ行が無い」をerrorにした
3. 組織の発見中に発生した`OSError`（存在しない・読めない入力ディレクトリ）が構造化issueに
   ならず、`--format json`のstdoutが空だった。`input_unavailable_issues`で構造化errorへ
   変換した。使い方の誤り（組織名の誤り・レイアウト混在）は従来どおりstderr + exit 1
4. 月が存在しても`month=None`で呼ぶと`MISSING_SPEND`errorになり、「対象月を特定できません
   （存在する月: ...）」と自己矛盾した案内をしていた。`month=None`を「最新月を対象にする」
   意味に統一し、月を確定できない場合だけ月単位の検査を省くようにした
5. 先頭ドットのディレクトリをvalidate_org_nameより前に除外していたため、入力構造を持つ
   不正名の組織をdoctorだけが黙って無視していた。候補に含めてanalyzeと同じ検証で拒否する
6. 欠月messageのテストがdoctorの文字列しか検証しておらず、analyzeの実挙動が変わっても
   通り続ける状態だった。同じ入力でanalyzeも実行し、statusが「変更推奨」になることを
   同一テストで照合した
7. `issues_to_json`が入力順に依存する点は、整列（`sort_issues`）と直列化を分離したStep 4の
   意図した契約であり、テストでも両方を固定している。暗黙ソートは名前から読み取れない
   順序変更になり、呼び出し側が出力順（組織単位・時系列・検出順）を選べなくなるため採らない。
   この裁定をレビュアへ差し戻して意見を求め、「条件付き同意（正準出力用の名前付きAPIを
   分けること）」との回答を得たため、次のとおり整理した。
   - `issues_to_json` は指定順を保持する低水準の直列化（契約は変更せず、docstringを明確化）
   - `issues_to_canonical_json` を追加し、内部で`sort_issues`を適用する。機械可読出力は
     この境界だけを使う（整列を呼び出し側の記述に依存させない）
   - 不変条件の記述を正確にした。保証されるのは「同一のissue多重集合を`sort_issues`で
     正準順序化してから直列化した場合に同一の文字列になること」であり、直列化関数単体の
     性質ではない（集合ではなく多重集合、バイト一致ではなく文字列一致）
   - レビュアの指摘のうち「暗黙ソートに機能上の利得がない」「完了済みStepのテスト変更に
     なる」という当初の論拠2点は誤りと認め、維持の根拠を「順序保持という契約自体の妥当性」に
     置き換えた

テスト（Round 2 修正後）:

- `uv run ruff check .`
- `uv run pytest`（259件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一

外部レビュー Round 3（指摘5件・全件をコードで再現確認のうえ修正。high severityなし）:

1. 直下の`spend/`まで旧レイアウト判定から除外していたため、組織名`spend`をdoctorだけが
   通常組織として受理し（analyzeは混在エラー）、直下の旧形式CSVを黙って無視していた。
   `spend/`は常に旧レイアウトの目印とし、除外は`members`・`code-analytics`に限定した
2. `_reason`が`入力ディレクトリ + "/"`しか置換していなかったため、パスが文中に単独で現れる
   例外（`{input_dir} に入力データがありません`）で絶対パスがmessageに残っていた。
   Round 2で追加した経路自体が決定性の不変条件を破っていた。入力ディレクトリ自身も固定語へ
   置換し、あわせて自分が投げる例外にはパスを埋め込まない方針にした
3. 入力ディレクトリが存在しない場合でも`--org`を付けると組織名の検証が先に走り、構造化issueに
   ならなかった（Round 2の修正が`--org`指定時に再発）。入力ディレクトリの可否を組織名の検証より
   先に判定するようにした
4. 対象月なし経路のメンバー一覧の採用がファイル名の辞書順だったため、同一月に複数ある場合に
   スナップショット日付の古い方を選び、誤った`MISSING_MEMBERS`が出ることがあった。
   自前の選択をやめて`ingest.load_members`の重複解決規則に委ねた
5. 組織も対象月も未解決のとき、text出力の見出しが「入力 入力検査」と重複していた

各巡の指摘は「前巡の修正が作った新しい経路」に集中した（Round 2は7件中3件、Round 3は5件中3件）。
新経路を追加したら、既存経路に対して確立した不変条件を同じ強さで満たしているかを確認する。

テスト（Round 3 修正後）:

- `uv run ruff check .`
- `uv run pytest`（264件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が修正前と同一

外部レビュー Round 4（指摘4件・全件をコードで再現確認のうえ修正。high severityなし）:

1. 直下の`members/`・`code-analytics/`を常に旧レイアウトの目印にしていたため、通常の組織と
   旧レイアウトの残骸が共存するとdoctorだけが混在エラーになっていた（analyzeは組織を処理する）。
   組織があるときは混在判定を直下`spend/`のみに戻し、組織が無いときだけ`spend/`を欠いた
   旧レイアウトを拾うようにした
2. Round 3で入れた「入力ディレクトリ自身の置換」が素朴な部分文字列置換だったため、相対指定
   （`.`・`a`・`input`）で例外文中の無関係な語やピリオドまで破壊していた
   （`--input-dir a` で "cannot parse header" が壊れる）。置換対象を絶対パスのみに限定した。
   相対指定はそれ自体が実行環境に依存せず決定的なので置換の必要がない
3. 月の一覧を得た後にファイルが変化すると、`_partial_month_issues`の再走査から例外が
   `inspect_input`の外へ漏れ、`--format json`でもJSONが出力されなかった。再走査の失敗も
   `MISSING_SPEND`のerrorへ変換した
4. 「ファイルを作成・変更しない」テストがパス一覧しか比較しておらず、既存ファイルの上書きを
   検出できなかった。サイズ・更新時刻・内容ハッシュまで比較するようにした

Round 4では、Round 2・3で追加した経路の退行有無も回帰テストで固定した（組織名が`members`・
`spend`の場合、組織が無い旧レイアウト、相対/絶対の入力ディレクトリ指定）。

テスト（Round 4 修正後）:

- `uv run ruff check .`
- `uv run pytest`（274件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が初回から不変。サンプルの`analyze`も従来どおり

外部レビュー Round 5（指摘2件・ともに競合状態と経路の取りこぼし。high・lowなし）:

1. `_reason`の置換候補が`str(input_dir)`と`resolve()`だけだったため、相対symlinkを
   `--input-dir`に渡すと、symlinkを解決しない絶対表記（`absolute()`相当）が例外文に現れた
   場合に置換されなかった。`absolute()`も候補に加えた（相対symlinkや`..`を含む指定では
   `absolute()`と`resolve()`が一致しない）
2. Round 4で`spend_file_period`の例外は捕捉したが、同じ再走査で`None`が返る経路
   （一覧取得後にファイルが消えた・名前が解釈不能になった）を`continue`で黙って通していた。
   対象月は`load_spend`が捕捉するため、過去月だけが無言で欠落し「問題なし・exit 0」に
   なり得た。一覧にあった月の`None`もerrorへ変換した

テスト（Round 5 修正後）:

- `uv run ruff check .`
- `uv run pytest`（277件成功）
- 実データ3組織・サンプル2組織のsmoke testで出力が初回から不変

外部レビュー Round 6: 指摘なし・マージ可の判断。6巡で終了。

レビュー全体の記録（累計27件・すべてコードで再現確認のうえ対応）:

| 巡 | 指摘 | high | mid | low | 前巡の修正由来 |
|---|---:|---:|---:|---:|---:|
| 1 | 9 | 2 | 5 | 2 | — |
| 2 | 7 | 0 | 6 | 1 | 3 |
| 3 | 5 | 0 | 3 | 2 | 3 |
| 4 | 4 | 0 | 3 | 1 | 2 |
| 5 | 2 | 0 | 2 | 0 | 1 |
| 6 | 0 | 0 | 0 | 0 | — |

このStepで得た教訓:

- 新しい経路を追加したら、既存経路に対して確立した不変条件（messageの決定性・JSON出力の
  保証・error/warningの分類）を同じ強さで満たしているかを必ず確認する。指摘の3分の1は
  「前巡の修正が作った新しい経路」由来だった
- 例外メッセージを再利用して構造化issueへ載せる場合、環境依存値の除去は素朴な部分文字列
  置換では成立しない。置換対象を絶対パスに限る等、対象を狭く定義する
- 「doctorが検査する」と決めた不整合は、対象月あり・なしの両経路で同じ強さで検査する。
  片方だけ緩いと、その経路でのみ無言の欠落が起きる
