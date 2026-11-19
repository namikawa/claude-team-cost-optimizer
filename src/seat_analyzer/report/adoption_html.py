"""adoption-report.html の書き出し（Claude Code の活用と GitHub の成果のレポート。設計書 §27.8）。

人 × 月の表（adoption-people）とチーム × 月の表（adoption-teams）の行だけから、開いて
そのまま読める1枚の HTML を作る。新しい集計は持たず、載せる値は2つの表の値と、その列
どうしの計算（割り算・前月比・3か月比）だけにする。利用者がレポートの値を2つの CSV から
検算できるようにするため。

数値は CSV に書くのと同じ字句（`adoption_csv._cells`・`_team_cells` が作るセル）を数値へ
読み戻してから計算する。丸める前の値から計算すると、CSV から検算した値と末尾の桁が
食い違うことがあるため。入力由来の文字列（集まりの名前・メール・部署・シート種別）は、
行の値に改行の正規化だけを掛けて渡す（CSV 用の式のエスケープは表示に引用符が付くので
掛けない）。HTML のエスケープは Jinja の autoescape に任せる。

組み立ては2段に分ける。行から表示の材料（文字列・数値・座標）を作る処理は I/O を持たない
関数で、タグはテンプレート（templates/adoption-report.html.j2）だけが書く。Python の側で
タグを文字列として組み立てない（CSV 由来の文字列が HTML として解釈されないようにするため）。

同じ行の集合から常に同じバイト列を書く（受け取った順に依らず、絶対パス・時刻・乱数を
書かない）。外部の URL を読まず、JavaScript を使わない。配色は閲覧する環境の設定
（prefers-color-scheme）に従う。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..adoption import (
    GithubData,
    GithubId,
    GroupMonth,
    GroupScope,
    OtelData,
    PersonMonth,
    group_order,
)
from ..calendar_months import previous_month
from .adoption_csv import _cells, _team_cells
from .csv_out import normalize_cell_newlines
from .html import _HTML_ENV, _asset
from .minify import strip_css_comments

# 1つの CSV の行（列名 → セルの字句）
Row = Mapping[str, str]

# 値が途中までになる条件の記号（ColumnTraits.partial などに、組み合わせた文字列で持つ。
# 組み合わせは `_union_partial` で作る）。支出レポートが月の途中までの月（印は＊）と、
# GitHub の収集が完了していない月（印は†）
_SPEND = "s"
_GITHUB = "g"
_BOTH = _SPEND + _GITHUB

# 変化の種類（Metric.kind）。差（人数）・ポイントの差（整数か小数1桁）・比（量）
_COUNT = "count"
_SHARE = "share"
_SHARE1 = "share1"
_RATIO = "pct"

# 値なしの表記（数値の表・変化・人数）
_DASH = "—"

# 入力由来の文字列の列（人 × 月の表）。式のエスケープを掛けず、改行の正規化だけを掛けて渡す
_PERSON_TEXT_COLUMNS = ("email", "seat", "department", "team", "role")

# チーム × 月の表の OpenTelemetry の数値の列
_OTEL_USERS = "otel_users"
_OTEL_EXCLUDED_PEOPLE = "otel_excluded_people"
_OTEL_ACTIVE_DAYS_MEDIAN = "otel_active_days_median"
_OTEL_PROMPTS_MEDIAN = "otel_prompts_median"
_OTEL_PROMPTS = "otel_prompts"
_OTEL_REQUESTS = "otel_requests"
_OTEL_SUBAGENT_REQUESTS = "otel_subagent_requests"
_OTEL_INTERACTIVE_DECISIONS = "otel_interactive_decisions"
_OTEL_INTERACTIVE_DECISIONS_ASKED = "otel_interactive_decisions_asked"
_OTEL_CUSTOM_SUBAGENT_USERS = "otel_custom_subagent_users"
_OTEL_SKILL_USERS = "otel_skill_users"
_OTEL_MCP_USERS = "otel_mcp_users"
_OTEL_CUSTOM_COMMAND_USERS = "otel_custom_command_users"


def _num(value: str) -> float | None:
    """セルの字句を数値へ読み戻す（空欄は None）。"""
    return float(value) if value != "" else None


def _div(a: float | None, b: float | None) -> float | None:
    """割り算（分子か分母が無いとき・分母が0のときは None）。"""
    return a / b if a is not None and b else None


def _value(row: Row, column: str, denominator: str | None = None) -> float | None:
    """CSV の1行から出す列の値、または列どうしの割り算。"""
    value = _num(row[column])
    return value if denominator is None else _div(value, _num(row[denominator]))


def _compact(value: float, prefix: str = "") -> str:
    """大きな数を K・M で縮めた表記（要約と図のラベル）。"""
    for limit, suffix in ((1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{prefix}{value / limit:.1f}{suffix}"
    return f"{prefix}{value:,.0f}"


def _usd_per_line(value: float) -> str:
    """マージ行あたりの費用の表記。

    1 セント未満の値（0 を除く）は、CSV と同じ小数4桁で出す（「$0.00」と書くと本当の 0 と
    見分けられないため）。それ以外（0 を含む）は小数2桁。負の値はほかの金額と同じく
    「$-」で書く。数値の表・図・要約が同じ表記になるよう、この関数だけを使う。
    """
    if 0 < abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


def _local_part(email: str) -> str:
    """メールのローカル部（`@` より後ろは出力のどこにも書かない）。"""
    return email.split("@")[0]


# ------------------------------------------------------------------ 列の性質


@dataclass(frozen=True)
class ColumnTraits:
    """レポートが読む列1つの性質（列の表 `COLUMNS` の1行）。

    partial は値が途中までになる条件（`_SPEND`・`_GITHUB` の組み合わせ。空は途中に
    ならない）、span は値の材料になる月の数（その月を含めて遡る。3か月続けて使った人の数
    だけ3）、github は GitHub の列か（GitHub 分析の無い組織では、その列から出す指標・図・
    要約・枠を出さない）。otel は OpenTelemetry の列か（設定した組織でだけ、その列から
    出す指標・図を出す）。
    """

    partial: str
    span: int = 1
    github: bool = False
    otel: bool = False

    def shown(self, has_github: bool, has_otel: bool) -> bool:
        """列の性質に応じて、この組織に値を載せるか。"""
        return (has_github or not self.github) and (has_otel or not self.otel)


# レポートが読む列 → 性質。指標・図・要約・人ごとの一覧の枠は、自分の列からこの表を引いて
# 途中の値の条件・材料の月数・GitHub や OpenTelemetry の列かを決める（同じ判定を複数の
# 場所に書かない）。
# チーム × 月の表の列と、人 × 月の表の列（code_demand_usd・loc_with_cc・lines_added_capped）を
# 1つの表に持つ。同じ名前の列は2つの表で同じ性質
COLUMNS: dict[str, ColumnTraits] = {
    "seat_holders": ColumnTraits(""),
    "code_users": ColumnTraits(_SPEND),
    "code_users_3m": ColumnTraits(_SPEND, span=3),
    "code_demand_usd": ColumnTraits(_SPEND),
    "code_demand_median_usd": ColumnTraits(_SPEND),
    "loc_median_of_users": ColumnTraits(_SPEND),
    "loc_with_cc": ColumnTraits(_SPEND),
    "github_people": ColumnTraits("", github=True),
    "outside_org_people": ColumnTraits(_SPEND, github=True),
    "lines_added_capped": ColumnTraits(_GITHUB, github=True),
    "merged_pr_count": ColumnTraits(_GITHUB, github=True),
    "pr_additions_median": ColumnTraits(_GITHUB, github=True),
    "lead_time_median_hours": ColumnTraits(_GITHUB, github=True),
    "lead_time_p90_hours": ColumnTraits(_GITHUB, github=True),
    "cost_per_merged_line_usd": ColumnTraits(_BOTH, github=True),
    _OTEL_USERS: ColumnTraits("", otel=True),
    _OTEL_EXCLUDED_PEOPLE: ColumnTraits("", otel=True),
    _OTEL_ACTIVE_DAYS_MEDIAN: ColumnTraits("", otel=True),
    _OTEL_PROMPTS_MEDIAN: ColumnTraits("", otel=True),
    _OTEL_PROMPTS: ColumnTraits("", otel=True),
    _OTEL_REQUESTS: ColumnTraits("", otel=True),
    _OTEL_SUBAGENT_REQUESTS: ColumnTraits("", otel=True),
    _OTEL_INTERACTIVE_DECISIONS: ColumnTraits("", otel=True),
    _OTEL_INTERACTIVE_DECISIONS_ASKED: ColumnTraits("", otel=True),
    _OTEL_CUSTOM_SUBAGENT_USERS: ColumnTraits("", otel=True),
    _OTEL_SKILL_USERS: ColumnTraits("", otel=True),
    _OTEL_MCP_USERS: ColumnTraits("", otel=True),
    _OTEL_CUSTOM_COMMAND_USERS: ColumnTraits("", otel=True),
}


def _union_partial(kinds: Iterable[str]) -> str:
    """途中の値の条件の和（記号は支出 → GitHub の順に並べる）。"""
    kinds = list(kinds)
    return "".join(mark for mark in (_SPEND, _GITHUB) if any(mark in kind for kind in kinds))


def _traits(columns: Iterable[str]) -> ColumnTraits:
    """いくつかの列から出す値の性質（途中の値の条件は和、材料の月数は大きい方、GitHub や
    OpenTelemetry の列が1つでもあれば該当する指標の値）。列の表に無い列は KeyError。"""
    found = [COLUMNS[name] for name in columns]
    return ColumnTraits(
        partial=_union_partial(traits.partial for traits in found),
        span=max(traits.span for traits in found),
        github=any(traits.github for traits in found),
        otel=any(traits.otel for traits in found),
    )


# ------------------------------------------------------------------ 指標


@dataclass(frozen=True)
class Metric:
    """数値の表と集まりの一覧に出す指標1つ（設計書 §27.8 の指標の表の1行）。

    columns はチーム × 月の表の列で、1つならその列の値、2つなら分子 ÷ 分母（値なしは
    None）。fmt は値の表記、kind は変化の種類（`_COUNT`＝差・`_SHARE` と `_SHARE1`＝
    ポイントの差の整数と小数1桁・`_RATIO`＝比）、sep は数値の表でこの行の上に区切りの線を
    引くか、overview は集まりの一覧に出すか。

    見出し・書式・変化の種類・一覧に出すか・区切りの線は、この指標の表（`METRICS`）が持つ。
    途中の値になる条件（partial）・材料の月数（span）・GitHub や OpenTelemetry の指標か
    （github・otel）は、columns から列の表（`COLUMNS`）を引いて決める（割り算の指標は
    分子と分母の列の和）。
    """

    key: str
    label: str
    columns: tuple[str, ...]
    fmt: Callable[[float], str]
    kind: str
    sep: bool = False
    overview: bool = False

    def __post_init__(self) -> None:
        if len(self.columns) not in (1, 2):
            raise ValueError(f"columns は列1つか、分子と分母の2つです: {self.columns!r}")
        _traits(self.columns)  # 列の表に無い列を、定義した時点で止める

    def value(self, row: Row) -> float | None:
        """チーム × 月の表の1行から出す値（列の値か、列どうしの割り算）。"""
        return _value(row, *self.columns)

    @property
    def traits(self) -> ColumnTraits:
        """指標を作る列の性質。"""
        return _traits(self.columns)

    @property
    def partial(self) -> str:
        """値が途中までになる条件（列の性質の和）。"""
        return self.traits.partial

    @property
    def span(self) -> int:
        """値の材料になる月の数（列の性質の大きい方）。"""
        return self.traits.span

    @property
    def github(self) -> bool:
        """GitHub の指標か（GitHub 分析の無い組織では出さない）。"""
        return self.traits.github

    @property
    def otel(self) -> bool:
        """OpenTelemetry の指標か（設定した組織でだけ出す）。"""
        return self.traits.otel


def _column(name: str) -> Callable[[Row], float | None]:
    """チーム × 月の表の列の値。"""
    return lambda row: _num(row[name])


def _percent(value: float) -> str:
    """割合の表記（整数の百分率）。"""
    return f"{value:.0%}"


METRICS = (
    Metric("holders", "シートを持つ人", ("seat_holders",), lambda v: f"{v:.0f} 人", _COUNT),
    Metric("share", "使った人の割合", ("code_users", "seat_holders"),
           lambda v: f"{v:.0%}", _SHARE, overview=True),
    Metric("share3", "3 か月続けて使った人の割合", ("code_users_3m", "seat_holders"),
           lambda v: f"{v:.0%}", _SHARE),
    Metric("demand_pp", "需要 / 人（USD）", ("code_demand_usd", "seat_holders"),
           lambda v: f"${v:,.0f}", _RATIO, overview=True),
    Metric("demand_median", "需要の中央値（USD）", ("code_demand_median_usd",),
           lambda v: f"${v:,.0f}", _RATIO),
    Metric("loc_median", "加えた行（中央値）", ("loc_median_of_users",),
           lambda v: f"{v:,.0f}", _RATIO, overview=True),
    Metric("gh_people", "GitHub の対象", ("github_people",), lambda v: f"{v:.0f} 人",
           _COUNT, sep=True),
    Metric("outside_org", "うち成果が見えない人", ("outside_org_people",),
           lambda v: f"{v:.0f} 人", _COUNT),
    Metric("lines_pp", "マージ行 / 人", ("lines_added_capped", "github_people"),
           lambda v: f"{v:,.0f}", _RATIO, overview=True),
    Metric("pr_pp", "PR / 人", ("merged_pr_count", "github_people"),
           lambda v: f"{v:.1f}", _RATIO, overview=True),
    Metric("pr_size", "PR の大きさ（追加行の中央値）", ("pr_additions_median",),
           lambda v: f"{v:,.0f}", _RATIO),
    Metric("lead_median", "リードタイム 中央値（時間）", ("lead_time_median_hours",),
           lambda v: f"{v:.1f}", _RATIO, overview=True),
    Metric("lead_p90", "リードタイム 遅い 1 割（時間）", ("lead_time_p90_hours",),
           lambda v: f"{v:.1f}", _RATIO),
    Metric("cost_per_line", "費用 / マージ行（USD）", ("cost_per_merged_line_usd",),
           _usd_per_line, _RATIO, overview=True),
    Metric("otel_users", "使い方が見える人（OpenTelemetry）", (_OTEL_USERS,),
           lambda v: f"{v:.0f} 人", _COUNT, sep=True),
    Metric("otel_excluded", "届きが悪く除いた人", (_OTEL_EXCLUDED_PEOPLE,),
           lambda v: f"{v:.0f} 人", _COUNT),
    Metric("otel_days", "使った日数（中央値）", (_OTEL_ACTIVE_DAYS_MEDIAN,),
           lambda v: f"{v:.1f}".removesuffix(".0") + " 日", _RATIO),
    Metric("otel_prompts", "指示の回数（中央値）", (_OTEL_PROMPTS_MEDIAN,),
           lambda v: f"{v:,.0f}", _RATIO),
    Metric("otel_rpp", "1 回の指示あたりのリクエスト", (_OTEL_REQUESTS, _OTEL_PROMPTS),
           lambda v: f"{v:.1f}", _RATIO),
    Metric("otel_asked", "その場で確認した割合",
           (_OTEL_INTERACTIVE_DECISIONS_ASKED, _OTEL_INTERACTIVE_DECISIONS),
           lambda v: f"{v:.1%}", _SHARE1),
    Metric("otel_sub", "サブエージェント経由の割合", (_OTEL_SUBAGENT_REQUESTS, _OTEL_REQUESTS),
           _percent, _SHARE),
    Metric("otel_custom_sub", "自作のサブエージェントを使った人の割合",
           (_OTEL_CUSTOM_SUBAGENT_USERS, _OTEL_USERS), _percent, _SHARE),
    Metric("otel_skill", "スキルを使った人の割合", (_OTEL_SKILL_USERS, _OTEL_USERS),
           _percent, _SHARE),
    Metric("otel_mcp", "MCP を使った人の割合", (_OTEL_MCP_USERS, _OTEL_USERS), _percent, _SHARE),
    Metric("otel_command", "自作のコマンドを使った人の割合",
           (_OTEL_CUSTOM_COMMAND_USERS, _OTEL_USERS), _percent, _SHARE),
)


def _series(
    value: Callable[[Row], float | None], months: Sequence[str], by_month: Mapping[str, Row]
) -> list[float | None]:
    """月ごとの値（行の無い月は None）。"""
    return [value(by_month[m]) if m in by_month else None for m in months]


# ------------------------------------------------------------ 途中の値・変化


@dataclass(frozen=True)
class Partial:
    """値が途中までの月。

    spend は支出レポートが月の途中までの月（組織全体の行の spend_complete が False。印は
    ＊）、github は GitHub の収集が完了していない月（github_cache_complete が False。印は†）。
    """

    spend: frozenset[str]
    github: frozenset[str]

    def mark(self, month: str, kinds: str) -> str:
        """その月の値に付ける印（kinds は途中までになる条件。当てはまらなければ空）。"""
        return ("＊" if _SPEND in kinds and month in self.spend else "") + (
            "†" if _GITHUB in kinds and month in self.github else "")

    def flags(self, months: Sequence[str], kinds: str, span: int = 1) -> list[bool]:
        """月ごとに、その値が途中までの値か。

        span は値の材料になる月の数で、その月を含めて遡る span か月のどれかが途中なら
        途中の値とする（months は暦で連続している前提。`_month_range` が作る月の軸）。
        """
        own = [bool(self.mark(month, kinds)) for month in months]
        return [any(own[max(0, i - span + 1):i + 1]) for i in range(len(months))]


def _change(latest: float, previous: float, kind: str) -> str:
    """変化の表記。差は引き算（人数は小数1桁まで・割合はポイントの整数か小数1桁）、
    比は新 ÷ 旧 − 1。

    比で古い方が0のときは値なし。負号は「−」（U+2212）で書く。増減の良し悪しを表す
    色は付けない（参考値で、良し悪しを断定しないため）。
    """
    if kind == _COUNT:
        text = f"{latest - previous:+.1f}".replace(".0", "")
    elif kind in (_SHARE, _SHARE1):
        digits = 1 if kind == _SHARE1 else 0
        text = f"{(latest - previous) * 100:+.{digits}f} pt"
    elif previous == 0:
        return _DASH
    else:
        text = f"{latest / previous - 1:+.0%}"
    return text.replace("-", "−")


def _delta(values: Sequence[float | None], kind: str, bad: Sequence[bool] = ()) -> str:
    """前月比（最新の月と、その前の月）。どちらかが無いか、途中までの値なら値なし。"""
    if len(values) < 2 or values[-1] is None or values[-2] is None or any(bad[-2:]):
        return _DASH
    return _change(values[-1], values[-2], kind)


def _trend3(values: Sequence[float | None], kind: str, bad: Sequence[bool] = ()) -> str:
    """3か月比（直近3か月の平均と、その前の3か月の平均）。

    6か月そろい、どれも値があり、途中までの値が無いときだけ出す。
    """
    recent, before = list(values[-3:]), list(values[-6:-3])
    if (len(recent) < 3 or len(before) < 3 or any(v is None for v in recent + before)
            or any(bad[-6:])):
        return _DASH
    return _change(sum(recent) / 3, sum(before) / 3, kind)


# ------------------------------------------------------------------ 月の軸


def _month_range(first: str, last: str) -> list[str]:
    """first から last までの暦の月（YYYY-MM）を、間を空けずに昇順で並べる。"""
    months = [last]
    while months[-1] > first:
        months.append(previous_month(months[-1]))
    return list(reversed(months))


def _year_starts(months: Sequence[str]) -> set[int]:
    """年を書き添える位置（最初の月と、各年の1月）。"""
    return {i for i, month in enumerate(months) if i == 0 or month[5:] == "01"}


def _month_heads(
    months: Sequence[str], partial: Partial, kinds: str, *, short: bool = False
) -> list[dict]:
    """表の月の見出し（year は添える年で、添えない月は None。mark は途中の値の印）。"""
    years = _year_starts(months)
    return [{
        "year": month[:4] if i in years else None,
        "label": f"{int(month[5:])}" if short else f"{int(month[5:])}月",
        "mark": partial.mark(month, kinds),
    } for i, month in enumerate(months)]


# ------------------------------------------------------------------ 図


def _f1(value: float) -> str:
    """座標の字句（小数1桁）。"""
    return f"{value:.1f}"


def _nice_scale(value: float, whole: bool = False) -> tuple[float, int]:
    """軸の上端（1・2・5 × 10 の累乗）と、目盛りの小数の桁数。

    whole は人数・件数の図（目盛りを整数にする。上端が5以下なら2の倍数に切り上げる）。
    """
    if whole and value <= 5:
        return float(max(2, 2 * math.ceil(value / 2))), 0
    if value <= 0:
        return 1.0, 1
    exp = math.floor(math.log10(value))
    step = 10
    for candidate in (1, 2, 5):
        if value <= candidate * 10.0 ** exp * (1 + 1e-9):
            step = candidate
            break
    top = step * 10.0 ** exp
    if step == 10:
        step, exp = 1, exp + 1
    return top, max(0, -exp + (1 if step in (1, 5) else 0))


def _tick_label(tick: float, top: float, decimals: int, prefix: str) -> str:
    """目盛りの表記（上端が1万以上なら K・M で縮める）。"""
    if tick == 0:
        return f"{prefix}0"
    if top >= 10000:
        for limit, suffix in ((1e6, "M"), (1e3, "K")):
            if tick >= limit:
                return f"{prefix}{tick / limit:g}{suffix}"
    return f"{prefix}{tick:,.{decimals}f}"


def _segments(points: Sequence[tuple[float, float | None]],
              y: Callable[[float], float]) -> list[str]:
    """折れ線の区切り（値の無い月で線を切る。点が2つ以上の区切りだけ）。"""
    out: list[str] = []
    segment: list[str] = []
    for x, value in points:
        if value is None:
            if len(segment) > 1:
                out.append(" ".join(segment))
            segment = []
        else:
            segment.append(f"{_f1(x)},{_f1(y(value))}")
    if len(segment) > 1:
        out.append(" ".join(segment))
    return out


def _line_chart(
    months: Sequence[str], values: Sequence[float | None], fmt: Callable[[float], str],
    prefix: str = "", whole: bool = False, marks: Sequence[str] = (),
) -> dict:
    """組織全体の推移の折れ線（1つの図に1つの指標）の材料。

    負の値は0の位置に置く（title とラベルの値は変えない）。月のラベルは月が増えたら最新の
    月を起点に間引き、印のある月は間引かない。年は最初の月と各年の1月に添える。値の無い
    月は点を置かず線を切り、最後の点に値のラベルを付ける。
    """
    width, height, left, right, top, bottom = 320, 182, 46, 66, 14, 38
    plot_w, plot_h = width - left - right, height - top - bottom
    top_value, decimals = _nice_scale(max((v for v in values if v is not None), default=1.0),
                                      whole)
    count = len(months)
    xs = [left + (plot_w * i / max(count - 1, 1)) for i in range(count)]
    every = math.ceil(count / 8)
    years = _year_starts(months)

    def y(v: float) -> float:
        return top + plot_h * (1 - max(v, 0.0) / top_value)

    ticks = [{
        "cls": "axisline" if tick == 0 else "gridline",
        "x1": str(left), "x2": str(left + plot_w), "y": _f1(y(tick)),
        "label_x": str(left - 6), "label_y": _f1(y(tick) + 4),
        "label": _tick_label(tick, top_value, decimals, prefix),
    } for tick in (0, top_value / 2, top_value)]
    # 横軸のラベル（月ごとに、月のラベル → 年のラベルの順）
    axis_labels = []
    for i, (x, month) in enumerate(zip(xs, months, strict=True)):
        mark = marks[i] if marks else ""
        if (count - 1 - i) % every == 0 or mark:
            axis_labels.append({"x": _f1(x), "y": str(height - 22),
                                "text": f"{int(month[5:])}月{mark}"})
        if i in years:
            axis_labels.append({"x": _f1(x), "y": str(height - 8), "text": month[:4]})
    last = max((i for i, v in enumerate(values) if v is not None), default=None)
    dots = []
    value_label = None
    for i, (x, value, month) in enumerate(zip(xs, values, months, strict=True)):
        if value is None:
            continue
        dots.append({"cx": _f1(x), "cy": _f1(y(value)), "tip": f"{month}: {fmt(value)}"})
        if i == last:
            value_label = {"x": _f1(x + 8), "y": _f1(y(value) + 4), "text": fmt(value)}
    return {
        "viewbox": f"0 0 {width} {height}",
        "ticks": ticks,
        "axis_labels": axis_labels,
        "segments": _segments(list(zip(xs, values, strict=True)), y),
        "dots": dots,
        "value_label": value_label,
    }


def _sparkline(values: Sequence[float | None]) -> dict:
    """推移の小さな線（上下の幅はその系列の最小〜最大。値が2つ未満なら空の図）。"""
    known = [v for v in values if v is not None]
    width, height, pad = 86, 22, 3
    view = {"viewbox": f"0 0 {width} {height}", "segments": [], "dot": None}
    if len(known) < 2:
        return view
    low, high = min(known), max(known)
    span = (high - low) or 1.0
    xs = [pad + (width - 2 * pad) * i / max(len(values) - 1, 1) for i in range(len(values))]

    def y(v: float) -> float:
        return pad + (height - 2 * pad) * (1 - (v - low) / span)

    view["segments"] = _segments(list(zip(xs, values, strict=True)), y)
    last = max(i for i, v in enumerate(values) if v is not None)
    view["dot"] = {"cx": _f1(xs[last]), "cy": _f1(y(values[last]))}
    return view


def _scatter(
    points: Sequence[tuple[str, float, float]], x_name: str, y_name: str, x_prefix: str = ""
) -> dict:
    """人ごとの散布図の材料（両対数。0 も置ける目盛りで、負の値は0の位置）。

    points は（表示名・横軸の値・縦軸の値）。
    """
    width, height, left, right, top, bottom = 460, 380, 70, 16, 14, 44
    plot_w, plot_h = width - left - right, height - top - bottom

    def pos(v: float) -> float:
        return math.log10(max(v, 0.0) + 1)

    x_top = max(math.ceil(max((pos(p[1]) for p in points), default=1)), 2)
    y_top = max(math.ceil(max((pos(p[2]) for p in points), default=1)), 2)

    def px(v: float) -> float:
        return left + plot_w * pos(v) / x_top

    def py(v: float) -> float:
        return top + plot_h * (1 - pos(v) / y_top)

    def tick(exp: int, prefix: str) -> str:
        return f"{prefix}0" if exp == 0 else _tick_label(10.0 ** exp, 10.0 ** exp, 0, prefix)

    rows = []
    for exp in range(y_top + 1):
        value = 0 if exp == 0 else 10 ** exp
        rows.append({
            "cls": "axisline" if exp == 0 else "gridline",
            "x1": str(left), "x2": str(left + plot_w),
            "y1": _f1(py(value)), "y2": _f1(py(value)),
            "label_x": str(left - 6), "label_y": _f1(py(value) + 4), "label": tick(exp, ""),
            "anchor": "end",
        })
    columns = []
    for exp in range(x_top + 1):
        value = 0 if exp == 0 else 10 ** exp
        columns.append({
            "cls": "axisline" if exp == 0 else "gridline",
            "x1": _f1(px(value)), "x2": _f1(px(value)),
            "y1": str(top), "y2": str(top + plot_h),
            "label_x": _f1(px(value)), "label_y": str(top + plot_h + 16),
            "label": tick(exp, x_prefix), "anchor": "middle",
        })
    return {
        "viewbox": f"0 0 {width} {height}",
        "grid": rows + columns,
        "x_name": {"x": _f1(left + plot_w / 2), "y": str(height - 6), "text": x_name},
        "y_name": {"transform": f"translate(14,{_f1(top + plot_h / 2)}) rotate(-90)",
                   "text": y_name},
        "points": [{
            "cx": _f1(px(xv)), "cy": _f1(py(yv)),
            "tip": f"{name}: {x_name} {x_prefix}{xv:,.0f} / {y_name} {yv:,.0f}",
        } for name, xv, yv in points],
    }


# ------------------------------------------------------------------ 表


def _metric_table(
    months: Sequence[str], by_month: Mapping[str, Row], metrics: Sequence[Metric], partial: Partial,
) -> dict:
    """1つの集まりの、指標 × 月の数値の表（前月比・3か月比・推移の小さな線を添える）。

    月の見出しの印の条件は、表に出す指標の途中の値の条件の和。
    """
    rows = []
    for metric in metrics:
        values = _series(metric.value, months, by_month)
        bad = partial.flags(months, metric.partial, metric.span)
        rows.append({
            "label": metric.label,
            "sep": metric.sep,
            "cells": [metric.fmt(v) if v is not None else _DASH for v in values],
            "delta": _delta(values, metric.kind, bad),
            "trend3": _trend3(values, metric.kind, bad),
            "spark": _sparkline(values),
        })
    heads = _month_heads(months, partial, _union_partial(metric.partial for metric in metrics))
    return {"heads": heads, "rows": rows}


def _overview_table(
    head: str, months: Sequence[str], groups: Sequence[tuple[str, str, Mapping[str, Row]]],
    metrics: Sequence[Metric], with_trend3: bool, partial: Partial,
) -> dict:
    """集まりの一覧（1行が1つの集まり。groups は（リンク先の id・名前・月ごとの行）で、
    先頭は組織全体）。

    名前に添える人数は最新の月の値で、最新の月に行の無い集まりは値なし。with_trend3 が
    偽なら3か月比を書かない（月の軸が6か月に満たない組織）。
    """
    metrics = [metric for metric in metrics if metric.overview]
    rows = []
    for index, (anchor, name, by_month) in enumerate(groups):
        last = by_month.get(months[-1])
        cells = []
        for metric in metrics:
            values = _series(metric.value, months, by_month)
            bad = partial.flags(months, metric.partial, metric.span)
            value = (_DASH if values[-1] is None
                     else metric.fmt(values[-1]) + partial.mark(months[-1], metric.partial))
            cells.append({
                "spark": _sparkline(values),
                "value": value,
                "delta": _delta(values, metric.kind, bad),
                "trend3": _trend3(values, metric.kind, bad) if with_trend3 else None,
            })
        rows.append({
            "anchor": anchor,
            "name": name,
            "holders": f"{last['seat_holders']} 人" if last else _DASH,
            "is_org": index == 0,
            "cells": cells,
        })
    return {"head": head, "labels": [metric.label for metric in metrics], "rows": rows}


# ------------------------------------------------------------ 人ごとの一覧


@dataclass(frozen=True)
class HeatBlock:
    """人ごとの一覧の1つの枠（人 × 月の色の濃淡）。

    column は人 × 月の表の列、edges は色の段の境目（この値以上で1段濃くなる。5段）、
    prefix は値の単位の接頭辞。GitHub の枠か（GitHub 分析の無い組織では出さない）と、月の
    見出しの印の条件は、column から列の表（`COLUMNS`）を引いて決める。
    境目は固定の値で、設定には置かない（色の段は見た目の区切りで、載せる行や数え方を
    変えないため。設計書 §27.8）。
    """

    column: str
    title: str
    edges: tuple[float, ...]
    prefix: str

    @property
    def traits(self) -> ColumnTraits:
        """列の性質。"""
        return COLUMNS[self.column]

    @property
    def github(self) -> bool:
        """GitHub の枠か。"""
        return self.traits.github


BLOCKS = (
    HeatBlock("code_demand_usd", "Claude Code の需要（USD）", (0.005, 50, 200, 1000, 3000), "$"),
    HeatBlock("loc_with_cc", "Claude Code が加えた行", (1, 100, 1000, 10000, 100000), ""),
    HeatBlock("lines_added_capped", "GitHub のマージ行（上限つき）",
              (1, 100, 1000, 5000, 20000), ""),
)

# GitHub ID が login でない人の、人ごとの一覧の表記（セルの title と、名前の横の注記）
_GITHUB_ID_REASONS = {
    GithubId.NONE.value: "この Organization で開発しない人（GitHub ID なし）",
    GithubId.UNSET.value: "GitHub ID が未記入",
}
_GITHUB_ID_NOTES = {GithubId.NONE.value: "GitHub なし", GithubId.UNSET.value: "GitHub 未記入"}


def _bucket(value: float, edges: Sequence[float]) -> str:
    """色の段のクラス（0 以下は z、境目以上の数だけ b1〜b5。負の値は0の位置）。"""
    level = sum(1 for edge in edges if value >= edge)
    return "z" if level == 0 else f"b{level}"


def _heat_cell(row: Row | None, block: HeatBlock, who: str, month: str,
               analyzed: bool) -> dict:
    """人ごとの一覧の1セル（cls が None のセルは空白＝その月にシートを持たない）。

    行の無い月（analyzed が偽）は全員「×」、GitHub ID が login でない人のマージ行は「–」、
    値なしは枠だけ、0 は色の段と別の色。値は title 属性に書く（負の値もそのまま）。
    """
    if not analyzed:
        return {"cls": "c na", "title": f"{who} {month}: 分析できなかった月", "text": "×"}
    if row is None:
        return {"cls": None, "title": None, "text": ""}
    head = f"{who} {month}"
    column = block.column
    if block.github:
        reason = _GITHUB_ID_REASONS.get(row["github_id"])
        if reason is not None:
            return {"cls": "c na", "title": f"{head}: {reason}", "text": "–"}
        if row[column] == "":
            return {"cls": "c nd", "title": f"{head}: 行数の材料なし", "text": ""}
        value = _num(row[column]) or 0.0
        tip = (f"{head}: マージ行 {value:,.0f}・コミット {row['commit_count']} 件・"
               f"PR {row['merged_pr_count']} 件")
        if row["outside_org"] == "True":
            tip += (
                f'（Claude Code が加えた行 {int(row["loc_with_cc"]):,} があるのに、この Organization に'
                f'コミットも PR も無い月・{row["outside_org_months"]} か月連続）'
            )
            return {"cls": f"c {_bucket(value, block.edges)} fl", "title": tip, "text": "!"}
        return {"cls": f"c {_bucket(value, block.edges)}", "title": tip, "text": ""}
    if row[column] == "":
        return {"cls": "c nd", "title": f"{head}: データなし", "text": ""}
    value = _num(row[column]) or 0.0
    digits = 2 if block.prefix else 0
    return {"cls": f"c {_bucket(value, block.edges)}",
            "title": f"{head}: {block.prefix}{value:,.{digits}f}", "text": ""}


def _legend_label(edge: float, prefix: str) -> str:
    """凡例の段の表記（境目が1未満の段は「0 より大」）。"""
    return f"{prefix}{int(edge):,}〜" if edge >= 1 else f"{prefix}0 より大"


def _people_table(
    people: Mapping[str, Mapping[str, Row]], months: Sequence[str], analyzed: frozenset[str],
    has_github: bool, partial: Partial,
) -> dict:
    """人ごとの一覧（参考）の材料。

    人の並びは部署（その人の最後の月の行の値。未記入は末尾）とメールの順で、量の多い順に
    しない。部署が変わるところに部署の行を挟む。メールはローカル部だけを出す。
    """
    blocks = [block for block in BLOCKS if has_github or not block.github]

    def last_row(email: str) -> Row:
        return people[email][max(people[email])]

    order = sorted(people, key=lambda e: (last_row(e)["department"] == "",
                                          last_row(e)["department"], e))
    rows: list[dict] = []
    department = None
    for email in order:
        row_last = last_row(email)
        if row_last["department"] != department:
            department = row_last["department"]
            rows.append({"department": department or "部署なし"})
        who = _local_part(email)
        rows.append({
            "name": who,
            "seat": row_last["seat"],
            "github": _GITHUB_ID_NOTES.get(row_last["github_id"], ""),
            "blocks": [[_heat_cell(people[email].get(m), block, who, m, m in analyzed)
                        for m in months] for block in blocks],
        })
    return {
        "legend": [{
            "title": block.title,
            "swatches": [{"cls": f"b{i + 1}", "label": _legend_label(edge, block.prefix)}
                         for i, edge in enumerate(block.edges)],
        } for block in blocks],
        "blocks": [{
            "title": block.title,
            "heads": _month_heads(months, partial, block.traits.partial, short=True),
        } for block in blocks],
        "span": len(months),
        "rows": rows,
    }


# ------------------------------------------------------------------ 組み立て


@dataclass(frozen=True)
class _Tile:
    """最新の月の要約1つ（組織全体の行の列から）。

    fmt は値と最新の月の組織全体の行から表記を作る関数、sub は最新の月の後ろに添える補足、
    unit は前月比に添える単位。途中の値の条件・GitHub の要約かは、column から列の表
    （`COLUMNS`）を引いて決める。
    """

    label: str
    column: str
    fmt: Callable[[float, Row], str]
    kind: str
    sub: str
    unit: str = ""

    @property
    def traits(self) -> ColumnTraits:
        """列の性質。"""
        return COLUMNS[self.column]


@dataclass(frozen=True)
class _Chart:
    """組織全体の推移の図1つ（組織全体の行の折れ線）。

    column は値の列。割合の図は denominator に分母の列を持ち、割り算した値を百分率にして
    描く。途中の値の条件・GitHub や OpenTelemetry の図かは、図が使う列から列の表
    （`COLUMNS`）を引いて決める。
    """

    title: str
    sub: str
    column: str
    fmt: Callable[[float], str]
    prefix: str
    whole: bool
    denominator: str | None = None

    @property
    def columns(self) -> tuple[str, ...]:
        """値を出す列（割合の図は分子と分母）。"""
        return (self.column,) if self.denominator is None else (self.column, self.denominator)

    @property
    def traits(self) -> ColumnTraits:
        """列の性質。"""
        return _traits(self.columns)

    def value(self, row: Row) -> float | None:
        """チーム × 月の表の1行から出す値（割合の図は分子 ÷ 分母 × 100）。"""
        value = _value(row, self.column, self.denominator)
        return value * 100 if self.denominator is not None and value is not None else value


_CHARTS = (
    _Chart("Claude Code を使った人", "その月に Claude Code のリクエストがあった人数",
           "code_users", lambda v: f"{v:.0f} 人", "", True),
    _Chart("Claude Code の需要", "API 換算の合計（USD）", "code_demand_usd",
           lambda v: _compact(v, "$"), "$", False),
    _Chart("Claude Code が加えた行（中央値）", "使った人の中央値", "loc_median_of_users",
           _compact, "", False),
    _Chart("GitHub のマージ行", "コミット単位・上限つきの合計", "lines_added_capped",
           _compact, "", False),
    _Chart("マージされた PR", "シートを持つ人が作った PR の件数", "merged_pr_count",
           lambda v: f"{v:,.0f} 件", "", True),
    _Chart("マージ行あたりの費用", "GitHub の対象の人の需要 ÷ その人たちのマージ行（USD）",
           "cost_per_merged_line_usd", _usd_per_line, "$", False),
    _Chart("その場で確認した割合（%）",
           "端末の対話で、編集・コマンド・MCP を実行してよいかの判断のうち",
           _OTEL_INTERACTIVE_DECISIONS_ASKED, lambda v: f"{v:.1f}%", "", False,
           denominator=_OTEL_INTERACTIVE_DECISIONS),
    _Chart("サブエージェント経由の割合（%）", "リクエストのうちサブエージェントが出した分",
           _OTEL_SUBAGENT_REQUESTS, lambda v: f"{v:.0f}%", "", False,
           denominator=_OTEL_REQUESTS),
    _Chart("自作のサブエージェントを使った人（%）", "使い方が見える人のうち",
           _OTEL_CUSTOM_SUBAGENT_USERS, lambda v: f"{v:.0f}%", "", False,
           denominator=_OTEL_USERS),
)

_TILES = (
    # 使った人の数は、最新の月のシートを持つ人の数と並べる
    _Tile("Claude Code を使った人", "code_users",
          lambda v, last: f"{v:.0f} / {last['seat_holders']} 人", _COUNT,
          "・シートを持つ人のうち", unit=" 人"),
    _Tile("Claude Code の需要", "code_demand_usd", lambda v, _: _compact(v, "$"), _RATIO,
          "・API 換算の合計"),
    _Tile("GitHub のマージ行", "lines_added_capped", lambda v, _: _compact(v), _RATIO,
          "・上限つきの合計"),
    _Tile("マージ行あたりの費用", "cost_per_merged_line_usd", lambda v, _: _usd_per_line(v),
          _RATIO, "・需要 ÷ マージ行"),
)

# 人ごとの散布図（参考）。（見出し・補足・横軸の列・横軸の名前・横軸の単位の接頭辞）
_SCATTERS = (
    ("Claude Code が加えた行 × マージ行",
     "右下にいる人ほど、加えた行に対してこの Organization にマージされた行が少ない",
     "loc_with_cc", "Claude Code が加えた行", ""),
    ("Claude Code の需要 × マージ行", "右下にいる人ほど、マージ行あたりの費用が大きい",
     "code_demand_usd", "Claude Code の需要（USD）", "$"),
)
# 散布図の縦軸（人 × 月の表の列と、その名前）
_SCATTER_Y_COLUMN = "lines_added_capped"
_SCATTER_Y_NAME = "GitHub のマージ行（上限つき）"

# 集まりの節（チームが先）。（種別・見出しの語）
_GROUP_SECTIONS = ((GroupScope.TEAM, "チーム"), (GroupScope.DEPARTMENT, "部署"))

# 集まりの一覧に3か月比を書く、月の軸の最小の長さ（3か月比は6か月そろうときだけ出る）
_TREND3_MONTHS = 6

# 組織全体の数値の表の id（集まりの一覧の先頭の行のリンク先）
_ORG_ANCHOR = "g-organization"


def _person_cells(row: PersonMonth) -> dict[str, str]:
    """人 × 月の表の1行のセル（数値は CSV の字句、入力由来の文字列は改行の正規化だけ）。"""
    cells = _cells(row)
    for name in _PERSON_TEXT_COLUMNS:
        cells[name] = normalize_cell_newlines(getattr(row, name))
    return cells


def _group_cells(row: GroupMonth) -> dict[str, str]:
    """チーム × 月の表の1行のセル（数値は CSV の字句、名前は改行の正規化だけ）。"""
    cells = _team_cells(row)
    cells["name"] = normalize_cell_newlines(row.name)
    return cells


def _check_rows(people: Iterable[PersonMonth], teams: Iterable[GroupMonth]
                ) -> tuple[list[PersonMonth], list[GroupMonth]]:
    """行の型と重なりを確かめ、並べた行を返す（型の誤りは TypeError、重なりは ValueError）。"""
    person_rows = list(people)
    for row in person_rows:
        if not isinstance(row, PersonMonth):
            raise TypeError(f"people には PersonMonth が必要です: {type(row).__name__}")
    team_rows = list(teams)
    for row in team_rows:
        if not isinstance(row, GroupMonth):
            raise TypeError(f"teams には GroupMonth が必要です: {type(row).__name__}")
    person_rows.sort(key=lambda row: (row.email, row.month))
    team_rows.sort(key=group_order)
    # 同じ鍵の行が2つあると、どちらを載せるかが受け取った順で変わる
    if len({(row.email, row.month) for row in person_rows}) != len(person_rows):
        raise ValueError("同じ人の同じ月の行が複数あります")
    if len({group_order(row) for row in team_rows}) != len(team_rows):
        raise ValueError("同じ集まりの同じ月の行が複数あります")
    return person_rows, team_rows


def _report_view(people: Iterable[PersonMonth], teams: Iterable[GroupMonth], org: str) -> dict:
    """レポートの表示の材料（I/O を持たない。テンプレートへそのまま渡す）。

    月の軸は組織全体の行の最初の月から最新の月までの暦の月で、行の無い月は値なしの月と
    して並べる。組織全体の行が1つも無ければ ValueError（最新の月が決まらない）。
    """
    person_rows, team_rows = _check_rows(people, teams)
    org_rows = {row.month: row for row in team_rows if row.scope is GroupScope.ORGANIZATION}
    if not org_rows:
        raise ValueError("組織全体の行（scope が organization）がありません")
    groups: dict[tuple[GroupScope, str], dict[str, Row]] = {}
    for row in team_rows:
        groups.setdefault((row.scope, row.name), {})[row.month] = _group_cells(row)
    org_series = groups[(GroupScope.ORGANIZATION, "")]
    present = sorted(org_rows)
    months = _month_range(present[0], present[-1])
    missing = [m for m in months if m not in org_rows]
    latest = months[-1]
    has_github = any(row.github_data is not GithubData.NOT_CONFIGURED for row in team_rows)
    has_otel = any(row.otel_data is not OtelData.NOT_CONFIGURED for row in team_rows)
    metrics = [metric for metric in METRICS if metric.traits.shown(has_github, has_otel)]
    partial = Partial(
        spend=frozenset(m for m, row in org_rows.items() if not row.spend_complete),
        github=frozenset(m for m, row in org_rows.items() if row.github_cache_complete is False),
    )

    def org_values(column: str) -> list[float | None]:
        return _series(_column(column), months, org_series)

    def ordered(scope: GroupScope) -> list[str]:
        # 最新の月の人数の多い順（同数は名前の順）。最新の月に行の無い集まりは0人として
        # 後ろへ回る（部署・チームの行は1人以上の月だけあるため）
        names = [name for (s, name) in groups if s is scope]
        return sorted(names, key=lambda n: (
            -int(groups[(scope, n)].get(latest, {}).get("seat_holders", "0")), n))

    sections = []
    for scope, title in _GROUP_SECTIONS:
        names = ordered(scope)
        if not names:
            continue
        overview = [(_ORG_ANCHOR, "組織全体", org_series)]
        overview += [(f"g-{scope.value}-{i}", name, groups[(scope, name)])
                     for i, name in enumerate(names)]
        cards = []
        for i, name in enumerate(names):
            by_month = groups[(scope, name)]
            last_month = max(by_month)
            cards.append({
                "anchor": f"g-{scope.value}-{i}",
                "name": name,
                "sub": f"{last_month} 時点 {by_month[last_month]['seat_holders']} 人",
                "table": _metric_table(months, by_month, metrics, partial),
            })
        sections.append({
            "id": scope.value,
            "title": title,
            "overview": _overview_table(title, months, overview, metrics,
                                        len(months) >= _TREND3_MONTHS, partial),
            "cards": cards,
        })

    org_last = org_series[latest]
    tile_views = []
    for tile in _TILES:
        traits = tile.traits
        if traits.github and not has_github:
            continue
        values = org_values(tile.column)
        value = (_DASH if values[-1] is None
                 else tile.fmt(values[-1], org_last) + partial.mark(latest, traits.partial))
        change = _delta(values, tile.kind, partial.flags(months, traits.partial, traits.span))
        tile_views.append({
            "label": tile.label, "value": value,
            "delta": change if change == _DASH else change + tile.unit,
            "sub": f"{latest}{tile.sub}",
        })

    charts = []
    for chart in _CHARTS:
        traits = chart.traits
        if traits.shown(has_github, has_otel):
            values = _series(chart.value, months, org_series)
            marks = [partial.mark(m, traits.partial) for m in months]
            charts.append({
                "title": chart.title, "sub": chart.sub,
                "chart": _line_chart(months, values, chart.fmt, chart.prefix, chart.whole, marks),
            })

    people_by_email: dict[str, dict[str, Row]] = {}
    for row in person_rows:
        people_by_email.setdefault(row.email, {})[row.month] = _person_cells(row)

    scatter = None
    if has_github:
        # 最新の月にマージ行の値がある人（メールの順）。点はさらに横軸の値もある人だけ
        latest_rows = [people_by_email[row.email][latest] for row in person_rows
                       if row.month == latest]
        latest_rows = [cells for cells in latest_rows if cells[_SCATTER_Y_COLUMN] != ""]
        # 見出しの月の印の条件は、2つの図が使う列の条件の和
        scatter_kinds = _traits(
            [_SCATTER_Y_COLUMN, *(column for _, _, column, _, _ in _SCATTERS)]).partial
        scatter = {
            "heading": f"使った量と GitHub の成果（{latest}{partial.mark(latest, scatter_kinds)}・"
                       "人ごと・参考）",
            "charts": [{
                "title": title, "sub": sub,
                "chart": _scatter([(_local_part(cells["email"]), _num(cells[column]) or 0.0,
                                    _num(cells[_SCATTER_Y_COLUMN]) or 0.0)
                                   for cells in latest_rows if cells[column] != ""],
                                  x_name, _SCATTER_Y_NAME, prefix),
            } for title, sub, column, x_name, prefix in _SCATTERS],
        }

    nav = [{"href": "#org", "label": "組織全体"}]
    nav += [{"href": f"#{section['id']}", "label": f"{section['title']}ごと"}
            for section in sections]
    nav.append({"href": "#people", "label": "人ごと（参考）"})
    if has_github:
        nav.append({"href": "#scatter", "label": "使った量と成果（参考）"})
    nav.append({"href": "#about", "label": "指標の読み方"})

    return {
        "org": org,
        "first": months[0],
        "latest": latest,
        "has_github": has_github,
        "has_otel": has_otel,
        "missing": "・".join(missing),
        "partial_spend": bool(partial.spend),
        "partial_github": bool(partial.github),
        "nav": nav,
        "tiles": tile_views,
        "charts": charts,
        "org_anchor": _ORG_ANCHOR,
        "org_table": _metric_table(months, org_series, metrics, partial),
        "sections": sections,
        "people": _people_table(people_by_email, months, frozenset(org_rows), has_github,
                                partial),
        "scatter": scatter,
    }


# 埋め込む CSS（コメントを落としてから。実体ファイル側の説明は出力に持ち込まない）
_ADOPTION_CSS = strip_css_comments(_asset("adoption-report.css"))


def _join_lines(source: str) -> str:
    """テンプレートの各行の前後の空白を落として、改行なしでつなぐ。

    テンプレートは読みやすさのため1要素1行で書くが、行の間の改行とインデントを出力に
    持ち込むと、隣り合う要素の文字の間に空白が入る。行の中の空白は変えない（テンプレートは
    タグや文の途中で改行しない）。入力由来の値には掛からない（描画の前のソースに掛ける）。
    """
    return "".join(line.strip() for line in source.splitlines())


_ADOPTION_TEMPLATE = _HTML_ENV.from_string(_join_lines(_asset("adoption-report.html.j2")))


def write_adoption_report(
    people: Iterable[PersonMonth], teams: Iterable[GroupMonth], path: Path, *, org: str
) -> None:
    """adoption-report.html を書く（人 × 月の表とチーム × 月の表の行から）。

    入力は2つの表の行だけ（分析結果・設定・キャッシュを受け取らない）で、受け取った順に
    依らず同じ行の集合から同じバイト列を書く。PersonMonth・GroupMonth でない行は
    TypeError、組織全体の行が1つも無いときと、同じ鍵の行が2つあるときは ValueError
    （どちらの値が正か決まらないため）。org は見出しに書く組織名。
    """
    html = _ADOPTION_TEMPLATE.render(css=_ADOPTION_CSS, v=_report_view(people, teams, org))
    path.write_text(html, encoding="utf-8", newline="\n")
