# リリース手順

新しいバージョンを配布するための手順。保守者向け。利用者はタグを指定して
`uv tool install` するだけなので、リリース＝タグと GitHub Releases を作ることを指す。

- 導入・アップデートの手順（利用者側）: [setup.md](./setup.md)

## 手順

1. [CHANGELOG.md](../CHANGELOG.md) に新しいバージョンのセクションを追記する。日付は
   リリース日。利用者に影響する変更だけを、追加 / 変更 / 修正のカテゴリで書く

2. `pyproject.toml` の `version` を更新する。タグ名（`vX.Y.Z`）と一致させること。
   `seat-analyzer --version` が返すのはこの値で、ずれると利用者が入れたものを
   特定できなくなる

3. 差分を公開テキストの検査に通す

   ```sh
   git diff | uv run seat-analyzer check-text --diff -
   ```

   終了コード 1 なら該当箇所を書き換えてから進む。追加したファイルは差分に現れないので
   `uv run seat-analyzer check-text <ファイル>` で個別に通す

4. コミットして PR を作り、CI（ubuntu / windows / macos のテストと配布物の検査）が
   通ることを確認してからマージする

5. マージ後の main でタグを打って push する

   ```sh
   git switch main && git pull --ff-only
   git tag v1.0.0
   git push origin v1.0.0
   ```

6. wheel を作り、GitHub Releases を作成して添付する。リリースノートは CHANGELOG の
   該当セクションを元に書き、コミットしない一時ファイルに置く

   ```sh
   uv build --out-dir dist
   uv run seat-analyzer check-text /tmp/release-notes.md
   gh release create v1.0.0 --title "v1.0.0" --notes-file /tmp/release-notes.md dist/*.whl
   ```

   Releases のタイトルと本文も公開面なので、投稿する前に検査に通す（タイトルは
   `--text` で確かめる）

7. 新しいタグから導入できることを確認する。ワークスペースとは別の場所で実行する

   ```sh
   uv tool install git+https://github.com/namikawa/claude-team-cost-optimizer@v1.0.0
   seat-analyzer --version
   ```
