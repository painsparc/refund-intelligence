# Final integrated test results (auto-generated)

Generated 2026-10-22T07:42:58Z by `scripts/run_full_pipeline.py`.

## Data used

| dataset | present under data/raw |
|---|---|
| olist | yes |
| uci | yes |
| amazon | yes |
| copydays | **NO** |

**NOT ALL REAL DATASETS WERE PRESENT: evaluation components without their dataset are reported NOT RUN in `docs/eval_report.md`. This is not the final real-data evaluation.**

## Steps

| step | result | seconds | detail |
|---|---|---|---|
| pytest -q | PASS | 135.9 | 53 passed in 130.99s (0:02:10) |
| live API workflow (FastAPI, uvicorn) | PASS | 6.3 | 19/19 checks |
| Streamlit process starts (health endpoint) | PASS | 9.3 | port 46180 |
| Streamlit pages render against the live API | PASS | 14.6 | 3 pages: ['Customer Portal', 'Operations Console', 'Management View'] |
| dataset check | PASS | 0.0 | olist=yes, uci=yes, amazon=yes, copydays=NO |
| dependency check | PASS | 0.0 | vaderSentiment, cv2 present; transformers installed |
| evaluation (eval_data.py all) | PASS | 764.7 | all --olist data\raw\olist --olist-limit 0 --uci data\raw\online_retail_II.xlsx --amazon data\raw\All_Beauty.jsonl.gz --n 20 --cost-scales 1,0.2,0.1,0.05 / wrote docs/dataset_manifest.md |

Evaluation outputs: `reports/eval_results.json`, `docs/eval_report.md`, `docs/dataset_manifest.md`.
