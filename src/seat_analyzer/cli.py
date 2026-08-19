"""CLI エントリポイント: seat-analyzer {analyze,discuss,check-text,doctor,init,init-org}"""

from __future__ import annotations

import argparse
import importlib.metadata
import re
import sys
from pathlib import Path

from . import analyze, data_quality, discussion, ingest, public_text, report
from .config import WORKSPACE_CONFIG_NAME, load_config, validate_config_path
from .discussion import DiscussionError
from .domain import IssueCode, QualityIssue, Severity
from .leakcheck import LeakCheckError

# ワークスペース雛形用の設定テンプレート（init がコピーする。中身は全行コメント）
WORKSPACE_CONFIG_TEMPLATE = Path(__file__).parent / "templates" / "workspace-config.yaml"

# --config 省略時の説明。上書きファイルは任意なので「無くても動く」ことを明示する
_CONFIG_HELP = f"設定の上書きファイル (default: ./{WORKSPACE_CONFIG_NAME} があれば適用)"

# 入出力ディレクトリの説明。省略時の値は設定から決まる（_resolve_dir）
_INPUT_DIR_HELP = f"入力ディレクトリ (default: {WORKSPACE_CONFIG_NAME} の paths.input。未設定なら input)"
_OUTPUT_DIR_HELP = f"出力ディレクトリ (default: {WORKSPACE_CONFIG_NAME} の paths.output。未設定なら reports)"

# ワークスペースを git 管理下に置いても、実データと組織固有の設定が入らないようにする。
# 追記する行の目印にもなるので、文言は変えずに使う
_GITIGNORE_NOTE = "# 実データと組織固有の設定をコミットしない（seat-analyzer init）"


def _add_dir_options(parser: argparse.ArgumentParser, *, output: bool = True) -> None:
    """入出力ディレクトリのオプションを足す。

    既定値は argparse に持たせず None のままにする。省略したのか同じ値を明示したのかを
    区別できないと、設定ファイルの paths とフラグの優先順位を決められないため
    （解決は _resolve_dir が行う）。
    """
    parser.add_argument("--input-dir", default=None, help=_INPUT_DIR_HELP)
    if output:
        parser.add_argument("--output-dir", default=None, help=_OUTPUT_DIR_HELP)


def _resolve_dir(flag: str | None, cfg: dict, key: str) -> Path:
    """入出力先を決める: CLI フラグ > ワークスペースの config.yaml > 組み込み既定。

    設定ファイルに書かれた相対パスは、その設定ファイルの置き場所を基準に load_config が
    解決済みなので、ここでは受け取った値をそのまま Path にする（フラグと組み込み既定は
    従来どおりカレントディレクトリ基準）。
    """
    return Path(cfg["paths"][key] if flag is None else flag)


def _force_utf8_io() -> None:
    """標準入力・標準出力・標準エラーを UTF-8 にする。

    ロケール既定の文字コード（日本語 Windows では cp932）はレポートやプロンプトに
    含まれる em dash・⚠️・≤ ≥ を表現できない。Windows はコンソール直結のときだけ
    UTF-8 で書くため、そのままだと同じコマンドがリダイレクトやパイプ経由でだけ
    UnicodeEncodeError で落ちる。出力先によって成否が変わる状態をなくす。

    標準入力も対象にする。check-text は git diff や公開予定の文章をパイプで受け取り、
    Windows のパイプはロケール既定で読む。UTF-8 のバイト列を cp932 として読むと、
    日本語の多くは例外にならずに別の文字列へ化けるため（「本部」→「譛ｬ驛ｨ」）、
    禁止語と一致しないまま「検出なし」を返す＝混入チェックが fail-open する。

    errors は strict のままにする。UTF-8 は通常の文字をすべて表現できるので置換の
    出番は壊れたデータのときだけで、doctor --format json は ensure_ascii=False の
    生の Unicode を出す。改変した内容を正常終了で返すより、明示的に失敗させる。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # テストの差し替え等、TextIOWrapper でないストリーム
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            pass


def _print_permission_hint(exc: BaseException) -> None:
    """ファイルを掴まれていて書けないときの案内。該当しない例外なら何も出さない。

    Windows は他プロセスが開いているファイルを書き換え・置換できない。CSV を Excel で
    開いたまま再分析したときの WinError 32 が最も多い経路で、素の例外文からは
    「閉じれば直る」ことが読み取れない。組織ごとに例外を握る経路からも呼ぶ。
    """
    if isinstance(exc, PermissionError):
        print(
            "  ヒント: 出力先のファイルを Excel やエディタで開いていると"
            "書き換えられないことがあります。閉じてから再実行してください",
            file=sys.stderr,
        )


def _version() -> str:
    """インストールされているパッケージのバージョン。取得できなければ "unknown"。"""
    try:
        return importlib.metadata.version("seat-analyzer")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    _force_utf8_io()
    parser = argparse.ArgumentParser(prog="seat-analyzer", description="Claude Team シート最適化分析")
    parser.add_argument(
        "--version", action="version", version=f"seat-analyzer {_version()}",
        help="バージョンを表示して終了する",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="スペンドレポートを分析してレポートを生成")
    p.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    p.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織を分析",
    )
    p.add_argument("--config", default=None, help=_CONFIG_HELP)
    _add_dir_options(p)
    p.add_argument(
        "--preview", action="store_true",
        help="速報モード: 部分月データから一次判断のみ行う（変更推奨・ヒステリシスなし）",
    )
    p.add_argument(
        "--days", type=int,
        help="速報モードの観測日数。省略時はスペンドレポートのファイル名の期間から自動判別",
    )
    p.add_argument(
        "--with-discussion", action="store_true",
        help="レポート生成後に考察の執筆まで行う（discuss と同じ処理。ヘッドレス Claude CLI を使用）",
    )
    p.add_argument(
        "--force-discussion", action="store_true",
        help="--with-discussion で記入済みの考察も上書きする",
    )
    p.add_argument(
        "--allow-term", action="append", metavar="語",
        help="--with-discussion の混入チェックで許可する語（discuss --allow-term と同じ）",
    )
    p.add_argument(
        "--with-previous-discussion", action="store_true",
        help="--with-discussion で前月の考察も資料に渡す（discuss と同じ。既定では渡さない）",
    )
    p.set_defaults(func=_run_analyze)

    pdis = sub.add_parser(
        "discuss", help="生成済みレポートの「## 考察」をヘッドレス Claude CLI に執筆させる")
    pdis.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    pdis.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織",
    )
    pdis.add_argument("--config", default=None, help=_CONFIG_HELP)
    _add_dir_options(pdis)
    pdis.add_argument(
        "--preview", action="store_true", help="report.md ではなく preview.md の考察を対象にする")
    pdis.add_argument(
        "--force", action="store_true",
        help="記入済みの考察を上書きする（既定では手書きの考察を守るため書き換えない）",
    )
    pdis.add_argument(
        "--dry-run", action="store_true",
        help="組み立てたプロンプトを標準出力へ出して終了する（Claude は呼ばない）",
    )
    pdis.add_argument(
        "--allow-term", action="append", metavar="語",
        help="混入チェックで検出された語のうち、内容を確認して無害と判断したものを許可する"
             "（複数指定可）。チェック全体を無効化する手段は用意しない",
    )
    pdis.add_argument(
        "--with-previous-discussion", action="store_true",
        help="前月レポートの考察も資料としてモデルに渡す。既定では渡さない"
             "（人手の文書で内容を検証できず、過去の混入を引き写す経路になるため）",
    )
    pdis.set_defaults(func=_run_discuss)

    pdoc = sub.add_parser("doctor", help="入力データ（スペンド・メンバー一覧）の品質を検査")
    pdoc.add_argument("--month", help="対象月 (YYYY-MM)。省略時は対象組織の spend の最新月")
    pdoc.add_argument(
        "--org", action="append",
        help="対象組織（input/ 直下のディレクトリ名）。複数指定可。省略時は全組織を検査",
    )
    pdoc.add_argument("--config", default=None, help=_CONFIG_HELP)
    _add_dir_options(pdoc, output=False)   # 出力を書かないコマンド
    pdoc.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="出力形式。json は構造化issueの配列を stdout へ出す (default: text)",
    )
    pdoc.set_defaults(func=_run_doctor)

    pchk = sub.add_parser(
        "check-text",
        help="公開予定のテキスト（PR 本文・コメント・コミットメッセージ等）に業務情報が"
             "含まれないか検査する",
    )
    pchk.add_argument(
        "files", nargs="*", metavar="ファイル",
        help="検査するファイル。'-' または省略で標準入力を読む",
    )
    pchk.add_argument("--text", help="ファイルではなく文字列を直接検査する")
    pchk.add_argument(
        "--diff", action="store_true",
        help="入力を unified diff として扱い、追加される内容だけを検査する"
             "（削除行に現れる語で落ちないようにする）",
    )
    pchk.add_argument(
        "--allow-term", action="append", metavar="語",
        help="内容を確認して無害と判断した語を許可する（複数指定可）",
    )
    pchk.add_argument("--config", default=None, help=_CONFIG_HELP)
    _add_dir_options(pchk)
    pchk.add_argument(
        "--repo-root", metavar="パス",
        help="「すでに公開されている内容」を読むリポジトリのルート。"
             "省略時はカレントディレクトリ",
    )
    pchk.set_defaults(func=_run_check_text)

    pini = sub.add_parser("init", help="カレントディレクトリにワークスペースの雛形を作成")
    # 設定ファイルはこのコマンドが作るものなので --config は取らない（カレントの
    # config.yaml が既にあれば、そこに書かれた paths に合わせる）
    _add_dir_options(pini, output=False)
    pini.set_defaults(func=_run_init)

    pi = sub.add_parser("init-org", help="新しい組織の入力/出力ディレクトリの雛形を作成")
    pi.add_argument("orgs", nargs="+", metavar="組織名",
                    help="作成する組織名（input/ 直下のディレクトリ名になる）。複数指定可")
    pi.add_argument("--config", default=None, help=_CONFIG_HELP)
    _add_dir_options(pi)
    pi.set_defaults(func=_run_init_org)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, LeakCheckError, DiscussionError) as e:
        # 入力の読み取りに由来する失敗（欠損・権限・不正な値）と、混入チェックを
        # 保証できない状況、考察の生成に失敗した場合は traceback を出さずエラー終了する
        print(f"エラー: {e}", file=sys.stderr)
        _print_permission_hint(e)
        return 1


INPUT_SUBDIRS = ingest.INPUT_SUBDIRS


def _gitignore_pattern(rel: str, *, directory: bool) -> str:
    """ワークスペース相対パスを、その名前どおりに一致する .gitignore の行にする。

    `*` `?` `[` `]` `\\` はパターンとして解釈されるため、名前に含まれていれば
    エスケープする（そのまま書くと、除外したつもりのディレクトリが対象から外れる）。
    行頭の `/` はワークスペース直下に限る指定で、`#` や `!` が先頭に来ることも防ぐ。
    """
    escaped = "".join("\\" + c if c in "\\*?[]" else c for c in rel)
    return f"/{escaped}" + ("/" if directory else "")


def _workspace_relative(path: Path) -> str | None:
    """カレント（ワークスペース）から見た相対パス。配下でなければ None。

    .gitignore のパターンはそれが置かれたディレクトリからの相対でしか書けないため、
    ワークスペースの外を指す入力ディレクトリは行にできない。
    """
    try:
        rel = path.resolve().relative_to(Path.cwd().resolve())
    except ValueError:
        return None
    return rel.as_posix() if rel.parts else None


def _gitignore_entries(input_dir: Path, output_dir: Path) -> tuple[list[str], list[str]]:
    """ワークスペースで git に入れてはいけないものと、行にできなかった場合の通知。

    設定には組織固有の語が入り、入力には利用実績、出力には生成したレポートが入る。
    ワークスペースが git 管理下にあると `git add .` がまとめて拾う。
    """
    entries = [_gitignore_pattern(WORKSPACE_CONFIG_NAME, directory=False)]
    notes: list[str] = []
    for label, path in (("入力", input_dir), ("出力", output_dir)):
        rel = _workspace_relative(path)
        if rel is None:
            notes.append(
                f"{label}ディレクトリ（{path}）はワークスペース配下の相対パスにできないため"
                " .gitignore の対象外です。git 管理下に置くなら自分で除外してください"
            )
            continue
        pattern = _gitignore_pattern(rel, directory=True)
        if pattern not in entries:   # 入力と出力が同じ場所なら行は1つでよい
            entries.append(pattern)
    return entries, notes


def _gitignore_path() -> Path:
    """ワークスペースの .gitignore（書ける形であることを確かめて返す）。

    git は symlink の .gitignore を除外設定として読まない（リンク先が通常ファイルでも
    無視される）。追記できても保護にならないので、書く前に拒否する。
    """
    path = Path(".gitignore")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(
            f"{path} が通常のファイルではありません"
            "（symlink やディレクトリの .gitignore は git が除外設定として読まないため、"
            "書いても保護になりません。取り除いてから実行してください）"
        )
    return path


def _write_gitignore(path: Path, entries: list[str]) -> str:
    """.gitignore に必要な行を用意する。戻り値は表示用の状態。

    既にあるファイルには足りない行だけを追記する（既存の内容と改行の形は変えない）。
    """
    if not path.exists():
        path.write_text(
            _GITIGNORE_NOTE + "\n" + "\n".join(entries) + "\n",
            encoding="utf-8", newline="\n",
        )
        return f"{len(entries)} 行で作成"

    raw = path.read_bytes()
    # 既存行との照合は正規化せず完全一致で行う（ファイル自体は書き戻さない）。
    # git は先頭の空白をパターンの一部として扱うため、見た目が同じでも除外にならない
    # 行がある。それを「設定済み」と数えると、保護がないまま済んだことになる。
    # 逆に、効いている行を取りこぼして同じ内容を足すのは無害
    existing = raw.decode("utf-8", errors="replace").splitlines()
    missing = [e for e in entries if e not in existing]
    if not missing:
        return "必要な行がそろっているため変更なし"
    # 追記なので既存のバイト列には触らない。最後の行が閉じていなければ閉じ、
    # 既存の内容とは1行空ける
    lead = "" if not raw else ("\n" if raw.endswith(b"\n") else "\n\n")
    with path.open("a", encoding="utf-8", newline="\n") as f:
        f.write(lead + _GITIGNORE_NOTE + "\n" + "\n".join(missing) + "\n")
    return f"{len(missing)} 行を追記"


def _run_init(args: argparse.Namespace) -> int:
    """カレントディレクトリをワークスペースにする（入力ディレクトリと設定の雛形を作る）。

    プログラム本体（uv tool install で入るパッケージ）と利用者のデータを分けるための入口。
    設定はここに作る config.yaml へ差分だけを書き、書かなかった項目はパッケージ内の
    既定が使われる。

    再実行するワークスペースに記入済みの config.yaml があれば、そこに書かれた入出力先に
    合わせる（作るディレクトリと .gitignore の行が、以降の分析が読み書きする場所と
    食い違わないようにする）。
    """
    cfg = load_config()
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    # このコマンドは出力を書かないので --output-dir を持たない（.gitignore の行に使う
    # 出力先は設定からだけ決まる）
    output_dir = _resolve_dir(None, cfg, "output")
    config_path = Path(WORKSPACE_CONFIG_NAME)
    # 書き込み先の可否は、1つも書き始める前にまとめて確かめる
    validate_config_path(config_path)  # ディレクトリ等を「既存」として扱わない
    gitignore_path = _gitignore_path()

    input_dir.mkdir(parents=True, exist_ok=True)
    print(f"入力ディレクトリ: {input_dir}/")

    if config_path.is_file():
        # 記入済みの上書き設定を消さない（雛形で塗り替えると組織固有の設定が失われる）
        print(f"設定ファイル:     {config_path}（既存のため変更しません）")
    else:
        config_path.write_text(
            WORKSPACE_CONFIG_TEMPLATE.read_text(encoding="utf-8"),
            encoding="utf-8", newline="\n",
        )
        print(f"設定ファイル:     {config_path}（全行コメントの雛形。差分だけ書く）")

    entries, notes = _gitignore_entries(input_dir, output_dir)
    print(f"除外設定:         .gitignore（{_write_gitignore(gitignore_path, entries)}）")
    for note in notes:
        print(f"  ! {note}")

    print("\n次の手順:")
    print("  1. seat-analyzer init-org <組織名>   ← 組織ごとの入力ディレクトリを作る")
    print("  2. spend / members の CSV を配置（エクスポート手順は docs/usage.md 参照）")
    print("  3. seat-analyzer analyze --month YYYY-MM")
    return 0


def _run_init_org(args: argparse.Namespace) -> int:
    # 設定を読むのは入出力先を決めるため（分析と同じ場所に雛形を作る）
    cfg = load_config(args.config)
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    output_dir = _resolve_dir(args.output_dir, cfg, "output")
    # 1つでも不正・衝突があれば1つも作らない（途中まで作ると片付けが要る）
    ingest.validate_org_names(args.orgs)

    for org in args.orgs:
        existed = (input_dir / org).is_dir()
        for subdir in INPUT_SUBDIRS:
            (input_dir / org / subdir).mkdir(parents=True, exist_ok=True)
        (output_dir / org).mkdir(parents=True, exist_ok=True)
        # members-info.csv はヘッダ行のみの雛形を作る。既存（記入済みの可能性）は上書きしない。
        # 人が Excel で編集するファイルなので recommendations.csv と同じく BOM を付ける
        # （BOM 無し UTF-8 は Windows の Excel がロケール既定で開き日本語ヘッダが化ける）
        info_path = input_dir / org / "members-info.csv"
        info_created = not info_path.exists()
        if info_created:
            info_path.write_text(
                "email,部署,チーム,職種,追加クレジット上限,備考\n",
                encoding="utf-8-sig", newline="\n",
            )
        print(f"組織 '{org}' の雛形を{'確認しました（既存）' if existed else '作成しました'}:")
        print(f"  {input_dir / org / 'spend'}/           ← spend_YYYY-MM.csv（必須）")
        print(f"  {input_dir / org / 'members'}/         ← members_YYYY-MM.csv（必須。最低限 email,seat_type の2列）")
        print(f"  {input_dir / org / 'code-analytics'}/  ← cc_YYYY-MM.csv（任意）")
        print(f"  {info_path}  ← 部署・チーム・職種・備考の任意マッピング（{'ヘッダ雛形を作成' if info_created else '既存を保持'}）")
        print(f"  {output_dir / org}/")

    if (input_dir / "spend").is_dir():
        print(
            f"\n! 旧レイアウトのデータが {input_dir}/spend/ にあります。この形では分析"
            "できないため、spend/・members/・code-analytics/ と members-info*.csv を"
            f" {input_dir}/<組織名>/ 配下へ移動してください"
            "（空になったディレクトリも消す。手順は docs/setup.md 参照）"
        )
    print("\nCSV 配置後: seat-analyzer analyze --month YYYY-MM（エクスポート手順は docs/usage.md 参照）")
    return 0


def _resolve_targets(
    input_dir: Path, output_dir: Path, org_args: list[str] | None,
    orgs: list[str] | None = None,
) -> list[tuple[str, Path, Path]]:
    """分析対象の (組織名, 入力dir, 出力dir) を解決する。

    入力は input/<組織名>/spend/ の形だけを受け付ける。直下に spend/ がある形は
    組織名が決まらず出力先も組織ごとに分けられないため、移行手順を添えて停止する。

    orgs を渡すと組織の発見条件を差し替えられる（doctor は spend/ が欠けた組織も
    検査対象にするため、より広い条件で発見する）。
    """
    if orgs is None:
        orgs = ingest.discover_orgs(input_dir)
    if (input_dir / "spend").is_dir():
        # 組織ディレクトリと共存している場合も同じ案内にする（どちらも直下のデータを
        # 組織配下へ動かせば解決する）
        raise ValueError(
            f"{input_dir} 直下の spend/ は旧レイアウトのため分析できません。"
            f"seat-analyzer init-org <組織名> --input-dir {input_dir} で雛形を作成し、"
            "spend/・members/・code-analytics/ と members-info*.csv を"
            f" {input_dir}/<組織名>/ 配下へ移動してください"
            "（空になったディレクトリも消す。手順は docs/setup.md 参照）"
        )
    if not orgs:
        if org_args:
            raise ValueError(
                f"{input_dir} に組織ディレクトリがありません（--org を使うには "
                f"{input_dir}/<組織名>/spend/ の形でデータを配置してください）"
            )
        raise FileNotFoundError(
            f"{input_dir} に入力データがありません。{input_dir}/<組織名>/spend/ に"
            "スペンドレポートを配置してください（docs/usage.md の月次運用手順参照）"
        )

    # 衝突は選択の前に、発見済みの組織全体で見る。--org で片方だけ選んだ実行でも、
    # もう一方が同じ出力先へ書いた成果物を上書きしうるため
    ingest.check_org_name_collisions(orgs)

    if org_args:
        unknown = [o for o in org_args if o not in orgs]
        if unknown:
            raise ValueError(f"組織が見つかりません: {unknown}。存在する組織: {orgs}")
        selected = list(dict.fromkeys(org_args))
    else:
        selected = orgs
    # 手動作成された不正名ディレクトリ（summary・パス/Markdown を壊す文字等）を弾く
    for org in selected:
        ingest.validate_org_name(org)
    return [(org, input_dir / org, output_dir / org) for org in selected]


def _discover_inspect_orgs(input_dir: Path) -> list[str]:
    """検査対象の組織候補（昇順）。

    分析用の discover_orgs は spend/ を持つディレクトリだけを組織とするが、doctor は
    spend/ が欠けていること自体を検査するため、入力ディレクトリらしさ（既知の入力
    サブディレクトリか members-info.csv を持つ）で判定する。
    """
    if not input_dir.is_dir():
        return []
    orgs = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_dir():
            continue
        # 先頭ドット等の不正名も候補に含め、analyze と同じ validate_org_name で拒否する
        if any((path / sub).is_dir() for sub in INPUT_SUBDIRS) or (
            path / "members-info.csv"
        ).exists():
            orgs.append(path.name)
    return orgs


def _resolve_input_targets(
    input_dir: Path, org_args: list[str] | None
) -> list[tuple[str, Path]]:
    """検査対象の (組織名, 入力dir)。出力を書かないコマンド用に出力dirを落とす。"""
    if not input_dir.is_dir():
        # 組織名の検証より先に入力ディレクトリ自体の可否を判定する
        # （--org 指定時も構造化 issue として JSON へ出せるようにするため）
        # message の決定性のためパスは埋め込まない（実行環境依存値を持ち込まないため）
        raise FileNotFoundError(
            "--input-dir に指定されたディレクトリがありません"
            "（docs/usage.md の月次運用手順に従いデータを配置してください）"
        )
    orgs = _discover_inspect_orgs(input_dir)
    # 対象の解決は analyze と同じ規則に委ねる（doctor だけが受理するレイアウトを作らない）。
    # 残骸の members/ 等が直下にあっても組織の検査は止めない
    return [(org, org_input) for org, org_input, _ in
            _resolve_targets(input_dir, input_dir, org_args, orgs=orgs)]


def _latest_month(org_input: Path) -> str | None:
    """対象組織のスペンドの最新月。ファイル名を解決できない場合は None（doctor が検査する）。"""
    try:
        months = ingest.discover_months(org_input)
    except (OSError, ValueError):
        return None
    return months[-1] if months else None


def _notice(message: str, as_json: bool) -> None:
    """JSON 出力時は stdout を JSON だけに保つため、通知は stderr へ出す。"""
    print(message, file=sys.stderr if as_json else sys.stdout)


def _run_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    as_json = args.format == "json"
    all_issues: list[QualityIssue] = []
    month = args.month
    try:
        targets = _resolve_input_targets(input_dir, args.org)
    except OSError as exc:
        # 入力ディレクトリ自体が読めない・存在しない。使い方の誤り（組織名の誤り・
        # レイアウト混在）は ValueError のまま main で扱う
        targets = []
        all_issues.extend(data_quality.input_unavailable_issues(input_dir, exc))

    # 対象月: 未指定なら対象組織全体での最新月。1件も無ければ None のまま検査に渡す
    if month is None and targets:
        latest = [m for _, org_input in targets if (m := _latest_month(org_input))]
        month = max(latest) if latest else None
        if month is not None:
            _notice(f"対象月未指定のため最新月を使用: {month}", as_json)

    for org, org_input in targets:
        issues = data_quality.inspect_input(org_input, month, cfg, org=org)
        all_issues.extend(issues)
        if not as_json:
            _print_issues(org, month, issues)
    if not targets and not as_json:
        _print_issues(None, month, all_issues)

    n_error = sum(1 for i in all_issues if i.severity is Severity.ERROR)
    if as_json:
        # 組織ごとの検査結果を連結したままでは --org の指定順で並びが変わるため、
        # 正準順序で直列化する（同一のissue多重集合なら常に同一の文字列になる）
        print(data_quality.issues_to_canonical_json(all_issues))
    else:
        n_warning = len(all_issues) - n_error
        print(f"\n検査結果: エラー {n_error} 件 / 警告 {n_warning} 件")
    return 1 if n_error else 0


def _print_issues(org: str | None, month: str | None, issues: list[QualityIssue]) -> None:
    scope = " ".join(x for x in (org, month) if x)
    print(f"\n=== {scope + ' ' if scope else ''}入力検査 ===")
    if not issues:
        print("  問題は見つかりませんでした")
        return
    for issue in issues:
        print(f"  [{issue.severity.value}] {issue.code.value}: {issue.message}")


def _run_analyze(args: argparse.Namespace) -> int:
    if args.days is not None and not args.preview:
        raise ValueError("--days は --preview 専用のオプションです")

    cfg = load_config(args.config)
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    output_dir = _resolve_dir(args.output_dir, cfg, "output")

    targets = _resolve_targets(input_dir, output_dir, args.org)
    # 使い方の誤りは分析を走らせる前に落とす（3組織の分析を完走してから失敗させない）
    if args.with_discussion:
        _check_allow_scope(tuple(args.allow_term or ()), len(targets))

    # 対象月: 未指定なら対象組織全体での最新月。その月のデータが無い組織はスキップ
    month = _resolve_month(targets, args.month)

    results: list[analyze.AnalysisResult] = []
    skipped: list[str] = []
    written: list[tuple[str, Path]] = []
    n_previewed = 0
    for org, org_input, org_output in targets:
        if month not in ingest.discover_months(org_input):
            if len(targets) == 1:
                raise FileNotFoundError(
                    f"{org_input}/spend/ に {month} のスペンドレポートがありません"
                )
            skipped.append(org)
            continue
        if args.preview:
            days = args.days
            if days is None:
                period = ingest.spend_file_period(org_input, month)
                days = period.days if period else None
                if days is None:
                    raise ValueError(
                        f"--days <観測日数> を指定してください"
                        f"（{org}: スペンドレポートのファイル名に期間が無いため自動判別できません）"
                    )
                print(f"{org}: ファイル名の期間から観測日数 {days} 日を使用")
            pv = analyze.preview(org_input, month, cfg, days, org=org)
            paths = report.write_preview(pv, org_output)
            _print_preview(pv, paths)
            written.append((org, org_output))
            n_previewed += 1
            continue
        result = analyze.analyze(org_input, month, cfg, org=org)
        paths = report.write_all(result, org_output)
        results.append(result)
        written.append((org, org_output))
        _print_result(result, paths)

    if skipped:
        print(f"\n! {month} のスペンドレポートが無いためスキップした組織: {', '.join(skipped)}")
    if not results and not n_previewed:
        raise FileNotFoundError(f"{month} のデータを持つ組織がありません")

    if len(results) > 1:
        summary_path = report.write_org_summary(results, output_dir)
        _print_totals(results, summary_path)

    if args.with_discussion:
        return _run_discussions(
            written, month=month, input_dir=input_dir, output_dir=output_dir, cfg=cfg,
            preview=args.preview, force=args.force_discussion, dry_run=False,
            allow=tuple(args.allow_term or ()),
            include_previous=args.with_previous_discussion,
        )
    return 0


# ASCII 数字のみ・全体一致で検証する（`\d` は全角数字にも一致し、`$` は末尾改行を許す）
_MONTH_RE = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


def _resolve_month(targets: list[tuple[str, Path, Path]], month: str | None) -> str:
    """対象月。未指定なら対象組織全体での spend の最新月。

    対象月は出力パスの一部（reports/<組織名>/<月>/）になるため形式を厳密に検証する。
    検証しないと `--month ../<他組織>/<月>` で別組織のレポートを読み書きできてしまう。
    """
    if month is not None:
        if not _MONTH_RE.fullmatch(month):
            raise ValueError(f"対象月の形式が不正です: {month!r}（YYYY-MM 形式で指定してください）")
        return month
    latest = [m[-1] for _, d, _ in targets if (m := ingest.discover_months(d))]
    if not latest:
        raise FileNotFoundError(
            "スペンドレポートがありません。docs/usage.md の月次運用手順に従いエクスポートしてください。"
        )
    month = max(latest)
    print(f"対象月未指定のため最新月を使用: {month}")
    return month


def _check_text_sources(name: str) -> list[tuple[str, str]]:
    """検査対象 name の (ラベル, 本文)。複数の文字コードで読めるならすべて返す。

    標準入力はテキストラッパーを介さずバイト列で読む。ラッパー越しだと、読み取り前に
    UTF-8 へ再設定できていたかどうかで結果が変わり、安全機構の成否が環境に依存する
    （すでに読み進めたラッパーは文字コードを変更できず、ロケール既定のまま読む）。
    """
    if name == "-":
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is None:  # テキストストリームに差し替えられている場合はそのまま読む
            return [("(標準入力)", sys.stdin.read())]
        raw, label = buffer.read(), "(標準入力)"
    else:
        raw, label = Path(name).read_bytes(), name
    candidates = public_text.decode_candidates(raw)
    if len(candidates) == 1:
        return [(label, candidates[0][1])]
    # 解釈が割れたときは、どの読み方で当たったのかが分かるようラベルを分ける
    return [(f"{label}（{enc} として解釈）", text) for enc, text in candidates]


def _run_check_text(args: argparse.Namespace) -> int:
    """公開予定のテキストを検査する。業務情報を検出したら終了コード 1。"""
    cfg = load_config(args.config)
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    output_dir = _resolve_dir(args.output_dir, cfg, "output")
    # baseline（すでに公開されている内容）はリポジトリのルートから読む。
    # 省略時はカレントディレクトリ（リポジトリの中で実行する運用）。取り違えると
    # 別のリポジトリの内容を公開済みとして扱うため、省略時はルートの同一性を確かめる
    if args.repo_root:
        root = Path(args.repo_root)
    else:
        root = Path.cwd()
        public_text.validate_baseline_root(root)

    sources: list[tuple[str, str]] = []
    if args.text is not None:
        sources.append(("--text", args.text))
    names = list(args.files) or ([] if args.text is not None else ["-"])
    # 検査対象のファイル自身を baseline から除く（自分自身を根拠に素通りさせない）
    exclude = tuple(Path(n) for n in names if n != "-")
    for name in names:
        sources.extend(_check_text_sources(name))

    n_hits = 0
    allowable_seen = False
    for label, text in sources:
        scope = ""
        if args.diff:
            extract = public_text.diff_added_text(text)
            text = extract.text
            # 抽出量を必ず出す。追加行 0 なら、検査すべき内容が無かったことに気づける
            scope = f"追加行 {extract.n_added_lines} / 対象パス {extract.n_paths} / "
        result = public_text.check_public_text(
            text, input_dir=input_dir, output_dir=output_dir,
            cfg=cfg, root=root, allow=tuple(args.allow_term or ()), exclude=exclude,
        )
        if not result.hits:
            print(f"  {label}: 業務情報は検出されませんでした"
                  f"（{scope}{result.n_terms} 語と照合）")
            continue
        n_hits += len(result.hits)
        print(f"\n! {label}: 業務情報が含まれています"
              f"（{len(result.hits)} 件 / {scope}{result.n_terms} 語と照合）", file=sys.stderr)
        for hit in result.hits:
            note = "" if hit.allowable else "・--allow-term では許可できません"
            allowable_seen = allowable_seen or hit.allowable
            print(f"    {hit.term}（{hit.kind}{note}） … {hit.context} …", file=sys.stderr)

    if n_hits:
        print(
            "\n公開する前に該当箇所を、出典が特定できない一般論へ書き換えてください"
            "（例: 組織名を『ある組織』、部署名を『短い英字略称の部署』とする）。",
            file=sys.stderr,
        )
        if allowable_seen:
            print("誤検出と判断した語は --allow-term <語> で許可できます。", file=sys.stderr)
        return 1
    return 0


def _check_allow_scope(allow: tuple[str, ...], n_targets: int) -> None:
    """--allow-term は「その組織の生成物を人が確認した」ことに基づくため単一組織限定。

    恒久的に許可したい語は config.yaml > discussion.allow_terms を使う。
    """
    if allow and n_targets > 1:
        raise ValueError(
            "--allow-term は単一組織に対してのみ指定できます（--org で対象を絞るか、"
            "恒久的に許可する語は config.yaml > discussion.allow_terms に書いてください）"
        )


def _run_discuss(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    input_dir = _resolve_dir(args.input_dir, cfg, "input")
    output_dir = _resolve_dir(args.output_dir, cfg, "output")
    targets = _resolve_targets(input_dir, output_dir, args.org)
    month = _resolve_month(targets, args.month)
    return _run_discussions(
        [(org, org_output) for org, _, org_output in targets],
        month=month, input_dir=input_dir, output_dir=output_dir, cfg=cfg,
        preview=args.preview, force=args.force, dry_run=args.dry_run,
        allow=tuple(args.allow_term or ()),
        include_previous=args.with_previous_discussion,
    )


def _run_discussions(
    items: list[tuple[str, Path]], *, month: str, input_dir: Path, output_dir: Path,
    cfg: dict, preview: bool, force: bool, dry_run: bool, allow: tuple[str, ...] = (),
    include_previous: bool = False,
) -> int:
    """組織ごとに考察を生成する。1組織の失敗で他組織を止めない。"""
    _check_allow_scope(allow, len(items))

    # 対象月のレポートが無い組織はスキップする（analyze と同じ扱い）。組織ごとに
    # spend の月がずれるため、月未指定の実行で他組織が毎回ハードエラーになるのを避ける。
    # 単一組織のときは generate のエラーで理由を示す
    skipped: list[str] = []
    if len(items) > 1:
        targets = []
        for org, org_output in items:
            if discussion.document_path(org_output, month, preview).exists():
                targets.append((org, org_output))
            else:
                skipped.append(org)
        items = targets

    if not dry_run:
        print(f"\n=== 考察の執筆（{len(items)} 組織） ===")
    failed: list[str] = []
    blocked: list[str] = []
    for org, org_output in items:
        scope = f"{org} {month}"
        try:
            outcome = discussion.generate(
                org=org, month=month, input_dir=input_dir, output_dir=output_dir,
                org_output=org_output, cfg=cfg, preview=preview, force=force, dry_run=dry_run,
                allow=allow, include_previous=include_previous,
                notify=lambda m, scope=scope: print(f"  {scope}: {m}", file=sys.stderr),
            )
        except (DiscussionError, LeakCheckError, OSError, ValueError) as exc:
            print(f"  ! {scope}: 考察を生成できませんでした: {exc}", file=sys.stderr)
            _print_permission_hint(exc)
            failed.append(scope)
            continue
        if outcome.status == "blocked":
            blocked.append(scope)
        _print_discussion(outcome, scope)

    if skipped:
        # --dry-run は stdout をプロンプトだけに保つ契約なので通知は stderr へ回す
        print(f"\n! {month} のレポートが無いためスキップした組織: {', '.join(skipped)}"
              f"（先に analyze を実行してください）",
              file=sys.stderr if dry_run else sys.stdout)
    if blocked:
        print(
            f"\n! 他組織情報の混入が解消しなかったため書き込みを中止した組織: {', '.join(blocked)}",
            file=sys.stderr,
        )
    if failed:
        print(f"! 考察の生成に失敗した組織: {', '.join(failed)}", file=sys.stderr)
    return 1 if (blocked or failed) else 0


def _print_discussion(outcome: discussion.DiscussionOutcome, scope: str) -> None:
    if outcome.status == "dry-run":
        print(f"===== プロンプト: {scope} =====", file=sys.stderr)
        print(outcome.prompt)
        return
    if outcome.status == "kept":
        print(f"  {scope}: 記入済みの考察があるため変更しません（上書きは --force）")
    elif outcome.status == "written":
        print(f"  {scope}: 考察を書き込みました "
              f"（{outcome.chars} 文字 / 試行 {outcome.attempts} 回）→ {outcome.path}")
    elif outcome.status == "blocked":
        print(f"  {scope}: 混入チェックで他組織の語を検出したため書き込みを中止しました "
              f"（試行 {outcome.attempts} 回）", file=sys.stderr)
        for hit in outcome.leaks:
            # 一致箇所の文脈を出す。誤検出（他組織の短い部署名が無関係な複合語に
            # 一致した等）かどうかを人が判断できるようにするため
            note = "" if hit.allowable else "・--allow-term では許可できません"
            print(f"    検出語: {hit.term}（{hit.kind}{note}） … {hit.context} …",
                  file=sys.stderr)
        if any(h.allowable for h in outcome.leaks):
            print("    誤検出と判断した語は --allow-term <語> で許可できます", file=sys.stderr)


def _print_preview(pv, paths: dict[str, Path]) -> None:
    s = pv.summary
    print(f"\n=== {pv.org} {pv.month} 速報プレビュー（{pv.days_observed}日間の観測） ===")
    print(f"メンバー: {s['n_members']} 名 (Standard {s['n_standard']} / Premium {s['n_premium']}"
          f" / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']})")
    print(f"観測需要: ${s['total_api_observed_usd']:,.2f} → 月末ペース換算 ${s['total_api_projected_usd']:,.2f}")
    counts = s["label_counts"]
    detail = " / ".join(f"{lb} {n} 名" for lb, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    print(f"一次判断: {detail}")
    if s["n_billed"]:
        print(f"実課金発生: {s['n_billed']} 名")

    if pv.warnings:
        print("\n--- 警告 ---")
        for w in pv.warnings:
            print(f"  ! {w}")
    print(f"\n--- 出力 ---\n  preview:   {paths['markdown']}\n  dashboard: {paths['html']}")


def _prohibited_warnings(result: analyze.AnalysisResult) -> list[str]:
    """policy で禁止指定した product を観測したことの警告。

    宛先は分析の実行者なので、共有物であるレポートには載せず実行時の出力にだけ出す
    （レポートの内容は判定に使う値だけで決まることを保つ）。product 名の欠落
    （CAPACITY_SIGNAL_UNAVAILABLE）は特徴量が空欄になることで usage-summary.csv から
    見えるため、ここでは扱わない。
    """
    usage = result.product_usage
    if usage is None:
        return []
    return [
        issue.message for issue in usage.issues
        if issue.code == IssueCode.PROHIBITED_PRODUCT_OBSERVED
    ]


def _print_result(result: analyze.AnalysisResult, paths: dict[str, Path]) -> None:
    s = result.summary
    print(f"\n=== {result.org} {result.month} 分析結果 ===")
    print(f"メンバー: {s['n_members']} 名 (Standard {s['n_standard']} / Premium {s['n_premium']}"
          f" / 未割当 {s.get('n_unassigned', 0)} / 不明 {s['n_unknown']})")
    print(f"現在のシート費用: ${s['seat_cost_now_usd']:,.2f}/月, API換算利用額: ${s['total_api_cost_usd']:,.2f}/月")
    if s.get("org_service_cost_usd"):
        print(f"組織サービス利用（非帰属）: ${s['org_service_cost_usd']:,.2f}/月")
    print(f"変更推奨: {s['n_change_recommended']} 名 (削減見込み ${s['est_monthly_saving_usd']:,.2f}/月)")
    print(f"要観察: {s['n_watching']} 名, 上限到達疑い: {s['n_cap_suspected']} 名")
    print(f"使用データ: {', '.join(s['months_used'])}")

    prohibited = _prohibited_warnings(result)
    if result.warnings or prohibited:
        print("\n--- 警告 ---")
        for w in [*result.warnings, *prohibited]:
            print(f"  ! {w}")

    print("\n--- 出力 ---")
    for kind, path in paths.items():
        print(f"  {kind}: {path}")


def _print_totals(results: list[analyze.AnalysisResult], summary_path: Path) -> None:
    n_members = sum(r.summary["n_members"] for r in results)
    seat_cost = sum(r.summary["seat_cost_now_usd"] for r in results)
    n_change = sum(r.summary["n_change_recommended"] for r in results)
    saving = sum(r.summary["est_monthly_saving_usd"] for r in results)
    print(f"\n=== 全体 ({len(results)} 組織) ===")
    print(f"メンバー: {n_members} 名, シート費用: ${seat_cost:,.2f}/月")
    print(f"変更推奨: {n_change} 名 (削減見込み ${saving:,.2f}/月)")
    print(f"横断サマリ: {summary_path}")


if __name__ == "__main__":
    raise SystemExit(main())
