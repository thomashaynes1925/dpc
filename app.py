import streamlit as st
import pandas as pd
import re
import io
from urllib.parse import quote_plus
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

# Regex for registrations (e.g., G-ABCD, N123AB)
REG_PATTERN = re.compile(r"\b(?:[A-Z0-9]{1,3}-[A-Z0-9]{1,5}|N\d{1,5}[A-Z]?)\b")

# Helper functions

def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

session = make_session()

def extract_regs(df, col):
    regs = set()
    if isinstance(col, int):
        series = df.iloc[:, col]
    else:
        series = df[col]
    for txt in series.astype(str):
        for m in REG_PATTERN.findall(txt):
            regs.add(m)
    return sorted(regs)

# Site checker using regex on HTML

def search_airteam(reg, timeout=10):
    base = "https://www.airteamimages.com"
    search_url = f"{base}/search?q={quote_plus(reg)}&sort=id%2Cdesc"
    try:
        resp = session.get(search_url, timeout=timeout)
        resp.raise_for_status()
    except:
        return False, ''
    html = resp.text
    # look for <img ... class="... h-auto ... max-h-[155px] ...">
    if re.search(r'<img[^>]+class="[^"]*h-auto[^"]*max-h-\[155px\][^"]*"', html):
        return True, search_url
    return False, ''

# Streamlit UI

st.title("Deliveries Photo Checker")

# File uploader
uploaded = st.file_uploader("Upload your file (.xlsx, .csv, .txt)")
if not uploaded:
    st.stop()

# Determine file type
ext = uploaded.name.split('.')[-1].lower()

# Advanced settings in expander
with st.expander("Advanced Settings", expanded=False):
    if ext in ['xlsx', 'xls', 'csv']:
        sheet = st.text_input("Sheet name", value="ExportedData")
        col_input = st.text_input("Column name or 1-based index", value="1")
    else:
        sheet = None
        col_input = None
    workers = st.slider("Parallel workers", 1, 20, 10)
    timeout = st.slider("Request timeout (seconds)", 5, 60, 10)

# Network selection
st.markdown("**Select networks to check:**")
check_ati = st.checkbox("AirTeamImages", value=True)

# Run checks
if st.button("Run Checks"):
    # Extract registrations
    if ext == 'txt':
        text_lines = uploaded.getvalue().decode('utf-8').splitlines()
        regs = sorted({line.strip() for line in text_lines if line.strip()})
    else:
        if ext in ['xlsx', 'xls']:
            df = pd.read_excel(uploaded, sheet_name=sheet, header=0)
        else:
            df = pd.read_csv(uploaded, header=0)
        col = int(col_input) - 1 if col_input and col_input.isdigit() else col_input
        regs = extract_regs(df, col)

    st.write(f"Found {len(regs)} registrations. Starting checks...")

    progress = st.progress(0)
    results = []

    # Define check function
    def check(reg):
        entry = {'Registration': reg}
        if check_ati:
            ok, link = search_airteam(reg, timeout)
            entry['AirTeamImages'] = ok
            entry['ATI_Link'] = link
        return entry

    # Run in parallel
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, entry in enumerate(executor.map(check, regs)):
            results.append(entry)
            progress.progress((i + 1) / len(regs))

    # Display results
    df_out = pd.DataFrame(results)
    st.dataframe(df_out)

    # Prepare Excel download
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df_out.to_excel(writer, index=False, sheet_name='Results')
        workbook = writer.book
        worksheet = writer.sheets['Results']
        green_fmt = workbook.add_format({'bg_color': '#C6EFCE'})
        url_fmt = workbook.add_format({'font_color': 'blue', 'underline': True})

        if check_ati:
            worksheet.conditional_format(f'B2:B{len(df_out)+1}', {
                'type': 'cell', 'criteria': '==', 'value': True, 'format': green_fmt
            })
            for row_idx, link in enumerate(df_out['ATI_Link'], start=1):
                if link:
                    worksheet.write_url(row_idx, 2, link, url_fmt, 'View ATI')

    output.seek(0)
    st.download_button(
        "Download Results as Excel",
        data=output,
        file_name="photo_availability.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
