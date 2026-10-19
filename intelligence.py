"""
intelligence.py  -  OUR MVP: INTELLIGENCE LAYER (the only genuinely new logic).  Canonical model = the final proposal.

The FIVE MVP CONTRIBUTIONS                 ->  where they live in this file
  1 Evidence Intelligence                  ->  evidence_match() + evidence_inconsistency + evidence_claim_agent()
  2 Customer Behavioural Risk              ->  build_features() + behaviour_risk_formula() + behaviour_abuse_agent()
  3 Claim & Abuse Risk                     ->  interpret_claim() + claim_risk_formula() + detect_abuse_patterns() (5 named patterns)
  4 Expected-Cost Resolution               ->  expected_cost_decision() + resolution_agent()
  5 Historical Intelligence & Prevention Loop -> bottom half of this file (offline, outside the real-time graph)
Deterministic blocks: policy_gate, build_features, evidence_match, interpret_claim, the formulas, abuse rules, cost model, guardrails.
AI agents (exactly 3): Evidence & Claim Agent, Behaviour & Abuse Agent, Resolution Reasoning Agent.
With no LLM configured (LLM_API_KEY) the agents add nothing and every number is exactly the documented formula.

------------------------------------------------------------------------------------------------------------------------------
SCORES ARE 0-100 EVERYWHERE (backend, API, Streamlit, report).  Components are normalised to 0-100 by the transparent rules below.

BEHAVIOURAL RISK   BR = 0.35 FrequencyRisk + 0.25 ValueRatioRisk + 0.15 RepeatReasonRisk + 0.15 ReturnEventExceptionRisk
                         + 0.10 EvidenceReuseRisk                          TRUST INDEX = 100 - BR
   FrequencyRisk       max over windows w in {30,90,180} days of 100*min(1, refunds_w / FREQ_SAT[w])      (3 / 5 / 8 refunds)
   ValueRatioRisk      100*min(1, (refund value / purchase value over 180 days) / 0.5)
   RepeatReasonRisk    100*min(1, (largest count of one refund reason in 180 days - 1) / 3)
   ReturnEventExceptionRisk  100*min(1, late-or-missing return events in 180 days / 3)      (none recorded -> 0, coverage reported)
   EvidenceReuseRisk   100*min(1, prior claims of this customer flagged for evidence reuse / 2)
   Blacklist is NOT in the formula: it is a deterministic hard signal that routes the claim to manual review.

CLAIM RISK         CR = 0.30 ClaimAmbiguity + 0.30 EvidenceInconsistency + 0.20 EvidenceReuse + 0.20 AbusePatternStrength
   ClaimAmbiguity         interpret_claim(): empty/thin description, reason-vs-text conflict, change-of-mind wording, vague wording
   EvidenceInconsistency  physical claim without evidence 65 | with evidence 100*(1 - image quality) | non-physical without evidence 0
   EvidenceReuse          exact copy of another customer's photo 100 | near-duplicate 70 | own earlier claim 50 | none 0
   AbusePatternStrength   50 per named abuse pattern detected, capped at 100

EXPECTED COST      ExpectedCost(a) = RiskExposure(a) + CustomerFrictionCost(a) + HandlingCost(a);  cheapest eligible action wins
   RiskExposure(a) = refund amount x exposure risk x EXPOSURE_FACTOR[a]  with exposure risk = (0.6 CR + 0.4 BR) / 100
   (a risk-normalised PROXY, not a calibrated probability; the 0.6/0.4 mix is a prototype setting - the proposal gives none)
   Actions: auto_approve (immediate refund) | refund_after_return | request_evidence | manual_review | policy_decline (deterministic only)
"""
import os
import io
import re
import json
import base64
import hashlib
from collections import Counter
from datetime import datetime, timedelta
from PIL import Image, ImageStat, ImageFilter
import db

MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")   # any model name your OpenAI-compatible provider serves
PHYSICAL_REASONS = {"damaged", "defective", "wrong_item", "not_as_described"}
ABUSE_PATTERNS = ["serial_refunder", "return_abuse", "fake_defect_pattern", "refund_farming", "policy_exploitation"]

# ---- documented weights / thresholds (single source of truth; tests pin them) --------------------------------------------
BEHAV_W = {"frequency": 0.35, "value_ratio": 0.25, "repeat_reason": 0.15, "return_exception": 0.15, "evidence_reuse": 0.10}
CLAIM_W = {"ambiguity": 0.30, "inconsistency": 0.30, "evidence_reuse": 0.20, "abuse_strength": 0.20}
EXPOSURE_W = {"claim": 0.6, "behaviour": 0.4}          # prototype choice for the risk used in RiskExposure
FREQ_SAT = {30: 3, 90: 5, 180: 8}                      # refunds in the window at which FrequencyRisk reaches 100
VALUE_RATIO_SAT, REPEAT_REASON_SAT, RETURN_EXC_SAT, REUSE_HISTORY_SAT = 0.5, 3, 3, 2
ABUSE_PER_PATTERN = 50
EVIDENCE_ASK_MIN = 30                                  # request_evidence is only eligible when EvidenceInconsistency >= 30
RISK_ADJUST_MAX = 10                                   # an agent may nudge CR / BR by at most +/-10 points
# abuse-pattern rules
SERIAL = {30: 3, 90: 4, 180: 7}                        # refunds within 30 / 90 / 180 days that make a serial refunder
RETURN_ABUSE_EXCEPTIONS = 2                            # late-or-missing return events in 180 days
FARMING_NEAR_LIMIT, FARMING_VALUE_RATIO, FARMING_MIN_REFUNDS = 3, 0.5, 4
POLICY_EDGE_REFUNDS = 3                                # refunds filed in the last 20% of the return window

ACTIONS = ["auto_approve", "refund_after_return", "request_evidence", "manual_review"]   # least -> most cautious
ALL_ACTIONS = ACTIONS + ["policy_decline"]
# cost matrix: cost(a) = amount*risk*EXPOSURE_FACTOR + FRICTION_FIXED + amount*(1-risk)*FRICTION_RATE + HANDLING
EXPOSURE_FACTOR = {"auto_approve": 1.00, "refund_after_return": 0.20, "request_evidence": 0.15, "manual_review": 0.03}
FRICTION_FIXED = {"auto_approve": 0, "refund_after_return": 25, "request_evidence": 10, "manual_review": 0}
FRICTION_RATE = {"auto_approve": 0.0, "refund_after_return": 0.05, "request_evidence": 0.05, "manual_review": 0.05}
HANDLING = {"auto_approve": 0, "refund_after_return": 40, "request_evidence": 10, "manual_review": 100}


COST_SCALE = float(os.getenv("COST_SCALE", "1.0"))    # scales friction + handling costs to the currency / claim size of the deployment


def clip(x, lo=0.0, hi=1.0):
    return round(max(lo, min(hi, x)), 4)


def clip100(x):
    return round(max(0.0, min(100.0, x)), 1)


def parse(ts):
    return datetime.fromisoformat(ts) if ts else None


def ref_time(claim):
    """'Now' for a claim. Live claims use the clock; evaluation data (2009-2018) sets claim['claim_time'] explicitly."""
    return parse(claim["claim_time"]) if claim.get("claim_time") else datetime.utcnow()


# =========================================================================== LLM helper
_LLM = {"fn": None}


def set_llm(fn):
    """Plug in any language-model provider: fn(system: str, payload: dict, image_bytes: bytes | None) -> dict | None. None resets."""
    _LLM["fn"] = fn


def llm_json(system, payload, image_bytes=None):
    """Ask an LLM for a JSON answer. Provider-neutral: an injected function (set_llm) or any OpenAI-compatible chat-completions endpoint
    configured with LLM_API_KEY / LLM_BASE_URL / LLM_MODEL. Returns None (-> deterministic result only) if not configured or on any error."""
    if _LLM["fn"]:
        try:
            return _LLM["fn"](system, payload, image_bytes)
        except Exception as e:
            print(f"[llm fallback] {e}")
            return None
    key = os.getenv("LLM_API_KEY")
    if not key:
        return None
    try:
        import requests
        content = [{"type": "text", "text": json.dumps(payload, default=str)}]
        if image_bytes:
            img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            img.thumbnail((1024, 1024))
            buf = io.BytesIO()
            img.save(buf, "JPEG")
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
        r = requests.post(os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
                          headers={"Authorization": f"Bearer {key}"}, timeout=60,
                          json={"model": MODEL, "max_tokens": 700, "response_format": {"type": "json_object"},
                                "messages": [{"role": "system", "content": system + "\nReply with ONLY a JSON object."},
                                             {"role": "user", "content": content}]})
        r.raise_for_status()
        t = r.json()["choices"][0]["message"]["content"]
        return json.loads(t[t.index("{"): t.rindex("}") + 1])
    except Exception as e:  # never let the AI layer break the refund flow
        print(f"[llm fallback] {e}")
        return None


def llm_adjustment(llm, hard=False):
    """Bounded risk nudge from an agent (+/-RISK_ADJUST_MAX points). A hard deterministic signal is never nudged DOWN."""
    if not llm:
        return 0.0
    adj = llm.get("risk_adjustment", 0)
    adj = max(-RISK_ADJUST_MAX, min(RISK_ADJUST_MAX, float(adj))) if isinstance(adj, (int, float)) else 0.0
    return max(adj, 0.0) if hard else adj


# =========================================================================== DETERMINISTIC 1: order + policy gate
def policy_gate(claim):
    order = db.get_order(claim["order_id"])
    v = []
    if not order:
        return {"eligible": False, "violations": ["order_not_found"], "order": None, "policy": None}
    policy = db.get_policy(order["category"])
    if order["customer_id"] != claim["customer_id"]:
        v.append("order_belongs_to_other_customer")
    if order["status"] != "delivered" and claim["reason"] != "not_delivered":
        v.append("order_not_delivered")
    if claim["amount"] > order["amount"] + 0.01:
        v.append("amount_exceeds_order_value")
    if policy and not policy["refundable"]:
        v.append("category_not_refundable")
    if policy and order["delivered_at"]:
        days = (ref_time(claim) - parse(order["delivered_at"])).total_seconds() / 86400
        if days > policy["window_days"]:
            v.append(f"outside_return_window({days:.0f}d>{policy['window_days']}d)")
    return {"eligible": not v, "violations": v, "order": order, "policy": policy}


# =========================================================================== DETERMINISTIC 2: feature builder (30/90/180-day windows)
def _latest_snapshot(table, col, val):
    if not val:
        return None
    return db.q1(f"SELECT outlier, risk_score FROM {table} WHERE {col}=:v ORDER BY snapshot_at DESC LIMIT 1", v=val)


def build_features(claim, order):
    cid = claim["customer_id"]
    t = ref_time(claim)
    hist = db.q("SELECT h.*, o.delivered_at, p.window_days, p.max_auto_amount FROM refund_history h "
                "JOIN orders o ON o.id=h.order_id JOIN policies p ON p.category=h.category WHERE h.customer_id=:c", c=cid)
    claims = db.q("SELECT created_at, reason, decision, evidence_confidence, flags FROM refund_requests WHERE customer_id=:c AND id<>:i",
                  c=cid, i=claim["id"])
    orders = db.q("SELECT amount, order_date FROM orders WHERE customer_id=:c AND status<>'cancellation_invoice'", c=cid)
    returns = db.q("SELECT status, event_at FROM return_events WHERE customer_id=:c", c=cid)
    cust = db.q1("SELECT created_at FROM customers WHERE id=:c", c=cid)

    def days_since(ts):
        return (t - parse(ts)).total_seconds() / 86400

    win = lambda rows, key, d: [r for r in rows if r[key] and 0 <= days_since(r[key]) <= d]
    r30, r90, r180 = (win(hist, "created_at", d) for d in (30, 90, 180))
    value180 = sum(h["amount"] for h in r180)
    bought180 = sum(o["amount"] for o in win(orders, "order_date", 180))
    ratio180 = (value180 / bought180) if bought180 > 0 else (1.0 if value180 > 0 else 0.0)
    reasons = Counter(h["reason"] for h in r180 if h["reason"] != "cancellation_proxy")
    ret180 = win(returns, "event_at", 180)
    near_limit = [h for h in hist if 0.85 * h["max_auto_amount"] <= h["amount"] <= h["max_auto_amount"]]
    late_window = [h for h in hist if h["delivered_at"] and
                   (parse(h["created_at"]) - parse(h["delivered_at"])).total_seconds() / 86400 >= 0.8 * h["window_days"]]
    pid, vid = (order["product_id"], order["vendor_id"]) if order else (None, None)
    defect_90d = (sum(1 for h in r90 if h["reason"] in PHYSICAL_REASONS) +
                  sum(1 for c in claims if c["reason"] in PHYSICAL_REASONS and c["decision"] != "auto_approve" and c["created_at"]
                      and 0 <= days_since(c["created_at"]) <= 90))
    weak_defect = sum(1 for c in claims if c["reason"] in PHYSICAL_REASONS and (c["evidence_confidence"] or 0) < 50)
    reuse_history = sum(1 for c in claims if c["flags"] and ("reused" in c["flags"] or "evidence_similar" in c["flags"]))

    def rate(col, val):
        if not val:
            return 0.0
        o = db.q1(f"SELECT COUNT(*) AS n FROM orders WHERE {col}=:v", v=val)["n"]
        r = db.q1(f"SELECT COUNT(*) AS n FROM refund_history WHERE {col}=:v", v=val)["n"]
        return r / max(o, 1)

    ps, vs = _latest_snapshot("product_risk_snapshot", "product_id", pid), _latest_snapshot("vendor_risk_snapshot", "vendor_id", vid)
    return {
        "orders_total": len(orders), "refunds_total": len(hist), "refund_rate": len(hist) / max(len(orders), 1),
        "refunds_30d": len(r30), "refunds_90d": len(r90), "refunds_180d": len(r180),
        "refund_value_90d": sum(h["amount"] for h in r90), "refund_value_180d": value180, "purchase_value_180d": bought180,
        "refund_value_ratio_180d": round(ratio180, 4), "top_reason_count_180d": max(reasons.values(), default=0),
        "return_events_180d": len(ret180), "return_exceptions_180d": sum(1 for r in ret180 if r["status"] in ("late", "missing")),
        "evidence_reuse_history": reuse_history,
        "near_limit_refunds": len(near_limit), "late_window_refunds": len(late_window),
        "defect_claims_90d": defect_90d, "weak_defect_claims": weak_defect,
        "claims_24h": sum(1 for c in claims if c["created_at"] and 0 <= days_since(c["created_at"]) <= 1),
        "account_age_days": days_since(cust["created_at"]) if cust else 0,
        "product_refund_rate": rate("product_id", pid), "vendor_refund_rate": rate("vendor_id", vid),
        # context fed back by the prevention loop (agents see it; it is NOT part of the formulas)
        "product_flagged_by_prevention_loop": bool(ps and ps["outlier"]), "vendor_flagged_by_prevention_loop": bool(vs and vs["outlier"]),
        "amount": claim["amount"],
    }


# =========================================================================== DETERMINISTIC 3: evidence match
def _hamming(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# Reuse detection = three signals; ANY of them marks two photos as the same picture:
#   aHash (cheap, brittle) | pHash (DCT, survives resize / recompression / blur) | ORB local features + RANSAC (survives crops and shifts)
# Thresholds are prototype settings; they are NOT calibrated on real product photos (see docs/spec_alignment.md).
SIMILAR_MAX_HAMMING = 5       # aHash: 64-bit distance
PHASH_MAX_HAMMING = 8         # pHash: 64-bit distance
ORB_MIN_INLIERS = 40          # geometrically verified ORB matches (synthetic calibration: ~0.3% false matches, 100% of 76% crops)
try:
    import numpy as np
    import cv2
except ImportError:           # OpenCV/numpy are optional: without them only the hash signals run
    np = cv2 = None
_DCT = None


def _dct_matrix(n=32):
    k, i = np.arange(n)[:, None], np.arange(n)[None, :]
    m = np.sqrt(2.0 / n) * np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    m[0] /= np.sqrt(2)
    return m


def _phash(gray):
    """64-bit DCT perceptual hash (low-frequency 8x8 block of a 32x32 DCT, thresholded at its median)."""
    global _DCT
    if np is None:
        return None
    _DCT = _dct_matrix() if _DCT is None else _DCT
    a = np.asarray(gray.resize((32, 32), Image.LANCZOS), dtype=np.float64)
    low = (_DCT @ a @ _DCT.T)[:8, :8].flatten()
    bits = low > np.median(low[1:])
    return f"{int(''.join('1' if b else '0' for b in bits), 2):016x}"


def _orb_signature(gray):
    """ORB keypoints + descriptors of a downscaled grayscale image (stored as JSON text); None if OpenCV is unavailable."""
    if cv2 is None:
        return None
    g = gray.copy()
    g.thumbnail((480, 480))
    kps, desc = cv2.ORB_create(nfeatures=400).detectAndCompute(np.asarray(g), None)
    if desc is None or len(kps) < 8:
        return None
    return {"d": base64.b64encode(desc.tobytes()).decode(), "k": [[int(k.pt[0]), int(k.pt[1])] for k in kps]}


def orb_inliers(a, b):
    """Number of RANSAC-verified ORB matches between two stored signatures (0 when none/unavailable)."""
    if cv2 is None or not a or not b:
        return 0
    da = np.frombuffer(base64.b64decode(a["d"]), dtype=np.uint8).reshape(-1, 32)
    db_ = np.frombuffer(base64.b64decode(b["d"]), dtype=np.uint8).reshape(-1, 32)
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db_, k=2)
    good = [m[0] for m in pairs if len(m) == 2 and m[0].distance < 0.75 * m[1].distance]
    if len(good) < 10:
        return 0
    pa = np.float32([a["k"][m.queryIdx] for m in good])
    pb = np.float32([b["k"][m.trainIdx] for m in good])
    _, mask = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=5.0)
    return int(mask.sum()) if mask is not None else 0


def image_signature(image_bytes):
    """Hashes / local features / metadata / quality of one image (no database access) - also used by the dataset evaluation."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    w, h = img.size
    gray = img.convert("L")
    small = gray.resize((8, 8))
    mean = sum(small.getdata()) / 64
    ahash = f"{int(''.join('1' if p > mean else '0' for p in small.getdata()), 2):016x}"
    sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
    brightness = ImageStat.Stat(gray).mean[0]
    quality = clip(0.4 * min(w * h / (640 * 480), 1) + 0.4 * min(sharp / 400, 1) + 0.2 * (1 if 35 < brightness < 220 else 0))
    return {"sha256": hashlib.sha256(image_bytes).hexdigest(), "ahash": ahash, "phash": _phash(gray), "orb": _orb_signature(gray),
            "width": w, "height": h, "sharpness": round(sharp, 1), "brightness": round(brightness, 1), "quality": round(quality, 3)}


def similarity_methods(sig, other):
    """Which signals say these two photos are the same picture? `other` holds ahash / phash / orb (JSON text or dict) of a stored photo."""
    methods = []
    if other.get("ahash") and _hamming(sig["ahash"], other["ahash"]) <= SIMILAR_MAX_HAMMING:
        methods.append("ahash")
    if sig.get("phash") and other.get("phash") and _hamming(sig["phash"], other["phash"]) <= PHASH_MAX_HAMMING:
        methods.append("phash")
    if sig.get("orb") and other.get("orb"):
        o = other["orb"] if isinstance(other["orb"], dict) else json.loads(other["orb"])
        if orb_inliers(sig["orb"], o) >= ORB_MIN_INLIERS:
            methods.append("orb")
    return methods


# ---- optional text-image consistency (CLIP-style): does the photo look like what the customer claims? ------------------------------
_IMG_TEXT = {"fn": None, "tried": False}
CLIP_PROMPTS = {"damaged": "a photo of a damaged, broken product", "defective": "a photo of a broken, faulty product",
                "wrong_item": "a photo of a product that is different from what was ordered",
                "not_as_described": "a photo of a counterfeit or misleading product"}
CLIP_BASELINE = "a photo of an intact, new, undamaged product"


def set_image_text_scorer(fn):
    """Inject fn(image_bytes, category) -> consistency in [0, 1]. None resets to the configured backend."""
    _IMG_TEXT.update(fn=fn, tried=fn is not None)


def _image_text_fn():
    if _IMG_TEXT["fn"] or _IMG_TEXT["tried"] or os.getenv("IMAGE_TEXT", "off") != "clip":
        return _IMG_TEXT["fn"]
    _IMG_TEXT["tried"] = True
    try:
        import torch
        from transformers import CLIPModel, CLIPProcessor
        name = os.getenv("CLIP_MODEL", "openai/clip-vit-base-patch32")
        model, proc = CLIPModel.from_pretrained(name), CLIPProcessor.from_pretrained(name)

        def fn(image_bytes, category):
            prompt = CLIP_PROMPTS.get(category)
            if not prompt:
                return None
            inputs = proc(text=[prompt, CLIP_BASELINE], images=Image.open(io.BytesIO(image_bytes)).convert("RGB"), return_tensors="pt", padding=True)
            with torch.no_grad():
                probs = model(**inputs).logits_per_image.softmax(dim=1)[0]
            return float(probs[0])                     # P(claimed condition) vs P(intact product)
        _IMG_TEXT["fn"] = fn
    except Exception as e:  # CLIP not installed / no network -> the check is simply not available
        print(f"[text-image consistency unavailable] {e}")
    return _IMG_TEXT["fn"]


def evidence_match(image_bytes, claim):
    """Hash / local-feature similarity / metadata. Quality in 0..1, reuse detection against stored evidence, optional text-image consistency."""
    if not image_bytes:
        return {"present": False}
    try:
        sig = image_signature(image_bytes)
    except Exception:  # corrupt / unsupported file
        return {"present": False, "invalid_file": True}
    exact, similar, methods = [], [], {}
    for e in db.q("SELECT * FROM evidence WHERE claim_id<>:c ORDER BY created_at DESC LIMIT 3000", c=claim["id"]):
        if e["sha256"] == sig["sha256"]:
            exact.append(e)
            continue
        m = similarity_methods(sig, e)
        if m:
            similar.append(e)
            methods[e["claim_id"]] = m
    other = lambda rows: [e for e in rows if e["customer_id"] != claim["customer_id"]]
    out = {"present": True, **sig, "orb": sig["orb"],
           "exact_reuse_other_customer": bool(other(exact)), "similar_reuse_other_customer": bool(other(similar)),
           "reuse_same_customer": any(e["customer_id"] == claim["customer_id"] for e in exact + similar),
           "matched_claims": [e["claim_id"] for e in exact + similar],
           "match_methods": sorted({m for v in methods.values() for m in v}), "text_image_consistency": None}
    fn = _image_text_fn()
    if fn and claim["reason"] in PHYSICAL_REASONS:
        try:
            out["text_image_consistency"] = fn(image_bytes, interpret_claim(claim)["reason_category"])
        except Exception as e:
            print(f"[text-image consistency failed] {e}")
    return out


# =========================================================================== CLAIM INTERPRETATION (NLP) + SENTIMENT
# Backends for reason classification:  "rules" (default: negation-aware regex phrases, English)  |  "transformer" (zero-shot NLI model,
# CLAIM_NLP=transformer, model CLAIM_NLP_MODEL; multilingual models work for Olist's Portuguese).  If the model cannot be loaded the rules run
# and the output says so (`nlp_backend`).  Tests/experiments can inject a classifier with set_text_classifier().
CATEGORY_LABELS = {
    "damaged": "the item arrived physically damaged or broken", "defective": "the item does not work or is faulty",
    "wrong_item": "the wrong item was delivered", "not_as_described": "the item is not as described or is counterfeit",
    "size_issue": "the size or fit is wrong", "changed_mind": "the customer changed their mind or no longer wants it",
    "not_delivered": "the order was never delivered"}
DESC_PATTERNS = {   # phrases already containing a negation ("doesn't work", "never arrived") are NOT negation-checked
    "damaged": r"\b(damag\w*|broke\w*|broken|crack\w*|shatter\w*|torn|tore|ripped|dent\w*|scratch\w*|smash\w*|crush\w*|bent|leak\w*|chipped|snapped)\b",
    "defective": r"\b(defect\w*|faulty|malfunction\w*|dead on arrival|doa|(?:does|did|do|is|was|has|have)(?:n't| not) (?:work|turn on|power|charge|start|function)\w*"
                 r"|stopped working|not working|won't (?:turn|work|charge|start)|no power|burn\w*)",
    "wrong_item": r"\b(wrong (?:item|product|size|colou?r|model)|different (?:item|product)|not what i ordered|incorrect (?:item|product)|instead of what i)\b",
    "not_as_described": r"\b(not as (?:described|shown|advertised|pictured)|looks? different|nothing like|counterfeit|fake|knock-?off|misleading|false advertis\w*"
                        r"|different from (?:the )?(?:photo|picture|listing|description))\b",
    "size_issue": r"\b(too (?:small|big|large|tight|loose|short|long)|(?:doesn't|does not|didn't|did not) fit|size (?:is|was) off)\b",
    "changed_mind": r"\b(changed my mind|(?:don't|do not) (?:like|want)|no longer (?:need|want)|decided not to|bought (?:it )?by mistake|ordered (?:it )?by mistake)\b",
    "not_delivered": r"\b(not delivered|never (?:arrived|came|received|showed)|(?:didn't|did not) (?:receive|arrive)|(?:hasn't|has not) (?:arrived|come)|never got|missing package|lost in transit)\b",
}
NEGATORS = {"not", "no", "never", "without", "nothing", "hardly", "neither", "nor", "cannot", "zero"}
VAGUE_WORDS = ["issue", "problem", "bad", "not good", "poor", "unsatisfied"]
_NLP = {"fn": None, "tried": False, "backend": "rules"}


def set_text_classifier(fn):
    """Inject a text classifier fn(text) -> {category: probability}. None resets to the configured backend."""
    _NLP.update(fn=fn, tried=fn is not None, backend="injected" if fn else "rules")


def _text_classifier():
    if _NLP["fn"] or _NLP["tried"] or os.getenv("CLAIM_NLP", "rules") != "transformer":
        return _NLP["fn"]
    _NLP["tried"] = True
    try:
        from transformers import pipeline
        pipe = pipeline("zero-shot-classification", model=os.getenv("CLAIM_NLP_MODEL", "MoritzLaurer/deberta-v3-base-zeroshot-v2.0"))
        names, labels = list(CATEGORY_LABELS), list(CATEGORY_LABELS.values())

        def fn(text):
            r = pipe(text, candidate_labels=labels, hypothesis_template="This customer message says that {}.", multi_label=True)
            return {names[labels.index(l)]: float(p) for l, p in zip(r["labels"], r["scores"])}
        _NLP.update(fn=fn, backend="transformer")
    except Exception as e:  # model not installed / no network -> fall back to the rules
        print(f"[claim NLP fallback to rules] {e}")
    return _NLP["fn"]


def _tokens(text):
    return re.findall(r"[a-z']+", text.lower())


def rule_categories(text):
    """Negation-aware phrase matching -> (affirmed categories, negated categories). 'It is not damaged' does NOT affirm 'damaged'."""
    text = text.lower()
    affirmed, negated = [], []
    for cat, pat in DESC_PATTERNS.items():
        hit_aff = hit_neg = False
        for m in re.finditer(pat, text):
            phrase = m.group(0)
            embeds_negation = bool(re.search(r"n't|\b(?:not|no|never)\b", phrase))
            before = _tokens(text[:m.start()])[-3:]
            neg = (not embeds_negation) and any(t in NEGATORS or t.endswith("n't") for t in before)
            hit_neg, hit_aff = hit_neg or neg, hit_aff or not neg
        if hit_aff:
            affirmed.append(cat)
        elif hit_neg:
            negated.append(cat)
    return affirmed, negated


def classify_text_category(text):
    """Best single reason category for free text (or None when unclear). Returns (category, backend)."""
    text = (text or "").strip()
    fn = _text_classifier()
    if fn and text:
        scores = fn(text)
        best = max(scores, key=scores.get)
        return (best if scores[best] >= 0.5 else None), _NLP["backend"]
    aff, _ = rule_categories(text)
    return (aff[0] if len(aff) == 1 else None), "rules"


# ---- sentiment: customer-experience routing + product insights ONLY. It is never an input to any risk score, cost or fraud rule. -------
_SENT = {"fn": None, "tried": False, "backend": "vader"}
_FALLBACK_LEXICON = {"angry": -.7, "furious": -.8, "terrible": -.7, "awful": -.7, "useless": -.6, "horrible": -.7, "worst": -.8, "disappointed": -.5,
                     "bad": -.4, "broken": -.3, "great": .6, "good": .5, "love": .7, "perfect": .7, "happy": .6, "thanks": .4}


def set_sentiment_scorer(fn):
    """Inject fn(text) -> score in [-1, 1]. None resets to the configured backend."""
    _SENT.update(fn=fn, tried=fn is not None, backend="injected" if fn else "vader")


def _sentiment_fn():
    if _SENT["fn"] or _SENT["tried"]:
        return _SENT["fn"]
    _SENT["tried"] = True
    model = os.getenv("SENTIMENT_MODEL")           # e.g. a multilingual transformer for Olist's Portuguese reviews
    try:
        if model:
            from transformers import pipeline
            pipe = pipeline("text-classification", model=model, top_k=None)

            def fn(text):
                out = {d["label"].lower(): d["score"] for d in pipe(text[:512])[0]}
                return out.get("positive", 0.0) - out.get("negative", 0.0)
            _SENT.update(fn=fn, backend=model)
        else:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            an = SentimentIntensityAnalyzer()
            _SENT.update(fn=lambda t: an.polarity_scores(t)["compound"], backend="vader (English only)")
    except Exception:
        _SENT.update(fn=lambda t: max(-1.0, min(1.0, sum(_FALLBACK_LEXICON.get(w, 0) for w in _tokens(t)))), backend="mini-lexicon")
    return _SENT["fn"]


def sentiment(text):
    text = (text or "").strip()
    if not text:
        return {"score": 0.0, "label": "neutral", "backend": "none"}
    score = round(max(-1.0, min(1.0, float(_sentiment_fn()(text)))), 3)
    return {"score": score, "label": "negative" if score <= -0.35 else "positive" if score >= 0.35 else "neutral", "backend": _SENT["backend"]}


def cx_priority(sent, trust, action):
    """Customer-experience routing: an upset customer with a trustworthy history gets priority care whenever we add friction or decline.
    Changes who contacts the customer, never the risk score, the cost or the decision."""
    return bool(sent["score"] <= -0.5 and trust >= 60 and action != "auto_approve")


def interpret_claim(claim):
    """Reason category, ambiguity flags and a 0-100 ClaimAmbiguity score (+ sentiment for CX routing, which does not affect the score)."""
    text = (claim.get("description") or "").strip()
    physical = claim["reason"] in PHYSICAL_REASONS
    fn = _text_classifier()
    scores = fn(text) if (fn and text) else None
    if scores is not None:
        matched, negated, backend = [c for c, p in scores.items() if p >= 0.5], [], _NLP["backend"]
    else:
        matched, negated = rule_categories(text)
        backend = "rules"
    flags, score = [], 0
    if len(text) < 5:
        score += 50
        flags.append("no_description")
    elif physical and len(text) < 15:
        score += 35
        flags.append("thin_description")
    if physical and "changed_mind" in matched:
        score += 50
        flags.append("reason_description_mismatch")
    elif claim["reason"] in negated and claim["reason"] not in matched:
        score += 50
        flags.append("claim_negated_by_text")          # e.g. claims 'damaged' but writes "it is not damaged"
    elif matched and claim["reason"] not in matched:
        score += 30
        flags.append("reason_category_conflict")
    if physical and not matched and not negated and any(w in text.lower() for w in VAGUE_WORDS):
        score += 25
        flags.append("vague_description")
    category = claim["reason"] if (not matched or claim["reason"] in matched) else matched[0]
    return {"reason_category": category, "text_categories": matched, "negated_categories": negated, "ambiguity_flags": flags,
            "ambiguity": clip100(score), "nlp_backend": backend, "sentiment": sentiment(text)}


def evidence_inconsistency(claim, ev):
    """0-100: how little the evidence supports the claim. Image quality is the deterministic proxy; when a text-image consistency scorer
    (CLIP) is enabled the photo must also look like the claimed condition:  max(100*(1-quality), 100*(1-consistency))."""
    physical = claim["reason"] in PHYSICAL_REASONS
    if not ev.get("present"):
        return 65.0 if physical else 0.0
    score = 100 * (1 - ev["quality"])
    if ev.get("text_image_consistency") is not None:
        score = max(score, 100 * (1 - ev["text_image_consistency"]))
    return clip100(score)


def evidence_reuse_score(ev):
    if not ev.get("present"):
        return 0.0
    if ev["exact_reuse_other_customer"]:
        return 100.0
    if ev["similar_reuse_other_customer"]:
        return 70.0
    return 50.0 if ev["reuse_same_customer"] else 0.0


def evidence_confidence(claim, ev):
    """0-100 confidence that the evidence can be trusted (inverse of inconsistency, collapsed by reuse). Display + guardrail input."""
    base = 100 - evidence_inconsistency(claim, ev)
    if not ev.get("present"):
        return clip100(base if claim["reason"] in PHYSICAL_REASONS else 60)
    if ev["exact_reuse_other_customer"]:
        return 2.0
    return clip100(base * (0.3 if ev["similar_reuse_other_customer"] else 1.0))


# =========================================================================== ABUSE SIGNALS (5 named patterns, deterministic)
def detect_abuse_patterns(f, claim, ev):
    """The five abuse patterns of the proposal, each an explicit deterministic rule on 30/90/180-day features."""
    found = []
    if f["refunds_30d"] >= SERIAL[30] or f["refunds_90d"] >= SERIAL[90] or f["refunds_180d"] >= SERIAL[180]:
        found.append("serial_refunder")
    if f["return_exceptions_180d"] >= RETURN_ABUSE_EXCEPTIONS:
        found.append("return_abuse")               # return missing / late / exception-based (needs return events)
    if claim["reason"] in PHYSICAL_REASONS:        # fake defect pattern: repeated defect claims with weak / reused evidence
        weak_now = (not ev.get("present")) or ev.get("quality", 1) < 0.4
        reused = (ev.get("exact_reuse_other_customer") or ev.get("similar_reuse_other_customer") or ev.get("reuse_same_customer"))
        if reused or (f["defect_claims_90d"] + 1 >= 3 and f["weak_defect_claims"] + (1 if weak_now else 0) >= 2):
            found.append("fake_defect_pattern")
    if f["near_limit_refunds"] >= FARMING_NEAR_LIMIT or (f["refunds_180d"] >= FARMING_MIN_REFUNDS and
                                                         f["refund_value_ratio_180d"] >= FARMING_VALUE_RATIO):
        found.append("refund_farming")             # many refunds just under the auto-approve limit, or an unusually high refund value share
    if f["late_window_refunds"] >= POLICY_EDGE_REFUNDS:
        found.append("policy_exploitation")        # always claiming in the last days of the return window
    return found


# =========================================================================== BEHAVIOURAL RISK + TRUST INDEX (proposal formula)
def behaviour_risk_formula(f):
    """BR = 0.35 freq + 0.25 value ratio + 0.15 repeat reason + 0.15 return exception + 0.10 evidence reuse -> (BR, components)."""
    comps = {
        "frequency": clip100(max(100 * min(1, f[f"refunds_{w}d"] / FREQ_SAT[w]) for w in (30, 90, 180))),
        "value_ratio": clip100(100 * min(1, f["refund_value_ratio_180d"] / VALUE_RATIO_SAT)),
        "repeat_reason": clip100(100 * min(1, max(0, f["top_reason_count_180d"] - 1) / REPEAT_REASON_SAT)),
        "return_exception": clip100(100 * min(1, f["return_exceptions_180d"] / RETURN_EXC_SAT)),
        "evidence_reuse": clip100(100 * min(1, f["evidence_reuse_history"] / REUSE_HISTORY_SAT)),
    }
    return clip100(sum(BEHAV_W[k] * comps[k] for k in BEHAV_W)), comps


def trust_index(behaviour_risk):
    return clip100(100 - behaviour_risk)


# =========================================================================== CLAIM RISK (proposal formula)
def claim_risk_formula(components):
    """CR = 0.30 ambiguity + 0.30 inconsistency + 0.20 evidence reuse + 0.20 abuse strength (all 0-100)."""
    return clip100(sum(CLAIM_W[k] * components[k] for k in CLAIM_W))


def abuse_strength(patterns):
    return clip100(ABUSE_PER_PATTERN * len(patterns))


# =========================================================================== AI AGENT 4: Evidence & Claim agent
def evidence_claim_agent(claim, gate, ev, image_bytes=None):
    """Claim interpretation + evidence intelligence: deterministic components, then (optional) LLM interpretation of text/image."""
    interp = interpret_claim(claim)
    physical = claim["reason"] in PHYSICAL_REASONS
    flags = list(gate["violations"]) + interp["ambiguity_flags"]
    if ev.get("invalid_file"):
        flags.append("invalid_evidence_file")
    if not ev.get("present"):
        if physical:
            flags.append("no_evidence_for_physical_claim")
    else:
        if ev["quality"] < 0.4:
            flags.append("poor_evidence_quality")
        if ev["exact_reuse_other_customer"]:
            flags.append("evidence_reused_by_other_customer")
        elif ev["similar_reuse_other_customer"]:
            flags.append("evidence_similar_to_other_customer")
        if ev["reuse_same_customer"]:
            flags.append("evidence_reused_across_own_claims")
        if ev.get("text_image_consistency") is not None and ev["text_image_consistency"] < 0.4:
            flags.append("image_text_mismatch")
    if claim["amount"] >= 10000:
        flags.append("high_value_claim")
    comps = {"ambiguity": interp["ambiguity"], "inconsistency": evidence_inconsistency(claim, ev), "evidence_reuse": evidence_reuse_score(ev)}
    hard = bool(ev.get("exact_reuse_other_customer") or ev.get("similar_reuse_other_customer"))
    rule = {"components": comps, "evidence_confidence": evidence_confidence(claim, ev), "reason_category": interp["reason_category"],
            "flags": sorted(set(flags)), "hard": hard,
            "nlp_backend": interp["nlp_backend"], "sentiment": interp["sentiment"],
            "summary": "claim/evidence assessment: " + (", ".join(sorted(set(flags))) or "no suspicious signals")}
    system = ("You are the Evidence & Claim agent of a refund-fraud system. The deterministic claim interpretation, evidence stats and "
              "component scores are given. Look at the image (if attached) and text: does the image support the stated reason, is quality "
              "adequate, anything suspicious or ambiguous? The customer's sentiment is for customer-experience routing ONLY: never treat anger or politeness as evidence of fraud or legitimacy. Return JSON {risk_adjustment: number between -10 and 10, flags:[str], summary:str}.")
    llm = llm_json(system, {"claim": claim, "gate": {k: gate[k] for k in ("eligible", "violations")}, "deterministic": rule,
                            "evidence": {k: v for k, v in ev.items() if k != "sha256"}}, image_bytes)
    out = dict(rule, llm_adjustment=llm_adjustment(llm, hard))
    if llm:
        out["flags"] = sorted(set(out["flags"] + [str(x) for x in llm.get("flags", [])]))
        out["summary"] = llm.get("summary", out["summary"])
    return out


# =========================================================================== AI AGENT 5: Behaviour & Abuse agent
def behaviour_abuse_agent(claim, f, ev=None):
    """Behavioural risk, trust index and abuse patterns: rules/formula first, then (optional) LLM interpretation of the history."""
    ev = ev or {"present": False}
    patterns = detect_abuse_patterns(f, claim, ev)
    other = []
    if f["claims_24h"] >= 3:
        other.append("claim_burst")
    if f["account_age_days"] < 14 and claim["amount"] >= 5000:
        other.append("new_account_high_value")
    blacklisted = bool(db.q1("SELECT 1 AS x FROM blacklist WHERE customer_id=:c", c=claim["customer_id"]))
    if blacklisted:
        other.append("blacklisted_customer")           # hard signal, NOT part of the BR formula
    br, comps = behaviour_risk_formula(f)
    sigs = patterns + other
    rule = {"behaviour_risk": br, "components": comps, "abuse_patterns": patterns, "signals": sorted(sigs), "blacklisted": blacklisted,
            "return_event_coverage": f["return_events_180d"],
            "summary": "behaviour assessment: " + (", ".join(sigs) or "normal customer behaviour")}
    system = ("You are the Behaviour & Abuse agent of a refund-fraud system. The deterministic behaviour features, the five abuse-pattern "
              "rules (serial_refunder, return_abuse, fake_defect_pattern, refund_farming, policy_exploitation) and the formula result are "
              "given. Interpret the history fairly: genuine customers with occasional refunds, or a product flagged by the prevention loop "
              "(real quality problem) deserve the benefit of the doubt. Return JSON {risk_adjustment: number between -10 and 10, "
              "signals:[str], summary:str}.")
    llm = llm_json(system, {"claim": claim, "features": f, "deterministic": rule})
    adj = llm_adjustment(llm)
    out = dict(rule, llm_adjustment=adj, behaviour_risk=clip100(br + adj))
    if llm:
        out["signals"] = sorted(set(out["signals"] + [str(x) for x in llm.get("signals", [])]))
        out["summary"] = llm.get("summary", out["summary"])
    out["trust_index"] = trust_index(out["behaviour_risk"])
    return out


# =========================================================================== RISK SYNTHESIS (abuse + claim risk)
def synthesize_risk(ev_out, beh_out):
    """Combine the agents' outputs into the decision state: CR, BR, Trust Index, exposure risk (0-1)."""
    comps = dict(ev_out["components"], abuse_strength=abuse_strength(beh_out["abuse_patterns"]))
    cr = clip100(claim_risk_formula(comps) + ev_out["llm_adjustment"])
    br = beh_out["behaviour_risk"]
    exposure = clip((EXPOSURE_W["claim"] * cr + EXPOSURE_W["behaviour"] * br) / 100)
    return {"claim_risk": cr, "claim_components": comps, "behaviour_risk": br, "behaviour_components": beh_out["components"],
            "trust_index": trust_index(br), "exposure_risk": exposure}


# =========================================================================== EXPECTED-COST RESOLUTION
def cost_table(amount, exposure):
    """cost(a) = RiskExposure + CustomerFriction + Handling for every candidate action (visible in the case console)."""
    out = {}
    for a in ACTIONS:
        parts = {"risk_exposure": amount * exposure * EXPOSURE_FACTOR[a],
                 "customer_friction": COST_SCALE * FRICTION_FIXED[a] + amount * (1 - exposure) * FRICTION_RATE[a],
                 "handling": COST_SCALE * HANDLING[a]}
        parts["total"] = sum(parts.values())
        out[a] = {k: round(v, 1) for k, v in parts.items()}
    return out


def eligible_actions(claim, ev, resubmission=False):
    """Which candidate actions make sense for this claim (policy eligibility is handled separately by the gate)."""
    acts = ["auto_approve", "manual_review"]
    if claim["reason"] != "not_delivered":
        acts.insert(1, "refund_after_return")          # there is an item to send back
    if claim["reason"] in PHYSICAL_REASONS and not resubmission and evidence_inconsistency(claim, ev) >= EVIDENCE_ASK_MIN:
        acts.insert(len(acts) - 1, "request_evidence")  # evidence quality/consistency is the open question; asked at most once
    return acts


def expected_cost_decision(claim, gate, risk, beh_out, ev, resubmission=False):
    """Decision state -> expected cost of each eligible action -> cheapest, then deterministic guardrails (which only add caution)."""
    A, p = float(claim["amount"]), risk["exposure_risk"]
    table = cost_table(A, p)
    eligible = eligible_actions(claim, ev, resubmission)
    action = min(eligible, key=lambda a: table[a]["total"])
    forced = []
    if beh_out["blacklisted"]:
        forced.append("blacklisted_customer")
    if ev.get("exact_reuse_other_customer"):
        forced.append("evidence_reused_across_customers")
    if forced:
        action = "manual_review"
    if not gate["eligible"]:                            # deterministic policy gate: AI can never relax it
        forced = [f"policy:{v}" for v in gate["violations"]] + forced
        action = "policy_decline"
    return {"exposure_risk": round(p, 4), "costs": {a: table[a]["total"] for a in ACTIONS}, "cost_breakdown": table,
            "eligible": eligible, "action": action, "forced": forced}


# =========================================================================== AI AGENT 6: Resolution reasoning agent
def resolution_agent(claim, ev_out, beh_out, risk, d):
    """Explains the recommended resolution; may only ESCALATE to a more cautious action, never relax one."""
    c = d["costs"]
    base = (f"{d['action']} recommended: claim risk {risk['claim_risk']:.0f}, behavioural risk {risk['behaviour_risk']:.0f} "
            f"(trust index {risk['trust_index']:.0f}); expected cost auto={c['auto_approve']}, return={c['refund_after_return']}, "
            f"evidence={c['request_evidence']}, review={c['manual_review']}.")
    if d["forced"]:
        base += f" Guardrails: {', '.join(d['forced'])}."
    if beh_out["abuse_patterns"]:
        base += f" Abuse patterns: {', '.join(beh_out['abuse_patterns'])}."
    if ev_out["flags"]:
        base += f" Claim/evidence flags: {', '.join(ev_out['flags'])}."
    system = ("You are the Resolution Reasoning agent. Given risk scores and expected cost per action, write a 2-sentence rationale for a "
              "support analyst. You may escalate to a MORE cautious action only if something looks off. "
              "Return JSON {rationale:str, escalate_to:null|'refund_after_return'|'request_evidence'|'manual_review'}.")
    llm = llm_json(system, {"claim": claim, "evidence_agent": ev_out, "behaviour_agent": beh_out, "risk": risk, "decision": d})
    d = dict(d, rationale=base)
    if llm:
        d["rationale"] = llm.get("rationale", base)
        esc = llm.get("escalate_to")
        if d["action"] in ACTIONS and esc in ACTIONS and ACTIONS.index(esc) > ACTIONS.index(d["action"]):
            d["action"], d["escalated_by_agent"] = esc, True
    return d


# =========================================================================== HUMAN-IN-THE-LOOP, RETURN EVENTS + case outcomes
def resolve_review(case_id, outcome, note=""):
    """Analyst closes a manual-review case. outcome: approve | reject | fraud (fraud also blacklists)."""
    case = db.q1("SELECT * FROM review_cases WHERE id=:i", i=case_id)
    if not case or case["status"] != "open":
        raise ValueError("case not found or already closed")
    claim = db.q1("SELECT * FROM refund_requests WHERE id=:i", i=case["claim_id"])
    if outcome == "approve":
        db.execute_refund(claim)
    else:
        db.notify_customer(claim["id"], "Your refund claim was not approved.")
    if outcome == "fraud":
        db.run("DELETE FROM blacklist WHERE customer_id=:c", c=claim["customer_id"])
        db.insert("blacklist", customer_id=claim["customer_id"], reason=f"case {case_id}: {note}", created_at=db.now())
    status = "refunded" if outcome == "approve" else "rejected"
    db.update("review_cases", case_id, status="closed", outcome=outcome, resolved_at=db.now(), note=note)
    db.update("refund_requests", claim["id"], status=status)
    db.audit(claim["id"], "review_resolved", f"{outcome} {note}")
    return {"case_id": case_id, "claim_id": claim["id"], "outcome": outcome, "claim_status": status}


def record_return(claim_id, status):
    """Return tracking for a 'refund after return' claim. received|late -> refund released; missing -> manual review."""
    claim = db.q1("SELECT * FROM refund_requests WHERE id=:i", i=claim_id)
    if not claim or claim["status"] != "awaiting_return":
        raise ValueError("claim not found or not awaiting a return")
    if status not in ("received", "late", "missing"):
        raise ValueError("status must be received | late | missing")
    db.record_return_event(claim, status)
    if status == "missing":
        db.open_review_case(claim_id)
        db.update("refund_requests", claim_id, status="under_review")
        return {"claim_id": claim_id, "return": status, "claim_status": "under_review"}
    db.execute_refund(claim)
    db.update("refund_requests", claim_id, status="refunded")
    return {"claim_id": claim_id, "return": status, "claim_status": "refunded"}


# ###########################################################################
# G5  HISTORICAL INTELLIGENCE & PREVENTION LOOP   (offline; NOT part of the real-time decision graph)
#     historical refund patterns -> product/vendor/policy insight -> intervention -> later outcome
# ###########################################################################
INTERVENTIONS = {
    "serial_refunder": "Cap auto-approvals for repeat refunders; route to review above 3 refunds/90d.",
    "return_abuse": "Add return-rate threshold per customer; require evidence for repeat returners.",
    "fake_defect_pattern": "Require clear original photos for defect claims; run reuse check on every upload.",
    "refund_farming": "Randomise auto-approve limit / sample claims just under the limit for review.",
    "policy_exploitation": "Require evidence for refunds filed in the last 20% of the return window.",
}
LATE_MIN_REFUNDS, LATE_SHARE_FLAG = 5, 0.25


NEG_REVIEW_MIN = 10   # reviews needed before an entity can be a negative-review outlier


def _rates(group_col, name_col):
    orders = db.q(f"SELECT {group_col} AS k, MAX({name_col}) AS name, COUNT(*) AS n FROM orders GROUP BY {group_col}")
    refunds = {r["k"]: r["n"] for r in db.q(f"SELECT {group_col} AS k, COUNT(*) AS n FROM refund_history GROUP BY {group_col}")}
    avoidable = {r["k"]: r for r in db.q(
        f"SELECT o.{group_col} AS k, COUNT(*) AS claims, SUM(r.amount * r.exposure_risk) AS exp_loss FROM refund_requests r "
        f"JOIN orders o ON o.id=r.order_id GROUP BY o.{group_col}")}
    # real customer-experience signals: Olist reviews linked to real orders, and the sentiment of refund-claim text (product insights only)
    reviews = {r["k"]: r for r in db.q(
        f"SELECT o.{group_col} AS k, COUNT(*) AS n, AVG(rv.rating) AS avg_rating, AVG(CASE WHEN rv.rating<=2 THEN 1.0 ELSE 0.0 END) AS neg "
        f"FROM source_reviews rv JOIN orders o ON o.id=rv.product_ref WHERE rv.source='olist' AND rv.rating IS NOT NULL GROUP BY o.{group_col}")}
    sent = {r["k"]: r["s"] for r in db.q(
        f"SELECT o.{group_col} AS k, AVG(r.sentiment) AS s FROM refund_requests r JOIN orders o ON o.id=r.order_id "
        f"WHERE r.sentiment IS NOT NULL GROUP BY o.{group_col}")}
    rows = []
    for o in orders:
        rate = refunds.get(o["k"], 0) / max(o["n"], 1)
        a, rv = avoidable.get(o["k"], {}), reviews.get(o["k"])
        rows.append({"id": o["k"], "name": o["name"], "orders": o["n"], "refunds": refunds.get(o["k"], 0),
                     "refund_rate": round(rate, 3), "new_claims": a.get("claims", 0),
                     "expected_avoidable_loss": round(a.get("exp_loss") or 0, 1),
                     "review_count": rv["n"] if rv else 0, "avg_review_score": round(rv["avg_rating"], 2) if rv else None,
                     "negative_review_rate": round(rv["neg"], 3) if rv else None,
                     "avg_sentiment": round(sent[o["k"]], 3) if sent.get(o["k"]) is not None else None})
    avg = sum(r["refund_rate"] for r in rows) / max(len(rows), 1)
    negs = [r["negative_review_rate"] for r in rows if r["review_count"] >= NEG_REVIEW_MIN]
    neg_avg = sum(negs) / len(negs) if negs else 0
    for r in rows:
        r["outlier"] = r["refund_rate"] >= 1.5 * avg and r["refunds"] >= 3
        r["review_outlier"] = bool(r["review_count"] >= NEG_REVIEW_MIN and r["negative_review_rate"] >= max(0.25, 1.5 * neg_avg))
    return sorted(rows, key=lambda r: -r["refund_rate"])


def product_intelligence():
    """Refund-rate outliers + top reasons per product."""
    rows = _rates("product_id", "product_name")
    for r in rows:
        reasons = db.q("SELECT reason, COUNT(*) AS n FROM refund_history WHERE product_id=:p GROUP BY reason ORDER BY n DESC", p=r["id"])
        r["top_reasons"] = [x["reason"] for x in reasons[:2]]
    return rows


def vendor_intelligence():
    """Vendor refund trends + concentration of avoidable loss."""
    return _rates("vendor_id", "vendor_id")


def _late_share(category):
    pol = db.q1("SELECT window_days FROM policies WHERE category=:c", c=category)
    rows = db.q("SELECT h.created_at, o.delivered_at FROM refund_history h JOIN orders o ON o.id=h.order_id WHERE h.category=:c", c=category)
    if not rows or not pol:
        return 0.0, 0
    late = sum(1 for r in rows if (parse(r["created_at"]) - parse(r["delivered_at"])).total_seconds() / 86400 >= 0.8 * pol["window_days"])
    return late / len(rows), len(rows)


def abuse_pattern_counts():
    """Claims per named abuse pattern (all five always present)."""
    got = {r["detail"]: r["n"] for r in db.q(
        "SELECT detail, COUNT(DISTINCT claim_id) AS n FROM risk_signals WHERE kind='abuse' GROUP BY detail")}
    return {p: got.get(p, 0) for p in ABUSE_PATTERNS}


def policy_insights():
    """Policy friction / abuse patterns needing intervention, as readable recommendations."""
    out = []
    for c in db.q("SELECT category, window_days FROM policies"):
        share, n = _late_share(c["category"])
        if n >= LATE_MIN_REFUNDS and share >= LATE_SHARE_FLAG:
            out.append(f"{c['category']}: {share:.0%} of {n} refunds filed in the last 20% of the {c['window_days']}-day window "
                       f"- consider a shorter window or evidence required late in the window.")
    for pat, n in abuse_pattern_counts().items():
        if n:
            out.append(f"abuse pattern '{pat}' seen on {n} claim(s) - {INTERVENTIONS[pat]}")
    forced = db.q1("SELECT COUNT(*) AS n FROM refund_requests WHERE flags LIKE :p", p="%policy:%")["n"]
    if forced:
        out.append(f"{forced} claim(s) breached policy rules and were routed to review - check if rules are unclear to customers.")
    return out


def operations_view():
    """Which claims need intervention now?"""
    return db.q("SELECT r.id AS claim_id, r.customer_id, r.amount, r.exposure_risk, r.decision, r.status, r.rationale, r.sentiment, r.cx_priority, "
                "c.id AS case_id FROM refund_requests r LEFT JOIN review_cases c ON c.claim_id=r.id "
                "WHERE r.status IN ('under_review','awaiting_evidence','awaiting_return') ORDER BY r.exposure_risk * r.amount DESC")


def management_view():
    """Where is refund cost / abuse concentrated?"""
    by_decision = db.q("SELECT decision, COUNT(*) AS claims, SUM(amount) AS value FROM refund_requests GROUP BY decision")
    total = sum(d["claims"] for d in by_decision) or 1
    pick = lambda name: next((d for d in by_decision if d["decision"] == name), {"claims": 0})
    avoided = db.q1("SELECT SUM(amount * exposure_risk) AS v FROM refund_requests WHERE decision<>'auto_approve'")["v"] or 0
    leakage = db.q1("SELECT SUM(amount * exposure_risk) AS v FROM refund_requests WHERE decision='auto_approve'")["v"] or 0
    by_cat = db.q("SELECT o.category, COUNT(*) AS claims, SUM(r.amount) AS value, AVG(r.exposure_risk) AS avg_exposure_risk "
                  "FROM refund_requests r JOIN orders o ON o.id=r.order_id GROUP BY o.category ORDER BY value DESC")
    prof = db.q("SELECT behavior_risk, trust_index FROM customer_risk_profile")
    high = [p for p in prof if p["behavior_risk"] >= 60]
    customer_risk = {"profiled_customers": len(prof), "high_behavioural_risk_customers(>=60)": len(high),
                     "avg_trust_index": round(sum(p["trust_index"] for p in prof) / len(prof), 1) if prof else None}
    cx = db.q1("SELECT SUM(cx_priority) AS n, AVG(sentiment) AS s FROM refund_requests")
    customer_experience = {"cx_priority_claims": int(cx["n"] or 0), "avg_claim_sentiment": round(cx["s"], 3) if cx["s"] is not None else None}
    return {"customer_risk": customer_risk, "customer_experience": customer_experience, "claims": total, "auto_resolution_rate": round(pick("auto_approve")["claims"] / total, 2),
            "manual_review_rate": round(pick("manual_review")["claims"] / total, 2),
            "expected_loss_avoided": round(avoided, 1), "expected_leakage_auto_approved": round(leakage, 1),
            "by_decision": by_decision, "by_category": by_cat, "abuse_patterns": abuse_pattern_counts()}


# ---- persistent snapshots + insight / intervention log --------------------------------------------
def _add_insight(kind, entity_id, name, pattern, insight, intervention, metric, baseline):
    if db.q1("SELECT id FROM insight_log WHERE kind=:k AND entity_id=:e AND pattern=:p AND status<>'evaluated'",
             k=kind, e=entity_id, p=pattern):
        return None  # an open insight for this pattern already exists
    iid = db.uid("INS")
    db.insert("insight_log", id=iid, created_at=db.now(), kind=kind, entity_id=entity_id, entity_name=name, pattern=pattern,
              insight=insight, intervention=intervention, metric_name=metric, baseline_value=round(baseline, 4), status="proposed",
              applied_at=None, applied_note=None, current_value=None, outcome=None, evaluated_at=None)
    return iid


def run_historical_loop():
    """Pattern -> insight: persist Product/Vendor Risk Snapshots and propose interventions in the Insight Log."""
    ts, new = db.now(), []
    prods, vends = product_intelligence(), vendor_intelligence()
    for p in prods:
        db.insert("product_risk_snapshot", id=db.uid("PS"), snapshot_at=ts, product_id=p["id"], product_name=p["name"],
                  vendor_id=(db.q1("SELECT vendor_id FROM orders WHERE product_id=:p LIMIT 1", p=p["id"]) or {}).get("vendor_id"),
                  orders=p["orders"], refunds=p["refunds"], refund_rate=p["refund_rate"], top_reasons=json.dumps(p["top_reasons"]),
                  risk_score=clip(p["refund_rate"] / 0.5), outlier=int(p["outlier"]), avg_sentiment=p["avg_sentiment"],
                  avg_review_score=p["avg_review_score"], negative_review_rate=p["negative_review_rate"], review_count=p["review_count"])
    for v in vends:
        db.insert("vendor_risk_snapshot", id=db.uid("VS"), snapshot_at=ts, vendor_id=v["id"], orders=v["orders"], refunds=v["refunds"],
                  refund_rate=v["refund_rate"], expected_avoidable_loss=v["expected_avoidable_loss"],
                  risk_score=clip(v["refund_rate"] / 0.5), outlier=int(v["outlier"]), avg_review_score=v["avg_review_score"],
                  negative_review_rate=v["negative_review_rate"], review_count=v["review_count"])
    for p in prods:
        if p["outlier"]:
            reasons = p["top_reasons"]
            fix = ("Quality inspection + packaging review; request vendor corrective action." if set(reasons) & {"defective", "damaged"}
                   else "Fix product listing / description accuracy." if "not_as_described" in reasons
                   else "Review product page and return reasons.")
            tone = f" Claim text is on average {'very negative' if p['avg_sentiment'] <= -0.5 else 'negative' if p['avg_sentiment'] <= -0.2 else 'neutral'} " \
                   f"(sentiment {p['avg_sentiment']})." if p["avg_sentiment"] is not None else ""
            new.append(_add_insight("product", p["id"], p["name"], "refund_rate_outlier",
                                    f"{p['name']} refund rate {p['refund_rate']:.0%} is an outlier (top reasons: {', '.join(reasons)}).{tone}",
                                    fix, "refund_rate", p["refund_rate"]))
    for p in prods:
        if p["review_outlier"]:
            new.append(_add_insight("product", p["id"], p["name"], "negative_review_outlier",
                                    f"{p['negative_review_rate']:.0%} of {p['review_count']} linked reviews for {p['name']} are 1-2 stars (average {p['avg_review_score']}).",
                                    "Read the complaint texts; check listing accuracy, delivery and quality for this product.", "negative_review_rate",
                                    p["negative_review_rate"]))
    for v in vends:
        if v["review_outlier"]:
            new.append(_add_insight("vendor", v["id"], v["id"], "negative_review_outlier",
                                    f"{v['negative_review_rate']:.0%} of {v['review_count']} linked reviews for vendor {v['id']} are 1-2 stars (average {v['avg_review_score']}).",
                                    "Vendor service/quality review; compare with category baseline.", "negative_review_rate", v["negative_review_rate"]))
    for v in vends:
        if v["outlier"]:
            new.append(_add_insight("vendor", v["id"], v["id"], "refund_rate_outlier",
                                    f"Vendor {v['id']} refund rate {v['refund_rate']:.0%}; expected avoidable loss {v['expected_avoidable_loss']}.",
                                    "Vendor corrective-action review; tighten evidence rules for this vendor.", "refund_rate", v["refund_rate"]))
    for c in db.q("SELECT category, window_days FROM policies"):
        share, n = _late_share(c["category"])
        if n >= LATE_MIN_REFUNDS and share >= LATE_SHARE_FLAG:
            new.append(_add_insight("policy", c["category"], c["category"], "late_window_refunds",
                                    f"{share:.0%} of {n} {c['category']} refunds are filed in the last 20% of the {c['window_days']}-day window.",
                                    INTERVENTIONS["policy_exploitation"], "late_window_share", share))
    for pat, n in abuse_pattern_counts().items():
        if n:
            new.append(_add_insight("abuse", pat, pat, "abuse_pattern_seen", f"Abuse pattern '{pat}' detected on {n} claim(s).",
                                    INTERVENTIONS[pat], "claims_with_pattern", n))
    return {"snapshot_at": ts, "products": len(prods), "vendors": len(vends), "new_insights": [i for i in new if i]}


def latest_snapshots(kind):
    table, key = {"products": ("product_risk_snapshot", "product_id"), "vendors": ("vendor_risk_snapshot", "vendor_id")}[kind]
    last = db.q1(f"SELECT MAX(snapshot_at) AS t FROM {table}")["t"]
    return db.q(f"SELECT * FROM {table} WHERE snapshot_at=:t ORDER BY risk_score DESC", t=last) if last else []


def list_insights(status=None):
    if status:
        return db.q("SELECT * FROM insight_log WHERE status=:s ORDER BY created_at DESC", s=status)
    return db.q("SELECT * FROM insight_log ORDER BY created_at DESC")


def apply_intervention(insight_id, note=""):
    """Insight -> intervention: the business confirms the intervention was put in place."""
    row = db.q1("SELECT * FROM insight_log WHERE id=:i", i=insight_id)
    if not row or row["status"] != "proposed":
        raise ValueError("insight not found or not in 'proposed' state")
    db.update("insight_log", insight_id, status="applied", applied_at=db.now(), applied_note=note)
    return db.q1("SELECT * FROM insight_log WHERE id=:i", i=insight_id)


def _current_metric(row):
    kind, ent = row["kind"], row["entity_id"]
    if row["metric_name"] == "negative_review_rate":
        col = "product_id" if kind == "product" else "vendor_id"
        r = db.q1(f"SELECT AVG(CASE WHEN rv.rating<=2 THEN 1.0 ELSE 0.0 END) AS neg FROM source_reviews rv JOIN orders o ON o.id=rv.product_ref "
                  f"WHERE rv.source='olist' AND rv.rating IS NOT NULL AND o.{col}=:v", v=ent)
        return r["neg"] or 0.0
    if kind in ("product", "vendor"):
        col = "product_id" if kind == "product" else "vendor_id"
        o = db.q1(f"SELECT COUNT(*) AS n FROM orders WHERE {col}=:v", v=ent)["n"]
        r = db.q1(f"SELECT COUNT(*) AS n FROM refund_history WHERE {col}=:v", v=ent)["n"]
        return r / max(o, 1)
    if kind == "policy":
        return _late_share(ent)[0]
    return abuse_pattern_counts().get(ent, 0)


def evaluate_outcome(insight_id):
    """Intervention -> later outcome: compare the metric now with the baseline recorded when the insight was raised."""
    row = db.q1("SELECT * FROM insight_log WHERE id=:i", i=insight_id)
    if not row or row["status"] != "applied":
        raise ValueError("insight not found or intervention not applied yet")
    cur, base = _current_metric(row), row["baseline_value"] or 0
    outcome = "no_change" if base == 0 else "improved" if cur <= 0.8 * base else "worse" if cur >= 1.1 * base else "no_change"
    db.update("insight_log", insight_id, status="evaluated", current_value=round(cur, 4), outcome=outcome, evaluated_at=db.now())
    return db.q1("SELECT * FROM insight_log WHERE id=:i", i=insight_id)
