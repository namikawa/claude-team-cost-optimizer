"""人（email）の層と、副スペースの払い出し・継続の参考判定（設計書 §26.4〜§26.5）。

デフォルト設定: Standard $25 / Premium $125、hysteresis=2ヶ月、trend.idle_usd=1.0、
usage_credits.cap_tolerance_usd=5.0。副の損益分岐は既定で副の fixed_seat の価格
（premium なら $125）。
"""

from pathlib import Path

import pandas as pd
import yaml

from seat_analyzer.analyze import (
    CONTINUATION_WATCH,
    CONTINUE,
    PAYOUT_CANDIDATE,
    PAYOUT_NO_EVIDENCE,
    PAYOUT_UNNEEDED,
    PAYOUT_WATCH,
    RETURN_CANDIDATE,
    STATUS_CHANGE,
    WAITING,
    analyze_org,
)
from seat_analyzer.config import load_config
from seat_analyzer.report.format import _group_summary_rows
from tests.conftest import spend_row

ORG = "org-x"


def _cfg(tmp_path: Path, workspaces: dict, **org_keys) -> dict:
    """組織の workspaces を書いた上書き設定をロードする（既定設定に重ねる）。"""
    path = tmp_path / "config-persons.yaml"
    path.write_text(
        yaml.safe_dump(
            {"organizations": {ORG: {"workspaces": workspaces, **org_keys}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return load_config(str(path))


def _write_members_info(input_dir: Path, text: str) -> None:
    """組織直下の members-info.csv（人単位の任意入力）を置く。"""
    (input_dir / ORG / "members-info.csv").write_text(text, encoding="utf-8")


def _write_code_analytics(base: Path, month: str, rows: list[tuple[str, int]]) -> None:
    """workspace の code-analytics（月のみの命名）を置く。"""
    directory = base / "code-analytics"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"cc_{month}.csv").write_text(
        "Email,Lines with CC\n"
        + "\n".join(f"{email},{loc}" for email, loc in rows) + "\n",
        encoding="utf-8",
    )


def _payout_input(make_input) -> Path:
    """主だけにアカウントを持つ人が、払い出し判定の全ステータスに散る構成。

    主の実課金は「副を足さずクレジットへ払っている額」なので、副の損益分岐
    （$125）との比較と、追加クレジット上限の有効・無効・不明で判定が分かれる。
    """
    def rows(carol_billed: float) -> list[str]:
        return [
            spend_row("alice@example.com", 300.0, net=125.0),
            spend_row("bob@example.com", 300.0, net=124.99),
            spend_row("carol@example.com", 300.0, net=carol_billed),
            spend_row("dave@example.com", 300.0, net=0.0),
            spend_row("erin@example.com", 300.0, net=0.0),
            spend_row("frank@example.com", 300.0, net=0.0),
            spend_row("grace@example.com", 300.0, net=0.0),
        ]

    input_dir = make_input(
        {"2026-05": rows(0.0), "2026-06": rows(125.0)},
        members=["alice@example.com,premium", "bob@example.com,premium",
                 "carol@example.com,premium", "dave@example.com,premium",
                 "erin@example.com,standard", "frank@example.com,premium",
                 "grace@example.com,premium"],
        org=ORG, workspace="main",
    )
    make_input(
        {"2026-05": [spend_row("zoe@example.com", 200.0, net=0.0)],
         "2026-06": [spend_row("zoe@example.com", 200.0, net=0.0)]},
        members=["zoe@example.com,premium"], org=ORG, workspace="second",
    )
    _write_members_info(
        input_dir,
        "email,追加クレジット上限\n"
        "alice@example.com,250\nbob@example.com,250\ncarol@example.com,250\n"
        "dave@example.com,250\nerin@example.com,250\nfrank@example.com,0\n"
        "grace@example.com,\nzoe@example.com,\n",
    )
    _write_code_analytics(input_dir / ORG / "main", "2026-06",
                          [("alice@example.com", 120)])
    return input_dir


def _payout(make_input, tmp_path, **second) -> dict:
    """払い出し判定を email → 判定の辞書で返す。"""
    input_dir = _payout_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium", **second},
    })
    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    return {j.email: j for j in layer.payout}


def test_payout_statuses(make_input, tmp_path):
    # 損益分岐 $125 との比較と、追加クレジットの状態で判定が分かれる
    judgments = _payout(make_input, tmp_path)
    # 副にアカウントを持つ人（zoe）は払い出し判定の対象ではない
    assert list(judgments) == [
        "alice@example.com", "bob@example.com", "carol@example.com",
        "dave@example.com", "erin@example.com", "frank@example.com",
        "grace@example.com",
    ]
    # 実課金が損益分岐ちょうどの月が2ヶ月続いた（副の固定費より多くを払っている）
    assert judgments["alice@example.com"].status == PAYOUT_CANDIDATE
    assert judgments["alice@example.com"].streak_months == 2
    assert judgments["alice@example.com"].cap_reached is False
    # 1セント足りないだけで連続は成立しない
    assert judgments["bob@example.com"].status == PAYOUT_WATCH
    assert judgments["bob@example.com"].streak_months == 0
    # 損益分岐以上だが1ヶ月だけ
    assert judgments["carol@example.com"].status == PAYOUT_WATCH
    assert judgments["carol@example.com"].streak_months == 1
    # 実課金ゼロ＝込み枠で足りている（需要の大小は問わない）
    assert judgments["dave@example.com"].status == PAYOUT_UNNEEDED
    assert judgments["dave@example.com"].api_cost_usd == 300.0


def test_payout_without_evidence(make_input, tmp_path):
    # 実課金が上限到達を語らない状態・V1 の昇格判定が先の状態は判断材料なし
    judgments = _payout(make_input, tmp_path)
    erin = judgments["erin@example.com"]
    assert erin.status == PAYOUT_NO_EVIDENCE
    assert erin.reason == "主が Standard のため V1 の昇格判定が先"
    frank = judgments["frank@example.com"]
    assert frank.status == PAYOUT_NO_EVIDENCE
    assert frank.reason == "主の追加クレジットが無効"
    grace = judgments["grace@example.com"]
    assert grace.status == PAYOUT_NO_EVIDENCE
    assert grace.reason == "主の追加クレジット上限が不明"


def test_payout_materials(make_input, tmp_path):
    # 月別の実課金・Code 比率・LoC を併記する（「さらに仕事が進むか」を読む材料）
    judgments = _payout(make_input, tmp_path)
    carol = judgments["carol@example.com"]
    assert carol.monthly_billed == (("2026-05", 0.0), ("2026-06", 125.0))
    assert carol.billed_usd == 125.0
    assert carol.code_ratio == 1.0
    assert judgments["alice@example.com"].loc_with_cc == 120
    assert judgments["bob@example.com"].loc_with_cc == 0


def test_payout_cap_reached_needs_only_one_month(make_input, tmp_path):
    # 上限到達は容量不足が確定しているので、連続月数を待たずに候補にする
    input_dir = make_input(
        {"2026-06": [spend_row("alice@example.com", 400.0, net=250.0)]},
        members=["alice@example.com,premium"], org=ORG, workspace="main",
    )
    make_input(
        {"2026-06": [spend_row("zoe@example.com", 200.0, net=0.0)]},
        members=["zoe@example.com,premium"], org=ORG, workspace="second",
    )
    _write_members_info(input_dir, "email,追加クレジット上限\nalice@example.com,250\n")
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })

    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    alice = {j.email: j for j in layer.payout}["alice@example.com"]
    assert alice.cap_reached is True
    assert alice.streak_months == 1  # 連続はまだ足りていない
    assert alice.status == PAYOUT_CANDIDATE
    assert layer.breakeven_usd == 125.0
    assert layer.evaluation_months == 2


def _write_member_snapshot(base: Path, date: str, members: list[str]) -> None:
    """workspace の members を単日スナップショット（日付つき命名）で置く。"""
    directory = base / "members"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"members-snap-{date}.csv").write_text(
        "Email,Seat Type\n" + "\n".join(members) + "\n", encoding="utf-8")


def _second_members(*emails: str) -> list[str]:
    return [f"{email}@example.com,premium" for email in emails]


def _continuation_input(make_input) -> Path:
    """副にアカウントを持つ人が、継続判定の全ステータスに散る構成。

    副は2026-04 から運用していて、dave は 2026-05・erin は 2026-06 に加わる。
    frank は 2026-04 から在籍しているが利用が始まったのは 2026-06（払い出した月は
    members 側から決まる）。zoe は主にアカウントを持たない。
    """
    main_rows = [spend_row("alice@example.com", 300.0, net=125.0),
                 spend_row("bob@example.com", 20.0, net=0.0),
                 spend_row("carol@example.com", 20.0, net=0.0)]
    input_dir = make_input(
        {"2026-05": main_rows, "2026-06": main_rows},
        members=["alice@example.com,premium", "bob@example.com,premium",
                 "carol@example.com,premium", "dave@example.com,standard",
                 "erin@example.com,standard", "frank@example.com,standard"],
        members_month="2026-06", org=ORG, workspace="main",
    )
    make_input(
        {
            "2026-04": [spend_row("alice@example.com", 150.0, net=0.0),
                        spend_row("bob@example.com", 20.0, net=0.0),
                        spend_row("carol@example.com", 140.0, net=0.0)],
            "2026-05": [spend_row("alice@example.com", 260.0, net=0.0),
                        spend_row("bob@example.com", 10.0, net=0.0),
                        spend_row("carol@example.com", 130.0, net=0.0),
                        spend_row("dave@example.com", 5.0, net=0.0)],
            "2026-06": [spend_row("alice@example.com", 300.0, net=0.0),
                        spend_row("bob@example.com", 0.5, net=0.0),
                        spend_row("carol@example.com", 50.0, net=0.0),
                        spend_row("dave@example.com", 5.0, net=0.0),
                        spend_row("erin@example.com", 30.0, net=0.0),
                        spend_row("frank@example.com", 3.0, net=0.0)],
        },
        members=_second_members("alice", "bob", "carol", "frank", "zoe"),
        members_month="2026-04", org=ORG, workspace="second",
    )
    for month, members in (
        ("2026-05", _second_members("alice", "bob", "carol", "frank", "zoe", "dave")),
        ("2026-06", _second_members("alice", "bob", "carol", "frank", "zoe",
                                    "dave", "erin")),
    ):
        make_input({}, members=members, members_month=month,
                   org=ORG, workspace="second")
    _write_members_info(
        input_dir,
        "email,部署,追加クレジット上限\n"
        "alice@example.com,推進部,250\nbob@example.com,推進部,\n"
        "carol@example.com,基盤部,\ndave@example.com,基盤部,\n"
        "erin@example.com,基盤部,\nfrank@example.com,基盤部,\n"
        "zoe@example.com,基盤部,\n",
    )
    return input_dir


def _continuation(make_input, tmp_path, second: dict | None = None, **org_keys) -> dict:
    """継続判定を email → 判定の辞書で返す（1人1副アカウントの構成）。"""
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True},
        "second": {"fixed_seat": "premium", **(second or {})},
    }, **org_keys)
    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    return {j.email: j for j in layer.continuation}


def test_continuation_statuses(make_input, tmp_path):
    # 副の需要と損益分岐 $125 の比較、完全月の数で判定が分かれる
    judgments = _continuation(make_input, tmp_path)
    assert list(judgments) == [
        "alice@example.com", "bob@example.com", "carol@example.com",
        "dave@example.com", "erin@example.com", "frank@example.com",
        "zoe@example.com",
    ]
    assert judgments["alice@example.com"].status == CONTINUE
    assert judgments["alice@example.com"].api_cost_usd == 300.0
    # 直近が損益分岐未満でも、1つ前が以上なら観察に留める
    carol = judgments["carol@example.com"]
    assert carol.status == CONTINUATION_WATCH
    assert (carol.complete_months, carol.streak_months) == (2, 1)
    # 加わった月は不完全月として数えない
    assert judgments["erin@example.com"].status == WAITING
    assert judgments["erin@example.com"].complete_months == 0
    dave = judgments["dave@example.com"]
    assert dave.status == WAITING
    assert dave.complete_months == 1


def test_continuation_return_candidate(make_input, tmp_path):
    # 損益分岐未満の完全月が2ヶ月続いたら戻す候補（削減見込みは副のシート料 − 需要）
    judgments = _continuation(make_input, tmp_path)
    bob = judgments["bob@example.com"]
    assert bob.status == RETURN_CANDIDATE
    assert (bob.complete_months, bob.streak_months) == (2, 2)
    assert bob.api_cost_usd == 0.5
    assert bob.saving_usd == 124.5
    assert bob.idle is True
    assert bob.monthly_demand == (
        ("2026-04", 20.0), ("2026-05", 10.0), ("2026-06", 0.5))
    # 利用が始まる前の月も在籍していれば完全月として数える（払い出し月は members 側）
    frank = judgments["frank@example.com"]
    assert frank.status == RETURN_CANDIDATE
    assert frank.complete_months == 2
    assert frank.saving_usd == 122.0
    assert frank.idle is False


def test_continuation_ignores_members_from_a_later_month(make_input, tmp_path):
    # members が対象月の翌月初の1本だけなら、過去月の在籍の証拠には使わない
    # （後の月のファイルを証拠にすると払い出し月が早まり、完全月を多く数えて
    # シートを外す側へ倒れる）。月別にファイルがある場合は従来どおり
    # （_continuation_input の frank は完全月2の戻す候補のまま）
    input_dir = make_input(
        {"2026-05": [spend_row("alice@example.com", 300.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 300.0, net=0.0)]},
        members=["alice@example.com,premium", "frank@example.com,standard"],
        members_month="2026-06", org=ORG, workspace="main",
    )
    make_input(
        {"2026-04": [spend_row("alice@example.com", 300.0, net=0.0)],
         "2026-05": [spend_row("alice@example.com", 300.0, net=0.0)],
         "2026-06": [spend_row("alice@example.com", 300.0, net=0.0)]},
        org=ORG, workspace="second",
    )
    _write_member_snapshot(input_dir / ORG / "second", "2026-07-03",
                           _second_members("alice", "frank"))
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })

    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    judgments = {j.email: j for j in layer.continuation}
    # spend に一度も現れない人の払い出し月は、そのファイルが通常運用の範囲に入る月
    frank = judgments["frank@example.com"]
    assert frank.complete_months == 0
    assert frank.status == WAITING
    # spend にいる人は従来どおり最初の月から数える
    assert judgments["alice@example.com"].status == CONTINUE


def test_continuation_over_primary_cap(make_input, tmp_path):
    # 副の需要が主の追加クレジット上限を超えた月＝クレジットでは賄えなかった量
    judgments = _continuation(make_input, tmp_path)
    assert judgments["alice@example.com"].over_primary_cap_months == (
        "2026-05", "2026-06")
    # 主の上限が不明な人は数えない
    assert judgments["carol@example.com"].over_primary_cap_months == ()
    # 主にアカウントが無い人も数えない
    assert judgments["zoe@example.com"].over_primary_cap_months == ()
    assert judgments["zoe@example.com"].status == RETURN_CANDIDATE


def test_continuation_breakeven_override(make_input, tmp_path):
    # 組織に書いた損益分岐は fixed_seat の価格より優先する
    judgments = _continuation(make_input, tmp_path, secondary_breakeven_usd=40.0)
    assert judgments["carol@example.com"].status == CONTINUE
    assert judgments["carol@example.com"].breakeven_usd == 40.0
    assert judgments["bob@example.com"].status == RETURN_CANDIDATE


def test_continuation_evaluation_months_override(make_input, tmp_path):
    # workspace に書いた連続月数が効く（3ヶ月なら完全月2ヶ月では判断しない）
    judgments = _continuation(make_input, tmp_path, second={"evaluation_months": 3})
    assert judgments["bob@example.com"].status == WAITING
    assert judgments["bob@example.com"].complete_months == 2


def test_continuation_breakeven_follows_fixed_seat(make_input, tmp_path):
    # 副が Standard を払い出す運用なら損益分岐も $25 になる
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "standard"},
    })
    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    judgments = {j.email: j for j in layer.continuation}
    assert layer.breakeven_usd == 25.0
    assert judgments["carol@example.com"].breakeven_usd == 25.0
    assert judgments["carol@example.com"].status == CONTINUE


def test_billed_with_secondary(make_input, tmp_path):
    # 副を持ちながら主で実課金が発生した人（月別の副の需要と並べて読む）
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })
    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    rows = layer.billed_with_secondary
    assert [r.email for r in rows] == ["alice@example.com"]
    assert rows[0].billed_usd == 125.0
    assert rows[0].secondary_api_cost_usd == 300.0
    assert rows[0].monthly == (
        ("2026-05", 125.0, 260.0), ("2026-06", 125.0, 300.0))


def test_person_frame_holds_both_accounts(make_input, tmp_path):
    # 人の行は保有シート・シート費合計・主と副の内訳を持つ
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })
    layer = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).persons
    frame = layer.frame.set_index("email")

    alice = frame.loc["alice@example.com"]
    assert alice["n_accounts"] == 2
    assert alice["seats"] == "main=premium; second=premium"
    assert alice["primary_seat"] == "premium"
    assert alice["seat_cost_usd"] == 250.0
    assert alice["api_cost_usd"] == 600.0
    assert alice["primary_api_cost_usd"] == 300.0
    assert alice["secondary_api_cost_usd"] == 300.0
    assert alice["secondary_ratio"] == 0.5
    assert alice["billed_extra_usd"] == 125.0
    assert alice["primary_billed_usd"] == 125.0
    assert alice["secondary_billed_usd"] == 0.0

    # 副にだけアカウントがある人も1人として数える（主は無い）
    zoe = frame.loc["zoe@example.com"]
    assert zoe["n_accounts"] == 1
    assert zoe["seats"] == "second=premium"
    assert zoe["primary_seat"] == ""
    assert zoe["seat_cost_usd"] == 125.0
    # 需要が無い人は副の比率を持たない（列は数値なので欠損として並ぶ）
    assert pd.isna(zoe["secondary_ratio"])

    # 主が Standard・副が Premium の人はシート費が両方の合計
    assert frame.loc["dave@example.com", "seats"] == "main=standard; second=premium"
    assert frame.loc["dave@example.com", "seat_cost_usd"] == 150.0


def test_primary_row_sees_secondary_only_usage(make_input, tmp_path):
    # 主に利用が無い人でも副の需要は主の行の判定に入る（月次表に行を新設する）。
    # 主に居ない人（副にだけアカウントがある zoe）には主の行を作らない
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })
    main = analyze_org(input_dir / ORG, "2026-06", cfg, ORG).workspaces["main"]
    users = main.users.set_index("email")

    assert users.loc["dave@example.com", "api_cost_usd"] == 5.0
    assert users.loc["dave@example.com", "billed_extra_usd"] == 0.0
    assert "zoe@example.com" not in users.index


def test_group_summary_counts_people_not_accounts(make_input, tmp_path):
    # 部署別サマリは人の層で数える（アカウント数を人数として数えない）
    input_dir = _continuation_input(make_input)
    cfg = _cfg(tmp_path, {
        "main": {"primary": True}, "second": {"fixed_seat": "premium"},
    })
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    summary = result.workspaces["main"].summary
    rows = {r["group"]: r
            for r in _group_summary_rows(result.persons.frame, summary, "department")}

    n_accounts = sum(len(w.users) for w in result.workspaces.values())
    assert n_accounts == 13
    assert sum(r["n"] for r in rows.values()) == 7
    assert rows["推進部"]["n"] == 2
    assert rows["基盤部"]["n"] == 5
    # シート費は保有シートの合計（Premium 2枚の2名）
    assert rows["推進部"]["seat_cost"] == 500.0


def _single_workspace_input(make_input) -> Path:
    """従来レイアウト（workspace が1つ）の組織。"""
    rows = [spend_row("alice@example.com", 20.0, net=0.0),
            spend_row("bob@example.com", 300.0, net=250.0),
            spend_row("carol@example.com", 5.0, net=0.0)]
    input_dir = make_input(
        {"2026-05": rows, "2026-06": rows},
        members=["alice@example.com,premium", "bob@example.com,standard",
                 "carol@example.com,premium"], org=ORG,
    )
    _write_members_info(
        input_dir,
        "email,部署\nalice@example.com,推進部\nbob@example.com,推進部\n"
        "carol@example.com,基盤部\n",
    )
    return input_dir


def test_single_workspace_person_frame_matches_users(cfg, make_input):
    # 単一 workspace の組織では人とアカウントが1対1（判定は行わない）
    input_dir = _single_workspace_input(make_input)
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    layer = result.persons
    users = result.workspaces[ORG].users.set_index("email")
    frame = layer.frame.set_index("email")

    assert layer.payout == () and layer.continuation == ()
    assert layer.billed_with_secondary == ()
    assert layer.breakeven_usd is None and layer.evaluation_months is None
    assert list(frame.index) == sorted(users.index)
    for email in frame.index:
        assert frame.loc[email, "n_accounts"] == 1
        assert frame.loc[email, "api_cost_usd"] == users.loc[email, "api_cost_usd"]
        assert frame.loc[email, "billed_extra_usd"] == users.loc[email, "billed_extra_usd"]
        assert frame.loc[email, "status"] == users.loc[email, "status"]
        assert frame.loc[email, "cost_current_usd"] == users.loc[email, "cost_current_usd"]
        if frame.loc[email, "status"] == STATUS_CHANGE:
            assert (frame.loc[email, "monthly_saving_usd"]
                    == users.loc[email, "monthly_saving_usd"])


def test_single_workspace_group_summary_is_unchanged(cfg, make_input):
    # 人の表から作る部署別サマリは、users から作ったものと一致する
    input_dir = _single_workspace_input(make_input)
    result = analyze_org(input_dir / ORG, "2026-06", cfg, ORG)
    workspace = result.workspaces[ORG]
    for col in ("department", "team"):
        assert (_group_summary_rows(result.persons.frame, workspace.summary, col)
                == _group_summary_rows(workspace.users, workspace.summary, col))
