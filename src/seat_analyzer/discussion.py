"""考察セクションの自動執筆。

`.claude/commands/seat-analysis.md` の手順3〜5（結果の検証・考察の執筆・混入チェック）を
CLI から完結させるためのモジュール。ローカルの Claude Code CLI をヘッドレス（`-p`）で呼び、
考察本文のテキストだけを受け取って report.md / preview.md へ差し込む。

この経路には人のレビューが入らない（cron 実行もできる）ため、対話セッションで人が担って
いた「他組織の情報が混ざっていないか」の確認を機械で代替する必要がある。以下はそのための
設計上の約束:

- LLM にツールを与えない。資料はすべてプロンプトへ埋め込み、出力は本文テキストのみ。
  ファイルへの書き込みはこのモジュールが行うため、生成側がレポート以外を触ることがない
- `--safe-mode` と空の作業ディレクトリで呼ぶ。プロジェクトの CLAUDE.md や auto-memory には
  他組織の業務情報が含まれうるため、モデルのコンテキストへ入れない（混入経路を塞ぐ）
- 他組織情報の混入チェックは LLM の自己申告に頼らず、`leakcheck` の決定論的な照合で行う。
  検出したら書き直しを1度求め、それでも残るなら report.md を書き換えない
- 照合は「取りこぼし（他組織情報を通す）」より「誤検出（正当な記述を止める）」に倒す。
  取りこぼしは他組織の情報が担当者に共有される一方、誤検出は書き込まずに止まるだけで
  人が確認すれば済む。誤検出時に人が判断できるよう一致箇所の文脈を返し、確認済みの語は
  allow で明示的に許可できるようにしている
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from . import report
from .leakcheck import (
    MONTH_DIR_RE,
    LeakHit,
    Term,
    find_leaks,
    forbidden_terms,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"


class DiscussionError(RuntimeError):
    """考察の生成に失敗した。

    送出された場合、レポートは書き換えていない。
    transient=True は再実行で解消しうる失敗（通信の一時障害・429・5xx・タイムアウト）で、
    認証や設定の誤りのような恒久的な失敗と区別してリトライの可否を決める。
    """

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


# ヘッドレス実行で禁止するツール。資料はプロンプトに埋め込むためツールは一切不要。
#
# 主たる保証は `--tools ""`（組み込みツールを空集合にする許可リスト方式）で、以下は
# それが効かなくなった場合に残る追加防御にすぎない。**denylist は網羅していない**:
# 実測（`--tools default` で init イベントのツール一覧を確認）では、この列挙を通した
# 後も 20件以上のツールが残る。ツール名は環境とバージョンに依存するため網羅は諦め、
# 混入・外部送信に直接つながる種類だけを挙げている。
# - ファイル・Web の読み取り: 他組織情報をモデルへ持ち込む経路
# - 外向きの送信・共有: 対象組織のデータを外へ出す経路
# - ツールの発見・追い足し: 上記2種を後から呼べるようにする経路
# 存在しない名前を渡しても CLI は警告のみで正常終了する（実測）ため、環境差で名前が
# 消えても壊れない。
DISALLOWED_TOOLS = (
    # ファイル・Web の読み取り
    "Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
    "Glob", "Grep", "WebFetch", "WebSearch",
    # 外向きの送信・共有
    "SendMessage", "PushNotification", "RemoteTrigger", "ShareOnboardingGuide",
    # ツールの発見・追い足し、他エージェントの起動、新しい実行コンテキストの作成
    # （この argv の制約が及ばない実行を後から作れる経路も塞ぐ）
    "ToolSearch", "Skill", "Task", "Agent", "Workflow", "TodoWrite",
    "CronCreate", "TaskCreate",
)


@dataclass
class DiscussionOutcome:
    """1組織1月ぶんの考察生成の結果。"""

    org: str
    month: str
    path: Path
    # written / blocked / superseded / kept / dry-run。
    # superseded は「生成はしたが、対象のレポートが生成中に新しい名前で作り直されたため
    # 書き込まなかった」状態。kept（＝設計どおり触らない no-op）と違い、依頼された仕事が
    # 終わっていないので blocked と同じく再実行が要る
    status: str
    attempts: int = 0
    leaks: tuple[LeakHit, ...] = ()
    prompt: str = ""
    chars: int = 0


# ---------------------------------------------------------------- プロンプト組み立て


_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


def _render(template: str, values: dict[str, str]) -> str:
    """`{{KEY}}` を一度だけ置換する。

    逐次 replace すると、先に挿入した値の中の `{{KEY}}` が後続キーとして再置換され、
    資料や組織名の内容がプロンプト構造を壊しうる（組織名には波括弧が使える）。
    """
    return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _material_block(title: str, content: str) -> str:
    # コードフェンスを使わない（資料本文が Markdown を含むため）
    return f"===== {title} ここから =====\n{content.strip()}\n===== {title} ここまで =====\n"


def _correction_block(leaks: tuple[LeakHit, ...]) -> str:
    terms = "\n".join(f"- {h.term}（該当箇所: …{h.context}…）" for h in leaks)
    return f"""
# 前回の出力の差し戻し（最優先で従うこと）

前回の出力に、対象組織の資料に現れない固有名詞が含まれていました。これは他組織の情報の
混入であり、このままでは共有できません。以下の語を出力から完全に取り除き、必要なら出典を
特定できない一般論に言い換えたうえで、考察の全文を書き直してください。

検出された語:
{terms}
"""


def build_prompt(
    *, org: str, scope: str, materials: list[tuple[str, str]],
    preview: bool, leaks: tuple[LeakHit, ...] = (),
) -> str:
    shell = (PROMPTS_DIR / "discussion.md").read_text(encoding="utf-8")
    aspects_name = "aspects-preview.md" if preview else "aspects-full.md"
    aspects = (PROMPTS_DIR / aspects_name).read_text(encoding="utf-8")
    return _render(shell, {
        "ORG": org,
        "SCOPE": scope,
        "ASPECTS": aspects.strip(),
        "CORRECTION": _correction_block(leaks) if leaks else "",
        "MATERIALS": "\n".join(_material_block(t, c) for t, c in materials),
    })


def document_path(org_output: Path, month: str, preview: bool, org: str) -> Path:
    """考察を読み書きする対象ファイル。

    ファイル名に月と組織名を含める前に生成したレポートも対象にできるよう、新しい名前が
    無ければ種別だけの旧名へフォールバックする（`report.naming`）。考察はこの文書を
    その場で書き換えるため、読み先と書き先は同じでなければならない。
    """
    artifact = report.PREVIEW if preview else report.REPORT
    return artifact.existing_path(org_output, month, org)


def _prev_output_month(org_output: Path, month: str) -> str | None:
    """対象月より前で最も新しい出力月。"""
    if not org_output.is_dir():
        return None
    months = sorted(
        p.name for p in org_output.iterdir()
        if p.is_dir() and MONTH_DIR_RE.match(p.name) and p.name < month
    )
    return months[-1] if months else None


# details.md が資料として使える形かを見分ける番兵。全ユーザ表は details.md に無条件で
# 載り（report/details.py）、再構成前の report.md にも必ずあった。条件つきの section を
# 番兵にすると、そのデータを持たない組織で「壊れている」と誤判定する。
# 見出し行そのもの（行頭〜行末）で照合する。部分文字列だと、組織名は # を許すため
# タイトル行（「… — <組織名> — <月>」）や表のセルに同じ文字列が入ると誤一致する。
_ALL_USERS_HEADING = "## 全ユーザ"
_ALL_USERS_HEADING_RE = re.compile(rf"^{re.escape(_ALL_USERS_HEADING)}[ \t]*$", re.M)


def _details_material(path: Path, month: str, report_body: str) -> str | None:
    """資料に足す details.md の本文（足さない場合は None）。

    details.md が無い状態は2つに分かれ、続行してよいのは片方だけ。

    - 再構成前に生成した月: report.md 自体がユーザ表を持つ。資料は欠けていないので続行する
    - 再構成後の月で details.md だけが無い・空・途中まで: 表を持たない資料でモデルに
      考察を書かせることになる。エラーも警告も出ずに考察の質だけが落ちるため中止する
      （書き込みの中断・手動削除でこの状態は作れる）

    再実行で解消しうるが、ヘッドレス実行のリトライで直る類ではないので transient にしない。
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if _ALL_USERS_HEADING_RE.search(text):
        return text
    if _ALL_USERS_HEADING_RE.search(report_body):
        return None
    raise DiscussionError(
        f"{path} が無い、または内容が不完全です（「{_ALL_USERS_HEADING}」が見つかりません）。"
        "ユーザ単位の表を欠いた資料で考察を書かせないため中止します。"
        f"`seat-analyzer analyze --month {month}` でレポートを作り直してください"
    )


def collect_materials(
    *, org: str, org_output: Path, month: str, preview: bool, doc_path: Path,
    terms: tuple[Term, ...] = (), include_previous: bool = False, notify=None,
) -> tuple[list[tuple[str, str]], str]:
    """(プロンプトへ渡す資料, 混入チェックの照合元テキスト) を返す。

    doc_path は資料1になるレポート本文で、呼び出し側が解決したものを受け取る（ここで
    document_path() を呼び直さない）。二重に解決すると、旧名しか無い状態で始めた実行の
    途中で並行する analyze が新名を作った場合に、資料は新名から読み・考察は旧名へ書く、
    という食い違いが起きる。書き込みは成功するので、生成した考察が二度と読まれない
    ファイルに残ることに気づけない。

    正式分析の資料は report.md 本文 → details.md → recommendations.csv の順。
    report.md は考察中心の短い文書なので、ユーザ単位の表・月中の推移・分布は
    details.md が資料として補う。

    照合元には機械生成された当月の資料だけを入れる。前月の考察は人・LLM の散文で、
    そこに混入があった場合に今月の混入を見逃す照合元になってしまうため含めない。

    前月の考察は既定では資料に含めない（include_previous=False）。これは検証できない
    人手の文書であり、含めるとモデルへ渡す資料が「機械生成された対象組織のデータのみ」
    という前提が崩れる。過去のレポートに混入があった場合、それを引き写す経路になる
    （混入チェックには 2文字の姓や金額のような死角があり、そこは塞げない）。
    include_previous=True のときも渡す前に混入チェックを通し、落ちたら除外する。
    """
    notify = notify or (lambda _message: None)
    if not doc_path.exists():
        raise DiscussionError(
            f"{doc_path} がありません。先に "
            f"`seat-analyzer analyze{' --preview' if preview else ''}` を実行してください"
        )
    body = report.document_body(doc_path.read_text(encoding="utf-8"))
    materials = [(f"資料1: 分析レポート本文（{month}）", body)]
    source = [body]

    def add(title: str, text: str) -> None:
        """資料を末尾に足す（番号は並び順から付ける）。照合元にも同じ本文を入れる。"""
        materials.append((f"資料{len(materials) + 1}: {title}", text))
        source.append(text)

    if not preview:
        # details.md は report.md から移した表の受け皿。再構成前に生成した月には
        # 無いので、その場合だけ省略して従来どおり動かす（_details_material 参照）
        details_path = report.DETAILS.existing_path(org_output, month, org)
        details_text = _details_material(details_path, month, body)
        if details_text is not None:
            add(f"分析詳細資料 {details_path.name}（{month}）", details_text)
        csv_path = report.RECOMMENDATIONS.existing_path(org_output, month, org)
        if csv_path.exists():
            add(f"ユーザ別推奨一覧 {csv_path.name}（{month}）",
                csv_path.read_text(encoding="utf-8"))

    source_text = "\n".join(source)
    prev_month = _prev_output_month(org_output, month) if include_previous else None
    if prev_month is not None:
        prev_report = report.REPORT.existing_path(org_output, prev_month, org)
        if prev_report.exists():
            prev = report.discussion_body(prev_report.read_text(encoding="utf-8"))
            if prev:
                # allow は「今回の生成物を人が確認した」ことに基づく許可なので、
                # 過去の考察の検査には適用しない
                hits = find_leaks(prev, terms, source=source_text)
                if hits:
                    notify(
                        f"前月（{prev_month}）の考察に他組織の語が含まれるため資料から除外します: "
                        f"{', '.join(h.term for h in hits)}"
                    )
                else:
                    materials.append((
                        f"資料{len(materials) + 1}: 前回の正式レポートの考察（{prev_month}）",
                        prev,
                    ))
    return materials, source_text


# ---------------------------------------------------------------- ヘッドレス実行


_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n(.*)\n```$", re.DOTALL)
# API エラーは出力の先頭に限らない（ストリーミング途中で失敗すると本文の後に付く）。
# 行頭一致を全行に対して探す
_API_ERROR_RE = re.compile(
    r"^[ \t]*API Error:?[ \t]*(?P<status>\d{3})?", re.IGNORECASE | re.MULTILINE)
_DISCUSSION_HEADING_RE = re.compile(r"^##[ \t]*考察[ \t]*$\n?", re.MULTILINE)
# 考察として最低限備えるべき形。プロンプトで `###` の小見出しを指示しているため、
# 1つも無い出力は指示に従っていない（前置きだけ・拒否文・途中で切れた出力等）
_HEADING_RE = re.compile(r"^###[ \t]*\S", re.MULTILINE)
# ツール呼び出しのマークアップ。ツールは渡していないが、モデルがテキストとして
# 捏造することがある。そのまま書き込むとレポートにマークアップが残る
_TOOL_MARKUP_RE = re.compile(
    r"</?\s*(?:function_calls|function_results|invoke|antml:[a-z_]+)\b", re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """全体がコードフェンスで囲まれていたら剥がす（指示に反した出力の救済）。"""
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text.strip()


def _strip_discussion_heading(text: str) -> str:
    """「## 考察」の見出し行を落とす。

    プロンプトでは出力しないよう指示しているが、付いてきた場合に差し込むとレポートに
    同名の H2 が2つ並ぶ。前置き（「以下が考察です。」等）の後に見出しが来ることも
    あるため、先頭限定ではなく行全体が見出しの箇所をすべて対象にする。
    """
    return _DISCUSSION_HEADING_RE.sub("", text.strip()).strip()


def _api_error_is_transient(status: str | None) -> bool:
    """API エラーが再実行で解消しうるものか（429 と 5xx のみ）。

    認証・引数・コンテキスト長などの 4xx を再試行しても同じ結果になり、待ち時間だけ増える。
    ステータスが読めない場合は一時扱いにする（通信断のメッセージ等）。
    """
    if status is None:
        return True
    code = int(status)
    return code == 429 or 500 <= code < 600


def _decode_output(raw: bytes | None) -> str:
    """claude の出力を UTF-8 として読み、改行を LF に揃える。

    text=True をやめた代わりの universal newlines。Windows の claude.cmd は CRLF を
    出しうるが、レポートの改行は LF 固定で、`write_text(newline="\\n")` は文字列の中の
    CR をそのまま書く。ここで落とさないと考察セクションだけが CRLF になる。
    """
    if not raw:
        return ""
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def run_claude(prompt: str, s: dict) -> str:
    """ヘッドレス Claude CLI を呼び、考察本文のテキストを返す。"""
    command = str(s["command"])
    # which() が返した実体を起動する。Windows の CreateProcess は拡張子を補うとき
    # .exe しか試さないため、npm 版の claude.cmd は名前だけでは起動できない
    # （which() は PATHEXT を見るので見つかる＝ガードだけ通って実行で落ちる）。
    resolved = shutil.which(command)
    if resolved is None and not Path(command).is_file():
        raise DiscussionError(
            f"'{command}' が見つかりません。Claude Code CLI を PATH に置くか、"
            "config.yaml > discussion.command にフルパスを指定してください"
        )
    cmd = [
        resolved or command, "-p",
        # プロジェクトの CLAUDE.md・auto-memory・hooks・MCP を読み込ませない
        "--safe-mode",
        "--output-format", "text",
        "--model", str(s["model"]),
        "--effort", str(s["effort"]),
        # 組み込みツールを空集合にする（許可リスト方式が主たる保証）
        "--tools", "",
        "--disallowedTools", *DISALLOWED_TOOLS,
        # 設定ファイル由来の MCP 設定を無視する。空の作業ディレクトリに .mcp.json を
        # 置かれた場合の保険。claude.ai 管理の MCP サーバには効かず、それを落として
        # いるのは --safe-mode（実測: --safe-mode を外すと claude mcp list に3件残る）。
        # つまり MCP は組み込みツールのような二重防御になっていない
        "--strict-mcp-config",
        # 対象組織のレポート全文が ~/.claude のトランスクリプトに残らないようにする
        "--no-session-persistence",
    ]
    # プロンプトも出力もバイト列で受け渡し、UTF-8 の変換は自分で行う。text=True に任せると
    # ロケールの文字コード（日本語 Windows では cp932）になり、レポート全文を含む
    # プロンプトの em dash・⚠️ を送れない。encoding="utf-8" を渡すだけでは足りず、
    # Windows ではデコードがリーダースレッドで走るため UnicodeDecodeError がスレッドの
    # 中で死んで stdout が None になる（「出力が空」と区別できず transient 扱いで
    # 生成をやり直してしまう）。自分でデコードすれば例外の出所が全 OS で1箇所になる。
    try:
        payload = prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DiscussionError(f"プロンプトを UTF-8 で送れませんでした: {exc}") from exc

    # 空の作業ディレクトリで実行し、リポジトリのファイルを起点にさせない
    with tempfile.TemporaryDirectory(prefix="seat-analyzer-discuss-") as workdir:
        try:
            proc = subprocess.run(
                cmd, input=payload, capture_output=True,
                timeout=float(s["timeout_seconds"]), cwd=workdir, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DiscussionError(
                f"{command} が {s['timeout_seconds']} 秒以内に応答しませんでした", transient=True
            ) from exc
        except OSError as exc:
            raise DiscussionError(f"{command} を実行できませんでした: {exc}") from exc

    # errors は既定の strict。置換して読み進めると、壊れた出力に含まれる他組織名が
    # U+FFFD へ化けて find_leaks の照合をすり抜ける。原因は CLI が非 UTF-8 を出す設定で
    # 同じ入力の再実行では直らないため transient にしない
    try:
        out = _decode_output(proc.stdout).strip()
        err = _decode_output(proc.stderr).strip()
    except UnicodeDecodeError as exc:
        raise DiscussionError(
            f"{command} の出力を UTF-8 として読めませんでした: {exc}"
        ) from exc

    if proc.returncode != 0:
        # claude -p は API エラーでも 0 を返すため、非ゼロは使い方・設定の誤りとみなす
        detail = (err or out).splitlines()
        raise DiscussionError(
            f"{command} が異常終了しました (exit {proc.returncode}): "
            f"{detail[0][:300] if detail else '出力なし'}"
        )
    # API エラー・空出力・形の崩れた出力をそのまま考察として書き込まないための門。
    # いずれも再実行で解消しうるため transient として扱う（4xx の API エラーを除く）
    if not out:
        raise DiscussionError(f"{command} の出力が空でした", transient=True)
    if m := _API_ERROR_RE.search(out):
        line = out[m.start():].splitlines()[0].strip()
        raise DiscussionError(
            f"{command} が API エラーを返しました: {line[:300]}",
            transient=_api_error_is_transient(m.group("status")),
        )
    body = _strip_discussion_heading(_strip_fence(out))
    if len(body) < int(s["min_output_chars"]):
        raise DiscussionError(
            f"生成された考察が短すぎます（{len(body)} 文字 < "
            f"{s['min_output_chars']} 文字）: {body[:200]}", transient=True,
        )
    # 長さだけでは前置き・拒否文・捏造されたツール呼び出しを弾けないため、
    # 「考察の形をしているか」を肯定的に検査する
    if mk := _TOOL_MARKUP_RE.search(body):
        raise DiscussionError(
            f"出力にツール呼び出しのマークアップが含まれます: {mk.group(0)}", transient=True)
    if not _HEADING_RE.search(body):
        raise DiscussionError(
            f"出力に `###` の小見出しがありません（考察の形になっていません）: {body[:200]}",
            transient=True,
        )
    return body


def _call_with_retry(runner, prompt: str, s: dict, notify) -> str:
    """一時的な失敗（429・5xx・タイムアウト等）に限ってリトライする。"""
    retries = max(0, int(s["retries"]))
    wait = float(s["retry_wait_seconds"])
    wait = wait if math.isfinite(wait) and wait > 0 else 0.0
    for i in range(retries + 1):
        try:
            return runner(prompt, s)
        except DiscussionError as exc:
            if not exc.transient or i == retries:
                raise
            notify(f"一時的な失敗のため再試行します（{i + 1}/{retries}）: {exc}")
            if wait:
                time.sleep(wait)
    raise AssertionError("到達しない")  # pragma: no cover


# ---------------------------------------------------------------- オーケストレーション


def generate(
    *, org: str, month: str, input_dir: Path, output_dir: Path, org_output: Path,
    cfg: dict, preview: bool = False, force: bool = False, dry_run: bool = False,
    allow: tuple[str, ...] = (), include_previous: bool = False, runner=None, notify=None,
) -> DiscussionOutcome:
    """1組織1月ぶんの考察を生成して書き込む。

    既に記入済みの考察は force が無ければ上書きしない（手書きの考察を守るため）。
    混入が検出された場合は書き直しを求め、max_attempts 回で解消しなければ書き込まない。
    生成中に対象のレポートが新しい名前で作り直された場合も書き込まない（superseded）。

    送出する例外は2系統ある: 生成そのものの失敗は DiscussionError、禁止語の収集・照合が
    続行できない場合は leakcheck.LeakCheckError（共通の基底を持たないため両方を捕まえる）。
    """
    # 既定の runner は呼び出し時に解決する（デフォルト引数で束縛すると差し替えが効かない）
    runner = runner or run_claude
    notify = notify or (lambda _message: None)
    s = cfg["discussion"]
    # config の allow_terms は「恒久的に無害と確認済みの語」。--allow-term は単一組織
    # 実行に限られるため、全組織実行でも効く許可の置き場としてこちらを使う
    allow = tuple(allow) + tuple(s.get("allow_terms") or ())
    doc_path = document_path(org_output, month, preview, org)
    scope = f"{org} {month}"

    terms = forbidden_terms(
        input_dir=input_dir, output_dir=output_dir, target_org=org, cfg=cfg)
    materials, source = collect_materials(
        org=org, org_output=org_output, month=month, preview=preview,
        doc_path=doc_path, terms=terms,
        include_previous=include_previous, notify=notify)
    prompt = build_prompt(org=org, scope=scope, materials=materials, preview=preview)

    # --dry-run はプロンプトの確認用なので、上書き可否に関係なく必ずプロンプトを返す
    if dry_run:
        return DiscussionOutcome(org, month, doc_path, "dry-run", prompt=prompt)
    if not force and (existing := _existing_discussion(doc_path)):
        return DiscussionOutcome(org, month, doc_path, "kept", chars=len(existing))

    max_attempts = max(1, int(s["max_attempts"]))
    leaks: tuple[LeakHit, ...] = ()
    for attempt in range(1, max_attempts + 1):
        body = _call_with_retry(
            runner,
            build_prompt(org=org, scope=scope, materials=materials,
                         preview=preview, leaks=leaks) if leaks else prompt,
            s, notify,
        )
        leaks = find_leaks(body, terms, source=source, allow=allow)
        if leaks:
            # 検出語は必ず運用者に残す。書き直しで解消した場合に何も出さないと、
            # 誤検出なら「正当な記述が静かに削られた」ことに気づけず、真の混入なら
            # 「モデルが他組織名を出力しようとした」という兆候の記録が消える。
            # 最終試行分は blocked として呼び出し側が表示するため、ここでは出さない
            if attempt < max_attempts:
                notify(f"試行 {attempt}: 混入を検出したため書き直しを求めます")
                for hit in leaks:
                    notify(f"  検出語: {hit.term}（{hit.kind}） … {hit.context} …")
            continue
        # 生成には最大で timeout_seconds かかる。その間に対象のファイル自体が入れ替わって
        # いないかを、内容の照合より先に見る（旧名しか無い状態で始めた実行の途中で
        # 並行する analyze が新名を作ると、以後は新名が読まれるため、旧名へ書いた考察は
        # 二度と読まれない）。書き込みは成功してしまうので、書く前に止める
        if document_path(org_output, month, preview, org) != doc_path:
            return DiscussionOutcome(
                org, month, doc_path, "superseded", attempts=attempt, chars=len(body))
        # その間に人が考察を書いた場合や、並行する analyze が本文を更新した場合に、
        # それを巻き戻さないよう書き込み側で判定と置換を畳み、置換直前にも内容が
        # 変わっていないか確認する
        if not report.write_discussion(doc_path, body, only_if_unwritten=not force):
            notify("生成中にレポートが変更されたため書き込みません（上書きは --force）")
            existing = _existing_discussion(doc_path) or ""
            return DiscussionOutcome(org, month, doc_path, "kept", chars=len(existing))
        return DiscussionOutcome(
            org, month, doc_path, "written", attempts=attempt, chars=len(body))
    return DiscussionOutcome(
        org, month, doc_path, "blocked", attempts=max_attempts, leaks=leaks)


def _existing_discussion(doc_path: Path) -> str | None:
    return report.discussion_body(doc_path.read_text(encoding="utf-8"))
