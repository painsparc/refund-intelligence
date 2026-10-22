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
    for k in ("evidence_reuse_copydays_validation_test_split", "cost_calibration_synthetic"):
        assert out[k]["status"] == "not_run"


# ----------------------------------------------------------------------------- Priority fixes: link audit, split evaluation, cost sweep, labelling
def test_olist_review_link_audit_explains_every_unlinked_review(olist):
    info = ed.ingest_olist(olist, limit=None)
    a = info["cleaning"]["review_link_audit"]
    assert a["reviews_in_raw_file"] == info["files"]["olist_order_reviews_dataset.csv"]
    assert a["linked_and_loaded"] == info["loaded"]["reviews"] == 33
    unlinked = a["reviews_in_raw_file"] - a["linked_and_loaded"]
    assert a["order_id_absent_from_raw_orders_file (join impossible)"] + a["order_exists_but_not_loaded (cleaning or --olist-limit sampling)"] <= unlinked
    assert unlinked == info["cleaning"]["reviews_not_linked_to_a_loaded_order"]
    assert (a["  of which order removed by cleaning (e.g. order has no items)"] + a["  of which order removed by --olist-limit sampling"]
            == a["order_exists_but_not_loaded (cleaning or --olist-limit sampling)"])


def test_manifest_has_missing_value_checks_and_schema_mapping(tmp_path, olist):
    ed.ingest_olist(olist, limit=None)
    ed.write_manifest()
    md = open(os.path.join(ed.DOCS, "dataset_manifest.md")).read()
    assert "Missing-value checks" in md and "| orders.delivered_at |" in md and "Source-to-schema mapping" in md and "review_link_audit" in md
    assert "Download record" in md


def test_copydays_validation_test_split_never_tunes_on_the_test_part(tmp_path):
    orig, var = make_copydays(str(tmp_path / "cd"))
    ed.ingest_copydays(orig, var, key_len=6)
    res = ed.eval_copydays_split(val_frac=0.4, seed=7, max_false_match=0.01)
    assert res["status"] == "run"
    sp = res["split"]
    assert sp["validation_originals"] + sp["test_originals"] == 6 and sp["validation_originals"] >= 2 and sp["test_originals"] >= 2
    assert sp["validation_variants"] + sp["test_variants"] == 12                     # a variant always stays with its original
    assert set(res["tuned_on_validation"]) >= {"ahash", "phash"}
    allrow = res["test_by_variant_type"]["ALL TYPES"]
    assert allrow["n_variants"] == sp["test_variants"] and 0.0 <= allrow["recall combined, tuned on validation"] <= 1.0
    assert allrow["recall combined, current system thresholds"] == 1.0                # jpeg + crop copies of synthetic images are found
    assert ai.PHASH_MAX_HAMMING == 8 and ai.SIMILAR_MAX_HAMMING == 5                  # the evaluation never changes the live thresholds
    again = ed.eval_copydays_split(val_frac=0.4, seed=7, max_false_match=0.01)
    assert again["split"] == sp                                                       # reproducible split
    tiny = tmp_path / "tiny"
    o2, v2 = make_copydays(str(tiny))
    for f in list(pathlib.Path(o2).glob("*"))[2:]:
        f.unlink()
    ed.ingest_copydays(o2, v2, key_len=6)
    assert ed.eval_copydays_split()["status"] == "not_run"                            # too few originals to split honestly


def test_cost_calibration_sweep_is_monotonic_and_restores_the_scale(olist):
    ed.ingest_olist(olist, limit=None)
    ed.generate_scenarios(n=4, seed=7)
    res = ed.eval_cost_calibration(scales=(1.0, 0.1, 0.01), max_fpr=0.5)
    assert res["status"] == "run" and ai.COST_SCALE == 1.0
    t = res["by_cost_scale"]
    assert t["x0.01"]["tp"] + t["x0.01"]["fn"] == t["x1.0"]["tp"] + t["x1.0"]["fn"]    # same labelled claims at every scale
    assert t["x0.01"]["recall"] >= t["x1.0"]["recall"]                                 # cheaper review -> never fewer interventions on abusers
    assert t["x0.01"]["false_positive_rate"] >= t["x1.0"]["false_positive_rate"] - 1e-9
    be = res["break_even_claim_amount_for_manual_review_vs_auto_approve"]
    assert be["exposure risk 0.8"]["x1.0"] < be["exposure risk 0.4"]["x1.0"] < be["exposure risk 0.2"]["x1.0"]   # riskier claim -> smaller amount justifies review
    assert be["exposure risk 0.4"]["x0.1"] < be["exposure risk 0.4"]["x1.0"]
    assert res["recommended_cost_scale"]["scale"] in (1.0, 0.1, 0.01)


def test_break_even_amount_matches_the_decision_engine():
    """The analytic break-even in the report must agree with expected_cost_decision on the same numbers."""
    for scale in (1.0, 0.2):
        for risk in (0.4, 0.8):
            slope = risk * (ai.EXPOSURE_FACTOR["auto_approve"] - ai.EXPOSURE_FACTOR["manual_review"]) - (1 - risk) * ai.FRICTION_RATE["manual_review"]
            amt = scale * (ai.HANDLING["manual_review"] + ai.FRICTION_FIXED["manual_review"]) / slope
            old, ai.COST_SCALE = ai.COST_SCALE, scale
            try:
                above, below = ai.cost_table(amt * 1.2, risk), ai.cost_table(amt * 0.8, risk)
            finally:
                ai.COST_SCALE = old
            assert above["manual_review"]["total"] < above["auto_approve"]["total"]
            assert below["manual_review"]["total"] > below["auto_approve"]["total"]


def test_two_annotator_labelling_workflow(tmp_path):
    import csv
    import label_tools as lt
    assert lt.cohen_kappa(["a", "a", "b", "b"], ["a", "a", "b", "b"]) == 1.0
    assert lt.cohen_kappa(["a", "b"], ["b", "a"]) == -1.0
    assert lt.cohen_kappa(["a", "a"], ["a", "a"]) is None                              # undefined, reported as such
    sheet, d = tmp_path / "sheet.csv", str(tmp_path / "lab")
    rows = [dict(id=f"R{i}", text=t, claimed_reason="", label_category="", text_supports_claim="", label_sentiment="", tag="", source="olist", rating="1")
            for i, t in enumerate(["it arrived cracked", "works great", "wrong colour sent", "never arrived", "too small for me", "arrived fine"])]
    with open(sheet, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader(), w.writerows(rows)
    pa, pb = lt.split(str(sheet), d, seed=7)
    a, b = lt.read(pa), lt.read(pb)
    assert [r["claimed_reason"] for r in a] == [r["claimed_reason"] for r in b] and all(r["claimed_reason"] in lt.REASONS for r in a)
    assert "rating" not in a[0] and all(r["label_category"] == "" for r in a)           # blind copies, no star ratings
    with pytest.raises(SystemExit):                                                    # blank labels are refused
        lt.reconcile(d)
    cats = ["damaged", "unclear", "wrong_item", "not_delivered", "size_issue", "unclear"]
    sents = ["negative", "positive", "negative", "negative", "neutral", "positive"]
    for r, c, s in zip(a, cats, sents):
        r.update(label_category=c, text_supports_claim="1", label_sentiment=s)
    for r, c, s in zip(b, cats, sents):
        r.update(label_category=c, text_supports_claim="1", label_sentiment=s)
    b[1]["label_sentiment"], b[2]["text_supports_claim"] = "neutral", "0"             # two disagreements
    for p, rs in ((pa, a), (pb, b)):
        lt.write(p, rs, list(rs[0]))
    rep = lt.reconcile(d)
    assert rep["n"] == 6 and rep["rows_with_any_disagreement"] == 2
    assert rep["fields"]["label_category"]["raw_agreement"] == 1.0 and rep["fields"]["label_sentiment"]["disagreements"] == 1
    out = str(tmp_path / "manual_labels_real.csv")
    orig_sheet = lt.SHEET
    lt.SHEET = str(sheet)
    try:
        with pytest.raises(SystemExit):                                                # unresolved disagreements block the final file
            lt.finalize(d, out)
        dis = lt.read(os.path.join(d, "disagreements.csv"))
        for r in dis:
            r["final_label_sentiment"] = r["final_label_sentiment"] or "negative"
            r["final_text_supports_claim"] = r["final_text_supports_claim"] or "1"
        lt.write(os.path.join(d, "disagreements.csv"), dis, list(dis[0]))
        path, n = lt.finalize(d, out)
    finally:
        lt.SHEET = orig_sheet
    final = lt.read(path)
    assert n == 6 and set(final[0]) == set(lt.OUT_COLUMNS) and all(r["tag"] == "real" for r in final)
    res = ed._eval_label_file(path)                                                    # the evaluation can consume the reconciled file
    assert res["n"] == 6 and "errors" in res


def test_backend_validation_fallback_keeps_the_rules_running(monkeypatch):
    import validate_backends as vb
    for k in ("CLAIM_NLP", "CLAIM_NLP_MODEL", "IMAGE_TEXT", "CLIP_MODEL", "SENTIMENT_MODEL"):
        monkeypatch.setenv(k, "x")
    try:
        fb = vb.validate_fallback()
    finally:
        vb.reset_backends()
    assert fb["fallback_ok"] and fb["backend_reported"] == "rules" and fb["category"] == "damaged"
    assert vb.auc([0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0]) == 1.0 and vb.auc([0.5], [1]) is None


def test_nlp_error_analysis_is_written_for_inspection():
    res = ed.eval_nlp()
    errs = json.load(open(os.path.join(ed.REPORTS, "nlp_error_analysis.json")))
    s = errs["developer-authored set"]
    assert set(s) == {"sentiment_negative_missed", "sentiment_false_negative_alarms", "category_errors_rules"}
    assert len(s["sentiment_negative_missed"]) == res["sets"]["developer-authored set"]["sentiment | negative_reviews_missed"] > 0


def test_resolution_rationale_explains_why_the_action_was_selected():
    risk = {"claim_risk": 20.0, "behaviour_risk": 10.0, "trust_index": 90.0}
    costs = {"auto_approve": 10.0, "refund_after_return": 60.0, "request_evidence": 30.0, "manual_review": 105.0}
    d = {"action": "auto_approve", "costs": costs, "forced": [], "eligible": ["auto_approve", "manual_review"]}
    out = ai.resolution_agent({"amount": 100}, {"flags": []}, {"abuse_patterns": []}, risk, d)
    assert "lowest expected cost" in out["rationale"] and "10.0 vs 105.0 for manual_review" in out["rationale"]
    forced = dict(d, action="manual_review", forced=["blacklisted_customer"])
    assert "lowest expected cost" not in ai.resolution_agent({"amount": 100}, {"flags": []}, {"abuse_patterns": []}, risk, forced)["rationale"]   # guardrail cases say so


def test_labelling_sheet_can_be_english_only_and_complaint_heavy(tmp_path, olist):
    import csv
    ed.ingest_olist(olist, limit=None)
    ed.ingest_amazon(make_amazon(str(tmp_path / "r.jsonl.gz")))
    path = ed.write_labelling_sheet(10, 7, str(tmp_path / "s.csv"), source="amazon_reviews_2023", low_share=0.5)
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    assert rows and {r["source"] for r in rows} == {"amazon_reviews_2023"}
    low = sum(1 for r in rows if float(r["rating"]) <= 2)
    avail_low = db.q1("SELECT COUNT(*) AS n FROM source_reviews WHERE source='amazon_reviews_2023' AND rating<=2 AND TRIM(review_text)<>''")["n"]
    assert low == min(avail_low, 5)
