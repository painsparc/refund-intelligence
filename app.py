"""
app.py  -  Customer portal API + human-in-the-loop + BUSINESS VIEWS (architecture slides 1-3)

  POST /claims                    customer portal: refund request + image evidence  -> LangGraph decision
  POST /claims/{id}/evidence      customer uploads the evidence we asked for        -> re-run the graph
  POST /claims/{id}/return        return tracking: received | late | missing (claims awaiting a return)
  GET  /claims/{id}               decision, signals, evidence, review case, audit trail
  GET  /reviews                   HUMAN-IN-THE-LOOP queue (open manual-review cases)
  POST /reviews/{case_id}/resolve analyst: approve | reject | fraud   (case outcome feeds the blacklist)
  GET  /insights/operations|management|products|vendors|policy|abuse   BUSINESS VIEWS
  POST /historical/run            G5 loop: persist product/vendor risk snapshots + propose insights/interventions
  GET  /historical/snapshots/{products|vendors}   latest persisted snapshot
  GET  /historical/insights       Insight / Intervention log
  POST /historical/insights/{id}/apply      intervention put in place
  POST /historical/insights/{id}/evaluate   later outcome vs baseline
  GET  /claims/{id}/evidence-image          stored evidence photo

Run:  uvicorn app:app --reload      then open http://localhost:8000/docs
"""
import json
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
import db
import intelligence as ai
from graph import process_claim


@asynccontextmanager
async def lifespan(_):
    db.init()
    db.seed()
    yield


app = FastAPI(title="Intelligent Refund Risk & Resolution Platform", lifespan=lifespan)


@app.post("/claims")
async def submit_claim(order_id: str = Form(...), customer_id: str = Form(...), reason: str = Form(...),
                       amount: float = Form(...), description: str = Form(""), image: Optional[UploadFile] = File(None)):
    img = await image.read() if image else None
    claim = dict(order_id=order_id, customer_id=customer_id, reason=reason, amount=amount, description=description)
    return process_claim(claim, img)


class ReturnEvent(BaseModel):
    status: str  # received | late | missing


@app.post("/claims/{claim_id}/return")
def return_event(claim_id: str, body: ReturnEvent):
    """Existing return tracking reports what happened to a 'refund after return' claim."""
    try:
        return ai.record_return(claim_id, body.status)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/claims/{claim_id}/evidence")
async def add_evidence(claim_id: str, image: UploadFile = File(...)):
    row = db.q1("SELECT * FROM refund_requests WHERE id=:i", i=claim_id)
    if not row:
        raise HTTPException(404, "claim not found")
    if row["status"] != "awaiting_evidence":
        raise HTTPException(409, f"claim is '{row['status']}', not awaiting evidence")
    claim = {k: row[k] for k in ("id", "order_id", "customer_id", "reason", "amount", "description")}
    return process_claim(claim, await image.read(), resubmission=True)


@app.get("/claims/{claim_id}")
def get_claim(claim_id: str):
    row = db.q1("SELECT * FROM refund_requests WHERE id=:i", i=claim_id)
    if not row:
        raise HTTPException(404, "claim not found")
    row["costs"], row["flags"] = json.loads(row["costs"]), json.loads(row["flags"])
    row["components"] = json.loads(row["components"]) if row.get("components") else {}
    return {"claim": row,
            "signals": db.q("SELECT kind, detail FROM risk_signals WHERE claim_id=:i", i=claim_id),
            "evidence": db.q("SELECT id, quality, width, height, path FROM evidence WHERE claim_id=:i", i=claim_id),
            "review_case": db.q1("SELECT * FROM review_cases WHERE claim_id=:i", i=claim_id),
            "audit_log": db.q("SELECT action, detail, created_at FROM action_log WHERE claim_id=:i ORDER BY created_at", i=claim_id)}


@app.get("/claims/{claim_id}/evidence-image")
def evidence_image(claim_id: str):
    ev = db.q1("SELECT path FROM evidence WHERE claim_id=:i", i=claim_id)
    if not ev:
        raise HTTPException(404, "no evidence stored for this claim")
    return FileResponse(ev["path"], media_type="image/jpeg")


@app.get("/reviews")
def open_reviews():
    return db.q("SELECT c.id AS case_id, c.opened_at, r.id AS claim_id, r.customer_id, r.amount, r.exposure_risk, r.flags, r.rationale "
                "FROM review_cases c JOIN refund_requests r ON r.id=c.claim_id WHERE c.status='open' ORDER BY r.exposure_risk DESC")


class Resolution(BaseModel):
    outcome: str  # approve | reject | fraud
    note: str = ""


@app.post("/reviews/{case_id}/resolve")
def resolve(case_id: str, body: Resolution):
    if body.outcome not in ("approve", "reject", "fraud"):
        raise HTTPException(400, "outcome must be approve | reject | fraud")
    try:
        return ai.resolve_review(case_id, body.outcome, body.note)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/insights/operations")
def operations():
    return ai.operations_view()


@app.get("/insights/management")
def management():
    return ai.management_view()


@app.get("/insights/products")
def products():
    return ai.product_intelligence()


@app.get("/insights/vendors")
def vendors():
    return ai.vendor_intelligence()


@app.get("/insights/policy")
def policy():
    return ai.policy_insights()


@app.get("/insights/abuse")
def abuse():
    return ai.abuse_pattern_counts()


# ---- G5 Historical Intelligence & Prevention Loop (offline, outside the decision graph) ----------
@app.post("/historical/run")
def historical_run():
    return ai.run_historical_loop()


@app.get("/historical/snapshots/{kind}")
def snapshots(kind: str):
    if kind not in ("products", "vendors"):
        raise HTTPException(404, "kind must be products | vendors")
    return ai.latest_snapshots(kind)


@app.get("/historical/insights")
def insights(status: Optional[str] = None):
    return ai.list_insights(status)


class Intervention(BaseModel):
    note: str = ""


@app.post("/historical/insights/{insight_id}/apply")
def apply_insight(insight_id: str, body: Intervention):
    try:
        return ai.apply_intervention(insight_id, body.note)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/historical/insights/{insight_id}/evaluate")
def evaluate_insight(insight_id: str):
    try:
        return ai.evaluate_outcome(insight_id)
    except ValueError as e:
        raise HTTPException(409, str(e))
