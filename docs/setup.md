# セットアップ手順（Claude Code に実行させる）

このツールをローカルマシンで動かせる状態にするための手順書。手順の実行者は人ではなく
Claude Code を想定している。macOS / Windows / Linux の差異は、Claude が実際の環境を
確認して判断する前提で書いてある。

やることは 2 つ。プログラムを GitHub Releases から導入し、データを置くワークスペースを
任意の場所に作る。プログラム本体とデータは分かれているため、リポジトリを clone する
必要はなく、git が入っていないマシンでも導入できる。

## 動作環境

- macOS / Windows / Linux で動作確認済み（CI で 3 OS すべてのテストを実行している）
- 必須なのは [uv](https://docs.astral.sh/uv/) のみ。Python（3.11 以上）は uv が用意するため、
  システムに Python が無くてもよい
- 任意で Claude Code CLI。レポートの考察を自動執筆する機能にだけ使う。分析そのものには不要

## 人が行うこと

1. ローカルに Claude Code を用意する（この手順書を実行する主体）
   - インストール手順: https://docs.claude.com/ja/docs/claude-code/setup
   - Claude のサブスクリプション（Pro / Max / Team など）でログインしておく
2. 任意のディレクトリで Claude Code を起動し、次のように依頼する

   ```
   https://github.com/namikawa/claude-team-cost-optimizer の docs/setup.md を読んで、
   その手順どおりにセットアップして
   ```

   すでにリポジトリが手元にある場合は、そのディレクトリで Claude Code を起動して
   「docs/setup.md の手順どおりにセットアップして」と伝えればよい。
3. Claude が確認を求めてきたら答える（ツールのインストール、PATH の変更、ワークスペースを
   作る場所など）
4. 最後にセットアップ結果の報告を受け取り、[usage.md](./usage.md) の月次運用手順へ進む

ここから先は Claude 向けの指示。

---

## ゴール（完了条件）

次がすべて満たされた状態にする。各ステップの検証コマンドの結果で成否を判断し、
最後にまとめて報告する。

1. `uv --version` が通る
2. `seat-analyzer --version` が通る（プログラムが導入されている）
3. ワークスペースができている（`input/` と `config.yaml` がある任意のディレクトリ）
4. 合成データで分析が最後まで走り、レポート一式が生成される
5. 実データを置く組織ディレクトリの雛形ができている
6. 考察の自動執筆に使うヘッドレス Claude Code CLI の呼び出しが通る（任意）

## 守ること

- ワークスペースの `input/` と `reports/` には利用実績とメンバー情報が入る。公開の場所に
  置かない・コミットしない。`seat-analyzer init` が `.gitignore` を作るので、それを編集しない
- 次の操作は勝手に実行せず、何をするかをユーザに伝えて承認を得てから行う
  - `sudo` を伴うもの
  - システム全体へのインストール（Homebrew / winget / apt など）
  - シェルの設定ファイル・PATH・既存の Python 環境の書き換え
  - 破壊的な git 操作（`git reset --hard`・`git clean` など、未コミットの変更を捨てるもの）
- 既存の環境を壊さない。依存関係は uv がツール専用の隔離環境に入れる。
  `pip install` をグローバルに実行しない
- 検証コマンドを飛ばさない。「たぶん入っている」で次のステップへ進まない
- 途中で失敗したら、その場で切り分けてユーザに状況を伝える。エラーを握りつぶして
  「セットアップ完了」と報告しない

## ステップ 0: 環境を確認する

先に何が入っているかを調べ、以降の分岐に使う。無いものがあってもここでは止まらない。

```sh
uv --version
seat-analyzer --version
claude --version
```

OS とシェルも確認する（`uname -sm` / PowerShell なら `$PSVersionTable`）。
必須なのは uv だけ。Claude Code CLI は分析そのものには不要で、考察の自動執筆
（ステップ 6）にだけ使う。

## ステップ 1: uv を導入する

uv は Python 本体の取得・仮想環境・依存解決をまとめて行うツール。ステップ 0 で
`uv --version` が通っていればこのステップは飛ばす。

未導入なら、環境に合うものを 1 つ選び、ユーザに承認を得てから実行する。

- macOS / Linux（公式インストーラ。`~/.local/bin` に入り sudo 不要）

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- macOS で Homebrew がすでにある場合

  ```sh
  brew install uv
  ```

- Windows（PowerShell）

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```

- Windows で winget を使う場合

  ```powershell
  winget install --id=astral-sh.uv -e
  ```

インストール直後は PATH が反映されておらず `uv` が見つからないことがある。その場合は
新しいシェルで確認し直すか、インストーラが表示したディレクトリを PATH に足す。
シェルの設定ファイルを恒久的に書き換える場合はユーザに確認する。

検証:

```sh
uv --version
```

## ステップ 2: seat-analyzer を導入する

最新リリースの wheel を GitHub Releases から直接インストールする。この手順に git は
不要（入っていないマシンでも導入できる）。

まず最新リリースの情報を GitHub API から取得する。

- macOS / Linux（応答の JSON の `tag_name` が最新バージョン、`assets` の
  `browser_download_url` が wheel の URL）

  ```sh
  curl -s https://api.github.com/repos/namikawa/claude-team-cost-optimizer/releases/latest
  ```

- Windows（PowerShell。wheel の URL を直接出力する）

  ```powershell
  (irm https://api.github.com/repos/namikawa/claude-team-cost-optimizer/releases/latest).assets.browser_download_url
  ```

得られた wheel の URL（`.../releases/download/vX.Y.Z/seat_analyzer-X.Y.Z-py3-none-any.whl`）
を指定してインストールする。

```sh
uv tool install "seat-analyzer @ <wheel の URL>"
```

最新バージョンと wheel の URL は
[Releases](https://github.com/namikawa/claude-team-cost-optimizer/releases) のページでも
確認できる（API が使えない場合の代替）。

- 依存（pandas ほか）はツール専用の隔離環境に入る。システムの Python や既存の仮想環境は
  変更されない
- 「実行ファイルの置き場所が PATH に無い」と警告が出たら、`uv tool update-shell` を実行
  するか（シェルの設定ファイルを書き換えるのでユーザに確認する）、警告が示した
  ディレクトリを PATH に足す。場所は `uv tool dir --bin` でも確認できる
- Python のバージョンを理由に失敗する場合は
  `uv tool install --python 3.12 "seat-analyzer @ <wheel の URL>"` と指定して再実行する

検証:

```sh
seat-analyzer --version
seat-analyzer --help
```

サブコマンド `analyze` / `discuss` / `doctor` / `check-text` / `init` / `init-org` が
並べば成功。

## ステップ 3: ワークスペースを作る

プログラムとは別に、データと設定を置くディレクトリ（ワークスペース）を作る。場所は
ユーザに確認する（例: `~/claude-seat-analysis`）。

```sh
mkdir ~/claude-seat-analysis
cd ~/claude-seat-analysis
seat-analyzer init
```

PowerShell では次のようにする。

```powershell
New-Item -ItemType Directory -Force ~\claude-seat-analysis
Set-Location ~\claude-seat-analysis
seat-analyzer init
```

作られるもの:

- `input/` — CSV を置くディレクトリ
- `config.yaml` — 設定の上書きファイル。全行コメントの雛形で、そのままでも動く。
  既定値はプログラムに同梱されているので、ここには既定から変えたい差分だけを書く
- `.gitignore` — `input/`・`reports/`・`config.yaml` を除外する行

以降 `analyze` / `doctor` / `discuss` はワークスペースのルートで実行する。入力・出力・
設定をカレントディレクトリから解決するため、別の場所で実行すると入力が見つからない。
入力・出力だけをワークスペースの外に置く場合は `config.yaml` の `paths.input` /
`paths.output` に書く（相対パスは `config.yaml` の置き場所が基準）。

検証:

```sh
ls input config.yaml
```

## ステップ 4: 合成データで動作確認する

実データを用意する前に、その場で作る最小の合成 CSV でパイプラインが最後まで走ることを
確認する。ワークスペースを汚さないよう、使い捨てのディレクトリで行う（既存のディレクトリは
使わない。下の作成コマンドが「既に存在する」エラーになったら別名にする。最後に削除するのは
ここで自分が作ったディレクトリだけ）。

```sh
mkdir /tmp/seat-analyzer-smoke
cd /tmp/seat-analyzer-smoke
seat-analyzer init-org demo-org

cat > input/demo-org/spend/spend_2026-06.csv <<'CSV'
Email,Product,Model,Request Count,Prompt Tokens,Completion Tokens,Total Net Spend USD
alice@example.com,Claude Code,claude-sonnet-4-5,120,2000000,200000,0
bob@example.com,Claude Code,claude-sonnet-4-5,2400,60000000,6000000,0
CSV

cat > input/demo-org/members/members_2026-06.csv <<'CSV'
Email,Seat Type
alice@example.com,Premium
bob@example.com,Standard
CSV

seat-analyzer analyze --month 2026-06
ls reports/demo-org/2026-06/
```

PowerShell ではヒアドキュメントの代わりにヒア文字列を使う（`@'` は行末、`'@` は行頭に
置く）。

```powershell
New-Item -ItemType Directory $env:TEMP\seat-analyzer-smoke
Set-Location $env:TEMP\seat-analyzer-smoke
seat-analyzer init-org demo-org

@'
Email,Product,Model,Request Count,Prompt Tokens,Completion Tokens,Total Net Spend USD
alice@example.com,Claude Code,claude-sonnet-4-5,120,2000000,200000,0
bob@example.com,Claude Code,claude-sonnet-4-5,2400,60000000,6000000,0
'@ | Set-Content -Encoding utf8 input\demo-org\spend\spend_2026-06.csv

@'
Email,Seat Type
alice@example.com,Premium
bob@example.com,Standard
'@ | Set-Content -Encoding utf8 input\demo-org\members\members_2026-06.csv

seat-analyzer analyze --month 2026-06
Get-ChildItem reports\demo-org\2026-06
```

期待される結果: 終了コード 0 で、メンバー 2 名・シート費用 $150.00/月 の分析結果が表示され、
`reports/demo-org/2026-06/` に `report.md`・`details.md`・`dashboard.html`・
`recommendations.csv`・`usage-summary.csv` の 5 ファイルが生成される。

警告が 2 件出るのは正常。

- 任意カラムなし（`uncached_input_tokens` などのキャッシュ内訳列）— 合成 CSV に無いため。
  この場合は prompt_tokens × 単価にフォールバックする
- `members-info.csv` に未登録のユーザ — 雛形がヘッダ行だけで中身が無いため

確認できたら使い捨てディレクトリを削除し、ステップ 3 で作ったワークスペースへ戻る
（以下の `~/claude-seat-analysis` は例。ステップ 3 で選んだ場所に読み替える）。

```sh
rm -rf /tmp/seat-analyzer-smoke
cd ~/claude-seat-analysis
```

```powershell
Remove-Item -Recurse -Force $env:TEMP\seat-analyzer-smoke
Set-Location ~\claude-seat-analysis
```

## ステップ 5: 実データ用のディレクトリを作る

ワークスペースのルートで、分析対象の組織（Team プランの workspace）ごとに雛形を作る。
組織名はレポートのタイトルと出力パスにそのまま使われる。ユーザに組織名を確認してから
実行する。

```sh
seat-analyzer init-org <組織名>
```

`input/<組織名>/{spend,members,code-analytics}/` と `reports/<組織名>/`、および
ヘッダ行だけの `input/<組織名>/members-info.csv` が作られる。CSV はまだ無くてよい。

エクスポート手順と月次の運用は [usage.md](./usage.md) の「月次運用手順」を参照する。

## ステップ 6: 考察の自動執筆を疎通確認する（任意）

`seat-analyzer discuss`（と `analyze --with-discussion`）は、ローカルの Claude Code CLI を
ヘッドレスで呼び出してレポートの「考察」セクションを書かせる。Anthropic API キーは不要で、
ログイン済みの Claude サブスクリプション枠を消費する。このときレポートの内容
（メールアドレス・利用額を含む）がプロンプトとして Anthropic へ送信されるため、
実データで使う前に、組織の方針上問題ないかをユーザに確認する。分析そのもの
（`analyze`）には不要なので、このステップが通らなくても他の機能は使える。

1. CLI があること

   ```sh
   claude --version
   ```

   無ければ https://docs.claude.com/ja/docs/claude-code/setup に従って導入する。

2. ログイン済みであること。未ログインなら `claude` を対話で起動してユーザにログインを
   依頼する。ログイン操作は代行できないので、必ずユーザに行ってもらう。

3. ツールが使うフラグにそのバージョンが対応していること。次を実行して `ok` 相当が
   返れば疎通している。

   ```sh
   printf 'ok とだけ返してください' | claude -p --safe-mode --output-format text \
     --model opus --effort xhigh --tools "" --strict-mcp-config --no-session-persistence
   ```

   PowerShell では `"ok とだけ返してください" | claude -p ...` のようにシェルに合わせて
   書き換える。未知のフラグだというエラーが出たら Claude Code を最新に更新する。
   `claude` が PATH に無くフルパスで呼ぶ場合は、ワークスペースの `config.yaml` に
   `discussion.command` として書く。

レポートを生成済みの月があれば、LLM を呼ばずにプロンプトの組み立てだけを確認できる。

```sh
seat-analyzer discuss --org <組織名> --month YYYY-MM --dry-run
```

## ステップ 7: 報告する

次をユーザに報告してセットアップを終える。

- 新しく導入したもの（uv・seat-analyzer・Claude Code のバージョン）と、システムに加えた変更
  （インストール先、PATH の変更の有無）
- ワークスペースの場所
- 合成データでの動作確認の結果（生成されたファイル）
- ヘッドレス Claude Code CLI の疎通結果（未疎通ならその理由と、分析自体は使えること）
- 作成した組織ディレクトリ
- 次にユーザが行うこと（claude.ai からの CSV エクスポート。[usage.md](./usage.md) の月次運用手順）

---

## アップデート

導入と同じ手順で行う。最新リリースの wheel の URL をステップ 2 のコマンドで調べ、
その URL を指定してインストールし直す。

```sh
uv tool install "seat-analyzer @ <最新リリースの wheel の URL>"
```

ツールの環境が入れ替わるだけで、ワークスペースの `input/`・`reports/`・`config.yaml` には
触らない。作業ディレクトリはどこでもよい。

設定の注意: モデル単価・シート料金・入力 CSV のカラム対応表は、プログラムに同梱された
既定設定が持っている。これらはアップデートで新しい値が届くので、ワークスペースの
`config.yaml` に写さない（写すとその項目だけ古い値で固定される）。ここに書くのは、
この環境やこの組織に固有の設定に限る。

## Windows での注意

- `uv` と `seat-analyzer` のコマンドはシェルによらず同じ形で動く。ディレクトリの作成・
  ファイルの作成・パイプなど、シェル側の書き方が違うところは上に PowerShell 版を併記した
- 文字コードはツール側で UTF-8 に固定している。日本語 Windows のロケール既定（cp932）では
  表現できない文字がレポートに含まれるが、コンソールへの表示・ファイルへのリダイレクト・
  他のコマンドへのパイプで結果が変わることはない
- 出力先のファイルを Excel やエディタで開いたままだと、レポートを書き換えられずに失敗する
  （他プロセスが開いているファイルを置換できないため）。閉じてから再実行する
- WSL 上の Claude Code から使う場合は Linux 環境に該当する。uv と seat-analyzer を WSL 側に
  導入し、ワークスペースも WSL 側のファイルシステムに置く

## トラブルシュート

- `uv: command not found`（インストール直後）
  PATH が未反映。新しいシェルで確認するか、インストーラが表示したディレクトリを PATH に足す。
- `seat-analyzer: command not found`
  ツールの実行ファイルの置き場所が PATH に無い。`uv tool dir --bin` で場所を確認し、
  PATH に足す（`uv tool update-shell` はシェルの設定ファイルを書き換えるのでユーザに確認）。
  導入されているかどうかは `uv tool list` で分かる。
- インストールが Python のバージョンで失敗する / pandas のビルドで失敗する
  ホイールが提供されていない Python バージョンを引いている可能性が高い。
  `uv tool install --python 3.12 "seat-analyzer @ <wheel の URL>"` とバージョンを
  指定して再実行する。
- 分析実行時に「入力データがありません」と言われる
  ワークスペースのルート以外で実行している。`input/` がある場所へ移動して実行し直す。
  別の場所のデータを使う場合は `--input-dir` / `--output-dir` を指定する（毎回付けずに
  済ませるなら `config.yaml` の `paths.input` / `paths.output` に書く）。
- `config.yaml` に書いた設定が効かない / 「既定に存在しないキーです」と言われる
  上書きファイルはカレントディレクトリの `config.yaml` を読む。ワークスペースのルートで
  実行しているかを確認する。キー名は既定設定と一致していなければエラーになる
  （綴り違いで設定が黙って無効になるのを防ぐため）。
- 分析実行時に「直下の spend/ は旧レイアウトのため分析できません」と言われる
  組織ディレクトリを挟まない配置（`input/spend/` 直下）のデータが残っている。
  次の手順で組織ディレクトリ配下へ移す。
  1. `seat-analyzer init-org <組織名>` で雛形を作る。`--input-dir` で入力先を
     変えている場合は init-org にも同じ `--input-dir` を付ける（付けないと既定の
     `input/` に別のディレクトリができる）
  2. `input/spend/`・`input/members/`・`input/code-analytics/` の中身を
     `input/<組織名>/` 配下の同名ディレクトリへ移す（spend と members は必須、
     code-analytics は任意）
  3. `input/` 直下に `members-info.csv`（日付つきのものも含む）があれば
     `input/<組織名>/` へ移す
  4. 空になった `input/spend/` を削除する（中身が無くても旧レイアウトとして拒否される）
  5. 過去のレポート（`reports/<月>/`）を引き継ぐなら `reports/<組織名>/<月>/` へ移す。
     移さないと記入済みの考察が再生成時に引き継がれない
- `claude` が見つからない、またはフラグが未対応
  Claude Code を導入・更新する。PATH に置けない場合はワークスペースの `config.yaml` に
  `discussion.command` としてフルパスを書く。
- 考察の生成が「他組織情報の混入」で止まる
  複数組織を扱っている場合、他組織の部署名などが一般語と衝突して誤検出になることがある。
  表示された一致箇所を確認し、無害なら `--allow-term <語>` で許可する（詳細は
  [tooling.md](./tooling.md)）。

## セットアップ後

- CSV のエクスポート手順と月次運用: [usage.md](./usage.md) の「月次運用手順」
  スペンドレポートのエクスポートには Owner / Primary Owner 権限が必要で、
  90 日より前には遡れない
- 入力データの事前検査: `seat-analyzer doctor`
- 料金前提の確認: 既定設定のシート料金とモデル単価は特定時点の値なので、使う前に最新の
  公式情報と照合する。変える必要があればワークスペースの `config.yaml` に差分を書く
- Claude Code のスラッシュコマンド `/seat-analysis` で、分析から考察執筆までを
  対話的に実行できる（リポジトリを clone している場合）

---

## 開発者向けセットアップ

ツールを使うだけなら不要。コードを変更する場合はリポジトリを clone して開発環境を作る。

```sh
git clone https://github.com/namikawa/claude-team-cost-optimizer.git
cd claude-team-cost-optimizer
uv sync
uv run pytest
```

- 開発時は `uv run seat-analyzer ...` で実行する（リポジトリの `.venv/` の中身を使う）。
  `uv tool install` で入れたものとは別の環境になる
- サンプル 2 組織での E2E は出力先を分けて実行する。既定の `reports/` に出すと、実データの
  分析結果と混ざり `reports/summary/YYYY-MM.md` を上書きしてしまう

  ```sh
  uv run seat-analyzer analyze --input-dir examples/input --output-dir examples/reports --month 2026-06
  ```

- `seat-analyzer check-text` は「すでに公開されている内容」をリポジトリの HEAD から読む
  ため、リポジトリのルートで実行する（別の場所で実行する場合は `--repo-root` にルートを
  指定する）。用途と使い方は [tooling.md](./tooling.md)
- 設定の既定値は `src/seat_analyzer/default-config.yaml` が持つ。単価・カラムエイリアス・
  閾値の変更はここに入れる（利用者のワークスペースにはアップデートで届く）
- テストは生成物と同梱の見本をバイト単位で比べるため、ワークツリーの改行が CRLF に
  なっていると `tests/test_golden.py` が失敗する（`core.autocrlf` や `core.eol` を
  有効にしていると OS によらず起きる）。改行の差は `git status` にも `git diff` にも
  出ないので、失敗の原因としては見えにくい

  ```sh
  git ls-files --eol | grep 'w/crlf' | grep -v examples/input
  ```

  何も出なければ問題ない。行が出た場合はワークツリーを取り直す。次の操作は未コミットの
  変更を捨てる破壊的な操作なので、`git status` で残したい変更が無いことを確かめ、承認を
  得てから実行する（`git rm --cached` が中断したときに `git reset --hard` を走らせない
  ため `&&` で繋ぐ）。

  ```sh
  git pull --ff-only
  git rm --cached -r . && git reset --hard
  ```

- リリース手順は [release.md](./release.md)
