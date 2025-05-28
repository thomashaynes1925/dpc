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
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Login form using a placeholder container
login_container = st.container()
with login_container:
    if not st.session_state.authenticated:
        st.title("Login to Deliveries Photo Checker")
        username = st.text_input("Username", key="login_user")
        password = st.text_input("Password", type="password", key="login_pwd")
        if st.button("Login", key="login_btn"):
            hash_val = creds.get(username)
            if hash_val and bcrypt.checkpw(password.encode(), hash_val.encode()):
                st.session_state.authenticated = True
                st.experimental_rerun()
            else:
                st.error("Invalid username or password")
        st.stop()

# --- Main App ---

st.title("Deliveries Photo Checker")

# HTTP session with retries
def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429,500,502,503,504])
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s

session = make_session()

# Regex for registrations
def extract_regs(df, col):
    pattern = re.compile(r"\b(?:[A-Z0-9]{1,3}-[A-Z0-9]{1,5}|N\d{1,5}[A-Z]?)\b")
    regs = set()
    series = df.iloc[:, col] if isinstance(col, int) else df[col]
    for txt in series.astype(str):
        for m in pattern.findall(txt): regs.add(m)
    return sorted(regs)

# Search functions
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

# File upload
uploaded = st.file_uploader("Upload file (.xls, .xlsx, .csv, .txt)")
if not uploaded:
    st.stop()
ext = uploaded.name.split('.')[-1].lower()

# Advanced Settings
with st.expander("Advanced Settings", expanded=False):
    if ext in ['xls','xlsx']:
        sheet = st.text_input("Excel sheet name", "ExportedData")
        col_input = st.text_input("Excel column (name or 1-based index)", "1")
    elif ext == 'csv':
        df_temp = pd.read_csv(uploaded, nrows=0)
        col_input = st.text_input("CSV column name", df_temp.columns[0])
    else:
        col_input = None
    workers = st.slider("Parallel workers", 1, 20, 10)
    timeout = st.slider("Request timeout (s)", 5, 60, 10)

# Network selection
check_ati = st.checkbox("AirTeamImages", value=True)
check_v1 = st.checkbox("V1Images", value=True)

# Load registrations
def load_regs():
    if ext=='txt':
        lines = uploaded.getvalue().decode().splitlines()
        return sorted({l.strip() for l in lines if l.strip()})
    if ext=='csv':
        df = pd.read_csv(uploaded, dtype=str)
        if col_input not in df: st.error(f"No column '{col_input}'"); st.stop()
        return extract_regs(df, col_input)
    df = pd.read_excel(uploaded, sheet_name=sheet, dtype=str)
    idx = int(col_input)-1 if col_input.isdigit() else col_input
    return extract_regs(df, idx)

regs = load_regs()

# Run Checks
if st.button("Run Checks"):
    st.write(f"Found {len(regs)} registrations.")
    progress = st.progress(0)
    results=[]

    def check(r):
        e={'Registration':r}
        if check_ati: ok,link=search_airteam(r,timeout); e.update({'AirTeamImages':ok,'ATI_Link':link})
        if check_v1: ok,link=search_v1(r,timeout); e.update({'V1Images':ok,'V1_Link':link})
        return e

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i,res in enumerate(ex.map(check,regs)):
            results.append(res)
            progress.progress((i+1)/len(regs))

    df_out=pd.DataFrame(results)[['Registration','AirTeamImages','ATI_Link','V1Images','V1_Link']]
    st.dataframe(df_out)
    buf=io.BytesIO()
    with pd.ExcelWriter(buf,engine='xlsxwriter') as w:
        df_out.to_excel(w,index=False,sheet_name='Results')
        wb,ws=w.book,w.sheets['Results']
        fmt=wb.add_format({'bg_color':'#C6EFCE'}); linkfmt=wb.add_format({'font_color':'blue','underline':True})
        if check_ati:
            ws.conditional_format(f'B2:B{len(df_out)+1}',{'type':'cell','criteria':'==','value':True,'format':fmt})
            for r,link in enumerate(df_out['ATI_Link'],start=1):
                if link: ws.write_url(r,2,link,linkfmt,'View ATI')
        if check_v1:
            ws.conditional_format(f'D2:D{len(df_out)+1}',{'type':'cell','criteria':'==','value':True,'format':fmt})
            for r,link in enumerate(df_out['V1_Link'],start=1):
                if link: ws.write_url(r,4,link,linkfmt,'View V1')
    buf.seek(0)
    st.download_button("Download Excel",data=buf,file_name='photo_availability.xlsx',mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
