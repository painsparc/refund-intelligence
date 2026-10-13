# Pipeline smoke run (FIXTURE data - not the approved datasets)

This is the auto-generated evaluation report from running `python eval_data.py all ...` on the tiny fixture files used by
`tests/test_eval_data.py` (38 Olist-layout orders, a handful of UCI-layout invoices, 3 Amazon-layout reviews, 6 synthetic Copydays-layout
image pairs, 12 synthetic scenarios per family) with the proposal-aligned risk model. It proves the pipeline runs end to end and shows what
the report looks like. **None of these numbers describe Olist, UCI, Amazon Reviews 2023 or Copydays.** Re-run on the real files for the real report.

What this run showed (details in `spec_alignment.md`):
1. At the default cost matrix (review handling cost 100) small abusive claims (~150 units) are auto-approved: reviewing them costs more than the
   exposure. The sensitivity table below shows the cost scale at which each pattern starts to be reviewed, and where legitimate near-threshold
   customers start to be over-reviewed. The cost matrix must be set to the deployment's currency / claim size.
2. Average-hash does not catch cropped re-uploads (JPEG / resize / blur are caught).

---

# Evaluation report (auto-generated)

Generated 2026-10-13T02:12:23 by `python eval_data.py evaluate`. Every number below is computed from the data actually loaded; components without data say NOT RUN.


## 1. Deterministic policy gate on real Olist orders

*Data:* Olist (real order timestamps, categories, amounts) + ASSUMED policy + synthetic claim timing

- **orders**: 35
- **checks**: 210
- **agreement**: 1.0
- **disagreements_sample**: []

> validates the deterministic gate implementation on real timestamps; says nothing about policy realism


## 2. Behavioural features / abuse rules on UCI return-like behaviour (unlabelled)

*Data:* UCI Online Retail II cancellation invoices as RETURN-LIKE PROXY events (unlabelled, not fraud)

- **customers_with_cancellations_evaluated**: 1

**pattern_flag_counts**

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation |
|---|---|---|---|---|
| 1 | 0 | 0 | 1 | 0 |


**pattern_flag_rate**

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation |
|---|---|---|---|---|
| 1.0 | 0.0 | 0.0 | 1.0 | 0.0 |


**behaviour_risk_0_100**

| p50 | p90 | p99 | share_ge_50 |
|---|---|---|---|
| 60.0 | 60.0 | 60.0 | 1.0 |


**refunds_90d**

| p50 | p90 | max |
|---|---|---|
| 6 | 6 | 6 |


**refund_rate**

| p50 | p90 |
|---|---|
| 1.0 | 1.0 |


> flag RATES on real, unlabelled behaviour - NOT precision/recall/FPR. UCI has no delivery dates, so timing patterns cannot fire; buyers are largely wholesalers. Cancellations are not confirmed refunds.


## 3. Evidence reuse detection on INRIA Copydays (source ground truth)

*Data:* INRIA Copydays (source-provided ground truth: variant -> original)

- **originals**: 6
- **variants**: 12
- **operating_threshold**: 5

**by_variant_type**


`crop`

| n | top1_accuracy | recall@hamming<=3 | recall@hamming<=5 | recall@hamming<=8 | recall@hamming<=12 | false_match_rate@hamming<=3 | false_match_rate@hamming<=5 | false_match_rate@hamming<=8 | false_match_rate@hamming<=12 |
|---|---|---|---|---|---|---|---|---|---|
| 6 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |


`jpeg`

| n | top1_accuracy | recall@hamming<=3 | recall@hamming<=5 | recall@hamming<=8 | recall@hamming<=12 | false_match_rate@hamming<=3 | false_match_rate@hamming<=5 | false_match_rate@hamming<=8 | false_match_rate@hamming<=12 |
|---|---|---|---|---|---|---|---|---|---|
| 6 | 1.0 | 1.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.0 |


**original_quality_score**

| p10 | p50 | p90 |
|---|---|---|
| 1.0 | 1.0 | 1.0 |


> average-hash is cheap but weak on crops / strong edits; the sweep shows the trade-off


## 4. Controlled abuse scenarios (SYNTHETIC labels)

*Data:* SYNTHETIC labelled scenarios (seeded) on top of real order attributes where available


**scenarios**


`serial_refunder`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate | target_detection_rate |
|---|---|---|---|---|---|
| abusive_claim | 12 | 0.0 | 1.0 | 1.0 | 1.0 |


`refund_farming`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate | target_detection_rate |
|---|---|---|---|---|---|
| abusive_claim | 12 | 0.0 | 1.0 | 1.0 | 1.0 |


`policy_exploitation`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate | target_detection_rate |
|---|---|---|---|---|---|
| abusive_claim | 12 | 0.0 | 1.0 | 1.0 | 1.0 |


`legit_control`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate |
|---|---|---|---|---|
| legitimate_claim | 12 | 0.0 | 1.0 | 0.0 |


`near_miss_serial`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate | wrongly_flagged_serial_refunder_rate |
|---|---|---|---|---|---|
| legitimate_claim | 12 | 0.0 | 1.0 | 0.0 | 0.0 |


`evidence_reuse`

| label | n | intervention_rate(not auto-approved) | auto_approve_rate | any_abuse_flag_rate | target_detection_rate |
|---|---|---|---|---|---|
| reused_evidence | 12 | 0.0 | 1.0 | 0.75 | 0.75 |

  detection_by_transformation: jpeg=1.0, resize=1.0, crop=0.0, blur=1.0


**intervention_rate_vs_cost_scale**


`handling/friction cost x1.0`

| serial_refunder | refund_farming | policy_exploitation | legit_control | near_miss_serial | evidence_reuse |
|---|---|---|---|---|---|
| 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |


`handling/friction cost x0.5`

| serial_refunder | refund_farming | policy_exploitation | legit_control | near_miss_serial | evidence_reuse |
|---|---|---|---|---|---|
| 1.0 | 1.0 | 0.0 | 0.0 | 0.0 | 0.1667 |


`handling/friction cost x0.2`

| serial_refunder | refund_farming | policy_exploitation | legit_control | near_miss_serial | evidence_reuse |
|---|---|---|---|---|---|
| 1.0 | 1.0 | 1.0 | 0.0 | 0.0 | 0.5 |


`handling/friction cost x0.1`

| serial_refunder | refund_farming | policy_exploitation | legit_control | near_miss_serial | evidence_reuse |
|---|---|---|---|---|---|
| 1.0 | 1.0 | 1.0 | 0.0 | 1.0 | 0.75 |


> synthetic and rule-aligned by construction: this verifies the pipeline end to end and the evidence-reuse robustness; it is NOT an estimate of real-world fraud detection performance. The expected-cost matrix is in ONE currency unit (handling 100, auto-approve limit 300 here): small claims are auto-approved when review costs more than the exposure, so set COST_SCALE to the deployment's currency/claim size (see the sensitivity table).


## 5. Amazon Reviews 2023 text/rating proxy

*Data:* Amazon Reviews 2023 (REVIEWS, not refund claims) - text/rating proxy

- **reviews**: 3
- **with_images**: 1
- **rating_le_2**: 2
- **rating_le_2_with_defect_words**: 1

> no refund labels exist here; review images are not refund evidence and are not in the evidence table
