"""Streamlit UI. Run with: streamlit run app.py"""
import pandas as pd
import streamlit as st

from checker import run_checks

CHECK_COLS = ["Check 1", "Check 2", "Check 3", "Check 4"]
STATUS_STYLE = {
    "FLAG": "background-color: #fdecea; color: #a4192c; font-weight: 600",
    "PASS": "color: #1b7f3b",
    "NOT RUN": "color: #6b6b6b; font-style: italic",
}


def style_results(df):
    """Red for FLAG, on both the status cell and its detail cell."""
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    for col in CHECK_COLS:
        if col not in df.columns:
            continue
        styles[col] = df[col].map(STATUS_STYLE).fillna("")
        detail = f"{col} Detail"
        if detail in df.columns:
            styles[detail] = df[col].map(
                {"FLAG": "background-color: #fdecea; color: #a4192c"}).fillna("")
    return df.style.apply(lambda _: styles, axis=None)


st.set_page_config(page_title="PDF Checker", layout="wide")
st.title("Mastersheet / Incident Report Checker")
st.caption("Checks: missing/duplicates, PQ-area matching, AFTER-photo completion dates, OIC instruction existence")

master_file = st.file_uploader("1. Upload mastersheet PDF", type="pdf")
report_files = st.file_uploader("2. Upload all incident report PDFs", type="pdf", accept_multiple_files=True)

if st.button("Run checks", type="primary", disabled=not (master_file and report_files)):
    with st.spinner("Processing PDFs..."):
        try:
            result = run_checks(master_file.getvalue(), report_files)
        except Exception as e:
            st.exception(e)
            st.stop()

    a, b, c, d = st.columns(4)
    a.metric("Master records", result["master_count"])
    b.metric("Reports uploaded", result["uploaded_count"])
    c.metric("Missing", len(result["missing"]))
    d.metric("Duplicates", len(result["duplicates"]))

    if result["missing"]:
        st.error("Missing: " + ", ".join(result["missing"]))
    if result["duplicates"]:
        st.error("Duplicates: " + ", ".join(result["duplicates"]))
    if result["extra"]:
        st.warning("Not in mastersheet: " + ", ".join(result["extra"]))
    if result["unreadable"]:
        st.warning("Unreadable Defect Ref: " + ", ".join(result["unreadable"]))

    with st.expander("Upload matching diagnostics"):
        st.caption("Use this table to verify which Defect Ref was extracted from each uploaded PDF.")
        st.dataframe(pd.DataFrame(result["mapping"]), use_container_width=True, hide_index=True)

    df = pd.DataFrame(result["rows"])
    st.subheader("All results")
    st.dataframe(style_results(df), use_container_width=True, hide_index=True)

    flagged = df[(df[CHECK_COLS] == "FLAG").any(axis=1)]
    st.subheader(f"Flagged records ({len(flagged)})")
    if flagged.empty:
        st.success("No records flagged.")
    else:
        st.dataframe(style_results(flagged), use_container_width=True, hide_index=True)

    st.download_button(
        "Download results CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        "rm206_check_results.csv",
        "text/csv",
    )
