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
from .config import discussion_settings
from .leakcheck import (
    _MONTH_DIR_RE,
    DiscussionError,
    LeakHit,
    Term,
    find_leaks,
    forbidden_terms,
)

PROMPTS_DIR = Path(__file__).parent / "prompts"

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

    org: str | None
    month: str
    path: Path
    status: str  # written / blocked / kept / dry-run
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
    *, org: str | None, scope: str, materials: list[tuple[str, str]],
    preview: bool, leaks: tuple[LeakHit, ...] = (),
) -> str:
    shell = (PROMPTS_DIR / "discussion.md").read_text(encoding="utf-8")
    aspects_name = "aspects-preview.md" if preview else "aspects-full.md"
    aspects = (PROMPTS_DIR / aspects_name).read_text(encoding="utf-8")
    return _render(shell, {
        "ORG": org or "（単一組織）",
        "SCOPE": scope,
        "ASPECTS": aspects.strip(),
        "CORRECTION": _correction_block(leaks) if leaks else "",
        "MATERIALS": "\n".join(_material_block(t, c) for t, c in materials),
    })


def document_path(org_output: Path, month: str, preview: bool) -> Path:
    """考察を書き込む対象ファイル。"""
    return org_output / month / ("preview.md" if preview else "report.md")


def _prev_month_dir(org_output: Path, month: str) -> Path | None:
    """対象月より前で最も新しい出力月のディレクトリ。"""
    if not org_output.is_dir():
        return None
    months = sorted(
        p.name for p in org_output.iterdir()
        if p.is_dir() and _MONTH_DIR_RE.match(p.name) and p.name < month
    )
    return org_output / months[-1] if months else None


def collect_materials(
    *, org_output: Path, month: str, preview: bool,
    terms: tuple[Term, ...] = (), include_previous: bool = False, notify=None,
) -> tuple[list[tuple[str, str]], str]:
    """(プロンプトへ渡す資料, 混入チェックの照合元テキスト) を返す。

    照合元には機械生成された当月の資料だけを入れる。前月の考察は人・LLM の散文で、
    そこに混入があった場合に今月の混入を見逃す照合元になってしまうため含めない。

    前月の考察は既定では資料に含めない（include_previous=False）。これは検証できない
    人手の文書であり、含めるとモデルへ渡す資料が「機械生成された対象組織のデータのみ」
    という前提が崩れる。過去のレポートに混入があった場合、それを引き写す経路になる
    （混入チェックには 2文字の姓や金額のような死角があり、そこは塞げない）。
    include_previous=True のときも渡す前に混入チェックを通し、落ちたら除外する。
    """
    notify = notify or (lambda _message: None)
    doc_path = document_path(org_output, month, preview)
    if not doc_path.exists():
        raise DiscussionError(
            f"{doc_path} がありません。先に "
            f"`seat-analyzer analyze{' --preview' if preview else ''}` を実行してください"
        )
    body = report.document_body(doc_path.read_text(encoding="utf-8"))
    materials = [(f"資料1: 分析レポート本文（{month}）", body)]
    source = [body]

    if not preview:
        csv_path = org_output / month / "recommendations.csv"
        if csv_path.exists():
            csv_text = csv_path.read_text(encoding="utf-8")
            materials.append((f"資料2: ユーザ別推奨一覧 recommendations.csv（{month}）", csv_text))
            source.append(csv_text)

    source_text = "\n".join(source)
    prev_dir = _prev_month_dir(org_output, month) if include_previous else None
    if prev_dir is not None:
        prev_report = prev_dir / "report.md"
        if prev_report.exists():
            prev = report.discussion_body(prev_report.read_text(encoding="utf-8"))
            if prev:
                # allow は「今回の生成物を人が確認した」ことに基づく許可なので、
                # 過去の考察の検査には適用しない
                hits = find_leaks(prev, terms, source=source_text)
                if hits:
                    notify(
                        f"前月（{prev_dir.name}）の考察に他組織の語が含まれるため資料から除外します: "
                        f"{', '.join(h.term for h in hits)}"
                    )
                else:
                    materials.append((
                        f"資料{len(materials) + 1}: 前回の正式レポートの考察（{prev_dir.name}）",
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


def run_claude(prompt: str, s: dict) -> str:
    """ヘッドレス Claude CLI を呼び、考察本文のテキストを返す。"""
    command = str(s["command"])
    if shutil.which(command) is None and not Path(command).is_file():
        raise DiscussionError(
            f"'{command}' が見つかりません。Claude Code CLI を PATH に置くか、"
            "config.yaml > discussion.command にフルパスを指定してください"
        )
    cmd = [
        command, "-p",
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
    # 空の作業ディレクトリで実行し、リポジトリのファイルを起点にさせない
    with tempfile.TemporaryDirectory(prefix="seat-analyzer-discuss-") as workdir:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=float(s["timeout_seconds"]), cwd=workdir, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DiscussionError(
                f"{command} が {s['timeout_seconds']} 秒以内に応答しませんでした", transient=True
            ) from exc
        except OSError as exc:
            raise DiscussionError(f"{command} を実行できませんでした: {exc}") from exc

    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # claude -p は API エラーでも 0 を返すため、非ゼロは使い方・設定の誤りとみなす
        detail = ((proc.stderr or "").strip() or out).splitlines()
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
    *, org: str | None, month: str, input_dir: Path, output_dir: Path, org_output: Path,
    cfg: dict, preview: bool = False, force: bool = False, dry_run: bool = False,
    allow: tuple[str, ...] = (), include_previous: bool = False, runner=None, notify=None,
) -> DiscussionOutcome:
    """1組織1月ぶんの考察を生成して書き込む。

    既に記入済みの考察は force が無ければ上書きしない（手書きの考察を守るため）。
    混入が検出された場合は書き直しを求め、max_attempts 回で解消しなければ書き込まない。
    """
    # 既定の runner は呼び出し時に解決する（デフォルト引数で束縛すると差し替えが効かない）
    runner = runner or run_claude
    notify = notify or (lambda _message: None)
    s = discussion_settings(cfg)
    # config の allow_terms は「恒久的に無害と確認済みの語」。--allow-term は単一組織
    # 実行に限られるため、全組織実行でも効く許可の置き場としてこちらを使う
    allow = tuple(allow) + tuple(s.get("allow_terms") or ())
    doc_path = document_path(org_output, month, preview)
    scope = f"{org} {month}" if org else month

    terms = forbidden_terms(
        input_dir=input_dir, output_dir=output_dir, target_org=org, cfg=cfg)
    materials, source = collect_materials(
        org_output=org_output, month=month, preview=preview, terms=terms,
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
        # 生成には最大で timeout_seconds かかる。その間に人が考察を書いた場合や
        # 並行する analyze が本文を更新した場合に、それを巻き戻さないよう書き込み側で
        # 判定と置換を畳み、置換直前にも内容が変わっていないか確認する
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
