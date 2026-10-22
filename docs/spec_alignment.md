# Proposal-to-code alignment (the final proposal is the canonical specification)

Source documents: *Final Submission* proposal (sections 11-15, 19, 25) and the Phase 1 Review Report. The code now implements the
proposal's model; each row says where, and what is a deliberate deviation or still open.

| Proposal element | Implementation | Status |
|---|---|---|
| Behavioural Risk 0-100 = 0.35 freq + 0.25 value ratio + 0.15 repeat reason + 0.15 return-event exception + 0.10 evidence reuse | `behaviour_risk_formula`, weights `BEHAV_W` | **done**, pinned by tests |
| Trust Index = 100 - Behavioural Risk (supporting only) | `trust_index`; stored in `customer_risk_profile`, shown in API/Streamlit | **done** |
| Blacklist = hard signal, not a weighted score | guardrail -> manual review; not in the formula | **done** |
| Claim Risk 0-100 = 0.30 ambiguity + 0.30 evidence inconsistency + 0.20 evidence reuse + 0.20 abuse pattern strength | `interpret_claim`, `evidence_inconsistency`, `claim_risk_formula`, `synthesize_risk` | **done** |
| Rolling 30/90/180-day features | `build_features` (counts, values, value ratio, repeat reasons, returns) | **done** |
| Return-event compliance | `return_events` table + `record_return` + feature; used by `return_abuse` | **done in code**; Olist/UCI contain no return events, so on those datasets it is 0 and reported as "no coverage" |
| Five abuse patterns with rules on 30/90/180-day features | `detect_abuse_patterns` | **done** (thresholds are fixed rules; percentile thresholds are Phase 2) |
| Expected cost = RiskExposure + CustomerFriction + Handling over five actions (immediate refund, refund after return, request evidence, manual review, policy decline) | `cost_table`, `eligible_actions`, `expected_cost_decision`; breakdown stored and shown | **done** |
| Deterministic guardrail; AI cannot override policy; AI failure -> manual review | guardrails in `expected_cost_decision`; `fallback_manual_review` in `graph.py` | **done**, tested |
| Customer_Risk_Profile, Evidence, Risk_Assessment, Product/Vendor snapshots, Insight_Log tables | `customer_risk_profile`, `evidence`, `refund_requests` (+ risk columns), snapshots, `insight_log` | **done** (names follow the code; see the mapping in `README`) |
| 0-100 scale in backend, Streamlit, report | all scores 0-100; exposure risk is 0-1 and labelled as a proxy | **done in code**; the report and diagrams must say the same |

## Mentor review feedback - status
| Feedback | What was done | Status |
|---|---|---|
| Claim interpretation is keyword-based, misses negation and paraphrase; use a transformer classifier | negation-aware rules (default, measured on the labelled set); **optional zero-shot transformer backend** (`CLAIM_NLP=transformer`) with fallback | rules done and measured; **transformer path written but never run with real weights** (no model-hub access here) |
| Add a text-image consistency check (CLIP) | optional CLIP backend (`IMAGE_TEXT=clip`) feeding EvidenceInconsistency, flag `image_text_mismatch`; `validate_backends.py` evaluates it on labelled pairs | **written, tested with a stand-in model; real CLIP not run (no model-hub access) - run `validate_backends.py`** |
| Sentiment for customer-experience routing and product insights, not fraud | `sentiment`, `cx_priority` routing, per-product average sentiment; a test proves no score/cost/decision changes | done (VADER default is weak on factual complaints - see report) |
| Average hash misses crops; use pHash or embeddings | aHash + pHash (DCT) + ORB local features with RANSAC; Copydays evaluation with a validation/test split of the originals (`eval_copydays_split`) | done in code and fixture tests; **real Copydays not run**; thresholds still calibrated on synthetic images only |
| Use the Olist reviews file linked to real orders | ingested and linked; link audit explains every unlinked review; used for product/vendor negative-review insights | done in code and fixture tests; **not run on the real file** |
| Small manually labelled test set | 60 labelled claim sentences + two-annotator workflow (`label_tools.py`: blind split, Cohen's kappa, reconciliation; `docs/labelling_guidelines.md`) | developer set is **not independent**; the independent labels **still need two people** to label (tooling and criteria done and tested) |
| Synthetic generator independent of the detection rules | behavioural profiles (Poisson/Beta), no thresholds referenced, test-enforced | done; results now non-circular (see findings) |
| Run the evaluation on the real datasets and share results | `scripts/get_datasets.py` (verified URLs, SHA-256 record) + `scripts/run_full_pipeline.py --require-real` | **not done: the real datasets could not be downloaded in the build environment (only PyPI is reachable); run the two commands on a machine with internet** |

## Deliberate deviations (decide as a team, then make the report say the same thing)
1. **Agent names.** Proposal: Claim Interpretation / Evidence Intelligence / Decision Intelligence Agent. Code and diagram: **Evidence & Claim Agent**
   (claim interpretation + evidence intelligence merged), **Behaviour & Abuse Agent**, **Resolution Reasoning Agent**. The proposal itself treats behaviour
   risk and abuse rules as deterministic engines; in the code the Behaviour & Abuse Agent wraps those engines and only adds an optional bounded
   interpretation. Recommendation: adopt the code/diagram names in the proposal.
2. **Who picks the action.** Proposal: the Decision Intelligence Agent "selects" the action. Code: the deterministic expected-cost engine selects (arithmetic must not
   depend on an LLM); the Resolution Reasoning Agent explains it and may only *escalate* to a more cautious action. Safer; report text should match.
3. **Exposure risk mix.** The proposal does not say how Claim Risk and Behavioural Risk become one risk for RiskExposure. Prototype choice: 0.6 CR + 0.4 BR (`EXPOSURE_W`).
4. **Cost matrix numbers.** The proposal asks for a configurable matrix; the constants in `intelligence.py` are prototype settings, not calibrated values.
5. **Not implemented (Phase 2 in the proposal anyway):** Isolation-Forest comparison, percentile-based thresholds, calibrated probabilities, learned classifier.

## Findings from the evaluation (fixture run in `pipeline_smoke_run.md`; **re-run on the real data**)
1. **Rules miss many synthetic abusers once the generator no longer mirrors them.** Pooled target-pattern recall is well below 100% (e.g. serial refunders drawn at
   about 10 refunds a year often have fewer than 4 in 90 days) while genuine heavy customers are rarely flagged. Whether the thresholds suit real behaviour is unknown.
2. **The cost matrix has a currency scale.** At the default scale (review cost 100) small claims are auto-approved; the decision-level precision/recall table by
   `COST_SCALE` shows the trade-off. Set the scale from real claim sizes.
3. **Negation handling is a real gain but hard cases remain.** On the labelled set the old substring matcher scored 45% category accuracy (9% on negation cases);
   the negation-aware rules 73% (64% on negation, about 21% on hard paraphrases). The 100% on 'paraphrase' cases is a sanity check only: those are phrasings the patterns were written for. Hard paraphrases are what a transformer should fix - not yet measured.
4. **Cost-matrix scale** (see `docs/cost_calibration.md`): the "zero true positives" smoke result was a currency-scale mismatch (review cost 100 vs median claim ~85), not a detector failure; on the real-Olist-attribute run COST_SCALE 0.1 gives precision 0.93 / recall 0.54 / false-positive rate 0.10 (22 % of claims to review). The real review cost and capacity must set the scale.
5. **Backend decision (real weights run on 2026-10-22, `docs/backend_validation.md`, developer-authored 60-row set - not independent):**
   * **Sentiment: adopt the multilingual transformer** (`SENTIMENT_MODEL=cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual`): 3-class accuracy 0.58 vs 0.40 and
     negative recall 0.76 vs 0.22 for VADER (29 -> 9 missed negative texts). It only drives customer-experience routing and product insights, never a score, cost or decision.
   * **Claim reason: keep the rules as the default.** The zero-shot DeBERTa scored 0.63 category accuracy vs 0.73 for the rules, and was worse on negation (0.55 vs 0.64);
     it was better on hard cases (0.43 vs 0.21). The rules' 100% on paraphrases is a sanity check only (the patterns were written for them). Re-decide on the independent labels.
   * **CLIP: not validated** (needs `data/image_text_pairs.csv`).
   * The `no-such-org/...` lines printed by `validate_backends.py` are the deliberate fallback test, not errors.
6. **Lexicon sentiment is weak on factual complaints** ("the jar arrived cracked" is neutral to VADER): 3-class accuracy about 40% on the labelled set; only strongly
   worded complaints trigger `cx_priority`. A transformer sentiment model is the upgrade.
7. **ORB catches crops that the hashes miss** (aHash/pHash 0% on crops; ORB about 100% at a 76% crop on synthetic images) at a small false-match cost; Copydays and real
   product photos must confirm the thresholds.
8. **Return abuse cannot be evaluated on Olist/UCI** (no return events); only synthetic/seeded data exercises it.
