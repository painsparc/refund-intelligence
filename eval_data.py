"""
eval_data.py  -  COMPOSITE EVALUATION DATASET TOOLING (offline; not part of the real-time system)

    python eval_data.py ingest --olist DIR --uci FILE --amazon FILE --copydays-originals DIR --copydays-variants DIR
    python eval_data.py scenarios          # labelled synthetic abuse scenarios (reproducible, seeded)
    python eval_data.py evaluate           # runs every component that has data -> reports/eval_results.json + docs/eval_report.md
    python eval_data.py manifest           # docs/dataset_manifest.md (actual counts, missing values, cleaning decisions)
    python eval_data.py all  <ingest flags>

Uses its OWN database (eval_data.db) so the seeded demo database is untouched; override with DATABASE_URL.

Rules that this file enforces (see docs/data_sources.md):
  * every loaded row has a data_provenance record (source, file, original id, label_origin = source | assumed | synthetic)
  * UCI cancellation invoices are NOT confirmed refunds or fraud: stored as status 'cancellation_proxy', never labelled
  * Amazon review/catalogue images are NOT refund evidence: they never enter the evidence table
  * synthetic labels live only in eval_labels and only on SYN-* entities; source-provided rows are never labelled
"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///eval_data.db")  # must happen before `import db`

import argparse
import gzip
import io
import json
import random
import re
import sys
from datetime import datetime, timedelta

import pandas as pd
from PIL import Image, ImageDraw, ImageFilter

import db
import intelligence as ai
from graph import process_claim

REPORTS = os.getenv("EVAL_REPORTS", "reports")
DOCS = os.getenv("EVAL_DOCS", "docs")
LOG_PATH = lambda: os.path.join(REPORTS, "ingest_log.json")
ASSUMED_POLICY = {"olist": (7, 300.0), "uci": (14, 150.0), "synthetic": (7, 300.0)}  # (window_days, auto-approve limit) - ASSUMPTIONS
DEFECT_WORDS = ["broken", "damaged", "defective", "cracked", "not working", "doesn't work", "stopped working", "faulty", "torn"]
IMG_EXT = (".jpg", ".jpeg", ".png")


# =========================================================================== small helpers
def iso(series):
    """pandas datetime-like -> list of ISO strings (None for missing)."""
    s = pd.to_datetime(series, errors="coerce")
    return [None if pd.isna(v) else v.strftime("%Y-%m-%dT%H:%M:%S") for v in s]


def load_log():
    return json.load(open(LOG_PATH())) if os.path.exists(LOG_PATH()) else {}


def save_log(source, info):
    os.makedirs(REPORTS, exist_ok=True)
    log = load_log()
    log[source] = dict(info, ingested_at=datetime.utcnow().isoformat(timespec="seconds"))
    json.dump(log, open(LOG_PATH(), "w"), indent=2, default=str)


def prov(table, row_id, source, source_file, source_id, label_origin):
    return dict(id=f"{table}:{row_id}", table_name=table, row_id=row_id, source=source, source_file=source_file,
                source_id=str(source_id), label_origin=label_origin)


def purge_source(source):
    """Make ingestion idempotent: remove everything previously loaded from this source."""
    for table in ("customers", "orders", "refund_history", "source_reviews"):
        db.run(f"DELETE FROM {table} WHERE id IN (SELECT row_id FROM data_provenance WHERE table_name=:t AND source=:s)", t=table, s=source)
    db.run("DELETE FROM data_provenance WHERE source=:s", s=source)


def ensure_policy(category, key):
    """Datasets carry no return policy: an explicit, labelled ASSUMPTION per source."""
    if db.q1("SELECT category FROM policies WHERE category=:c", c=category):
        return
    window, limit = ASSUMED_POLICY[key]
    db.insert("policies", category=category, window_days=window, refundable=1, max_auto_amount=limit)
    db.insert("data_provenance", **prov("policies", category, "assumed_policy", "-", category, "assumed"))


def missing(df, cols):
    return {c: int(df[c].isna().sum()) for c in cols if c in df.columns}


# =========================================================================== INGEST 1: Olist
def ingest_olist(folder, limit=20000, seed=7):
    """Olist -> customers / orders. Olist has NO refunds: nothing is written to refund_history."""
    f = lambda n: os.path.join(folder, n)
    orders = pd.read_csv(f("olist_orders_dataset.csv"))
    items = pd.read_csv(f("olist_order_items_dataset.csv"))
    cust = pd.read_csv(f("olist_customers_dataset.csv"))
    prods = pd.read_csv(f("olist_products_dataset.csv"))
    trans_path = f("product_category_name_translation.csv")
    trans = dict(pd.read_csv(trans_path).values[:, :2]) if os.path.exists(trans_path) else {}
    info = {"files": {n: len(d) for n, d in [("olist_orders_dataset.csv", orders), ("olist_order_items_dataset.csv", items),
                                              ("olist_customers_dataset.csv", cust), ("olist_products_dataset.csv", prods)]},
            "missing_values_in_raw": {"orders": missing(orders, ["order_purchase_timestamp", "order_delivered_customer_date"]),
                                      "products": missing(prods, ["product_category_name"])},
            "cleaning": {}, "assumptions": [f"policy per category: window {ASSUMED_POLICY['olist'][0]} days, auto-approve limit "
                                            f"{ASSUMED_POLICY['olist'][1]} (BRL) - NOT in the dataset"]}
    cl = info["cleaning"]
    n = len(orders)
    orders = orders.dropna(subset=["order_purchase_timestamp"])
    cl["dropped_no_purchase_timestamp"] = n - len(orders)
    orders = orders.merge(cust[["customer_id", "customer_unique_id"]], on="customer_id", how="left")
    n = len(orders)
    orders = orders.dropna(subset=["customer_unique_id"])
    cl["dropped_no_customer_match"] = n - len(orders)
    items = items.sort_values(["order_id", "order_item_id"])
    first, total = items.groupby("order_id").first(), items.groupby("order_id")["price"].sum()
    n = len(orders)
    orders = orders[orders["order_id"].isin(total.index)]
    cl["dropped_orders_without_items"] = n - len(orders)
    if limit and len(orders) > limit:
        cl["sampled_down_from"] = len(orders)
        orders = orders.sample(limit, random_state=seed)
    cl["sampling"] = f"random sample of {limit} orders, seed {seed}" if cl.get("sampled_down_from") else "all orders loaded"
    prod_cat = prods.set_index("product_id")["product_category_name"].to_dict()
    status_map = {"delivered": "delivered", "canceled": "canceled", "unavailable": "canceled"}

    purge_source("olist")
    P, order_rows, cust_first, policies_done = [], [], {}, set()
    unknown_cat = 0
    for r, dl, pt in zip(orders.itertuples(), iso(orders["order_delivered_customer_date"]), iso(orders["order_purchase_timestamp"])):
        it = first.loc[r.order_id]
        raw_cat = prod_cat.get(it["product_id"])
        cat = trans.get(raw_cat, raw_cat) if isinstance(raw_cat, str) else None
        if cat is None:
            cat, unknown_cat = "unknown", unknown_cat + 1
        if cat not in policies_done:
            ensure_policy(cat, "olist")
            policies_done.add(cat)
        order_rows.append(dict(id=r.order_id, customer_id=r.customer_unique_id, product_id=it["product_id"], product_name=cat,
                               category=cat, vendor_id=it["seller_id"], amount=float(total[r.order_id]),
                               status=status_map.get(r.order_status, "in_transit"), order_date=pt, delivered_at=dl))
        cust_first[r.customer_unique_id] = min(cust_first.get(r.customer_unique_id, pt), pt)
        P.append(prov("orders", r.order_id, "olist", "olist_orders_dataset.csv + olist_order_items_dataset.csv", r.order_id, "source"))
    cust_rows = [dict(id=c, name=None, email=None, created_at=t) for c, t in cust_first.items()]
    P += [prov("customers", c, "olist", "olist_customers_dataset.csv", c, "source") for c in cust_first]
    db.bulk_insert("customers", cust_rows)
    db.bulk_insert("orders", order_rows)
    db.bulk_insert("data_provenance", P)
    cl["products_without_category_set_to_unknown"] = unknown_cat
    info["loaded"] = {"customers": len(cust_rows), "orders": len(order_rows), "refund_history": 0}
    info["notes"] = ["customer id = customer_unique_id; customer.created_at = first purchase INSIDE the loaded sample",
                     "amount = sum of item prices (freight excluded); product/vendor = first item of the order",
                     "no refund or cancellation is converted into a refund; Olist order_status 'canceled' stays an order status"]
    save_log("olist", info)
    return info


# =========================================================================== INGEST 2: UCI Online Retail II
_UCI_COLS = {"Invoice": "invoice", "InvoiceNo": "invoice", "StockCode": "stock", "Description": "desc", "Quantity": "qty",
             "InvoiceDate": "date", "Price": "price", "UnitPrice": "price", "Customer ID": "customer", "CustomerID": "customer"}


def ingest_uci(path, limit_customers=3000, seed=7):
    """UCI -> customers / orders; 'C' invoices -> refund_history rows with status 'cancellation_proxy' (NOT confirmed refunds)."""
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.concat(pd.read_excel(path, sheet_name=None).values(), ignore_index=True)
    else:
        df = pd.read_csv(path, encoding="latin-1")
    df = df.rename(columns={k: v for k, v in _UCI_COLS.items() if k in df.columns})
    info = {"files": {os.path.basename(path): len(df)}, "missing_values_in_raw": missing(df, ["customer", "desc", "price", "qty", "date"]),
            "cleaning": {}, "assumptions": [f"policy for category 'retail': window {ASSUMED_POLICY['uci'][0]} days, auto-approve limit "
                                            f"{ASSUMED_POLICY['uci'][1]} (GBP) - NOT in the dataset",
                                            "no delivery date exists: orders.delivered_at is NULL, so window/late-filing logic is not exercised on UCI"]}
    cl = info["cleaning"]
    df["invoice"] = df["invoice"].astype(str)
    n = len(df)
    df = df.dropna(subset=["customer", "date"])
    cl["dropped_no_customer_id_or_date"] = n - len(df)
    df["customer"] = df["customer"].astype(float).astype(int).astype(str)
    n = len(df)
    df = df[~df["invoice"].str.startswith("A")]
    cl["dropped_adjust_bad_debt_invoices"] = n - len(df)
    n = len(df)
    df = df[df["stock"].astype(str).str.match(r"^\d{5}")]
    cl["dropped_non_product_stock_codes"] = n - len(df)
    cancel = df["invoice"].str.startswith("C")
    n = len(df)
    df = df[cancel | ((df["qty"] > 0) & (df["price"] > 0))]
    cl["dropped_non_positive_qty_or_price_on_sales"] = n - len(df)
    if limit_customers:
        custs = sorted(df["customer"].unique())
        if len(custs) > limit_customers:
            keep = set(random.Random(seed).sample(custs, limit_customers))
            df = df[df["customer"].isin(keep)]
            cl["sampling"] = f"random sample of {limit_customers} customers (all their invoices), seed {seed}"
    cl.setdefault("sampling", "all customers loaded")
    df = df.assign(value=(df["qty"].abs() * df["price"]), iso_date=iso(df["date"]), is_cancel=df["invoice"].str.startswith("C"))
    ensure_policy("retail", "uci")

    purge_source("uci")
    P, orders, refunds = [], [], []
    g = df.groupby("invoice")
    agg = g.agg(customer=("customer", "first"), date=("iso_date", "min"), amount=("value", "sum"), cancel=("is_cancel", "first"))
    top = df.loc[g["value"].idxmax()].set_index("invoice")[["stock", "desc"]]
    for inv, r in agg.iterrows():
        t = top.loc[inv]
        pid, name = str(t["stock"]), (None if pd.isna(t["desc"]) else str(t["desc"]))
        if r["cancel"]:
            oid = f"UCI-CANCEL-{inv}"
            orders.append(dict(id=oid, customer_id=f"UCI-{r['customer']}", product_id=pid, product_name=name, category="retail",
                               vendor_id="UCI-RETAILER", amount=float(r["amount"]), status="cancellation_invoice",
                               order_date=r["date"], delivered_at=None))
            refunds.append(dict(id=f"RF-UCI-{inv}", order_id=oid, customer_id=f"UCI-{r['customer']}", product_id=pid, vendor_id="UCI-RETAILER",
                                category="retail", reason="cancellation_proxy", amount=float(r["amount"]), status="cancellation_proxy",
                                created_at=r["date"]))
            P += [prov("orders", oid, "uci", os.path.basename(path), inv, "source"),
                  prov("refund_history", f"RF-UCI-{inv}", "uci", os.path.basename(path), inv, "source")]
        else:
            oid = f"UCI-{inv}"
            orders.append(dict(id=oid, customer_id=f"UCI-{r['customer']}", product_id=pid, product_name=name, category="retail",
                               vendor_id="UCI-RETAILER", amount=float(r["amount"]), status="invoiced", order_date=r["date"], delivered_at=None))
            P.append(prov("orders", oid, "uci", os.path.basename(path), inv, "source"))
    first_seen = agg.groupby("customer")["date"].min()
    cust_rows = [dict(id=f"UCI-{c}", name=None, email=None, created_at=t) for c, t in first_seen.items()]
    P += [prov("customers", f"UCI-{c}", "uci", os.path.basename(path), c, "source") for c in first_seen.index]
    for table, rows in (("customers", cust_rows), ("orders", orders), ("refund_history", refunds), ("data_provenance", P)):
        db.bulk_insert(table, rows)
    info["loaded"] = {"customers": len(cust_rows), "orders": len(orders) - len(refunds), "refund_history(cancellation_proxy)": len(refunds),
                      "cancellation_pseudo_orders": len(refunds)}
    info["notes"] = ["C-prefixed invoices are cancellations/credit notes: stored as refund_history rows with status 'cancellation_proxy', "
                     "NEVER as confirmed refunds, and NEVER labelled fraud",
                     "each cancellation also gets a pseudo-order (status 'cancellation_invoice', excluded from order counts) so joins work"]
    save_log("uci", info)
    return info


# =========================================================================== INGEST 3: Amazon Reviews 2023 (text side only)
def ingest_amazon(path, limit=5000):
    """Amazon reviews -> source_reviews only. Review images are NOT refund evidence and never enter the evidence table."""
    opener = gzip.open if path.endswith(".gz") else open
    rows, miss_text, with_img = [], 0, 0
    with opener(path, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if limit and i >= limit:
                break
            r = json.loads(line)
            imgs = [x.get("large_image_url") or x.get("medium_image_url") or x.get("small_image_url") for x in (r.get("images") or [])]
            imgs = [u for u in imgs if u]
            miss_text += 0 if (r.get("text") or "").strip() else 1
            with_img += 1 if imgs else 0
            ts = r.get("timestamp")
            rows.append(dict(id=f"AMZ-{i}", source="amazon_reviews_2023", source_id=f"{r.get('user_id')}|{r.get('parent_asin')}|{ts}",
                             product_ref=r.get("parent_asin") or r.get("asin"), rating=r.get("rating"), title=r.get("title"),
                             review_text=r.get("text"), n_images=len(imgs), image_urls=json.dumps(imgs),
                             created_at=datetime.utcfromtimestamp(ts / 1000).isoformat(timespec="seconds") if ts else None))
    purge_source("amazon_reviews_2023")
    db.bulk_insert("source_reviews", rows)
    db.bulk_insert("data_provenance", [prov("source_reviews", r["id"], "amazon_reviews_2023", os.path.basename(path), r["source_id"], "source")
                                       for r in rows])
    info = {"files": {os.path.basename(path): len(rows)}, "cleaning": {"limit_applied": limit, "reviews_with_empty_text_kept_flagged": miss_text},
            "loaded": {"source_reviews": len(rows), "reviews_with_images": with_img},
            "notes": ["review images are customer REVIEW content, not refund evidence: only image URLs are stored, nothing enters `evidence`",
                      "used only as a text/rating proxy for the Evidence & Claim agent and (optionally) for image-quality distribution"]}
    save_log("amazon_reviews_2023", info)
    return info


def fetch_amazon_images(n=100, out_dir=None):
    """Optional helper (needs internet): download up to n review images for the image-quality distribution."""
    import requests
    out_dir = out_dir or os.path.join(REPORTS, "amazon_images")
    os.makedirs(out_dir, exist_ok=True)
    got = 0
    for r in db.q("SELECT id, image_urls FROM source_reviews WHERE n_images>0"):
        if got >= n:
            break
        try:
            url = json.loads(r["image_urls"])[0]
            open(os.path.join(out_dir, f"{r['id']}.jpg"), "wb").write(requests.get(url, timeout=20).content)
            got += 1
        except Exception:
            continue
    return got


# =========================================================================== INGEST 4: Copydays (image-similarity benchmark)
def _images_under(folder):
    out = []
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            if fn.lower().endswith(IMG_EXT):
                out.append(os.path.join(root, fn))
    return sorted(out)


def copydays_pairs(originals, variants, key_len):
    """Ground truth from the benchmark: a variant belongs to the original sharing the first key_len chars of the file stem."""
    stem = lambda p: os.path.splitext(os.path.basename(p))[0]
    orig = {stem(p)[:key_len]: p for p in _images_under(originals)}
    pairs, unmatched = [], 0
    for v in _images_under(variants):
        k = stem(v)[:key_len]
        rel = os.path.relpath(v, variants).split(os.sep)
        vtype = rel[0] if len(rel) > 1 else "variant"
        if k in orig:
            pairs.append({"variant": v, "original": orig[k], "type": vtype})
        else:
            unmatched += 1
    return orig, pairs, unmatched


def ingest_copydays(originals, variants, key_len=6):
    orig, pairs, unmatched = copydays_pairs(originals, variants, key_len)
    types = {}
    for p in pairs:
        types[p["type"]] = types.get(p["type"], 0) + 1
    info = {"files": {"originals": len(orig), "variants_total": len(pairs) + unmatched}, "originals_dir": originals,
            "variants_dir": variants, "key_len": key_len, "loaded": {"matched_pairs": len(pairs), "by_variant_type": types},
            "cleaning": {"variants_without_matching_original": unmatched},
            "notes": ["images are NOT loaded into the database; they are read from disk by the evaluation",
                      "CHECK the pairing: verify a few (variant, original) pairs by eye, adjust --key-len if unmatched is large"]}
    save_log("copydays", info)
    return info


# =========================================================================== SYNTHETIC IMAGES / SCENARIOS
def synthetic_image(seed, size=(800, 600)):
    r = random.Random(seed)
    img = Image.new("RGB", size, tuple(r.randint(40, 200) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(80):
        x, y = r.randint(0, size[0]), r.randint(0, size[1])
        d.rectangle([x, y, x + r.randint(20, 200), y + r.randint(20, 150)], fill=tuple(r.randint(0, 255) for _ in range(3)))
    return _jpeg(img)


def _jpeg(img, q=90):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=q)
    return buf.getvalue()


VARIANTS = {  # how a fraudster might re-upload someone else's photo
    "jpeg": lambda im: _jpeg(im, 40),
    "resize": lambda im: _jpeg(im.resize((int(im.width * 0.6), int(im.height * 0.6))), 80),
    "crop": lambda im: _jpeg(im.crop((int(im.width * .12), int(im.height * .12), int(im.width * .88), int(im.height * .88))), 85),
    "blur": lambda im: _jpeg(im.filter(ImageFilter.GaussianBlur(2.5)), 85),
}


def reset_synthetic():
    claims = [r["id"] for r in db.q("SELECT id FROM refund_requests WHERE customer_id LIKE 'SYN-%'")]
    for cid in claims:
        for t in ("review_cases", "action_log", "risk_signals", "evidence"):
            db.run(f"DELETE FROM {t} WHERE claim_id=:c", c=cid)
    for t, col in (("refund_requests", "customer_id"), ("refund_history", "customer_id"), ("orders", "customer_id")):
        db.run(f"DELETE FROM {t} WHERE {col} LIKE 'SYN-%'")
    db.run("DELETE FROM customers WHERE id LIKE 'SYN-%'")
    db.run("DELETE FROM data_provenance WHERE source='synthetic_generator'")
    db.run("DELETE FROM eval_labels")


def _templates(rnd):
    """Real delivered orders to clone attributes from (Olist if loaded); built-in fallback otherwise."""
    rows = db.q("SELECT * FROM orders WHERE status='delivered' AND delivered_at IS NOT NULL AND id NOT LIKE 'SYN-%' LIMIT 3000")
    if rows:
        rnd.shuffle(rows)
        return rows, "real orders (cloned attributes)"
    base = [("electronics", "V-A", 180.0), ("fashion", "V-B", 90.0), ("home", "V-C", 120.0), ("beauty", "V-D", 40.0)]
    return [dict(id=f"BUILTIN-{i}", category=c, vendor_id=v, amount=a, product_id=f"P{i}", product_name=c, delivered_at="2018-06-01T10:00:00")
            for i, (c, v, a) in enumerate(base)], "built-in templates (no real orders loaded)"


def generate_scenarios(n=30, seed=7):
    """Reproducible, explicitly labelled synthetic scenarios. Everything created here is SYN-* with label_origin='synthetic'."""
    rnd = random.Random(seed)
    reset_synthetic()
    temps, tsource = _templates(rnd)
    cd = load_log().get("copydays")
    originals = _images_under(cd["originals_dir"]) if cd else []
    P, labels, counter = [], [], [0]

    def nid(prefix):
        counter[0] += 1
        return f"{prefix}-{counter[0]:06d}"

    def label(scn, ctype, ent, lab, note=None):
        labels.append(dict(id=nid("LBL"), scenario_id=f"{scn}-{seed}", scenario=scn, entity_type=ctype, entity_id=ent, label=lab,
                           label_origin="synthetic", note=json.dumps(note) if note else None))

    def make_customer(scn, k, claim_time):
        cid = f"SYN-{scn}-{k:03d}"
        db.insert("customers", id=cid, name=None, email=None, created_at=(claim_time - timedelta(days=500)).isoformat(timespec="seconds"))
        P.append(prov("customers", cid, "synthetic_generator", "eval_data.py", cid, "synthetic"))
        return cid

    def make_order(cid, tmpl, delivered, amount=None, status="delivered"):
        oid = nid("SYN-ORD")
        ensure_policy(tmpl["category"], "synthetic")
        db.insert("orders", id=oid, customer_id=cid, product_id=tmpl["product_id"], product_name=tmpl.get("product_name") or tmpl["category"],
                  category=tmpl["category"], vendor_id=tmpl["vendor_id"], amount=amount or tmpl["amount"], status=status,
                  order_date=(delivered - timedelta(days=4)).isoformat(timespec="seconds"), delivered_at=delivered.isoformat(timespec="seconds"))
        P.append(prov("orders", oid, "synthetic_generator", "eval_data.py", tmpl["id"], "synthetic"))
        return oid

    def past_refund(cid, tmpl, claim_time, days_before, frac_of_window, amount_frac):
        pol = db.get_policy(tmpl["category"]) or {"window_days": 7, "max_auto_amount": 300.0}
        filed = claim_time - timedelta(days=days_before)
        delivered = filed - timedelta(days=frac_of_window * pol["window_days"])
        amount = round(pol["max_auto_amount"] * amount_frac, 2)
        oid = make_order(cid, tmpl, delivered, amount=amount)
        rid = nid("SYN-RF")
        db.insert("refund_history", id=rid, order_id=oid, customer_id=cid, product_id=tmpl["product_id"], vendor_id=tmpl["vendor_id"],
                  category=tmpl["category"], reason=rnd.choice(["damaged", "defective", "changed_mind", "size_issue"]), amount=amount,
                  status="refunded", created_at=filed.isoformat(timespec="seconds"))
        P.append(prov("refund_history", rid, "synthetic_generator", "eval_data.py", tmpl["id"], "synthetic"))

    def plain_orders(cid, tmpl, claim_time, k):
        for j in range(k):
            make_order(cid, tmpl, claim_time - timedelta(days=rnd.randint(40, 400)))

    def current_claim(scn, cid, tmpl, claim_time, image_spec, amount_frac=0.5, reason="damaged"):
        pol = db.get_policy(tmpl["category"]) or {"max_auto_amount": 300.0, "window_days": 7}
        delivered = claim_time - timedelta(days=rnd.uniform(0.5, 0.4 * pol["window_days"]))
        amt = round(pol["max_auto_amount"] * amount_frac, 2)
        oid = make_order(cid, tmpl, delivered, amount=amt)
        claim = dict(id=f"CLM-{cid}", order_id=oid, customer_id=cid, reason=reason, amount=amt, claim_time=claim_time.isoformat(timespec="seconds"),
                     description="The item arrived damaged and does not work as described.")
        return claim

    ct0 = datetime(2018, 6, 1, 12, 0, 0)
    for k in range(n):
        ct = ct0 + timedelta(days=k)
        t = temps[k % len(temps)]

        # --- serial refunder: 5 refunds in the last 60 days
        cid = make_customer("serial_refunder", k, ct)
        for j in range(5):
            past_refund(cid, t, ct, days_before=8 + j * 11, frac_of_window=0.4, amount_frac=0.4)
        plain_orders(cid, t, ct, 5)
        cl = current_claim("serial_refunder", cid, t, ct, {"seed": 10_000 + k})
        label("serial_refunder", "customer", cid, "serial_refunder"); label("serial_refunder", "claim", cl["id"], "abusive_claim",
                                                                           {"claim": cl, "image": {"kind": "synthetic", "seed": 10_000 + k}, "expect_pattern": "serial_refunder"})

        # --- refund farming: 4 refunds just under the auto-approve limit, spread over ~4 months
        cid = make_customer("refund_farming", k, ct)
        for j in range(4):
            past_refund(cid, t, ct, days_before=120 + j * 30, frac_of_window=0.3, amount_frac=rnd.uniform(0.87, 0.99))
        plain_orders(cid, t, ct, 12)
        cl = current_claim("refund_farming", cid, t, ct, {"seed": 20_000 + k}, amount_frac=0.95)
        label("refund_farming", "customer", cid, "refund_farming"); label("refund_farming", "claim", cl["id"], "abusive_claim",
                                                                         {"claim": cl, "image": {"kind": "synthetic", "seed": 20_000 + k}, "expect_pattern": "refund_farming"})

        # --- policy exploitation: 4 refunds filed in the last 15% of the return window
        cid = make_customer("policy_exploitation", k, ct)
        for j in range(4):
            past_refund(cid, t, ct, days_before=130 + j * 25, frac_of_window=0.9, amount_frac=0.5)
        plain_orders(cid, t, ct, 12)
        cl = current_claim("policy_exploitation", cid, t, ct, {"seed": 30_000 + k})
        label("policy_exploitation", "customer", cid, "policy_exploitation"); label("policy_exploitation", "claim", cl["id"], "abusive_claim",
                                                                                   {"claim": cl, "image": {"kind": "synthetic", "seed": 30_000 + k}, "expect_pattern": "policy_exploitation"})

        # --- evidence reuse: victim's photo re-uploaded by another customer after a transformation
        variant = list(VARIANTS)[k % len(VARIANTS)]
        src = {"kind": "copydays", "path": originals[k % len(originals)]} if originals else {"kind": "synthetic", "seed": 40_000 + k}
        a = make_customer("evidence_reuse", k, ct); plain_orders(a, t, ct, 8)
        ca = current_claim("evidence_reuse", a, t, ct - timedelta(hours=2), src)
        b = make_customer("evidence_reuse_b", k, ct); plain_orders(b, t, ct, 8)
        cb = current_claim("evidence_reuse_b", b, t, ct, src)
        label("evidence_reuse", "claim", ca["id"], "original_photo_owner", {"claim": ca, "image": dict(src), "order": 0})
        label("evidence_reuse", "claim", cb["id"], "reused_evidence",
              {"claim": cb, "image": dict(src, variant=variant), "order": 1, "expect_flag": "evidence_*_other_customer", "variant": variant})

        # --- controls: legitimate customers (and a near-miss just below the serial threshold)
        cid = make_customer("legit_control", k, ct); plain_orders(cid, t, ct, 8)
        if k % 3 == 0:
            past_refund(cid, t, ct, days_before=200, frac_of_window=0.3, amount_frac=0.3)
        cl = current_claim("legit_control", cid, t, ct, {"seed": 50_000 + k}, amount_frac=0.3)
        label("legit_control", "customer", cid, "legitimate"); label("legit_control", "claim", cl["id"], "legitimate_claim",
                                                                    {"claim": cl, "image": {"kind": "synthetic", "seed": 50_000 + k}})
        cid = make_customer("near_miss_serial", k, ct); plain_orders(cid, t, ct, 8)
        for j in range(3):  # 3 refunds in 90 days: below the serial_refunder threshold of 4
            past_refund(cid, t, ct, days_before=15 + j * 25, frac_of_window=0.3, amount_frac=0.4)
        cl = current_claim("near_miss_serial", cid, t, ct, {"seed": 60_000 + k}, amount_frac=0.3)
        label("near_miss_serial", "customer", cid, "legitimate_near_threshold"); label("near_miss_serial", "claim", cl["id"], "legitimate_claim",
                                                                                      {"claim": cl, "image": {"kind": "synthetic", "seed": 60_000 + k}, "forbid_pattern": "serial_refunder"})
    db.bulk_insert("data_provenance", P)
    db.bulk_insert("eval_labels", labels)
    info = {"seed": seed, "n_per_scenario": n, "templates": tsource, "scenarios": sorted({l["scenario"] for l in labels}),
            "synthetic_rows": {"data_provenance": len(P), "eval_labels": len(labels)},
            "assumptions": [f"policy window/limit for synthetic orders: {ASSUMED_POLICY['synthetic']}",
                            "labels are synthetic and tell us what the generator intended, not what real fraud looks like"]}
    save_log("synthetic_scenarios", info)
    return info


def _image_bytes(spec):
    if spec["kind"] == "copydays":
        raw = open(spec["path"], "rb").read()
        base = Image.open(io.BytesIO(raw)).convert("RGB")
        base.thumbnail((1024, 1024))
    else:
        base = Image.open(io.BytesIO(synthetic_image(spec["seed"]))).convert("RGB")
    if spec.get("variant"):
        return VARIANTS[spec["variant"]](base)
    return _jpeg(base)


# =========================================================================== EVALUATION
def _pct(vals, q):
    vals = sorted(vals)
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 4) if vals else None


def eval_gate_olist(max_orders=1500):
    rows = db.q("SELECT o.* FROM orders o JOIN data_provenance p ON p.table_name='orders' AND p.row_id=o.id WHERE p.source='olist' "
                "AND o.status='delivered' AND o.delivered_at IS NOT NULL LIMIT :n", n=max_orders)
    if not rows:
        return {"status": "not_run", "reason": "no Olist delivered orders loaded"}
    checks = agree = 0
    bad = []
    for o in rows:
        pol = db.get_policy(o["category"])
        delivered = datetime.fromisoformat(o["delivered_at"])
        cases = [(0.5, o["customer_id"], o["amount"], True), (0.99, o["customer_id"], o["amount"], True),
                 (1.01, o["customer_id"], o["amount"], False), (2.0, o["customer_id"], o["amount"], False),
                 (0.5, "someone-else", o["amount"], False), (0.5, o["customer_id"], o["amount"] * 1.5 + 1, False)]
        for frac, cust, amt, expected in cases:
            ct = delivered + timedelta(days=frac * pol["window_days"])
            g = ai.policy_gate(dict(order_id=o["id"], customer_id=cust, reason="damaged", amount=amt, claim_time=ct.isoformat()))
            checks += 1
            if g["eligible"] == expected:
                agree += 1
            elif len(bad) < 5:
                bad.append({"order": o["id"], "frac": frac, "expected": expected, "got": g["violations"]})
    return {"status": "run", "data": "Olist (real order timestamps, categories, amounts) + ASSUMED policy + synthetic claim timing",
            "orders": len(rows), "checks": checks, "agreement": round(agree / checks, 4), "disagreements_sample": bad,
            "note": "validates the deterministic gate implementation on real timestamps; says nothing about policy realism"}


def eval_behaviour_uci(max_customers=1500):
    custs = db.q("SELECT DISTINCT r.customer_id AS c FROM refund_history r JOIN data_provenance p ON p.table_name='refund_history' AND p.row_id=r.id "
                 "WHERE p.source='uci' LIMIT :n", n=max_customers)
    if not custs:
        return {"status": "not_run", "reason": "no UCI cancellation rows loaded"}
    pats, brs, r90, rates = {p: 0 for p in ai.ABUSE_PATTERNS}, [], [], []
    for row in custs:
        c = row["c"]
        last = db.q1("SELECT MAX(created_at) AS t FROM refund_history WHERE customer_id=:c", c=c)["t"]
        order = db.q1("SELECT * FROM orders WHERE customer_id=:c AND status='invoiced' ORDER BY order_date DESC LIMIT 1", c=c)
        if not order:
            continue
        claim = dict(id="PROBE", order_id=order["id"], customer_id=c, reason="damaged", amount=min(order["amount"], 100.0), claim_time=last)
        f = ai.build_features(claim, order)
        found = ai.detect_abuse_patterns(f, claim, {"present": False})
        for p in found:
            pats[p] += 1
        brs.append(ai.behaviour_risk_formula(f)[0])
        r90.append(f["refunds_90d"])
        rates.append(f["refund_rate"])
    n = len(brs)
    return {"status": "run", "data": "UCI Online Retail II cancellation invoices as RETURN-LIKE PROXY events (unlabelled, not fraud)",
            "customers_with_cancellations_evaluated": n, "pattern_flag_counts": pats,
            "pattern_flag_rate": {p: round(v / max(n, 1), 4) for p, v in pats.items()},
            "behaviour_risk_0_100": {"p50": _pct(brs, .5), "p90": _pct(brs, .9), "p99": _pct(brs, .99), "share_ge_50": round(sum(b >= 50 for b in brs) / max(n, 1), 4)},
            "refunds_90d": {"p50": _pct(r90, .5), "p90": _pct(r90, .9), "max": max(r90) if r90 else None},
            "refund_rate": {"p50": _pct(rates, .5), "p90": _pct(rates, .9)},
            "note": "flag RATES on real, unlabelled behaviour - NOT precision/recall/FPR. UCI has no delivery dates, so timing patterns cannot fire; "
                    "buyers are largely wholesalers. Cancellations are not confirmed refunds."}


def eval_copydays(thresholds=(3, 5, 8, 12)):
    info = load_log().get("copydays")
    if not info:
        return {"status": "not_run", "reason": "Copydays not ingested"}
    orig, pairs, _ = copydays_pairs(info["originals_dir"], info["variants_dir"], info["key_len"])
    if not pairs:
        return {"status": "not_run", "reason": "no (variant, original) pairs found - check --key-len"}
    sig = lambda p: ai.image_signature(open(p, "rb").read())
    osig = {k: sig(p) for k, p in orig.items()}
    keys = list(osig)
    stem_key = lambda p: os.path.splitext(os.path.basename(p))[0][:info["key_len"]]
    per = {}
    top1 = {}
    for p in pairs:
        vs = sig(p["variant"])
        true_k = stem_key(p["original"])
        dists = {k: ai._hamming(vs["ahash"], osig[k]["ahash"]) for k in keys}
        t = per.setdefault(p["type"], {"n": 0, **{f"recall@{h}": 0 for h in thresholds}, **{f"false_matches@{h}": 0 for h in thresholds}, "top1": 0})
        t["n"] += 1
        t["top1"] += int(min(dists, key=dists.get) == true_k)
        for h in thresholds:
            t[f"recall@{h}"] += int(dists[true_k] <= h)
            t[f"false_matches@{h}"] += sum(1 for k, d in dists.items() if k != true_k and d <= h)
    out = {}
    for typ, t in per.items():
        n = t["n"]
        out[typ] = {"n": n, "top1_accuracy": round(t["top1"] / n, 4),
                    **{f"recall@hamming<={h}": round(t[f"recall@{h}"] / n, 4) for h in thresholds},
                    **{f"false_match_rate@hamming<={h}": round(t[f"false_matches@{h}"] / (n * max(len(keys) - 1, 1)), 6) for h in thresholds}}
    q = [v["quality"] for v in osig.values()]
    return {"status": "run", "data": "INRIA Copydays (source-provided ground truth: variant -> original)", "originals": len(keys),
            "variants": len(pairs), "operating_threshold": ai.SIMILAR_MAX_HAMMING, "by_variant_type": out,
            "original_quality_score": {"p10": _pct(q, .1), "p50": _pct(q, .5), "p90": _pct(q, .9)},
            "note": "average-hash is cheap but weak on crops / strong edits; the sweep shows the trade-off"}


def _run_scenarios():
    """Process every labelled scenario claim through the real graph and score it against the (synthetic) labels."""
    labels = db.q("SELECT * FROM eval_labels WHERE entity_type='claim' ORDER BY id")
    for cid in [l["entity_id"] for l in labels]:  # make evaluation re-runnable
        for t in ("review_cases", "action_log", "risk_signals", "evidence"):
            db.run(f"DELETE FROM {t} WHERE claim_id=:c", c=cid)
        db.run("DELETE FROM refund_requests WHERE id=:c", c=cid)
    db.run("DELETE FROM refund_history WHERE status='refunded' AND id NOT LIKE 'SYN-RF-%' AND customer_id LIKE 'SYN-%'")
    db.run("DELETE FROM return_events WHERE customer_id LIKE 'SYN-%'")
    specs = sorted([(json.loads(l["note"]), l) for l in labels], key=lambda x: x[0].get("order", 0))
    res = {}
    for spec, l in specs:
        out = process_claim(spec["claim"], _image_bytes(spec["image"]))
        if l["label"] == "original_photo_owner":
            continue  # only seeds the evidence store; not scored
        s = res.setdefault(l["scenario"], {"label": l["label"], "n": 0, "detected": 0, "not_auto_approved": 0, "auto_approved": 0, "any_abuse_flag": 0})
        s["n"] += 1
        s["not_auto_approved"] += int(out["decision"] != "auto_approve")
        s["auto_approved"] += int(out["decision"] == "auto_approve")
        s["any_abuse_flag"] += int(bool(out["abuse_patterns"]))
        if "expect_pattern" in spec:
            s["detected"] += int(spec["expect_pattern"] in out["abuse_patterns"])
        elif "expect_flag" in spec:
            hit = any(f.startswith("evidence_") and "other_customer" in f for f in out["flags"])
            s["detected"] += int(hit)
            v = s.setdefault("by_variant", {}).setdefault(spec["variant"], {"n": 0, "detected": 0})
            v["n"] += 1
            v["detected"] += int(hit)
        elif "forbid_pattern" in spec:
            s["detected"] += int(spec["forbid_pattern"] in out["abuse_patterns"])  # here: wrongly flagged
    summary = {}
    for scn, s in res.items():
        n = max(s["n"], 1)
        row = {"label": s["label"], "n": s["n"], "intervention_rate(not auto-approved)": round(s["not_auto_approved"] / n, 4),
               "auto_approve_rate": round(s["auto_approved"] / n, 4), "any_abuse_flag_rate": round(s["any_abuse_flag"] / n, 4)}
        if scn == "near_miss_serial":
            row["wrongly_flagged_serial_refunder_rate"] = round(s["detected"] / n, 4)
        elif scn != "legit_control":
            row["target_detection_rate"] = round(s["detected"] / n, 4)
        if "by_variant" in s:
            row["detection_by_transformation"] = {k: round(v["detected"] / v["n"], 4) for k, v in s["by_variant"].items()}
        summary[scn] = row
    return summary


def eval_scenarios(scales=(1.0, 0.5, 0.2, 0.1)):
    if not db.q1("SELECT 1 AS x FROM eval_labels WHERE entity_type='claim' LIMIT 1"):
        return {"status": "not_run", "reason": "run `scenarios` first"}
    default_scale, by_scale = ai.COST_SCALE, {}
    try:
        for sc in scales:
            ai.COST_SCALE = sc
            by_scale[sc] = _run_scenarios()
    finally:
        ai.COST_SCALE = default_scale
    base = by_scale[scales[0]]
    sensitivity = {f"handling/friction cost x{sc}": {k: v["intervention_rate(not auto-approved)"] for k, v in summ.items()}
                   for sc, summ in by_scale.items()}
    return {"status": "run", "data": "SYNTHETIC labelled scenarios (seeded) on top of real order attributes where available",
            "scenarios": base, "intervention_rate_vs_cost_scale": sensitivity,
            "note": "synthetic and rule-aligned by construction: this verifies the pipeline end to end and the evidence-reuse robustness; "
                    "it is NOT an estimate of real-world fraud detection performance. The expected-cost matrix is in ONE currency unit "
                    "(handling 100, auto-approve limit 300 here): small claims are auto-approved when review costs more than the exposure, "
                    "so set COST_SCALE to the deployment's currency/claim size (see the sensitivity table)."}


def eval_amazon(image_dir=None):
    rows = db.q("SELECT rating, review_text, n_images FROM source_reviews WHERE source='amazon_reviews_2023'")
    if not rows:
        return {"status": "not_run", "reason": "Amazon reviews not ingested"}
    low = [r for r in rows if r["rating"] is not None and r["rating"] <= 2]
    defect = [r for r in low if any(w in (r["review_text"] or "").lower() for w in DEFECT_WORDS)]
    out = {"status": "run", "data": "Amazon Reviews 2023 (REVIEWS, not refund claims) - text/rating proxy",
           "reviews": len(rows), "with_images": sum(1 for r in rows if r["n_images"]), "rating_le_2": len(low),
           "rating_le_2_with_defect_words": len(defect),
           "note": "no refund labels exist here; review images are not refund evidence and are not in the evidence table"}
    if image_dir and os.path.isdir(image_dir):
        q = [ai.image_signature(open(p, "rb").read())["quality"] for p in _images_under(image_dir)]
        out["review_image_quality_distribution"] = {"n": len(q), "p10": _pct(q, .1), "p50": _pct(q, .5), "p90": _pct(q, .9),
                                                    "share_below_0.4": round(sum(x < .4 for x in q) / max(len(q), 1), 4)}
    return out


def evaluate(amazon_images=None):
    os.makedirs(REPORTS, exist_ok=True)
    results = {"generated_at": datetime.utcnow().isoformat(timespec="seconds"), "database": db.DB_URL.split("@")[-1],
               "gate_on_olist_orders": eval_gate_olist(), "behaviour_on_uci_returns": eval_behaviour_uci(),
               "evidence_reuse_on_copydays": eval_copydays(), "abuse_scenarios_synthetic": eval_scenarios(),
               "amazon_text_proxy": eval_amazon(amazon_images)}
    json.dump(results, open(os.path.join(REPORTS, "eval_results.json"), "w"), indent=2, default=str)
    write_eval_report(results)
    return results


def _md_table(d):
    keys = list(d)
    return "| " + " | ".join(keys) + " |\n|" + "---|" * len(keys) + "\n| " + " | ".join(str(d[k]) for k in keys) + " |\n"


def write_eval_report(res):
    os.makedirs(DOCS, exist_ok=True)
    L = [f"# Evaluation report (auto-generated)\n", f"Generated {res['generated_at']} by `python eval_data.py evaluate`. "
         "Every number below is computed from the data actually loaded; components without data say NOT RUN.\n"]
    titles = {"gate_on_olist_orders": "1. Deterministic policy gate on real Olist orders",
              "behaviour_on_uci_returns": "2. Behavioural features / abuse rules on UCI return-like behaviour (unlabelled)",
              "evidence_reuse_on_copydays": "3. Evidence reuse detection on INRIA Copydays (source ground truth)",
              "abuse_scenarios_synthetic": "4. Controlled abuse scenarios (SYNTHETIC labels)",
              "amazon_text_proxy": "5. Amazon Reviews 2023 text/rating proxy"}
    for key, title in titles.items():
        r = res[key]
        L.append(f"\n## {title}\n")
        if r["status"] != "run":
            L.append(f"**NOT RUN** - {r['reason']}\n")
            continue
        L.append(f"*Data:* {r['data']}\n")
        for k, v in r.items():
            if k in ("status", "data", "note"):
                continue
            if isinstance(v, dict) and v and all(isinstance(x, dict) for x in v.values()):
                L.append(f"\n**{k}**\n")
                for name, row in v.items():
                    L.append(f"\n`{name}`\n\n" + _md_table({a: b for a, b in row.items() if not isinstance(b, dict)}))
                    for a, b in row.items():
                        if isinstance(b, dict):
                            L.append(f"  {a}: " + ", ".join(f"{x}={y}" for x, y in b.items()) + "\n")
            elif isinstance(v, dict):
                L.append(f"\n**{k}**\n\n" + _md_table(v))
            else:
                L.append(f"- **{k}**: {v}")
        L.append(f"\n> {r['note']}\n")
    open(os.path.join(DOCS, "eval_report.md"), "w").write("\n".join(L))


# =========================================================================== MANIFEST
COMPONENT_SOURCES = [
    ("Deterministic order + policy gate", "Olist (real timestamps/categories/amounts); policy = ASSUMED"),
    ("Behavioural features, abuse rules", "UCI Online Retail II cancellations as return-like proxy; synthetic labelled abusers for recall"),
    ("Evidence reuse / similarity", "INRIA Copydays (ground-truth copies); synthetic re-upload scenarios"),
    ("Claim text consistency (Evidence & Claim agent)", "Amazon Reviews 2023 text as a proxy only"),
    ("Expected-cost decision, guardrails, end-to-end routing", "Synthetic labelled scenarios + seeded demo data"),
    ("Historical product/vendor intelligence", "Olist sellers/products (real); UCI products"),
]


def write_manifest():
    os.makedirs(DOCS, exist_ok=True)
    log = load_log()
    L = ["# Dataset manifest (auto-generated)\n", f"Generated {datetime.utcnow().isoformat(timespec='seconds')} by `python eval_data.py manifest` "
         f"from `{LOG_PATH()}` and the evaluation database. Counts are what was ACTUALLY loaded.\n"]
    if not log:
        L.append("**No ingestion has been run yet.** Run `python eval_data.py ingest ...` and regenerate this file.\n")
    for src, info in log.items():
        L.append(f"\n## {src}\n\n- ingested at: {info.get('ingested_at')}")
        for k in ("files", "missing_values_in_raw", "loaded", "cleaning", "assumptions", "notes"):
            if k in info:
                v = info[k]
                L.append(f"- **{k}**: " + (json.dumps(v, default=str) if isinstance(v, (dict, list)) and k not in ("assumptions", "notes") else ""))
                if k in ("assumptions", "notes"):
                    L += [f"  - {x}" for x in v]
    counts = db.q("SELECT source, table_name, label_origin, COUNT(*) AS n FROM data_provenance GROUP BY source, table_name, label_origin "
                  "ORDER BY source, table_name")
    L.append("\n## Rows in the unified schema (by provenance)\n\n| source | table | label_origin | rows |\n|---|---|---|---|")
    L += [f"| {c['source']} | {c['table_name']} | {c['label_origin']} | {c['n']} |" for c in counts]
    L.append("\n## Source supporting each evaluation component\n\n| component | data |\n|---|---|")
    L += [f"| {a} | {b} |" for a, b in COMPONENT_SOURCES]
    open(os.path.join(DOCS, "dataset_manifest.md"), "w").write("\n".join(L) + "\n")


# =========================================================================== CLI
def cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ingest", "scenarios", "evaluate", "manifest", "fetch-amazon-images", "all"])
    ap.add_argument("--olist"), ap.add_argument("--uci"), ap.add_argument("--amazon")
    ap.add_argument("--copydays-originals"), ap.add_argument("--copydays-variants")
    ap.add_argument("--key-len", type=int, default=6, help="chars of the file stem that pair a Copydays variant with its original")
    ap.add_argument("--olist-limit", type=int, default=20000), ap.add_argument("--uci-customers", type=int, default=3000)
    ap.add_argument("--amazon-limit", type=int, default=5000), ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=7), ap.add_argument("--amazon-images")
    a = ap.parse_args(argv)
    db.init()
    if a.cmd in ("ingest", "all"):
        if a.olist:
            print("olist:", ingest_olist(a.olist, a.olist_limit, a.seed)["loaded"])
        if a.uci:
            print("uci:", ingest_uci(a.uci, a.uci_customers, a.seed)["loaded"])
        if a.amazon:
            print("amazon:", ingest_amazon(a.amazon, a.amazon_limit)["loaded"])
        if a.copydays_originals and a.copydays_variants:
            print("copydays:", ingest_copydays(a.copydays_originals, a.copydays_variants, a.key_len)["loaded"])
    if a.cmd in ("scenarios", "all"):
        print("scenarios:", generate_scenarios(a.n, a.seed)["synthetic_rows"])
    if a.cmd == "fetch-amazon-images":
        print("downloaded", fetch_amazon_images(a.n), "images")
    if a.cmd in ("evaluate", "all"):
        r = evaluate(a.amazon_images)
        print({k: (v["status"] if isinstance(v, dict) and "status" in v else v) for k, v in r.items()})
        print(f"wrote {REPORTS}/eval_results.json and {DOCS}/eval_report.md")
    if a.cmd in ("manifest", "all"):
        write_manifest()
        print(f"wrote {DOCS}/dataset_manifest.md")


if __name__ == "__main__":
    sys.exit(cli())
