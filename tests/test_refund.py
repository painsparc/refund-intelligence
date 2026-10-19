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
os.environ.pop("LLM_API_KEY", None)
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


# ------------------------------------------------------------------ claim NLP: negation, paraphrase, pluggable transformer
@pytest.fixture()
def reset_nlp():
    yield
    ai.set_text_classifier(None)
    ai.set_sentiment_scorer(None)
    ai.set_image_text_scorer(None)
    ai._NLP.update(fn=None, tried=False, backend="rules")


def test_negation_is_understood():
    aff, neg = ai.rule_categories("It is not damaged, I just do not want it anymore.")
    assert "damaged" not in aff and "damaged" in neg and "changed_mind" in aff
    assert ai.rule_categories("The box is not broken but the charger does not work.") == (["defective"], ["damaged"])
    assert ai.rule_categories("No damage at all, works great.") == ([], ["damaged"])
    assert ai.rule_categories("The jar arrived cracked at the base.") == (["damaged"], [])
    assert ai.rule_categories("Parcel never arrived.")[0] == ["not_delivered"]               # phrases that embed a negation are positive
    i = ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="No damage at all, works great, but I would like to return it."))
    assert "claim_negated_by_text" in i["ambiguity_flags"] and i["ambiguity"] == 50
    j = ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="It is not damaged, I just do not want it anymore."))
    assert "reason_description_mismatch" in j["ambiguity_flags"]
    assert ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="The handle snapped off the first time I used it."))["ambiguity"] == 0


def test_transformer_backend_is_pluggable(reset_nlp):
    c = claim("ORD-1001", "C001", "damaged", desc="The ceramic mug came in two pieces.")
    assert ai.interpret_claim(c)["ambiguity_flags"] == []                                        # rules: no keyword -> no flag, no match
    assert ai.interpret_claim(c)["text_categories"] == []
    ai.set_text_classifier(lambda t: {"damaged": 0.93, "defective": 0.05, "changed_mind": 0.01})
    out = ai.interpret_claim(c)
    assert out["nlp_backend"] == "injected" and out["text_categories"] == ["damaged"] and out["ambiguity"] == 0
    ai.set_text_classifier(lambda t: {"changed_mind": 0.9, "damaged": 0.1})                       # model says the customer changed their mind
    out = ai.interpret_claim(c)
    assert "reason_description_mismatch" in out["ambiguity_flags"]
    assert ai.classify_text_category("anything")[1] == "injected"


def test_transformer_failure_falls_back_to_rules(monkeypatch, reset_nlp):
    monkeypatch.setenv("CLAIM_NLP", "transformer")
    monkeypatch.setitem(sys.modules, "transformers", None)                                       # simulate: model library not installed
    ai._NLP.update(fn=None, tried=False, backend="rules")
    out = ai.interpret_claim(claim("ORD-1001", "C001", "damaged", desc="The jar arrived cracked at the base."))
    assert out["nlp_backend"] == "rules" and out["ambiguity"] == 0


# ------------------------------------------------------------------ sentiment: customer experience only, never a fraud signal
def test_sentiment_labels(reset_nlp):
    assert ai.sentiment("I am absolutely furious, this is a disgrace!")["label"] == "negative"
    assert ai.sentiment("")["label"] == "neutral"
    ai.set_sentiment_scorer(lambda t: 0.9)
    assert ai.sentiment("whatever") == {"score": 0.9, "label": "positive", "backend": "injected"}


def test_sentiment_never_changes_risk_or_decision(reset_nlp):
    calm = claim("ORD-1002", "C003", "damaged", 2890, desc="Hi team, the kettle arrived broken and cracked. Could you please help? Many thanks!")
    angry = claim("ORD-1002", "C003", "damaged", 2890, desc="I am absolutely furious, this kettle is broken and cracked, a disgrace and terrible!")
    f = ai.build_features(calm, db.get_order("ORD-1002"))
    outs = []
    for c in (calm, angry):
        ev = ai.evidence_claim_agent(c, OK_GATE, GOOD_EVIDENCE)
        beh = ai.behaviour_abuse_agent(c, f, GOOD_EVIDENCE)
        risk = ai.synthesize_risk(ev, beh)
        outs.append((ev["components"], risk, ai.expected_cost_decision(c, OK_GATE, risk, beh, GOOD_EVIDENCE), ev["sentiment"]["score"]))
    assert outs[0][:3] == outs[1][:3]                                  # identical components, risk scores, costs and action
    assert outs[1][3] < outs[0][3] - 0.3                                # ... even though the sentiment differs a lot
    assert ai.cx_priority({"score": -0.8}, 80, "manual_review") is True
    assert ai.cx_priority({"score": -0.8}, 80, "auto_approve") is False
    assert ai.cx_priority({"score": -0.8}, 20, "manual_review") is False  # low-trust customers are not 'priority care'
    assert ai.cx_priority({"score": -0.1}, 80, "manual_review") is False


def test_cx_routing_end_to_end(reset_nlp):
    r = process_claim(claim("ORD-1005", "C002", "damaged", 1800, desc="I am absolutely furious, this sleeve is broken and torn, terrible and a disgrace!",
                            cid="CLM-cx"), None)
    assert r["decision"] != "auto_approve" and r["cx_priority"] is True and r["sentiment"]["label"] == "negative"
    row = db.q1("SELECT sentiment, cx_priority FROM refund_requests WHERE id='CLM-cx'")
    assert row["cx_priority"] == 1 and row["sentiment"] < -0.5
    assert db.q1("SELECT COUNT(*) AS n FROM action_log WHERE action='cx_priority'")["n"] == 1
    assert "cx_priority" not in r["flags"] and "sentiment" not in " ".join(r["flags"])             # not a risk flag
    assert ai.management_view()["customer_experience"]["cx_priority_claims"] == 1


# ------------------------------------------------------------------ image matching: pHash + ORB catch what average-hash misses
def _variants(raw):
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    dx, dy = int(im.width * .12), int(im.height * .12)
    crop = im.crop((dx, dy, im.width - dx, im.height - dy))
    out = io.BytesIO()
    crop.save(out, "JPEG", quality=85)
    small = io.BytesIO()
    im.resize((int(im.width * .6), int(im.height * .6))).save(small, "JPEG", quality=80)
    return out.getvalue(), small.getvalue()


def test_phash_and_orb_signatures_and_methods():
    pytest.importorskip("cv2")
    raw = make_image(5)
    sig = ai.image_signature(raw)
    assert len(sig["phash"]) == 16 and sig["orb"] and sig["orb"]["k"]
    cropped, resized = _variants(raw)
    m_crop = ai.similarity_methods(ai.image_signature(cropped), sig)
    assert "orb" in m_crop and "ahash" not in m_crop                         # a crop: only local features see it (average hash misses it)
    assert {"phash", "ahash"} & set(ai.similarity_methods(ai.image_signature(resized), sig))   # a resize: the hashes still agree
    assert ai.similarity_methods(ai.image_signature(make_image(77)), sig) == []                 # a different picture


def test_cropped_reuse_is_detected_across_customers():
    pytest.importorskip("cv2")
    raw = make_image(2)
    process_claim(claim("ORD-1002", "C003", "defective", 2890, cid="CLM-orig"), raw)
    cropped, _ = _variants(raw)
    ev = ai.evidence_match(cropped, claim("ORD-1004", "C005", cid="CLM-other"))
    assert ev["similar_reuse_other_customer"] and "orb" in ev["match_methods"] and not ev["exact_reuse_other_customer"]
    stored = db.q1("SELECT phash, orb FROM evidence WHERE claim_id='CLM-orig'")
    assert stored["phash"] and stored["orb"]


def test_text_image_consistency_hook(reset_nlp):
    raw = make_image(1)
    c = claim("ORD-1001", "C001", "damaged", 3500, desc="The jar arrived cracked at the base.")
    ev = ai.evidence_match(raw, c)
    assert ev["text_image_consistency"] is None                                                   # CLIP is off by default
    base = ai.evidence_inconsistency(c, ev)
    ai.set_image_text_scorer(lambda image_bytes, category: 0.1)                                   # the photo does NOT look damaged
    ev = ai.evidence_match(raw, c)
    assert ev["text_image_consistency"] == 0.1 and ai.evidence_inconsistency(c, ev) >= 90 > base
    assert "image_text_mismatch" in ai.evidence_claim_agent(c, OK_GATE, ev)["flags"]
    ai.set_image_text_scorer(lambda image_bytes, category: 0.95)                                  # the photo looks as claimed
    ev = ai.evidence_match(raw, c)
    assert ai.evidence_inconsistency(c, ev) == max(base, 5.0)                                      # 100*(1-0.95): a consistent photo adds nothing material
    calls = []
    ai.set_image_text_scorer(lambda image_bytes, category: calls.append(1) or 0.5)
    ai.evidence_match(raw, claim("ORD-1001", "C001", "changed_mind"))
    assert calls == []                                                                           # only physical-condition claims are checked


# ------------------------------------------------------------------ provider-neutral LLM layer
def test_llm_is_optional_bounded_and_provider_neutral(monkeypatch, reset_nlp):
    assert ai.llm_json("sys", {"x": 1}) is None                                              # not configured -> deterministic only
    c = claim("ORD-1001", "C001", "damaged", 2000, desc="The jar arrived cracked at the base.")
    base = ai.synthesize_risk(ai.evidence_claim_agent(c, OK_GATE, NO_EVIDENCE), {"abuse_patterns": [], "behaviour_risk": 0.0, "components": {}})
    # (a) injected provider
    ai.set_llm(lambda system, payload, image: {"risk_adjustment": 50, "flags": ["looks_staged"], "summary": "provider says so"})
    try:
        out = ai.evidence_claim_agent(c, OK_GATE, NO_EVIDENCE)
        assert out["llm_adjustment"] == 10 and "looks_staged" in out["flags"]                 # +50 is clipped to +10 points
        assert ai.synthesize_risk(out, {"abuse_patterns": [], "behaviour_risk": 0.0, "components": {}})["claim_risk"] == base["claim_risk"] + 10
        ai.set_llm(lambda *a: (_ for _ in ()).throw(RuntimeError("provider down")))
        assert ai.evidence_claim_agent(c, OK_GATE, NO_EVIDENCE)["llm_adjustment"] == 0        # a failing provider changes nothing
    finally:
        ai.set_llm(None)
    # (b) generic OpenAI-compatible HTTP endpoint (mocked)
    seen = {}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": 'noise {"risk_adjustment": -3, "summary": "ok"} noise'}}]}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, auth=headers["Authorization"], model=json["model"], roles=[m["role"] for m in json["messages"]])
        return Resp()
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.test/v1/")
    assert ai.llm_json("sys", {"x": 1}) == {"risk_adjustment": -3, "summary": "ok"}
    assert seen["url"] == "https://llm.example.test/v1/chat/completions" and seen["auth"] == "Bearer test-key" and seen["roles"] == ["system", "user"]
