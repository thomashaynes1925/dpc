import streamlit as st
import pandas as pd
import re
import io
import bcrypt
from urllib.parse import quote_plus
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

# Initialize authentication state
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False

# Load credentials from secrets
creds = st.secrets.get("credentials", {})

# --- Login Screen ---
if not st.session_state.authenticated:
    st.title("Login to Deliveries Photo Checker")
    with st.form(key="login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        if submitted:
            hash_val = creds.get(username)
            if hash_val and bcrypt.checkpw(password.encode(), hash_val.encode()):
                st.session_state.authenticated = True
            else:
                st.error("Invalid username or password")
    st.stop()

# --- Main App ---

st.title("Deliveries Photo Checker")

# Create HTTP session with retries
def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
session = make_session()

# Regex for registrations
REG_PATTERN = re.compile(r"\b(?:[A-Z0-9]{1,3}-[A-Z0-9]{1,5}|N\d{1,5}[A-Z]?)\b")

def extract_regs(df, col):
    regs = set()
    series = df.iloc[:, col] if isinstance(col, int) else df[col]
    for txt in series.astype(str):
        for m in REG_PATTERN.findall(txt):
            regs.add(m)
    return sorted(regs)

# Search functions
def search_airteam(reg, timeout):
    url = f"https://www.airteamimages.com/search?q={quote_plus(reg)}&sort=id%2Cdesc"
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
    except:
        return False, ''
    if re.search(r'<img[^>]+class="[^"]*h-auto[^"]*max-h-\[155px\][^"]*"', r.text):
        return True, url
    return False, ''


def search_v1(reg, timeout):
    base = "https://www.v1images.com"
    url = f"{base}/?s={quote_plus(reg)}&post_type=product&orderby=date-DESC"
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
    except:
        return False, ''
    final = r.url
    if final.rstrip('/') != url.rstrip('/') or re.search(r'<figure[^>]+class="[^"]*woocom-project[^"]*"', r.text):
        return True, final
    return False, ''

# File upload
uploaded = st.file_uploader("Upload file (.xls, .xlsx, .csv, .txt)")
if not uploaded:
    st.info("Please upload a file to proceed.")
    st.stop()
ext = uploaded.name.split('.')[-1].lower()

# Advanced Settings
with st.expander("Advanced Settings", expanded=False):
    if ext in ['xls', 'xlsx']:
        sheet = st.text_input("Excel sheet name", "ExportedData")
        col_input = st.text_input("Excel column (name or 1-based index)", "1")
    elif ext == 'csv':
        df_temp = pd.read_csv(uploaded, nrows=0)
        col_input = st.text_input("CSV column name", df_temp.columns[0])
    else:
        col_input = None
    workers = st.slider("Parallel workers", 1, 20, 10)
    timeout = st.slider("Request timeout (seconds)", 5, 60, 10)

# Network selection
st.markdown("**Select networks to check:**")
check_ati = st.checkbox("AirTeamImages", value=True)
check_v1 = st.checkbox("V1Images", value=True)

# Load registrations
def load_regs():
    if ext == 'txt':
        lines = uploaded.getvalue().decode('utf-8').splitlines()
        return sorted({l.strip() for l in lines if l.strip()})
    elif ext == 'csv':
        df = pd.read_csv(uploaded, dtype=str)
        if col_input not in df.columns:
            st.error(f"Column '{col_input}' not found.")
            st.stop()
        return extract_regs(df, col_input)
    else:
        try:
            df = pd.read_excel(uploaded, sheet_name=sheet, dtype=str)
        except Exception as e:
            st.error(f"Failed to read Excel: {e}")
            st.stop()
        col_idx = int(col_input) - 1 if col_input.isdigit() else col_input
        return extract_regs(df, col_idx)
regs = load_regs()

# Run Checks
if st.button("Run Checks"):
    st.write(f"Found {len(regs)} registrations. Starting checks...")
    progress = st.progress(0)
    results = []

    def check_entry(reg):
        entry = {'Registration': reg}
        if check_ati:
            ok, link = search_airteam(reg, timeout)
            entry['AirTeamImages'] = ok; entry['ATI_Link'] = link
        if check_v1:
            ok2, link2 = search_v1(reg, timeout)
            entry['V1Images'] = ok2; entry['V1_Link'] = link2
        return entry

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for i, res in enumerate(executor.map(check_entry, regs)):
            results.append(res)
            progress.progress((i+1)/len(regs))

    df_out = pd.DataFrame(results)[['Registration','AirTeamImages','ATI_Link','V1Images','V1_Link']]
    st.dataframe(df_out)

    # Prepare Excel
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine='xlsxwriter') as writer:
        df_out.to_excel(writer, index=False, sheet_name='Results')
        wb = writer.book; ws = writer.sheets['Results']
        green = wb.add_format({'bg_color':'#C6EFCE'})
        link_fmt = wb.add_format({'font_color':'blue','underline':True})
        if check_ati:
            ws.conditional_format(f'B2:B{len(df_out)+1}', {'type':'cell','criteria':'==','value':True,'format':green})
            for r, link in enumerate(df_out['ATI_Link'], start=1):
                if link: ws.write_url(r,2,link,link_fmt,'View ATI')
        if check_v1:
            ws.conditional_format(f'D2:D{len(df_out)+1}', {'type':'cell','criteria':'==','value':True,'format':green})
            for r, link in enumerate(df_out['V1_Link'], start=1):
                if link: ws.write_url(r,4,link,link_fmt,'View V1')
    buffer.seek(0)
    st.download_button("Download Results as Excel", data=buffer, file_name="photo_availability.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
