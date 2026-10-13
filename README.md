# Intelligent Refund Risk & Resolution Platform (prototype)

An **intelligence layer on top of an existing refund process**. Order, policy, refund execution, case and notification
systems are *stubbed* (`db.py`), not rebuilt. Only the new capabilities are real.

```
Existing refund process -> Streamlit -> LangGraph intelligence layer -> risk/evidence analysis -> expected-cost decision
   -> existing action execution -> historical intelligence & prevention loop (offline, outside the graph)
```

## Five MVP contributions
1. **Evidence Intelligence** - image quality, hash/similarity reuse detection, claim-evidence consistency
2. **Customer Behavioural Risk** - rolling refund features (30/90 day) and the Behavioural Risk formula
3. **Claim & Abuse Risk** - Claim Risk formula plus five named abuse patterns (serial refunder, return abuse, fake defect pattern, refund farming, policy exploitation)
4. **Expected-Cost Resolution** - cost(action) = fraud/loss + review + CX cost; cheapest of auto-approve / request evidence / manual review, with guardrails
5. **Historical Intelligence & Prevention Loop** - product/vendor risk snapshots and an insight -> intervention -> outcome log (offline, outside the real-time graph)

Three agents only: **Evidence & Claim Agent**, **Behaviour & Abuse Agent**, **Resolution Reasoning Agent**.

## The system = 5 files
| File | Architecture block |
|---|---|
| `db.py` | SQL data store (**SQLite for MVP / PostgreSQL extension**) incl. snapshots, insight log and provenance tables; stubs of the EXISTING systems; seed data |
| `intelligence.py` | The five contributions, the 3 agents, the documented formulas, the historical loop |
| `graph.py` | LangGraph real-time orchestration + Action Execution Layer |
| `app.py` | FastAPI backend: customer portal, human-in-the-loop, business views, historical-loop endpoints |
| `streamlit_app.py` | Streamlit UI: Customer Portal, Operations Console, Management View (calls the API only) |

Outside the system: `demo.py` (seeded demo), `eval_data.py` (offline dataset ingestion / scenarios / evaluation), `tests/`.

## Decision flow (slide 2 / proposal section 11)
```
claim ─┬─ Deterministic Order+Policy Gate ──┐
       ├─ Evidence Match (hash/similarity/  ├─► Evidence & Claim Agent ───┐
       │   metadata/quality)               ─┤                              ├─► Abuse + Claim Risk Synthesis ─► Expected-Cost Engine
       └─ Feature Builder (30/90/180d) ─────┴─► Behaviour & Abuse Agent ──┘      ─► Resolution Reasoning Agent ─► Guardrails
                         ─► immediate refund | refund after return | request evidence | manual review | policy decline  ─► existing systems
```
Exactly **3 agents**. Features, formulas, abuse rules, expected cost and guardrails are deterministic; the agents interpret text/image/history,
explain, and may only *escalate*. If anything fails, the claim goes to manual review (never a silent approval).

## The risk model = the final proposal (all scores 0-100)
Weights and thresholds are constants at the top of `intelligence.py`; `tests/test_refund.py` pins them.
```
Behavioural Risk   BR = 0.35 Frequency + 0.25 ValueRatio + 0.15 RepeatReason + 0.15 ReturnEventException + 0.10 EvidenceReuse
Trust Index        100 - BR            (supporting index, never proof of wrongdoing)        Blacklist = hard signal -> manual review, not weighted
Claim Risk         CR = 0.30 ClaimAmbiguity + 0.30 EvidenceInconsistency + 0.20 EvidenceReuse + 0.20 AbusePatternStrength
Exposure risk      (0.6 CR + 0.4 BR) / 100    <- prototype choice: the proposal does not say how CR and BR combine
Expected cost      ExpectedCost(a) = RiskExposure(a) + CustomerFrictionCost(a) + HandlingCost(a)   -> cheapest eligible action
```
Component rules (each normalised to 0-100, details in the header of `intelligence.py`): Frequency = worst of refunds in 30/90/180 days vs
3/5/8; ValueRatio = refund value / purchase value over 180 days (saturates at 50%); RepeatReason = most repeated reason in 180 days;
ReturnEventException = late-or-missing returns in 180 days (saturates at 3); EvidenceReuse = prior claims flagged for reuse. Ambiguity comes
from `interpret_claim` (empty/thin/vague text, reason-vs-text conflict); Inconsistency = 100 x (1 - image quality), 65 if a physical claim
has no photo; reuse = 100 exact copy / 70 near-duplicate / 50 own earlier claim; abuse strength = 50 per pattern (cap 100).

Actions and cost matrix (configurable constants; `COST_SCALE` adapts friction/handling to the deployment's currency and claim size):
| action | when eligible | exposure factor | friction | handling |
|---|---|---|---|---|
| `auto_approve` (immediate refund) | always | 1.00 | 0 | 0 |
| `refund_after_return` | something to send back | 0.20 | 25 + 5% | 40 |
| `request_evidence` | physical claim, evidence inconsistency >= 30, asked at most once | 0.15 | 10 + 5% | 10 |
| `manual_review` | always | 0.03 | 5% | 100 |
| `policy_decline` | deterministic: policy gate fails (AI can never override) | - | - | - |

Guardrails (only ever add caution): blacklisted customer and exact evidence reuse across customers -> manual review; policy violation -> decline.

## The five abuse patterns (`detect_abuse_patterns`, deterministic, on 30/90/180-day features)
| Pattern | Rule |
|---|---|
| `serial_refunder` | >= 3 refunds in 30 days, or >= 4 in 90 days, or >= 7 in 180 days |
| `return_abuse` | >= 2 late-or-missing return events in 180 days (needs return events; none in Olist/UCI) |
| `fake_defect_pattern` | defect-type claim with reused evidence, or >= 3 defect claims in 90 days with >= 2 weak/missing-evidence |
| `refund_farming` | >= 3 refunds just under the auto-approve limit, or >= 4 refunds in 180 days with refund value >= 50% of purchases |
| `policy_exploitation` | >= 3 refunds filed in the last 20% of the return window |

## G5 Historical Intelligence & Prevention Loop (offline)
`POST /historical/run` (or the Management View button) persists **Product Risk Snapshots**, **Vendor Risk Snapshots** and raises
insights in the **Insight / Intervention Log**:  pattern → insight → intervention (`/apply`) → later outcome (`/evaluate`, compares
the metric now vs the baseline stored when the insight was raised: improved / no_change / worse). Flags from the loop reach later
claims only as *context for the agents*; the loop is never called inside the real-time graph (a test enforces this).

## Run
```bash
pip install -r requirements.txt
python demo.py                          # end-to-end story in the terminal, no API key needed
uvicorn app:app --reload                # backend  -> http://localhost:8000/docs
streamlit run streamlit_app.py          # UI       (set API_URL if backend is elsewhere)
pytest -q                               # 29 tests
```
* **Agents**: with `ANTHROPIC_API_KEY` Claude interprets image/text/history; without it only the deterministic logic runs.
* **PostgreSQL**: `docker compose up -d` and set `DATABASE_URL` (see `.env.example`). Default is SQLite.
* Seeded try-it orders `ORD-1001`..`ORD-1006` (customers C001-C005); `demo.py` tells each story.

## Evaluation datasets (offline; separate DB `eval_data.db`, seeded demo untouched)
Approved composite strategy: **Olist** (orders/sellers), **UCI Online Retail II** (return-like behaviour), **Amazon Reviews 2023** (text proxy),
**INRIA Copydays** (image-similarity ground truth) + **synthetic labelled abuse scenarios**. Sources, licences, fields and limitations:
`docs/data_sources.md`. Precautions: cancellations are *not* refunds/fraud, review images are *not* refund evidence, synthetic labels are kept
apart from source data (`data_provenance`, `eval_labels`).
```bash
python eval_data.py all --olist data/olist --uci data/online_retail_II.xlsx --amazon data/All_Beauty.jsonl.gz \
       --copydays-originals data/copydays/original --copydays-variants data/copydays/variants
# or step by step: ingest | scenarios | evaluate | manifest      (writes docs/dataset_manifest.md, docs/eval_report.md, reports/eval_results.json)
```
`docs/pipeline_smoke_run.md` is a run on tiny fixtures only - the real report is generated by the command above.
Open items (risk-model alignment with the proposal, diagram edits): `docs/spec_alignment.md`, `docs/diagram_changes.md`.
