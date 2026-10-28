"""Drafts the hand-labeled gold set inputs. Run: python make_gold_cases.py

Writes (no labels anywhere in these files):
  data/gold_cases.jsonl   25 hard cases, IDs GS-001..GS-025, same schema as the dev cases
  data/gold_meta.jsonl    which hard-case pattern each case targets (kept out of the label cards)
  gold/cards.md           readable cards for the human labeler
  gold/label_sheet.csv    blank sheet: case_id, outcome, rule_id, confidence, notes

Cases are written by hand, not sampled from generate_cases.py, and use their own ID range so
they cannot collide with the dev or test splits. Several deliberately make the structured flags
(evidence_conflict, merchant_response, ...) disagree with what the free text suggests, because
that is where a rule engine and a careful human are most likely to differ.
"""
import csv
import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
ORDER = date(2026, 6, 1)


def iso(d):
    return d.isoformat()


def inr(cid, pattern, item, amount, tracking, proof, addr, merch, days_past, buyer, merchant,
        prior=0, conflict=False, dup=False, be=(), me=(), filed_after_order=None):
    promised = ORDER + timedelta(days=7)
    filed = ORDER + timedelta(days=filed_after_order) if filed_after_order else promised + timedelta(days=days_past)
    return {
        "case_id": cid, "case_type": "INR", "item": item, "order_amount": amount,
        "order_date": iso(ORDER), "filed_date": iso(filed), "promised_delivery_date": iso(promised),
        "tracking_status": tracking, "delivery_proof": proof, "address_match": addr,
        "merchant_response": merch, "prior_buyer_disputes_90d": prior, "return_status": "not_required",
        "is_duplicate": dup, "evidence_conflict": conflict,
        "buyer_statement": buyer, "merchant_statement": merchant,
        "buyer_evidence": list(be), "merchant_evidence": list(me),
    }, pattern


def snad(cid, pattern, item, amount, issue, merch, buyer, merchant, days_past=10, desc=True, photo=True,
         listing_match=False, packaging="none", ret="received", prior=0, conflict=False,
         be=(), me=(), tracking="delivered", proof="photo"):
    promised = ORDER + timedelta(days=7)
    return {
        "case_id": cid, "case_type": "SNAD", "item": item, "order_amount": amount,
        "order_date": iso(ORDER), "filed_date": iso(promised + timedelta(days=days_past)),
        "promised_delivery_date": iso(promised), "tracking_status": tracking, "delivery_proof": proof,
        "address_match": True, "merchant_response": merch, "prior_buyer_disputes_90d": prior,
        "return_status": ret, "is_duplicate": False, "evidence_conflict": conflict,
        "snad_issue": issue, "buyer_description_provided": desc, "buyer_photo_provided": photo,
        "listing_matches_item": listing_match, "packaging_evidence": packaging,
        "buyer_statement": buyer, "merchant_statement": merchant,
        "buyer_evidence": list(be), "merchant_evidence": list(me),
    }, pattern


CASES = [
    # --- signature present but credible counter-claim ---
    inr("GS-001", "signature_counterclaim", "headphones", 240, "delivered", "signature", True, "provided", 4,
        "The signature is not mine. I work nights and my building has a doorman who logs every parcel. Nothing was logged.",
        "Delivered and signed for. Carrier record attached.", conflict=True,
        be=["doorman log with no parcel entry"], me=["carrier signature record"]),
    inr("GS-002", "signature_counterclaim", "blender", 45, "delivered", "signature", True, "provided", 3,
        "A neighbour says the driver left it with them and they signed. I never received it from them and they now say they cannot find it.",
        "Delivered and signed for.", conflict=False,
        be=["message from neighbour"], me=["carrier signature record"]),
    inr("GS-003", "signature_counterclaim", "monitor", 1350, "delivered", "photo", True, "provided", 5,
        "The delivery photo shows a door that is not mine. My house number is 14 and the photo shows number 41.",
        "Carrier photo shows the parcel at the delivery address.", conflict=True,
        be=["photo of my front door"], me=["carrier delivery photo"]),
    # --- merchant replies late ---
    inr("GS-004", "late_merchant_reply", "desk lamp", 80, "none", "none", "unknown", "provided", 9,
        "Nothing has arrived after more than a week past the promised date.",
        "Sorry for the delay. Shipped yesterday, tracking number to follow. (sent 3 days after the buyer filed)", me=["shipping label created after claim"]),
    inr("GS-005", "late_merchant_reply", "yoga mat", 60, "in_transit", "none", "unknown", "provided", 15,
        "Fifteen days late and the parcel is still moving around.",
        "It is on its way, carrier delayed. (sent after the buyer filed)", me=["carrier tracking page"]),
    snad("GS-006", "late_merchant_reply", "tablet", 150, "attribute_mismatch", "provided",
         "Listed as 128GB but the device reports 64GB.",
         "Spec sheet attached shows 128GB. (reply arrived after the buyer filed)", me=["spec sheet"], be=["photo of storage screen"]),
    # --- tracking contradicts the buyer's dates ---
    inr("GS-007", "tracking_vs_dates", "jacket", 95, "delivered", "none", True, "none", 2,
        "Ordered on 1 June for delivery by 8 June. It never came.",
        "Tracking shows delivery on 5 June, before the promised date.", me=["tracking history"]),
    inr("GS-008", "tracking_vs_dates", "keyboard", 120, "in_transit", "none", True, "provided", -2,
        "The seller emailed me promising delivery by 3 June. It is now 6 June and I have nothing.",
        "Our promised date on the order is 8 June. Parcel is in transit.", me=["order confirmation showing 8 June"],
        be=["seller email promising 3 June"], conflict=False),
    inr("GS-009", "tracking_vs_dates", "camera lens", 70, "delivered", "signature", True, "provided", 0,
        "The first parcel was returned to the seller and they only re-shipped months later. The re-shipment never arrived.",
        "Original order delivered and signed for.", filed_after_order=200,
        be=["email thread about re-shipment"], me=["carrier signature record"]),
    # --- repeat disputers with strong proof ---
    inr("GS-010", "repeat_disputer", "sneakers", 55, "delivered", "photo", True, "provided", 3,
        "I never received this order.", "Delivered, photo attached.", prior=4, me=["carrier delivery photo"]),
    inr("GS-011", "repeat_disputer", "tablet case", 300, "delivered", "signature", True, "provided", 6,
        "Parcel was stolen from my porch shortly after delivery. I filed a police report.",
        "Signed for at the delivery address.", prior=3, be=["police report reference"], me=["carrier signature record"]),
    inr("GS-012", "repeat_disputer", "mug set", 35, "delivered", "signature", True, "provided", 4,
        "Did not receive it.", "Signed for.", prior=2, me=["carrier signature record"]),
    # --- high value with partial evidence ---
    inr("GS-013", "high_value_partial", "laptop", 2100, "none", "none", "unknown", "none", 8,
        "Nothing has arrived and the seller does not answer.", "(no response)"),
    snad("GS-014", "high_value_partial", "camera", 1800, "wrong_item", "none",
         "I received a different camera model from the one listed.", "(no response)", ret="not_shipped",
         be=["photo of received camera", "photo of listing"]),
    snad("GS-015", "high_value_partial", "e-bike battery", 1200, "damaged", "none",
         "Battery casing cracked on arrival.", "(no response)", ret="received",
         be=["photo of cracked casing"]),
    # --- vague text that looks like SNAD but is remorse ---
    snad("GS-016", "remorse", "sweater", 65, "subjective_only", "provided",
         "Not what I expected. The colour looks different on me.",
         "Item matches the listing exactly.", listing_match=True, be=["photo of item"], me=["listing snapshot"]),
    snad("GS-017", "remorse", "sofa cover", 90, "subjective_only", "provided",
         "The fabric feels cheap, nothing like the listing photos.",
         "Item matches the listing.", listing_match=False, be=["photo of fabric", "photo of listing"]),
    snad("GS-018", "remorse", "speaker", 40, "subjective_only", "none",
         "Not as good as I hoped.", "(no response)", photo=False, desc=True),
    # --- plausible but conflicting evidence ---
    snad("GS-019", "conflicting_evidence", "ceramic vase", 420, "damaged", "provided",
         "Arrived in pieces.", "Packed in double-walled box with foam, packing photos attached.",
         packaging="sound", conflict=True, be=["photo of broken vase"], me=["packing photos"]),
    snad("GS-020", "conflicting_evidence", "glass jug", 75, "damaged", "provided",
         "There is a crack along the base.", "Packed with bubble wrap, shipped intact, so any crack happened after delivery.",
         packaging="sound", conflict=False, be=["photo of cracked jug"], me=["packing photos"]),
    inr("GS-021", "conflicting_evidence", "perfume", 150, "delivered", "signature", False, "provided", 5,
        "Delivered to a wrong address. I live at 22 Elm Street.",
        "Signed by the buyer at the confirmed address.", conflict=True,
        be=["proof of address"], me=["carrier signature record"]),
    # --- missing or malformed ---
    inr("GS-022", "missing_fields", "backpack", 110, "delivered", "signature", "unknown", "none", 4,
        "Never received.", "(no response)"),
    inr("GS-023", "missing_fields", "phone charger", 85, "none", "none", True, "none", 4,
        "No tracking, nothing here.", "(no response)"),
    inr("GS-024", "missing_fields", "drone", 500, "none", "none", True, "none", 10,
        "The tracking number I was given shows delivered, but nothing arrived.",
        "(no response)", be=["screenshot of tracking page showing delivered"]),
    # --- counterfeit ---
    snad("GS-025", "counterfeit", "designer handbag", 650, "counterfeit", "provided",
         "The stitching and logo look fake. I think it is counterfeit.", "Genuine item, supplier invoice available.",
         conflict=False, be=["photos of logo and stitching"], me=["supplier invoice"]),
]


def card(case):
    lines = [f"## {case['case_id']}  ({case['case_type']})", ""]
    past = (date.fromisoformat(case["filed_date"]) - date.fromisoformat(case["promised_delivery_date"])).days
    since = (date.fromisoformat(case["filed_date"]) - date.fromisoformat(case["order_date"])).days
    lines += [f"- item: {case['item']}   amount: {case['order_amount']}",
              f"- order {case['order_date']} | promised {case['promised_delivery_date']} | filed {case['filed_date']}"
              f"  (filed {since} days after order, {past:+d} days vs promised)",
              f"- tracking: {case['tracking_status']} | delivery proof: {case['delivery_proof']} | address match: {case['address_match']}",
              f"- merchant response: {case['merchant_response']} | prior buyer disputes (90d): {case['prior_buyer_disputes_90d']}",
              f"- return status: {case['return_status']} | duplicate: {case['is_duplicate']} | evidence conflict flag: {case['evidence_conflict']}"]
    if case["case_type"] == "SNAD":
        lines.append(f"- SNAD: issue={case['snad_issue']} | description provided={case['buyer_description_provided']} | "
                     f"photo provided={case['buyer_photo_provided']} | listing matches item={case['listing_matches_item']} | "
                     f"packaging evidence={case['packaging_evidence']}")
    lines += ["", f"Buyer: \"{case['buyer_statement']}\"", f"Buyer evidence: {case['buyer_evidence'] or 'none'}", "",
              f"Merchant: \"{case['merchant_statement']}\"", f"Merchant evidence: {case['merchant_evidence'] or 'none'}", "", "---", ""]
    return "\n".join(lines)


def main():
    built = list(CASES)
    ids = [c["case_id"] for c, _ in built]
    assert len(ids) == len(set(ids))
    (ROOT / "gold").mkdir(exist_ok=True)
    with open(ROOT / "data/gold_cases.jsonl", "w", encoding="utf-8") as f:
        for c, _ in built:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(ROOT / "data/gold_meta.jsonl", "w", encoding="utf-8") as f:
        for c, pattern in built:
            f.write(json.dumps({"case_id": c["case_id"], "pattern": pattern}) + "\n")
    header = ("# Gold set cards (blind labeling)\n\nLabel each case in gold/label_sheet.csv. Do not run the "
              "rulebook code or read rulebook.py while labeling. Use the rulebook document and your own judgment.\n\n---\n\n")
    (ROOT / "gold/cards.md").write_text(header + "".join(card(c) for c, _ in built), encoding="utf-8")
    sheet = ROOT / "gold/label_sheet.csv"
    if sheet.exists():
        print(f"{sheet} already exists; not overwriting your labels")
    else:
        with open(sheet, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["case_id", "outcome", "rule_id", "confidence", "notes"])
            for c, _ in built:
                w.writerow([c["case_id"], "", "", "", ""])
    print(f"wrote {len(built)} cases")


if __name__ == "__main__":
    main()
