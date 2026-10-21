# ダウンロード取り違えの合成検証

作成日: 2026-10-21

BACKLOG 項目1の判断材料を集め、JSON保存修正の回帰を確かめる手順。実アカウントへのアクセス、ブラウザーの自動操作基盤やCIの追加は含めない。

## 検証する範囲

`scripts/download_probe.py` は新しい一時ディレクトリに `extension/`、`profile/`、`downloads/`、`results/` を分けて作る。既存のプロファイルと入力・レポートは使わない。ブラウザーは起動しない。

`run.js` から `trackDownload`、`waitUntil`、`exportWith`、`saveJson`、`WAIT`、`sleep`、`basename` を原文抽出する。抽出が一意でなければ止まる。`background.js` はバイト一致でコピーし、別の worker から読み込む。元ソースと各抽出部分の SHA-256 は `probe.json` と結果に残す。検証側は `runId` と `log` を提供し、tracker に渡す既知ID（`known_id`）と Promise の完了を観測する。Chrome のダウンロードAPIは置き換えず、ファイル名決定のlistenerも追加しない。

拡張の権限は `downloads`、`storage`、`tabs`、`scripting`、ホストは `127.0.0.1` のみ。Cookie権限、コンテンツスクリプト、実サイトへの接続はない。ダウンロードの主体は合成ページのリンクで、専用Chrome上の通常のダウンロードイベントを観測する。実際の claude.ai の URL・referrer・認証・画面操作の成立は証明しない。

## 準備とブラウザーなしの確認

リポジトリ直下で実行する。

```sh
uv run python scripts/download_probe.py prepare
```

出力の「検証root」を以下の `<root>` に、「拡張を読み込む絶対パス」をChromeの読み込みダイアログに使う。`prepare` を呼ぶたびに新しいディレクトリになる。以後は同じrootを使う。ポート使用中なら `prepare --port 8875 --other-port 8876` などで別の環境を作る。

```sh
uv run python scripts/download_probe.py self-check <root>
node scripts/download_probe.js --mock <root>
node --check <root>/extension/production.js
node --check <root>/extension/probe.js
node --check <root>/extension/background.js
node --check <root>/extension/worker.js
```

`self-check` はパッケージ同梱の既定設定だけを読み、合成したmembers・spend・code CSVを本番の `verify_export` と `place_export` に渡す。配置先も `<root>/results/cli-*/input/` の専用領域で、コピー後のバイト一致まで確かめる。別組織のUUIDを持つmembers/spendは拒否される。同期間のcode CSVは本文の合成マーカーが別組織相当でも通る。この結果は現行CLI検査の限界を示し、ブラウザーで取り違えた証拠にはしない。

修正後のNodeモードは「返却IDが届く前に別件が完了」「返却IDの検索応答待ちに別件が完了」の2順序を与え、どちらも別件ID 101を採用せず対象ID 202だけで完了することをassertする。これは任意順のモックであり、実機の順序や発生頻度とは分けて扱う。本番関数による通常保存、即時完了、検索結果のID照合、遅い応答、失敗時の後片付け、backgroundの保存先の回帰は `uv run pytest tests/test_browser_extension.py tests/test_claude_export.py` で確かめる。

## 通常Chromeで実行する

1. 別のターミナルでサーバを起動して残す。

   ```sh
   uv run python scripts/download_probe.py serve <root>
   ```

2. `prepare` が表示したChrome起動コマンドを実行する。この手順はmacOS向けで、`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome` を直接指定し、`--user-data-dir=<root>/profile` を渡す。個人のChromeウィンドウと専用のウィンドウを区別する。Linux/Windowsの実機とシェルの引用符・改行の違いは未確認。Playwright・CDP・headlessは使わない。

3. 専用ウィンドウの `chrome://extensions/` で「デベロッパーモード」を有効にし、「パッケージ化されていない拡張機能を読み込む」から、表示された `<root>/extension` の絶対パスを選ぶ。[Chrome公式の読み込み手順](https://developer.chrome.com/docs/extensions/get-started/tutorial/hello-world#load-unpacked)も参照できる。

4. 拡張一覧の `Synthetic download probe` にエラーがないことを確認する。ツールバーの拡張機能メニューから同拡張を押すと、永続した「合成ダウンロード検証」タブが開く。popupは使わない。

5. 「検証を開始」を1回押す。このタブを開いたまま待つ。合成ページ2タブができ、次の群を順次実行する。ダウンロード許可のUIが出た場合は専用ウィンドウで許可し、結果には操作した旨を記録する。許可待ちで時間切れになった場合は新しいrootでやり直す。

| 群 | 与える条件 | 観測する点 |
| --- | --- | --- |
| normal | CSV単独取得後にJSON単独保存 | CSVの対象URL、JSONのknown IDと返却ID、パスと内容 |
| overwrite | 同一JSONパスへ異なるマーカーを2回保存 | 2回とも同一パスで完了し、実ファイルは第2マーカー、連番の別名がないか |
| same | 同originの別タブから無関係CSVを先に要求 | 同じURL・referrerでも最初の別件をtrackerが受けるか、別件もprefixへ入るか |
| foreign | 別ポートのoriginから無関係CSVを先に要求 | URL・referrerのoriginが異なる別件でも追跡・振り分けされるか |
| late | 最初の応答を6.5秒遅延、1秒で再試行、その後の取得の応答を5秒遅延 | 再試行完了から3秒の後処理を超えた元CSVが次のprefixへ入るか、次取得が元CSVのIDで完了するか |
| json_1〜3 | JSON保存とページCSVを並行して3回 | JSONのknown/返却IDと保存完了、並行CSVがJSON用prefixへ入らずdownloads直下に届くか |

`same` は同一URLの第1応答に `UNRELATED`、第2応答に `TARGET` を入れ、ダウンロード名にも区別を付ける。`foreign` も同じ応答順を使う。`late` は最初が `LATE_FIRST`、再試行が `RETRY`、次取得が `TARGET` で、3件とも同名にして上書きを観測する。後続の正常な上書きで先行データが見えなくなる場合があるため、最終内容だけで判定せずIDとURLの時系列も見る。

各群の後にサーバの予定応答とダウンロードの終了を待つ。正常系と上書きのAPI観測は完了パスも検査し、不一致や失敗、終了待ちの時間切れは後続を停止する。元コードの待機上限は変更しない。遅着ケースの再試行間隔だけは `exportWith` の既存引数で1秒を指定し、本番の3秒後処理はそのまま使う。

6. 「記録完了」は受入成功を意味せず、実ファイル確認前は「受入未判定」と表示する。リポジトリ直下で専用downloadsの内容を採取し、機械判定する。

   ```sh
   uv run python scripts/download_probe.py inspect <root>
   ```

7. `<root>/results/acceptance.json` の `status` を確認する。`passed` はJSONの受入成功、`failed` は受入失敗で、画面にも反映する。`inspect` は正常系のCSVが `TARGET`、指定パスの `normal.json` が `NORMAL_JSON`、`overwrite.json` が `OVERWRITE_SECOND` で連番別名がないこと、JSON並行3回のパス・内容・IDとCSVがdownloads直下へ届くことを確認する。実ファイルとAPI観測が一致しなければ成功にしない。CSVの既知問題はこのJSON受入判定とは分ける。

## 結果の読み方

| ファイル | 証拠の種類 | 主な内容 |
| --- | --- | --- |
| `probe.json` | 準備情報 | 新規root、2origin、元ソース・抽出部分のハッシュ |
| `results/chrome_live.json` | `chrome_live` | userAgent、ケース、APIイベントの順番・時刻、routing変更、known/返却ID、最終ダウンロード情報 |
| `results/server.json` | `loopback_server` | 要求番号、originポート、referrer、応答遅延、合成マーカー、応答終了時刻 |
| `results/chrome_files.json` | `dedicated_download_files` | downloadsからの相対パス、最終バイト数・ハッシュ・内容 |
| `results/acceptance.json` | `dedicated_files_acceptance` | 正常パス・内容・上書き・別件CSV分離のJSON受入判定 |
| `results/node_mock.json` | `node_mock` | 任意に与えた2順序、対象/返却ID、Node版 |
| `results/cli_synthetic.json` | `cli_synthetic` | 本番CLI関数の合成CSV検証・配置、組織識別の限界 |

実機は `cases[].trackers[].returned.id/url/filename` と `known_id`、`cases[].downloads` を比較する。ページ取得の対象IDは対象URLまたは `target_` の名前を持つダウンロードから特定し、JSONの対象IDは `known_id` とする。修正前の記録は `adopted_id` を使う。`late` の第2 tracker が `mode=late` のURLを返した場合、予定した次取得の `mode=next` を受けていない。`created` のイベント順とroutingの切替、サーバの応答終了、実ファイルのマーカーを合わせて誤配置と上書きを判定する。

結果はケースごとに「再現」「未再現」「条件未成立」「未確認」を記す。要求順だけではChromeの `onCreated` 順を保証しない。`late` は、最初と再試行の両要求がサーバへ届き、最初の要求に `connection_closed` がなく、遅着CSVの `onCreated` が `next_export_start` より後になることを成立条件とする。最初の遅延要求が応答前から `onCreated` になって再試行が発生しなかった場合や、次取得へ切り替わる前に遅着CSVが到着した場合は「条件未成立」。条件が成立して取り違えを観測しなかった場合が「未再現」。リンクには空の `download` 属性を付け、同originでは遅延応答をページ遷移にしない。ファイル名はサーバの `Content-Disposition` に任せる。タイミングに依存するJSON競合は3回で起きなくても安全の証明にはならない。実機未実施のケースはモック結果で埋めない。

実行記録にはOS、Chrome版（`chrome://version/`）、Node版、元ソースのハッシュ、正常系の成否、群ごとの判定と該当ID・パス・マーカーを添える。Chrome版はuserAgentに細部が出ない場合があるため画面の値を残す。

## 修正前の結果（2026-10-21）

2026-10-21のブラウザーなし確認では、原文一致、生成JavaScriptの構文、2ポートの基本route、Nodeの2競合再現、CSV3種の検証・配置後バイト一致を確認した。別UUIDのmembers/spend拒否と、別組織相当の同期間codeが通る現行検査の限界も確認した。

同日、macOSの通常Chromeで合成データの全群を1回実行し、`fatal` なしで終了した。Chromeは起動前のアプリの `Info.plist` で `155.0.8059.85` を確認した（結果のuserAgentは `155.0.0.0`）。Nodeは `26.12.0`。以下は `chrome_live.json`、`server.json`、`chrome_files.json` を照合した実機結果で、実サービスの発生条件や頻度は未確認。

使用した元ソースのSHA-256は次のとおり。抽出部分とコピーの一致も確認済み。

```text
run.js        601ce6cdf2a358ed9969740d2bc7610eda8937858bb63e89323d4c90c544bb8c
background.js b7519c6e972373bca550f47520551aaab71549a09a0051824bdc603e058980ce
```

表のパスは `downloads/` からの相対位置で、実行固有の接頭辞を省略して `same/` などの群名で示す。`…csv` は `claude_code_2026_10_01_to_2026_10_31.csv` を省略したもの。

| 群 | 対象ID → 採用ID | 保存先・実ファイルの合成マーカー | 判定 |
| --- | --- | --- | --- |
| normal CSV | 8 → 8 | `normal/…csv` = `TARGET` | 正常 |
| normal JSON | adopt 9 → 返却9 | `normal/normal.json` = `NORMAL_JSON` | 正常 |
| same | 11 → 10 | `same/unrelated_…csv` = `UNRELATED`。対象11はdownloads直下の `target_…csv` = `TARGET` | 追跡・振り分けの取り違えを再現 |
| foreign | 13 → 12 | `foreign/unrelated_…csv` = `UNRELATED`。対象13はdownloads直下の `target_… (1).csv` = `TARGET` | 追跡・振り分けの取り違えを再現 |
| lateの次取得 | 16 → 15 | `late/first/…csv` = `RETRY`、`late/next/…csv` = `LATE_FIRST`、downloads直下の `…csv` = `TARGET` | 遅着CSVによる次取得の取り違えを再現 |
| JSON並行3回のJSON | adopt 17・19・21 → 各同ID | 各 `json_N/parallel.json` = `PARALLEL_JSON` | ID取り違えは未再現。危険な先行順は条件未成立 |
| JSON並行3回のページCSV | 別件18・20・22 | 全件がJSON用の `json_N/…csv` へ入り、内容は `TARGET` | 振り分けの取り違えを3/3回再現 |

`same` のID10と11は `url`・`finalUrl`・`referrer` がすべて同一だった。この条件ではこれらの一致だけで対象操作を区別できない。`foreign` は無関係な取得がポート8766、対象取得が8765で、originが異なっていても別件を採用した。

`late` は最初と再試行の両要求がサーバへ到着し、接続切断の記録はなかった。ブラウザーのイベント時刻は次の順で、3秒の後処理を超える遅着条件が成立した。

| UTC時刻 | イベント |
| --- | --- |
| 13:12:54.631 | 再試行ID14で最初のtrackerが完了 |
| 13:12:57.634 | 3秒の後処理後、次の取得を開始 |
| 13:13:00.100 | 最初の要求の遅着CSV、ID15の `onCreated` |
| 13:13:00.119 | 次のtrackerがID15で完了 |
| 13:13:02.646 | 本来の次取得、ID16の `onCreated` |

今回は同名ファイルの上書き自体は観測していない。JSON並行3回はすべてJSONの作成・adoptがページCSVより先だった。Nodeで再現した「別件がadopt前に完了」「古いsearch応答がadopt後に返る」の2件は、任意順を与えたモックの証拠として区別する。

実機で誤採用されたsame・foreign・lateの保存済み合成CSV（ID10・12・15）を、本番の `verify_export(path, "code", "2026-10", columns_aliases=既定設定のcolumns, org_id=合成UUID)` へ渡すと、3件すべて `ok=True` だった。したがって、この取り違えを現行CLIの配置前検証では拒否できない。これら実保存3件の配置は実行しておらず、配置の確認は前述のself-checkによる合成3種のバイト一致まで。

## JSON保存の修正と残る作業

対策を推奨する。合成環境で先着採用、全件の保存先振り分け、再試行後の固定3秒待ちによる取り違えを実機で確認し、現行CLIも誤ファイルを拒否できなかったため。

2026-10-21の初回修正では、`download`の返却IDを得てからそのIDだけを追跡し、保存先をfilenameで直接指定する実装へ変更した。IDは待機中に変更せず、listener登録直後にも検索する。JSON用の共有routingとadopt APIは廃止し、backgroundは自拡張の取得で `suggest()` を引数なしで呼ぶ実装とした。Blob URL・listener・timerの後片付けも成功・失敗の両経路で行う。

修正後のNodeモック2件と、関連317テスト（本番関数を実行する12ケースのNode回帰を含む）は成功した。通常のJSONパス・内容・overwrite指定、返却前の即時完了、別件を検索・採用しないこと、検索失敗・中断・timeoutの後片付けをモックで確認した。

同日の修正後実機確認は受入失敗だった。新しい専用環境で全群が `fatal` なしで終了し、使用ソースと検証用コピーのハッシュも一致したが、JSON4件は指定パスではなくBlobのUUID名でdownloads直下へ保存された。内容とknown/返却IDは正しく、並行CSV18・20・22がJSON用prefixに入らずdownloads直下へ届く改善は確認した。今回のソースハッシュとJSONの保存先を以下に残す。期待パスは実行固有の接頭辞を省略し、実保存パスは `downloads/` からの相対位置で示す。

```text
run.js        e2734a0294dba2ecd1f18eb15474d4d6f04c380a4b0553e6eb41ee292695b015
background.js 5fc818a122c17cdab8a9740980fe267d57a76d252a24dabe0208e29c19a17e01
```

| JSON群 | known ID → 返却ID | 期待パス | 実保存パス |
| --- | --- | --- | --- |
| normal | 9 → 9 | `normal/normal.json` | `a339db7d-dd38-4d8e-ad3a-d5b5ad87c29d.json` |
| json_1 | 17 → 17 | `json_1/parallel.json` | `388cda18-9542-43c6-92dd-08a7c33440c0.json` |
| json_2 | 19 → 19 | `json_2/parallel.json` | `b83b3126-e469-4971-85b4-fd1dc30c2133.json` |
| json_3 | 21 → 21 | `json_3/parallel.json` | `7d7ce29f-ab5c-41ff-9fb5-1aaa6a12a09d.json` |

JSONの内容はnormalが `NORMAL_JSON`、並行3件が `PARALLEL_JSON`。この時点では返却IDの追跡と別件CSVの分離だけで受入完了とせず、固定パス保存の修正と再確認を継続した。修正前と初回修正の失敗の生記録はそれぞれ保持する。

初回修正の全テストは3071件成功・1件skip、`uv run ruff check .` も成功した。

### Blob URL別の命名登録による再修正

初回修正の失敗を受け、返却ID追跡と直接filename指定を維持しつつ、download開始前に `jsonDownload:<Blob URL>` を独立したstorage.sessionキーへ登録する方式へ変更した。backgroundは自拡張IDとBlob URLが完全一致した場合だけ登録された相対パスとoverwriteを指定する。完了パスの区切りを正規化して期待パスとの終端一致を検査し、不一致は失敗にする。共有routingは戻さず、登録の個別削除・Blob URL解放をfinallyで行う。登録・削除失敗でもURLを解放し、後片付けの失敗で主エラーを隠さない。

2026-10-22時点で、再修正後の関連317テストと全3071テスト（1件skip）、`uv run ruff check .` は成功した。本番関数のNode回帰18ケースには、登録完了前にdownloadを開始しないこと、完全一致だけの命名、並行した独立登録と個別削除、保存パス不一致の拒否、登録・削除失敗の後片付けを加えた。`inspect` の合成確認では正常条件をpassed、保存先違い・連番別名・上書き第1マーカーの3条件をfailedと判定した。初回の受入失敗記録は保持する。

### 再修正後の実機受入成功（2026-10-22 JST）

開始は2026-10-22 00:08 JST（2026-10-21 15:08 UTC）。新しい専用環境で全8群がfatal・case errorなしで終了し、17ダウンロードがすべてcompleteとなった。API記録と実ファイル16件を照合し、`acceptance.json` は11項目すべてtrue、`status: passed`。JSONの正常パス・内容・同名上書き・並行CSVの分離を確認し、この合成実機環境でのJSON受入は完了した。

```text
run.js        96ff3437373dd2b252e86e298fd0f2aacdbb34c10f93586948f1111159f088a0
background.js 3d0d0c516747bd6495a767b1ac763cf6f56e313d1ce79d7248eb0d232aa92a6f
```

以下も実行固有の接頭辞を省略し、downloadsからの相対パスで示す。

| 確認 | ID | 実保存パス・内容 |
| --- | --- | --- |
| normal | CSV8、JSON known9 → 9 | `normal/…csv` = `TARGET`、`normal/normal.json` = `NORMAL_JSON` |
| overwrite | known10 → 10、known11 → 11 | 2回とも `overwrite/overwrite.json`。最終内容は `OVERWRITE_SECOND`、連番の別名なし |
| 並行JSON3回 | known19・21・23 → 各同ID | 各 `json_N/parallel.json` = `PARALLEL_JSON`、attemptは1・2・3 |
| 並行ページCSV3回 | 20・22・24 | downloads直下の `… (1).csv`・`… (2).csv`・`… (3).csv` = `TARGET`。JSON用ディレクトリに入らない |

CSVの既知問題は残り、sameは対象13に対して12、foreignは対象15に対して14の `UNRELATED` を採用した。lateの次取得も対象18に対して17の `LATE_FIRST` を採用した。JSON受入成功をCSV対策の完了とは扱わない。実サービスでの発生頻度や、JSONの危険な先行順の実機成立は引き続き未確認。

CSVの先着採用・再試行方式は今回の対象外で、上記same・foreign・lateの問題は残る。対象操作とダウンロードを一意に対応付ける方法を実サービスで調べてから方式を確定する。CSVの方式調査は別段階で未着手。実サービスでの再現や頻度、JSONの危険な順序の実機成立も引き続き未確認。

## 終了方法

専用Chromeの全ウィンドウを閉じ、サーバのターミナルでCtrl-Cを押す。結果を確認するまでrootは残す。不要になったら、表示された `seat-download-probe-` から始まる当該ディレクトリだけをOSのファイル管理画面から削除する。既存のChromeプロファイルは操作しない。
