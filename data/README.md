# data/

* `manual_claims_labelled.csv` - **60 hand-labelled claim texts** used by `python eval_data.py evaluate` (component 6, claim NLP).
  Columns: `label_category` (what the text is about; `unclear` if no concrete complaint), `text_supports_claim` (does the text support the
  claimed reason?), `label_sentiment` (negative / neutral / positive), `tag` (plain / paraphrase / negation / hard / vague).
  **Honest limitation:** this set was written and labelled by the developer who also wrote the rules, so it is a regression and sanity set,
  not an independent benchmark. It deliberately includes negation, paraphrase and hard cases that the rules miss.
  For an independent test, have two teammates label real reviews: `python eval_data.py labelling-sheet --n 200` writes
  `data/manual_label_sheet.csv` (random Olist + Amazon reviews, blank label columns). Save the finished sheet as
  `data/manual_labels_real.csv`; the evaluation picks it up automatically and reports it separately.
* `manual_labels_real.csv` - **independent** labels of real reviews, produced by two annotators + reconciliation (`label_tools.py`, criteria in
  `docs/labelling_guidelines.md`, agreement in `labelling/agreement.json`). Created by people, never by the developer who wrote the rules or by the system.
* `image_text_pairs.csv` (optional) - `image_path,category,consistent` rows for validating CLIP (`validate_backends.py`).
* Label origins are kept apart: developer-authored (regression only) / independent human / source star ratings (weak proxy) / synthetic (`eval_labels`).
* `raw/` - put the downloaded Olist / UCI / Amazon / Copydays files here (git-ignored).
