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

## Findings from the dataset evaluation (`pipeline_smoke_run.md`; re-run on the real data)
1. **The cost matrix has a currency scale.** With handling cost 100, abusive claims of ~150 units are auto-approved because reviewing them costs more than the
   exposure (this is exactly how refund farming exploits a flat review cost). `COST_SCALE` rescales friction/handling; the sensitivity table shows the scale
   at which each pattern starts to be reviewed and where legitimate near-threshold customers start to be over-reviewed. Choose the scale from real claim sizes.
2. **Average-hash misses cropped re-uploads** (JPEG / resize / blur are caught). A crop-tolerant similarity signal is a Phase 2 option.
3. **Return abuse cannot be evaluated on Olist/UCI** (no return events); only the synthetic or seeded data exercises it.
