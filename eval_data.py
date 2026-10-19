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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

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
    rev_path = f("olist_order_reviews_dataset.csv")
    reviews_loaded = 0
    if os.path.exists(rev_path):
        rv = pd.read_csv(rev_path)
        loaded_ids = {r["id"] for r in order_rows}
        linked = rv[rv["order_id"].isin(loaded_ids) & rv["review_score"].notna()]
        clean = lambda v: None if pd.isna(v) else str(v)
        review_rows = [dict(id=f"OLR-{i}", source="olist", source_id=f"{r.review_id}|{r.order_id}", product_ref=r.order_id,
                            rating=float(r.review_score), title=clean(r.review_comment_title), review_text=clean(r.review_comment_message),
                            n_images=0, image_urls="[]", created_at=(iso([r.review_creation_date])[0]))
                       for i, r in enumerate(linked.itertuples())]
        db.bulk_insert("source_reviews", review_rows)
        db.bulk_insert("data_provenance", [prov("source_reviews", r["id"], "olist", "olist_order_reviews_dataset.csv", r["source_id"], "source")
                                           for r in review_rows])
        reviews_loaded = len(review_rows)
        info["files"]["olist_order_reviews_dataset.csv"] = len(rv)
        info["missing_values_in_raw"]["reviews"] = missing(rv, ["review_score", "review_comment_message"])
        cl["reviews_not_linked_to_a_loaded_order"] = int(len(rv) - len(linked))
        cl["reviews_loaded_with_empty_comment"] = int(sum(1 for r in review_rows if not (r["review_text"] or "").strip()))
    info["loaded"] = {"customers": len(cust_rows), "orders": len(order_rows), "refund_history": 0, "reviews": reviews_loaded}
    info["notes"] = ["customer id = customer_unique_id; customer.created_at = first purchase INSIDE the loaded sample",
                     "amount = sum of item prices (freight excluded); product/vendor = first item of the order",
                     "no refund or cancellation is converted into a refund; Olist order_status 'canceled' stays an order status",
                     "reviews are real customer text + 1-5 scores linked to real orders (Portuguese); a bad review is NOT a refund claim and NOT a fraud signal"]
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





def reset_synthetic():
    claims = [r["id"] for r in db.q("SELECT id FROM refund_requests WHERE customer_id LIKE 'SYN-%'")]
    for cid in claims:
        for t in ("review_cases", "action_log", "risk_signals", "evidence"):
            db.run(f"DELETE FROM {t} WHERE claim_id=:c", c=cid)
    for t, col in (("refund_requests", "customer_id"), ("refund_history", "customer_id"), ("orders", "customer_id")):
        db.run(f"DELETE FROM {t} WHERE {col} LIKE 'SYN-%'")
    db.run("DELETE FROM return_events WHERE customer_id LIKE 'SYN-%'")
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


def _center_crop(im, frac):
    dx, dy = int(im.width * (1 - frac) / 2), int(im.height * (1 - frac) / 2)
    return im.crop((dx, dy, im.width - dx, im.height - dy))


VARIANTS = {  # how a fraudster might re-upload someone else's photo; the 2nd argument is the strength (drawn at random per instance)
    "jpeg": lambda im, p=40: _jpeg(im, int(p)),
    "resize": lambda im, p=0.6: _jpeg(im.resize((int(im.width * p), int(im.height * p))), 80),
    "crop": lambda im, p=0.76: _jpeg(_center_crop(im, p), 85),
    "blur": lambda im, p=2.5: _jpeg(im.filter(ImageFilter.GaussianBlur(p)), 85),
    "strong": lambda im, p=0.6: _jpeg(ImageEnhance.Brightness(_center_crop(im, p).filter(ImageFilter.GaussianBlur(1.5))).enhance(1.3), 60),
}
VARIANT_RANGE = {"jpeg": (25, 60), "resize": (0.4, 0.8), "crop": (0.5, 0.9), "blur": (1.5, 4.0), "strong": (0.5, 0.75)}

# GENERATING PROCESSES for the synthetic customers. They describe how each kind of customer BEHAVES (how many refunds per year, how late in
# the return window they file, how close to the auto-approve limit, how often they skip the return). They deliberately do NOT use, import or
# mirror any detection threshold of the system, so a detector can miss abusers and flag heavy genuine customers: the evaluation is not circular.
PROFILES = {
    #                  abusive  orders/yr  refunds/yr ~Poisson  filing delay in window ~Beta   amount of order  returns late/missing
    "normal":           dict(abusive=0, orders=(5, 12),  refunds=0.6, delay=(2, 5),   amount=(0.2, 1.0), bad_return=0.04),
    "heavy_legit":      dict(abusive=0, orders=(10, 16), refunds=3.5, delay=(2, 5),   amount=(0.2, 1.0), bad_return=0.04),
    "serial_refunder":  dict(abusive=1, orders=(8, 14),  refunds=10,  delay=(2, 3),   amount=(0.2, 0.8), bad_return=0.10, pattern="serial_refunder"),
    "refund_farmer":    dict(abusive=1, orders=(8, 14),  refunds=6,   delay=(2, 4),   amount=None,       bad_return=0.05, pattern="refund_farming"),
    "policy_exploiter": dict(abusive=1, orders=(8, 14),  refunds=5,   delay=(7, 1.5), amount=(0.3, 0.9), bad_return=0.05, pattern="policy_exploitation"),
    "return_abuser":    dict(abusive=1, orders=(8, 14),  refunds=4,   delay=(2, 4),   amount=(0.3, 0.9), bad_return=0.65, pattern="return_abuse"),
}
DESCRIPTIONS = ["The item arrived damaged and does not work properly.", "Parts are broken, it was like this when I opened the box.",
                "The product is defective, it stopped working after a day.", "Packaging was crushed and the item inside is cracked.",
                "It arrived with visible damage on one side.", "Not working at all, I tried everything."]


def _poisson(rnd, lam):
    import math
    L, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rnd.random()
        if p <= L:
            return k
        k += 1


def generate_scenarios(n=40, seed=7):
    """Reproducible labelled synthetic customers drawn from PROFILES. Everything created is SYN-*, provenance 'synthetic_generator',
    label origin 'synthetic'. The label is the generating profile - never the outcome of a detection rule."""
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

    def make_order(cid, tmpl, delivered, amount=None):
        oid = nid("SYN-ORD")
        ensure_policy(tmpl["category"], "synthetic")
        db.insert("orders", id=oid, customer_id=cid, product_id=tmpl["product_id"], product_name=tmpl.get("product_name") or tmpl["category"],
                  category=tmpl["category"], vendor_id=tmpl["vendor_id"], amount=amount or tmpl["amount"], status="delivered",
                  order_date=(delivered - timedelta(days=4)).isoformat(timespec="seconds"), delivered_at=delivered.isoformat(timespec="seconds"))
        P.append(prov("orders", oid, "synthetic_generator", "eval_data.py", tmpl["id"], "synthetic"))
        return oid

    def past_refund(cid, prof, claim_time):
        t = rnd.choice(temps)
        pol = db.get_policy(t["category"]) or {"window_days": 7, "max_auto_amount": 300.0}
        filed = claim_time - timedelta(days=rnd.uniform(3, 365))
        delivered = filed - timedelta(days=rnd.betavariate(*prof["delay"]) * pol["window_days"])
        if prof["amount"] is None:      # a farmer knows the auto-approve limit and stays below it, with some slack
            amount = round(pol["max_auto_amount"] * rnd.uniform(0.6, 1.0), 2)
        else:
            amount = round(t["amount"] * rnd.uniform(*prof["amount"]), 2)
        oid = make_order(cid, t, delivered, amount=max(amount, 1.0))
        rid = nid("SYN-RF")
        db.insert("refund_history", id=rid, order_id=oid, customer_id=cid, product_id=t["product_id"], vendor_id=t["vendor_id"],
                  category=t["category"], reason=rnd.choice(["damaged", "defective", "wrong_item", "size_issue", "changed_mind"]),
                  amount=max(amount, 1.0), status="refunded", created_at=filed.isoformat(timespec="seconds"))
        P.append(prov("refund_history", rid, "synthetic_generator", "eval_data.py", t["id"], "synthetic"))
        if t["category"] != "grocery":
            bad = rnd.random() < prof["bad_return"]
            db.insert("return_events", id=nid("SYN-RET"), customer_id=cid, claim_id=None, refund_id=rid,
                      status=(rnd.choice(["late", "missing"]) if bad else "received"), event_at=filed.isoformat(timespec="seconds"))

    def current_claim(cid, ct, image_spec, extra=None):
        t = rnd.choice(temps)
        ensure_policy(t["category"], "synthetic")
        pol = db.get_policy(t["category"])
        delivered = ct - timedelta(days=rnd.uniform(0.5, 0.4 * pol["window_days"]))
        amt = round(min(t["amount"], 3 * pol["max_auto_amount"]) * rnd.uniform(0.6, 1.0), 2)
        oid = make_order(cid, t, delivered, amount=max(amt, 1.0))
        claim = dict(id=f"CLM-{cid}", order_id=oid, customer_id=cid, reason=rnd.choice(["damaged", "defective"]), amount=max(amt, 1.0),
                     claim_time=ct.isoformat(timespec="seconds"), description=rnd.choice(DESCRIPTIONS))
        return claim

    ct0 = datetime(2018, 6, 1, 12, 0, 0)
    for k in range(n):
        ct = ct0 + timedelta(days=k)
        for name, prof in PROFILES.items():
            cid = make_customer(name, k, ct)
            for _ in range(rnd.randint(*prof["orders"])):
                make_order(cid, rnd.choice(temps), ct - timedelta(days=rnd.randint(10, 400)))
            for _ in range(_poisson(rnd, prof["refunds"])):
                past_refund(cid, prof, ct)
            cl = current_claim(cid, ct, None)
            seed_img = {"kind": "synthetic", "seed": 1_000_000 + 1000 * len(name) + k * 7 + sum(map(ord, name))}
            label(name, "customer", cid, name)
            label(name, "claim", cid + "-claim", "abusive_claim" if prof["abusive"] else "legitimate_claim",
                  {"claim": cl, "image": seed_img, "abusive": prof["abusive"], "expect_pattern": prof.get("pattern")})
            labels[-1]["entity_id"] = cl["id"]
        # evidence reuse: an ordinary customer's photo is re-uploaded by another ordinary customer after an edit of random strength
        variant = list(VARIANTS)[k % len(VARIANTS)]
        strength = round(rnd.uniform(*VARIANT_RANGE[variant]), 3)
        src = {"kind": "copydays", "path": originals[k % len(originals)]} if originals else {"kind": "synthetic", "seed": 40_000 + k}
        a = make_customer("evidence_reuse", k, ct)
        for _ in range(rnd.randint(5, 10)):
            make_order(a, rnd.choice(temps), ct - timedelta(days=rnd.randint(10, 400)))
        ca = current_claim(a, ct - timedelta(hours=2), src)
        b = make_customer("evidence_reuse_b", k, ct)
        for _ in range(rnd.randint(5, 10)):
            make_order(b, rnd.choice(temps), ct - timedelta(days=rnd.randint(10, 400)))
        cb = current_claim(b, ct, src)
        label("evidence_reuse", "claim", ca["id"], "original_photo_owner", {"claim": ca, "image": dict(src), "order": 0})
        label("evidence_reuse", "claim", cb["id"], "reused_evidence",
              {"claim": cb, "image": dict(src, variant=variant, strength=strength), "order": 1, "abusive": 1,
               "expect_flag": "evidence_*_other_customer", "variant": variant})
    db.bulk_insert("data_provenance", P)
    db.bulk_insert("eval_labels", labels)
    info = {"seed": seed, "n_per_profile": n, "templates": tsource, "profiles": {k: {a: b for a, b in v.items()} for k, v in PROFILES.items()},
            "synthetic_rows": {"data_provenance": len(P), "eval_labels": len(labels)},
            "assumptions": [f"policy window/limit for synthetic orders: {ASSUMED_POLICY['synthetic']}",
                            "customers are drawn from behavioural PROFILES that do not reference any detection threshold; abusers overlap with heavy "
                            "genuine customers, so recall < 100% and false positives > 0 are expected",
                            "labels are synthetic: they say how the generator made the customer, not what real fraud looks like"]}
    save_log("synthetic_scenarios", info)
    return info


def _image_bytes(spec):
    if spec["kind"] == "copydays":
        base = Image.open(io.BytesIO(open(spec["path"], "rb").read())).convert("RGB")
        base.thumbnail((1024, 1024))
    else:
        base = Image.open(io.BytesIO(synthetic_image(spec["seed"]))).convert("RGB")
    if spec.get("variant"):
        return VARIANTS[spec["variant"]](base, spec["strength"]) if "strength" in spec else VARIANTS[spec["variant"]](base)
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


def eval_copydays(orb=True, phash_thresholds=(6, 8, 12), orb_thresholds=(25, 40, 60)):
    """Reuse detection on INRIA Copydays with source ground truth, per method: aHash, pHash, ORB local features, and the combined rule."""
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
    use_orb = orb and ai.cv2 is not None
    per = {}
    for p in pairs:
        vs = sig(p["variant"])
        true_k = stem_key(p["original"])
        ah = {k: ai._hamming(vs["ahash"], osig[k]["ahash"]) for k in keys}
        ph = {k: ai._hamming(vs["phash"], osig[k]["phash"]) for k in keys} if vs.get("phash") else None
        ob = {k: ai.orb_inliers(vs["orb"], osig[k]["orb"]) for k in keys} if use_orb and vs.get("orb") else None
        t = per.setdefault(p["type"], {"n": 0, "m": {}})
        t["n"] += 1
        rules = {f"ahash<={ai.SIMILAR_MAX_HAMMING}": (ah, lambda d: d <= ai.SIMILAR_MAX_HAMMING)}
        if ph:
            rules.update({f"phash<={h}": (ph, (lambda h: lambda d: d <= h)(h)) for h in phash_thresholds})
        if ob:
            rules.update({f"orb>={m}": (ob, (lambda m: lambda d: d >= m)(m)) for m in orb_thresholds})
        for name, (dist, ok) in rules.items():
            m = t["m"].setdefault(name, {"hit": 0, "false": 0})
            m["hit"] += int(ok(dist[true_k]))
            m["false"] += sum(1 for k, d in dist.items() if k != true_k and ok(d))
        # the operating rule used by the system: ANY of aHash, pHash, ORB at the configured thresholds
        m = t["m"].setdefault("combined (system rule)", {"hit": 0, "false": 0})
        hit = lambda k: (ah[k] <= ai.SIMILAR_MAX_HAMMING or (ph and ph[k] <= ai.PHASH_MAX_HAMMING) or (ob and ob[k] >= ai.ORB_MIN_INLIERS))
        m["hit"] += int(bool(hit(true_k)))
        m["false"] += sum(1 for k in keys if k != true_k and hit(k))
    out = {}
    for typ, t in per.items():
        n = t["n"]
        out[typ] = {"n": n}
        for name, m in t["m"].items():
            out[typ][f"recall {name}"] = round(m["hit"] / n, 4)
            out[typ][f"false-match rate {name}"] = round(m["false"] / (n * max(len(keys) - 1, 1)), 6)
    q = [v["quality"] for v in osig.values()]
    return {"status": "run", "data": "INRIA Copydays (source-provided ground truth: variant -> original)", "originals": len(keys),
            "variants": len(pairs), "orb_available": use_orb, "operating_thresholds": {"ahash": ai.SIMILAR_MAX_HAMMING, "phash": ai.PHASH_MAX_HAMMING,
                                                                                         "orb_inliers": ai.ORB_MIN_INLIERS},
            "by_variant_type": out, "original_quality_score": {"p10": _pct(q, .1), "p50": _pct(q, .5), "p90": _pct(q, .9)},
            "note": "aHash is cheap but brittle; pHash survives resize/recompression/blur; ORB local-feature matching is what catches crops. "
                    "Calibrate the thresholds on these numbers; Copydays are holiday photos, so check false matches again on real product photos."}


def _run_scenarios():
    """Process every labelled scenario claim through the real graph and score it against the (synthetic) labels."""
    labels = db.q("SELECT * FROM eval_labels WHERE entity_type='claim' ORDER BY id")
    for cid in [l["entity_id"] for l in labels]:  # make evaluation re-runnable
        for t in ("review_cases", "action_log", "risk_signals", "evidence"):
            db.run(f"DELETE FROM {t} WHERE claim_id=:c", c=cid)
        db.run("DELETE FROM refund_requests WHERE id=:c", c=cid)
    db.run("DELETE FROM refund_history WHERE status='refunded' AND id NOT LIKE 'SYN-RF-%' AND customer_id LIKE 'SYN-%'")
    db.run("DELETE FROM return_events WHERE customer_id LIKE 'SYN-%' AND id NOT LIKE 'SYN-RET-%'")
    specs = sorted([(json.loads(l["note"]), l) for l in labels], key=lambda x: x[0].get("order", 0))
    recs = []
    for spec, l in specs:
        out = process_claim(spec["claim"], _image_bytes(spec["image"]))
        if l["label"] == "original_photo_owner":
            continue  # only seeds the evidence store; not scored
        reuse = any(f.startswith("evidence_") and "other_customer" in f for f in out["flags"])
        recs.append({"scenario": l["scenario"], "abusive": int(spec.get("abusive", 0)), "decision": out["decision"],
                     "patterns": out["abuse_patterns"], "reuse_flag": reuse, "expect_pattern": spec.get("expect_pattern"),
                     "variant": spec.get("variant"), "methods": out.get("match_methods", []), "cx_priority": out.get("cx_priority", False)})
    return recs


def _binary(recs, pred):
    tp = sum(1 for r in recs if r["abusive"] and pred(r))
    fn = sum(1 for r in recs if r["abusive"] and not pred(r))
    fp = sum(1 for r in recs if not r["abusive"] and pred(r))
    tn = sum(1 for r in recs if not r["abusive"] and not pred(r))
    prec, rec = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": round(prec, 3), "recall": round(rec, 3),
            "false_positive_rate": round(fp / max(fp + tn, 1), 3), "f1": round(2 * prec * rec / max(prec + rec, 1e-9), 3)}


def _profile_summary(recs):
    out = {}
    for scn in dict.fromkeys(r["scenario"] for r in recs):
        rs = [r for r in recs if r["scenario"] == scn]
        n = len(rs)
        row = {"abusive": rs[0]["abusive"], "n": n, "intervention_rate": round(sum(r["decision"] != "auto_approve" for r in rs) / n, 3),
               "any_pattern_flag_rate": round(sum(bool(r["patterns"]) or r["reuse_flag"] for r in rs) / n, 3)}
        pat = rs[0]["expect_pattern"]
        if pat:
            row["target_pattern"] = pat
            row["target_pattern_recall"] = round(sum(pat in r["patterns"] for r in rs) / n, 3)
        if scn == "evidence_reuse_b" or any(r["variant"] for r in rs):
            row["reuse_flag_recall"] = round(sum(r["reuse_flag"] for r in rs) / n, 3)
            by = {}
            for r in rs:
                d = by.setdefault(r["variant"], [0, 0])
                d[0] += 1
                d[1] += int(r["reuse_flag"])
            row["recall_by_transformation"] = {k: round(v[1] / v[0], 3) for k, v in sorted(by.items())}
            row["detected_by_orb_only"] = round(sum(r["reuse_flag"] and r["methods"] == ["orb"] for r in rs) / n, 3)
        out[scn] = row
    return out


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
    legit = [r for r in base if not r["abusive"]]
    fp_by_pattern = {}
    for scn in dict.fromkeys(r["scenario"] for r in legit):
        rs = [r for r in legit if r["scenario"] == scn]
        fp_by_pattern[scn] = {p: round(sum(p in r["patterns"] for r in rs) / len(rs), 3) for p in ai.ABUSE_PATTERNS}
        fp_by_pattern[scn]["any_reuse_flag"] = round(sum(r["reuse_flag"] for r in rs) / len(rs), 3)
    pattern_flag = _binary(base, lambda r: bool(r["patterns"]) or r["reuse_flag"])
    return {"status": "run", "data": "SYNTHETIC labelled customers drawn from independent behavioural profiles, real order attributes where available",
            "profiles": _profile_summary(base), "false_flags_on_legitimate_profiles": fp_by_pattern,
            "pattern_flag_as_classifier (any abuse pattern or evidence-reuse flag)": pattern_flag,
            "decision_as_classifier_vs_cost_scale": {f"handling/friction cost x{sc}": _binary(r, lambda x: x["decision"] != "auto_approve")
                                                      for sc, r in by_scale.items()},
            "note": "the generator describes HOW customers behave and never references a detection threshold, so the system can miss abusers "
                    "(recall < 1) and flag heavy genuine customers (false positives > 0). Still synthetic: it shows how the pipeline separates "
                    "profiles, not real-world fraud performance. Decisions depend on the cost matrix scale (handling 100, limit 300 here); see the "
                    "table by cost scale and set COST_SCALE for the deployment."}


def _prf(y_true, y_pred, labels):
    f1s = []
    for c in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == c and p == c)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != c and p == c)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == c and p != c)
        pr, rc = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
        f1s.append(2 * pr * rc / max(pr + rc, 1e-9))
    return round(sum(f1s) / len(f1s), 3)


LEGACY_KEYWORDS = {  # the original substring matcher (no negation, no paraphrase) - kept only as the baseline to beat
    "damaged": ["damaged", "broken", "cracked", "torn", "dent", "shattered", "scratch"],
    "defective": ["defective", "not working", "doesn't work", "does not work", "stopped working", "faulty", "malfunction"],
    "wrong_item": ["wrong item", "different item", "not what i ordered", "incorrect item"],
    "not_as_described": ["not as described", "not as shown", "looks different", "counterfeit", "fake"],
    "size_issue": ["too small", "too big", "doesn't fit", "does not fit"],
    "changed_mind": ["changed my mind", "don't like", "do not like", "no longer need", "don't want"],
    "not_delivered": ["not delivered", "never arrived", "didn't receive", "did not receive"]}


def _legacy(text):
    hits = [c for c, w in LEGACY_KEYWORDS.items() if any(x in text.lower() for x in w)]
    return hits, (hits[0] if len(hits) == 1 else "unclear")


def _eval_label_file(path):
    import csv
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    cats = sorted({r["label_category"] for r in rows})
    systems = {"legacy substring keywords": lambda t: _legacy(t)[1] if True else None,
               "rules (negation-aware, current default)": lambda t: (lambda a: a[0] if len(a) == 1 else "unclear")(ai.rule_categories(t)[0])}
    configured = ai._text_classifier()
    if configured:
        systems[f"configured backend ({ai._NLP['backend']})"] = lambda t: ai.classify_text_category(t)[0] or "unclear"
    out = {}
    for name, fn in systems.items():
        pred = [fn(r["text"]) for r in rows]
        gold = [r["label_category"] for r in rows]
        acc = lambda sel: round(sum(p == g for (p, g, r) in sel) / max(len(sel), 1), 3)
        trip = list(zip(pred, gold, rows))
        # does the text support the claimed reason? (claimed reason must be positively affirmed by the text)
        if name.startswith("legacy"):
            supports = [r["claimed_reason"] in _legacy(r["text"])[0] for r in rows]
        elif name.startswith("rules"):
            supports = [r["claimed_reason"] in ai.rule_categories(r["text"])[0] for r in rows]
        else:
            supports = [r["claimed_reason"] == (ai.classify_text_category(r["text"])[0]) for r in rows]
        truth = [r["text_supports_claim"] == "1" for r in rows]
        inc_tp = sum(1 for s, t in zip(supports, truth) if not s and not t)
        inc_fp = sum(1 for s, t in zip(supports, truth) if not s and t)
        inc_fn = sum(1 for s, t in zip(supports, truth) if s and not t)
        out[name] = {"category_accuracy": acc(trip), "category_macro_F1": _prf(gold, pred, cats),
                     "accuracy_negation_cases": acc([x for x in trip if x[2]["tag"] == "negation"]),
                     "accuracy_paraphrase_cases": acc([x for x in trip if x[2]["tag"] == "paraphrase"]),
                     "accuracy_hard_cases": acc([x for x in trip if x[2]["tag"] == "hard"]),
                     "unsupported-claim detection precision": round(inc_tp / max(inc_tp + inc_fp, 1), 3),
                     "unsupported-claim detection recall": round(inc_tp / max(inc_tp + inc_fn, 1), 3)}
    sent = [(ai.sentiment(r["text"])["label"], r["label_sentiment"]) for r in rows]
    neg = [(p, g) for p, g in sent if g == "negative"]
    return {"n": len(rows), "labels": sorted(cats), "systems": out,
            "sentiment": {"backend": ai.sentiment("x y")["backend"], "accuracy_3class": round(sum(p == g for p, g in sent) / len(sent), 3),
                          "recall_of_negative": round(sum(p == "negative" for p, g in neg) / max(len(neg), 1), 3)}}


def eval_nlp(labels_path=None):
    """Claim NLP + sentiment on the hand-labelled set (shipped, authored by the developer) and on the team's real-review labels if present."""
    paths = {"developer-authored set": labels_path or os.path.join("data", "manual_claims_labelled.csv"),
             "team labels of real reviews": os.path.join("data", "manual_labels_real.csv")}
    res, notes = {}, []
    for name, p in paths.items():
        if os.path.exists(p):
            r = _eval_label_file(p)
            res[name] = {"n": r["n"], **{f"{s} | {k}": v for s, d in r["systems"].items() for k, v in d.items()},
                         **{f"sentiment | {k}": v for k, v in r["sentiment"].items()}}
        else:
            notes.append(f"{name}: file {p} not found")
    if not res:
        return {"status": "not_run", "reason": "; ".join(notes)}
    return {"status": "run", "data": "hand-labelled claim texts (see data/README.md for who labelled them)", "sets": res,
            "claim_nlp_backend_active": ai._NLP["backend"] if ai._text_classifier() else "rules (transformer not enabled / not loadable)",
            "note": "the developer-authored set is a regression/sanity set, not an independent benchmark (the same person wrote the rules). "
                    "A transformer backend is evaluated here only when CLAIM_NLP=transformer loads; text sentiment is English-only with the default lexicon. "
                    + " ".join(notes)}


def eval_olist_reviews():
    """Real Olist reviews linked to real orders: link integrity, score distribution, product/vendor review outliers (no refunds in Olist)."""
    rows = db.q("SELECT rv.rating, rv.review_text, rv.product_ref, o.id AS oid FROM source_reviews rv LEFT JOIN orders o ON o.id=rv.product_ref "
                "WHERE rv.source='olist'")
    if not rows:
        return {"status": "not_run", "reason": "Olist reviews not ingested (needs olist_order_reviews_dataset.csv next to the other Olist files)"}
    dist = {}
    for r in rows:
        dist[int(r["rating"])] = dist.get(int(r["rating"]), 0) + 1
    low = [r for r in rows if r["rating"] <= 2]
    out = {"status": "run", "data": "Olist order reviews (real text and scores) linked to real Olist orders", "reviews": len(rows),
           "linked_to_orders": sum(1 for r in rows if r["oid"]), "with_comment_text": sum(1 for r in rows if (r["review_text"] or "").strip()),
           "score_distribution": {str(k): v for k, v in sorted(dist.items())}, "share_1_2_stars": round(len(low) / len(rows), 3)}
    prods = [p for p in ai.product_intelligence() if p["review_outlier"]][:5]
    vends = [v for v in ai.vendor_intelligence() if v["review_outlier"]][:5]
    out["product_negative_review_outliers"] = {p["id"]: {"reviews": p["review_count"], "negative_review_rate": p["negative_review_rate"],
                                                         "avg_score": p["avg_review_score"]} for p in prods}
    out["vendor_negative_review_outliers"] = {v["id"]: {"reviews": v["review_count"], "negative_review_rate": v["negative_review_rate"],
                                                        "avg_score": v["avg_review_score"]} for v in vends}
    out["note"] = ("Olist review text is Portuguese: the default sentiment lexicon and rule classifier are English-only, so text-level NLP is NOT "
                   "evaluated on it (set SENTIMENT_MODEL / CLAIM_NLP_MODEL to multilingual models to do so). Review scores are real and are used for "
                   "the product/vendor insights. A low review is not a refund claim, and it is never used as a fraud signal.")
    return out


def eval_amazon(image_dir=None):
    rows = db.q("SELECT rating, review_text, n_images FROM source_reviews WHERE source='amazon_reviews_2023'")
    if not rows:
        return {"status": "not_run", "reason": "Amazon reviews not ingested"}
    low = [r for r in rows if r["rating"] is not None and r["rating"] <= 2]
    defect = [r for r in low if any(w in (r["review_text"] or "").lower() for w in DEFECT_WORDS)]
    texted = [r for r in rows if (r["review_text"] or "").strip() and r["rating"] is not None and (r["rating"] <= 2 or r["rating"] >= 4)]
    if texted:
        hit = sum((ai.sentiment(r["review_text"])["score"] < 0) == (r["rating"] <= 2) for r in texted)
        sent_eval = {"reviews_used": len(texted), "sign_agrees_with_rating": round(hit / len(texted), 3), "backend": ai.sentiment("x y")["backend"],
                     "label": "WEAK label: star rating (<=2 negative, >=4 positive); not a human sentiment label"}
    else:
        sent_eval = None
    out = {"status": "run", "data": "Amazon Reviews 2023 (REVIEWS, not refund claims) - text/rating proxy", "sentiment_vs_rating": sent_eval,
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
               "claim_nlp_on_labelled_set": eval_nlp(), "olist_reviews_real_orders": eval_olist_reviews(),
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
              "abuse_scenarios_synthetic": "4. Synthetic customers from independent behavioural profiles (SYNTHETIC labels)",
              "claim_nlp_on_labelled_set": "5. Claim NLP + sentiment on the hand-labelled set",
              "olist_reviews_real_orders": "6. Olist reviews linked to real orders",
              "amazon_text_proxy": "7. Amazon Reviews 2023 text/rating proxy"}
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
    ("Claim NLP (negation / paraphrase) + sentiment", "hand-labelled claim set (data/manual_claims_labelled.csv; team labels of real Olist/Amazon reviews when added)"),
    ("Product / vendor customer-experience insights", "Olist order reviews linked to real orders (scores are real; Portuguese text not NLP-evaluated by default)"),
    ("Claim text proxy", "Amazon Reviews 2023 text and ratings as a weak-label proxy only"),
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


def write_labelling_sheet(n=200, seed=7, path=os.path.join("data", "manual_label_sheet.csv")):
    """Random real reviews (Olist + Amazon) with BLANK label columns, for two teammates to label -> data/manual_labels_real.csv."""
    import csv
    rows = db.q("SELECT id, source, review_text, rating FROM source_reviews WHERE review_text IS NOT NULL AND TRIM(review_text) <> ''")
    random.Random(seed).shuffle(rows)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "text", "claimed_reason", "label_category", "text_supports_claim", "label_sentiment", "tag", "source", "rating"])
        for r in rows[:n]:
            w.writerow([r["id"], r["review_text"].replace("\n", " "), "", "", "", "", "", r["source"], r["rating"]])
    return path


# =========================================================================== CLI
def cli(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["ingest", "scenarios", "evaluate", "manifest", "fetch-amazon-images", "labelling-sheet", "all"])
    ap.add_argument("--olist"), ap.add_argument("--uci"), ap.add_argument("--amazon")
    ap.add_argument("--copydays-originals"), ap.add_argument("--copydays-variants")
    ap.add_argument("--key-len", type=int, default=6, help="chars of the file stem that pair a Copydays variant with its original")
    ap.add_argument("--olist-limit", type=int, default=20000), ap.add_argument("--uci-customers", type=int, default=3000)
    ap.add_argument("--amazon-limit", type=int, default=5000), ap.add_argument("--n", type=int, default=40, help="synthetic customers per profile (scenarios) / rows (labelling-sheet)")
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
    if a.cmd == "labelling-sheet":
        print("wrote", write_labelling_sheet(a.n, a.seed))
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
