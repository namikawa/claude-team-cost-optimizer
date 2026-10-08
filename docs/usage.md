# 入力データの準備と月次運用

claude.ai からエクスポートした CSV を配置し、毎月の分析を回すための手順。
セットアップが済んでいる前提で書いてある。

コマンドはワークスペース（`input/` と `config.yaml` があるディレクトリ）のルートで実行する。
リポジトリを clone した開発環境では `uv run seat-analyzer ...` の形で呼ぶ。CSV とレポートを
ワークスペースの外に置いている場合は、その場所を `config.yaml` の `paths.input` /
`paths.output`（または `--input-dir` / `--output-dir`）で指す。

- 環境構築: [setup.md](./setup.md)
- レポートの読み方と判定の仕様: [reference.md](./reference.md)
- 考察の自動執筆と公開テキストの検査: [tooling.md](./tooling.md)

## 入力データの構成（複数組織対応）

組織ごとに `input/<組織名>/` を作り、その配下に
CSV を配置する。組織名はディレクトリ名がそのまま識別子になる（レポートの
タイトル・出力先に使われる）。通常は Team プランの 1 スペースが 1 組織に対応する。
複数のスペースを運用する組織は後述の入れ子レイアウトを使う。

```
input/
  <組織名A>/
    spend/            spend_YYYY-MM.csv        （必須）
    members/          members_YYYY-MM.csv      （必須）
    code-analytics/   cc_YYYY-MM.csv           （任意）
    members-info.csv                           （任意）
    github-cache/     prs-YYYY-MM.json         （collect が作る。手で編集しない）
  <組織名B>/
    ...
```

`members-info.csv` は部署・チーム・職種・備考・追加クレジット上限・GitHub ID をメール
アドレスに紐づける任意のマッピングファイル。組織ディレクトリ直下に置く手動メンテの
ファイル。カラムは email（必須）・部署・チーム・職種・追加クレジット上限・備考・
GitHub ID で、`email` 以外はすべて空欄でよい。組織階層は部署 > チームだが、部署と
チームは別軸として扱うためどちらか一方だけの記入でもよい。日本語ヘッダ
（email,部署,チーム,職種,追加クレジット上限,備考,GitHub ID）と英語ヘッダ
（email,department,team,role,credit limit,note,github login）のどちらも使える。置くと
レポートに部署列・チーム列・部署別サマリ・チーム別サマリ・備考・追加クレジット関連の
表示が追加され（データがある軸のみ）、無ければ従来どおり動作する。
兼務は部署・チームのセルを `;`（半角セミコロン）で区切って複数記載できる（例:
`基盤チーム; SREチーム`）。部署別・チーム別サマリでは兼務者を所属数で均等按分（1/n）
して計上するため、各サマリの縦合計は常に全体と一致する。
分析対象ユーザのうち `members-info.csv` に行が無い人がいると警告が出る（管理画面への
メンバー追加に追記が追従していないと部署別・チーム別サマリの人数が実態とズレるため）。
ファイル自体を置いていない組織では出ない。

`GitHub ID` の列は GitHub 分析を有効にした組織だけが使う（`seat-analyzer doctor` が
記入状況を検査する）。その組織の PR を誰の実績として数えるかがこの列で決まるため、
GitHub の login をそのまま書く。空欄は未記入で記入を促す警告が出るが、GitHub の
アカウントを持たない人は `なし` と書いておくとその警告から外れる。有効にしていない
組織では列そのものが不要で、空欄のままでも何も起きない。

`members-info.csv` は固定名のほか、日付つき（例: `members-info-2026-07-16.csv`）でも
置ける。日付つきが1つでもあると固定名は無視し、対象月の月末以前で最新の日付のものを
採用する（月末以前に無ければ最古へフォールバックして強警告を出す）。管理画面で設定を
変えた日に日付つきで保存し直しておくと、対象月当時の設定でさかのぼって分析できる。
対象月内に日付つきが2つ以上あると、追加クレジット上限の変更（誰が $X→$Y になったか）を
「月中のメンバー変動」セクションに併記する。

雛形は以下のコマンドで作成できる（`input/<組織名>/{spend,members,code-analytics}/` と
`reports/<組織名>/` をまとめて作る。複数指定可）:

```sh
seat-analyzer init-org <組織名>
```

組織が1つだけの場合も同じ構成にする。`input/spend/` のように組織ディレクトリを挟まず
直下に置いた形は受け付けず、移行手順を示してエラー終了する（手順は docs/setup.md の
トラブルシューティング）。組織名 `spend` と `summary` は予約されていて使えない。

## 複数の Team スペースを運用する組織（入れ子レイアウト）

1 つの組織に主スペースと副スペースがある場合、入力をスペースごとに分ける。
`members-info.csv`（日付つきも）と `github-cache/` は組織直下に置く。

```
input/
  <組織名>/
    main/
      spend/
      members/
      code-analytics/
    second/
      spend/
      members/
      code-analytics/
    members-info.csv
    members-info-YYYY-MM-DD.csv
    github-cache/
```

新しい組織の雛形は `seat-analyzer init-org <組織名> --workspaces main,second` で作れる。
従来レイアウトのデータが既にある組織ではこのコマンドは止まる。workspace 名には
`main` / `second` のような一般名を使い、Team 側の表示名は `label` に書く。

`config.yaml` の `organizations.<組織名>.workspaces` に各ディレクトリを登録する。
組織直下には必要に応じて `secondary_breakeven_usd` を書く。

```yaml
organizations:
  <組織名>:
    secondary_breakeven_usd: 125
    workspaces:
      main:
        primary: true
        label: 主スペース
      second:
        label: 副スペース
        fixed_seat: premium
        credit_limit_default_usd: 0
        evaluation_months: 2
```

`primary` は主の印で、ちょうど 1 つに付ける。`label` は成果物に出す表示名。
`fixed_seat` は副スペースで払い出すシート種別が運用方針で固定されているときに書く。
その workspace のアカウントは Standard / Premium の損益分岐判定・感度分析・追加クレジット
付与候補の対象外になる。`credit_limit_default_usd` は副アカウントの追加クレジット上限の既定、
`evaluation_months` は払い出し判定と継続判定に必要な連続月数（省略時は
`decision.hysteresis_months`）。組織直下の `secondary_breakeven_usd` は副の損益分岐の設定で、
省略時は副の `fixed_seat` の価格を使う。workspace が 1 つの組織には `fixed_seat` を書かない。

スペンドレポート・メンバー一覧・code-analytics は、エクスポート元のスペースの
同名ディレクトリへ置く。code-analytics は主と副で同じ名前のファイルになることがあるため、
配置先を取り違えない。組織直下の `spend/` と workspace 配下の `spend/` が共存すると混在
レイアウトとして止まる。

対象月以前に spend が無い workspace は未開始として飛ばす。開始後に対象月の spend が無い場合は
正式分析を止める。利用が無くエクスポートしなかった月は
`analyze --allow-missing-workspace <名前>` で需要 0 として続行できる。速報ではこの指定は使えず、
始まっている全 workspace に対象月の spend が必要。`doctor` はレイアウトの混在、設定と
ディレクトリの不一致、主の設定、workspace ごとの入力、主に居ない副のアカウント、
members-info の未登録・読み取り失敗を検査する。`doctor` も未開始の workspace は検査せず、
警告して飛ばす。

### 既存の組織を複数スペースの構成へ移す

1. `input/<組織名>/main/` を作り、`spend/`・`members/`・`code-analytics/` をその下へ移す。
   `members-info.csv`（日付つきも）と `github-cache/` は組織直下に残す。従来レイアウトの
   データがある組織には `init-org --workspaces` を使えないので手で移す。
2. `config.yaml` の `organizations.<組織名>.workspaces` に、まず主の `main` だけを登録し、
   `primary: true` を付ける。
3. `seat-analyzer doctor --org <組織名>` で構造の検査が通ることを確かめる。
4. 移行前の最終月の `reports/<組織名>/<月>/` を別の場所へ複製し、同じ版のツールで
   `seat-analyzer analyze --org <組織名> --month <月>` を実行して複製と差分が無いことを確かめる。
   workspace が 1 つの間は従来レイアウトとバイト一致し、記入済みの考察も引き継ぐ。
5. `input/<組織名>/second/{spend,members,code-analytics}/` を作り、config に副を登録する。
   `label`・`fixed_seat`・`credit_limit_default_usd` と、必要なら `evaluation_months` を書く。
6. 副のエクスポートを副のディレクトリへ置く。`reports/<組織名>/` はそのまま使う。

## 月次運用手順（毎月月初・組織ごとに実施）

> ⚠️ スペンドレポートは90日より前に遡れません。毎月必ずエクスポートしてください。

1〜3 のエクスポートと配置は、`claude_export` を設定した組織では
`seat-analyzer collect --source claude` で行える（下の「claude.ai からの CSV 取得」）。

1. スペンドレポート（必須） — Owner / Primary Owner のみ
   - claude.ai 左下のイニシャル → Settings > Analytics（対象組織の workspace で）
   - 「How much is Claude costing?」セクション → Export spend report
   - 期間は Custom で前月1日〜末日 を指定
   - ダウンロードした CSV をそのままのファイル名で `input/<組織名>/spend/` に置く
     （従来レイアウトの場合。入れ子レイアウトでは `input/<組織名>/<workspace名>/spend/`）
2. メンバー一覧（必須）
   - 管理画面のメンバー管理からエクスポート（email とシート種別を含むもの）
   - そのまま `input/<組織名>/members/` に置く
     （従来レイアウトの場合。入れ子レイアウトでは `input/<組織名>/<workspace名>/members/`）
   - エクスポートが無い場合は `email,seat_type` の2列 CSV（ファイル名に YYYY-MM を含める）を手動作成でも可
3. Claude Code 分析（任意・活用度分析用）
   - https://claude.ai/analytics/claude-code → Leaderboard → Export all users
   - そのまま `input/<組織名>/code-analytics/` に置く
     （従来レイアウトの場合。入れ子レイアウトでは `input/<組織名>/<workspace名>/code-analytics/`）

ファイル名の解釈ルール（リネーム不要）:

- 期間付き（`...-2026-06-01-to-2026-06-30.csv`、アンダースコア区切りも可）は開始月を対象月とする。
  月をまたぐ期間のエクスポートはエラーになるため、月単位でエクスポートすること
- 日付のみ（`members-...-2026-07-05.csv`）はエクスポート日の月のスナップショットとして扱う
- 同一月にファイルが複数ある場合、期間が包含関係なら広い方を自動採用し、警告を表示する。
  判断できない場合はエラー
- members は月ではなくファイル単位で、対象月の末日に最も近いスナップショットを採用する。
  月末までのデータは翌月の最初の営業日に取得することが多く、対象月末時点のシート構成は
  翌月初のファイルに入っているため（末日から8日以上離れたファイルしか無い場合は、当時の
  構成と異なる可能性が高い旨の警告を出す）。どのファイルを採用したかにかかわらず、月中の
  メンバー変動の検出には対象月内のスナップショットを使う。採用は実行時点で置いてある
  ファイルから選ぶため、過去の月を再実行すると採用ファイルが変わり、判定が変わることがある
- 期間が1ヶ月に満たないスペンドレポートを通常分析に使うと警告が出る（速報モードを案内）
4. 入力データの検査（任意）

   分析の前に、配置した CSV に不足や不整合がないかを確認できる。

   ```sh
   seat-analyzer doctor                           # 全組織を検査（最新月）
   seat-analyzer doctor --org <組織名> --month YYYY-MM
   seat-analyzer doctor --format json             # 機械可読な構造化 issue
   ```

   検査するのはスペンドレポートとメンバー一覧で、対象月の欠損・部分月・ヒステリシス窓の
   欠月・単価表に無いモデル・数値として読めない値・メンバー一覧との突き合わせ不整合
   （spend にいるが members にいない、シート種別が判別できない、未割当なのに利用実績がある）
   を検出する。メンバー一覧が対象月のものでない場合も警告するが、対象月末の直後（7日以内）の
   スナップショットを採用したときは出さない（月末までのデータを翌月初に取得する通常の運用経路の
   ため）。分析を止めるべき問題（error）があれば終了コード 1 を返し、警告だけなら 0 を返す。
   レポートは書き換えず、判定にも影響しない読み取り専用の検査
   複数スペースの組織では構造・workspace ごとの入力・人（主に居ない副のアカウント）も
   検査する。両レイアウトで members-info の未登録と読めないファイルも検査する。
5. 分析実行

   ```sh
   seat-analyzer analyze                          # 全組織を一括分析（最新月）
   seat-analyzer analyze --month YYYY-MM          # 月を指定
   seat-analyzer analyze --org <組織名>           # 特定組織のみ（複数指定可）
   seat-analyzer analyze --with-discussion        # 考察の執筆まで行う（tooling.md）
   seat-analyzer analyze --decision-version v2    # V2 判定の根拠も併記する
   ```

   リポジトリを clone している場合は、Claude Code から `/seat-analysis` を実行すると、
   分析に加えて警告の検証と考察の執筆までを対話的に行える。

   `--decision-version v2` は上の5種の成果物に加えて `decision-evidence` を出力する。
   V1 の判定・成果物・組織横断サマリは変わらない（V2 の判定を並べて比較するための
   追加出力）。省略時は `config.yaml > decision_v2.enabled` に従い、既定は v1。
   速報モード（`--preview`）は V2 判定を行わないため、このオプションと併用できない。
   列の意味は [docs/reference.md](reference.md) を参照

6. 組織ごとに `reports/<組織名>/YYYY-MM/` に以下が生成される。ファイル名は
   `{種別}-{YYYYMM}-{組織名}.{拡張子}` で、共有でフォルダの外へ出しても
   どの組織のいつの分析かが分かる（例: `report-202607-<組織名>.md`）
   - `report-YYYYMM-<組織名>.md` — サマリ + 前月からの変化 + 追加クレジット付与候補 + シート変更推奨 + 注意事項 + データ検証・警告 + 考察
   - `details-YYYYMM-<組織名>.md` — 機械生成の詳細資料（全ユーザ + 部署別/チーム別サマリ + 詳細利用状況 + 組織内の分布 + 月中の推移 + シートが吸収した量の実測 + 感度分析）
   - `dashboard-YYYYMM-<組織名>.html` — 経営層共有用ダッシュボード（自己完結 HTML）
   - `recommendations-YYYYMM-<組織名>.csv` — スプレッドシート二次加工用
   - `usage-summary-YYYYMM-<組織名>.csv` — ユーザ単位の product 利用特徴量（全 product と Claude Code の需要・リクエスト数など。確定できない値は空欄）
   - `decision-evidence-YYYYMM-<組織名>.csv` — V2 判定の根拠（`--decision-version v2` のときだけ）
   - `github-summary-YYYYMM-<組織名>.csv` — GitHub の merged PR 数と lead time の参考値（GitHub 分析を有効にした組織で、対象月のキャッシュがあるときだけ）

   以下では種別名（report / details / dashboard …）で呼ぶ。

   report はアクションと考察を読むための短い文書で、ユーザ単位の数値は details と
   dashboard が持つ。details は dashboard と同じ数値の Markdown 版で、
   考察の執筆（`discuss`）へ渡す資料も兼ねる

   details / dashboard には「詳細利用状況」として、ユーザごとの input/output
   トークン量、モデル利用割合（トークン量基準）、LoC（code-analytics がある場合）を
   出力する。input トークンはキャッシュ読取分を含むため実入力量より大きく見えることがある

   その直後の「組織内の分布（参考値）」は、個々のユーザの数値が組織の中でどの位置に
   あるかを読むための統計量（n・平均・中央値・標準偏差・p25/p75/p90・最大）。母集団は
   シート未割当を除く分析対象ユーザで、利用ゼロのユーザも含む。表示専用でシートの判定には
   使わない。平均は少数の大口利用に引かれるため「平均以下＝低活用」とは読めない点に注意する

   複数組織を一括分析した場合は `reports/summary/YYYY-MM.md` に組織横断サマリ
   （組織別のシート費用・削減見込みと合計）も生成される。この名前だけは月と組織名を
   付けない（担当者へ共有しない内部の文書で、月は既に名前に入っているため）

   ファイル名の規則を変える前に生成した成果物（`report.md` 等）は自動で改名も削除も
   しない。再生成すると新旧が併存するので、旧名の整理は必要に応じて手で行う。
   記入済みの「## 考察」は旧名のレポートからも引き継がれる

## GitHub の PR メタデータの収集（有効にした組織のみ）

GitHub 分析は組織ごとの opt-in で、`config.yaml` の
`organizations.<組織名>.github_org` に GitHub の Organization 名を書いた組織だけが
対象になる（書き方は [reference.md](./reference.md) の GitHub 分析の有効化）。
設定していない組織でこのコマンドを実行すると、設定の場所を示して終了コード 1 を返す。

```sh
seat-analyzer collect --org <組織名> --source github --month YYYY-MM
```

対象月に merge された PR のメタデータを `input/<組織名>/github-cache/prs-YYYY-MM.json`
へ保存する。月ごとに実行するコマンドなので、初回は直近3ヶ月分を月を変えて順に実行する。

- 保存するのは repository 名・PR 番号・作成者・作成日時・merge 日時・追加行数・
  削除行数・draft かどうかだけ。title・本文・レビュー本文・変更ファイル・diff・
  コミットメッセージ・コードは取得も保存もしない
- PR と一緒に、そのとき参照できた repository の一覧（archived / fork / template を
  除いた名前と、除いた件数）も保存する。分析はこの一覧を読むので、`analyze` は `gh` も
  ネットワークも呼ばない
- GitHub 側の情報は参照するだけで変更しない（読み取りの API しか呼ばない）
- 認証は GitHub CLI（`gh`）に委ねる。事前に `seat-analyzer doctor` で認証・権限・
  利用上限を確認できる
- GitHub 側の一時的なエラー（HTTP 502 など）は、少し待って数回まで自動で再試行する
- API の利用上限や、再試行しても解消しないエラーで止まった場合は、それまでに読み切れた
  期間を保存して終了コード 1 を返す。時間をおいて同じコマンドを実行すると続きから収集する
- 対象月がまだ終わっていない場合、期間の終わりから1日が過ぎていない部分は次回の実行でも
  取り直す（検索の反映遅れで日付境界の PR を取りこぼさないため）。同じ PR は何度取っても
  1件にまとまる
- 収集したキャッシュは `analyze` の判定と V1 の成果物には影響しない。参考値の成果物
  `github-summary` の材料になる（列の意味は [reference.md](./reference.md)）

## claude.ai からの CSV 取得（有効にした組織のみ）

月次運用手順の 1〜3（スペンドレポート・メンバー一覧・Claude Code 分析のエクスポートと
配置）をコマンドで行える。`config.yaml` に `claude_export` を書いた組織（スペース）だけが
対象で、初回の設定（専用の Chrome プロファイル・拡張機能の読み込み・組織の UUID の確認）は
[setup.md](./setup.md) の「claude.ai からの CSV 取得を設定する」で行う。設定していない
状態でこのコマンドを実行すると、設定の場所を示して終了コード 1 を返す。

```sh
seat-analyzer collect --source claude                    # 当月モード（設定した全組織）
seat-analyzer collect --source claude --month YYYY-MM    # 前月を指定すると前月モード
seat-analyzer collect --source claude --dry-run          # 計画の表示だけ（ブラウザを起動しない）
```

取得できるのは当月と前月の 2 つのモードだけで、それ以外の月を指定するとエラーになる
（管理画面に他の選択肢が無いため）。それより前の月は従来どおり手動でダウンロードして置く。
当月は実行機のローカル日付で決まる。

| モード | 指定 | 支出レポート | Claude Code analytics | メンバー一覧 |
|---|---|---|---|---|
| 当月 | `--month` を省略するか当月を指定 | 「月累計」 | 表示中の月 | 取得した日のスナップショット |
| 前月 | `--month` に前月を指定 | 「先月」 | 表示を 1 か月戻した月 | 取得した日のスナップショット |

運用例:

- 週次: 当月モードで取得する。速報（`--preview`）と月中の推移の材料になる
- 月初: 前月モードで前月分を取得し、正式分析に使う

どこに何が置かれるか:

- 配置先は `input/<組織名>/{members,spend,code-analytics}/`（入れ子レイアウトでは
  `input/<組織名>/<workspace名>/` の下）。ファイル名はダウンロードしたときのままなので、
  期間や日付の解釈は手動で置いた場合と同じ
- 同じ名前のファイルは上書きする（同じ日の再取得は新しいスナップショットになる）
- 配置の前に、種別ごとの必須の列・ファイル名の組織 UUID（メンバー一覧と支出レポート）・
  ファイル名の期間が対象の組織と対象月に合うかを確かめ、合わないファイルは配置しない。
  組織ディレクトリが無い組織にも配置しない（`init-org` で作る）

プロファイルごとに Chrome を 1 つずつ順に起動し、取得が終わると Chrome を終了させる。
同じプロファイルの取得は同時に 1 つだけで、別の実行が使っている間は止まる。
Chrome の実行ページ（seat-analyzer export）に進み具合が表示され、コマンドにも組織と種別
ごとの途中経過が出る。最後に 1 件ずつ「配置: <パス>」か「失敗: <組織> <種別> <理由>
（<パス>）」を表示し、1 件でも失敗があれば終了コード 1 を返す。取得中にログインや外部
セキュリティ検証の確認が要るときは、実行ページに「要操作」が出る（途中経過が 1 分止まると
コマンドもブラウザの確認を促す）。人がブラウザで済ませれば取得は続く。

失敗したとき:

- 失敗した種別のファイルは staging（既定 `~/.seat-analyzer/exports/<run_id>/`）に残る。
  表示された理由とパスで中身を確かめる（理由の読み方は [setup.md](./setup.md) の
  トラブル）
- 時間切れのときは Chrome を終了させない。ブラウザの表示を確認し、取得が終わったら
  `seat-analyzer collect --source claude --import <run_id>` で検証と配置だけをやり直す
  （ブラウザは起動しない。検証はその実行の対象月で行う）
- 手動でダウンロードして置く運用はいつでも使える

オプション:

- `--org <組織名>`（複数指定可）: 取得する組織を絞る（省略時は設定した全組織）
- `--profile <プロファイル名>`: 使うプロファイルで絞る
- `--keep-browser`: 取得の後も Chrome を終了させない（実行ページのログを確かめたいとき）
- `--timeout <分>`: 取得を待つ上限（省略時は `claude_export.timeout_minutes`、既定 15 分）
- `--import <run_id>`: staging に残った実行の検証と配置だけをやり直す（単独で使う）

staging の実行ディレクトリは配置の後は使わない。ツールは自動では消さないので、不要に
なれば `<staging>/<run_id>` ごと削除してよい。

## 速報モード（部分月データでの一次判断）

導入直後の組織などで月初の正式分析を待たずにシート構成を確認したい場合、
月の途中までのスペンドレポート（例: 1日〜10日）を通常どおり
`input/<組織名>/spend/` に配置して実行する:

```sh
seat-analyzer analyze --preview [--org <組織名>] [--days 10]
```

観測日数はファイル名の期間（`...-2026-07-01-to-2026-07-10.csv` なら10日）から
自動判別される。期間の無いファイル名の場合のみ `--days` で指定する。

- 出力は `reports/<組織名>/<月>/` の `preview-YYYYMM-<組織名>.md` と
  `preview-dashboard-YYYYMM-<組織名>.html`（経営層共有向けの速報ダッシュボード）。
  変更推奨・ヒステリシス判定・正式レポートには影響しない
- 需要を月末ペースに日割り換算し、遊休候補 / Standard候補 / Premium妥当 /
  判断保留 などの一次判断ラベルを付ける。境界付近は判断保留に倒す
- 日割り換算は利用の偏りを補正しない参考値。シート変更の確定判断は
  全月データ2ヶ月分の正式分析で行うこと
- 月初に全月分のエクスポートで同じファイルを上書きすれば、そのまま正式分析に移行できる
- 複数スペースの組織では workspace ごとの一次判断、「スペース別」、
  「人別の需要（スペース合算）」を出す。主の行は全スペースの合算需要で一次判断する。
  観測日数は全 workspace で同じ必要があり、違えば `--days` で指定する。開始済みの
  workspace に対象月の spend が無ければ止まる
