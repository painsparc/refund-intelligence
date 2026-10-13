# Architecture-diagram corrections (apply to the source of the 3-page PDF)

The diagram source file was not available, so these are exact edits to make rather than a regenerated PDF.

## Slide 1 - Solution Architecture
- Title of the green box stays **"OUR MVP: INTELLIGENCE LAYER"**. Re-label the five cards:
  1 **Evidence Intelligence** | 2 **Customer Behavioural Risk** | 3 **Claim & Abuse Risk** *(sub-labels: Claim Risk, Abuse Signals - five named patterns: serial refunder, return abuse, fake defect pattern, refund farming, policy exploitation)* | 4 **Expected-Cost Resolution** | 5 **Historical Intelligence & Prevention Loop** (move the loop into card 5; keep it visually outside the real-time graph).
- Banner "MVP BOUNDARY = 5 ADDITIONS" stays; there must be no sixth card.

## Slide 2 - Agentic Refund Decision Flow
- Add a node **"Abuse + Claim Risk Synthesis"** between the two agents and "Decision state" (the code has it as its own graph node).
- Resolution box: replace the three outcomes with the proposal's five: **Immediate refund (auto-approve) | Refund after return | Request evidence | Manual review | Policy decline**
  (policy decline is deterministic, taken from the policy gate; the AI cannot override it). Slide 1's "EXISTING ACTION LAYER" list must match.
- Decision state text: "Claim risk (0-100) + Behavioural risk (0-100) + Trust index + evidence confidence + refund amount + policy eligibility".
- Expected-cost line: "ExpectedCost(a) = Risk exposure + Customer friction + Handling cost" (the proposal's wording; the diagram says fraud/loss + review + CX).
- Agent names (already match the code): **Evidence & Claim Agent**, **Behaviour & Abuse Agent**, **Resolution Reasoning Agent**. Use these same three names in the proposal text (replace Claim Interpretation / Evidence Intelligence / Decision Intelligence Agent).
- Add a small note under "Decision state": "Risk scores 0-100 (once the formula is confirmed - see spec_alignment.md)".
- Keep the graph separate from the historical loop (no arrow from the loop into the graph except "context for agents").

## Slide 3 - Data, Evidence & Business Analytics
- Replace the label **"PostgreSQL"** with **"SQL Data Store - SQLite for MVP / PostgreSQL extension"**.
- Add the new tables to the store box: Product Risk Snapshot, Vendor Risk Snapshot, Insight / Intervention Log.
- Add a separate strip at the bottom (or a 4th slide): **dataset preparation flow** - see `dataset_flow.svg`.
