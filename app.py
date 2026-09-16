"""Streamlit UI. Run with: streamlit run app.py"""
import os

import pandas as pd
import streamlit as st

from checker import check_batch, read_rm_pdfs, run_checks

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FROM_FOLDER, FROM_UPLOAD = "Batch folder in data/", "Upload PDFs (RM205 / RM206)"

CHECK_COLS = ["Check 1", "Check 2", "Check 3", "Check 4"]
STATUS_STYLE = {
    "FLAG": "background-color: #fdecea; color: #a4192c; font-weight: 600",
    "REVIEW": "background-color: #fff4e0; color: #8a5300; font-weight: 600",
    "PASS": "color: #1b7f3b",
    "N/A": "color: #6b6b6b",
    "NOT RUN": "color: #6b6b6b; font-style: italic",
}
DETAIL_STYLE = {
    "FLAG": "background-color: #fdecea; color: #a4192c",
    "REVIEW": "background-color: #fff4e0; color: #8a5300",
}


def style_results(df):
    """Red for FLAG and amber for REVIEW, on both the status cell and its detail cell."""
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in CHECK_COLS:
        if col not in df.columns:
            continue
        styles[col] = df[col].map(STATUS_STYLE).fillna("")
        detail = f"{col} Detail"
        if detail in df.columns:
            styles[detail] = df[col].map(DETAIL_STYLE).fillna("")
    return df.style.apply(lambda _: styles, axis=None)


def batch_folders():
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(d for d in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, d)))


def joined(values):
    return ", ".join(map(str, values))


st.set_page_config(page_title="PDF Checker", layout="wide")
st.title("Mastersheet / Incident Report Checker")
st.caption("Checks: missing/duplicates, quantities, AFTER-photo completion dates, OIC instruction existence")

source = st.radio("Input", [FROM_FOLDER, FROM_UPLOAD], horizontal=True)
if source == FROM_FOLDER:
    batch = st.selectbox("Batch", batch_folders(),
                         help="Each folder holds the mastersheet PDF, named after the folder, and its evidence.")
    ready = bool(batch)
    csv_name = f"{batch}_check_results.csv" if batch else "check_results.csv"
else:
    master_file = st.file_uploader("1. Upload mastersheet PDF", type="pdf")
    report_files = st.file_uploader("2. Upload all incident report PDFs", type="pdf", accept_multiple_files=True)
    ready = bool(master_file and report_files)
    csv_name = "check_results.csv"

if st.button("Run checks", type="primary", disabled=not ready):
    bar = st.progress(0.0, text="Starting...")

    def show(phase, done, total):
        bar.progress(done / total if total else 1.0, text=f"{phase}: {done} of {total}")

    try:
        if source == FROM_FOLDER:
            result = check_batch(os.path.join(DATA_DIR, batch), show)
        else:
            items, evidence = read_rm_pdfs(
                master_file.getvalue(),
                [(f.name, f.getvalue()) for f in report_files],
                progress=lambda done, total: show("Reading reports", done, total))
            result = run_checks(items, evidence,
                                progress=lambda done, total: show("Running checks", done, total))
    except Exception as e:
        bar.empty()
        st.exception(e)
        st.stop()
    bar.empty()

    a, b, c, d = st.columns(4)
    a.metric("Master records", result["master_count"])
    b.metric("Evidence", result["uploaded_count"])
    c.metric("Missing", len(result["missing"]))
    d.metric("Duplicates", len(result["duplicates"]))

    if result["missing"]:
        st.error("Missing: " + joined(result["missing"]))
    if result["duplicates"]:
        st.error("Duplicates: " + joined(result["duplicates"]))
    if result["extra"]:
        st.warning("Not in mastersheet: " + joined(result["extra"]))
    if result["unreadable"]:
        st.warning("Unreadable Defect Ref: " + joined(result["unreadable"]))

    with st.expander("Evidence matching diagnostics"):
        st.caption("Use this table to verify which mastersheet item each piece of evidence was matched to.")
        st.dataframe(pd.DataFrame(result["mapping"]), width="stretch", hide_index=True)

    df = pd.DataFrame(result["rows"])
    # Formats without defect references (TR387) identify items by S/N and location.
    if "Defect Ref" in df.columns and (df["Defect Ref"].astype(str) == "").all():
        df = df.drop(columns="Defect Ref")
    st.subheader("All results")
    st.dataframe(style_results(df), width="stretch", hide_index=True)

    flagged = df[(df[CHECK_COLS] == "FLAG").any(axis=1)]
    st.subheader(f"Flagged records ({len(flagged)})")
    if flagged.empty:
        st.success("No records flagged.")
    else:
        st.dataframe(style_results(flagged), width="stretch", hide_index=True)

    review = df[(df[CHECK_COLS] == "REVIEW").any(axis=1) & ~df.index.isin(flagged.index)]
    if not review.empty:
        st.subheader(f"Needs a manual look ({len(review)})")
        st.caption("OCR could not settle these - usually a hand-written board it could not read.")
        st.dataframe(style_results(review), width="stretch", hide_index=True)

    st.download_button(
        "Download results CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        csv_name,
        "text/csv",
    )
