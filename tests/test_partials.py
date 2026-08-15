"""HTML 断片（templates/partials/*.j2）の条件分岐を両側から固定するテスト。

tests/test_golden.py は生成物を丸ごと固定するが、examples/input の合成データが
満たさない条件の分岐は一度も描画されないため、その中身は golden では守られない。
このテストは断片を本番と同じ経路（_HTML_ENV + _embed_shared_text）で直接
レンダリングし、そうした分岐について「描画される文脈」と「描画されない文脈」の
両方を固定する。

固定の仕方は分岐の性質で2通りに分かれる。どちらも「分岐が要素を1つ増やす」
変更と「分岐が何も出さなくなる」変更の両方が落ちる形になっている:

- 描画される側が描画されない側に要素を足すだけの分岐は、足された分を取り除いた
  結果が描画されない側と完全一致することを検査する（`_without(a, added) == b`）。
  素の `a.replace(added, "") == b` は使わない。分岐が何も出さなくなると除去が
  空振りして両辺が一致し、同じ出力が重複しても全部消えて一致するため、
  `_without` が除去の前に出現回数を1と検査する
- 両側が排他で足し算にならない分岐（「変動なし」対「一覧」、各リストの
  {% else %}: なし）は、両側とも要素の全文で一致を検査する

比較する2つのレンダリングは分岐条件以外のデータを揃える。その分岐の中でしか
使われない値だけは揃えなくてよく、その場合はテスト内にその旨を1行残す。

文言はテンプレートに書かれているものを直接書く（_TEXT から引いて突き合わせると
両方が同時に変わったとき素通りするため）。context は analyze が返す生データを
組み立ててビューモデル生成関数に通し、断片と整形の両方が同時に守られるように
している。

断片に新しい分岐を足したときは、ここにもケースを足すこと。
"""

import pandas as pd

from seat_analyzer.product_usage import ProductUsage
from seat_analyzer.report.html import (
    _CODE_DIFF_HTML,
    _HTML_ENV,
    _MEMBER_CHANGES_HTML,
    _PRODUCT_HTML,
    _SNAPSHOT_HTML,
    _STATS_HTML,
    _TREND_HTML,
    _code_diff_view,
    _member_changes_view,
    _product_view,
    _snapshot_view,
    _stats_view,
    _trend_view,
)
from seat_analyzer.report.stats import Distribution
from seat_analyzer.report.text import _embed_shared_text


def _render(src: str, **ctx) -> str:
    """断片を本番と同じ経路でレンダリングする（<!--text:キー--> も解決する）。"""
    return _HTML_ENV.from_string(_embed_shared_text(src)).render(**ctx)


def _without(html: str, *blocks: str) -> str:
    """描画された側から分岐の出力を取り除く。

    各ブロックがちょうど1回描画されたことも同時に固定する。これが無いと、分岐が
    何も出さなくなったときに除去が空振りして両辺が一致し、テストが通ってしまう。
    """
    for b in blocks:
        n = html.count(b)
        assert n == 1, f"1回だけ描画されるはずが {n} 回: {b!r}"
        html = html.replace(b, "", 1)
    return html


# --- 生データの組み立て（analyze 側の計算結果と同じ形。ビューモデル生成関数に通す） ---

def _trend(**over) -> dict:
    """_compute_trend が返す形。既定は「増減も開始/停止も無い最小の月」。"""
    return {
        "compare_month": "2026-06",
        "gap_skipped": False,
        "started": [],
        "stopped": [],
        "new_billed": [],
        "changes": [],
        "series": [{"month": "2026-06", "api": 100.0, "billed": 0.0, "active": 1}],
        **over,
    }


def _person(email: str, amount: float) -> dict:
    return {"email": email, "amount": amount}


def _trend_html(**over) -> str:
    return _render(_TREND_HTML, trend=_trend_view(_trend(**over)))


def _snapshot(**over) -> dict:
    """_compute_snapshot_diff が返す形。既定は「2時点・停止も課金発生も無し」。"""
    return {
        "labels": ["〜07-15", "〜07-31"],
        "snaps": [{"label": "〜07-15", "days": 15}, {"label": "〜07-31", "days": 31}],
        "latest_interval_days": 16,
        "judged": True,
        "rows": [{"email": "a@x.jp", "cum": [80.0, 130.0], "latest_delta": 50.0,
                  "stall": False}],
        "stalled_capped": [],
        "billed_emerged": [],
        **over,
    }


def _snapshot_html(**over) -> str:
    return _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot(**over)))


def _member_changes(**over) -> dict:
    """_compute_member_changes が返す形。既定は「2時点・変動なし」。"""
    return {
        "labels": ["07-05", "07-16"],
        "empty": True,
        "seat_changes": [],
        "joined": [],
        "left": [],
        "credit_changes": [],
        **over,
    }


def _member_changes_html(**over) -> str:
    return _render(_MEMBER_CHANGES_HTML,
                   member_changes=_member_changes_view(_member_changes(**over)))


def _code_diff(has_prs: bool, prs_delta: int | None) -> dict:
    """_compute_code_diff が返す形（1ユーザ・2時点）。"""
    return {
        "labels": ["〜07-15", "〜07-31"],
        "has_prs": has_prs,
        "rows": [{"email": "a@x.jp", "loc_cum": [1200, 1900], "loc_delta": 700,
                  "prs_delta": prs_delta}],
    }


# --- trend.html.j2 ---

def test_trend_section_absent_without_trend():
    """比較できる前月が無い初月は「前月からの変化」セクションを一切出さない。"""
    # 片側は trend が無い（描画すると Undefined で落ちる）ため差分の一致は取れない
    assert "前月からの変化" not in _render(_TREND_HTML, trend=_trend_view(None))
    assert "<h2>前月からの変化</h2>" in _trend_html()


def test_trend_gap_skipped_note_only_when_month_missing():
    """直前月が欠測で1つ前の存在月と比べたときだけ、その断りを添える。"""
    note = "（直前月が欠測のため直前の存在月と比較）"
    skipped = _trend_html(gap_skipped=True)
    normal = _trend_html(gap_skipped=False)
    assert f"比較対象: 2026-06{note}</p>" in skipped
    assert _without(skipped, note) == normal        # 足すのはこの一文だけ


def test_trend_stopped_lists_people_when_present():
    """利用停止は該当者がいれば名前と金額を並べ、いなければ「なし」と書く。"""
    with_people = _trend_html(stopped=[_person("gone@x.jp", 120.0)])
    without = _trend_html()
    # 一覧と「なし」は排他なので、対象の <li> を両側とも全文で固定する
    assert "<li>利用停止 1 名: gone@x.jp（$120）</li>" in with_people
    assert "<li>利用停止 0 名: なし</li>" in without
    assert "gone@x.jp" not in without


def test_trend_new_billed_says_none_when_empty():
    """実課金の新規発生は該当者ゼロでも項目を残し「なし」と書く。"""
    empty = _trend_html()
    present = _trend_html(new_billed=[_person("bill@x.jp", 40.0)])
    assert "<li>実課金の新規発生 0 名: なし</li>" in empty
    assert "<li>実課金の新規発生 1 名: bill@x.jp（$40.00）</li>" in present


def test_trend_multiple_people_are_comma_separated():
    """同じ項目に複数名いるときだけ区切りを入れ、最後の1人の後ろには付けない。

    3項目それぞれに別のデータを渡し、<li> ごとに全文で照合する（同じデータを
    使い回すと1項目だけが壊れても他の項目が assert を満たしてしまう）。
    """
    html = _trend_html(
        started=[_person("st-1@x.jp", 120.0), _person("st-2@x.jp", 110.0)],
        stopped=[_person("sp-1@x.jp", 220.0), _person("sp-2@x.jp", 210.0)],
        new_billed=[_person("nb-1@x.jp", 40.50), _person("nb-2@x.jp", 30.25)],
    )
    # 3項目を「なし」の形へ1つずつ戻すと該当者ゼロのレンダリングと完全一致する
    # （各項目が1回だけ描画され、余計な <li> が増えていないことまで固定する）
    restored = html
    for li, none_li in (
        ("<li>利用開始 2 名: st-1@x.jp（$120）, st-2@x.jp（$110）</li>",
         "<li>利用開始 0 名: なし</li>"),
        ("<li>利用停止 2 名: sp-1@x.jp（$220）, sp-2@x.jp（$210）</li>",
         "<li>利用停止 0 名: なし</li>"),
        ("<li>実課金の新規発生 2 名: nb-1@x.jp（$40.50）, nb-2@x.jp（$30.25）</li>",
         "<li>実課金の新規発生 0 名: なし</li>"),
    ):
        assert restored.count(li) == 1
        restored = restored.replace(li, none_li, 1)
    assert restored == _trend_html()


# --- snapshot.html.j2 ---

def test_snapshot_short_interval_note_only_when_unjudged():
    """最新区間が短く停止判定を保留したときだけ、その旨を注記する。"""
    # latest_interval_days はこの注記の中でしか使われないため両側で揃えなくてよい
    unjudged = _snapshot_html(judged=False, latest_interval_days=3)
    judged = _snapshot_html(judged=True)
    note = "<p>最新区間が 3 日と短いため停止判定は行っていません。</p>"
    assert _without(unjudged, note) == judged       # 足すのはこの1要素だけ


def test_snapshot_supplement_block_only_when_findings():
    """上限停止も課金発生も無ければ、補足の枠自体を出さない。"""
    # stalled_capped / billed_emerged はこの枠の中でしか使われない
    none = _snapshot_html()
    with_stall = _snapshot_html(
        stalled_capped=[{"email": "cap@x.jp", "cum_at_stall": 210.0, "loc_note": ""}])
    with_billed = _snapshot_html(
        billed_emerged=[{"email": "bill@x.jp", "interval_label": "〜07-15→〜07-31",
                         "prev_cum": 180.0, "curr_cum": 260.0, "billed": 12.0}])
    stall_box = (
        '<div class="card note"><ul>\n'
        "<li>cap@x.jp: 上限停止の可能性。停止時点の累積 $210 は実効込み量の実測候補。</li>\n"
        "\n</ul></div>\n\n"
    )
    billed_box = (
        '<div class="card note"><ul>\n\n'
        "<li>bill@x.jp: 〜07-15→〜07-31 で従量課金 $12.00 が発生"
        "（実効込み量は累積需要 $180〜$260 の間）。</li>\n"
        "</ul></div>\n\n"
    )
    assert '<div class="card note"><ul>' not in none
    assert _without(with_stall, stall_box) == none
    assert _without(with_billed, billed_box) == none


def test_snapshot_loc_note_only_when_corroborated():
    """LoC の傍証がある上限停止のときだけ、本文にその一文を足す。"""
    def _with(loc_note: str) -> str:
        # loc_note はこの一文の中でしか使われない
        return _snapshot_html(stalled_capped=[
            {"email": "cap@x.jp", "cum_at_stall": 210.0, "loc_note": loc_note}])

    added = "。同期間の LoC は増加しており活動は継続"
    with_note = _with("同期間の LoC は増加しており活動は継続")
    without = _with("")
    assert f"実測候補{added}。</li>" in with_note
    assert "実測候補。</li>" in without
    assert _without(with_note, added) == without        # 足すのはこの一文だけ


# --- member-changes.html.j2 ---

def test_member_changes_empty_says_no_change():
    """スナップショットを取って変動が無かった月は「変動なし」と明示する。"""
    empty = _member_changes_html()                    # empty=True
    listed = _member_changes_html(empty=False)        # 一覧の枠のみ（各ループとも空）
    head = ("\n\n<h2>月中のメンバー変動（スナップショット差分）</h2>\n"
            '<div class="card note">\n'
            "  <p>スナップショット時点: 07-05 / 07-16</p>\n")
    # 「変動なし」と一覧は排他なので、両側とも全文で固定する。
    # 一覧側の空行4本はシート変更・追加・削除・上限変更の各ループの跡。
    assert empty == head + "  <p>変動なし</p>\n</div>\n"
    assert listed == head + "  <ul>\n  \n  \n  \n  \n  </ul>\n</div>\n"


def test_member_changes_lists_removed_members():
    """メンバー削除も追加と同じ粒度で列挙する。"""
    interval = "07-05→07-16"
    base = _member_changes_html(empty=False)          # 一覧の枠のみ（各ループとも空）
    removed = _member_changes_html(empty=False, left=[
        {"email": "gone@x.jp", "seat": "premium", "interval_label": interval}])
    added = _member_changes_html(empty=False, joined=[
        {"email": "new@x.jp", "seat": "premium", "interval_label": interval}])
    li_removed = f"<li>gone@x.jp: {interval} で削除（Premium）</li>"
    li_added = f"<li>new@x.jp: {interval} で追加（Premium）</li>"
    assert _without(removed, li_removed) == base      # 足すのはこの1行だけ
    assert _without(added, li_added) == base


def test_member_changes_credit_note_only_when_limit_changed():
    """追加クレジット上限の変更があった月だけ、判定を翌月から行う旨を注記する。"""
    interval = "07-05→07-16"
    base = _member_changes_html(empty=False)          # 一覧の枠のみ（各ループとも空）
    changed = _member_changes_html(empty=False, credit_changes=[
        {"email": "c@x.jp", "interval_label": interval, "from": "無効", "to": "$150"}])
    li = (f"<li>c@x.jp: {interval} で 追加クレジット上限 無効 → $150"
          "（members-info 由来）</li>")
    note = ("<p>追加クレジット上限を変更した月の課金は部分月のため、"
            "上限に基づく判定は翌月から行ってください。</p>")
    assert note not in base
    assert _without(changed, li, note) == base        # 足すのはこの2要素だけ


# --- code-diff.html.j2 ---

def test_code_diff_pr_column_only_when_prs_available():
    """PR 数を持つ code-analytics のときだけ PR 列を足す（見出しとセルの両方）。"""
    header = '<th class="num">PR 増分</th>'
    cell = '<td class="num">+3</td>'
    with_prs = _render(_CODE_DIFF_HTML, code_diff=_code_diff_view(_code_diff(True, 3)))
    without = _render(_CODE_DIFF_HTML, code_diff=_code_diff_view(_code_diff(False, None)))
    assert header not in without
    # 足すのは見出し1つとセル1つだけ（列数のズレも余計な要素の追加も検出する）
    assert _without(with_prs, header, cell) == without


# --- stats.html.j2 ---

def _distribution(**over) -> Distribution:
    """distributions() が返す形（1指標分）。"""
    return Distribution(**{
        "key": "api_cost", "label": "API換算需要", "kind": "usd", "n": 3,
        "mean": 120.0, "median": 40.0, "std": 90.0,
        "p25": 20.0, "p75": 180.0, "p90": 240.0, "maximum": 300.0,
        **over,
    })


def test_stats_section_absent_when_population_is_empty():
    """母集団がいない（未割当しかいない）組織では分布のセクションを出さない。"""
    # 片側は行が1つも無いため差分の一致は取れない（表も注記も丸ごと出ない）
    empty = _render(_STATS_HTML, stats=_stats_view([]))
    assert empty.strip() == ""
    assert "<h2>組織内の分布（参考値）</h2>" in _render(
        _STATS_HTML, stats=_stats_view([_distribution()]))


def test_stats_row_is_added_per_metric():
    """指標が1つ増えると行が1つだけ増える（n も統計量もその指標のものを出す）。"""
    one = _render(_STATS_HTML, stats=_stats_view([_distribution()]))
    two = _render(_STATS_HTML, stats=_stats_view([
        _distribution(),
        _distribution(key="loc", label="LoC", kind="count", n=2, mean=1500.0,
                      median=1500.0, std=500.0, p25=1250.0, p75=1750.0, p90=1900.0,
                      maximum=2000.0),
    ]))
    row = ('\n<tr><td>LoC</td><td class="num">2</td><td class="num">1,500</td>'
           '<td class="num">1,500</td><td class="num">500</td><td class="num">1,250</td>'
           '<td class="num">1,750</td><td class="num">1,900</td>'
           '<td class="num">2,000</td></tr>\n')
    assert _without(two, row) == one          # 足すのはこの1行だけ


def test_stats_money_and_count_use_different_formats():
    """金額はダッシュボードの短縮金額、個数は K/M の短縮表記で出す。"""
    html = _render(_STATS_HTML, stats=_stats_view([
        _distribution(),
        _distribution(key="input", label="input", kind="count", mean=1_234_567.0),
    ]))
    assert '<td class="num">$120</td>' in html      # usd: $100 以上は整数表示
    assert '<td class="num">1.23M</td>' in html     # count: 1e6 以上は M・小数2桁


# --- product.html.j2 ---

# features の dtype。compute の出力と同じであることは
# tests/test_product_section.py の test_feature_dtypes_match_compute が守る
_FEATURE_DTYPES = {
    "total_demand_usd": "Float64",
    "code_demand_usd": "Float64",
    "code_demand_share": "Float64",
    "total_requests": "Float64",
    "code_requests": "Float64",
    "product_breadth": "Int64",
    "supplementary_high": "boolean",
    "prohibited_observed": "boolean",
}

# ⚑ の凡例に出る閾値。この断片の分岐には関与しない
_THRESHOLD_USD = 100.0


def _row(**over) -> dict:
    """compute が返す features の1行分。既定は8特徴量すべてが確定した行。"""
    return {"total_demand_usd": 400.0, "code_demand_usd": 300.0,
            "code_demand_share": 0.75, "total_requests": 100.0, "code_requests": 80.0,
            "product_breadth": 2, "supplementary_high": False,
            "prohibited_observed": False, **over}


def _product_html(*, extra: dict | None = None, **over) -> str:
    """a@x.jp 1人分の断片。extra を渡すと2人目（z@x.jp）を足す。

    Code の需要が1人も確定しないとセクションごと消えるため、確定しない行を見る
    ケースでは extra に確定した行を足して、セクションが出る文脈を作る。
    """
    people = {"a@x.jp": _row(**over)}
    if extra is not None:
        people["z@x.jp"] = _row(**extra)
    features = pd.DataFrame(
        list(people.values()), index=pd.Index(list(people), name="email"),
    ).astype(_FEATURE_DTYPES)
    usage = ProductUsage(features=features, issues=[])
    return _render(_PRODUCT_HTML, product=_product_view(usage, _THRESHOLD_USD))


def test_product_section_absent_without_confirmed_code_demand():
    """Code の需要が1人も確定しない組織ではセクションを一切出さない。"""
    # 片側は product が無いため差分の一致は取れない（見出しも表も丸ごと出ない）
    assert _render(_PRODUCT_HTML, product=None).strip() == ""
    assert "<h2>Codeと他プロダクトの需要（API換算）</h2>" in _product_html()


def test_product_summary_line_only_when_amounts_are_confirmed():
    """合計できる行が1つも無い月は、組織サマリの1行ごと出さない。"""
    # サマリ行の有無は行データそのもので決まり、他の要素も同時に変わるため、
    # 差分の一致ではなく両側を全文（と枠の不在）で固定する
    confirmed = _product_html()
    unconfirmed = _product_html(total_demand_usd=pd.NA, code_demand_share=pd.NA)
    assert ('<div class="card note"><p>Code需要 $300 / 全需要 $400（75%）・対象 1名'
            "</p></div>") in confirmed
    assert '<div class="card note">' not in unconfirmed


def test_product_bar_shape_follows_what_is_known():
    """内訳が分かれば2色、需要だけなら斜線、需要も不明なら棒を描かない。"""
    # 3通りは排他なので、track の中身を両側とも全文で固定する
    split = _product_html()
    hatched = _product_html(extra={}, code_demand_usd=pd.NA, code_demand_share=pd.NA)
    nothing = _product_html(extra={}, total_demand_usd=pd.NA, code_demand_share=pd.NA)
    assert ('<div class="track"><div class="fill" style="width: 100.0%; background: '
            "linear-gradient(to right, var(--ok) 75.0%, #9aa3ad 75.0%);\"></div></div>"
            ) in split
    assert ('<div class="track"><div class="fill" style="width: 100.0%; background: '
            "repeating-linear-gradient(45deg, #b9c0c8 0, #b9c0c8 4px, #dfe3e8 4px, "
            "#dfe3e8 8px);\"></div></div>") in hatched
    assert '<div class="track"></div>' in nothing


def test_product_flag_marks_only_confirmed_supplementary_high():
    """⚑ は真のときだけ他product需要のセルに付く（偽と欠損はどちらも無印）。"""
    flagged = _product_html(supplementary_high=True)
    plain = _product_html(supplementary_high=False)
    unknown = _product_html(supplementary_high=pd.NA)
    mark = ' <span class="cap">⚑</span>'
    assert _without(flagged, mark) == plain     # 足すのはこの印だけ
    assert unknown == plain
