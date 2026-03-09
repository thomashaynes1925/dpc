import streamlit as st
import pandas as pd
import re
import io
import time
import random
import bcrypt
import threading
from urllib.parse import quote_plus
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# -----------------------------
# App + Auth
# -----------------------------
st.set_page_config(page_title="Photo Checker", layout="wide")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

creds = st.secrets.get("credentials", {})  # expects { "username": "<bcrypt_hash_string>" }

if not st.session_state.authenticated:
    st.title("Login to Photo Checker")
    with st.form(key="login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login")
        if submitted:
            hash_val = creds.get(username)
            if hash_val and bcrypt.checkpw(password.encode("utf-8"), hash_val.encode("utf-8")):
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Invalid username or password")
    st.stop()

st.title("Photo Checker")

# -----------------------------
# Networking (thread-safe sessions)
# -----------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=50, pool_maxsize=50)
    s.mount("http://", adapter)
    s.mount("https://", adapter)

    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Connection": "keep-alive",
        }
    )
    return s


_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = make_session()
    return _thread_local.session


# -----------------------------
# Registration parsing
# -----------------------------
REG_PATTERN = re.compile(r"\b(?:[A-Z0-9]{1,3}-[A-Z0-9]{1,5}|N\d{1,5}[A-Z]{0,2})\b")


def extract_regs_from_text(text: str) -> list[str]:
    if not text:
        return []
    matches = REG_PATTERN.findall(text.upper())
    return sorted(set(matches))


def extract_regs_from_df(df: pd.DataFrame, col) -> list[str]:
    regs = set()
    series = df.iloc[:, col] if isinstance(col, int) else df[col]
    for txt in series.astype(str):
        for m in REG_PATTERN.findall(str(txt).upper()):
            regs.add(m)
    return sorted(regs)


# -----------------------------
# Search functions
# -----------------------------
def _polite_delay(min_ms: int, max_ms: int):
    time.sleep(random.uniform(min_ms / 1000.0, max_ms / 1000.0))


def search_airteam(reg: str, timeout_s: int, min_delay_ms: int, max_delay_ms: int):
    """
    Returns: (found: bool, link: str, debug_flag: str)
    """
    _polite_delay(min_delay_ms, max_delay_ms)
    s = get_session()
    url = f"https://www.airteamimages.com/search?q={quote_plus(reg)}&sort=id%2Cdesc"

    try:
        r = s.get(url, timeout=timeout_s)
        html = r.text or ""
    except Exception as e:
        return False, "", f"ATI request error: {e}"

    if r.status_code >= 400:
        return False, "", f"ATI HTTP {r.status_code}"

    block_markers = [
        "captcha",
        "cloudflare",
        "access denied",
        "forbidden",
        "blocked",
        "verify you are human",
        "attention required",
    ]
    if any(m in html.lower() for m in block_markers):
        return False, "", "ATI possible block page"

    has_reg = reg.upper() in html.upper()
    has_result_signal = bool(
        re.search(r"Image ID:\s*\d+|View Large Photo|Loading Images", html, re.IGNORECASE)
    )

    if has_reg and has_result_signal:
        return True, url, ""

    if re.search(r"Image ID:\s*\d+", html, re.IGNORECASE):
        return True, url, "ATI matched Image ID but registration text not found"

    return False, "", ""


# -----------------------------
# Upload + settings
# -----------------------------
uploaded = st.file_uploader("Upload file (.xls, .xlsx, .csv, .txt)", type=["xls", "xlsx", "csv", "txt"])
if not uploaded:
    st.info("Please upload a file to proceed.")
    st.stop()

ext = uploaded.name.split(".")[-1].lower()

with st.expander("Advanced Settings", expanded=False):
    if ext in ["xls", "xlsx"]:
        sheet = st.text_input("Excel sheet name", "ExportedData")
        col_input = st.text_input("Excel column (name or 1-based index)", "1")
    elif ext == "csv":
        uploaded.seek(0)
        df_temp = pd.read_csv(uploaded, nrows=0)
        uploaded.seek(0)
        default_col = df_temp.columns[0] if len(df_temp.columns) else ""
        col_input = st.text_input("CSV column name", default_col)
        sheet = None
    else:
        sheet = None
        col_input = None

    workers = st.slider("Parallel workers", 1, 20, 8)
    timeout_s = st.slider("Request timeout (seconds)", 5, 60, 12)

    st.markdown("**Rate limiting (helps from AWS / avoids soft-blocks):**")
    min_delay_ms = st.slider("Minimum delay per request (ms)", 0, 1000, 50, step=25)
    max_delay_ms = st.slider("Maximum delay per request (ms)", 0, 2000, 200, step=25)


def load_regs() -> list[str]:
    uploaded.seek(0)
    if ext == "txt":
        text = uploaded.getvalue().decode("utf-8", errors="ignore")
        return extract_regs_from_text(text)

    if ext == "csv":
        df = pd.read_csv(uploaded, dtype=str)
        if not col_input or col_input not in df.columns:
            st.error(f"Column '{col_input}' not found in CSV.")
            st.stop()
        return extract_regs_from_df(df, col_input)

    try:
        df = pd.read_excel(uploaded, sheet_name=sheet, dtype=str)
    except Exception as e:
        st.error(f"Failed to read Excel: {e}")
        st.stop()

    if not col_input:
        st.error("Please specify an Excel column (name or 1-based index).")
        st.stop()

    col_idx = int(col_input) - 1 if str(col_input).strip().isdigit() else col_input
    try:
        return extract_regs_from_df(df, col_idx)
    except Exception as e:
        st.error(f"Failed to extract registrations from the chosen column: {e}")
        st.stop()


regs = load_regs()
if not regs:
    st.warning("No registrations were found in the uploaded file.")
    st.stop()

st.success(f"Loaded {len(regs)} registrations.")

# -----------------------------
# Optional quick debug
# -----------------------------
with st.expander("Debug tools", expanded=False):
    test_reg = st.text_input("Test registration", "A7-BHY")

    if st.button("Test ATI from this server"):
        s = get_session()
        ati_url = f"https://www.airteamimages.com/search?q={quote_plus(test_reg)}&sort=id%2Cdesc"

        try:
            r = s.get(ati_url, timeout=timeout_s)
            html_preview = (r.text or "")[:5000]
            found, link, dbg = search_airteam(test_reg, timeout_s, min_delay_ms, max_delay_ms)

            st.subheader("AirTeamImages")
            st.write("Found:", found)
            st.write("Link:", link if link else "(none)")
            st.write("Note:", dbg if dbg else "(none)")
            st.write("HTTP status:", r.status_code)
            st.write("Final URL:", r.url)
            st.code(html_preview)
        except Exception as e:
            st.error(f"ATI debug request failed: {e}")


# -----------------------------
# Run checks
# -----------------------------
if st.button("Run Checks"):
    st.write(f"Starting checks for {len(regs)} registrations…")
    progress = st.progress(0.0)
    status = st.empty()

    results = []
    debug_notes = []

    def check_entry(reg: str):
        entry = {"Registration": reg}
        notes = []

        ok, link, dbg = search_airteam(reg, timeout_s, min_delay_ms, max_delay_ms)
        entry["AirTeamImages"] = ok
        entry["ATI_Link"] = link
        if dbg:
            notes.append(f"ATI: {dbg}")

        return entry, (reg, "; ".join(notes) if notes else "")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(check_entry, reg) for reg in regs]
        total = len(futures)

        for i, fut in enumerate(as_completed(futures), start=1):
            entry, note = fut.result()
            results.append(entry)
            if note[1]:
                debug_notes.append(note)

            progress.progress(i / total)
            if i % 10 == 0 or i == total:
                status.write(f"Checked {i}/{total}")

    cols = ["Registration", "AirTeamImages", "ATI_Link"]

    df_out = pd.DataFrame(results)
    df_out = df_out[cols].sort_values("Registration").reset_index(drop=True)

    st.subheader("Results")
    st.dataframe(df_out, use_container_width=True)

    if debug_notes:
        with st.expander("Request notes (possible blocks/errors)", expanded=False):
            st.write(pd.DataFrame(debug_notes, columns=["Registration", "Note"]))

    # -----------------------------
    # Write Excel with formatting + links
    # -----------------------------
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="xlsxwriter") as writer:
        df_out.to_excel(writer, index=False, sheet_name="Results")
        wb = writer.book
        ws = writer.sheets["Results"]

        green = wb.add_format({"bg_color": "#C6EFCE"})
        link_fmt = wb.add_format({"font_color": "blue", "underline": True})

        header_to_col = {name: idx for idx, name in enumerate(df_out.columns)}

        col_letter = chr(ord("A") + header_to_col["AirTeamImages"])
        ws.conditional_format(
            f"{col_letter}2:{col_letter}{len(df_out)+1}",
            {"type": "cell", "criteria": "==", "value": True, "format": green},
        )

        link_col = header_to_col["ATI_Link"]
        for r, link in enumerate(df_out["ATI_Link"], start=1):
            if link:
                ws.write_url(r, link_col, link, link_fmt, "View ATI")

        for i, col_name in enumerate(df_out.columns):
            max_len = max([len(str(col_name))] + [len(str(x)) for x in df_out[col_name].fillna("").astype(str).head(200)])
            ws.set_column(i, i, min(max_len + 2, 45))

    buffer.seek(0)

    st.download_button(
        "Download Results as Excel",
        data=buffer,
        file_name="photo_availability.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
