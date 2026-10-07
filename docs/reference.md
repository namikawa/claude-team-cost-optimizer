# レポートの読み方と判定の仕様

生成されたレポートに出る各セクションの意味と、シート推奨を決めているロジックの前提。
分析結果を読む人・判定の妥当性を確かめる人向け。

- 環境構築: [setup.md](./setup.md)
- 入力データの準備と月次運用: [usage.md](./usage.md)
- 考察の自動執筆と公開テキストの検査: [tooling.md](./tooling.md)

成果物は役割で分かれている。report はサマリ・シート変更推奨・考察を読むための短い文書、
details は全ユーザ表・部署別/チーム別サマリ・詳細利用状況・組織内の分布・月中の推移・
シートが吸収した量の実測・感度分析を集めた機械生成の詳細資料、dashboard は同じ数値をタブに
分けて見せる共有用の自己完結 HTML。

ファイル名は `{種別}-{YYYYMM}-{組織名}.{拡張子}`（例: `report-202607-<組織名>.md`）で、
共有でフォルダの外へ出してもどの組織のいつの分析かが分かる。以下では種別名だけで呼ぶ。

以下のセクション名は、断りがなければ details と dashboard の両方に出るものを指す。
片方にしか出ないものもあり、「シートが吸収した量の実測（E = API換算需要 − 実課金）」と
「感度分析」は details だけ、「Codeと他プロダクトの需要（API換算）」は dashboard だけに出る。
どちらの成果物でも、材料になるデータが無いセクションは省かれる。

設定について: 以下で `config.yaml > trend` のように書くのは設定のキーの位置を指す。既定値は
パッケージ同梱の `default-config.yaml` が持ち、ワークスペースの `config.yaml` には既定から
変えたい差分だけを書く（書かなかった項目は既定が使われる）。モデル単価やカラム対応表のように
プログラムの更新で新しい値が届く項目は、ワークスペース側に写さないこと。

## dashboard の構造と操作

共有用の自己完結 HTML。スタイル・スクリプト・データを1ファイルに埋め込んであり、外部への
通信を一切しないため、オフラインや外部アクセスを絞った環境でもブラウザで開くだけで読める。
内容は5つのタブに分かれる。
複数スペースの組織では組織タブの次に「複数スペース」タブが加わり、6つになる。

- 概要 — KPI 4枚（メンバー数・現在のシート費用・API換算利用額・削減見込み）と、月次推移・
  前月からの変化・追加クレジットの状態・月中のメンバー変動・主な増減
- 推奨アクション — 判定サマリ（判定ごとの件数）と追加クレジット付与候補を先に置き、その下に
  推奨一覧（ユーザごとの現在→推奨シート・API換算需要・実課金・Std時/Prem時・削減/月・判定）
- メンバー別 — ユーザ別 API 換算コスト（棒グラフ）、詳細利用状況、月中の利用推移、
  月中の Claude Code 活動、Codeと他プロダクトの需要（API換算）とその内訳表
- 組織 — 部署別サマリ・チーム別サマリ、組織内の分布（参考値）
- 前提と注意 — シート単価・列の意味・確度・上限フラグ・判定に使用した月

タブ名の脇の数字はそのタブに載る件数（概要はメンバー数、推奨アクションは推奨一覧の行数、
メンバー別は詳細利用状況の行数、組織は部署別サマリ（部署が無ければチーム別）の行数）。
前提と注意は数えるものが無いので数字が付かない。どのタブでも、材料になるデータが無い
セクションは出ない。

右上の Light / Dark / Auto でテーマを切り替える。既定は Auto で OS の設定に追従し、明示的に
選んだテーマはそのブラウザに保存されて次に開いたときも保たれる。

対話でできることは次のとおり。

- 列ソート — 表のヘッダをクリックすると並べ替える（同じ列をもう一度クリックで逆順）。
  数値の列は降順、文字列の列は昇順から始まる
- 検索 — 人が並ぶ一覧には検索ボックスが付き、ユーザ名（メールアドレスのローカル部）と
  メールアドレスの部分一致で絞り込む（部署・チーム単位の集計表は対象外）
- 判定フィルタ — 推奨一覧のみ。判定（変更推奨・要観察 など）で絞り込む
- 高さの変更 — 表のスクロール領域は、その直下のバーをドラッグして縦の高さを変えられる
  （動かしても見え方が変わらない短い表にはバーが出ない）

一覧は表も棒グラフも最初から全行を表示する（絞り込みで隠れる行だけが減る）。

JavaScript が無効な環境でも内容はすべて読める。その場合は上記の操作用の UI が現れず、
すべてのタブの中身が見出し付きで縦に積まれる（テーマの切替ボタンも出ないが、明暗は OS の
設定に追従する）。

## 前月からの変化（正式分析のみ）

正式分析（`analyze`）では、過去月のスペンドがそろっていれば report / dashboard の
サマリ直後に「前月からの変化」セクションが出る。対象月と、その直前に存在する月
（欠月があれば飛ばした直前の月）を比べ、次を示す。

- 利用開始 / 利用停止 — 需要が新たに立ち上がった、または止まったメンバー
- 主な増減 — 需要が大きく増減したメンバーを増減額の大きい順に表示
- 実課金の新規発生 — 前月まで従量課金ゼロだったのに当月に発生したメンバー
- 月次推移 — 直近数ヶ月の API 換算需要・実課金・アクティブ人数

シート判定やヒステリシスには影響しない表示専用のセクション。初月（比較対象が無い月）は
出ない。表示閾値は `config.yaml > trend` で調整できる。

## 月中の利用推移（スナップショット差分）

同じ月のスペンドを、月初〜当日の累積で複数回エクスポートしておくと（例: 毎週金曜に
「7/1〜当日」でエクスポート）、それらの差分から月の途中での利用の伸び・停止を検出できる。
対象月の `spend/` に月初開始（1日〜）の累積エクスポートが2つ以上あると、正式分析・速報の
どちらでも「月中の利用推移（スナップショット差分）」セクションが自動で出る（フラグ不要）。
主データには従来どおり期間の広いファイルを使い、狭いファイルは差分の計算に使う。

- 停止疑い — 直近の区間で需要の伸びが止まったメンバー（休暇・案件の谷でも起こるため断定には本人確認が必要）
- 停止した Standard ユーザで実課金ゼロの場合、停止時点の累積需要は「シートに含まれる
  利用量（allowance）」の実測候補になる（従量課金が有効なら本来はそこで課金が始まるため）
- 込み量の消化 — 区間の途中で実課金が 0 から発生したメンバー（実効込み量がその区間の
  累積需要の間にあると分かる）

閾値（停止とみなす増分・停止判定の前提となる累積需要・判定する最小区間日数）は
`config.yaml > snapshot_diff` で調整できる。エクスポートは月初1日開始で揃えること
（1日開始でない期間のファイルは差分対象から外れる）。

### 月中のメンバー変動（members スナップショット）

members も同じ月に単日スナップショットを複数置くと（例: `members-...-2026-07-05.csv` と
`members-...-2026-07-16.csv`）、隣り合う時点の差分から月中のシート変更・メンバーの追加・
削除を検出し「月中のメンバー変動（スナップショット差分）」セクションに出す。日付付きの
members ファイルが対象月に2つ以上あると自動で発動する（月のみの `members_2026-07.csv` は
時点が定まらないため差分の対象外）。変動が無くてもセクションは出し、スナップショットを
取ったこと自体を記録する。当月の損益分岐判定は最新スナップショット時点のシートで行うため、
月中にシート変更があったユーザには参考値である旨の警告を添える。判定・ヒステリシスの数値
そのものは変わらない。

### 月中の Claude Code 活動（code-analytics スナップショット）

code-analytics も同じ月に日付付きスナップショットを複数置くと、累積 LoC（あれば PR 数）の
月中の伸びを「月中の Claude Code 活動（code-analytics 差分）」セクションに出す。さらに
スペンドの停止疑いと email で突き合わせ、停止したユーザの LoC も止まっていれば「停止の傍証」、
逆に LoC が伸びていれば「利用継続の形跡あり（スペンドとの食い違いは要確認）」という注記を
停止疑いの箇条書きに添える。これも表示専用でシート判定には影響しない。

## Codeと他プロダクトの需要（API換算）（dashboard のみ）

メンバー別タブの下部に出る、金額（API 換算需要）を Code とそれ以外に分けて見るセクション。
棒グラフと内訳表の2つで構成される。対象は対象月のスペンドに明細のあるユーザで、利用ゼロの
メンバーと組織サービス利用の行は含まない。

- 棒グラフ — 需要の合計を棒の長さに、Code の割合を色の切り替え位置に取る。Code の需要を
  確定できないユーザは斜線の棒（内訳不明）になる
- 内訳表 — 需要（計）・Code需要・Code比率・他product需要・product数。他product需要 =
  需要（計） − Code需要。「—」は確定できなかった値で、0 ではない
- ⚑ — 補助プロダクト（`product_policy.supplementary` に挙げた product）の需要が
  `supplementary_high_usd` 以上であることの印。他product需要のセルに付くが、判定に使うのは
  supplementary に挙げた product の需要合計なので、どちらの分類にも書いていない product の
  需要は額には含まれても ⚑ には効かない

読み方は「Code が低く他が高いユーザは、自動での変更ではなくレビューの対象」。この判断は
機械化しておらず、シートの判定・推奨にも反映しない（表示専用のセクション）。

「詳細利用状況」の product構成 とは基準が違う。あちらは利用回数（リクエスト数）基準の構成比で、
こちらは金額（API 換算需要）基準。回数の多い product と金額の大きい product は一致しないため、
2つの並びが違っていてもデータの矛盾ではない。

### product の分類（config.yaml > product_policy）

上のセクションと usage-summary の `supplementary_high` / `prohibited_observed` 列は、
この設定を根拠に計算される。シート判定に使う需要は分類に関係なく全 product の合計で、
この設定が変えるのは活用の見方だけ。

- `primary` — 活用評価の主軸にする product。ここに挙げたものの需要が「Code需要」になる
  （既定は Claude Code）。空にはできない
- `supplementary` — 補助的な利用面（既定は Chat・Cowork・Design・Research・Code Review・
  Claude in Slack）
- `prohibited` — 組織の方針で使わせない product（既定は空）。観測されると警告に出る。
  シート判定には影響しない
- `supplementary_high_usd` — supplementary の需要合計がこの額以上なら ⚑（既定 $100/月）

primary と supplementary は排他で、同じ product 名を両方に書くとエラーになる。prohibited は
分類と直交する指定なので、primary・supplementary に挙げた product を重ねて書いてよい。

product 名の照合は、正規化（前後空白の除去・大小文字・Unicode の正規化形式）後の完全一致で行う。
部分一致・あいまい一致はしない（`Code Review` が `Claude Code` に一致するような取り違えは、
費用ではなく活用の評価を歪めるため）。CSV 側の表記ゆれは、リストに名前を並べて吸収する。

## usage-summary の列

ユーザ単位の product 利用特徴量。email + 8つの特徴量の計9列で、行は対象月のスペンドに明細の
あるユーザ（メールアドレス昇順）。利用ゼロのメンバーは行を持たないため recommendations とは
対象がそろわず、組織サービス利用の行も含まない。

- `email` — ユーザのメールアドレス
- `total_demand_usd` — 全 product の API 換算需要 [USD/月]
- `code_demand_usd` — primary に挙げた product の需要
- `code_demand_share` — `code_demand_usd` ÷ `total_demand_usd`（需要の合計が 0 のユーザは
  比を定義できないため空欄）
- `total_requests` — 全 product のリクエスト数
- `code_requests` — primary に挙げた product のリクエスト数
- `product_breadth` — そのユーザの全リクエストの 5% 以上を占める product の数
- `supplementary_high` — supplementary の需要合計が `supplementary_high_usd` 以上か（True / False）
- `prohibited_observed` — prohibited に指定した product の明細行が1行でもあるか（True / False）

金額は小数2桁、比率は小数4桁で書き出す。値は分析時に計算したものをそのまま出しており、この
CSV のための再計算はしない。

確定できなかった値は空欄にする。0 や False で埋めると「観測した結果が 0 だった」ことと区別が
つかなくなるため、欠損は欠損のまま出す。product 名が空の明細行があるユーザや、product 列
そのものが無いスペンドレポートでは、その行をどう数えるかで結論が変わる特徴量が空欄になる
（変わらないものは確定した値が入る）。

## decision-evidence の列（V2 判定）

V2 判定の結論と、その結論を出すのに使った材料。V1 の判定（report / details / dashboard /
recommendations）とは別系統の出力で、V1 の内容には影響しない。

出力するのは正式分析で判定の版が v2 のときだけ。版は「CLI の `--decision-version` の明示指定 >
`config.yaml > decision_v2.enabled` > v1」で決まる。`enabled: true` は「V1 の成果物に V2 判定の
根拠を併記する」ワークスペースの opt-in で、主判定が V2 に変わるわけではない（`--decision-version v1`
を渡せばその実行では併記しない）。速報モード（`--preview`）は V2 判定を行わないため出力されず、
`--decision-version` との併用もできない。

行は members ∪ 対象月のスペンドのユーザ（メールアドレス昇順）で、recommendations と同じ対象。

- `email` — ユーザのメールアドレス
- `workspace` — 複数 workspace の組織（config の `workspaces` が2つ以上）でだけ `email` の
  次に出る列で、主 workspace の名前。複数アカウントを持つ人は主 workspace の行1本にまとめ、
  需要（全 product・Code・補助）は全 workspace の合算、実課金・κ・現シート・シート変更は
  主 workspace の値を使う。副 workspace にだけアカウントがある人の行は無い
- `subject_id` — 解決した stable ID（`account:` / `user:` / `email:` の接頭辞つき。確定できなければ空欄）
- `identity_quality` — `stable` / `email_consistent` / `email_fallback` / `conflict` / `unresolved`
  （複数 workspace の組織では、副 workspace でその email の Identity が衝突した場合も
  `conflict` になるが、`subject_id` は主 workspace の解決結果のまま）
- `current_seat` — 対象月末時点のシート（`standard` / `premium` / `unassigned` / `unknown`）
- `month` — 対象月
- `complete` — 対象月のスペンドが全月ぶんか（True / False）
- `complete_months` — 履歴のうち完全月（`;` 区切り・昇順）
- `total_demand_usd` — 対象月の全 product の API 換算需要 [USD/月]
- `code_demand_usd` — 対象月の primary product の需要（確定できなければ空欄）
- `supplementary_high` — supplementary の需要が閾値以上か（確定できなければ空欄）
- `billed_extra_usd` — 対象月の実課金
- `credit_limit_usd` — 追加クレジット上限 κ（空欄は不明・`0.00` は無効・`inf` は上限なし）
- `premium_justification_usd` — 判定に使った方針線（`decision_v2.premium_justification_usd`）
- `status` — 結論（`recommended` / `observe` / `no_decision` / `keep` / `excluded`）。
  `excluded` はシート未割当のほか、主 workspace に `fixed_seat` を設定した組織の
  Standard / Premium のアカウントにも付く
- `seat_action` — シートへの推奨（`upgrade_to_premium` / `downgrade_to_standard` /
  `review_assignment` / `keep` / `none`）
- `credit_action` — 追加クレジットへの推奨（`enable_with_cap` / `review` / `keep` / `none`）
- `reason_codes` — 判定の根拠・保留理由（`;` 区切り・主理由 → 補助 → 情報の固定順）
- `policy_stability` — 方針線を ±$100 ずらしても同じ seat action になった数（1〜3。経済軸に
  到達しない判定は空欄）
- `suggested_credit_cap_usd` — `credit_action` が `enable_with_cap` のときに提示する初期上限

金額は小数2桁、確定できなかった値は空欄にする（usage-summary と同じ流儀）。語彙の一覧と
各理由コードの意味は実装設計書の §12 が唯一の源。

履歴（判定が見る月の並び）は次のように組む。

- 対象月から古い方へ遡り、暦で連続する月だけを採る（間に欠月があればそこで打ち切る）
- 複数 workspace の組織では、副 workspace にシートのあるアカウント（Standard / Premium と、
  スペンドにだけ現れる不明）がある人について、副の部分月を不完全月に数え、副を使い始めた後に
  副のスペンドが無い月（欠月）があればその月以前を履歴から外す。副にシートの無い人（副で
  未割当の人を含む）の履歴は主 workspace のまま
- スペンドに明細が無い月は需要ゼロの観測として入れる（利用ゼロは観測であって欠損ではない）
- シート変更 event から加入が読み取れる場合は、加入より前の月を履歴から外し、加入がまたがる
  月を完全月に数えない（在籍が月全体に及んでいない月を完全月として扱わないため）

既知の制約が3点ある。

- 日付つきの members スナップショットが2つ未満の組織では加入・シート変更の event を作れない。
  この場合は加入の考慮が働かず、実際には在籍していなかった月も需要ゼロの観測として履歴に入る
- Identity は対象月の行だけで解く。月をまたいだメールアドレスの変更は同一人物として束ねない
  （過去の全期間を一括で解くと、退職者のアドレスが再割当された場合に別人どうしが1人へ結合する）
- 管理画面の当月消費（`input/<組織名>/admin/`）はまだ読まない。実課金はスペンドレポートの
  `net_spend` 由来で、この列を持たない旧形式のレポートでは実課金 0 として扱う

## github-summary の列（GitHub の参考値）

GitHub の merged PR 数とリードタイムの参考値。GitHub 分析を有効にした組織で、対象月の
キャッシュ（`collect --source github`）があるときだけ正式分析が書く。

シートの判定には一切使わない。report / details / dashboard / recommendations /
usage-summary の内容はこのファイルの有無で変わらず、逆にこのファイルの値が判定へ入る
こともない。PR 数もリードタイムもチームの働き方と repository の性質に強く依存するため、
値の大小から因果（活用度が高い・低い）を断定する材料にはならない。個人行はメール
アドレス昇順で、件数の多い順には並べない（個人のランキングを作らないため）。

行は次の2種類で、`scope` 列が区別する。

- `user` — `members-info.csv` の `GitHub ID` 列に login を書いた人全員。その月の PR が
  0 件の人も行を持つ（0 件であることも参考情報のため）
- `organization` — 末尾の1行。組織全体の値で、集計から外した分の内訳もここに入る

各列の意味は次のとおり。

- `scope` — 行の単位（`user` / `organization`）
- `email` — ユーザのメールアドレス（組織全体行は空欄）
- `github_login` — 対応表に書かれた GitHub の login（組織全体行は空欄）
- `month` — 対象月
- `merged_pr_count` — その月に merge された PR の件数。個人行は本人が作成した分、
  組織全体行は対象 repository の Bot 以外の PR 全件（個人へ帰属した分 + 対応表に無い
  作成者の分 + 削除済みアカウントの分）。対応表の記入状況で組織全体の母数は動かない
- `lead_time_median_hours` / `lead_time_p75_hours` / `lead_time_p90_hours` —
  `merged_at − created_at` の時間（小数1桁）。Draft だった期間も含み、日時は UTC で
  計算する。3点はいずれも線形補間の百分位（Excel の `PERCENTILE.INC` と同じ）で、
  PR が 0 件の行は3列とも空欄
- `unmapped_authors` — 対応表に無い作成者の人数（組織全体行のみ）
- `unmapped_prs` — その作成者による PR の件数（組織全体行のみ）
- `bot_prs` — Bot が作成した PR の件数（組織全体行のみ）
- `deleted_author_prs` — 作成者が削除済みアカウントの PR の件数（組織全体行のみ）
- `excluded_repository_prs` — 対象外 repository の PR の件数（組織全体行のみ）
- `total_prs` — キャッシュが持つ PR の全件数（組織全体行のみ）。個人行の
  `merged_pr_count` の合計に `unmapped_prs`・`bot_prs`・`deleted_author_prs`・
  `excluded_repository_prs` を足した数に一致する（組織全体行の `merged_pr_count` は
  Bot と対象外 repository の分を含まないので、この検算には使わない）
- `cache_complete` — 対象月の収集を読み切ったか（True / False）。False のときの件数は
  部分的な値

repository 名・GitHub の Organization 名・対応表に無い作成者の login はこのファイルに
書かない（対象外 repository と対応表に無い作成者は件数だけを載せる）。

次の場合は書かず、実行時の出力に理由と次の一手を出す。

- 対象月のキャッシュが無い（`collect --source github` をその月について実行していない）
- キャッシュに repository の一覧が無い（一覧を保存する前に作られたキャッシュ。収集を
  もう一度実行すると一覧が付く）
- 速報モード（`--preview`）

前回の github-summary が残っている場合もツールは消さない。有効にした組織で書けなかった
実行では、そのファイルが今回は更新されないことを伝える。`organizations` から外した組織に
ついては、残っているファイルにも触れない。

## 複数スペース（workspace）の組織の成果物

`config.yaml` の `organizations.<組織名>.workspaces` が 2 つ以上ある組織では、
組織 1 セットの成果物に全スペースのアカウントを載せる。サマリの対象メンバー数は
「人数（アカウント数）」とし、「スペース別」表に各スペースのシート構成・費用・需要を示す。
件数（一次判断や変更推奨など）はアカウント単位。複数組織の横断サマリでも
「人数（アカウント数）」を使う。月中の推移などは表示名つきの見出しでスペースごとに出る。

判定系の全アカウント表では、複数アカウントを持つ人の主の行を全スペースの合算需要で
判定する。したがって、この表の需要を縦に足すと副の分が二重になる。観測系の
詳細利用状況、スペース別表、人別の利用では各アカウント自身の需要を使い、
人の需要は全アカウントの和として示す。

report の「複数スペースの利用」には、副のシートを持たない人の払い出し判定、
副にシートを持つ人の継続判定、副を持ちながら主で実課金が発生した人の一覧がある。
払い出し判定は主の実課金を副の損益分岐 `secondary_breakeven_usd` と比較する。
対象月に主の上限へ到達した場合、または必要な連続月数の実課金が損益分岐以上なら
「候補」。主の追加クレジットが無効の人、有効かどうか分からない人（上限が未記入で
実課金も観測されていない）、主のシートが払い出すシート種別と違う人は「判断材料なし」。
それ以外で実課金があれば「観察」、なければ「不要」。
継続判定は副の需要を損益分岐と比較する。以上なら「継続」、払い出し後の観測月が
`evaluation_months` に満たなければ「データ蓄積待ち」、必要月数すべてが損益分岐未満なら
「戻す候補」、それ以外は「観察」。損益分岐の既定は副の固定シートの価格。
主で実課金が出た人の一覧は判定ではなく、副への切り替え状況を月別の副の需要と
突き合わせて読むための事実。詳しい読み方は report 内の凡例にある。

details には「人別の利用」、dashboard には「複数スペース」タブが加わる。
recommendations と usage-summary の CSV には email の次に `workspace` 列が入り、
ディレクトリ名で各アカウントを区別する。V2 の decision-evidence の列は
[decision-evidence の列](#decision-evidence-の列v2-判定)を参照。

速報の preview と preview-dashboard では「スペース別」「人別の需要（スペース合算）」を
出し、一次判断テーブルにスペース列を足す。主の行の需要は全スペースの合算、
スペース別と人別の需要は各アカウント自身の観測値と月末ペース換算を使う。
固定シートの副アカウントは一次判断の対象外。ただし利用がほぼ無ければ遊休候補として
示す。速報には副の払い出し判定・継続判定を置かず、正式分析で行う。

### 設定（config.yaml > organizations.<組織名>.workspaces）

- `primary` — 主スペースだけを `true` にする
- `label` — 成果物に出すスペースの表示名
- `fixed_seat` — 運用方針で固定するシート種別。副スペース（複数 workspace の組織）のための設定。
  指定された workspace のアカウントは Standard / Premium の損益分岐判定・感度分析・
  追加クレジット付与候補の対象外
- `credit_limit_default_usd` — 副アカウントの追加クレジット上限の既定
- `evaluation_months` — 払い出し判定と継続判定に必要な連続月数（省略時は `decision.hysteresis_months`）

## 追加クレジット（usage credits）の上限

Team プランの各シートには利用の込み枠（レート制限型）があり、組織やユーザ設定で「追加
クレジット」（枠超過時の従量課金）を有効化できる。上限はユーザごとに管理画面で設定される。
この設定はエクスポート CSV に含まれないため、`members-info.csv` の追加クレジット上限の列に
手入力する。複数スペースの組織では、副アカウントの上限は `credit_limit_default_usd` を既定とし、
主アカウントは従来どおり members-info の列を使う。列の値の意味は次のとおり:

- 正の数値（`$` やカンマ可）: その金額を上限に従量課金が有効
- `0`: 無効（枠超過分は課金されず、需要は上限で頭打ちになる）
- `無制限` / `unlimited` / `inf` / `∞`: 上限なしで有効
- 空欄: 不明（当月までに実課金が観測されていれば、無効なら課金は発生し得ないため有効と自動確定する）

上の4語以外は数値として解釈する。数値として受けるのは半角の数字（符号・小数点・4 桁までの指数）に
`$`・`＄` と桁区切りのカンマが付いた形だけで、全角数字（`２５０`）・円記号を含む値（`￥1,500`）・
桁区切りに `_` を使った値（`1_0`）・`Infinity` や `1e309` のような非有限の値・負値は「不明」として
警告に載せる。

クレジットの有無で分析の見方が変わる。有効なユーザは超過分が実課金（spend の net_spend）として
観測されるため、実課金がセンサーとして働く（実課金ゼロなら枠内と判断でき、上限到達疑いの
フラグは付けない）。無効なユーザは超過需要が課金に表れず、絞り（throttle）として観測不能に
なるため、「Standard時/Premium時」の枠超過分は実際には請求されない「絞り負担のドル換算」
（需要が上限で抑えられる分の目安）である点に注意する。

追加クレジット上限を入れておくと、正式分析・速報に次が加わる（列が空・実課金ゼロなら従来どおり）:

- サマリに追加クレジットの構成（有効/無効/不明の人数・上限の合計）
- 上限到達（実課金が上限に迫る）・整合性の警告（上限を超過している/無効なのに課金がある）
- シートが吸収した量の実測（E = API換算需要 − 実課金）の分布。E は各ユーザの容量の下限を示す
  （上限は分からないため、E が小さいことは容量に余裕がないことを意味しない）
- 昇格の前に、まず上限つきクレジットを付与して1ヶ月の課金実測で判断すべきユーザ（付与候補）。
  dashboard ではこのカードを常に表示し、候補がいない場合は「該当者なし」と「判定不能
  （上限が未記入）」を区別して示す
- 速報では、有効ユーザの残額と到達見込み（観測実課金ペースの線形外挿による目安）

## GitHub 分析の有効化（config.yaml > organizations）

GitHub の情報（PR 数・リードタイム）を参考値として使うかどうかは組織ごとに決める。
`organizations` に書いた組織だけが対象で、書かない組織は GitHub 関連の処理と警告から
一切除外される。

```yaml
organizations:
  example:
    github_org: example-org
```

キーは入力ディレクトリ直下の組織名、値はその組織の GitHub Organization 名。両者は
一致しない前提の対応表なので、名前が同じ組織でも省略はできない。`organizations` の
直下だけは既定に無いキー（組織名）を書けるが、その中身に書けるのは `github_org` だけで、
値が GitHub の Organization 名として読めなければ設定の読み込みでエラーになる。

### GitHub ID の列（email → GitHub login の対応表）

PR を誰の実績として数えるかは、`members-info.csv` の `GitHub ID` 列が決める。GitHub の
API から email は取れないため人手で記入する列で、有効にした組織だけが使う。値は3種類:

| 値 | 意味 |
|---|---|
| 空欄 | 未記入。未対応として扱い、doctor が記入を促す |
| `なし` / `none` / `-` | GitHub のアカウントを持たない。未対応として扱うが警告しない |
| それ以外 | GitHub の login（英数字で始まり英数字で終わる 1〜39 文字。区切りに使えるのは連続しないハイフンと高々 1 個のアンダースコア） |

login として読めない値は未対応として扱い、写し間違いに気付けるよう warning に出す。
email の重複と login の重複（大文字小文字を区別しない）は error で、対応表としては
読まない（別人の PR を帰属させたまま集計が完走しないようにするため）。日付つきの
members-info を置いている組織では、対象月の月末以前で最新のファイルの列を読む。

有効にした組織では `seat-analyzer doctor` が次を検査する。読み取りだけを行い、PR も
repository も取得しない。

- GitHub CLI（`gh`）で認証できているか。できていなければ error
- token の権限（`read:org` と `repo`）が足りているか。足りなければ error。権限を
  機械的に確認できない token（fine-grained PAT・GitHub App）では判定しない
- 設定した Organization を参照できるか。参照できない場合と、SAML SSO の承認が要る
  場合を区別して error
- GitHub API の利用上限に達していないか。達していれば warning（再実行で解消する）
- `input/<組織名>/members-info.csv` の `GitHub ID` 列（email → GitHub login の対応表）の
  有無と中身。ファイルが無い場合・列が無い場合・対応づかないメンバーが居る場合は
  warning、対応表そのものが壊れている場合は error（誤った対応で集計を完走させない）

`organizations` に書いた組織名が入力の組織ディレクトリのどれとも一致しない場合は、
`=== 設定検査 ===` として warning を出す。綴り違いで検査が黙って全部飛ぶのを防ぐため。

### github-cache（collect が作るキャッシュ）

`seat-analyzer collect --org <組織名> --source github --month YYYY-MM` は、対象月に merge
された PR のメタデータを `input/<組織名>/github-cache/prs-YYYY-MM.json` に保存する。
1ファイル = 1組織 × 1月で、PR は merge した日時（UTC）の月に帰属する。ツールが読み書きする
ファイルで、手で編集する前提ではない。

PR 1件につき保存するのは次の9項目だけ。title・本文・レビュー本文・変更ファイル・diff・
コミットメッセージ・コードは取得も保存もしない。

| 項目 | 内容 |
|---|---|
| `repository` | repository 名（Organization 名は含まない） |
| `number` | PR 番号 |
| `author_login` | 作成者の GitHub login（削除済みのアカウントは空） |
| `author_type` | 作成者の種別（`User`・`Bot` など。同上） |
| `created_at` | 作成日時（UTC） |
| `merged_at` | merge 日時（UTC） |
| `additions` | 追加行数 |
| `deletions` | 削除行数 |
| `is_draft` | draft かどうか |

PR を指すキーは `repository#番号`（例: `repo-a#12`）で、同じ PR を2度保存しない。
repository 名の大文字小文字は区別せず同じ1つとして扱う。

`repositories` には収集した時点の repository の一覧（archived / fork / template を除いた
名前と、除いた件数）が入る。分析はこの一覧を集計の対象として読むので、`gh` もネット
ワークも呼ばずにキャッシュだけで参考値を出せる。一覧を保存する前に作られたキャッシュは
そのまま読めるが github-summary の材料にはならないので、収集をもう一度実行して一覧を
付ける。

`complete_windows` は「読み切れて、かつ期間の終わりから1日が過ぎた」期間の一覧。月は
1–7 / 8–14 / 15–21 / 22–28 / 29–月末 の固定の期間に分けて収集し、この一覧に入っていない
期間は次回の実行で取り直す。1日の猶予を置くのは、検索の反映遅れで日付境界の PR を
取りこぼさないため。したがって対象月の全期間が揃うのは翌月2日以降になる。

読み込みは厳密で、形式の版・組織名・対象月が合わないファイル、9項目以外のキーを持つ
ファイル、キーと内容が食い違うファイルはエラーにする（別の組織のキャッシュや、項目を
足したファイルをそのまま集計へ渡さないため）。エラーが出た場合はそのファイルを別の場所へ
移してから収集し直す。

## claude.ai からの CSV 取得（config.yaml > claude_export）

`seat-analyzer collect --source claude` の設定と、取得したファイルの扱い。使い方は
[usage.md](./usage.md) の「claude.ai からの CSV 取得」、初回の設定は [setup.md](./setup.md) の
「claude.ai からの CSV 取得を設定する」。既定は不活性で、組織（または workspace）の区画に
`org_id` を書いたものだけが取得の対象になる。

### 共通の設定（トップレベルの claude_export）

| キー | 既定 | 内容 |
|---|---|---|
| `chrome_path` | `""` | Chrome の実行ファイル。空文字なら OS ごとの既定の場所を探す。書くときは絶対パス（`~` 可） |
| `profiles_dir` | `~/.seat-analyzer/profiles` | 専用プロファイルの置き場。配下の `<profile>` ディレクトリが 1 アカウント |
| `staging_dir` | `~/.seat-analyzer/exports` | Chrome のダウンロード先（取得の一時置き場）。検証の後に `input/` へコピーする |
| `timeout_minutes` | `15` | 取得の完了を待つ上限（分）。`--timeout` を付けた実行ではそちらが優先 |

`profiles_dir`・`staging_dir` の相対パスは、`paths` と同じくそれを書いた設定ファイルの
置き場所が基準になる（`~` はホームディレクトリに展開し、絶対パスはそのまま使う）。
`chrome_path` は設定ファイルの置き場所で解決しないので、絶対パスか空文字で書く（相対パスは
設定の読み込みでエラーになる）。空文字のときに探す場所は、macOS が
`/Applications/Google Chrome.app`、Windows が Program Files・Program Files (x86)・
LocalAppData の下の `Google\Chrome\Application\chrome.exe`、それ以外が PATH 上の
`google-chrome`・`google-chrome-stable`・`chromium`・`chromium-browser`。

### 組織・workspace の区画

単一スペースの組織は `organizations.<組織名>.claude_export`、複数スペースの組織は
`organizations.<組織名>.workspaces.<workspace名>.claude_export` に書く。

| キー | 内容 |
|---|---|
| `profile` | 使うプロファイル名（英数字と `.` `_` `-`。`.` と `..` は使えない）。`--setup`・`--finish-setup`・`--login`・`--list-orgs` に渡す名前と揃える |
| `org_id` | claude.ai の組織 UUID（8-4-4-4-12 桁の16進。`--list-orgs` で調べる） |
| `kinds` | 取得する種別（`members` / `spend` / `code` から重複なく 1 つ以上。省略時は 3 種すべて） |

```yaml
organizations:
  example:
    claude_export:
      profile: corp
      org_id: 00000000-0000-4000-8000-000000000001
      kinds: [members, spend, code]
  example2:
    workspaces:
      main:
        primary: true
        claude_export:
          profile: corp
          org_id: 00000000-0000-4000-8000-000000000002
      second:
        claude_export:
          profile: corp
          org_id: 00000000-0000-4000-8000-000000000003
```

設定の読み込みで次をエラーにする。

- `org_id` が UUID の形でない、`profile` が名前の規則に合わない
- `kinds` に 3 種以外の値や重複がある、空のリストである（`org_id` を書かない区画でも検査する）
- `org_id` を書かずに `profile` や既定と違う `kinds` を書いた区画（有効にしたつもりの設定を
  黙って不活性にしない）
- `workspaces` を持つ組織の直下の区画（どの workspace へ置くかが決まらない）
- 同じ `org_id` を 2 か所に書いた（大文字小文字の違いも同じ UUID とみなす）
- 未知のキー

取得の計画を作る時点で、大文字小文字や文字の合成の違いだけの組織名（同じ組織の workspace
名どうしも）を止める。それを区別しないファイルシステムでは同じディレクトリになるため。

### コマンドのオプション（collect --source claude）

| オプション | 内容 |
|---|---|
| `--month YYYY-MM` | 当月か前月（それ以外はエラー）。省略時は当月 |
| `--org <組織名>` | 取得する組織（複数指定可）。省略時は設定した全組織 |
| `--profile <名前>` | 使うプロファイルで対象を絞る |
| `--dry-run` | 計画を表示して終了する（ブラウザを起動しない） |
| `--keep-browser` | 取得の後も Chrome を終了させない |
| `--timeout <分>` | 取得を待つ上限 |
| `--import <run_id>` | staging に残った実行の検証と配置だけをやり直す（ブラウザを起動しない） |
| `--setup <名前>` | 専用プロファイルを作って Chrome をログイン画面で起動し、人が行う手順（ログイン・拡張機能の読み込み）を表示して終わる（待たない） |
| `--finish-setup <名前>` | `--setup` の後、人の操作が終わってから実行する。そのプロファイルの Chrome を終了させ、同梱の拡張機能が表示した場所から読み込まれていることを確かめてからプロファイルの設定を書く |
| `--login <名前>` | プロファイルの Chrome で claude.ai のログイン画面を開く（終了は待たない） |
| `--list-orgs <名前>` | アカウントが参加している組織の uuid・name・rate_limit_tier・plan を表示する |

`--setup`・`--finish-setup`・`--login`・`--list-orgs`・`--import` は単独で使う（`--list-orgs` だけは
`--timeout`・`--keep-browser` を併用できる）。これらと `--profile`・`--dry-run`・
`--keep-browser`・`--timeout` は `--source github` では使えない。組み合わせの誤りは終了コード 2。

終了コードは、対象のすべての (組織, 種別) を配置できたときだけ 0。1 件でも失敗・時間切れが
あれば 1。

### staging の配置

1 回の取得（プロファイル 1 つ）ごとに `<staging_dir>/<run_id>/` を作る。`run_id` は
`<profile>-<current|previous>-<YYYYMMDD-HHMMSS>`（組織一覧は `<profile>-list-orgs-<...>`。
ローカル時刻）。

```text
<run_id>/run.json                           コマンドが起動の前に書く、その実行の計画（--import が使う）
<run_id>/progress.json                      拡張機能が各手順の後に上書きする途中経過
<run_id>/manifest.json                      拡張機能が最後に書く結果（コマンドはこれを待つ）
<run_id>/<dir>/<kind_dir>/<元のファイル名>  ダウンロードした CSV
<run_id>/orgs.json                          --list-orgs のときの組織一覧
```

`dir` は `<組織名>` か `<組織名>/<workspace名>`、`kind_dir` は `members`・`spend`・
`code-analytics`。manifest の結果は計画の (配置先, 種別) と突き合わせるだけで、配置先の
パスは常に設定から組む（計画に無い結果は捨て、計画にあって結果の無い組み合わせは失敗に
する）。`--import` は run.json から計画を組み直し、その実行の対象月で検証する。run.json の
対象が設定に無いか、UUID が設定と違えば配置しない。

専用プロファイルの設定（`Default/Preferences`）に `--finish-setup` が書くのは、claude.ai からの
自動ダウンロードの許可・ダウンロードの確認を出さないこと・ダウンロード先（staging）だけで、
他の項目は保つ（書き換えるときは元の内容を `Preferences.bak` に残す）。書く前にその
プロファイルの Chrome を終了させ、動いている間は書かない。拡張機能の読み込みは
`Default/Secure Preferences` と `Default/Preferences` の拡張機能の設定で確かめ、同梱の場所と
違う場所から読み込まれていれば書かずに止める。取得の前にはダウンロード先が staging を指して
いることを確かめ、違えば `--setup` と `--finish-setup` を案内して起動しない。

### 配置前の検証

ダウンロードしたファイルごとに次を確かめ、最初に外れた理由を表示して配置しない（ファイルは
staging に残る）。

1. ファイルがあり、空でない
2. 先頭行（64 KiB まで）のヘッダに、種別ごとの必須の列がある。照合は `columns.<種別>` の
   エイリアスと、分析の読み込みと同じ正規化で行う
   - メンバー一覧: email・シート種別
   - 支出レポート: email・model・prompt_tokens・completion_tokens
   - Claude Code analytics: email と月間の LoC（支出レポートとの取り違えを止める）
3. ファイル名の期間が対象月に合う
   - 支出レポート: 期間付きで、開始が対象月の 1 日、終了が対象月の末日以前
   - Claude Code analytics: 期間付きで、開始が対象月の 1 日（終了日は部分月でも月末日に
     なるので見ない）
   - メンバー一覧: スナップショットの日付が対象月の 1 日以降（前月モードでは取得した日＝
     翌月の日付になる）

通ったファイルは元のファイル名のまま入力の種別ディレクトリへコピーする（同名は上書き）。
組織ディレクトリ（入れ子レイアウトなら workspace のディレクトリ）が無ければ作らずに配置しない。

## モデル単価（config.yaml > model_prices）

API 換算需要は、モデル名の部分一致で引いた単価（USD per 1M tokens）で計算する。単価表は
`default-config.yaml` が持ち、プログラムの更新で新しい値が届く（上の「設定について」のとおり
ワークスペース側へは写さない）。どのパターンにも一致しないモデルは `default` の単価で試算し、
その旨が警告に載る。

各パターンは任意で `cache_read`（入力単価に対するキャッシュ読取の倍率）を持てる。持たない
パターンのモデルは `config.yaml > cache_multipliers` の `read` を倍率に使う。

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
  ユーザの観測最大値 ≒ `S_allowance` として `config.yaml > seats` で上書き
- Premium は Standard の 5 倍程度（セッション倍率 1.25x vs 6.25x）を目安に設定
