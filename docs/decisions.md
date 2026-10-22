# Decisions taken (2026-10-22) and what is still open

These settings were chosen from the measured evidence in `docs/eval_report.md`, `docs/cost_calibration.md` and `docs/backend_validation.md`. Each one is a
**prototype decision on synthetic or developer-labelled evidence** and must be re-confirmed with real inputs before any deployment.

| # | Decision | Value | Evidence | Revisit when |
|---|---|---|---|---|
| 1 | Cost scale | `COST_SCALE=0.1` (a manual review costs about 10 units; Olist claims are in BRL) | best recall with false-positive rate <= 15 %: precision 0.92-0.93, recall 0.54-0.55, FPR 0.10-0.125, 22-26 % of claims to review | the real cost of a review and the team's review capacity are known |
| 2 | Acceptable false-positive rate on legitimate customers | 15 % (`max_fpr`) | scales below x0.1 push FPR to 25-49 % and the review queue to 42-77 % of claims | capacity is known |
| 3 | Sentiment backend | `SENTIMENT_MODEL=cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual` (opt-in via environment; VADER stays the code default so the system runs with no download) | accuracy 0.58 vs 0.40, negative recall 0.76 vs 0.22 on the developer set | independent labels exist |
| 4 | Claim-reason backend | **rules** (negation-aware), transformer stays optional | rules 0.73 vs zero-shot transformer 0.63 category accuracy | independent labels exist; consider a rules + transformer hybrid for hard cases (0.43 vs 0.21) |
| 5 | Image-reuse thresholds | unchanged (aHash 5, pHash 8, ORB 40) | synthetic calibration only | Copydays has been run (`eval_copydays_split` tunes on a validation split) |
| 6 | Olist loading | all 98,666 orders (`--olist-limit 0`) | 98,465 of 99,224 reviews link to a loaded order; the other 759 belong to orders removed because they have no items | - |
| 7 | Policy gate, blacklist and exact-evidence-reuse guardrails | deterministic, never configurable by AI or by cost scale | `tests/test_refund.py` | - |

The code defaults in `intelligence.py` are unchanged (tests pin them). Use `.env.example` to apply decisions 1 and 3 to a deployment.

## Still open (cannot be decided or produced by the tooling)
1. **Copydays** benchmark: the INRIA server was unreachable. Until it runs, component 3 of the evaluation report says NOT RUN and decision 5 stands.
2. **Independent labels**: two people must label `data/labelling/annotator_A.csv` / `annotator_B.csv` (see `docs/labelling_guidelines.md`). Until then every NLP and
   sentiment figure is based on the 60 developer-written rows and decisions 3 and 4 are provisional.
3. **CLIP** validation needs `data/image_text_pairs.csv`.
4. **Real review cost and capacity** (decisions 1 and 2).
