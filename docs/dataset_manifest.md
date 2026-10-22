# Dataset manifest (auto-generated)

Generated 2026-10-22T07:42:56 by `python eval_data.py manifest` from `reports\ingest_log.json` and the evaluation database. Counts are what was ACTUALLY loaded.


## olist

- ingested at: 2026-10-22T07:31:30
- **files**: {"olist_orders_dataset.csv": 99441, "olist_order_items_dataset.csv": 112650, "olist_customers_dataset.csv": 99441, "olist_products_dataset.csv": 32951, "olist_order_reviews_dataset.csv": 99224}
- **missing_values_in_raw**: {"orders": {"order_purchase_timestamp": 0, "order_delivered_customer_date": 2965}, "products": {"product_category_name": 610}, "reviews": {"review_score": 0, "review_comment_message": 58247}}
- **loaded**: {"customers": 95420, "orders": 98666, "refund_history": 0, "reviews": 98465}
- **cleaning**: {"dropped_no_purchase_timestamp": 0, "dropped_no_customer_match": 0, "dropped_orders_without_items": 775, "sampling": "all orders loaded", "products_without_category_set_to_unknown": 1416, "reviews_not_linked_to_a_loaded_order": 759, "review_link_audit": {"reviews_in_raw_file": 99224, "linked_and_loaded": 98465, "order_id_absent_from_raw_orders_file (join impossible)": 0, "order_exists_but_not_loaded (cleaning or --olist-limit sampling)": 759, "  of which order removed by cleaning (e.g. order has no items)": 759, "  of which order removed by --olist-limit sampling": 0, "reviews_without_score_dropped": 0, "orders_in_raw_reviews_with_more_than_one_review": 547}, "reviews_loaded_with_empty_comment": 58039}
- **assumptions**: 
  - policy per category: window 7 days, auto-approve limit 300.0 (BRL) - NOT in the dataset
- **notes**: 
  - customer id = customer_unique_id; customer.created_at = first purchase INSIDE the loaded sample
  - amount = sum of item prices (freight excluded); product/vendor = first item of the order
  - no refund or cancellation is converted into a refund; Olist order_status 'canceled' stays an order status
  - reviews are real customer text + 1-5 scores linked to real orders (Portuguese); a bad review is NOT a refund claim and NOT a fraud signal

## uci

- ingested at: 2026-10-22T07:33:20
- **files**: {"online_retail_II.xlsx": 1067371}
- **missing_values_in_raw**: {"customer": 243007, "desc": 4382, "price": 0, "qty": 0, "date": 0}
- **loaded**: {"customers": 3000, "orders": 19523, "refund_history(cancellation_proxy)": 3902, "cancellation_pseudo_orders": 3902}
- **cleaning**: {"dropped_no_customer_id_or_date": 243007, "dropped_adjust_bad_debt_invoices": 0, "dropped_non_product_stock_codes": 3738, "dropped_non_positive_qty_or_price_on_sales": 61, "sampling": "random sample of 3000 customers (all their invoices), seed 7"}
- **assumptions**: 
  - policy for category 'retail': window 14 days, auto-approve limit 150.0 (GBP) - NOT in the dataset
  - no delivery date exists: orders.delivered_at is NULL, so window/late-filing logic is not exercised on UCI
- **notes**: 
  - C-prefixed invoices are cancellations/credit notes: stored as refund_history rows with status 'cancellation_proxy', NEVER as confirmed refunds, and NEVER labelled fraud
  - each cancellation also gets a pseudo-order (status 'cancellation_invoice', excluded from order counts) so joins work

## amazon_reviews_2023

- ingested at: 2026-10-22T07:33:20
- **files**: {"All_Beauty.jsonl.gz": 5000}
- **loaded**: {"source_reviews": 5000, "reviews_with_images": 599}
- **cleaning**: {"limit_applied": 5000, "reviews_with_empty_text_kept_flagged": 1}
- **notes**: 
  - review images are customer REVIEW content, not refund evidence: only image URLs are stored, nothing enters `evidence`
  - used only as a text/rating proxy for the Evidence & Claim agent and (optionally) for image-quality distribution

## synthetic_scenarios

- ingested at: 2026-10-22T07:33:36
- **assumptions**: 
  - policy window/limit for synthetic orders: (7, 300.0)
  - customers are drawn from behavioural PROFILES that do not reference any detection threshold; abusers overlap with heavy genuine customers, so recall < 100% and false positives > 0 are expected
  - labels are synthetic: they say how the generator made the customer, not what real fraud looks like

## Rows in the unified schema (by provenance)

| source | table | label_origin | rows |
|---|---|---|---|
| amazon_reviews_2023 | source_reviews | source | 5000 |
| assumed_policy | policies | assumed | 75 |
| olist | customers | source | 95420 |
| olist | orders | source | 98666 |
| olist | source_reviews | source | 98465 |
| synthetic_generator | customers | synthetic | 160 |
| synthetic_generator | orders | synthetic | 2379 |
| synthetic_generator | refund_history | synthetic | 599 |
| uci | customers | source | 3000 |
| uci | orders | source | 23425 |
| uci | refund_history | source | 3902 |

## Missing-value checks in the unified schema (actual rows loaded)

| table.column | rows | NULL / empty | share |
|---|---|---|---|
| customers.created_at | 98580 | 0 | 0.0% |
| orders.customer_id | 124470 | 0 | 0.0% |
| orders.category | 124470 | 0 | 0.0% |
| orders.vendor_id | 124470 | 0 | 0.0% |
| orders.amount | 124470 | 0 | 0.0% |
| orders.order_date | 124470 | 0 | 0.0% |
| orders.delivered_at | 124470 | 25615 | 20.6% |
| refund_history.reason | 4578 | 0 | 0.0% |
| refund_history.created_at | 4578 | 0 | 0.0% |
| source_reviews.rating | 103465 | 0 | 0.0% |
| source_reviews.review_text | 103465 | 58022 | 56.1% |
| source_reviews.product_ref | 103465 | 0 | 0.0% |

## Source-to-schema mapping

| source field | unified column | note |
|---|---|---|
| Olist customers.customer_unique_id | customers.id | one person across several customer_id values |
| Olist orders.order_id | orders.id |  |
| Olist orders.order_purchase_timestamp / order_delivered_customer_date | orders.order_date / orders.delivered_at | missing delivery date stays NULL |
| Olist order_items.price (sum, freight excluded) | orders.amount |  |
| Olist order_items.seller_id / product_id (first item) | orders.vendor_id / orders.product_id |  |
| Olist products.product_category_name (+ English translation) | orders.category | missing -> 'unknown' |
| Olist order_reviews.order_id / review_score / review_comment_message | source_reviews.product_ref / rating / review_text | only reviews whose order was loaded |
| UCI Invoice (normal) | orders.id (status 'invoiced') |  |
| UCI Invoice starting with 'C' | refund_history (status 'cancellation_proxy') + pseudo-order | NOT a confirmed refund, NOT fraud |
| UCI Customer ID / InvoiceDate / Price x Quantity | customers.id / order_date / amount |  |
| Amazon rating / title / text / images | source_reviews.rating / title / review_text / image_urls | reviews, not refund claims; images not evidence |
| Copydays originals + variants | (not loaded; read from disk) | ground truth = file-stem pairing |

## Download record (written by `scripts/get_datasets.py`)

| source | file | bytes | SHA-256 | downloaded (UTC) | URL |
|---|---|---|---|---|---|
| Olist Brazilian E-Commerce | brazilian-ecommerce.zip | 44717580 | `967e41e04fc306fe604e2a693f488995a8b41e5047418f8a5c8e4abd6deca784` | 2026-10-21T15:59:28+00:00 | https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce |
| UCI Online Retail II | online_retail_ii.zip | 45622418 | `572e36277c2390fbfde10664750731e0a86f55e33470d91919085f0408e67bfb` | 2026-10-21T16:00:02+00:00 | http://archive.ics.uci.edu/static/public/502/online+retail+ii.zip |
| Amazon Reviews 2023 (All_Beauty) | All_Beauty.jsonl.gz | 94441517 | `ee00e66835567c3f12fde6a482f8d7055c22cac2d0924f677263affdd8a0e349` | 2026-10-21T16:00:38+00:00 | https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/All_Beauty.jsonl.gz |

## Source supporting each evaluation component

| component | data |
|---|---|
| Deterministic order + policy gate | Olist (real timestamps/categories/amounts); policy = ASSUMED |
| Behavioural features, abuse rules | UCI Online Retail II cancellations as return-like proxy; synthetic labelled abusers for recall |
| Evidence reuse / similarity | INRIA Copydays (ground-truth copies); synthetic re-upload scenarios |
| Claim NLP (negation / paraphrase) + sentiment | hand-labelled claim set (data/manual_claims_labelled.csv; team labels of real Olist/Amazon reviews when added) |
| Product / vendor customer-experience insights | Olist order reviews linked to real orders (scores are real; Portuguese text not NLP-evaluated by default) |
| Claim text proxy | Amazon Reviews 2023 text and ratings as a weak-label proxy only |
| Expected-cost decision, guardrails, end-to-end routing | Synthetic labelled scenarios + seeded demo data |
| Historical product/vendor intelligence | Olist sellers/products (real); UCI products |
