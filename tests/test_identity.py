import pandas as pd
import pytest

from seat_analyzer.identity import IdentityEvidence, resolve_identities


def test_account_uuid_has_priority_over_user_id():
    results = resolve_identities([
        IdentityEvidence(
            email=" A@X.JP ",
            account_uuid=" account-1 ",
            user_id=" user-1 ",
        ),
    ])

    assert results[0].subject_id == "account:account-1"
    assert results[0].quality == "stable"
    assert results[0].conflict is False
    assert results[0].emails == ("a@x.jp",)
    assert results[0].user_ids == ("user-1",)


def test_user_id_is_used_when_account_uuid_is_missing():
    result = resolve_identities([
        IdentityEvidence(email="a@x.jp", user_id="user-1"),
    ])[0]

    assert result.subject_id == "user:user-1"
    assert result.quality == "stable"


def test_email_change_is_joined_by_stable_id():
    result = resolve_identities([
        IdentityEvidence(email="old@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="new@x.jp", account_uuid="account-1"),
    ])[0]

    assert result.subject_id == "account:account-1"
    assert result.emails == ("new@x.jp", "old@x.jp")
    assert result.conflict is False


def test_stable_id_propagates_to_evidence_with_same_email():
    result = resolve_identities([
        IdentityEvidence(email="a@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="a@x.jp"),
    ])[0]

    assert result.subject_id == "account:account-1"
    assert result.emails == ("a@x.jp",)


def test_conflict_applies_to_entire_connected_component():
    result = resolve_identities([
        IdentityEvidence(email="a@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="a@x.jp", account_uuid="account-2"),
        IdentityEvidence(email="b@x.jp", account_uuid="account-2"),
    ])[0]

    assert result.subject_id is None
    assert result.quality == "conflict"
    assert result.conflict is True
    assert result.account_uuids == ("account-1", "account-2")
    assert result.emails == ("a@x.jp", "b@x.jp")


def test_multiple_user_ids_in_same_identity_are_conflict():
    result = resolve_identities([
        IdentityEvidence(account_uuid="account-1", user_id="user-1"),
        IdentityEvidence(account_uuid="account-1", user_id="user-2"),
    ])[0]

    assert result.subject_id is None
    assert result.quality == "conflict"
    assert result.user_ids == ("user-1", "user-2")


def test_email_quality_requires_explicit_history_confirmation():
    results = resolve_identities(
        [
            IdentityEvidence(email="consistent@x.jp"),
            IdentityEvidence(email="fallback@x.jp"),
        ],
        consistent_emails={" CONSISTENT@X.JP "},
    )

    assert results[0].subject_id == "email:consistent@x.jp"
    assert results[0].quality == "email_consistent"
    assert results[1].subject_id == "email:fallback@x.jp"
    assert results[1].quality == "email_fallback"


def test_mixed_stable_ids_connect_multiple_email_changes():
    result = resolve_identities([
        IdentityEvidence(email="a@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="b@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="b@x.jp", user_id="user-9"),
        IdentityEvidence(email="c@x.jp", user_id="user-9"),
    ])[0]

    assert result.subject_id == "account:account-1"
    assert result.quality == "stable"
    assert result.conflict is False
    assert result.emails == ("a@x.jp", "b@x.jp", "c@x.jp")
    assert result.user_ids == ("user-9",)


def test_result_order_uses_first_evidence_even_with_unresolved_rows():
    results = resolve_identities([
        IdentityEvidence(),
        IdentityEvidence(email="old@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="   "),
        IdentityEvidence(email="new@x.jp", account_uuid="account-1"),
        IdentityEvidence(email="fallback@x.jp"),
    ])

    assert [result.subject_id for result in results] == [
        None,
        "account:account-1",
        None,
        "email:fallback@x.jp",
    ]


def test_consistent_email_does_not_override_stable_quality():
    result = resolve_identities(
        [IdentityEvidence(email="a@x.jp", user_id="user-1")],
        consistent_emails={"a@x.jp"},
    )[0]

    assert result.subject_id == "user:user-1"
    assert result.quality == "stable"


def test_empty_evidence_returns_empty_result():
    assert resolve_identities([]) == []


def test_missing_and_whitespace_values_are_unresolved():
    results = resolve_identities([
        IdentityEvidence(email="   ", account_uuid=pd.NA, user_id=""),
    ])

    assert results[0].subject_id is None
    assert results[0].quality == "unresolved"
    assert results[0].conflict is False


def test_non_scalar_evidence_is_rejected():
    with pytest.raises(TypeError, match="スカラー値"):
        resolve_identities([IdentityEvidence(email=["a@x.jp"])])
