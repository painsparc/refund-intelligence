# Expected-cost decision layer: calibration and rationale

## How an action is selected (all deterministic)
1. `policy_gate` decides eligibility under the refund policy. A violation is a **policy decline** - no score, cost or AI output can override it.
2. For every eligible action, `cost_table` computes `cost(a) = RiskExposure + CustomerFriction + Handling`:
   * RiskExposure = `amount x exposure_risk x EXPOSURE_FACTOR[a]` (auto-approve 1.00, refund-after-return 0.20, request-evidence 0.15, manual review 0.03)
   * CustomerFriction = `COST_SCALE x FRICTION_FIXED[a] + amount x (1 - exposure_risk) x FRICTION_RATE[a]` (the good-customer cost of being slowed down)
   * Handling = `COST_SCALE x HANDLING[a]` (review 100, return 40, evidence 10, approve 0)
   * `exposure_risk = (0.6 x ClaimRisk + 0.4 x BehaviouralRisk) / 100` - a prototype choice, the proposal does not specify the mix.
3. The cheapest eligible action wins. **Guardrails then only add caution**: blacklisted customer, or an exact evidence copy used by another customer ->
   manual review.
4. The Resolution Reasoning Agent writes the explanation (the rationale now states the winning cost, the runner-up and the difference, or the guardrail that
   forced the action) and may only *escalate* to a more cautious action.

Why review is not always chosen for risky customers: manual review costs `100 x COST_SCALE` in handling. It only beats auto-approval when
`amount x (risk x 0.97 - (1 - risk) x 0.05) > COST_SCALE x 100`, i.e. when the money at stake justifies a person's time.

## What the "zero true positives" warning actually was
The smoke report treated "decision != auto_approve" as an abuse classifier and found recall 0 at the default scale. The real-data run confirms the cause: at
`COST_SCALE = 1.0` a review costs 100, but the synthetic claims (real Olist order attributes, 40 customers per profile) are small - **median about 70 for abusive
and 63 for legitimate claims** - so even at exposure risk 0.8 the break-even claim amount is **130**. The cost layer behaved as designed; the default currency scale
did not fit the claim sizes. It is a configuration finding, not evidence that the detectors fail: the abuse patterns are flagged independently of the decision
(pattern-flag classifier: precision 0.97, recall 0.57, false-positive rate 0.05).

## Sweep on SYNTHETIC labels (run of 2026-10-21 on the real Olist order attributes; 280 claims: 200 abusive, 80 legitimate; seed 7)
The abuse labels are synthetic, so this shows the trade-off, not real-world fraud performance. Source: component 4b of `docs/eval_report.md`.

| COST_SCALE (review cost) | precision | recall | false-positive rate | manual-review share | action mix |
|---|---|---|---|---|---|
| 1.0 (100) | 1.00 | 0.04 | 0.00 | 0 % | 98 % auto-approve |
| 0.5 (50) | 1.00 | 0.09 | 0.00 | 2 % | 94 % auto-approve |
| 0.2 (20) | 0.97 | 0.28 | 0.03 | 8 % | 79 % approve, 12 % return, 8 % review |
| **0.1 (10)** | **0.93** | **0.54** | **0.10** | **22 %** | 59 % approve, 18 % return, 22 % review |
| 0.05 (5) | 0.88 | 0.72 | 0.25 | 42 % | 42 % approve, 42 % review |
| 0.02 (2) | 0.84 | 0.87 | 0.41 | 67 % | 67 % review |
| 0.01 (1) | 0.82 | 0.91 | 0.49 | 77 % | 77 % review |

Break-even claim amount above which manual review is cheaper than auto-approval:

| exposure risk | x1.0 | x0.5 | x0.2 | x0.1 | x0.05 | x0.02 |
|---|---|---|---|---|---|---|
| 0.2 | 649 | 325 | 130 | 65 | 33 | 13 |
| 0.4 | 279 | 140 | 56 | 28 | 14 | 5.6 |
| 0.6 | 178 | 89 | 36 | 18 | 8.9 | 3.6 |
| 0.8 | 131 | 65 | 26 | 13 | 6.5 | 2.6 |

(`tests/test_eval_data.py::test_break_even_amount_matches_the_decision_engine` checks these formulas against the real decision engine.)

## How the operating point is chosen (rule, not taste)
`eval_cost_calibration` recommends the scale with **the best recall among scales whose false-positive rate on the legitimate profiles is <= 15 %**
(`max_fpr`, an explicit parameter). On this run that is **x0.1**: precision 0.93, recall 0.54, false-positive rate 0.10, 22 % of claims to manual review.
Going lower buys recall with a queue no team could staff (42-77 % of claims) and false-positive rates of 25-49 %, which is why the manual-review share is
reported next to precision and recall. Going higher (x0.2 and above) keeps false positives near zero but lets most synthetic abusers through.

Decisions to take as a team (business inputs, not something the data can decide):
1. **The real cost of one manual review** in your currency: set `COST_SCALE = real_review_cost / 100` (the matrix is written for a 100-unit review).
   x0.1 means "a review costs about 10 BRL" for Olist-sized claims.
2. **Review capacity**: pick `max_fpr` and the acceptable manual-review share from the staff you have (22 % at x0.1 may already be too many).
3. **Currency and claim sizes**: Olist is BRL, UCI is GBP. Re-run the sweep on the real order amounts of the deployment.

The defaults in `intelligence.py` are **unchanged** (`COST_SCALE=1.0` from the environment; the tests pin the matrix). Set e.g. `COST_SCALE=0.1` in the environment of
your deployment once the team has fixed the inputs above.

## What stays deterministic
Policy restrictions, the blacklist guardrail and the exact-evidence-reuse guardrail never depend on `COST_SCALE`, the LLM, or the sweep
(`tests/test_refund.py::test_blacklist_is_a_hard_signal_not_a_weight`, `test_policy_gate_is_a_deterministic_decline`).
