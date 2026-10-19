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
os.environ.pop("LLM_API_KEY", None)
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
    reviews = []
    for i in range(35):                                           # real-looking review rows linked to orders; p0 orders get bad scores
        if i in (6, 7):
            continue
        reviews.append(dict(review_id=f"r{i:03d}", order_id=f"o{i:03d}", review_score=1 if i % 6 == 0 else 5,
                            review_comment_title=None, review_comment_message=("Produto chegou quebrado, pessimo" if i % 6 == 0 and i % 4 else None),
                            review_creation_date="2017-06-01 00:00:00", review_answer_timestamp="2017-06-02 00:00:00"))
    reviews.append(dict(review_id="rZZ", order_id="oZZZ", review_score=3, review_comment_title=None, review_comment_message="sem pedido",
                        review_creation_date="2017-06-01 00:00:00", review_answer_timestamp="2017-06-02 00:00:00"))
    pd.DataFrame(reviews).to_csv(f"{folder}/olist_order_reviews_dataset.csv", index=False)
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
    assert info["loaded"]["orders"] == 38 and info["loaded"]["refund_history"] == 0 and info["loaded"]["reviews"] == 33
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
    assert res["status"] == "run" and res["by_variant_type"]["jpeg"]["recall ahash<=5"] == 1.0 and res["by_variant_type"]["crop"]["recall combined (system rule)"] == 1.0


# ----------------------------------------------------------------------------- Olist reviews linked to real orders
def test_olist_reviews_are_linked_to_real_orders(olist):
    info = ed.ingest_olist(olist, limit=None)
    assert info["cleaning"]["reviews_not_linked_to_a_loaded_order"] == 1                 # rZZ points at an order that is not in the data
    rv = db.q("SELECT * FROM source_reviews WHERE source='olist'")
    assert len(rv) == 33 and all(db.get_order(r["product_ref"]) for r in rv)              # every stored review points at a real loaded order
    assert db.q1("SELECT COUNT(*) AS n FROM evidence")["n"] == 0 and db.q1("SELECT COUNT(*) AS n FROM refund_history")["n"] == 0
    assert db.q1("SELECT COUNT(*) AS n FROM data_provenance WHERE table_name='source_reviews' AND source='olist' AND label_origin='source'")["n"] == 33
    res = ed.eval_olist_reviews()
    assert res["reviews"] == 33 and res["linked_to_orders"] == 33 and res["share_1_2_stars"] == pytest.approx(5 / 33, abs=1e-3)
    assert "Portuguese" in res["note"]
    # review outliers (real signal for product/vendor intelligence); the minimum-review rule is relaxed for the tiny fixture
    old = ai.NEG_REVIEW_MIN
    ai.NEG_REVIEW_MIN = 3
    try:
        out = {p["id"]: p for p in ai.product_intelligence()}
        assert out["p0"]["review_outlier"] and out["p0"]["negative_review_rate"] == 1.0 and not out["p1"]["review_outlier"]
        run = ai.run_historical_loop()
        assert any(i["pattern"] == "negative_review_outlier" for i in ai.list_insights("proposed"))
    finally:
        ai.NEG_REVIEW_MIN = old


def test_labelling_sheet_for_real_reviews(tmp_path, olist):
    ed.ingest_olist(olist, limit=None)
    ed.ingest_amazon(make_amazon(str(tmp_path / "r.jsonl.gz")))
    import csv
    path = ed.write_labelling_sheet(10, 7, str(tmp_path / "sheet.csv"))
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert 0 < len(rows) <= 10 and all(r["label_category"] == "" and r["text"].strip() for r in rows)
    assert {r["source"] for r in rows} <= {"olist", "amazon_reviews_2023"}


# ----------------------------------------------------------------------------- hand-labelled NLP set
def test_nlp_evaluation_on_the_labelled_set():
    res = ed.eval_nlp()
    assert res["status"] == "run"
    s = res["sets"]["developer-authored set"]
    assert s["n"] == 60
    legacy, rules = "legacy substring keywords", "rules (negation-aware, current default)"
    assert s[f"{rules} | accuracy_negation_cases"] > s[f"{legacy} | accuracy_negation_cases"] + 0.3      # negation handling is a real gain
    assert s[f"{rules} | category_accuracy"] > s[f"{legacy} | category_accuracy"]
    assert s[f"{rules} | accuracy_hard_cases"] < 1.0                                                      # the set is not trivially solved
    assert s["sentiment | accuracy_3class"] < 1.0 and "sentiment | backend" in s


# ----------------------------------------------------------------------------- label provenance + independence of the generator
def test_scenarios_are_labelled_synthetic_and_reproducible(olist):
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=3, seed=7)
    labels = db.q("SELECT * FROM eval_labels ORDER BY id")
    assert labels and {l["label_origin"] for l in labels} == {"synthetic"}
    assert all(l["entity_id"].startswith(("SYN-", "CLM-SYN-")) for l in labels)           # source rows are never labelled
    assert {l["scenario"] for l in labels} >= set(ed.PROFILES) | {"evidence_reuse"}
    syn = db.q("SELECT * FROM data_provenance WHERE source='synthetic_generator'")
    assert syn and {r["label_origin"] for r in syn} == {"synthetic"}
    assert db.q1("SELECT COUNT(*) AS n FROM data_provenance WHERE source='olist' AND label_origin<>'source'")["n"] == 0
    first = json.dumps(labels, sort_keys=True)
    ed.generate_scenarios(n=3, seed=7)
    assert json.dumps(db.q("SELECT * FROM eval_labels ORDER BY id"), sort_keys=True) == first   # reproducible with the same seed


def test_generator_is_independent_of_the_detection_rules():
    import inspect
    src = inspect.getsource(ed.generate_scenarios) + inspect.getsource(ed._poisson) + "\n".join(f"{k}={v}" for k, v in ed.PROFILES.items())
    for forbidden in ("ai.", "SERIAL", "FREQ_SAT", "POLICY_EDGE", "FARMING_", "RETURN_ABUSE", "SIMILAR_MAX", "PHASH", "ORB_MIN", "detect_abuse"):
        assert forbidden not in src, forbidden
    # and the outcome shows it: pooled recall of the intended pattern over all abusive profiles is clearly below 100%
    # (checked in test_scenario_evaluation_is_not_circular)


def test_scenario_evaluation_is_not_circular(olist):
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=6, seed=7)
    res = ed.eval_scenarios(scales=(1.0, 0.2))
    prof = res["profiles"]
    assert set(prof) >= set(ed.PROFILES) | {"evidence_reuse_b"} or set(prof) >= set(ed.PROFILES)
    abusive = [v for k, v in prof.items() if v.get("target_pattern")]
    pooled = sum(v["target_pattern_recall"] * v["n"] for v in abusive) / sum(v["n"] for v in abusive)
    assert 0.0 < pooled < 0.95                                    # the detectors miss some abusers: the labels do not come from the rules
    assert all(0.0 <= v["intervention_rate"] <= 1.0 for v in prof.values())
    cls = res["decision_as_classifier_vs_cost_scale"]
    full, scaled = cls["handling/friction cost x1.0"], cls["handling/friction cost x0.2"]
    assert scaled["recall"] >= full["recall"] and scaled["tp"] + scaled["fn"] == full["tp"] + full["fn"]     # cheaper review -> more interventions
    assert ai.COST_SCALE == 1.0                                                                          # the evaluation restores the setting
    assert res["pattern_flag_as_classifier (any abuse pattern or evidence-reuse flag)"]["precision"] >= 0.0


def test_evidence_reuse_scenarios_use_random_strength_and_orb(olist):
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=5, seed=7)
    notes = [json.loads(l["note"]) for l in db.q("SELECT note FROM eval_labels WHERE label='reused_evidence'")]
    assert {n["variant"] for n in notes} == set(ed.VARIANTS)
    for n in notes:
        lo, hi = ed.VARIANT_RANGE[n["variant"]]
        assert lo <= n["image"]["strength"] <= hi
    prof = ed.eval_scenarios(scales=(1.0,))["profiles"]["evidence_reuse"]
    assert prof["reuse_flag_recall"] >= 0.8 and set(prof["recall_by_transformation"]) == set(ed.VARIANTS)


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
    assert f"| olist | orders | source | {n_orders} |" in md and "cancellation_proxy" in md and "| olist | source_reviews | source | 33 |" in md


def test_end_to_end_evaluation_on_fixtures(tmp_path, olist):
    orig, var = make_copydays(str(tmp_path / "cd"))
    ed.cli(["all", "--olist", olist, "--uci", make_uci(str(tmp_path / "u.csv")), "--amazon", make_amazon(str(tmp_path / "r.jsonl.gz")),
            "--copydays-originals", orig, "--copydays-variants", var, "--olist-limit", "0", "--n", "3"])
    res = json.load(open(os.path.join(ed.REPORTS, "eval_results.json")))
    keys = ("gate_on_olist_orders", "behaviour_on_uci_returns", "evidence_reuse_on_copydays", "abuse_scenarios_synthetic",
            "claim_nlp_on_labelled_set", "olist_reviews_real_orders", "amazon_text_proxy")
    assert all(res[k]["status"] == "run" for k in keys)
    assert res["gate_on_olist_orders"]["agreement"] == 1.0
    cd = res["evidence_reuse_on_copydays"]
    assert cd["by_variant_type"]["crop"]["recall combined (system rule)"] == 1.0
    assert "recall ahash<=5" in cd["by_variant_type"]["crop"] and "recall orb>=40" in cd["by_variant_type"]["crop"]
    uci = res["behaviour_on_uci_returns"]
    assert uci["customers_with_cancellations_evaluated"] == 1
    report = open(os.path.join(ed.DOCS, "eval_report.md")).read()
    assert "**NOT RUN**" not in report and "SYNTHETIC" in report and "Olist reviews linked to real orders" in report
    for t in TABLES:                                              # components without data are reported as NOT RUN, never skipped
        db.run(f"DELETE FROM {t}")
    os.remove(ed.LOG_PATH())
    out = ed.evaluate()
    assert out["evidence_reuse_on_copydays"]["status"] == "not_run" and out["olist_reviews_real_orders"]["status"] == "not_run"
