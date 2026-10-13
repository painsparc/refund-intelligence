"""Tests for the dataset layer (loaders, schema mappings, label provenance, data-quality checks).
They run on tiny FIXTURE files that follow the official file/column layouts - not on the real datasets."""
import gzip
import io
import json
import os
import re
import sys
import pathlib
import tempfile
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp()
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TMP}/test.db")
os.environ.setdefault("EVIDENCE_DIR", f"{TMP}/evidence")
os.environ["EVAL_REPORTS"], os.environ["EVAL_DOCS"] = f"{TMP}/reports", f"{TMP}/docs"
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402
import db  # noqa: E402
import intelligence as ai  # noqa: E402
import eval_data as ed  # noqa: E402

TABLES = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", db.SCHEMA)


@pytest.fixture(autouse=True)
def clean():
    db.init()
    for t in TABLES:
        db.run(f"DELETE FROM {t}")
    if os.path.exists(ed.LOG_PATH()):
        os.remove(ed.LOG_PATH())


# ----------------------------------------------------------------------------- fixture builders
def make_olist(folder):
    os.makedirs(folder, exist_ok=True)
    base = datetime(2017, 5, 1, 9, 0, 0)
    orders, items, customers = [], [], []
    for i in range(40):
        oid, cid = f"o{i:03d}", f"c{i:03d}"
        ts = (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M:%S")
        delivered = (base + timedelta(days=i + 5)).strftime("%Y-%m-%d %H:%M:%S")
        status = "delivered"
        if i in (3, 4):
            status, delivered = "canceled", None
        if i == 5:
            status, delivered = "shipped", None
        if i == 6:
            ts = None                                   # missing purchase timestamp -> dropped
        orders.append(dict(order_id=oid, customer_id=cid, order_status=status, order_purchase_timestamp=ts, order_approved_at=ts,
                           order_delivered_carrier_date=None, order_delivered_customer_date=delivered, order_estimated_delivery_date=delivered))
        customers.append(dict(customer_id=cid, customer_unique_id=f"u{i % 30:03d}", customer_zip_code_prefix=1000, customer_city="x", customer_state="SP"))
        if i != 7:                                      # order without items -> dropped
            for j in (1, 2) if i % 5 == 0 else (1,):
                items.append(dict(order_id=oid, order_item_id=j, product_id=f"p{i % 6}", seller_id=f"s{i % 3}", shipping_limit_date=ts,
                                  price=50.0 + i, freight_value=10.0))
    pd.DataFrame(orders).to_csv(f"{folder}/olist_orders_dataset.csv", index=False)
    pd.DataFrame(items).to_csv(f"{folder}/olist_order_items_dataset.csv", index=False)
    pd.DataFrame(customers).to_csv(f"{folder}/olist_customers_dataset.csv", index=False)
    pd.DataFrame([dict(product_id=f"p{k}", product_category_name=(None if k == 5 else f"cat_{k}")) for k in range(6)]).to_csv(
        f"{folder}/olist_products_dataset.csv", index=False)
    pd.DataFrame([dict(product_category_name=f"cat_{k}", product_category_name_english=f"category_{k}") for k in range(5)]).to_csv(
        f"{folder}/product_category_name_translation.csv", index=False)
    return folder


def make_uci(path, xlsx=False):
    rows = []

    def line(inv, stock, qty, price, cust, day, desc="WIDGET"):
        rows.append({"Invoice": inv, "StockCode": stock, "Description": desc, "Quantity": qty,
                     "InvoiceDate": datetime(2010, 1, 1) + timedelta(days=day), "Price": price, "Customer ID": cust, "Country": "United Kingdom"})
    n = 1000
    for cust in (11111.0, 22222.0, 33333.0):                 # 3 customers with normal sales
        for k in range(6):
            n += 1
            line(str(n), "85123A", 5, 2.5, cust, k * 20)
    for k in range(6):                                          # customer 44444: many cancellation invoices within 90 days
        n += 1
        line(str(n), "84406B", 3, 4.0, 44444.0, k * 10)
        line(f"C{n}", "84406B", -3, 4.0, 44444.0, k * 10 + 2)
    line("5001", "85123A", 2, 3.0, None, 5)                      # no customer id -> dropped
    line("A5002", "B", 1, 100.0, 11111.0, 5)                     # adjust bad debt -> dropped
    line("5003", "POST", 1, 18.0, 11111.0, 6)                    # non-product code -> dropped
    line("5004", "85123A", -4, 3.0, 22222.0, 7)                  # negative qty without C -> dropped
    df = pd.DataFrame(rows)
    if xlsx:
        with pd.ExcelWriter(path) as w:
            df.iloc[:20].to_excel(w, sheet_name="Year 2009-2010", index=False)
            df.iloc[20:].to_excel(w, sheet_name="Year 2010-2011", index=False)
    else:
        df.to_csv(path, index=False)
    return path


def make_amazon(path):
    recs = [{"rating": 1.0, "title": "bad", "text": "Arrived broken and does not work", "images": [{"large_image_url": "http://x/1.jpg"}],
             "asin": "A1", "parent_asin": "P1", "user_id": "U1", "timestamp": 1600000000000, "verified_purchase": True},
            {"rating": 5.0, "title": "ok", "text": "Great product", "images": [], "asin": "A2", "parent_asin": "P2", "user_id": "U2",
             "timestamp": 1600000100000, "verified_purchase": True},
            {"rating": 2.0, "title": "", "text": "", "images": [], "asin": "A3", "parent_asin": "P3", "user_id": "U3", "timestamp": 1600000200000}]
    with gzip.open(path, "wt") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    return path


def make_copydays(root):
    orig, var = pathlib.Path(root, "orig"), pathlib.Path(root, "var")
    for sub in ("jpeg", "crop", "strong"):
        (var / sub).mkdir(parents=True, exist_ok=True)
    orig.mkdir(parents=True, exist_ok=True)
    for k in range(6):
        raw = ed.synthetic_image(900 + k)
        (orig / f"20{k:04d}.jpg").write_bytes(raw)
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        (var / "jpeg" / f"20{k:04d}.jpg").write_bytes(ed._jpeg(im, 30))
        (var / "crop" / f"20{k:04d}.jpg").write_bytes(ed.VARIANTS["crop"](im))
    (var / "strong" / "999999.jpg").write_bytes(ed.synthetic_image(5))   # no matching original
    return str(orig), str(var)


@pytest.fixture()
def olist(tmp_path):
    return make_olist(str(tmp_path / "olist"))


# ----------------------------------------------------------------------------- loaders + mappings
def test_olist_mapping_cleaning_and_provenance(olist):
    info = ed.ingest_olist(olist, limit=None)
    assert info["cleaning"]["dropped_no_purchase_timestamp"] == 1 and info["cleaning"]["dropped_orders_without_items"] == 1
    assert info["loaded"]["orders"] == 38 and info["loaded"]["refund_history"] == 0
    assert db.q1("SELECT COUNT(*) AS n FROM refund_history")["n"] == 0                  # Olist has no refunds
    o = db.q1("SELECT * FROM orders WHERE id='o000'")                                    # two items: 50 + 50
    assert o["amount"] == 100.0 and o["customer_id"] == "u000" and o["category"] == "category_0"
    assert db.q1("SELECT status FROM orders WHERE id='o003'")["status"] == "canceled"    # cancelled order stays an order status
    assert db.q1("SELECT status FROM orders WHERE id='o005'")["status"] == "in_transit"
    assert db.q1("SELECT COUNT(*) AS n FROM orders WHERE category='unknown'")["n"] > 0
    pol = db.q1("SELECT * FROM data_provenance WHERE table_name='policies' LIMIT 1")
    assert pol["label_origin"] == "assumed"                                              # policies are assumptions, not data
    # re-ingesting is idempotent
    ed.ingest_olist(olist, limit=None)
    assert db.q1("SELECT COUNT(*) AS n FROM orders")["n"] == 38


def test_uci_cancellations_are_proxies_not_refunds(tmp_path):
    for xlsx in (False, True):
        p = make_uci(str(tmp_path / ("u.xlsx" if xlsx else "u.csv")), xlsx=xlsx)
        info = ed.ingest_uci(p, limit_customers=None)
        cl = info["cleaning"]
        assert cl["dropped_no_customer_id_or_date"] == 1 and cl["dropped_adjust_bad_debt_invoices"] == 1
        assert cl["dropped_non_product_stock_codes"] == 1 and cl["dropped_non_positive_qty_or_price_on_sales"] == 1
        rows = db.q("SELECT * FROM refund_history")
        assert len(rows) == 6 and {r["status"] for r in rows} == {"cancellation_proxy"}
        assert db.q1("SELECT COUNT(*) AS n FROM refund_history WHERE status='refunded'")["n"] == 0
        assert db.q1("SELECT COUNT(*) AS n FROM eval_labels")["n"] == 0                  # never labelled fraud
        claim = dict(id="X", order_id="UCI-1007", customer_id="UCI-44444", reason="damaged", amount=10.0, claim_time="2010-03-01T00:00:00")
        f = ai.build_features(claim, db.q1("SELECT * FROM orders WHERE customer_id='UCI-44444' AND status='invoiced' LIMIT 1"))
        assert f["orders_total"] == 6                                                    # pseudo cancellation orders are not counted as orders


def test_amazon_reviews_are_not_refund_evidence(tmp_path):
    info = ed.ingest_amazon(make_amazon(str(tmp_path / "r.jsonl.gz")))
    assert info["loaded"] == {"source_reviews": 3, "reviews_with_images": 1}
    assert info["cleaning"]["reviews_with_empty_text_kept_flagged"] == 1
    assert db.q1("SELECT COUNT(*) AS n FROM evidence")["n"] == 0                          # review images never become refund evidence
    assert json.loads(db.q1("SELECT image_urls FROM source_reviews WHERE id='AMZ-0'")["image_urls"]) == ["http://x/1.jpg"]
    assert ed.eval_amazon()["rating_le_2_with_defect_words"] == 1


def test_copydays_pairing(tmp_path):
    orig, var = make_copydays(str(tmp_path / "cd"))
    info = ed.ingest_copydays(orig, var, key_len=6)
    assert info["loaded"]["matched_pairs"] == 12 and info["cleaning"]["variants_without_matching_original"] == 1
    assert info["loaded"]["by_variant_type"] == {"jpeg": 6, "crop": 6}
    res = ed.eval_copydays()
    assert res["status"] == "run" and res["by_variant_type"]["jpeg"]["recall@hamming<=5"] == 1.0


# ----------------------------------------------------------------------------- label provenance
def test_scenarios_are_labelled_synthetic_and_reproducible(olist):
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=3, seed=7)
    labels = db.q("SELECT * FROM eval_labels ORDER BY id")
    assert labels and {l["label_origin"] for l in labels} == {"synthetic"}
    assert all(l["entity_id"].startswith(("SYN-", "CLM-SYN-")) for l in labels)           # source rows are never labelled
    assert {l["scenario"] for l in labels} >= {"serial_refunder", "refund_farming", "policy_exploitation", "evidence_reuse", "legit_control"}
    syn = db.q("SELECT * FROM data_provenance WHERE source='synthetic_generator'")
    assert syn and {r["label_origin"] for r in syn} == {"synthetic"}
    assert db.q1("SELECT COUNT(*) AS n FROM data_provenance WHERE source='olist' AND label_origin<>'source'")["n"] == 0
    first = json.dumps(labels, sort_keys=True)
    ed.generate_scenarios(n=3, seed=7)
    assert json.dumps(db.q("SELECT * FROM eval_labels ORDER BY id"), sort_keys=True) == first   # reproducible with the same seed


# ----------------------------------------------------------------------------- data quality + end to end
def test_every_loaded_row_has_provenance_and_manifest_matches(tmp_path, olist):
    ed.ingest_olist(olist, limit=None)
    ed.ingest_uci(make_uci(str(tmp_path / "u.csv")), limit_customers=None)
    ed.ingest_amazon(make_amazon(str(tmp_path / "r.jsonl.gz")))
    for table in ("customers", "orders", "refund_history", "source_reviews"):
        missing = db.q1(f"SELECT COUNT(*) AS n FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM data_provenance p "
                        f"WHERE p.table_name='{table}' AND p.row_id=t.id)")["n"]
        assert missing == 0, table
    assert db.q1("SELECT COUNT(*) AS n FROM orders WHERE amount<=0 OR customer_id IS NULL")["n"] == 0
    ed.write_manifest()
    md = open(os.path.join(ed.DOCS, "dataset_manifest.md")).read()
    n_orders = db.q1("SELECT COUNT(*) AS n FROM data_provenance WHERE source='olist' AND table_name='orders'")["n"]
    assert f"| olist | orders | source | {n_orders} |" in md and "cancellation_proxy" in md


def test_end_to_end_evaluation_on_fixtures(tmp_path, olist):
    orig, var = make_copydays(str(tmp_path / "cd"))
    ed.cli(["all", "--olist", olist, "--uci", make_uci(str(tmp_path / "u.csv")), "--amazon", make_amazon(str(tmp_path / "r.jsonl.gz")),
            "--copydays-originals", orig, "--copydays-variants", var, "--olist-limit", "0", "--n", "4"])
    res = json.load(open(os.path.join(ed.REPORTS, "eval_results.json")))
    assert all(res[k]["status"] == "run" for k in ("gate_on_olist_orders", "behaviour_on_uci_returns", "evidence_reuse_on_copydays",
                                                   "abuse_scenarios_synthetic", "amazon_text_proxy"))
    assert res["gate_on_olist_orders"]["agreement"] == 1.0
    sc = res["abuse_scenarios_synthetic"]["scenarios"]
    assert sc["serial_refunder"]["target_detection_rate"] == 1.0 and sc["refund_farming"]["target_detection_rate"] == 1.0
    assert sc["policy_exploitation"]["target_detection_rate"] == 1.0
    assert sc["near_miss_serial"]["wrongly_flagged_serial_refunder_rate"] == 0.0 and sc["legit_control"]["any_abuse_flag_rate"] == 0.0
    assert set(sc["evidence_reuse"]["detection_by_transformation"]) <= set(ed.VARIANTS)
    uci = res["behaviour_on_uci_returns"]
    assert uci["customers_with_cancellations_evaluated"] == 1 and uci["pattern_flag_counts"]["serial_refunder"] in (0, 1)
    report = open(os.path.join(ed.DOCS, "eval_report.md")).read()
    assert "**NOT RUN**" not in report and "SYNTHETIC" in report
    # components without data are reported as NOT RUN, never silently skipped
    for t in TABLES:
        db.run(f"DELETE FROM {t}")
    os.remove(ed.LOG_PATH())
    assert ed.evaluate()["evidence_reuse_on_copydays"]["status"] == "not_run"


def test_cost_scale_decides_whether_small_abusive_claims_are_reviewed(olist):
    """Documents the economics: with handling cost 100 and claims of ~150, review costs more than the exposure, so abusive claims are
    auto-approved; scaling the cost matrix to the claim size makes the same claims go to review while controls stay untouched."""
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=4, seed=7)
    res = ed.eval_scenarios(scales=(1.0, 0.2))
    sens = res["intervention_rate_vs_cost_scale"]
    full, scaled = sens["handling/friction cost x1.0"], sens["handling/friction cost x0.2"]
    assert full["serial_refunder"] == full["refund_farming"] == full["policy_exploitation"] == 0.0
    assert scaled["serial_refunder"] == scaled["refund_farming"] == scaled["policy_exploitation"] == 1.0
    assert scaled["legit_control"] == 0.0 and scaled["near_miss_serial"] == 0.0
    assert ai.COST_SCALE == 1.0                                                           # the evaluation restores the setting
