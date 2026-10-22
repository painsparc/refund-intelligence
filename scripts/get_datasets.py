"""
scripts/get_datasets.py  -  reproducible download of the evaluation datasets into data/raw/ (git-ignored; raw data is never committed)

    python scripts/get_datasets.py --list                       # what would be downloaded, from where, under which licence
    python scripts/get_datasets.py --only uci amazon copydays   # download a subset
    python scripts/get_datasets.py --all                        # everything (Copydays crop is ~2 GB)
    python scripts/get_datasets.py --all --skip-copydays-crop   # documented subset: originals + jpeg + strong variants only

Each download appends {source, file, url, bytes, sha256, downloaded_at} to data/raw/download_record.json; `python eval_data.py manifest`
copies that table into docs/dataset_manifest.md. URLs were read from the official pages on 2026-10-21:
  UCI Online Retail II : https://archive.ics.uci.edu/dataset/502/online+retail+ii   (CC BY 4.0)
  Amazon Reviews 2023  : https://amazon-reviews-2023.github.io/                     (no licence text on the page - check before use beyond research)
  INRIA Copydays       : https://lear.inrialpes.fr/~jegou/data.php                    (INRIA holds the copyright; research evaluation only)
  Olist                : https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce  (needs a free Kaggle account + API token, see below)

The script skips files already present in data/raw/_downloads/, tries http and https, retries, and carries on if one source fails (INRIA's server
is sometimes unreachable: download the archives in a browser from the official page, save them under data/raw/_downloads/ with the names in --list,
and re-run `--only copydays`).
Olist cannot be fetched anonymously. Either put a Kaggle API token in ~/.kaggle/kaggle.json (or KAGGLE_USERNAME / KAGGLE_KEY) and install
`pip install kaggle` - this script then runs `kaggle datasets download` - or download the zip in your browser and run
`python scripts/get_datasets.py --olist-zip path/to/brazilian-ecommerce.zip`.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from datetime import datetime, timezone

RAW = os.path.join("data", "raw")
RECORD = os.path.join(RAW, "download_record.json")
COPY = "http://pascal.inrialpes.fr/data2/holidays/"
SOURCES = {
    "uci": [("UCI Online Retail II", "http://archive.ics.uci.edu/static/public/502/online+retail+ii.zip", "online_retail_ii.zip")],
    "amazon": [("Amazon Reviews 2023 (All_Beauty)", "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/review_categories/All_Beauty.jsonl.gz",
                "All_Beauty.jsonl.gz")],
    "copydays": [("INRIA Copydays originals", COPY + "copydays_original.tar.gz", "copydays_original.tar.gz"),
                 ("INRIA Copydays jpeg", COPY + "copydays_jpeg.tar.gz", "copydays_jpeg.tar.gz"),
                 ("INRIA Copydays strong", COPY + "copydays_strong.tar.gz", "copydays_strong.tar.gz"),
                 ("INRIA Copydays crop", COPY + "copydays_crop.tar.gz", "copydays_crop.tar.gz")],
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def record(source, path, url):
    rows = json.load(open(RECORD)) if os.path.exists(RECORD) else []
    rows = [r for r in rows if not (r["source"] == source and r["file"] == os.path.basename(path))]
    rows.append({"source": source, "file": os.path.basename(path), "url": url, "bytes": os.path.getsize(path), "sha256": sha256(path),
                 "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    json.dump(rows, open(RECORD, "w"), indent=2)


def download(url, dest, tries=3):
    """Skip if the file is already there (e.g. downloaded manually into data/raw/_downloads/); else try the URL, its https/http twin, with retries."""
    import requests
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  {os.path.basename(dest)} already present ({os.path.getsize(dest) / 1e6:.0f} MB) - not downloading again")
        return url
    twin = ("https://" + url[len("http://"):]) if url.startswith("http://") else ("http://" + url[len("https://"):])
    last = None
    for u in (url, twin):
        for attempt in range(1, tries + 1):
            try:
                with requests.get(u, stream=True, timeout=(30, 120)) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length", 0))
                    done = 0
                    with open(dest + ".part", "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                print(f"\r  {os.path.basename(dest)} {done / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
                os.replace(dest + ".part", dest)
                print()
                return u
            except requests.RequestException as e:
                last = e
                print(f"\n  attempt {attempt}/{tries} on {u} failed: {type(e).__name__}")
                time.sleep(3)
    raise RuntimeError(f"could not download {url} ({type(last).__name__}). Download it in a browser from the official page and save it as {dest}, "
                       f"then re-run this script: it will skip the download and only extract + record it.") from last


def safe_extract_tar(archive, target):
    os.makedirs(target, exist_ok=True)
    root = os.path.realpath(target)
    with tarfile.open(archive) as tf:
        for m in tf.getmembers():
            if not os.path.realpath(os.path.join(target, m.name)).startswith(root + os.sep) or m.issym() or m.islnk():
                raise SystemExit(f"refusing unsafe archive member {m.name!r} in {archive}")
        tf.extractall(target)


def safe_extract_zip(archive, target):
    os.makedirs(target, exist_ok=True)
    root = os.path.realpath(target)
    with zipfile.ZipFile(archive) as zf:
        for n in zf.namelist():
            if not os.path.realpath(os.path.join(target, n)).startswith(root + os.sep) and n.rstrip("/"):
                raise SystemExit(f"refusing unsafe archive member {n!r} in {archive}")
        zf.extractall(target)


def get(group, skip_crop=False):
    for name, url, fn in SOURCES[group]:
        if skip_crop and "crop" in fn:
            print(f"skipping {name} (--skip-copydays-crop); document this subset in docs/data_sources.md")
            continue
        dest = os.path.join(RAW, "_downloads", fn)
        print(f"{name}\n  {url}")
        used = download(url, dest)
        record(name, dest, used)
        if group == "uci":
            safe_extract_zip(dest, RAW)
            print("  -> data/raw/online_retail_II.xlsx")
        elif group == "copydays":
            kind = fn.replace("copydays_", "").replace(".tar.gz", "")
            target = os.path.join(RAW, "copydays", "original" if kind == "original" else os.path.join("variants", kind))
            safe_extract_tar(dest, target)
            print(f"  -> {target}")
        else:
            shutil.copy(dest, os.path.join(RAW, fn))
            print(f"  -> data/raw/{fn}")


def get_olist(zip_path=None):
    dest = os.path.join(RAW, "_downloads", "brazilian-ecommerce.zip")
    if zip_path:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy(zip_path, dest)
    else:
        if not shutil.which("kaggle"):
            raise SystemExit("Olist needs the Kaggle CLI (pip install kaggle + API token) or --olist-zip PATH (download the zip from the Kaggle page)")
        subprocess.run(["kaggle", "datasets", "download", "-d", "olistbr/brazilian-ecommerce", "-p", os.path.dirname(dest)], check=True)
    safe_extract_zip(dest, os.path.join(RAW, "olist"))
    record("Olist Brazilian E-Commerce", dest, "https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce")
    print("  -> data/raw/olist/")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true"), ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", nargs="+", choices=["olist", "uci", "amazon", "copydays"])
    ap.add_argument("--olist-zip"), ap.add_argument("--skip-copydays-crop", action="store_true")
    a = ap.parse_args(argv)
    if a.list or not (a.all or a.only or a.olist_zip):
        for g, items in SOURCES.items():
            for name, url, _ in items:
                print(f"{g:9} {name:34} {url}")
        print("olist     Olist Brazilian E-Commerce         https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce (Kaggle login)")
        return 0
    groups = ["olist", "uci", "amazon", "copydays"] if a.all else (a.only or [])
    if a.olist_zip and "olist" not in groups:
        groups.append("olist")
    failed = {}
    for g in groups:                      # one failing source must not stop the others
        try:
            get_olist(a.olist_zip) if g == "olist" else get(g, a.skip_copydays_crop)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            failed[g] = str(e)
            print(f"\n!! {g} FAILED: {e}\n")
    print(f"download record: {RECORD}")
    if failed:
        print("FAILED sources: " + ", ".join(failed) + ". Fix and re-run with --only <source>; finished downloads are not repeated.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
