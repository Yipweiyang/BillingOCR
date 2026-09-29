"""Streamlit UI. Run with: streamlit run app.py"""
import pandas as pd
import streamlit as st

from checker import price_list_for, read_uploads, run_checks

CHECK_COLS = ["Check 1", "Check 2", "Check 3", "Check 4", "Check 5"]
STATUS_STYLE = {
    "FLAG": "background-color: #fdecea; color: #a4192c; font-weight: 600",
    "PASS": "color: #1b7f3b",
    "N/A": "color: #6b6b6b",
    "NOT RUN": "color: #6b6b6b; font-style: italic",
}
DETAIL_STYLE = {
    "FLAG": "background-color: #fdecea; color: #a4192c",
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
            styles[detail] = df[col].map(DETAIL_STYLE).fillna("")
    return df.style.apply(lambda _: styles, axis=None)


def joined(values):
    return ", ".join(map(str, values))


st.set_page_config(page_title="PDF Checker", layout="wide")
# baseFontSize in .streamlit/config.toml enlarges everything; hold the title at its default size.
# Widget labels and the upload dropzone use Streamlit's smaller text size; bring them up to body size.
st.markdown("""<style>
h1 { font-size: 44px !important; }
[data-testid="stWidgetLabel"] p { font-size: 1.1rem !important; }
[data-testid="stFileUploader"] section,
[data-testid="stFileUploader"] section *,
[data-testid="stFileUploader"] button { font-size: 1rem !important; }
</style>""", unsafe_allow_html=True)
st.title("Mastersheet / Incident Report Checker")
st.markdown("""
**Checks run on every mastersheet item:**
1. **Evidence** - exactly one incident report / photo set, not missing or duplicated
2. **Quantities** - mastersheet quantities match the evidence, and the measurements marked on the AFTER photos match the report's QUANTITY
3. **AFTER photos** - completion dates on the AFTER photos agree with the mastersheet
4. **OIC instruction** - an OIC instruction exists in the report
5. **PQ items and prices** - each line is billed under the right PQ item for its area, at the contract's rate
""")

master_file = st.file_uploader("1. Upload mastersheet PDF", type="pdf")
report_files = st.file_uploader(
    "2. Upload the evidence", type=["pdf", "zip"], accept_multiple_files=True,
    help="Incident report PDFs, or - for formats whose evidence is a folder of site photos "
         "per S/N - those folders zipped up.")
price_files = st.file_uploader(
    "3. Upload the contract's price schedule", type=["pdf", "xls", "xlsb"], accept_multiple_files=True,
    help="The Bill of Quantities the mastersheet is billed against (Section B, provisional quantities). "
         "Add every schedule the contract has had - an original and its extensions - and each line is "
         "priced against the one covering its completion date. Without it, check 5 is not run.")
ready = bool(master_file and report_files)
csv_name = "check_results.csv"

if st.button("Run checks", type="primary", disabled=not ready):
    bar = st.progress(0.0, text="Starting...")

    def show(phase, done, total):
        bar.progress(done / total if total else 1.0, text=f"{phase}: {done} of {total}")

    try:
        if price_files:
            bar.progress(0.0, text="Reading price schedules...")
        price_list = price_list_for(master_file.getvalue(), [(f.name, f.getvalue()) for f in price_files])
        items, evidence, evidence_name = read_uploads(
            master_file.getvalue(),
            [(f.name, f.getvalue()) for f in report_files],
            progress=lambda done, total: show("Reading reports", done, total))
        result = run_checks(items, evidence, evidence_name,
                            progress=lambda done, total: show("Running checks", done, total),
                            price_list=price_list)
    except ValueError as e:
        # The batch is not a shape this app can check - say why, plainly.
        bar.empty()
        st.error(str(e))
        st.stop()
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
    for name, why in price_list["rejected"]:
        st.warning(f"Price schedule {name} was not used: it {why}.")
    if not price_list["schedules"]:
        st.warning(f"No price schedule for {price_list['contract'] or 'this contract'} was uploaded, "
                   f"so check 5 (PQ items and prices) was not run.")
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

    # Every problem, proven or unreadable, is a FLAG; the detail column says which.
    issues = df[(df[CHECK_COLS] == "FLAG").any(axis=1)]
    st.subheader(f"Issues ({len(issues)})")
    if issues.empty:
        st.success("No issues found.")
    else:
        st.dataframe(style_results(issues), width="stretch", hide_index=True)

    st.download_button(
        "Download results CSV",
        df.to_csv(index=False).encode("utf-8-sig"),
        csv_name,
        "text/csv",
    )
