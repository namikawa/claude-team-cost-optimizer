# セットアップ手順（Claude Code に実行させる）

このツールをローカルマシンで動かせる状態にするための手順書。手順の実行者は人ではなく
Claude Code を想定している。macOS / Windows / Linux の差異は、Claude が実際の環境を
確認して判断する前提で書いてある。

## 人が行うこと

1. ローカルに Claude Code を用意する（この手順書を実行する主体）
   - インストール手順: https://docs.claude.com/ja/docs/claude-code/setup
   - Claude のサブスクリプション（Pro / Max / Team など）でログインしておく
2. 任意のディレクトリで Claude Code を起動し、次のように依頼する

   ```
   https://github.com/namikawa/claude-team-cost-optimizer を clone して、
   docs/setup.md の手順どおりにセットアップして
   ```

   すでにリポジトリが手元にある場合は、そのディレクトリで Claude Code を起動して
   「docs/setup.md の手順どおりにセットアップして」と伝えればよい。
3. Claude が確認を求めてきたら答える（ツールのインストール、PATH の変更など）
4. 最後にセットアップ結果の報告を受け取り、[usage.md](./usage.md) の月次運用手順へ進む

ここから先は Claude 向けの指示。

---

## ゴール（完了条件）

次がすべて満たされた状態にする。各ステップの検証コマンドの結果で成否を判断し、
最後にまとめて報告する。

1. リポジトリのルートにいる（`config.yaml` と `pyproject.toml` がある）
2. `uv --version` が通る
3. `uv sync` が成功し、`uv run seat-analyzer --help` が通る
4. `uv run pytest` が全件パスする
5. 同梱の合成サンプルデータで分析が最後まで走り、レポート一式が生成される
6. 考察の自動執筆に使うヘッドレス Claude Code CLI の呼び出しが通る
7. 実データを置くディレクトリの雛形ができている

## 守ること

- `input/` と `reports/` の中身をコミットしない。ここには利用実績とメンバー情報が入る。
  どちらも gitignore 済みなので、`.gitignore` を編集しない
- このリポジトリは公開されている。セットアップの過程で作るメモ・設定・コミットメッセージに、
  組織名・メンバー名・利用金額を書かない
- 次の操作は勝手に実行せず、何をするかをユーザに伝えて承認を得てから行う
  - `sudo` を伴うもの
  - システム全体へのインストール（Homebrew / winget / apt など）
  - シェルの設定ファイル・PATH・既存の Python 環境の書き換え
- 既存の環境を壊さない。Python の依存関係は `uv` がリポジトリ内に作る `.venv/` に閉じる。
  `pip install` をグローバルに実行しない
- 検証コマンドを飛ばさない。「たぶん入っている」で次のステップへ進まない
- 途中で失敗したら、その場で切り分けてユーザに状況を伝える。エラーを握りつぶして
  「セットアップ完了」と報告しない

## ステップ 0: 環境を確認する

先に何が入っているかを調べ、以降の分岐に使う。無いものがあってもここでは止まらない。

```sh
git --version
uv --version
claude --version
python3 --version   # Windows なら python --version
```

OS とシェルも確認する（`uname -sm` / PowerShell なら `$PSVersionTable`）。
必須なのは git と uv の 2 つ。Python は uv が用意するため、システムに無くてもよい。
Claude Code CLI は分析そのものには不要で、考察の自動執筆（ステップ 6）にだけ使う。

## ステップ 1: リポジトリを取得する

```sh
git clone https://github.com/namikawa/claude-team-cost-optimizer.git
cd claude-team-cost-optimizer
```

すでに手元にある場合、clone は不要だが、次のワークツリーの確認を行ってから先へ進む。
以降のコマンドはすべてリポジトリのルートで実行する（サブディレクトリで実行すると
設定ファイルと入力ディレクトリの解決に失敗する）。

手元のクローンは、ワークツリーの改行が CRLF になっていることがある（`core.autocrlf` と
`core.eol` の設定で決まる。Git for Windows のインストーラ既定では CRLF になる）。テストは
生成物と同梱の見本をバイト単位で比べるため、見本が CRLF のままだとステップ 5 で失敗する。
改行の差は `git status` にも `git diff` にも出ないので、失敗の原因としては見えにくい。

```sh
git pull --ff-only
git ls-files --eol | grep 'w/crlf' | grep -v examples/input
```

`git pull --ff-only` は、改行を LF に揃える `.gitattributes` を取り込むために先に行う
（`--ff-only` はマージコミットのエディタが開いて止まるのを避けるため）。2つ目のコマンドが
何も出さなければ以降の操作は要らない。`examples/input` の合成サンプルは CRLF が正しい
ので除いてある。

行が出た場合はワークツリーを取り直す。次の操作は未コミットの変更を捨てるので、残したい
変更があれば先に退避する。

```sh
git rm --cached -r . && git reset --hard
```

`git reset --hard` は HEAD の内容で取り直すので、`.gitattributes` を取り込んだ後に行う。
`&&` で繋ぐのは、`git rm --cached` が中断したときに `git reset --hard` を走らせない
ため（作業ツリーとも HEAD とも違う staged 内容が1つでもあると `git rm --cached` は
何も変更せずに終了する）。新しく clone した場合はこの操作は要らない。

検証:

```sh
ls config.yaml pyproject.toml src/seat_analyzer/cli.py
```

## ステップ 2: uv を導入する

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

## ステップ 3: Python を確保する

このプロジェクトは Python 3.11 以上を要求する（`pyproject.toml` の `requires-python`）。
uv が必要に応じて自動で取得するため、通常このステップで行う操作はない。

ステップ 4 が Python のバージョンを理由に失敗した場合のみ、次を実行してから再試行する。
システムの Python は変更されず、uv の管理領域に入る。

```sh
uv python install 3.12
```

## ステップ 4: 依存関係をインストールする

```sh
uv sync
```

リポジトリ直下に `.venv/` を作り、実行用（pandas / PyYAML / Jinja2）と開発用
（pytest / ruff）の依存を入れる。`uv.lock` があるためバージョンは固定される。
`.venv/` は gitignore 済み。

検証:

```sh
uv run seat-analyzer --help
```

サブコマンド `analyze` / `discuss` / `doctor` / `check-text` / `init-org` が並べば成功。
以降このツールは常に `uv run` 経由で呼ぶ（仮想環境を activate する必要はない）。

## ステップ 5: テストを実行する

```sh
uv run pytest
```

全件パスすることを確認する。失敗が出たら内容を報告し、原因が環境側（Python の
バージョン、依存の解決）かコード側かを切り分ける。パスしないまま先へ進まない。

## ステップ 6: 合成サンプルデータで動作確認する

実データを用意する前に、リポジトリ同梱の合成データ（`examples/input/`）で
パイプラインが最後まで走ることを確認する。

```sh
uv run seat-analyzer analyze --input-dir examples/input --month 2026-06
```

`reports/org-a/2026-06/` と `reports/org-b/2026-06/` に `report.md`・`dashboard.html`・
`recommendations.csv` が、`reports/summary/2026-06.md` に組織横断サマリが生成される。
警告がいくつか表示されるのは正常（合成データはキャッシュ内訳の列などを持たないため）。
終了コードが 0 で、上記のファイルが生成されていれば成功。

注意: この出力先は既定の `reports/` である。すでに実データの分析結果が `reports/` に
ある環境で確認する場合は、`--output-dir` に別のディレクトリを指定して既存の
`reports/summary/2026-06.md` を上書きしないようにする。

検証:

```sh
ls reports/org-a/2026-06/ reports/org-b/2026-06/
```

生成物は gitignore 済みで、消しても同じコマンドで再生成できる。

## ステップ 7: 考察の自動執筆を疎通確認する

`seat-analyzer discuss`（と `analyze --with-discussion`）は、ローカルの Claude Code CLI を
ヘッドレスで呼び出してレポートの「考察」セクションを書かせる。Anthropic API キーは不要で、
ログイン済みの Claude サブスクリプション枠を消費する。分析そのもの（`analyze`）には
不要なので、このステップが通らなくても他の機能は使える。

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
   `claude` が PATH に無くフルパスで呼ぶ場合は `config.yaml > discussion.command` に書く。

4. LLM を呼ばずにプロンプトの組み立てだけを確認する（ステップ 6 の生成物を使う）

   ```sh
   uv run seat-analyzer discuss --input-dir examples/input --org org-a --month 2026-06 --dry-run
   ```

   考察執筆の指示文が表示されれば、資料の収集からプロンプト生成までが動いている。

## ステップ 8: 実データ用のディレクトリを作る

分析対象の組織（Team プランの workspace）ごとに雛形を作る。組織名はレポートのタイトルと
出力パスにそのまま使われる。ユーザに組織名を確認してから実行する。

```sh
uv run seat-analyzer init-org <組織名>
```

`input/<組織名>/{spend,members,code-analytics}/` と `reports/<組織名>/`、および
ヘッダ行だけの `input/<組織名>/members-info.csv` が作られる。CSV はまだ無くてよい。

エクスポート手順と月次の運用は [usage.md](./usage.md) の「月次運用手順」を参照する。

## ステップ 9: 報告する

次をユーザに報告してセットアップを終える。

- 新しく導入したもの（uv・Python・Claude Code のバージョン）と、システムに加えた変更
  （インストール先、PATH の変更の有無）
- `uv run pytest` の結果
- サンプル E2E で生成されたファイル
- ヘッドレス Claude Code CLI の疎通結果（未疎通ならその理由と、分析自体は使えること）
- 作成した組織ディレクトリ
- 次にユーザが行うこと（claude.ai からの CSV エクスポート。[usage.md](./usage.md) の月次運用手順）

---

## トラブルシュート

- `uv: command not found`（インストール直後）
  PATH が未反映。新しいシェルで確認するか、インストーラが表示したディレクトリを PATH に足す。
- `uv sync` が Python のバージョンで失敗する
  `uv python install 3.12` を実行してから `uv sync` を再実行する。
- `seat-analyzer: command not found`
  `uv run seat-analyzer ...` の形で呼ぶ。`.venv` のコマンドを直接叩く運用にしない。
- pandas のビルドで失敗する
  ホイールが提供されていない Python バージョンを引いている可能性が高い。
  `uv python install 3.12` で 3.12 を入れて `uv sync` をやり直す。
- `claude` が見つからない、またはフラグが未対応
  Claude Code を導入・更新する。PATH に置けない場合は `config.yaml > discussion.command`
  にフルパスを書く。
- 分析実行時に「直下の spend/ は旧レイアウトのため分析できません」と言われる
  組織ディレクトリを挟まない配置（`input/spend/` 直下）のデータが残っている。
  次の手順で組織ディレクトリ配下へ移す。
  1. `uv run seat-analyzer init-org <組織名>` で雛形を作る。`--input-dir` で入力先を
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
- `check-text` や `discuss` が対象語を集められずにエラー終了する
  リポジトリのルート以外で実行している可能性が高い。ルートに移動して実行し直す。
- `tests/test_golden.py` だけが落ちる
  ワークツリーの改行が CRLF のまま残っている。`core.autocrlf` や `core.eol` を有効に
  していると OS によらず起きる。`git ls-files --eol | grep 'w/crlf' | grep -v examples/input`
  で確認し、行が出たらステップ 1 の手順でワークツリーを取り直す。

## セットアップ後

- CSV のエクスポート手順と月次運用: [usage.md](./usage.md) の「月次運用手順」
  スペンドレポートのエクスポートには Owner / Primary Owner 権限が必要で、
  90 日より前には遡れない
- 入力データの事前検査: `uv run seat-analyzer doctor`
- 料金前提の確認: `config.yaml` のシート料金とモデル単価は特定時点の値なので、
  使う前に最新の公式情報と照合する
- Claude Code のスラッシュコマンド `/seat-analysis` で、分析から考察執筆までを
  対話的に実行できる
