"""Spend / Members間でstable IDを解決するための純粋なIdentityドメイン。

V1のemail joinには接続せず、後続機能が利用するsubject_id・品質・conflictを
入力証拠から決定する。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Literal

import pandas as pd

IdentityQuality = Literal[
    "stable",
    "email_consistent",
    "email_fallback",
    "conflict",
    "unresolved",
]

_Node = tuple[str, str]


@dataclass(frozen=True)
class IdentityEvidence:
    """1入力行から得られるIdentity証拠。各値はスカラーに限り、正規化はresolve時に行う。"""

    email: object = None
    account_uuid: object = None
    user_id: object = None


@dataclass(frozen=True)
class ResolvedIdentity:
    """相互に結び付いたIdentity証拠1組の解決結果。"""

    subject_id: str | None
    quality: IdentityQuality
    conflict: bool
    emails: tuple[str, ...]
    account_uuids: tuple[str, ...]
    user_ids: tuple[str, ...]


def normalize_value(
    value: object,
    *,
    field_name: str,
    lowercase: bool = False,
) -> str | None:
    """Identity 値を照合用の文字列へ揃える（前後空白を除去し、email だけ小文字化）。

    空文字・欠損は None。非スカラーは TypeError で拒否する。resolve_identities と
    seat_changes の逆引きは同じ規則で照合する必要があるため、この関数を共有する
    （規則が違うと逆引き表を引けない）。
    """
    if not pd.api.types.is_scalar(value):
        value_repr = repr(value)
        if len(value_repr) > 160:
            value_repr = f"{value_repr[:157]}..."
        raise TypeError(f"{field_name}にはスカラー値が必要です: {value_repr}")
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lowercase else text


def _component_result(
    nodes: set[_Node],
    consistent_emails: set[str],
) -> ResolvedIdentity:
    emails = tuple(sorted(value for kind, value in nodes if kind == "email"))
    account_uuids = tuple(sorted(value for kind, value in nodes if kind == "account"))
    user_ids = tuple(sorted(value for kind, value in nodes if kind == "user"))

    conflict = len(account_uuids) > 1 or len(user_ids) > 1
    if conflict:
        subject_id = None
        quality: IdentityQuality = "conflict"
    elif account_uuids:
        subject_id = f"account:{account_uuids[0]}"
        quality = "stable"
    elif user_ids:
        subject_id = f"user:{user_ids[0]}"
        quality = "stable"
    elif emails:
        subject_id = f"email:{emails[0]}"
        quality = "email_consistent" if emails[0] in consistent_emails else "email_fallback"
    else:
        subject_id = None
        quality = "unresolved"

    return ResolvedIdentity(
        subject_id=subject_id,
        quality=quality,
        conflict=conflict,
        emails=emails,
        account_uuids=account_uuids,
        user_ids=user_ids,
    )


def resolve_identities(
    evidence: Iterable[IdentityEvidence],
    *,
    consistent_emails: Collection[str] = (),
) -> list[ResolvedIdentity]:
    """Identity証拠を連結し、決定的な順序で解決結果を返す。

    同じemailまたはstable IDを共有する証拠を1組へまとめる。異なるemailでも
    stable IDが同じなら同一subjectとなる。account_uuidとuser_idの併存は正常で、
    同じ組の中に同種stable IDが複数ある場合だけconflictとする。

    email_consistentは履歴十分性を呼び出し側が確認したemailに限る。指定がなければ、
    stable IDのないemailはemail_fallbackとなる。

    全Identity値が欠損した証拠は互いの同一性を判断できないため、入力行ごとに
    unresolvedを返す。したがって戻り値の件数はsubject数と一致するとは限らない。
    """
    graph: dict[_Node, set[_Node]] = {}
    node_order: dict[_Node, int] = {}
    unresolved: list[tuple[int, ResolvedIdentity]] = []

    for index, item in enumerate(evidence):
        email = normalize_value(
            item.email,
            field_name="IdentityEvidence.email",
            lowercase=True,
        )
        account_uuid = normalize_value(
            item.account_uuid,
            field_name="IdentityEvidence.account_uuid",
        )
        user_id = normalize_value(
            item.user_id,
            field_name="IdentityEvidence.user_id",
        )
        nodes = [
            node
            for node in (
                ("email", email) if email else None,
                ("account", account_uuid) if account_uuid else None,
                ("user", user_id) if user_id else None,
            )
            if node is not None
        ]
        if not nodes:
            unresolved.append((index, _component_result(set(), set())))
            continue

        for node in nodes:
            graph.setdefault(node, set())
            node_order.setdefault(node, index)
        anchor = nodes[0]
        for node in nodes[1:]:
            graph[anchor].add(node)
            graph[node].add(anchor)

    normalized_consistent_emails = {
        normalized
        for value in consistent_emails
        if (
            normalized := normalize_value(
                value,
                field_name="consistent_emailsの要素",
                lowercase=True,
            )
        )
        is not None
    }
    results: list[tuple[int, ResolvedIdentity]] = []
    seen: set[_Node] = set()
    for start in sorted(graph, key=lambda node: node_order[node]):
        if start in seen:
            continue
        component: set[_Node] = set()
        pending = [start]
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(graph[node] - component)
        seen.update(component)
        first_index = min(node_order[node] for node in component)
        results.append(
            (first_index, _component_result(component, normalized_consistent_emails))
        )

    results.extend(unresolved)
    results.sort(key=lambda item: item[0])
    return [result for _, result in results]
