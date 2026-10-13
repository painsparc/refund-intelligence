"""
graph.py  -  LANGGRAPH: REAL-TIME REASONING ORCHESTRATION  (architecture slide 2/3, proposal section 11)

  START -> [Deterministic Order+Policy Gate] [Feature Builder] [Evidence Match]              (deterministic, parallel)
              gate + evidence_match       -> Evidence & Claim Agent  (claim interpretation + evidence intelligence)
              features + evidence_match   -> Behaviour & Abuse Agent (behavioural risk, trust index, 5 abuse patterns)
              both agents                 -> Abuse + Claim Risk Synthesis  (decision state, 0-100 scores)
                                          -> Expected-Cost Engine (+ deterministic guardrails)
                                          -> Resolution Reasoning Agent (explain / escalate only)
                                          -> ACTION EXECUTION LAYER (existing systems: refund / return / evidence request /
                                             review case / policy decline)

"AI recommends; existing systems execute."  If anything in the graph fails, the claim goes to manual review - never a silent approval.
"""
import os
import io
import json
from typing import TypedDict, Optional
from langgraph.graph import StateGraph, START, END
from PIL import Image
import db
import intelligence as ai

EVIDENCE_DIR = os.getenv("EVIDENCE_DIR", "evidence_store")
STATUS = {"auto_approve": "refunded", "refund_after_return": "awaiting_return", "request_evidence": "awaiting_evidence",
          "manual_review": "under_review", "policy_decline": "declined"}


class State(TypedDict, total=False):
    claim: dict
    image: Optional[bytes]
    resubmission: bool
    gate: dict
    features: dict
    evidence: dict
    ev_out: dict
    beh_out: dict
    risk: dict
    decision: dict
    result: dict


# ---- deterministic nodes -------------------------------------------------------------------------
def gate_node(s):
    return {"gate": ai.policy_gate(s["claim"])}


def features_node(s):
    return {"features": ai.build_features(s["claim"], db.get_order(s["claim"]["order_id"]))}


def evidence_match_node(s):
    return {"evidence": ai.evidence_match(s.get("image"), s["claim"])}


# ---- AI agent nodes ------------------------------------------------------------------------------
def evidence_agent_node(s):
    return {"ev_out": ai.evidence_claim_agent(s["claim"], s["gate"], s["evidence"], s.get("image"))}


def behaviour_agent_node(s):
    return {"beh_out": ai.behaviour_abuse_agent(s["claim"], s["features"], s["evidence"])}


# ---- synthesis, expected cost, resolution reasoning ----------------------------------------------
def risk_synthesis_node(s):
    return {"risk": ai.synthesize_risk(s["ev_out"], s["beh_out"])}


def cost_node(s):
    return {"decision": ai.expected_cost_decision(s["claim"], s["gate"], s["risk"], s["beh_out"], s["evidence"],
                                                   s.get("resubmission", False))}


def resolution_agent_node(s):
    return {"decision": ai.resolution_agent(s["claim"], s["ev_out"], s["beh_out"], s["risk"], s["decision"])}


# ---- action execution layer: persist + call the EXISTING systems ---------------------------------
def execute_node(s):
    c, d, ev, eo, bo, rk, f = s["claim"], s["decision"], s["evidence"], s["ev_out"], s["beh_out"], s["risk"], s["features"]
    action = d["action"]
    status = STATUS[action]
    flags = sorted(set(eo["flags"] + bo["signals"] + d["forced"]))
    row = dict(order_id=c["order_id"], customer_id=c["customer_id"], reason=c["reason"], amount=c["amount"],
               description=c.get("description", ""), claim_risk=rk["claim_risk"], behaviour_risk=rk["behaviour_risk"],
               trust_index=rk["trust_index"], evidence_confidence=eo["evidence_confidence"], exposure_risk=d["exposure_risk"],
               decision=action, costs=json.dumps(d["costs"]), flags=json.dumps(flags), rationale=d["rationale"], status=status,
               components=json.dumps({"claim_risk": rk["claim_components"], "behaviour_risk": rk["behaviour_components"],
                                      "cost_breakdown": d["cost_breakdown"], "llm_adjustment": {
                                          "claim": eo["llm_adjustment"], "behaviour": bo["llm_adjustment"]}}))
    if db.q1("SELECT id FROM refund_requests WHERE id=:i", i=c["id"]):
        db.update("refund_requests", c["id"], **row)
    else:
        db.insert("refund_requests", id=c["id"], created_at=db.now(), **row)

    if ev.get("present"):  # evidence store: image + quality/metadata/reuse signatures
        os.makedirs(EVIDENCE_DIR, exist_ok=True)
        path = os.path.join(EVIDENCE_DIR, f"{c['id']}.jpg")
        Image.open(io.BytesIO(s["image"])).convert("RGB").save(path, "JPEG")
        db.run("DELETE FROM evidence WHERE claim_id=:c", c=c["id"])
        db.insert("evidence", id=db.uid("EV"), claim_id=c["id"], customer_id=c["customer_id"], path=path, sha256=ev["sha256"],
                  ahash=ev["ahash"], width=ev["width"], height=ev["height"], quality=ev["quality"], created_at=db.now())

    db.run("DELETE FROM risk_signals WHERE claim_id=:c", c=c["id"])  # claim / behaviour risk signals
    for flag in eo["flags"]:
        db.insert("risk_signals", id=db.uid("SIG"), claim_id=c["id"], customer_id=c["customer_id"], kind="claim", detail=flag, created_at=db.now())
    for flag in bo["signals"]:
        kind = "abuse" if flag in bo["abuse_patterns"] else "behaviour"
        db.insert("risk_signals", id=db.uid("SIG"), claim_id=c["id"], customer_id=c["customer_id"], kind=kind, detail=flag, created_at=db.now())

    case_id = None
    if action == "auto_approve":
        db.execute_refund(c)
    elif action == "refund_after_return":
        db.request_return(c["id"])
    elif action == "request_evidence":
        db.request_more_evidence(c["id"])
    elif action == "policy_decline":
        db.decline_refund(c, "; ".join(v.replace("policy:", "") for v in d["forced"] if v.startswith("policy:")))
    else:
        case_id = db.open_review_case(c["id"])
    db.audit(c["id"], "decision", f"{action} claim_risk={rk['claim_risk']} behaviour_risk={rk['behaviour_risk']}")

    db.run("DELETE FROM behaviour_features WHERE customer_id=:c", c=c["customer_id"])   # behaviour feature table
    db.insert("behaviour_features", customer_id=c["customer_id"], orders_total=f["orders_total"], refunds_total=f["refunds_total"],
              refunds_30d=f["refunds_30d"], refunds_90d=f["refunds_90d"], refunds_180d=f["refunds_180d"],
              refund_value_90d=f["refund_value_90d"], refund_value_180d=f["refund_value_180d"],
              refund_value_ratio_180d=f["refund_value_ratio_180d"], return_events_180d=f["return_events_180d"],
              return_exceptions_180d=f["return_exceptions_180d"], refund_rate=round(f["refund_rate"], 3), updated_at=db.now())
    db.run("DELETE FROM customer_risk_profile WHERE customer_id=:c", c=c["customer_id"])  # Customer_Risk_Profile (BR + Trust Index)
    db.insert("customer_risk_profile", customer_id=c["customer_id"], behavior_risk=rk["behaviour_risk"], trust_index=rk["trust_index"],
              components=json.dumps(rk["behaviour_components"]), updated_at=db.now())

    return {"result": {"claim_id": c["id"], "decision": action, "status": status, "claim_risk": rk["claim_risk"],
                       "behaviour_risk": rk["behaviour_risk"], "trust_index": rk["trust_index"], "exposure_risk": d["exposure_risk"],
                       "evidence_confidence": eo["evidence_confidence"], "costs": d["costs"], "cost_breakdown": d["cost_breakdown"],
                       "abuse_patterns": bo["abuse_patterns"], "flags": flags, "rationale": d["rationale"], "review_case_id": case_id}}


# ---- wiring --------------------------------------------------------------------------------------
def build_graph():
    g = StateGraph(State)
    for name, fn in [("gate", gate_node), ("features", features_node), ("evidence_match", evidence_match_node),
                     ("evidence_agent", evidence_agent_node), ("behaviour_agent", behaviour_agent_node),
                     ("risk_synthesis", risk_synthesis_node), ("cost", cost_node), ("resolution_agent", resolution_agent_node),
                     ("execute", execute_node)]:
        g.add_node(name, fn)
    for n in ("gate", "features", "evidence_match"):
        g.add_edge(START, n)
    g.add_edge(["gate", "evidence_match"], "evidence_agent")
    g.add_edge(["features", "evidence_match"], "behaviour_agent")
    g.add_edge(["evidence_agent", "behaviour_agent"], "risk_synthesis")
    g.add_edge("risk_synthesis", "cost")
    g.add_edge("cost", "resolution_agent")
    g.add_edge("resolution_agent", "execute")
    g.add_edge("execute", END)
    return g.compile()


GRAPH = build_graph()


def fallback_manual_review(claim, error):
    """Safe failure: if any node fails the claim is parked for a human - never silently approved or declined."""
    flags = ["system_error_routed_to_review"]
    row = dict(order_id=claim["order_id"], customer_id=claim["customer_id"], reason=claim["reason"], amount=claim["amount"],
               description=claim.get("description", ""), claim_risk=None, behaviour_risk=None, trust_index=None,
               evidence_confidence=None, exposure_risk=None, decision="manual_review", costs="{}", flags=json.dumps(flags),
               rationale=f"Automated analysis failed ({type(error).__name__}); routed to manual review.", status="under_review",
               components="{}")
    if db.q1("SELECT id FROM refund_requests WHERE id=:i", i=claim["id"]):
        db.update("refund_requests", claim["id"], **row)
    else:
        db.insert("refund_requests", id=claim["id"], created_at=db.now(), **row)
    case_id = db.open_review_case(claim["id"])
    db.audit(claim["id"], "system_error", f"{type(error).__name__}: {error}")
    return {"claim_id": claim["id"], "decision": "manual_review", "status": "under_review", "claim_risk": None, "behaviour_risk": None,
            "trust_index": None, "exposure_risk": None, "evidence_confidence": None, "costs": {}, "cost_breakdown": {},
            "abuse_patterns": [], "flags": flags, "rationale": row["rationale"], "review_case_id": case_id}


def process_claim(claim, image_bytes=None, resubmission=False):
    """Entry point used by the API and the demo. claim: order_id, customer_id, reason, amount, description."""
    claim = dict(claim)
    claim.setdefault("id", db.uid("CLM"))
    try:
        return GRAPH.invoke({"claim": claim, "image": image_bytes, "resubmission": resubmission})["result"]
    except Exception as e:  # noqa: BLE001 - deliberate catch-all: safe failure route
        return fallback_manual_review(claim, e)
