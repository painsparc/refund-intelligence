# Evaluation report (auto-generated)

Generated 2026-10-22T07:33:36 by `python eval_data.py evaluate`. Every number below is computed from the data actually loaded; components without data say NOT RUN.


## 1. Deterministic policy gate on real Olist orders

*Data:* Olist (real order timestamps, categories, amounts) + ASSUMED policy + synthetic claim timing

- **orders**: 1500
- **checks**: 9000
- **agreement**: 1.0
- **disagreements_sample**: []

> validates the deterministic gate implementation on real timestamps; says nothing about policy realism


## 2. Behavioural features / abuse rules on UCI return-like behaviour (unlabelled)

*Data:* UCI Online Retail II cancellation invoices as RETURN-LIKE PROXY events (unlabelled, not fraud)

- **customers_with_cancellations_evaluated**: 1230

**pattern_flag_counts**

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation |
|---|---|---|---|---|
| 68 | 0 | 0 | 2 | 0 |


**pattern_flag_rate**

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation |
|---|---|---|---|---|
| 0.0553 | 0.0 | 0.0 | 0.0016 | 0.0 |


**behaviour_risk_0_100**

| p50 | p90 | p99 | share_ge_50 |
|---|---|---|---|
| 15.1 | 36.7 | 45.9 | 0.0008 |


**refunds_90d**

| p50 | p90 | max |
|---|---|---|
| 1 | 2 | 23 |


**refund_rate**

| p50 | p90 |
|---|---|
| 0.3333 | 1.0 |


> flag RATES on real, unlabelled behaviour - NOT precision/recall/FPR. UCI has no delivery dates, so timing patterns cannot fire; buyers are largely wholesalers. Cancellations are not confirmed refunds.


## 3. Evidence reuse detection on INRIA Copydays (source ground truth)

**NOT RUN** - Copydays not ingested


## 3b. Copydays: thresholds tuned on a validation split, reported on a held-out test split

**NOT RUN** - Copydays not ingested


## 4. Synthetic customers from independent behavioural profiles (SYNTHETIC labels)

*Data:* SYNTHETIC labelled customers drawn from independent behavioural profiles, real order attributes where available


**profiles**


`normal`

| abusive | n | intervention_rate | any_pattern_flag_rate |
|---|---|---|---|
| 0 | 20 | 0.0 | 0.1 |


`heavy_legit`

| abusive | n | intervention_rate | any_pattern_flag_rate |
|---|---|---|---|
| 0 | 20 | 0.0 | 0.05 |


`serial_refunder`

| abusive | n | intervention_rate | any_pattern_flag_rate | target_pattern | target_pattern_recall |
|---|---|---|---|---|---|
| 1 | 20 | 0.05 | 0.3 | serial_refunder | 0.2 |


`refund_farmer`

| abusive | n | intervention_rate | any_pattern_flag_rate | target_pattern | target_pattern_recall |
|---|---|---|---|---|---|
| 1 | 20 | 0.05 | 0.75 | refund_farming | 0.7 |


`policy_exploiter`

| abusive | n | intervention_rate | any_pattern_flag_rate | target_pattern | target_pattern_recall |
|---|---|---|---|---|---|
| 1 | 20 | 0.05 | 0.65 | policy_exploitation | 0.6 |


`return_abuser`

| abusive | n | intervention_rate | any_pattern_flag_rate | target_pattern | target_pattern_recall |
|---|---|---|---|---|---|
| 1 | 20 | 0.0 | 0.3 | return_abuse | 0.3 |


`evidence_reuse`

| abusive | n | intervention_rate | any_pattern_flag_rate | reuse_flag_recall | detected_by_orb_only |
|---|---|---|---|---|---|
| 1 | 20 | 0.1 | 0.95 | 0.95 | 0.35 |

  recall_by_transformation: blur=1.0, crop=1.0, jpeg=1.0, resize=1.0, strong=0.75


**false_flags_on_legitimate_profiles**


`normal`

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation | any_reuse_flag |
|---|---|---|---|---|---|
| 0.0 | 0.0 | 0.1 | 0.0 | 0.0 | 0.1 |


`heavy_legit`

| serial_refunder | return_abuse | fake_defect_pattern | refund_farming | policy_exploitation | any_reuse_flag |
|---|---|---|---|---|---|
| 0.0 | 0.0 | 0.0 | 0.05 | 0.0 | 0.0 |


**pattern_flag_as_classifier (any abuse pattern or evidence-reuse flag)**

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 |
|---|---|---|---|---|---|---|---|
| 59 | 3 | 41 | 37 | 0.952 | 0.59 | 0.075 | 0.728 |


**decision_as_classifier_vs_cost_scale**


`handling/friction cost x1.0`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 |
|---|---|---|---|---|---|---|---|
| 5 | 0 | 95 | 40 | 1.0 | 0.05 | 0.0 | 0.095 |


`handling/friction cost x0.5`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 |
|---|---|---|---|---|---|---|---|
| 10 | 0 | 90 | 40 | 1.0 | 0.1 | 0.0 | 0.182 |


`handling/friction cost x0.2`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 |
|---|---|---|---|---|---|---|---|
| 29 | 2 | 71 | 38 | 0.935 | 0.29 | 0.05 | 0.443 |


`handling/friction cost x0.1`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 |
|---|---|---|---|---|---|---|---|
| 55 | 5 | 45 | 35 | 0.917 | 0.55 | 0.125 | 0.687 |


> the generator describes HOW customers behave and never references a detection threshold, so the system can miss abusers (recall < 1) and flag heavy genuine customers (false positives > 0). Still synthetic: it shows how the pipeline separates profiles, not real-world fraud performance. Decisions depend on the cost matrix scale (handling 100, limit 300 here); see the table by cost scale and set COST_SCALE for the deployment.


## 4b. Expected-cost layer: sweep over COST_SCALE (SYNTHETIC labels)

*Data:* SYNTHETIC labelled scenarios run through the real graph at each COST_SCALE (see docs/cost_calibration.md)


**by_cost_scale**


`x1.0`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 | action_mix | manual_review_share |
|---|---|---|---|---|---|---|---|---|---|
| 5 | 0 | 95 | 40 | 1.0 | 0.05 | 0.0 | 0.095 | auto_approve=96%, refund_after_return=4% | 0.0 |


`x0.2`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 | action_mix | manual_review_share |
|---|---|---|---|---|---|---|---|---|---|
| 29 | 2 | 71 | 38 | 0.935 | 0.29 | 0.05 | 0.443 | auto_approve=78%, refund_after_return=11%, request_evidence=1%, manual_review=10% | 0.1 |


`x0.1`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 | action_mix | manual_review_share |
|---|---|---|---|---|---|---|---|---|---|
| 55 | 5 | 45 | 35 | 0.917 | 0.55 | 0.125 | 0.687 | auto_approve=57%, refund_after_return=16%, request_evidence=1%, manual_review=26% | 0.257 |


`x0.05`

| tp | fp | fn | tn | precision | recall | false_positive_rate | f1 | action_mix | manual_review_share |
|---|---|---|---|---|---|---|---|---|---|
| 73 | 10 | 27 | 30 | 0.88 | 0.73 | 0.25 | 0.798 | auto_approve=41%, refund_after_return=15%, request_evidence=1%, manual_review=43% | 0.429 |


**recommended_cost_scale**

| scale | rule | precision | recall | false_positive_rate | action_mix |
|---|---|---|---|---|---|
| 0.1 | best recall among scales with false-positive rate <= 0.15 | 0.917 | 0.55 | 0.125 | auto_approve=57%, refund_after_return=16%, request_evidence=1%, manual_review=26% |


**break_even_claim_amount_for_manual_review_vs_auto_approve**


`exposure risk 0.2`

| x1.0 | x0.2 | x0.1 | x0.05 |
|---|---|---|---|
| 649.4 | 129.9 | 64.9 | 32.5 |


`exposure risk 0.4`

| x1.0 | x0.2 | x0.1 | x0.05 |
|---|---|---|---|
| 279.3 | 55.9 | 27.9 | 14.0 |


`exposure risk 0.6`

| x1.0 | x0.2 | x0.1 | x0.05 |
|---|---|---|---|
| 177.9 | 35.6 | 17.8 | 8.9 |


`exposure risk 0.8`

| x1.0 | x0.2 | x0.1 | x0.05 |
|---|---|---|---|
| 130.5 | 26.1 | 13.1 | 6.5 |


**scenario_claim_amounts**

| abusive_median | legitimate_median |
|---|---|
| 71.96 | 58.31 |


> Costs here are in the synthetic claims' currency units; choose COST_SCALE from the deployment's real claim sizes and review capacity, not from these synthetic labels alone. Exposure risk is the 0-1 proxy (0.6 CR + 0.4 BR)/100, so an abusive claim only changes the decision when amount x risk is large enough to beat the handling cost. A recall of 0 at x1.0 means 'every synthetic claim was too small to justify a 100-unit review at its risk', not that the detectors found nothing: see the pattern-flag classifier in component 4. Set COST_SCALE via the environment; defaults are unchanged.


## 5. Claim NLP + sentiment on the hand-labelled set

*Data:* hand-labelled claim texts (see data/README.md for who labelled them)


**sets**


`developer-authored set`

| n | legacy substring keywords | category_accuracy | legacy substring keywords | category_macro_F1 | legacy substring keywords | accuracy_negation_cases | legacy substring keywords | accuracy_paraphrase_cases | legacy substring keywords | accuracy_hard_cases | legacy substring keywords | unsupported-claim detection precision | legacy substring keywords | unsupported-claim detection recall | rules (negation-aware, current default) | category_accuracy | rules (negation-aware, current default) | category_macro_F1 | rules (negation-aware, current default) | accuracy_negation_cases | rules (negation-aware, current default) | accuracy_paraphrase_cases | rules (negation-aware, current default) | accuracy_hard_cases | rules (negation-aware, current default) | unsupported-claim detection precision | rules (negation-aware, current default) | unsupported-claim detection recall | sentiment | backend | sentiment | accuracy_3class | sentiment | recall_of_negative | sentiment | negative_reviews_missed | sentiment | false_negative_alarms |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 60 | 0.45 | 0.519 | 0.091 | 0.0 | 0.214 | 0.303 | 0.625 | 0.733 | 0.77 | 0.636 | 1.0 | 0.214 | 0.577 | 0.938 | vader (English only) | 0.4 | 0.216 | 29 | 1 |

- **claim_nlp_backend_active**: rules (transformer not enabled / not loadable)

> the developer-authored set is a regression/sanity set, not an independent benchmark (the same person wrote the rules). A transformer backend is evaluated here only when CLAIM_NLP=transformer loads; text sentiment is English-only with the default lexicon. team labels of real reviews: file data\manual_labels_real.csv not found misclassified examples are listed in reports/nlp_error_analysis.json.


## 6. Olist reviews linked to real orders

*Data:* Olist order reviews (real text and scores) linked to real Olist orders

- **reviews**: 98465
- **linked_to_orders**: 98465
- **with_comment_text**: 40426

**score_distribution**

| 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|
| 10884 | 3089 | 8126 | 19109 | 57257 |

- **share_1_2_stars**: 0.142

**product_negative_review_outliers**


`97f1396a5a1f7c07ba51784efdec44b8`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 10 | 0.3 | 3.4 |


`2052b3c5e08893e567c2fb7778476c75`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 12 | 0.25 | 3.58 |


`642a40d4d73b4b2604968937de13634a`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 18 | 0.278 | 3.56 |


`6d2fde7d12bb6ff367dbda120ba8828e`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 11 | 0.818 | 1.73 |


`8338cef8355d238f43711dcb9c0657b2`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 21 | 0.333 | 3.57 |


**vendor_negative_review_outliers**


`3f8034bfc6b8b4135534975d49f04c63`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 12 | 0.25 | 3.75 |


`e64d65bc8dbec2accda90c58de5d1246`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 12 | 0.417 | 3.08 |


`8f54d64f28a993550aa7943f12572959`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 15 | 0.267 | 3.73 |


`610f72e407cdd7caaa2f8167b0163fd8`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 15 | 0.267 | 3.8 |


`86bb7c4b535e49a541baf3266b1c95b1`

| reviews | negative_review_rate | avg_score |
|---|---|---|
| 15 | 0.4 | 3.2 |


> Olist review text is Portuguese: the default sentiment lexicon and rule classifier are English-only, so text-level NLP is NOT evaluated on it (set SENTIMENT_MODEL / CLAIM_NLP_MODEL to multilingual models to do so). Review scores are real and are used for the product/vendor insights. A low review is not a refund claim, and it is never used as a fraud signal.


## 7. Amazon Reviews 2023 text/rating proxy

*Data:* Amazon Reviews 2023 (REVIEWS, not refund claims) - text/rating proxy


**sentiment_vs_rating**

| reviews_used | sign_agrees_with_rating | backend | label |
|---|---|---|---|
| 4499 | 0.894 | vader (English only) | WEAK label: star rating (<=2 negative, >=4 positive); not a human sentiment label |

- **reviews**: 5000
- **with_images**: 599
- **rating_le_2**: 636
- **rating_le_2_with_defect_words**: 31

> no refund labels exist here; review images are not refund evidence and are not in the evidence table
