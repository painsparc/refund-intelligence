"""Full customer/ops/management journey through the real HTTP API (FastAPI TestClient, rule-based agents)."""
import os
import re
import sys
import pathlib
import tempfile

TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TMP}/test.db")
os.environ.setdefault("EVIDENCE_DIR", f"{TMP}/evidence")
os.environ.pop("LLM_API_KEY", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import db  # noqa: E402
from app import app  # noqa: E402
from demo import make_image, resized_copy  # noqa: E402

TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA)


@pytest.fixture()
def client():
    db.init()
    for t in TABLES:
        db.run(f"DELETE FROM {t}")
    with TestClient(app) as c:       # lifespan seeds the demo data
        yield c


def post(c, order, cust, reason, amount, desc, img=None):
    files = {"image": ("e.jpg", img, "image/jpeg")} if img else None
    r = c.post("/claims", data=dict(order_id=order, customer_id=cust, reason=reason, amount=amount, description=desc), files=files)
    assert r.status_code == 200, r.text
    return r.json()


def test_full_journey(client):
    serial_img = make_image(2)
    normal = post(client, "ORD-1001", "C001", "damaged", 3500, "The jar arrived cracked at the base.", make_image(1))
    assert normal["decision"] == "auto_approve" and client.get(f"/claims/{normal['claim_id']}").json()["claim"]["status"] == "refunded"

    ask = post(client, "ORD-1005", "C002", "damaged", 1800, "Stitching came apart on the sleeve.")
    assert ask["decision"] == "request_evidence"
    more = client.post(f"/claims/{ask['claim_id']}/evidence", files={"image": ("m.jpg", make_image(55), "image/jpeg")})
    assert more.status_code == 200 and more.json()["decision"] == "refund_after_return" and more.json()["status"] == "awaiting_return"
    assert client.post(f"/claims/{ask['claim_id']}/return", json={"status": "teleported"}).status_code == 409
    back = client.post(f"/claims/{ask['claim_id']}/return", json={"status": "received"})
    assert back.status_code == 200 and back.json()["claim_status"] == "refunded"
    assert client.post(f"/claims/{ask['claim_id']}/evidence", files={"image": ("m.jpg", make_image(56), "image/jpeg")}).status_code == 409

    violation = post(client, "ORD-1006", "C002", "not_as_described", 700, "Rice was not what the listing showed.", make_image(6))
    assert violation["decision"] == "policy_decline" and any(f.startswith("policy:") for f in violation["flags"])
    assert client.get(f"/claims/{violation['claim_id']}").json()["claim"]["status"] == "declined"

    serial = post(client, "ORD-1002", "C003", "defective", 2890, "Sole came off after two days.", serial_img)
    assert serial["decision"] == "manual_review" and "serial_refunder" in serial["abuse_patterns"]

    reused = post(client, "ORD-1004", "C005", "damaged", 2400, "Earbuds case is broken.", resized_copy(serial_img))
    assert reused["decision"] == "manual_review" and "fake_defect_pattern" in reused["abuse_patterns"]
    assert any(f.startswith("evidence_") for f in reused["flags"])

    queue = client.get("/reviews").json()
    assert {q["claim_id"] for q in queue} >= {serial["claim_id"], reused["claim_id"]}
    assert violation["claim_id"] not in {q["claim_id"] for q in queue}            # a policy decline needs no human
    done = client.post(f"/reviews/{serial['review_case_id']}/resolve", json={"outcome": "fraud", "note": "serial"}).json()
    assert done["claim_status"] == "rejected"
    detail = client.get(f"/claims/{serial['claim_id']}").json()
    assert detail["claim"]["components"]["behaviour_risk"] and detail["review_case"]["outcome"] == "fraud"
    assert 0 <= detail["claim"]["trust_index"] <= 100 and detail["claim"]["components"]["cost_breakdown"]["manual_review"]["handling"] == 100
    assert client.get(f"/claims/{reused['claim_id']}/evidence-image").status_code == 200

    run = client.post("/historical/run").json()
    assert run["new_insights"]
    insights = client.get("/historical/insights", params={"status": "proposed"}).json()
    product = next(i for i in insights if i["kind"] == "product")
    assert client.post(f"/historical/insights/{product['id']}/apply", json={"note": "QC audit"}).json()["status"] == "applied"
    assert client.post(f"/historical/insights/{product['id']}/evaluate").json()["status"] == "evaluated"
    mgmt = client.get("/insights/management").json()
    assert mgmt["claims"] == 5 and mgmt["abuse_patterns"]["serial_refunder"] >= 1
    for path in ("operations", "products", "vendors", "policy", "abuse"):
        assert client.get(f"/insights/{path}").status_code == 200
