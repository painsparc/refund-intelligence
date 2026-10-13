"""
streamlit_app.py  -  the Streamlit front-end of the proposal. It only CALLS the FastAPI backend (app.py), which runs the
LangGraph intelligence layer; no decision logic lives here.

    uvicorn app:app --reload            # terminal 1  (backend)
    streamlit run streamlit_app.py      # terminal 2  (UI)        set API_URL if the backend is not on localhost:8000

Pages:  Customer Portal  |  Operations Console  |  Management View
"""
import os
import pandas as pd
import requests
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")
REASONS = ["damaged", "defective", "wrong_item", "not_as_described", "changed_mind", "size_issue", "not_delivered"]
FRIENDLY = {"refunded": "Approved - your refund has been issued.",
            "awaiting_evidence": "We need a clearer photo of the item to continue (upload it below).",
            "awaiting_return": "Please return the item - your refund is issued as soon as it is received.",
            "under_review": "Under review by our team. We will update you soon.",
            "declined": "Not eligible under the refund policy.",
            "rejected": "Not approved."}

st.set_page_config(page_title="Refund Intelligence", layout="wide")


def api(method, path, **kw):
    try:
        r = requests.request(method, f"{API}{path}", timeout=60, **kw)
        if r.status_code >= 400:
            st.error(f"{r.status_code}: {r.json().get('detail', r.text)}")
            return None
        return r.json()
    except requests.RequestException as e:
        st.error(f"Backend not reachable at {API} ({e}). Start it with: uvicorn app:app --reload")
        return None


# =============================================================================== Customer Portal
def customer_portal():
    st.header("Customer Portal")
    st.caption("Submit a refund request with photo evidence and track its status. Try seeded orders ORD-1001 ... ORD-1006 "
               "(customers C001-C005).")
    left, right = st.columns(2)
    with left:
        st.subheader("New refund request")
        with st.form("claim"):
            customer_id = st.text_input("Customer ID", "C001")
            order_id = st.text_input("Order ID", "ORD-1001")
            reason = st.selectbox("Reason", REASONS)
            amount = st.number_input("Refund amount", min_value=1.0, value=3500.0, step=100.0)
            desc = st.text_area("What went wrong?", "The jar arrived cracked at the base.")
            img = st.file_uploader("Evidence photo", type=["jpg", "jpeg", "png"])
            go = st.form_submit_button("Submit claim")
        if go:
            files = {"image": (img.name, img.getvalue(), img.type)} if img else None
            res = api("POST", "/claims", data=dict(order_id=order_id, customer_id=customer_id, reason=reason,
                                                   amount=amount, description=desc), files=files)
            if res:
                st.session_state["claim_id"] = res["claim_id"]
                st.success(f"Claim {res['claim_id']} submitted.")
    with right:
        st.subheader("Claim status")
        claim_id = st.text_input("Claim ID", st.session_state.get("claim_id", ""))
        if claim_id:
            data = api("GET", f"/claims/{claim_id}")
            if data:
                status = data["claim"]["status"]
                st.info(FRIENDLY.get(status, status))
                if status == "awaiting_evidence":
                    more = st.file_uploader("Upload requested photo", type=["jpg", "jpeg", "png"], key="more")
                    if more and st.button("Send photo"):
                        res = api("POST", f"/claims/{claim_id}/evidence", files={"image": (more.name, more.getvalue(), more.type)})
                        if res:
                            st.success("Photo received - claim re-evaluated.")
                            st.rerun()


# =============================================================================== Operations Console
def operations_console():
    st.header("Operations Console")
    st.caption("Which claims need intervention now? Risk, evidence and decision details for each case.")
    queue = api("GET", "/insights/operations") or []
    if not queue:
        st.success("Queue is empty.")
        return
    st.dataframe(pd.DataFrame(queue)[["claim_id", "customer_id", "amount", "exposure_risk", "decision", "status", "case_id"]],
                 width="stretch", hide_index=True)
    claim_id = st.selectbox("Open a claim", [q["claim_id"] for q in queue])
    d = api("GET", f"/claims/{claim_id}")
    if not d:
        return
    c = d["claim"]
    st.subheader(f"{claim_id}  -  {c['decision'].replace('_', ' ').upper()}")
    m = st.columns(5)
    m[0].metric("Claim risk (0-100)", c["claim_risk"])
    m[1].metric("Behavioural risk (0-100)", c["behaviour_risk"])
    m[2].metric("Trust index (0-100)", c["trust_index"])
    m[3].metric("Evidence confidence (0-100)", c["evidence_confidence"])
    m[4].metric("Amount", c["amount"])
    st.caption(f"Exposure risk used for expected cost = 0.6 x claim risk + 0.4 x behavioural risk = {c['exposure_risk']} "
               "(a risk-normalised proxy, not a calibrated probability). Risk scores prioritise review; they are not proof of fraud.")
    a, b, e = st.columns(3)
    with a:
        st.markdown("**Expected cost by action** = risk exposure + customer friction + handling")
        breakdown = c.get("components", {}).get("cost_breakdown")
        if breakdown:
            st.dataframe(pd.DataFrame(breakdown).T, width="stretch")
        else:
            st.bar_chart(pd.Series(c["costs"]))
        st.write(c["rationale"])
    with b:
        st.markdown("**Risk formula components**")
        comp = c.get("components", {})
        for name in ("claim_risk", "behaviour_risk"):
            if comp.get(name):
                st.caption(name.replace("_", " "))
                st.dataframe(pd.Series(comp[name], name="score"), width="stretch")
        abuse = [s["detail"] for s in d["signals"] if s["kind"] == "abuse"]
        st.markdown("**Abuse patterns:** " + (", ".join(abuse) if abuse else "none"))
        st.markdown("**Other flags:** " + (", ".join(f for f in c["flags"] if f not in abuse) or "none"))
    with e:
        st.markdown("**Evidence**")
        if d["evidence"]:
            st.image(f"{API}/claims/{claim_id}/evidence-image", caption=f"quality {d['evidence'][0]['quality']}")
        else:
            st.write("No evidence uploaded.")
    with st.expander("Audit trail (existing action layer)"):
        st.dataframe(pd.DataFrame(d["audit_log"]), width="stretch", hide_index=True)

    if c["status"] == "awaiting_return":
        st.markdown("**Return tracking (existing return system)**")
        rc = st.columns(3)
        for col, ev_status in zip(rc, ["received", "late", "missing"]):
            if col.button(f"Return {ev_status}", key=f"ret_{ev_status}"):
                if api("POST", f"/claims/{claim_id}/return", json={"status": ev_status}):
                    st.rerun()

    case = d.get("review_case")
    if case and case["status"] == "open":
        st.markdown("**Human-in-the-loop decision**")
        note = st.text_input("Note", key="note")
        cols = st.columns(3)
        for col, outcome in zip(cols, ["approve", "reject", "fraud"]):
            if col.button(outcome.capitalize() + (" (blacklist)" if outcome == "fraud" else ""), key=outcome):
                if api("POST", f"/reviews/{case['id']}/resolve", json={"outcome": outcome, "note": note}):
                    st.rerun()


# =============================================================================== Management View
def management_view():
    st.header("Management View")
    t1, t2, t3, t4, t5 = st.tabs(["Refund & abuse", "Product", "Vendor", "Policy / ops", "Prevention loop"])
    with t1:
        m = api("GET", "/insights/management")
        if m:
            k = st.columns(4)
            k[0].metric("Claims", m["claims"])
            k[1].metric("Auto-resolved", f"{m['auto_resolution_rate']:.0%}")
            k[2].metric("Manual review", f"{m['manual_review_rate']:.0%}")
            k[3].metric("Expected loss avoided", m["expected_loss_avoided"])
            cr = m.get("customer_risk", {})
            st.caption(f"Customer risk: {cr.get('profiled_customers', 0)} profiled | "
                       f"{cr.get('high_behavioural_risk_customers(>=60)', 0)} with behavioural risk >= 60 | "
                       f"average trust index {cr.get('avg_trust_index')}")
            c1, c2 = st.columns(2)
            c1.markdown("**Abuse patterns detected**")
            c1.bar_chart(pd.Series(m["abuse_patterns"]))
            c2.markdown("**Refund value by category**")
            if m["by_category"]:
                c2.bar_chart(pd.DataFrame(m["by_category"]).set_index("category")["value"])
    with t2:
        rows = api("GET", "/insights/products")
        if rows:
            df = pd.DataFrame(rows)
            st.bar_chart(df.set_index("name")["refund_rate"])
            st.dataframe(df, width="stretch", hide_index=True)
    with t3:
        rows = api("GET", "/insights/vendors")
        if rows:
            df = pd.DataFrame(rows)
            st.bar_chart(df.set_index("id")[["refund_rate"]])
            st.dataframe(df, width="stretch", hide_index=True)
    with t4:
        for line in api("GET", "/insights/policy") or []:
            st.write("- " + line)
    with t5:
        st.caption("Patterns -> insight -> intervention -> later outcome. Runs offline, outside the real-time decision graph; "
                   "results are persisted as risk snapshots and an insight/intervention log.")
        if st.button("Run historical analysis (persist snapshots + insights)"):
            r = api("POST", "/historical/run")
            if r:
                st.success(f"Snapshot saved: {r['products']} products, {r['vendors']} vendors, {len(r['new_insights'])} new insights.")
        s1, s2 = st.columns(2)
        s1.markdown("**Product Risk Snapshot (latest)**")
        s1.dataframe(pd.DataFrame(api("GET", "/historical/snapshots/products") or []), width="stretch", hide_index=True)
        s2.markdown("**Vendor Risk Snapshot (latest)**")
        s2.dataframe(pd.DataFrame(api("GET", "/historical/snapshots/vendors") or []), width="stretch", hide_index=True)
        st.markdown("**Insight / Intervention Log**")
        for ins in api("GET", "/historical/insights") or []:
            with st.expander(f"[{ins['status']}] {ins['kind']} - {ins['entity_name']}: {ins['pattern']}"):
                st.write(ins["insight"])
                st.write("Intervention: " + ins["intervention"])
                if ins["status"] == "proposed":
                    note = st.text_input("Intervention note", key=f"n{ins['id']}")
                    if st.button("Mark intervention applied", key=f"a{ins['id']}"):
                        if api("POST", f"/historical/insights/{ins['id']}/apply", json={"note": note}):
                            st.rerun()
                elif ins["status"] == "applied":
                    st.write(f"Applied {ins['applied_at']}: {ins['applied_note']}  | baseline {ins['metric_name']} = {ins['baseline_value']}")
                    if st.button("Evaluate outcome now", key=f"e{ins['id']}"):
                        if api("POST", f"/historical/insights/{ins['id']}/evaluate"):
                            st.rerun()
                else:
                    st.write(f"Outcome: **{ins['outcome']}** (baseline {ins['baseline_value']} -> now {ins['current_value']})")


PAGES = {"Customer Portal": customer_portal, "Operations Console": operations_console, "Management View": management_view}
st.sidebar.title("Refund Intelligence")
PAGES[st.sidebar.radio("View", list(PAGES))]()
st.sidebar.caption("Existing refund process -> Streamlit -> LangGraph intelligence layer -> expected-cost decision -> "
                   "existing action execution -> historical intelligence loop")
