"""Unit tests for Rulebook v0.1. Run with `python test_rulebook.py` or `pytest`.

One test per rule, plus the threshold boundaries and the precedence order.
"""
from datetime import date, timedelta

from rulebook import APPROVE, DENY, ESCALATE, REQUEST, label_case

ORDER = date(2026, 3, 1)
PROMISED = ORDER + timedelta(days=7)


def mk(case_type="INR", days_past=5, **kw):
    case = {
        "case_type": case_type, "order_amount": 100,
        "order_date": ORDER.isoformat(),
        "promised_delivery_date": PROMISED.isoformat(),
        "filed_date": (PROMISED + timedelta(days=days_past)).isoformat(),
        "tracking_status": "delivered", "delivery_proof": "none", "address_match": True,
        "merchant_response": "none", "prior_buyer_disputes_90d": 0,
        "return_status": "not_required", "is_duplicate": False, "evidence_conflict": False,
    }
    if case_type == "SNAD":
        case.update({"snad_issue": "none", "buyer_description_provided": True,
                     "buyer_photo_provided": True, "listing_matches_item": False,
                     "packaging_evidence": "none"})
    case.update(kw)
    return case


def check(case, outcome, rule):
    assert label_case(case) == (outcome, rule), (label_case(case), (outcome, rule), case)


# ---------------- global rules ----------------
def test_g1_window_and_boundary():
    check(mk(filed_date=(ORDER + timedelta(days=181)).isoformat()), DENY, "G1")
    # exactly 180 days is still inside the window
    check(mk(filed_date=(ORDER + timedelta(days=180)).isoformat(), tracking_status="lost"), APPROVE, "INR-4")


def test_g2_duplicate():
    check(mk(is_duplicate=True, tracking_status="lost"), DENY, "G2")


def test_g3_counterfeit():
    check(mk("SNAD", snad_issue="counterfeit"), ESCALATE, "G3")


def test_g4_high_value_conflict():
    check(mk(order_amount=1000, evidence_conflict=True), ESCALATE, "G4")
    check(mk(order_amount=999, evidence_conflict=True), ESCALATE, "G6")


def test_g5_abuse_signal():
    check(mk(prior_buyer_disputes_90d=3, delivery_proof="photo"), ESCALATE, "G5")
    check(mk(prior_buyer_disputes_90d=2, delivery_proof="photo", address_match=True), DENY, "INR-2")
    # no delivery proof: abuse signal does not apply
    check(mk(prior_buyer_disputes_90d=5, delivery_proof="none"), APPROVE, "INR-3")


def test_g6_conflict():
    check(mk(evidence_conflict=True, delivery_proof="signature"), ESCALATE, "G6")


def test_precedence():
    # G1 beats duplicate, counterfeit and conflict
    late = (ORDER + timedelta(days=200)).isoformat()
    check(mk("SNAD", snad_issue="counterfeit", is_duplicate=True, filed_date=late), DENY, "G1")
    # G2 beats G3
    check(mk("SNAD", snad_issue="counterfeit", is_duplicate=True), DENY, "G2")


# ---------------- INR ----------------
def test_inr1_too_early():
    check(mk(days_past=-1, tracking_status="in_transit"), DENY, "INR-1")
    check(mk(days_past=-1, tracking_status="lost"), APPROVE, "INR-4")


def test_inr2_proven_delivery():
    for proof in ("signature", "photo"):
        check(mk(delivery_proof=proof, address_match=True), DENY, "INR-2")


def test_inr3_weak_proof():
    check(mk(delivery_proof="none"), APPROVE, "INR-3")
    check(mk(delivery_proof="photo", address_match=False), APPROVE, "INR-3")


def test_inr4_lost_or_returned():
    for status in ("lost", "returned_to_sender"):
        check(mk(tracking_status=status), APPROVE, "INR-4")


def test_inr5_inr6_no_tracking_boundary():
    check(mk(tracking_status="none", days_past=7), APPROVE, "INR-5")
    check(mk(tracking_status="none", days_past=6), REQUEST, "INR-6")
    check(mk(tracking_status="none", days_past=10, merchant_response="provided"), REQUEST, "DEFAULT")


def test_inr7_transit_boundary():
    check(mk(tracking_status="in_transit", days_past=14), APPROVE, "INR-7")
    check(mk(tracking_status="in_transit", days_past=13), DENY, "INR-7")


def test_inr_unknown_address_defaults_to_request():
    check(mk(delivery_proof="signature", address_match="unknown"), REQUEST, "DEFAULT")


# ---------------- SNAD ----------------
def test_snad1_missing_evidence():
    check(mk("SNAD", snad_issue="attribute_mismatch", buyer_photo_provided=False), REQUEST, "SNAD-1")
    check(mk("SNAD", snad_issue="attribute_mismatch", buyer_description_provided=False), REQUEST, "SNAD-1")


def test_snad2_wrong_item():
    check(mk("SNAD", snad_issue="wrong_item", return_status="received"), APPROVE, "SNAD-2")


def test_snad3_clear_mismatch():
    check(mk("SNAD", snad_issue="attribute_mismatch", return_status="received"), APPROVE, "SNAD-3")
    # merchant responded: no rule fires, default applies
    check(mk("SNAD", snad_issue="attribute_mismatch", merchant_response="provided"), REQUEST, "DEFAULT")


def test_snad4_buyers_remorse():
    check(mk("SNAD", snad_issue="subjective_only", listing_matches_item=True), DENY, "SNAD-4")


def test_snad5_damage():
    check(mk("SNAD", snad_issue="damaged", packaging_evidence="none", return_status="received"), APPROVE, "SNAD-5")
    check(mk("SNAD", snad_issue="damaged", packaging_evidence="sound"), ESCALATE, "SNAD-5")


def test_snad6_return_gate_boundary():
    check(mk("SNAD", snad_issue="wrong_item", order_amount=100, return_status="not_shipped"), REQUEST, "SNAD-6")
    check(mk("SNAD", snad_issue="wrong_item", order_amount=99, return_status="not_shipped"), APPROVE, "SNAD-2")
    check(mk("SNAD", snad_issue="wrong_item", order_amount=500, return_status="received"), APPROVE, "SNAD-2")


def test_snad_vague_defaults_to_request():
    check(mk("SNAD", snad_issue="none"), REQUEST, "DEFAULT")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"\n{len(tests)} tests passed")
