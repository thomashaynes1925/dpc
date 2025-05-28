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

# --- Authentication via Streamlit secrets ---
creds = st.secrets.get("credentials", {})
if 'authenticated' not in st.session_state:
    st.session_state.authenticated = False
if not st.session_state.authenticated:
    st.title("Login to Photo Checker")
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        hash_val = creds.get(username)
        if hash_val and bcrypt.checkpw(password.encode(), hash_val.encode()):
            st.session_state.authenticated = True
        else:
            st.error("Invalid username or password")
    st.stop()

# --- Main App ---
st.title("Photo Checker")

# Create HTTP session with retries
def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5,
                  status_forcelist=[429,500,502,503,504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
session = make_session()

# Extract registrations
REG_PATTERN = re.compile(r"\b(?:[A-Z0-9]{1,3}-[A-Z0-9]{1,5}|N\d{1,5}[A-Z]?)\b")
def extract_regs(df, col):
    regs = set()
    series = df.iloc[:, col] if isinstance(col, int) else df[col]
    for txt in series.astype(str):
        for m in REG_PATTERN.findall(txt):
            regs.add(m)
    return sorted(regs)

# Site 1: AirTeamImages
def search_airteam(reg, timeout=10):
    url = f"https://www.airteamimages.com/search?q={quote_plus(reg)}&sort=id%2Cdesc"
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
    except:
        return False, ''
    if re.search(r'<img[^>]+class="[^"]*h-auto[^"]*max-h-\[155px\][^"]*"', r.text):
        return True, url
    return False, ''

# Site 2: V1Images
def search_v1(reg, timeout=10):
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

# Site 3: Aviation Image Network
def search_ain(reg, timeout=10):
    url = f"https://www.aviationimagenetwork.com/search/?n=aviationimagenetwork&scope=node&scopeValue=cm8GDr&c=photos&q={quote_plus(reg)}"
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
    except:
        return False, ''
    # if links to '/photos/' exist, assume results
    if re.search(r'href="/photos/', r.text):
        return True, url
    return False, ''

# File uploader
uploaded = st.file_uploader("Upload file (.xls, .xlsx, .csv, .txt)")
if not uploaded:
    st.info("Please upload a file to proceed.")
    st.stop()
ext = uploaded.name.split('.')[-1].lower()

# Advanced Settings
with st.expander("Advanced Settings", expanded=False):
    if ext in ['xls','xlsx']:
        sheet = st.text_input("Excel sheet name", value="ExportedData")
        col_input = st.text_input("Excel column (name or 1-based index)", value="1")
    elif ext=='csv':
        tmp = pd.read_csv(uploaded, nrows=0)
        col_input = st.text_input("CSV column name", value=tmp.columns[0])
    else:
        col_input = None
    workers = st.slider("Parallel workers", 1, 20, 10)
    timeout = st.slider("Request timeout (seconds)", 5, 60, 10)

# Network selection
st.markdown("**Select networks to check:**")
check_ati = st.checkbox("AirTeamImages", value=True)
check_v1 = st.checkbox("V1Images", value=True)
check_ain = st.checkbox("Aviation Image Network", value=False)

# Load registrations
def load_regs():
    if ext=='txt':
        return sorted({l.strip() for l in uploaded.getvalue().decode().splitlines() if l.strip()})
    if ext=='csv':
        df = pd.read_csv(uploaded, dtype=str)
        if col_input not in df.columns:
            st.error(f"Column '{col_input}' not found.")
            st.stop()
        return extract_regs(df, col_input)
    df = pd.read_excel(uploaded, sheet_name=sheet, dtype=str)
    idx = int(col_input)-1 if col_input.isdigit() else col_input
    return extract_regs(df, idx)
regs = load_regs()

# Run Checks
if st.button("Run Checks"):
    st.write(f"Checking {len(regs)} registrations...")
    progress = st.progress(0)
    results = []
    def check(reg):
        entry = {'Registration':reg}
        if check_ati:
            ok,ln=search_airteam(reg, timeout)
            entry['AirTeamImages']=ok; entry['ATI_Link']=ln
        if check_v1:
            ok,ln=search_v1(reg, timeout)
            entry['V1Images']=ok; entry['V1_Link']=ln
        if check_ain:
            ok,ln=search_ain(reg, timeout)
            entry['AIN']=ok; entry['AIN_Link']=ln
        return entry
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i,res in enumerate(ex.map(check, regs)):
            results.append(res)
            progress.progress((i+1)/len(regs))
    df_out = pd.DataFrame(results)[['Registration','AirTeamImages','ATI_Link','V1Images','V1_Link','AIN','AIN_Link']]
    st.dataframe(df_out)
    # Excel export
    buf=io.BytesIO()
    with pd.ExcelWriter(buf, engine='xlsxwriter') as writer:
        df_out.to_excel(writer, index=False, sheet_name='Results')
        wb,ws=writer.book,writer.sheets['Results']
        green=wb.add_format({'bg_color':'#C6EFCE'})
        linkfmt=wb.add_format({'font_color':'blue','underline':True})
        if check_ati:
            ws.conditional_format(f'B2:B{len(df_out)+1}',{'type':'cell','criteria':'==','value':True,'format':green})
            for r,link in enumerate(df_out['ATI_Link'],start=1):
                if link: ws.write_url(r,2,link,linkfmt,'View ATI')
        if check_v1:
            ws.conditional_format(f'D2:D{len(df_out)+1}',{'type':'cell','criteria':'==','value':True,'format':green})
            for r,link in enumerate(df_out['V1_Link'],start=1):
                if link: ws.write_url(r,4,link,linkfmt,'View V1')
        if check_ain:
            ws.conditional_format(f'F2:F{len(df_out)+1}',{'type':'cell','criteria':'==','value':True,'format':green})
            for r,link in enumerate(df_out['AIN_Link'],start=1):
                if link: ws.write_url(r,6,link,linkfmt,'View AIN')
    buf.seek(0)
    st.download_button("Download Excel", data=buf, file_name="photo_availability.xlsx", mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
