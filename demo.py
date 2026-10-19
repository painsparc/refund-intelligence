"""
demo.py  -  runs 6 realistic claims through the full LangGraph pipeline and prints the business views.

    python demo.py            (fresh DB each run; uses SQLite unless DATABASE_URL is set)
"""
import io
import os
import random
import sys
from PIL import Image, ImageDraw, ImageFilter

import db
import intelligence as ai
from graph import process_claim


def make_image(seed, blurry=False, size=(800, 600)):
    """Synthetic 'product photo' so the demo needs no real images."""
    r = random.Random(seed)
    img = Image.new("RGB", size, tuple(r.randint(40, 200) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(80):
        x, y = r.randint(0, size[0]), r.randint(0, size[1])
        d.rectangle([x, y, x + r.randint(20, 200), y + r.randint(20, 150)],
                    fill=tuple(r.randint(0, 255) for _ in range(3)), outline=tuple(r.randint(0, 255) for _ in range(3)))
    if blurry:
        img = img.filter(ImageFilter.GaussianBlur(9)).resize((320, 240))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def resized_copy(jpeg_bytes):
    """Same photo, re-saved smaller - what a fraudster re-uploading someone else's evidence looks like."""
    img = Image.open(io.BytesIO(jpeg_bytes)).resize((640, 480))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=70)
    return buf.getvalue()


def show(title, r):
    print(f"\n=== {title}")
    print(f"  decision : {r['decision'].upper()}   (claim_risk={r['claim_risk']}/100, behaviour_risk={r['behaviour_risk']}/100, "
          f"trust_index={r['trust_index']}, exposure={r['exposure_risk']}, evidence_conf={r['evidence_confidence']}/100)")
    print(f"  costs    : {r['costs']}")
    print(f"  abuse    : {r['abuse_patterns']}   image-match signals: {r.get('match_methods')}")
    print(f"  tone     : sentiment {r['sentiment']['score']} ({r['sentiment']['label']}), cx_priority={r['cx_priority']}  [routing only, never a risk input]")
    print(f"  flags    : {r['flags']}")
    print(f"  why      : {r['rationale']}")


def main():
    if "DATABASE_URL" not in os.environ and os.path.exists("refund_intel.db"):
        os.remove("refund_intel.db")  # start clean for the demo
    db.init()
    db.seed()
    sharp_serial = make_image(2)

    claims = [
        ("1. Good customer, damaged mixer, clear photo", dict(order_id="ORD-1001", customer_id="C001", reason="damaged", amount=3500,
         description="The jar arrived cracked at the base, photo attached."), make_image(1)),
        ("2. Serial refunder (7 refunds in 60 days, all just under limit, last days of window)",
         dict(order_id="ORD-1002", customer_id="C003", reason="defective", amount=2890,
              description="Sole came off after two days of use."), sharp_serial),
        ("3. New account, Rs 18,000 phone, blurry photo", dict(order_id="ORD-1003", customer_id="C004", reason="damaged", amount=18000,
         description="Screen is cracked."), make_image(3, blurry=True)),
        ("4. Another customer re-uploads C003's photo (evidence reuse)", dict(order_id="ORD-1004", customer_id="C005", reason="damaged",
         amount=2400, description="Earbuds case is broken and one bud does not charge."), resized_copy(sharp_serial)),
        ("5. Occasional customer, physical damage claim, NO photo", dict(order_id="ORD-1005", customer_id="C002", reason="damaged",
         amount=1800, description="Stitching came apart on the sleeve."), None),
        ("6. Grocery claim 6 days after delivery (window is 2 days)", dict(order_id="ORD-1006", customer_id="C002", reason="not_as_described",
         amount=700, description="Rice quality was poor, not what the listing showed."), make_image(6)),
    ]
    results = {}
    for title, claim, img in claims:
        results[title[0]] = process_claim(claim, img)
        show(title, results[title[0]])

    # evidence-request loop: claim 5 is asked for evidence (if the model chose so), customer replies with a photo
    r5 = results["5"]
    again = r5
    if r5["decision"] == "request_evidence":
        again = process_claim(dict(id=r5["claim_id"], order_id="ORD-1005", customer_id="C002", reason="damaged", amount=1800,
                                   description="Stitching came apart on the sleeve."), make_image(55), resubmission=True)
        show("5b. Same claim after customer uploads the requested photo", again)

    # refund-after-return: the existing return system reports the item came back -> refund released
    if again["decision"] == "refund_after_return":
        print("\n=== Return event for 5b ->", ai.record_return(again["claim_id"], "received"))

    # human-in-the-loop: analyst confirms the serial refunder as fraud -> blacklist (case outcome feeds future claims)
    case = results["2"]["review_case_id"]
    print("\n=== Analyst resolves case", case, "->", ai.resolve_review(case, "fraud", "serial late-window refunds"))

    print("\n##### BUSINESS VIEWS #####")
    print("\n-- Operations: which claims need intervention now?")
    for r in ai.operations_view():
        print(f"   {r['claim_id']}  cust={r['customer_id']}  amt={r['amount']}  exposure={r['exposure_risk']}  {r['status']}")
    m = ai.management_view()
    print(f"\n-- Management: {m['claims']} claims | auto-resolved {m['auto_resolution_rate']:.0%} | manual review {m['manual_review_rate']:.0%} "
          f"| expected loss avoided {m['expected_loss_avoided']} | expected leakage {m['expected_leakage_auto_approved']}")
    print("\n-- Product intelligence (refund-rate outliers marked *)")
    for p in ai.product_intelligence()[:4]:
        print(f"   {'*' if p['outlier'] else ' '} {p['name']:<22} orders={p['orders']:<3} refund_rate={p['refund_rate']:<6} top_reasons={p['top_reasons']}")
    print("\n-- Vendor intelligence")
    for v in ai.vendor_intelligence()[:3]:
        print(f"   {'*' if v['outlier'] else ' '} {v['id']:<10} refund_rate={v['refund_rate']:<6} expected_avoidable_loss={v['expected_avoidable_loss']}")
    print("\n-- Policy / ops insight")
    for line in ai.policy_insights():
        print("   -", line)
    print("\n-- Abuse patterns seen:", ai.abuse_pattern_counts())

    print("\n##### G5 HISTORICAL INTELLIGENCE & PREVENTION LOOP (offline) #####")
    run = ai.run_historical_loop()
    print(f"   snapshot persisted: {run['products']} products, {run['vendors']} vendors, {len(run['new_insights'])} new insights")
    for ins in ai.list_insights("proposed")[:6]:
        print(f"   [{ins['kind']:<7}] {ins['insight']}\n             -> intervention: {ins['intervention']}")
    first = next((i for i in ai.list_insights("proposed") if i["kind"] == "product"), None)
    if first:
        ai.apply_intervention(first["id"], "QC audit ordered with vendor")
        done = ai.evaluate_outcome(first["id"])
        print(f"   applied + evaluated {first['entity_name']}: baseline={done['baseline_value']} now={done['current_value']} "
              f"-> outcome={done['outcome']} (needs new orders over time to show improvement)")


if __name__ == "__main__":
    sys.exit(main())
