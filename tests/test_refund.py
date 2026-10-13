"""pytest suite - run from the repo root:  pytest -q   (throw-away SQLite DB, rule-based agents, no API key).
Pins the CANONICAL MODEL of the final proposal: 0-100 scores, Behavioural Risk + Trust Index, Claim Risk, expected cost, 30/90/180-day features."""
import os
import re
import sys
import pathlib
import tempfile

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["EVIDENCE_DIR"] = f"{TMP}/evidence"
os.environ.pop("ANTHROPIC_API_KEY", None)
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402
import db  # noqa: E402
import intelligence as ai  # noqa: E402
import graph  # noqa: E402
from graph import process_claim  # noqa: E402
from demo import make_image, resized_copy  # noqa: E402

TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA)
OK_GATE = {"eligible": True, "violations": []}
NO_EVIDENCE = {"present": False}
GOOD_EVIDENCE = {"present": True, "quality": 1.0, "exact_reuse_other_customer": False, "similar_reuse_other_customer": False,
                 "reuse_same_customer": False}


@pytest.fixture(autouse=True)
def fresh_db():
    db.init()
    for t in TABLES:
        db.run(f"DELETE FROM {t}")
    db.seed()


def claim(order, cust, reason="damaged", amount=1000, desc="The item arrived broken and unusable.", cid="CLM-test"):
    return dict(id=cid, order_id=order, customer_id=cust, reason=reason, amount=amount, description=desc)


def zero_features(**kw):
    f = dict(orders_total=10, refunds_total=0, refund_rate=0.0, refunds_30d=0, refunds_90d=0, refunds_180d=0, refund_value_90d=0,
             refund_value_180d=0, purchase_value_180d=0, refund_value_ratio_180d=0.0, top_reason_count_180d=0, return_events_180d=0,
             return_exceptions_180d=0, evidence_reuse_history=0, near_limit_refunds=0, late_window_refunds=0, defect_claims_90d=0,
             weak_defect_claims=0, claims_24h=0, account_age_days=300, amount=1000)
    f.update(kw)
    return f


def decide(exposure, ev=GOOD_EVIDENCE, amount=1000, reason="damaged", **kw):
    c = claim("ORD-1001", "C001", reason, amount)
    return ai.expected_cost_decision(c, OK_GATE, {"exposure_risk": exposure}, {"blacklisted": False}, ev, **kw)


# ------------------------------------------------------------------ deterministic policy gate
def test_policy_gate():
    assert ai.policy_gate(claim("ORD-1001", "C001", amount=3500))["eligible"]
    late = ai.policy_gate(claim("ORD-1006", "C002", "not_as_described", 700))
    assert any(v.startswith("outside_return_window") for v in late["violations"])
    assert "order_belongs_to_other_customer" in ai.policy_gate(claim("ORD-1001", "C002", amount=3500))["violations"]
    assert "amount_exceeds_order_value" in ai.policy_gate(claim("ORD-1001", "C001", amount=99999))["violations"]
    assert ai.policy_gate(claim("ORD-NOPE", "C001"))["violations"] == ["order_not_found"]


# ------------------------------------------------------------------ evidence reuse detection
def test_evidence_reuse_detection():
    img = make_image(2)
    process_claim(claim("ORD-1002", "C003", "defective", 2890, cid="CLM-orig"), img)
    other = claim("ORD-1004", "C005", cid="CLM-other")
    assert ai.evidence_match(img, other)["exact_reuse_other_customer"]
    sim = ai.evidence_match(resized_copy(img), other)
    assert sim["similar_reuse_other_customer"] and not sim["exact_reuse_other_customer"]
    fresh = ai.evidence_match(make_image(77), other)
    assert not fresh["exact_reuse_other_customer"] and not fresh["similar_reuse_other_customer"]
    own = ai.evidence_match(img, claim("ORD-1002", "C003", cid="CLM-second"))
    assert own["reuse_same_customer"] and not own["exact_reuse_other_customer"]
    assert ai.evidence_match(b"not an image", other) == {"present": False, "invalid_file": True}   # corrupt file is handled


# ------------------------------------------------------------------ proposal formulas (0-100)
def test_weights_match_the_proposal():
    assert ai.BEHAV_W == {"frequency": 0.35, "value_ratio": 0.25, "repeat_reason": 0.15, "return_exception": 0.15, "evidence_reuse": 0.10}
    assert ai.CLAIM_W == {"ambiguity": 0.30, "inconsistency": 0.30, "evidence_reuse": 0.20, "abuse_strength": 0.20}
    assert sum(ai.BEHAV_W.values()) == pytest.approx(1.0) and sum(ai.CLAIM_W.values()) == pytest.approx(1.0)


def test_behavioural_risk_and_trust_index():
    worst = zero_features(refunds_30d=3, refunds_90d=5, refunds_180d=8, refund_value_ratio_180d=0.5, top_reason_count_180d=4,
                          return_exceptions_180d=3, evidence_reuse_history=2)
    br, comps = ai.behaviour_risk_formula(worst)
    assert comps == {"frequency": 100.0, "value_ratio": 100.0, "repeat_reason": 100.0, "return_exception": 100.0, "evidence_reuse": 100.0}
    assert br == 100.0 and ai.trust_index(br) == 0.0
    br, comps = ai.behaviour_risk_formula(zero_features(refunds_90d=2, refunds_180d=2, refund_value_ratio_180d=0.25, top_reason_count_180d=2))
    assert comps["frequency"] == 40.0 and comps["value_ratio"] == 50.0 and comps["repeat_reason"] == pytest.approx(33.3, abs=0.1)
    assert br == pytest.approx(0.35 * 40 + 0.25 * 50 + 0.15 * 33.3, abs=0.1)
    assert ai.trust_index(br) == pytest.approx(100 - br)
    assert ai.behaviour_risk_formula(zero_features())[0] == 0.0
    # frequency is judged on all three rolling windows
    assert ai.behaviour_risk_formula(zero_features(refunds_180d=4))[1]["frequency"] == 50.0
    assert ai.behaviour_risk_formula(zero_features(refunds_30d=3))[1]["frequency"] == 100.0


def test_claim_ambiguity_interpretation():
    ok = ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="The jar arrived cracked at the base."))
    assert ok["ambiguity"] == 0 and ok["reason_category"] == "damaged"
    assert "thin_description" in ai.interpret_claim(claim("ORD-1001", "C001", desc="Broken."))["ambiguity_flags"]
    assert "no_description" in ai.interpret_claim(claim("ORD-1001", "C001", desc=""))["ambiguity_flags"]
    assert "reason_description_mismatch" in ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="I changed my mind, do not like it"))["ambiguity_flags"]
    assert "reason_category_conflict" in ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="You sent the wrong item entirely"))["ambiguity_flags"]
    assert "vague_description" in ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="There is some problem with it."))["ambiguity_flags"]


def test_claim_risk_formula():
    c = claim("ORD-1001", "C001", "damaged", 2000, desc="The jar arrived cracked at the base.")
    out = ai.evidence_claim_agent(c, OK_GATE, NO_EVIDENCE)
    assert out["components"] == {"ambiguity": 0.0, "inconsistency": 65.0, "evidence_reuse": 0.0}
    risk = ai.synthesize_risk(out, {"abuse_patterns": [], "behaviour_risk": 0.0, "components": {}})
    assert risk["claim_risk"] == pytest.approx(0.30 * 0 + 0.30 * 65 + 0.20 * 0 + 0.20 * 0)                 # 19.5
    one = ai.synthesize_risk(out, {"abuse_patterns": ["serial_refunder"], "behaviour_risk": 0.0, "components": {}})
    assert one["claim_risk"] == pytest.approx(19.5 + 0.20 * 50)
    two = ai.synthesize_risk(out, {"abuse_patterns": ["a", "b", "c"], "behaviour_risk": 0.0, "components": {}})
    assert two["claim_components"]["abuse_strength"] == 100.0                                              # capped
    reused = ai.evidence_claim_agent(c, OK_GATE, dict(GOOD_EVIDENCE, exact_reuse_other_customer=True))
    assert reused["components"]["evidence_reuse"] == 100.0 and reused["evidence_confidence"] == 2.0
    # exposure risk = 0.6 CR + 0.4 BR, on a 0-1 scale
    mix = ai.synthesize_risk(out, {"abuse_patterns": [], "behaviour_risk": 50.0, "components": {}})
    assert mix["exposure_risk"] == pytest.approx((0.6 * 19.5 + 0.4 * 50) / 100, abs=1e-4) and mix["trust_index"] == 50.0


def test_agent_nudge_is_bounded_and_cannot_lower_hard_signals():
    assert ai.llm_adjustment({"risk_adjustment": -50}) == -10 and ai.llm_adjustment({"risk_adjustment": 50}) == 10
    assert ai.llm_adjustment({"risk_adjustment": -50}, hard=True) == 0 and ai.llm_adjustment(None) == 0


# ------------------------------------------------------------------ abuse patterns + rolling windows
def test_rolling_windows_and_return_features():
    c = claim("ORD-1002", "C003", "defective", 2890)
    f = ai.build_features(c, db.get_order("ORD-1002"))
    assert f["refunds_30d"] <= f["refunds_90d"] <= f["refunds_180d"] and f["refunds_180d"] == 7
    assert f["return_events_180d"] == 7 and f["return_exceptions_180d"] == 4          # 2 missing + 2 late
    assert 0 < f["refund_value_ratio_180d"] <= 1.0
    assert ai.build_features(claim("ORD-1001", "C001", amount=3500), db.get_order("ORD-1001"))["refunds_180d"] == 0


def test_serial_refunder_detection():
    c = claim("ORD-1002", "C003", "defective", 2890)
    f = ai.build_features(c, db.get_order("ORD-1002"))
    patterns = ai.detect_abuse_patterns(f, c, NO_EVIDENCE)
    for p in ("serial_refunder", "return_abuse", "refund_farming", "policy_exploitation"):
        assert p in patterns
    assert ai.behaviour_risk_formula(f)[0] >= 80
    good = claim("ORD-1001", "C001", amount=3500)
    fg = ai.build_features(good, db.get_order("ORD-1001"))
    assert ai.detect_abuse_patterns(fg, good, NO_EVIDENCE) == [] and ai.behaviour_risk_formula(fg)[0] == 0.0


def test_each_pattern_has_an_explicit_rule():
    c = claim("ORD-1001", "C001", "damaged")
    assert ai.detect_abuse_patterns(zero_features(refunds_30d=3), c, NO_EVIDENCE) == ["serial_refunder"]
    assert ai.detect_abuse_patterns(zero_features(refunds_180d=7), c, NO_EVIDENCE) == ["serial_refunder"]
    assert ai.detect_abuse_patterns(zero_features(return_exceptions_180d=2), c, NO_EVIDENCE) == ["return_abuse"]
    assert ai.detect_abuse_patterns(zero_features(near_limit_refunds=3), c, NO_EVIDENCE) == ["refund_farming"]
    assert ai.detect_abuse_patterns(zero_features(refunds_180d=4, refund_value_ratio_180d=0.6), c, NO_EVIDENCE) == ["refund_farming"]
    assert ai.detect_abuse_patterns(zero_features(late_window_refunds=3), c, NO_EVIDENCE) == ["policy_exploitation"]
    assert ai.ABUSE_PATTERNS == ["serial_refunder", "return_abuse", "fake_defect_pattern", "refund_farming", "policy_exploitation"]


def test_fake_defect_pattern():
    c = claim("ORD-1001", "C001", "damaged")
    reused = {"present": True, "quality": 0.9, "similar_reuse_other_customer": True}
    assert "fake_defect_pattern" in ai.detect_abuse_patterns(zero_features(), c, reused)
    assert "fake_defect_pattern" in ai.detect_abuse_patterns(zero_features(defect_claims_90d=2, weak_defect_claims=1), c, NO_EVIDENCE)
    assert "fake_defect_pattern" not in ai.detect_abuse_patterns(zero_features(), c, NO_EVIDENCE)               # one-off, no history
    assert "fake_defect_pattern" not in ai.detect_abuse_patterns(zero_features(), claim("ORD-1001", "C001", "changed_mind"), reused)


# ------------------------------------------------------------------ expected-cost resolution
def test_expected_cost_components_and_selection():
    table = ai.cost_table(1000, 0.25)
    for a, parts in table.items():
        assert parts["total"] == pytest.approx(parts["risk_exposure"] + parts["customer_friction"] + parts["handling"], abs=0.2)
    assert table["auto_approve"]["risk_exposure"] == 250.0 and table["manual_review"]["handling"] == 100
    assert decide(0.0)["action"] == "auto_approve"
    assert decide(0.15)["action"] == "refund_after_return"                       # some risk, evidence fine -> wait for the item
    assert decide(0.25, ev=NO_EVIDENCE)["action"] == "request_evidence"          # evidence is the open question
    assert decide(0.80, ev=NO_EVIDENCE)["action"] == "manual_review"             # high exposure -> human
    assert decide(0.30)["action"] == "manual_review"
    assert "request_evidence" not in decide(0.25)["eligible"]                    # good evidence -> not an evidence problem
    assert decide(0.25, ev=NO_EVIDENCE, resubmission=True)["action"] != "request_evidence"      # asked at most once
    assert "refund_after_return" not in decide(0.15, reason="not_delivered")["eligible"]        # nothing to send back
    d = decide(0.25, ev=NO_EVIDENCE)
    assert d["action"] == min(d["eligible"], key=lambda a: d["costs"][a])
    tiny = decide(0.02, amount=100)
    assert tiny["action"] == "auto_approve"


# ------------------------------------------------------------------ guardrails
def test_blacklist_is_a_hard_signal_not_a_weight():
    c = claim("ORD-1001", "C001", amount=3500, cid="CLM-a")
    f = ai.build_features(c, db.get_order("ORD-1001"))
    before = ai.behaviour_abuse_agent(c, f, NO_EVIDENCE)
    db.insert("blacklist", customer_id="C001", reason="test", created_at=db.now())
    after = ai.behaviour_abuse_agent(c, f, NO_EVIDENCE)
    assert before["blacklisted"] is False and after["blacklisted"] is True
    assert after["behaviour_risk"] == before["behaviour_risk"] and after["trust_index"] == before["trust_index"]   # not in the formula
    r = process_claim(c, make_image(1))
    assert r["decision"] == "manual_review" and "blacklisted_customer" in r["flags"]                               # the guardrail routes it
    assert r["review_case_id"] and db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE order_id='ORD-1001'")["n"] == 0


def test_policy_gate_is_a_deterministic_decline():
    r = process_claim(claim("ORD-1006", "C002", "not_as_described", 700), make_image(6))
    assert r["decision"] == "policy_decline" and r["status"] == "declined" and any(f.startswith("policy:") for f in r["flags"])
    assert r["review_case_id"] is None
    assert db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE order_id='ORD-1006'")["n"] == 0              # no refund executed
    assert db.q1("SELECT COUNT(*) AS n FROM action_log WHERE action='policy_decline'")["n"] == 1
    # the resolution agent can never turn a decline into anything else, even if an LLM asked it to
    d = {"action": "policy_decline", "costs": {a: 1 for a in ai.ACTIONS}, "forced": ["policy:x"], "exposure_risk": 0.1}
    out = ai.resolution_agent(claim("ORD-1006", "C002"), {"flags": []}, {"abuse_patterns": []},
                              {"claim_risk": 0, "behaviour_risk": 0, "trust_index": 100}, d)
    assert out["action"] == "policy_decline"


def test_exact_evidence_reuse_forces_review():
    img = make_image(2)
    process_claim(claim("ORD-1002", "C003", "defective", 2890, cid="CLM-a"), img)
    r2 = process_claim(claim("ORD-1004", "C005", "damaged", 2400, cid="CLM-b"), img)
    assert r2["decision"] == "manual_review" and "evidence_reused_across_customers" in r2["flags"]


def test_system_failure_routes_to_manual_review(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(graph.GRAPH, "invoke", boom)
    r = process_claim(claim("ORD-1001", "C001", amount=3500, cid="CLM-err"), make_image(1))
    assert r["decision"] == "manual_review" and r["status"] == "under_review" and "system_error_routed_to_review" in r["flags"]
    assert r["review_case_id"] and db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE order_id='ORD-1001' AND amount=3500")["n"] == 0


# ------------------------------------------------------------------ end-to-end routing + lifecycle
def test_end_to_end_routing():
    serial_img = make_image(2)
    r1 = process_claim(claim("ORD-1001", "C001", "damaged", 3500, desc="The jar arrived cracked at the base.", cid="CLM-1"), make_image(1))
    r2 = process_claim(claim("ORD-1002", "C003", "defective", 2890, cid="CLM-2"), serial_img)
    r3 = process_claim(claim("ORD-1003", "C004", "damaged", 18000, desc="Screen is cracked.", cid="CLM-3"), make_image(3, blurry=True))
    r4 = process_claim(claim("ORD-1004", "C005", "damaged", 2400, cid="CLM-4"), resized_copy(serial_img))
    r5 = process_claim(claim("ORD-1005", "C002", "damaged", 1800, desc="Stitching came apart on the sleeve.", cid="CLM-5"), None)
    r6 = process_claim(claim("ORD-1006", "C002", "not_as_described", 700, cid="CLM-6"), make_image(6))
    assert [r["decision"] for r in (r1, r2, r3, r4, r5, r6)] == [
        "auto_approve", "manual_review", "manual_review", "manual_review", "request_evidence", "policy_decline"]
    assert "serial_refunder" in r2["abuse_patterns"] and "fake_defect_pattern" in r4["abuse_patterns"]
    for r in (r1, r2, r5):                                                         # every score is on the 0-100 scale
        assert 0 <= r["claim_risk"] <= 100 and 0 <= r["behaviour_risk"] <= 100 and r["trust_index"] == pytest.approx(100 - r["behaviour_risk"])
        assert set(r["cost_breakdown"]["auto_approve"]) == {"risk_exposure", "customer_friction", "handling", "total"}

    assert db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE order_id='ORD-1001' AND amount=3500")["n"] == 1
    assert r2["review_case_id"] and r5["status"] == "awaiting_evidence"
    assert db.q1("SELECT COUNT(*) AS n FROM action_log WHERE action='evidence_requested'")["n"] == 1
    prof = db.q1("SELECT * FROM customer_risk_profile WHERE customer_id='C003'")
    assert prof["behavior_risk"] == r2["behaviour_risk"] and prof["trust_index"] == r2["trust_index"]

    # evidence loop: the customer uploads a photo -> the claim is re-evaluated (never asked again)
    again = process_claim(claim("ORD-1005", "C002", "damaged", 1800, desc="Stitching came apart on the sleeve.", cid="CLM-5"),
                          make_image(55), resubmission=True)
    assert again["decision"] == "refund_after_return" and again["status"] == "awaiting_return"
    # refund after return: the existing return system reports the item came back -> refund released
    assert ai.record_return("CLM-5", "received")["claim_status"] == "refunded"
    assert db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE order_id='ORD-1005'")["n"] == 1
    assert db.q1("SELECT status FROM return_events WHERE claim_id='CLM-5'")["status"] == "received"

    # human in the loop: a fraud outcome blacklists the customer
    ai.resolve_review(r2["review_case_id"], "fraud", "serial")
    assert db.q1("SELECT 1 AS x FROM blacklist WHERE customer_id='C003'")


def test_missing_return_goes_to_review_and_counts_as_an_exception():
    db.insert("refund_requests", id="CLM-ret", order_id="ORD-1001", customer_id="C001", reason="damaged", amount=100, description="",
              created_at=db.now(), decision="refund_after_return", costs="{}", flags="[]", rationale="", status="awaiting_return", components="{}")
    with pytest.raises(ValueError):
        ai.record_return("CLM-ret", "teleported")
    assert ai.record_return("CLM-ret", "missing")["claim_status"] == "under_review"
    assert db.q1("SELECT COUNT(*) AS n FROM review_cases WHERE claim_id='CLM-ret'")["n"] == 1
    f = ai.build_features(claim("ORD-1001", "C001", amount=100, cid="CLM-next"), db.get_order("ORD-1001"))
    assert f["return_events_180d"] == 1 and f["return_exceptions_180d"] == 1


# ------------------------------------------------------------------ historical loop + architecture rules
def test_historical_loop_persists_and_tracks_outcome():
    first = ai.run_historical_loop()
    assert first["products"] > 0 and first["new_insights"]
    assert db.q1("SELECT COUNT(*) AS n FROM product_risk_snapshot")["n"] == first["products"]
    assert db.q1("SELECT COUNT(*) AS n FROM vendor_risk_snapshot")["n"] == first["vendors"]
    assert ai.run_historical_loop()["new_insights"] == []                       # no duplicate open insights
    product = next(i for i in ai.list_insights("proposed") if i["kind"] == "product")
    with pytest.raises(ValueError):
        ai.evaluate_outcome(product["id"])                                        # cannot evaluate before intervening
    assert ai.apply_intervention(product["id"], "QC audit")["status"] == "applied"
    done = ai.evaluate_outcome(product["id"])
    assert done["status"] == "evaluated" and done["outcome"] in ("improved", "no_change", "worse")
    f = ai.build_features(claim("ORD-1004", "C005", amount=2400), db.get_order("ORD-1004"))
    assert f["product_flagged_by_prevention_loop"] is True                       # the loop feeds later claims as agent context


def test_architecture_rules():
    graph_src = (ROOT / "graph.py").read_text()
    assert "run_historical_loop" not in graph_src                                 # the loop stays outside the real-time graph
    assert graph_src.count("_agent_node(s)") == 3                                 # exactly 3 agent nodes in the graph
    assert sorted(n for n in dir(ai) if n.endswith("_agent")) == ["behaviour_abuse_agent", "evidence_claim_agent", "resolution_agent"]
    assert list(ai.abuse_pattern_counts()) == ai.ABUSE_PATTERNS
    assert ai.ALL_ACTIONS == ["auto_approve", "refund_after_return", "request_evidence", "manual_review", "policy_decline"]
