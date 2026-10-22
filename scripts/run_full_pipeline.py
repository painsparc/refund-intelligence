"""
scripts/run_full_pipeline.py  -  one command for the final integrated run; writes reports/final_test_results.md (+ .json)

    python scripts/run_full_pipeline.py                 # uses whatever is in data/raw/ and SAYS what was missing
    python scripts/run_full_pipeline.py --require-real  # exit 1 if any real dataset is absent (use this for the final report)
    python scripts/run_full_pipeline.py --skip-eval --skip-tests

Steps (each recorded as pass / fail / skipped with its evidence):
  1. dataset check          which real datasets are present under data/raw/ (python scripts/get_datasets.py fetches them)
  2. evaluation             python eval_data.py all ...  -> reports/eval_results.json, docs/eval_report.md, docs/dataset_manifest.md
  3. pytest -q              the whole test-suite
  4. live API workflow      a real uvicorn process + HTTP calls: normal claim, policy violation, additional-evidence request, repeat refunder,
                            reused image (manual review), human resolution, historical product/vendor loop (run -> apply -> evaluate)
  5. Streamlit              a real `streamlit run` process (health endpoint) + every page rendered with Streamlit's AppTest against the live API
The report states plainly whether the evaluation ran on REAL data or on nothing / fixtures.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
RAW = os.path.join("data", "raw")
DATASETS = {"olist": os.path.join(RAW, "olist", "olist_orders_dataset.csv"),
            "uci": os.path.join(RAW, "online_retail_II.xlsx"),
            "amazon": os.path.join(RAW, "All_Beauty.jsonl.gz"),
            "copydays": os.path.join(RAW, "copydays", "original")}
results = []


def record(name, ok, detail="", seconds=0.0):
    results.append({"step": name, "status": "pass" if ok is True else "fail" if ok is False else "skipped", "detail": detail, "seconds": round(seconds, 1)})
    print(f"[{results[-1]['status'].upper():7}] {name} - {detail[:160]}", flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url, secs=90, ok=lambda r: r.status_code == 200):
    import requests
    end = time.time() + secs
    while time.time() < end:
        try:
            if ok(requests.get(url, timeout=3)):
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def present():
    return {k: os.path.exists(p) for k, p in DATASETS.items()}


def dependency_check():
    """The evaluation silently falls back when optional packages are missing (VADER -> a tiny lexicon, OpenCV -> no ORB crop matching)."""
    import importlib.util
    need = {"vaderSentiment": "sentiment baseline (else a tiny fallback lexicon with much worse recall)", "cv2": "ORB local-feature matching for cropped images"}
    missing = [f"{m} ({why})" for m, why in need.items() if importlib.util.find_spec(m) is None]
    info = "transformers " + ("installed" if importlib.util.find_spec("transformers") else "not installed (optional)")
    record("dependency check", not missing, ("MISSING: " + "; ".join(missing) + " -> pip install -r requirements.txt" if missing else "vaderSentiment, cv2 present; ") + info)
    return not missing


def run_eval(have, fast=False):
    cmd = [sys.executable, "eval_data.py", "all"]
    if have["olist"]:
        cmd += ["--olist", os.path.join(RAW, "olist"), "--olist-limit", "0"]     # ALL orders, so every review whose join data exists is linked
    if have["uci"]:
        cmd += ["--uci", DATASETS["uci"]]
    if have["amazon"]:
        cmd += ["--amazon", DATASETS["amazon"]]
    if have["copydays"]:
        cmd += ["--copydays-originals", DATASETS["copydays"], "--copydays-variants", os.path.join(RAW, "copydays", "variants")]
    if fast:      # ~3x quicker: half the synthetic customers, 4 cost scales (the 3 that matter + the default)
        cmd += ["--n", "20", "--cost-scales", "1,0.2,0.1,0.05"]
    t = time.time()
    print("evaluation running - the full version takes ~25 min (UCI spreadsheet + synthetic claims x cost scales); --fast takes ~8 min", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    record("evaluation (eval_data.py all)", p.returncode == 0, " ".join(cmd[2:]) + " | " + (p.stdout.strip().splitlines() or [p.stderr[-300:]])[-1], time.time() - t)
    return p.returncode == 0


def run_pytest():
    t = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], capture_output=True, text=True)
    tail = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
    record("pytest -q", p.returncode == 0, tail, time.time() - t)
    if p.returncode != 0:
        print(p.stdout[-3000:])


def api_workflow():
    import requests
    from demo import make_image, resized_copy
    tmp = tempfile.mkdtemp()
    port = free_port()
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{tmp}/api.db", EVIDENCE_DIR=f"{tmp}/evidence")
    env.pop("LLM_API_KEY", None)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port), "--log-level", "warning"], env=env)
    base = f"http://127.0.0.1:{port}"
    checks = []
    t = time.time()

    def check(name, cond, extra=""):
        checks.append((name, bool(cond), extra))

    try:
        if not wait_http(base + "/docs"):
            return record("live API workflow", False, "uvicorn did not start"), None, None
        post = lambda o, c, r, a, d, img=None: requests.post(f"{base}/claims", data=dict(order_id=o, customer_id=c, reason=r, amount=a, description=d),
                                                              files={"image": ("e.jpg", img, "image/jpeg")} if img else None, timeout=120).json()
        serial_img = make_image(2)
        normal = post("ORD-1001", "C001", "damaged", 3500, "The jar arrived cracked at the base.", make_image(1))
        check("normal claim -> immediate refund", normal["decision"] == "auto_approve", normal["decision"])
        ask = post("ORD-1005", "C002", "damaged", 1800, "Stitching came apart on the sleeve.")
        check("weak evidence -> additional-evidence request", ask["decision"] == "request_evidence", ask["decision"])
        more = requests.post(f"{base}/claims/{ask['claim_id']}/evidence", files={"image": ("m.jpg", make_image(55), "image/jpeg")}, timeout=120).json()
        check("evidence supplied -> refund after return", more["decision"] == "refund_after_return", more["decision"])
        back = requests.post(f"{base}/claims/{ask['claim_id']}/return", json={"status": "received"}, timeout=60).json()
        check("return received -> refunded", back.get("claim_status") == "refunded", str(back.get("claim_status")))
        violation = post("ORD-1006", "C002", "not_as_described", 700, "Rice was not what the listing showed.", make_image(6))
        check("policy violation -> deterministic decline", violation["decision"] == "policy_decline", violation["decision"])
        serial = post("ORD-1002", "C003", "defective", 2890, "Sole came off after two days.", serial_img)
        check("repeat refunder -> manual review + serial_refunder", serial["decision"] == "manual_review" and "serial_refunder" in serial["abuse_patterns"],
              f"{serial['decision']} {serial['abuse_patterns']}")
        reused = post("ORD-1004", "C005", "damaged", 2400, "Earbuds case is broken.", resized_copy(serial_img))
        check("reused image -> manual review + reuse flag", reused["decision"] == "manual_review" and any(f.startswith("evidence_") for f in reused["flags"]),
              f"{reused['decision']} {reused['flags']}")
        queue = requests.get(f"{base}/reviews", timeout=30).json()
        ids = {q["claim_id"] for q in queue}
        check("manual-review queue holds both cases, not the decline", {serial["claim_id"], reused["claim_id"]} <= ids and violation["claim_id"] not in ids)
        done = requests.post(f"{base}/reviews/{serial['review_case_id']}/resolve", json={"outcome": "fraud", "note": "serial"}, timeout=30).json()
        check("human resolves a case", done.get("claim_status") == "rejected", str(done.get("claim_status")))
        run = requests.post(f"{base}/historical/run", timeout=120).json()
        check("historical loop run raises insights", bool(run.get("new_insights")), str(run.get("new_insights"))[:80])
        ins = requests.get(f"{base}/historical/insights", params={"status": "proposed"}, timeout=30).json()
        prod = next((i for i in ins if i["kind"] == "product"), None)
        if prod:
            ap = requests.post(f"{base}/historical/insights/{prod['id']}/apply", json={"note": "QC audit"}, timeout=30).json()
            ev = requests.post(f"{base}/historical/insights/{prod['id']}/evaluate", timeout=60).json()
            check("insight -> intervention -> outcome", ap.get("status") == "applied" and ev.get("status") == "evaluated", f"{ap.get('status')}/{ev.get('status')}")
        else:
            check("insight -> intervention -> outcome", False, "no product insight proposed")
        for kind in ("products", "vendors"):
            r = requests.get(f"{base}/historical/snapshots/{kind}", timeout=30)
            check(f"{kind} risk snapshots persisted", r.status_code == 200 and len(r.json()) > 0, str(r.status_code))
        for path in ("operations", "management", "products", "vendors", "policy", "abuse"):
            check(f"/insights/{path}", requests.get(f"{base}/insights/{path}", timeout=30).status_code == 200)
        bad = [c for c in checks if not c[1]]
        record("live API workflow (FastAPI, uvicorn)", not bad, f"{len(checks) - len(bad)}/{len(checks)} checks" + ("" if not bad else f"; failed: {[b[0] for b in bad]}"), time.time() - t)
        return checks, base, srv
    except Exception as e:  # noqa: BLE001
        record("live API workflow (FastAPI, uvicorn)", False, f"{type(e).__name__}: {e}", time.time() - t)
        return checks, base, srv


def streamlit_check(base):
    import requests  # noqa: F401
    t = time.time()
    port = free_port()
    env = dict(os.environ, API_URL=base)
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "streamlit_app.py", "--server.headless=true", f"--server.port={port}",
                             "--browser.gatherUsageStats=false"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        healthy = wait_http(f"http://127.0.0.1:{port}/_stcore/health", 90, lambda r: r.text.strip() == "ok")
        record("Streamlit process starts (health endpoint)", healthy, f"port {port}", time.time() - t)
    finally:
        proc.terminate()
    try:
        from streamlit.testing.v1 import AppTest
        os.environ["API_URL"] = base
        at = AppTest.from_file(os.path.join(ROOT, "streamlit_app.py"), default_timeout=90).run()
        pages = list(at.sidebar.radio[0].options)
        bad = []
        for pg in pages:
            at.sidebar.radio[0].set_value(pg).run()
            if at.exception or at.error:
                bad.append(f"{pg}: {[str(e.value)[:80] for e in list(at.exception) + list(at.error)]}")
        record("Streamlit pages render against the live API", not bad, f"{len(pages)} pages: {pages}" + (f"; problems: {bad}" if bad else ""), time.time() - t)
    except Exception as e:  # noqa: BLE001
        record("Streamlit pages render against the live API", False, f"{type(e).__name__}: {e}", time.time() - t)


def write_report(have):
    real = all(have.values())
    prev = os.path.join("reports", "final_test_results.json")
    if os.path.exists(prev):                       # merge with earlier partial runs (e.g. --skip-eval then --skip-live): newest result per step wins
        old = [r for r in json.load(open(prev)).get("steps", []) if r["step"] not in {x["step"] for x in results}]
        results[:0] = [r for r in old if r["step"] != "dataset check"]
    L = [f"# Final integrated test results (auto-generated)\n", f"Generated {datetime.utcnow().isoformat(timespec='seconds')}Z by `scripts/run_full_pipeline.py`.\n",
         "## Data used\n", "| dataset | present under data/raw |\n|---|---|", *[f"| {k} | {'yes' if v else '**NO**'} |" for k, v in have.items()],
         "", "**All four real datasets were present: the evaluation figures are real-data figures.**" if real else
         "**NOT ALL REAL DATASETS WERE PRESENT: evaluation components without their dataset are reported NOT RUN in `docs/eval_report.md`. "
         "This is not the final real-data evaluation.**", "", "## Steps\n", "| step | result | seconds | detail |\n|---|---|---|---|"]
    L += [f"| {r['step']} | {r['status'].upper()} | {r['seconds']} | {r['detail'].replace('|', '/')} |" for r in results]
    L += ["", "Evaluation outputs: `reports/eval_results.json`, `docs/eval_report.md`, `docs/dataset_manifest.md`."]
    open(os.path.join("reports", "final_test_results.md"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    json.dump({"datasets_present": have, "real_data_run": real, "steps": results}, open(os.path.join("reports", "final_test_results.json"), "w"), indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--require-real", action="store_true"), ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--allow-missing", nargs="*", default=[], choices=list(DATASETS), help="datasets that may be absent with --require-real (reported NOT RUN)")
    ap.add_argument("--fast", action="store_true", help="smaller synthetic set + fewer cost scales (quicker, same pipeline)")
    ap.add_argument("--skip-tests", action="store_true"), ap.add_argument("--skip-live", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs("reports", exist_ok=True)
    have = present()
    needed = all(v for k, v in have.items() if k not in a.allow_missing)
    record("dataset check", needed if a.require_real else None, ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in have.items()))
    if a.require_real and not needed:
        write_report(have)
        print("missing real datasets; run `python scripts/get_datasets.py --all`")
        return 1
    if not a.skip_eval:
        if not dependency_check():
            write_report(have)
            print("fix the missing packages first (pip install -r requirements.txt), otherwise the sentiment/crop numbers are not the real ones")
            return 1
        run_eval(have, a.fast)
    if not a.skip_tests:
        run_pytest()
    srv = None
    if not a.skip_live:
        try:
            checks, base, srv = api_workflow()
            if base and srv:
                for name, ok, extra in (checks or []):
                    print(f"    {'ok ' if ok else 'BAD'} {name} {extra}")
                streamlit_check(base)
        finally:
            if srv:
                srv.terminate()
    write_report(have)
    print("wrote reports/final_test_results.md")
    return 0 if all(r["status"] != "fail" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
