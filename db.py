"""
db.py  -  SQL DATA STORE (SQLite default for the MVP, PostgreSQL as the extension) + STUBS of the EXISTING refund infrastructure.

Architecture slide 1/3:
  * "EXISTING REFUND INFRASTRUCTURE"  -> get_order / get_policy / execute_refund / open_review_case /
                                         request_more_evidence / notify_customer / audit   (stubs, NOT rebuilt)
  * "SQL DATA STORE (SQLite for MVP / PostgreSQL extension)" -> the tables below (incl. Product/Vendor Risk Snapshots,
                                         Insight Log, and data_provenance / eval_labels for the dataset evaluation)
Set DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/refunds to use PostgreSQL.
"""
import os
import json
import uuid
import random
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL", "sqlite:///refund_intel.db")
engine = create_engine(DB_URL, future=True)


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def ago(days=0, hours=0):
    return (datetime.utcnow() - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (id TEXT PRIMARY KEY, name TEXT, email TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT, product_name TEXT,
    category TEXT, vendor_id TEXT, amount REAL, status TEXT, order_date TEXT, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS policies (category TEXT PRIMARY KEY, window_days INTEGER, refundable INTEGER, max_auto_amount REAL);
CREATE TABLE IF NOT EXISTS refund_history (id TEXT PRIMARY KEY, order_id TEXT, customer_id TEXT, product_id TEXT,
    vendor_id TEXT, category TEXT, reason TEXT, amount REAL, status TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS refund_requests (id TEXT PRIMARY KEY, order_id TEXT, customer_id TEXT, reason TEXT, amount REAL,
    description TEXT, created_at TEXT, claim_risk REAL, behaviour_risk REAL, trust_index REAL, evidence_confidence REAL, exposure_risk REAL,
    decision TEXT, costs TEXT, flags TEXT, rationale TEXT, status TEXT, components TEXT);
CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, claim_id TEXT, customer_id TEXT, path TEXT, sha256 TEXT,
    ahash TEXT, width INTEGER, height INTEGER, quality REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS behaviour_features (customer_id TEXT PRIMARY KEY, orders_total INTEGER, refunds_total INTEGER,
    refunds_30d INTEGER, refunds_90d INTEGER, refunds_180d INTEGER, refund_value_90d REAL, refund_value_180d REAL,
    refund_value_ratio_180d REAL, return_events_180d INTEGER, return_exceptions_180d INTEGER, refund_rate REAL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS customer_risk_profile (customer_id TEXT PRIMARY KEY, behavior_risk REAL, trust_index REAL,
    components TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS return_events (id TEXT PRIMARY KEY, customer_id TEXT, claim_id TEXT, refund_id TEXT, status TEXT, event_at TEXT);
CREATE TABLE IF NOT EXISTS risk_signals (id TEXT PRIMARY KEY, claim_id TEXT, customer_id TEXT, kind TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS blacklist (customer_id TEXT PRIMARY KEY, reason TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS review_cases (id TEXT PRIMARY KEY, claim_id TEXT, status TEXT, opened_at TEXT,
    outcome TEXT, resolved_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS action_log (id TEXT PRIMARY KEY, claim_id TEXT, action TEXT, detail TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS data_provenance (id TEXT PRIMARY KEY, table_name TEXT, row_id TEXT, source TEXT, source_file TEXT,
    source_id TEXT, label_origin TEXT);
CREATE TABLE IF NOT EXISTS eval_labels (id TEXT PRIMARY KEY, scenario_id TEXT, scenario TEXT, entity_type TEXT, entity_id TEXT,
    label TEXT, label_origin TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS source_reviews (id TEXT PRIMARY KEY, source TEXT, source_id TEXT, product_ref TEXT, rating REAL,
    title TEXT, review_text TEXT, n_images INTEGER, image_urls TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS product_risk_snapshot (id TEXT PRIMARY KEY, snapshot_at TEXT, product_id TEXT, product_name TEXT,
    vendor_id TEXT, orders INTEGER, refunds INTEGER, refund_rate REAL, top_reasons TEXT, risk_score REAL, outlier INTEGER);
CREATE TABLE IF NOT EXISTS vendor_risk_snapshot (id TEXT PRIMARY KEY, snapshot_at TEXT, vendor_id TEXT, orders INTEGER,
    refunds INTEGER, refund_rate REAL, expected_avoidable_loss REAL, risk_score REAL, outlier INTEGER);
CREATE TABLE IF NOT EXISTS insight_log (id TEXT PRIMARY KEY, created_at TEXT, kind TEXT, entity_id TEXT, entity_name TEXT,
    pattern TEXT, insight TEXT, intervention TEXT, metric_name TEXT, baseline_value REAL, status TEXT, applied_at TEXT,
    applied_note TEXT, current_value REAL, outcome TEXT, evaluated_at TEXT);
"""


def init():
    with engine.begin() as c:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                c.execute(text(stmt))


# --------------------------------------------------------------------------- tiny SQL helpers
def q(sql, **p):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), p)]


def q1(sql, **p):
    rows = q(sql, **p)
    return rows[0] if rows else None


def run(sql, **p):
    with engine.begin() as c:
        c.execute(text(sql), p)


def insert(table, **row):
    cols = ", ".join(row)
    vals = ", ".join(f":{k}" for k in row)
    run(f"INSERT INTO {table} ({cols}) VALUES ({vals})", **row)


def bulk_insert(table, rows):
    """Fast multi-row insert for dataset ingestion (rows: list of dicts with identical keys)."""
    if not rows:
        return 0
    cols = list(rows[0])
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"
    with engine.begin() as c:
        for i in range(0, len(rows), 5000):
            c.execute(text(sql), rows[i:i + 5000])
    return len(rows)


def update(table, key, **row):
    sets = ", ".join(f"{k}=:{k}" for k in row)
    run(f"UPDATE {table} SET {sets} WHERE id=:pk", pk=key, **row)


# --------------------------------------------------------------------------- EXISTING SYSTEMS (stubs)
def get_order(order_id):
    return q1("SELECT * FROM orders WHERE id=:i", i=order_id)


def get_policy(category):
    return q1("SELECT * FROM policies WHERE category=:c", c=category)


def audit(claim_id, action, detail=""):
    insert("action_log", id=uid("LOG"), claim_id=claim_id, action=action, detail=detail, created_at=now())


def notify_customer(claim_id, message):
    audit(claim_id, "notify_customer", message)  # stub: real system sends email/SMS/push


def execute_refund(claim):
    """Existing refund API (stub): money moves, refund history updated."""
    o = get_order(claim["order_id"])
    insert("refund_history", id=uid("RF"), order_id=o["id"], customer_id=o["customer_id"], product_id=o["product_id"],
           vendor_id=o["vendor_id"], category=o["category"], reason=claim["reason"], amount=claim["amount"],
           status="refunded", created_at=now())
    audit(claim["id"], "refund_executed", f"amount={claim['amount']}")
    notify_customer(claim["id"], "Your refund has been approved.")


def decline_refund(claim, reason=""):
    """Existing policy-decline path (stub): deterministic policy says the request is ineligible."""
    audit(claim["id"], "policy_decline", reason)
    notify_customer(claim["id"], f"Your refund request is not eligible under the refund policy ({reason}).")


def request_return(claim_id):
    """Existing return flow (stub): refund is released after the item comes back."""
    audit(claim_id, "return_requested", "refund released after the return is received")
    notify_customer(claim_id, "Please return the item; your refund will be issued once it is received.")


def record_return_event(claim, status):
    """Existing return tracking (stub). status: received | late | missing. Feeds the return-event compliance feature."""
    insert("return_events", id=uid("RET"), customer_id=claim["customer_id"], claim_id=claim["id"], refund_id=None,
           status=status, event_at=now())
    audit(claim["id"], "return_event", status)


def open_review_case(claim_id):
    """Existing case-management API (stub)."""
    cid = uid("CASE")
    insert("review_cases", id=cid, claim_id=claim_id, status="open", opened_at=now(), outcome=None,
           resolved_at=None, note=None)
    audit(claim_id, "review_case_opened", cid)
    notify_customer(claim_id, "Your claim is under review by our team.")
    return cid


def request_more_evidence(claim_id):
    audit(claim_id, "evidence_requested", "ask customer for clearer photos")
    notify_customer(claim_id, "Please upload clearer photos of the item to continue.")


# --------------------------------------------------------------------------- demo seed data
def seed():
    """Fake 'existing' data: customers, orders, policies, refund history. Idempotent."""
    if q1("SELECT id FROM customers LIMIT 1"):
        return
    rnd = random.Random(7)
    for cat, window, auto_limit in [("electronics", 10, 5000), ("fashion", 15, 3000), ("home", 7, 3000),
                                    ("grocery", 2, 800), ("beauty", 7, 1500)]:
        insert("policies", category=cat, window_days=window, refundable=1, max_auto_amount=auto_limit)

    products = {  # id: (name, category, vendor, price)
        "P-PHONE": ("Nova X Smartphone", "electronics", "V-TECHNO", 18000),
        "P-EARBUD": ("BassPods Earbuds", "electronics", "V-SOUNDX", 2400),
        "P-KURTA": ("Cotton Kurta", "fashion", "V-ETHNIC", 1800),
        "P-SNEAK": ("RunFast Sneakers", "fashion", "V-STRIDE", 2800),
        "P-MIXER": ("Mixer Grinder", "home", "V-HOMEO", 3500),
        "P-SERUM": ("Glow Serum", "beauty", "V-GLOW", 900),
        "P-RICE": ("Basmati Rice 5kg", "grocery", "V-FARM", 700),
    }
    people = {"C001": "Asha (good customer)", "C002": "Rohan (occasional)", "C003": "Vikram (serial refunder)",
              "C004": "Neha (new account)", "C005": "Imran", "C006": "Priya", "C007": "Karan", "C008": "Meera"}
    for cid, name in people.items():
        age = 5 if cid == "C004" else rnd.randint(200, 700)
        insert("customers", id=cid, name=name, email=f"{cid.lower()}@example.com", created_at=ago(days=age))

    reasons = ["damaged", "defective", "wrong_item", "not_as_described", "changed_mind", "size_issue"]
    ret_rnd = random.Random(11)
    n = 0

    def make_order(cust, pid, days_ago, price=None):
        nonlocal n
        n += 1
        name, cat, vendor, base = products[pid]
        oid = f"ORD-{n:04d}"
        delivered = ago(days=days_ago)
        insert("orders", id=oid, customer_id=cust, product_id=pid, product_name=name, category=cat, vendor_id=vendor,
               amount=price or base, status="delivered", order_date=ago(days=days_ago + 3), delivered_at=delivered)
        return oid, cat, vendor, price or base

    def make_refund(cust, pid, oid, cat, vendor, amount, created_days_ago, reason=None, ret=None):
        rid = uid("RF")
        if cat != "grocery":  # physical items come back: log the return event (existing return tracking)
            insert("return_events", id=uid("RET"), customer_id=cust, claim_id=None, refund_id=rid,
                   status=ret or ("received" if ret_rnd.random() < 0.92 else "late"), event_at=ago(days=created_days_ago))
        insert("refund_history", id=rid, order_id=oid, customer_id=cust, product_id=pid, vendor_id=vendor,
               category=cat, reason=reason or rnd.choice(reasons), amount=amount, status="refunded",
               created_at=ago(days=created_days_ago))

    # normal customers; BassPods earbuds are a deliberate refund-rate outlier (product/vendor intelligence demo)
    for cust in ["C001", "C002", "C004", "C005", "C006", "C007", "C008"]:
        if cust == "C004":
            continue  # new account: no history
        for _ in range(rnd.randint(8, 11)):
            pid = rnd.choice(list(products))
            if pid == "P-PHONE":
                pid = "P-EARBUD"
            if cust == "C001" and pid == "P-EARBUD":
                pid = "P-KURTA"  # the 'good customer' never buys the outlier product
            days = rnd.randint(20, 250)
            oid, cat, vendor, amt = make_order(cust, pid, days)
            p_ref = 0.45 if pid == "P-EARBUD" else (0.0 if cust == "C001" else 0.06)
            if rnd.random() < p_ref:
                reason = "defective" if pid == "P-EARBUD" else None
                make_refund(cust, pid, oid, cat, vendor, amt, days - 2, reason)

    # serial refunder C003: 12 orders, 7 refunds in ~60 days, filed late in the window, just under auto limit
    for i in range(12):
        price = rnd.randint(2700, 2990)
        days = 5 + i * 6
        oid, cat, vendor, amt = make_order("C003", "P-SNEAK", days + 14, price)
        if i < 7:  # serial refunder: returns often missing or late
            make_refund("C003", "P-SNEAK", oid, cat, vendor, amt, days,
                        ret=["missing", "late", "missing", "received", "late", "received", "received"][i])

    # fresh delivered orders used by demo.py / API tryouts
    demo = [("C001", "P-MIXER", 2, None), ("C003", "P-SNEAK", 3, 2890), ("C004", "P-PHONE", 1, None),
            ("C005", "P-EARBUD", 2, None), ("C002", "P-KURTA", 4, None), ("C002", "P-RICE", 6, None)]
    for i, (cust, pid, d, price) in enumerate(demo, start=1):
        name, cat, vendor, base = products[pid]
        insert("orders", id=f"ORD-{1000 + i}", customer_id=cust, product_id=pid, product_name=name, category=cat,
               vendor_id=vendor, amount=price or base, status="delivered", order_date=ago(days=d + 3),
               delivered_at=ago(days=d))
