# Data sources for the composite evaluation dataset

Status key: **verified** = checked against the official page while preparing this document; **to verify** = could not be confirmed
and must be checked on the official page before the final report cites it.

> Nothing here is downloaded by the repository. Download the files yourself, then run `python eval_data.py ingest ...`
> (see README). Counts, missing values and cleaning decisions are generated into `dataset_manifest.md` from what was actually loaded.

## 1. Olist - Brazilian E-Commerce Public Dataset
| | |
|---|---|
| Official source | https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce (Kaggle account needed to download) |
| What it is | ~100k anonymised orders from a Brazilian marketplace, 2016-2018 (**verified**) |
| Licence / access | **to verify** on the Kaggle page (not stated in the pages I could read). Check commercial/derivative terms before redistributing any data - this repo does not redistribute it |
| Citation | Olist; dataset as published on Kaggle (cite the Kaggle URL and the version/date you downloaded) |
| Files used | `olist_orders_dataset.csv`, `olist_order_items_dataset.csv`, `olist_customers_dataset.csv`, `olist_products_dataset.csv`, `product_category_name_translation.csv` (nine CSVs exist; the file names are **verified**, the column layout is as documented on Kaggle - **to verify** against your copy) |
| Fields extracted | orders: `order_id, customer_id, order_status, order_purchase_timestamp, order_delivered_customer_date`; items: `order_id, order_item_id, product_id, seller_id, price`; customers: `customer_id, customer_unique_id`; products: `product_id, product_category_name` |
| Mapping | `customer_unique_id`→`customers.id`; `order_id`→`orders.id`; `seller_id`→`vendor_id`; category (English via translation file) →`category`; sum of item `price` (freight excluded)→`amount`; `order_delivered_customer_date`→`delivered_at` |
| Transformations | drop orders with no purchase timestamp / no customer match / no items; optional random sample (`--olist-limit`, seed 7); `canceled`/`unavailable`→`canceled`, other non-delivered→`in_transit`; missing category→`unknown` |
| Limitations | **no refunds, no fraud labels**; most customers buy once, so behavioural history is thin; policy window / auto-approve limit are **assumptions** (provenance `assumed`) |
| Used for | deterministic policy gate on real timestamps; real order attributes cloned into synthetic scenarios; product/vendor intelligence |

## 2. UCI Online Retail II
| | |
|---|---|
| Official source | https://archive.ics.uci.edu/dataset/502/online+retail+ii - DOI https://doi.org/10.24432/C5CG6D (**verified**) |
| What it is | all transactions of a UK-based online gift-ware retailer, 2009-12-01 to 2011-12-09; 1,067,371 instances; many customers are wholesalers; has missing values (**verified**) |
| Licence | CC BY 4.0 (**verified**) - attribute: Chen, D. (2012), Online Retail II [Dataset], UCI Machine Learning Repository, https://doi.org/10.24432/C5CG6D |
| File used | the downloaded spreadsheet/CSV (`.xlsx` with two year sheets, or a CSV export; both column spellings `Invoice`/`InvoiceNo`, `Price`/`UnitPrice`, `Customer ID`/`CustomerID` are handled) |
| Fields extracted | `Invoice, StockCode, Description, Quantity, InvoiceDate, Price, Customer ID` |
| Mapping | normal invoices→`orders` (status `invoiced`); **`C` invoices→`refund_history` with status `cancellation_proxy`** plus a pseudo-order (status `cancellation_invoice`, excluded from order counts) |
| Transformations | drop rows with no Customer ID/date; drop `A` (bad-debt adjustment) invoices; drop non-product stock codes (anything not starting with 5 digits: POST, M, D, BANK CHARGES...); drop non-positive quantity/price on sales; optional customer sample (`--uci-customers`, seed 7) |
| Limitations | **a cancellation is not a confirmed refund and not fraud** - it is only a return-like event; no delivery date (so window/late-filing logic cannot be exercised); no return reasons; wholesale behaviour differs from consumer refunds |
| Used for | behavioural features / abuse-rule **flag rates** on real return-like behaviour (unlabelled) |

## 3. Amazon Reviews 2023 (McAuley Lab)
| | |
|---|---|
| Official source | https://amazon-reviews-2023.github.io/ ; Hugging Face `McAuley-Lab/Amazon-Reviews-2023` (**verified**) |
| What it is | 571.5M reviews with ratings, text, helpful votes, item metadata and review images in small/medium/large URL variants (**verified**). The Hugging Face loading script is no longer supported by the viewer, so download the raw per-category review JSONL files instead (**to verify** exact file path/format) |
| Licence | **to verify** on the official site before citing/using beyond research |
| Citation | Hou, Y., Li, J., He, Z., Yan, A., Chen, X., McAuley, J. (2024), *Bridging Language and Items for Retrieval and Recommendation*, arXiv:2403.03952 (**verified**) |
| File used | one category review file, e.g. a `*.jsonl` / `*.jsonl.gz` of reviews (choose a category with physical products) |
| Fields extracted | `rating, title, text, images (URLs), asin/parent_asin, user_id, timestamp` |
| Mapping | `source_reviews` table only (text, rating, image URLs, provenance) |
| Limitations | these are **reviews, not refund claims**; review/catalogue images are **not photographs uploaded as refund evidence** and are never written to the `evidence` table; images are not downloaded unless you run `fetch-amazon-images` |
| Used for | text/rating proxy for the Evidence & Claim agent; optional image-quality distribution |

## 4. Image-similarity benchmark: INRIA Copydays
| | |
|---|---|
| Official source | https://lear.inrialpes.fr/~jegou/data.php (**verified**) |
| What it is | 157 original holiday photos, each attacked by JPEG recompression, cropping and "strong" manual edits (**verified**; counts as reported in the literature) |
| Licence | **to verify**: INRIA states it is the copyright holder of the images; the page I read gave no open licence. Use for research evaluation only unless INRIA confirms otherwise |
| Citation | Jegou, H., Douze, M., Schmid, C. (2008), *Hamming embedding and weak geometric consistency for large scale image search*, ECCV (**to verify** exact reference on the page) |
| Files used | the originals archive and the JPEG / crop / strong variant archives |
| Mapping | not loaded into the database; read from disk. Ground truth = benchmark pairing of each variant with its original (matched by file-stem prefix, `--key-len`, **check the pairing on your copy**) |
| Limitations | holiday photos, not product-damage photos; "strong" edits are far harder than what a typical reuser does |
| Used for | evidence-reuse recall / false-match rate of the hash-similarity check |
*(Alternative if you prefer a larger benchmark: the Image Similarity Challenge dataset, DISC21. Copydays was chosen because it is small enough to run locally.)*

## 5. Synthetic labelled abuse scenarios (generated, not a data source)
`python eval_data.py scenarios --seed 7 --n 30`: serial refunders, refund farmers, policy exploiters, evidence re-uploads (JPEG / resize /
crop / blur), legitimate controls and near-threshold controls. Every row is `SYN-*`, provenance `synthetic_generator`, label origin
`synthetic`; labels live only in `eval_labels`. The generator is aligned with the detection rules by construction, so scenario results verify
the pipeline end to end - they are **not** an estimate of real-world fraud detection performance.

## Download record (fill in when you download)
| Source | Downloaded on | Version / date shown | File | SHA-256 (`sha256sum file`) |
|---|---|---|---|---|
| Olist | | | | |
| UCI Online Retail II | | | | |
| Amazon Reviews 2023 | | | | |
| Copydays | | | | |

## Precautions applied in code
1. Cancellations are never stored as confirmed refunds (`status='cancellation_proxy'`) and never labelled as fraud.
2. Review/catalogue images never enter `evidence`; they are not refund evidence.
3. Synthetic labels exist only for `SYN-*` entities in `eval_labels`; source-provided rows are never labelled.
4. Every loaded row has a `data_provenance` row (`source`, `source_file`, `source_id`, `label_origin` = `source` | `assumed` | `synthetic`).
5. The seeded demo database (`refund_intel.db`) is separate from the evaluation database (`eval_data.db`).
