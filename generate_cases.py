#!/usr/bin/env python3
"""Generate synthetic INR / SNAD dispute cases and gold labels from Rulebook v0.1.

Each scenario states the outcome and rule it is *meant* to trigger. Gold labels
always come from `rulebook.label_case`; the generator then asserts the labeler
agrees with the scenario's intent, so a mismatch means a bug in the generator
or the rulebook, never a silent label error.

Outputs (in --out):
  cases_dev.jsonl / cases_test.jsonl    model inputs only (no labels)
  labels_dev.jsonl / labels_test.jsonl  gold outcome, rule, scenario, hard flag
Keep the test split untouched while tuning prompts; tune on dev only.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
from datetime import date, timedelta
from pathlib import Path

from rulebook import APPROVE, DENY, ESCALATE, REQUEST, label_case

QUOTAS = {APPROVE: 0.30, DENY: 0.30, REQUEST: 0.20, ESCALATE: 0.20}
DEV_FRACTION = 0.30

# (item, min price, max price)
ITEMS = [
    ("phone case", 8, 30), ("desk lamp", 25, 90), ("blender", 40, 150),
    ("wireless headphones", 40, 180), ("running shoes", 50, 160), ("jacket", 60, 220),
    ("watch", 100, 900), ("gaming console", 250, 600), ("camera lens", 300, 1400),
    ("laptop", 700, 1800),
]


def pick_item(rng, lo, hi):
    cands = [i for i in ITEMS if i[1] <= hi and i[2] >= lo]
    if not cands:
        return "household item", rng.randint(lo, hi)
    name, a, b = rng.choice(cands)
    return name, rng.randint(max(lo, a), min(hi, b))


def _val(rng, v):
    return rng.randint(*v) if isinstance(v, tuple) else v


def _ret(r, a):
    """Return status for cases that end in an approval (satisfies SNAD-6)."""
    return "received" if a >= 100 else r.choice(["not_required", "received"])


def S(case_type, intent, rule, hard, amount, buyer, merchant=None, days_past=None,
      order_offset=None, over=None, b_ev=(), m_ev=()):
    return dict(case_type=case_type, intent=intent, rule=rule, hard=hard, amount=amount,
                buyer=buyer, merchant=merchant, days_past=days_past, order_offset=order_offset,
                over=over or {}, b_ev=list(b_ev), m_ev=list(m_ev))


INR_BUYER = [
    "I ordered {item} and it never arrived.",
    "My {item} order still has not shown up. I want a refund.",
    "Nothing was delivered for the {item} I paid for.",
]
DELIVERED_BUYER = [
    "Tracking says delivered, but I never received my {item}.",
    "The system shows my {item} as delivered. It is not here and nobody at home saw it.",
]
SNAD_PHOTO = ["photo of item received"]

SCENARIOS = {
    # ------------------------------- INR -------------------------------
    "inr_too_early": S("INR", DENY, "INR-1", False, (20, 300), INR_BUYER, days_past=(-4, -1),
                       over={"tracking_status": lambda r, a: r.choice(["in_transit", "none"])}),
    "inr_proven_delivery": S("INR", DENY, "INR-2", False, (20, 400), DELIVERED_BUYER,
                             ["Delivered and signed for by the recipient. Proof attached."],
                             days_past=(3, 20),
                             over={"delivery_proof": lambda r, a: r.choice(["signature", "photo"]),
                                   "merchant_response": "provided", "prior_buyer_disputes_90d": lambda r, a: r.randint(0, 1)},
                             m_ev=["carrier delivery proof"]),
    "inr_proven_delivery_prior2": S("INR", DENY, "INR-2", True, (30, 400),
                                    ["This is unacceptable, my {item} was never delivered and I am sick of this. Refund me now."],
                                    ["Carrier photo shows the parcel at the delivery address."], days_past=(3, 20),
                                    over={"delivery_proof": "photo", "merchant_response": "provided",
                                          "prior_buyer_disputes_90d": 2},
                                    m_ev=["carrier delivery photo"]),
    "inr_delivered_no_proof": S("INR", APPROVE, "INR-3", False, (20, 400), DELIVERED_BUYER, days_past=(3, 20),
                                over={"delivery_proof": "none",
                                      "address_match": lambda r, a: r.choice([True, "unknown"])}),
    "inr_persuasive_merchant_no_proof": S(
        "INR", APPROVE, "INR-3", True, (30, 500), DELIVERED_BUYER,
        ["We shipped on time with a reputable carrier and have never had a complaint on this product. "
         "The tracking page shows delivered, so we consider this order fulfilled."],
        days_past=(3, 20), over={"delivery_proof": "none", "merchant_response": "provided"}),
    "inr_address_mismatch": S("INR", APPROVE, "INR-3", False, (20, 400),
                              ["The {item} was delivered to a different address than mine."],
                              days_past=(3, 20), over={"delivery_proof": "photo", "address_match": False},
                              b_ev=["screenshot of shipping address"]),
    "inr_lost": S("INR", APPROVE, "INR-4", False, (20, 400), INR_BUYER, days_past=(3, 25),
                  over={"tracking_status": lambda r, a: r.choice(["lost", "returned_to_sender"])}),
    "inr_lost_early": S("INR", APPROVE, "INR-4", True, (20, 400),
                        ["Carrier notified me the parcel for my {item} is lost, and the promised date has not passed yet."],
                        days_past=(-3, -1), over={"tracking_status": "lost"}),
    "inr_window_boundary_lost": S("INR", APPROVE, "INR-4", True, (20, 400), INR_BUYER, order_offset=180,
                                  over={"tracking_status": "lost"}),
    "inr_no_tracking_silent": S("INR", APPROVE, "INR-5", False, (20, 400), INR_BUYER, days_past=(10, 25),
                                over={"tracking_status": "none"}),
    "inr_no_tracking_7d": S("INR", APPROVE, "INR-5", True, (20, 400), INR_BUYER, days_past=7,
                            over={"tracking_status": "none"}),
    "inr_no_tracking_6d": S("INR", REQUEST, "INR-6", True, (20, 400), INR_BUYER, days_past=6,
                            over={"tracking_status": "none"}),
    "inr_no_tracking_recent": S("INR", REQUEST, "INR-6", False, (20, 400), INR_BUYER, days_past=(0, 4),
                                over={"tracking_status": "none"}),
    "inr_long_transit": S("INR", APPROVE, "INR-7", False, (20, 400),
                          ["My {item} has been stuck in transit for weeks."], days_past=(18, 30),
                          over={"tracking_status": "in_transit"}),
    "inr_transit_14d": S("INR", APPROVE, "INR-7", True, (20, 400),
                         ["My {item} is still in transit and is two weeks late."], days_past=14,
                         over={"tracking_status": "in_transit"}),
    "inr_transit_13d": S("INR", DENY, "INR-7", True, (20, 400),
                         ["My {item} is still in transit and almost two weeks late."], days_past=13,
                         over={"tracking_status": "in_transit"}),
    "inr_short_transit": S("INR", DENY, "INR-7", False, (20, 400),
                           ["My {item} is a few days late and still in transit."], days_past=(1, 6),
                           over={"tracking_status": "in_transit"}),
    "inr_unknown_address": S("INR", REQUEST, "DEFAULT", True, (20, 400), DELIVERED_BUYER, days_past=(3, 20),
                             over={"delivery_proof": "signature", "address_match": "unknown",
                                   "merchant_response": "provided"},
                             m_ev=["carrier signature record without address detail"],
                             merchant=["Carrier shows a signature but our system cannot confirm the address match."]),
    "inr_merchant_late_no_tracking": S(
        "INR", REQUEST, "DEFAULT", True, (20, 400), INR_BUYER,
        ["We posted the parcel last week but do not have a tracking number. Please wait a bit longer."],
        days_past=(8, 20), over={"tracking_status": "none", "merchant_response": "provided"}),
    "inr_signature_counterclaim": S(
        "INR", ESCALATE, "G6", True, (30, 700),
        ["The signature on the delivery record is not mine and I live in an apartment with a doorman. "
         "Nobody accepted a parcel."],
        ["Delivered and signed for. Carrier record attached."], days_past=(3, 20),
        over={"delivery_proof": "signature", "address_match": True, "merchant_response": "provided",
              "evidence_conflict": True},
        b_ev=["doorman log with no parcel entry"], m_ev=["carrier signature record"]),
    "inr_abuse_pattern": S("INR", ESCALATE, "G5", True, (30, 700), DELIVERED_BUYER,
                           ["Delivered with photo proof to the address on the order."], days_past=(3, 20),
                           over={"delivery_proof": "photo", "address_match": True, "merchant_response": "provided",
                                 "prior_buyer_disputes_90d": lambda r, a: r.randint(3, 6)},
                           m_ev=["carrier delivery photo"]),
    "inr_highvalue_conflict": S(
        "INR", ESCALATE, "G4", True, (1000, 2500), DELIVERED_BUYER,
        ["Delivered and signed for by a household member."], days_past=(3, 20),
        over={"delivery_proof": "signature", "address_match": True, "merchant_response": "provided",
              "evidence_conflict": True},
        m_ev=["carrier signature record"]),
    "inr_outside_window": S("INR", DENY, "G1", False, (20, 400), INR_BUYER, order_offset=(181, 260),
                            over={"tracking_status": lambda r, a: r.choice(["lost", "none", "delivered"])}),
    "inr_duplicate": S("INR", DENY, "G2", False, (20, 400),
                       ["I am opening this again because I never received my {item}."], days_past=(5, 25),
                       over={"is_duplicate": True,
                             "tracking_status": lambda r, a: r.choice(["lost", "none", "in_transit"])}),
    # ------------------------------- SNAD -------------------------------
    "snad_wrong_item": S("SNAD", APPROVE, "SNAD-2", False, (30, 400),
                         ["I ordered {item} but received a completely different product."], days_past=(3, 20),
                         over={"snad_issue": "wrong_item", "delivery_proof": "photo", "return_status": _ret},
                         b_ev=SNAD_PHOTO),
    "snad_mismatch_silent": S("SNAD", APPROVE, "SNAD-3", False, (30, 400),
                              ["The listing said the {item} was brand new but it arrived used with visible wear."],
                              days_past=(3, 20),
                              over={"snad_issue": "attribute_mismatch", "delivery_proof": "photo",
                                    "return_status": _ret},
                              b_ev=SNAD_PHOTO),
    "snad_damaged_no_packaging": S("SNAD", APPROVE, "SNAD-5", False, (30, 400),
                                   ["My {item} arrived broken. The box was crushed."], days_past=(3, 20),
                                   over={"snad_issue": "damaged", "delivery_proof": "photo", "return_status": _ret,
                                         "packaging_evidence": "none"},
                                   b_ev=SNAD_PHOTO + ["photo of crushed box"]),
    "snad_damaged_packaging_sound": S(
        "SNAD", ESCALATE, "SNAD-5", True, (30, 700), ["My {item} arrived with a cracked body."],
        ["We packed it in double-walled boxing with padding. Photos attached. The damage may have happened after delivery."],
        days_past=(3, 20),
        over={"snad_issue": "damaged", "delivery_proof": "photo", "merchant_response": "provided",
              "packaging_evidence": "sound", "return_status": "not_required"},
        b_ev=SNAD_PHOTO, m_ev=["packaging photos"]),
    "snad_remorse": S("SNAD", DENY, "SNAD-4", True, (30, 400),
                      ["The {item} is okay I guess, but it is not what I imagined. I want to return it.",
                       "I changed my mind about the {item}. It just does not feel right for me."],
                      days_past=(3, 20),
                      over={"snad_issue": "subjective_only", "listing_matches_item": True, "delivery_proof": "photo"},
                      b_ev=SNAD_PHOTO),
    "snad_no_photo": S("SNAD", REQUEST, "SNAD-1", False, (30, 400),
                       ["The {item} is not as described."], days_past=(3, 20),
                       over={"snad_issue": "attribute_mismatch", "buyer_photo_provided": False,
                             "delivery_proof": "photo"}),
    "snad_no_description": S("SNAD", REQUEST, "SNAD-1", False, (30, 400), [""], days_past=(3, 20),
                             over={"snad_issue": "attribute_mismatch", "buyer_description_provided": False,
                                   "delivery_proof": "photo"},
                             b_ev=SNAD_PHOTO),
    "snad_return_not_shipped": S("SNAD", REQUEST, "SNAD-6", False, (150, 600),
                                 ["The {item} is clearly not what was advertised."], days_past=(3, 20),
                                 over={"snad_issue": "attribute_mismatch", "delivery_proof": "photo",
                                       "return_status": "not_shipped"},
                                 b_ev=SNAD_PHOTO),
    "snad_return_not_shipped_100": S("SNAD", REQUEST, "SNAD-6", True, (100, 100),
                                     ["The {item} is clearly not what was advertised."], days_past=(3, 20),
                                     over={"snad_issue": "wrong_item", "delivery_proof": "photo",
                                           "return_status": "not_shipped"},
                                     b_ev=SNAD_PHOTO),
    "snad_return_not_shipped_99": S("SNAD", APPROVE, "SNAD-2", True, (99, 99),
                                    ["I received the wrong product instead of the {item}."], days_past=(3, 20),
                                    over={"snad_issue": "wrong_item", "delivery_proof": "photo",
                                          "return_status": "not_shipped"},
                                    b_ev=SNAD_PHOTO),
    "snad_counterfeit": S("SNAD", ESCALATE, "G3", False, (50, 700),
                          ["I believe this {item} is a counterfeit. The logo and stitching are wrong."],
                          days_past=(3, 20),
                          over={"snad_issue": "counterfeit", "delivery_proof": "photo"}, b_ev=SNAD_PHOTO),
    "snad_conflict": S("SNAD", ESCALATE, "G6", True, (30, 700),
                       ["The {item} listing said it was the pro model but this is the base model."],
                       ["Our listing clearly states the base model; screenshot attached."], days_past=(3, 20),
                       over={"snad_issue": "attribute_mismatch", "delivery_proof": "photo",
                             "merchant_response": "provided", "evidence_conflict": True},
                       b_ev=SNAD_PHOTO + ["listing screenshot saved at purchase"], m_ev=["listing snapshot"]),
    "snad_highvalue_conflict": S("SNAD", ESCALATE, "G4", True, (1000, 2000),
                                 ["The {item} does not match the specifications that were advertised."],
                                 ["The specifications in the listing match the item shipped."], days_past=(3, 20),
                                 over={"snad_issue": "attribute_mismatch", "delivery_proof": "photo",
                                       "merchant_response": "provided", "evidence_conflict": True},
                                 b_ev=SNAD_PHOTO, m_ev=["listing snapshot"]),
    "snad_vague_issue": S("SNAD", REQUEST, "DEFAULT", True, (30, 400),
                          ["The {item} is not what I expected."], days_past=(3, 20),
                          over={"snad_issue": "none", "delivery_proof": "photo"}, b_ev=SNAD_PHOTO),
}


def build_case(rng, name, sp):
    item, amount = pick_item(rng, *sp["amount"])
    order = date(2025, 11, 1) + timedelta(days=rng.randint(0, 300))
    promised = order + timedelta(days=rng.randint(5, 12))
    if sp["order_offset"] is not None:
        filed = order + timedelta(days=_val(rng, sp["order_offset"]))
    else:
        filed = promised + timedelta(days=_val(rng, sp["days_past"]))
    case = {
        "case_type": sp["case_type"], "item": item, "order_amount": amount,
        "order_date": order.isoformat(), "filed_date": filed.isoformat(),
        "promised_delivery_date": promised.isoformat(),
        "tracking_status": "delivered", "delivery_proof": "none", "address_match": True,
        "merchant_response": "none", "prior_buyer_disputes_90d": rng.randint(0, 1),
        "return_status": "not_required", "is_duplicate": False, "evidence_conflict": False,
    }
    if sp["case_type"] == "SNAD":
        case.update({"snad_issue": "none", "buyer_description_provided": True, "buyer_photo_provided": True,
                     "listing_matches_item": False, "packaging_evidence": "none"})
    for k, v in sp["over"].items():
        case[k] = v(rng, amount) if callable(v) else v
    case["buyer_statement"] = rng.choice(sp["buyer"]).format(item=item)
    case["merchant_statement"] = rng.choice(sp["merchant"]).format(item=item) if sp["merchant"] else None
    case["buyer_evidence"] = list(sp["b_ev"])
    case["merchant_evidence"] = list(sp["m_ev"])
    return case


def generate(n, seed):
    rng = random.Random(seed)
    by_outcome = collections.defaultdict(list)
    for name, sp in SCENARIOS.items():
        by_outcome[sp["intent"]].append(name)

    counts = {o: round(n * q) for o, q in QUOTAS.items()}
    counts[APPROVE] += n - sum(counts.values())  # absorb rounding

    cases, labels = [], []
    for outcome, count in counts.items():
        pool = []
        while len(pool) < count:  # cycle through shuffled scenarios for even coverage
            names = by_outcome[outcome][:]
            rng.shuffle(names)
            pool.extend(names)
        for name in pool[:count]:
            sp = SCENARIOS[name]
            case = build_case(rng, name, sp)
            got, rule = label_case(case)
            if (got, rule) != (sp["intent"], sp["rule"]):
                raise AssertionError(f"{name}: intended {(sp['intent'], sp['rule'])}, labeler gave {(got, rule)}: {case}")
            cases.append(case)
            labels.append({"gold_outcome": got, "gold_rule": rule, "scenario": name, "is_hard": sp["hard"]})

    order = list(range(len(cases)))
    rng.shuffle(order)
    out = []
    for new_id, i in enumerate(order, 1):
        cid = f"RC-{new_id:04d}"
        out.append(({"case_id": cid, **cases[i]}, {"case_id": cid, **labels[i]}))
    return out


def stratified_split(pairs, seed):
    rng = random.Random(seed + 1)
    groups = collections.defaultdict(list)
    for p in pairs:
        groups[p[1]["gold_outcome"]].append(p)
    dev, test = [], []
    for g in groups.values():
        rng.shuffle(g)
        k = round(len(g) * DEV_FRACTION)
        dev.extend(g[:k])
        test.extend(g[k:])
    return dev, test


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()

    pairs = generate(a.n, a.seed)
    dev, test = stratified_split(pairs, a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for tag, split in (("dev", dev), ("test", test)):
        write_jsonl(out / f"cases_{tag}.jsonl", [c for c, _ in split])
        write_jsonl(out / f"labels_{tag}.jsonl", [l for _, l in split])

    print(f"{len(pairs)} cases -> {len(dev)} dev / {len(test)} test, written to {out}/")
    print("outcome  :", dict(collections.Counter(l["gold_outcome"] for _, l in pairs)))
    print("rule     :", dict(sorted(collections.Counter(l["gold_rule"] for _, l in pairs).items())))
    hard = sum(l["is_hard"] for _, l in pairs)
    print(f"hard     : {hard}/{len(pairs)} ({hard / len(pairs):.0%})")


if __name__ == "__main__":
    main()
