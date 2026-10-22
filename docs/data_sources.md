# Data sources for the composite evaluation dataset

Status key: **verified** = checked against the official page while preparing this document; **to verify** = could not be confirmed
and must be checked on the official page before the final report cites it.

> Raw data is never committed (`data/raw/` is git-ignored). `python scripts/get_datasets.py --all` downloads everything reproducibly, records
> file, URL, size, SHA-256 and UTC download time in `data/raw/download_record.json`, and `python eval_data.py manifest` copies that table plus the
> actual row counts, missing-value checks and source-to-schema mapping into `dataset_manifest.md`.
> `python scripts/run_full_pipeline.py --require-real` refuses to run the final evaluation unless all four real datasets are present.
> URLs and licence statements below were re-checked against the official pages on **2026-10-21** (marked *checked 2026-10-21*).

## 1. Olist - Brazilian E-Commerce Public Dataset
| | |
|---|---|
| Official source | https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce (Kaggle account needed to download) |
| What it is | ~100k anonymised orders from a Brazilian marketplace, 2016-2018 (**verified**) |
| Download | Kaggle login required: `kaggle datasets download -d olistbr/brazilian-ecommerce` (needs an API token) or the browser download, then `python scripts/get_datasets.py --olist-zip FILE`. Not fetchable anonymously |
| Licence / access | **CC-BY-NC-SA-4.0** as reported by the Kaggle CLI when the dataset was downloaded on 2026-10-21: attribution required, **non-commercial**, share-alike. Fine for coursework / research; do not use commercially. This repo does not redistribute the data |
| Citation | Olist; dataset as published on Kaggle (cite the Kaggle URL and the version/date you downloaded) |
| Files used | `olist_orders_dataset.csv`, `olist_order_items_dataset.csv`, `olist_customers_dataset.csv`, `olist_products_dataset.csv`, `product_category_name_translation.csv`, **`olist_order_reviews_dataset.csv`** (nine CSVs exist; the file names are **verified**, the column layout is as documented on Kaggle - **to verify** against your copy) |
| Fields extracted | orders: `order_id, customer_id, order_status, order_purchase_timestamp, order_delivered_customer_date`; items: `order_id, order_item_id, product_id, seller_id, price`; customers: `customer_id, customer_unique_id`; products: `product_id, product_category_name`; **reviews: `review_id, order_id, review_score, review_comment_title, review_comment_message, review_creation_date`** |
| Mapping | `customer_unique_id`→`customers.id`; `order_id`→`orders.id`; `seller_id`→`vendor_id`; category (English via translation file) →`category`; sum of item `price` (freight excluded)→`amount`; `order_delivered_customer_date`→`delivered_at`; **review → `source_reviews` (`product_ref` = the real `order_id`, `rating` = score, text kept as is)** |
| Transformations | drop orders with no purchase timestamp / no customer match / no items; optional random sample (`--olist-limit`, seed 7); `canceled`/`unavailable`→`canceled`, other non-delivered→`in_transit`; missing category→`unknown`; **only reviews whose order was loaded are kept (the rest are counted); reviews without comment text are kept for their score** |
| Limitations | **no refunds, no fraud labels**; **review text is Portuguese (the default English NLP/sentiment is not evaluated on it) and a bad review is not a refund claim**; most customers buy once, so behavioural history is thin; policy window / auto-approve limit are **assumptions** (provenance `assumed`) |
| Used for | deterministic policy gate on real timestamps; real order attributes cloned into synthetic scenarios; product/vendor intelligence **including negative-review outliers from the real reviews** |

## 2. UCI Online Retail II
| | |
|---|---|
| Official source | https://archive.ics.uci.edu/dataset/502/online+retail+ii - DOI https://doi.org/10.24432/C5CG6D (**verified**) |
| Download | http://archive.ics.uci.edu/static/public/502/online+retail+ii.zip -> `online_retail_II.xlsx` (43.5 MB, two year sheets) - *checked 2026-10-21* |
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
| Download | `https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/{Category}.jsonl.gz`; the script fetches `All_Beauty.jsonl.gz` (any category works, e.g. `Electronics`, `Home_and_Kitchen` for physical goods) - *checked 2026-10-21* |
| Licence | the official page states **no licence or terms** (only "Copyright 2024, McAuley Lab" and a citation request) - *checked 2026-10-21*. Treat as research-use only and say so in the report |
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
| Download | all at `http://pascal.inrialpes.fr/data2/holidays/`: `copydays_original.tar.gz` (208 MB), `copydays_jpeg.tar.gz` (77 MB; scale 1/16 + JPEG quality 75...3), `copydays_strong.tar.gz` (60 MB; 229 print-and-scan / blur / paint edits), `copydays_crop.tar.gz` (**2.0 GB**; crops removing 10-80 % of the surface) - *checked 2026-10-21*. `--skip-copydays-crop` gives a documented subset |
| Licence | "INRIA is the copyright holder of all the images included in the dataset"; provided "as is" - *checked 2026-10-21*. Research evaluation only; never redistribute the images |
| Citation | the Copydays section has no citation of its own; the page asks Holidays users to cite Jegou, H., Douze, M., Schmid, C. (2008), *Hamming Embedding and Weak geometry consistency for large scale image search*, Proc. 10th ECCV - cite that |
| Files used | the originals archive and the JPEG / crop / strong variant archives (extracted to `data/raw/copydays/original` and `data/raw/copydays/variants/{jpeg,crop,strong}`; the first folder name is the transformation type in the report) |
| Mapping | not loaded into the database; read from disk. Ground truth = benchmark pairing of each variant with its original (matched by file-stem prefix, `--key-len`, **check the pairing on your copy**) |
| Limitations | holiday photos, not product-damage photos; "strong" edits are far harder than what a typical reuser does |
| Used for | evidence-reuse recall / false-match rate by transformation type (`eval_copydays`), and threshold tuning on a **validation split of the originals** with the reported numbers taken from the **held-out test split** (`eval_copydays_split`) |
*(Alternative if you prefer a larger benchmark: the Image Similarity Challenge dataset, DISC21. Copydays was chosen because it is small enough to run locally.)*

## 5. Hand-labelled claim set (`data/manual_claims_labelled.csv`)
60 claim sentences labelled with reason category, whether the text supports the claimed reason, and sentiment, covering plain, paraphrased, negated, vague and
deliberately hard cases. **Authored and labelled by the developer who also wrote the rules**: a regression/sanity set, not an independent benchmark. For an
independent test, two teammates label real reviews with the workflow in `docs/labelling_guidelines.md` (`labelling-sheet` -> `label_tools.py split` ->
independent labelling -> `reconcile` (Cohen's kappa) -> `finalize` -> `data/manual_labels_real.csv`); the evaluation reports it separately.

## 6. Synthetic customers (generated, not a data source)
`python eval_data.py scenarios --seed 7 --n 40`: customers are drawn from **behavioural profiles** (normal, heavy-but-genuine, serial refunder, refund farmer,
policy exploiter, return abuser) described by refunds per year (Poisson), how late in the return window they file (Beta), how close to the auto-approve limit
they stay, and how often they skip the return, plus evidence re-uploads with randomly drawn edit strength (JPEG / resize / crop / blur / strong). The generator
**never references a detection threshold** (a test enforces it), so abusers overlap with genuine heavy users and the system can miss some and flag some: recall
below 100% and false positives above 0 are expected. Every row is `SYN-*`, provenance `synthetic_generator`, label origin `synthetic`; labels live only in
`eval_labels`. Still synthetic: it shows how the pipeline separates profiles, not real-world fraud performance.

## Download record
Generated, not hand-typed: `python scripts/get_datasets.py ...` writes `data/raw/download_record.json` (source, file, URL, bytes, SHA-256, UTC time) and
`python eval_data.py manifest` copies it into `docs/dataset_manifest.md` under "Download record". Commit the manifest, not the raw files. Record the dataset
*version* shown on each page (Kaggle "Version N", UCI "Last updated", Amazon release 2023) in the table below when you download.

| Source | Version / date shown on the page | Notes |
|---|---|---|
| Olist | | Kaggle version number and licence field as displayed |
| UCI Online Retail II | | UCI shows the donation date (2019) |
| Amazon Reviews 2023 | 2023 release | category file(s) used: |
| Copydays | | subset used (all variants, or `--skip-copydays-crop`): |

## Precautions applied in code
1. Cancellations are never stored as confirmed refunds (`status='cancellation_proxy'`) and never labelled as fraud.
2. Review/catalogue images never enter `evidence`; they are not refund evidence.
3. Synthetic labels exist only for `SYN-*` entities in `eval_labels`; source-provided rows are never labelled.
4. Every loaded row has a `data_provenance` row (`source`, `source_file`, `source_id`, `label_origin` = `source` | `assumed` | `synthetic`).
5. The seeded demo database (`refund_intel.db`) is separate from the evaluation database (`eval_data.db`).
