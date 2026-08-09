"""HTML 断片（templates/partials/*.j2）の条件分岐を両側から固定するテスト。

tests/test_golden.py は生成物を丸ごと固定するが、examples/input の合成データが
満たさない条件の分岐は一度も描画されないため、その中身は golden では守られない。
このテストは断片を本番と同じ経路（_HTML_ENV + _embed_shared_text）で直接
レンダリングし、そうした分岐について「描画される文脈」と「描画されない文脈」の
両方を固定する。

各ケースは分岐の両側を assert する（片側だけだと、条件を無視して常に描画する
変更を検出できない）。文言はテンプレートに書かれているものを直接書く（_TEXT から
引いて突き合わせると両方が同時に変わったとき素通りするため）。context は
analyze が返す生データを組み立ててビューモデル生成関数に通し、断片と整形の
両方が同時に守られるようにしている。

断片に新しい分岐を足したときは、ここにもケースを足すこと。
"""

from seat_analyzer.report.html import (
    _CODE_DIFF_HTML,
    _E_DIST_HTML,
    _HTML_ENV,
    _MEMBER_CHANGES_HTML,
    _SNAPSHOT_HTML,
    _TREND_HTML,
    _code_diff_view,
    _e_distribution_view,
    _member_changes_view,
    _snapshot_view,
    _trend_view,
)
from seat_analyzer.report.text import _embed_shared_text


def _render(src: str, **ctx) -> str:
    """断片を本番と同じ経路でレンダリングする（<!--text:キー--> も解決する）。"""
    return _HTML_ENV.from_string(_embed_shared_text(src)).render(**ctx)


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


def _code_diff(has_prs: bool, prs_delta: int | None) -> dict:
    """_compute_code_diff が返す形（1ユーザ・2時点）。"""
    return {
        "labels": ["〜07-15", "〜07-31"],
        "has_prs": has_prs,
        "rows": [{"email": "a@x.jp", "loc_cum": [1200, 1900], "loc_delta": 700,
                  "prs_delta": prs_delta}],
    }


def _e_dist() -> dict:
    """_compute_e_distribution が返す形（Standard 1名）。"""
    return {"groups": [{
        "seat": "standard", "count": 1,
        "median": 50.0, "min": 50.0, "max": 50.0,
        "allowance_mid": 25.0, "ratio": 2.0,
        "rows": [{"email": "a@x.jp", "demand": 80.0, "billed": 30.0, "e": 50.0}],
    }]}


# --- trend.html.j2 ---

def test_trend_section_absent_without_trend():
    """比較できる前月が無い初月は「前月からの変化」セクションを一切出さない。"""
    assert "前月からの変化" not in _render(_TREND_HTML, trend=_trend_view(None))
    assert "<h2>前月からの変化</h2>" in _render(_TREND_HTML, trend=_trend_view(_trend()))


def test_trend_gap_skipped_note_only_when_month_missing():
    """直前月が欠測で1つ前の存在月と比べたときだけ、その断りを添える。"""
    note = "（直前月が欠測のため直前の存在月と比較）"
    skipped = _render(_TREND_HTML, trend=_trend_view(_trend(gap_skipped=True)))
    normal = _render(_TREND_HTML, trend=_trend_view(_trend(gap_skipped=False)))
    assert f"比較対象: 2026-06{note}</p>" in skipped
    assert "比較対象: 2026-06</p>" in normal
    assert note not in normal


def test_trend_stopped_lists_people_when_present():
    """利用停止は該当者がいれば名前と金額を並べ、いなければ「なし」と書く。"""
    with_people = _render(_TREND_HTML, trend=_trend_view(
        _trend(stopped=[_person("gone@x.jp", 120.0)])))
    without = _render(_TREND_HTML, trend=_trend_view(_trend()))
    assert "利用停止 1 名: gone@x.jp（$120）</li>" in with_people
    assert "利用停止 0 名: なし</li>" in without
    assert "gone@x.jp" not in without


def test_trend_new_billed_says_none_when_empty():
    """実課金の新規発生は該当者ゼロでも項目を残し「なし」と書く。"""
    empty = _render(_TREND_HTML, trend=_trend_view(_trend()))
    present = _render(_TREND_HTML, trend=_trend_view(
        _trend(new_billed=[_person("bill@x.jp", 40.0)])))
    assert "実課金の新規発生 0 名: なし</li>" in empty
    assert "実課金の新規発生 1 名: bill@x.jp（$40.00）</li>" in present


def test_trend_multiple_people_are_comma_separated():
    """同じ項目に複数名いるときだけ区切りを入れ、最後の1人の後ろには付けない。"""
    two = [_person("a@x.jp", 120.0), _person("b@x.jp", 110.0)]
    html = _render(_TREND_HTML, trend=_trend_view(
        _trend(started=two, stopped=two, new_billed=two)))
    assert "a@x.jp（$120）, b@x.jp（$110）</li>" in html
    assert html.count("）, ") == 3      # 利用開始・利用停止・実課金の新規発生で各1箇所
    assert "）, </li>" not in html      # 末尾に区切りを残さない


# --- snapshot.html.j2 ---

def test_snapshot_short_interval_note_only_when_unjudged():
    """最新区間が短く停止判定を保留したときだけ、その旨を注記する。"""
    note = "日と短いため停止判定は行っていません"
    unjudged = _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(
        _snapshot(judged=False, latest_interval_days=3)))
    judged = _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot(judged=True)))
    assert f"<p>最新区間が 3 {note}。</p>" in unjudged
    assert note not in judged


def test_snapshot_supplement_block_only_when_findings():
    """上限停止も課金発生も無ければ、補足の枠自体を出さない。"""
    box = '<div class="card note"><ul>'
    none = _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot()))
    with_stall = _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot(
        stalled_capped=[{"email": "cap@x.jp", "cum_at_stall": 210.0, "loc_note": ""}])))
    with_billed = _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot(
        billed_emerged=[{"email": "bill@x.jp", "interval_label": "〜07-15→〜07-31",
                         "prev_cum": 180.0, "curr_cum": 260.0, "billed": 12.0}])))
    assert box not in none
    assert box in with_stall and box in with_billed
    assert ("<li>cap@x.jp: 上限停止の可能性。停止時点の累積 $210 は実効込み量の実測候補。"
            "</li>") in with_stall
    assert ("<li>bill@x.jp: 〜07-15→〜07-31 で従量課金 $12.00 が発生"
            "（実効込み量は累積需要 $180〜$260 の間）。</li>") in with_billed


def test_snapshot_loc_note_only_when_corroborated():
    """LoC の傍証がある上限停止のときだけ、本文にその一文を足す。"""
    def _render_with(loc_note: str) -> str:
        return _render(_SNAPSHOT_HTML, snapshot=_snapshot_view(_snapshot(
            stalled_capped=[{"email": "cap@x.jp", "cum_at_stall": 210.0,
                             "loc_note": loc_note}])))

    with_note = _render_with("同期間の LoC は増加しており活動は継続")
    without = _render_with("")
    assert "実測候補。同期間の LoC は増加しており活動は継続。</li>" in with_note
    assert "実測候補。</li>" in without


# --- member-changes.html.j2 ---

def test_member_changes_empty_says_no_change():
    """スナップショットを取って変動が無かった月は「変動なし」と明示する。"""
    empty = _render(_MEMBER_CHANGES_HTML,
                    member_changes=_member_changes_view(_member_changes()))
    changed = _render(_MEMBER_CHANGES_HTML, member_changes=_member_changes_view(
        _member_changes(empty=False, joined=[
            {"email": "new@x.jp", "seat": "standard", "interval_label": "07-05→07-16"}])))
    assert "<p>変動なし</p>" in empty
    assert "<ul>" not in empty
    assert "<p>変動なし</p>" not in changed
    assert "<ul>" in changed


def test_member_changes_lists_removed_members():
    """メンバー削除も追加と同じ粒度で列挙する。"""
    interval = "07-05→07-16"
    removed = _render(_MEMBER_CHANGES_HTML, member_changes=_member_changes_view(
        _member_changes(empty=False, left=[
            {"email": "gone@x.jp", "seat": "premium", "interval_label": interval}])))
    added = _render(_MEMBER_CHANGES_HTML, member_changes=_member_changes_view(
        _member_changes(empty=False, joined=[
            {"email": "new@x.jp", "seat": "premium", "interval_label": interval}])))
    assert f"<li>gone@x.jp: {interval} で削除（Premium）</li>" in removed
    assert f"<li>new@x.jp: {interval} で追加（Premium）</li>" in added
    assert "で削除（" not in added
    assert "で追加（" not in removed


def test_member_changes_credit_note_only_when_limit_changed():
    """追加クレジット上限の変更があった月だけ、判定を翌月から行う旨を注記する。"""
    note = "上限に基づく判定は翌月から行ってください"
    interval = "07-05→07-16"
    changed = _render(_MEMBER_CHANGES_HTML, member_changes=_member_changes_view(
        _member_changes(empty=False, credit_changes=[
            {"email": "c@x.jp", "interval_label": interval, "from": "無効", "to": "$150"}])))
    other = _render(_MEMBER_CHANGES_HTML, member_changes=_member_changes_view(
        _member_changes(empty=False, joined=[
            {"email": "new@x.jp", "seat": "standard", "interval_label": interval}])))
    assert (f"<li>c@x.jp: {interval} で 追加クレジット上限 無効 → $150"
            "（members-info 由来）</li>") in changed
    assert f"{note}。</p>" in changed
    assert note not in other


# --- code-diff.html.j2 ---

def test_code_diff_pr_column_only_when_prs_available():
    """PR 数を持つ code-analytics のときだけ PR 列を足す（見出しとセルの両方）。"""
    header = '<th class="num">PR 増分</th>'
    with_prs = _render(_CODE_DIFF_HTML,
                       code_diff=_code_diff_view(_code_diff(True, 3)))
    without = _render(_CODE_DIFF_HTML,
                      code_diff=_code_diff_view(_code_diff(False, None)))
    assert header in with_prs
    assert header not in without
    assert '<td class="num">+3</td>' in with_prs
    # 列が1つ増えるのは見出しだけでなく行のセルも（列数のズレを検出する）
    assert with_prs.count("<td ") == without.count("<td ") + 1
    assert with_prs.count("<th ") == without.count("<th ") + 1


# --- e-dist.html.j2 ---

def test_e_distribution_section_absent_without_billers():
    """実課金が1人も発生していない組織では E 分布のセクションを出さない。"""
    heading = "<h2>込み枠の実測（E = API換算需要 − 実課金）</h2>"
    assert heading not in _render(_E_DIST_HTML,
                                  e_distribution=_e_distribution_view(None))
    assert heading in _render(_E_DIST_HTML,
                              e_distribution=_e_distribution_view(_e_dist()))
