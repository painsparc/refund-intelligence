# data/

* `manual_claims_labelled.csv` - **60 hand-labelled claim texts** used by `python eval_data.py evaluate` (component 6, claim NLP).
  Columns: `label_category` (what the text is about; `unclear` if no concrete complaint), `text_supports_claim` (does the text support the
  claimed reason?), `label_sentiment` (negative / neutral / positive), `tag` (plain / paraphrase / negation / hard / vague).
  **Honest limitation:** this set was written and labelled by the developer who also wrote the rules, so it is a regression and sanity set,
  not an independent benchmark. It deliberately includes negation, paraphrase and hard cases that the rules miss.
  For an independent test, have two teammates label real reviews: `python eval_data.py labelling-sheet --n 200` writes
  `data/manual_label_sheet.csv` (random Olist + Amazon reviews, blank label columns). Save the finished sheet as
  `data/manual_labels_real.csv`; the evaluation picks it up automatically and reports it separately.
* `raw/` - put the downloaded Olist / UCI / Amazon / Copydays files here (git-ignored).
