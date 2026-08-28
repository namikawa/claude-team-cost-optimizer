# リリース手順

新しいバージョンを配布するための手順。保守者向け。利用者はタグを指定して
`uv tool install` するだけなので、リリース＝タグと GitHub Releases を作ることを指す。

- 導入・アップデートの手順（利用者側）: [setup.md](./setup.md)

## 手順

手順中の `vX.Y.Z` は、リリースするバージョン（例: `v1.0.0`）に読み替える。

1. [CHANGELOG.md](../CHANGELOG.md) に新しいバージョンのセクションを追記する。日付は
   リリース日。利用者に影響する変更だけを、追加 / 変更 / 修正のカテゴリで書く

   書くのは「何がどう変わったか」だけで、1 変更 1 行に収める。理由・仕様の詳細・
   使い方・注意点は CHANGELOG に持ち込まず docs 側に書く（長い CHANGELOG は読まれない）

   `## [未リリース]` セクションがある場合は、新しく書き足すのではなくその見出しを
   `## [X.Y.Z] - YYYY-MM-DD` へ書き換えて畳む（マージ済みの変更が二重に載るのを防ぐ）。
   畳む前に、前回のリリース以降にマージした PR がすべて載っているか確認すること。
   セクションの末尾のリンク定義（`[X.Y.Z]: .../releases/tag/vX.Y.Z`）も追記する。
   利用者の手順が変わる変更（出力ファイル名の変更など）は「変更」の先頭に置く

2. `pyproject.toml` の `version` を更新する。タグ名（`vX.Y.Z`）と一致させること。
   `seat-analyzer --version` が返すのはこの値で、ずれると利用者が入れたものを
   特定できなくなる。更新したら `uv lock` を実行して lockfile も揃える

3. 差分を公開テキストの検査に通す

   ```sh
   git diff HEAD | uv run seat-analyzer check-text --diff -
   ```

   `HEAD` を付けるのはステージ済みの変更も対象にするため。終了コード 1 なら該当箇所を
   書き換えてから進む。追加したファイルは差分に現れないので
   `uv run seat-analyzer check-text <ファイル>` で個別に通す。対象語は分析用データから
   収集するため、`input/`・`reports/` をリポジトリの外に置いている場合は
   `--input-dir` / `--output-dir` でその場所を指定する

4. コミットして PR を作り、CI（ubuntu / windows / macos のテストと配布物の検査）が
   通ることを確認してからマージする

5. マージ後の main でタグを打って push する

   ```sh
   git switch main && git pull --ff-only
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

6. wheel を作り、GitHub Releases を作成して添付する。リリースノートは CHANGELOG の
   該当セクションをそのまま使い（手順 1 と同じ簡潔さを保つ）、コミットしない一時ファイルに置く

   ```sh
   rm -rf dist && uv build --out-dir dist   # 過去の成果物を混載しないよう空にしてから作る
   uv run seat-analyzer check-text /tmp/release-notes.md
   gh release create vX.Y.Z --title "vX.Y.Z" --notes-file /tmp/release-notes.md dist/*.whl
   ```

   Releases のタイトルと本文も公開面なので、投稿する前に検査に通す（タイトルは
   `--text` で確かめる）

7. 利用者と同じ経路で導入できることを確認する。ワークスペースとは別の場所で実行する

   ```sh
   uv tool install "seat-analyzer @ https://github.com/namikawa/claude-team-cost-optimizer/releases/download/vX.Y.Z/seat_analyzer-X.Y.Z-py3-none-any.whl"
   seat-analyzer --version
   ```

   確かめるのは手順 6 で添付した wheel の URL からの導入。[setup.md](./setup.md) が案内して
   いるのがこの経路で、git の入っていないマシンでも導入できることがその前提になっている
