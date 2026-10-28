# Gold set cards (blind labeling)

Label each case in gold/label_sheet.csv. Do not run the rulebook code or read rulebook.py while labeling. Use the rulebook document and your own judgment.

---

## GS-001  (INR)

- item: headphones   amount: 240
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-12  (filed 11 days after order, +4 days vs promised)
- tracking: delivered | delivery proof: signature | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: True

Buyer: "The signature is not mine. I work nights and my building has a doorman who logs every parcel. Nothing was logged."
Buyer evidence: ['doorman log with no parcel entry']

Merchant: "Delivered and signed for. Carrier record attached."
Merchant evidence: ['carrier signature record']

---
## GS-002  (INR)

- item: blender   amount: 45
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-11  (filed 10 days after order, +3 days vs promised)
- tracking: delivered | delivery proof: signature | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "A neighbour says the driver left it with them and they signed. I never received it from them and they now say they cannot find it."
Buyer evidence: ['message from neighbour']

Merchant: "Delivered and signed for."
Merchant evidence: ['carrier signature record']

---
## GS-003  (INR)

- item: monitor   amount: 1350
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-13  (filed 12 days after order, +5 days vs promised)
- tracking: delivered | delivery proof: photo | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: True

Buyer: "The delivery photo shows a door that is not mine. My house number is 14 and the photo shows number 41."
Buyer evidence: ['photo of my front door']

Merchant: "Carrier photo shows the parcel at the delivery address."
Merchant evidence: ['carrier delivery photo']

---
## GS-004  (INR)

- item: desk lamp   amount: 80
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-17  (filed 16 days after order, +9 days vs promised)
- tracking: none | delivery proof: none | address match: unknown
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "Nothing has arrived after more than a week past the promised date."
Buyer evidence: none

Merchant: "Sorry for the delay. Shipped yesterday, tracking number to follow. (sent 3 days after the buyer filed)"
Merchant evidence: ['shipping label created after claim']

---
## GS-005  (INR)

- item: yoga mat   amount: 60
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-23  (filed 22 days after order, +15 days vs promised)
- tracking: in_transit | delivery proof: none | address match: unknown
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "Fifteen days late and the parcel is still moving around."
Buyer evidence: none

Merchant: "It is on its way, carrier delayed. (sent after the buyer filed)"
Merchant evidence: ['carrier tracking page']

---
## GS-006  (SNAD)

- item: tablet   amount: 150
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-18  (filed 17 days after order, +10 days vs promised)
- tracking: delivered | delivery proof: photo | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: received | duplicate: False | evidence conflict flag: False
- SNAD: issue=attribute_mismatch | description provided=True | photo provided=True | listing matches item=False | packaging evidence=none

Buyer: "Listed as 128GB but the device reports 64GB."
Buyer evidence: ['photo of storage screen']

Merchant: "Spec sheet attached shows 128GB. (reply arrived after the buyer filed)"
Merchant evidence: ['spec sheet']

---
## GS-007  (INR)

- item: jacket   amount: 95
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-10  (filed 9 days after order, +2 days vs promised)
- tracking: delivered | delivery proof: none | address match: True
- merchant response: none | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "Ordered on 1 June for delivery by 8 June. It never came."
Buyer evidence: none

Merchant: "Tracking shows delivery on 5 June, before the promised date."
Merchant evidence: ['tracking history']

---
## GS-008  (INR)

- item: keyboard   amount: 120
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-06  (filed 5 days after order, -2 days vs promised)
- tracking: in_transit | delivery proof: none | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "The seller emailed me promising delivery by 3 June. It is now 6 June and I have nothing."
Buyer evidence: ['seller email promising 3 June']

Merchant: "Our promised date on the order is 8 June. Parcel is in transit."
Merchant evidence: ['order confirmation showing 8 June']

---
## GS-009  (INR)

- item: camera lens   amount: 70
- order 2026-06-01 | promised 2026-06-08 | filed 2026-12-18  (filed 200 days after order, +193 days vs promised)
- tracking: delivered | delivery proof: signature | address match: True
- merchant response: provided | prior buyer disputes (90d): 0
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "The first parcel was returned to the seller and they only re-shipped months later. The re-shipment never arrived."
Buyer evidence: ['email thread about re-shipment']

Merchant: "Original order delivered and signed for."
Merchant evidence: ['carrier signature record']

---
## GS-010  (INR)

- item: sneakers   amount: 55
- order 2026-06-01 | promised 2026-06-08 | filed 2026-06-11  (filed 10 days after order, +3 days vs promised)
- tracking: delivered | delivery proof: photo | address match: True
- merchant response: provided | prior buyer disputes (90d): 4
- return status: not_required | duplicate: False | evidence conflict flag: False

Buyer: "I never received this order."
Buyer evidence: none

Merchant: "Delivered, photo attached."
Merchant evidence: ['carrier delivery photo']

---
