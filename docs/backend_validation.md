# Backend validation (auto-generated)

Generated 2026-10-22T05:19:38 by `python validate_backends.py`.

## Environment

- transformers: 4.44.2
- torch: 2.14.1+cpu
- cv2: 5.0.0
- PIL: 12.1.0

## Selected backends

- claim_backend_requested: default zero-shot model in intelligence.py
- claim_backend_loaded: transformer
- sentiment_backend_loaded: cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual

## Claim NLP + sentiment on the developer-authored set

### baseline (rules + VADER) (n=60)

- **legacy substring keywords**: category_accuracy=0.45, category_macro_F1=0.519, accuracy_negation_cases=0.091, accuracy_paraphrase_cases=0.0, accuracy_hard_cases=0.214, unsupported-claim detection precision=0.303, unsupported-claim detection recall=0.625
- **rules (negation-aware, current default)**: category_accuracy=0.733, category_macro_F1=0.77, accuracy_negation_cases=0.636, accuracy_paraphrase_cases=1.0, accuracy_hard_cases=0.214, unsupported-claim detection precision=0.577, unsupported-claim detection recall=0.938
- **sentiment**: backend=vader (English only), accuracy_3class=0.4, recall_of_negative=0.216
- failure cases: 16 category errors (rules), 29 negative texts missed - listed in `reports/backend_validation.json`

### transformer run (n=60)

- **legacy substring keywords**: category_accuracy=0.45, category_macro_F1=0.519, accuracy_negation_cases=0.091, accuracy_paraphrase_cases=0.0, accuracy_hard_cases=0.214, unsupported-claim detection precision=0.303, unsupported-claim detection recall=0.625
- **rules (negation-aware, current default)**: category_accuracy=0.733, category_macro_F1=0.77, accuracy_negation_cases=0.636, accuracy_paraphrase_cases=1.0, accuracy_hard_cases=0.214, unsupported-claim detection precision=0.577, unsupported-claim detection recall=0.938
- **configured backend (transformer)**: category_accuracy=0.633, category_macro_F1=0.668, accuracy_negation_cases=0.545, accuracy_paraphrase_cases=0.5, accuracy_hard_cases=0.429, unsupported-claim detection precision=0.5, unsupported-claim detection recall=0.875
- **sentiment**: backend=cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual, accuracy_3class=0.583, recall_of_negative=0.757
- failure cases: 16 category errors (rules), 9 negative texts missed - listed in `reports/backend_validation.json`

## CLIP text-image consistency

**NOT RUN** - data\image_text_pairs.csv not found. Provide image_path,category,consistent (1 = the photo shows the claimed condition, 0 = it does not), e.g. 50 damaged-product photos + 50 intact-product photos.

## Fallback behaviour

- fallback_ok: True
- category: damaged
- backend_reported: rules
- interpret_claim_backend: rules
- image_text_scorer_available: False

> Interpretation rule: a transformer is adopted only if it beats the rules on the INDEPENDENT team labels (not the developer-authored set), keeps negation accuracy, and the failure cases above are acceptable. Otherwise the rules stay the default.
