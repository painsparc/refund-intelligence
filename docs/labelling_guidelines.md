# Labelling guidelines for the independent NLP / sentiment benchmark

Purpose: build `data/manual_labels_real.csv` from **real review text** so claim-reason classification, negation handling and sentiment can be
measured on labels that the rule author did not write. The 60-row `data/manual_claims_labelled.csv` stays as a regression set only.

## Procedure
1. `python eval_data.py ingest ...` (Olist + Amazon), then `python eval_data.py labelling-sheet --n 200` -> `data/manual_label_sheet.csv`.
2. `python label_tools.py split` -> `data/labelling/annotator_A.csv` and `annotator_B.csv` (identical, blind, **no star ratings**).
3. Two teammates (**neither may be the person who wrote the rules in `intelligence.py`**) each label their own file **independently**; they do not look at
   each other's file or the rating of the review, and do not run the system on the rows.
4. `python label_tools.py reconcile` -> `data/labelling/agreement.json` (raw agreement and Cohen's kappa per field) and `disagreements.csv`.
5. The two annotators resolve every row of `disagreements.csv` together (fill the `final_*` columns, optionally `resolution_note`).
6. `python label_tools.py finalize` -> `data/manual_labels_real.csv`. It refuses to write while any disagreement is open.
7. Commit `data/labelling/agreement.json` and `data/manual_labels_real.csv` (labels only; the review texts come from public datasets - check the
   licence in `docs/data_sources.md` before committing text of a dataset that forbids redistribution; if in doubt commit ids + labels only).
8. Report the kappa **before** reconciliation. Kappa < 0.6 on a field means the criteria below are ambiguous: tighten them and re-label a sample.

## Columns to fill
| column | allowed values | question |
|---|---|---|
| `label_category` | `damaged`, `defective`, `wrong_item`, `not_as_described`, `size_issue`, `changed_mind`, `not_delivered`, `unclear` | What problem does the text actually **affirm**? |
| `text_supports_claim` | `1` / `0` | Given `claimed_reason` (assigned at random by the sheet generator - it is **not** what the reviewer said), does the text affirm that reason? |
| `label_sentiment` | `negative`, `neutral`, `positive` | How does the author feel about the product / experience? |

## Criteria
* **Affirm, don't mention.** "It is not damaged" and "I thought it would arrive broken but it was fine" do **not** affirm `damaged` (label `unclear` or the
  real problem if one is stated). A negated problem never counts as support.
* **One category: the main complaint.** If two problems are stated, pick the one the author stresses; if equal, pick the first stated.
  `defective` = does not work / stops working / dead on arrival. `damaged` = physical breakage in transit or on opening. `wrong_item` = a different
  product, colour or size **was sent**. `size_issue` = the right item does not fit. `not_as_described` = fake, misleading photos/description.
* **`unclear`** = no concrete problem is stated (praise, thanks, a pure opinion, a question, a delivery-speed remark without a missing item, spam, or text
  that cannot be understood). Do **not** guess a category from the star rating.
* **`text_supports_claim` = 1** only if the text affirms exactly `claimed_reason`. A different problem, no problem, or a negated problem = 0.
* **Sentiment is about the author's feeling, not about fraud or the category.** Polite factual complaints ("the jar arrived cracked") are `negative`.
  Mixed praise + complaint: choose the dominant feeling; if balanced, `neutral`. Sarcasm: label the meant feeling.
* **Language.** Olist text is Portuguese. Only label languages you read fluently; skip (leave a note in `notes`) rows you cannot judge. Skipped rows must be
  skipped by **both** annotators (agree in `notes`), otherwise they appear as disagreements. Keep the language in the final report.
* **Do not use** the review score, the order, the product, or the system's output while labelling.

## Keeping label origins apart
| origin | where | what it is | used for |
|---|---|---|---|
| developer-authored | `data/manual_claims_labelled.csv` | written by the rule author | regression / sanity only |
| independent human labels | `data/manual_labels_real.csv` (+ `data/labelling/`) | two annotators + reconciliation | the **only** accuracy claim for claim NLP and sentiment |
| source labels | `source_reviews.rating` (stars) | provided by the data source | weak proxy only, labelled as such in the report |
| synthetic labels | `eval_labels` (SYN-* entities) | generated from behavioural profiles | abuse-pattern / decision recall, never NLP accuracy |
