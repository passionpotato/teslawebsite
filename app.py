# app.py — Tesla One-Stop (FREE) + X 임베드/폴링
import time
import re
from datetime import datetime
from typing import List, Dict, Optional
import os

import pandas as pd
import requests
import yfinance as yf
import feedparser
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from xml.etree import ElementTree as ET

# ---------------------------
# Streamlit Config
# ---------------------------
st.set_page_config(page_title="Tesla One-Stop (Free)", page_icon="🚗", layout="wide")

# ---------------------------
# Constants & Settings
# ---------------------------
TSLA = "TSLA"
CUSIP_TSLA_PREFIX = "88160R"  # TSLA CUSIP prefix
INTRADAY = {"1m", "2m", "5m", "15m", "30m", "60m", "90m"}

# 기본 추적 기관(CIK)
DEFAULT_CIKS = {
    "BlackRock Inc.": "0001364742",
    "The Vanguard Group, Inc.": "0000102909",
    "FMR LLC (Fidelity)": "0000315066",
    "State Street Corp.": "0000093751",
    "ARK Investment Management LLC": "0001697747",
    "T. Rowe Price Associates, Inc.": "0000080255",
}

RSS_SOURCES = {
    "Tesla IR (Press)": "https://ir.tesla.com/press-releases/rss",
    "Electrek – Tesla": "https://electrek.co/guides/tesla/feed/",
    "Reuters – Tesla": "https://feeds.reuters.com/reuters/businessNews?query=tesla",
    "CNBC – Tesla": "https://www.cnbc.com/id/15839135/device/rss/rss.html?query=tesla",
    "Google News – Tesla": "https://news.google.com/rss/search?q=Tesla%20OR%20TSLA&hl=en-US&gl=US&ceid=US:en",
}

# X(트위터) 핸들/ID
X_USERNAMES = {
    "Elon Musk": "elonmusk",
    "Donald Trump": "realDonaldTrump",
    "Tesla": "Tesla",
}
# API 호출 수 절감용: 잘 알려진 고정 user_id 하드코딩(없으면 자동 조회)
X_USER_IDS = {
    "elonmusk": "44196397",
    "realDonaldTrump": "25073877",
    "Tesla": "13298072",
}

# ---- SEC base (무료 13F) ----
SEC_BASE = "https://data.sec.gov"
# ⚠️ 꼭 본인 이메일로 교체하세요(SEC 권장)
SEC_UA = {"User-Agent": "TeslaDash/1.0 (your-email@example.com)"}

# X API (선택)
def get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)  # secrets.toml이 없으면 예외가 날 수 있음
    except Exception:
        return os.getenv(key, default) or default

X_BEARER = get_secret("X_BEARER_TOKEN", "")  # ⬅️ 기존 st.secrets.get(...) 대신 이걸 사용

# ---------------------------
# Utils — Price (Yahoo + Stooq fallback)
# ---------------------------
@st.cache_data(ttl=120)
def safe_yf_download(symbol: str, period: str, interval: str):
    """
    야후 규칙에 맞게 period/interval 자동 보정 + 세션 UA 주입 + 재시도 + Stooq 일봉 백업
    반환: (df, (used_period, used_interval), note|None)
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    combos = [(period, interval)]

    # 보정 후보 추가
    if interval == "1m" and period not in {"1d", "5d", "7d"}:
        combos.append(("5d", "1m"))
    if interval in INTRADAY and period not in {"1d", "5d", "7d", "1mo", "3mo", "6mo", "60d", "90d"}:
        combos.append(("1mo", "5m"))
    combos.append(("1y", "1d"))  # 최후의 야후 데일리 요청

    last_err = None
    for p, i in combos:
        try:
            df = yf.download(
                symbol, period=p, interval=i,
                auto_adjust=False, prepost=False, progress=False, session=session
            )
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            if not df.empty:
                return df, (p, i), None
        except Exception as e:
            last_err = str(e)

    # Yahoo 실패 시 Stooq 일봉 백업
    try:
        stooq = pd.read_csv("https://stooq.com/q/d/l/?s=tsla.us&i=d")
        stooq.rename(columns={
            "Date": "Datetime",
            "Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"
        }, inplace=True)
        stooq["Datetime"] = pd.to_datetime(stooq["Datetime"])
        stooq.set_index("Datetime", inplace=True)
        stooq = stooq.dropna()
        if not stooq.empty:
            return stooq, ("stooq-daily", "1d"), "Yahoo empty → Stooq daily fallback"
    except Exception as e:
        last_err = f"Yahoo+Stooq failed: {e}"

    return pd.DataFrame(), None, last_err

def plot_candles(df: pd.DataFrame, title: str):
    if df.empty:
        st.error("차트 데이터가 비어 있습니다.")
        return
    fig = go.Figure([go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"], name="Price"
    )])
    if "Volume" in df.columns:
        fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="Volume", yaxis="y2", opacity=0.3))
        fig.update_layout(
            yaxis=dict(domain=[0.3, 1.0], title="Price"),
            yaxis2=dict(domain=[0.0, 0.25], title="Volume"),
            xaxis=dict(title="Time"),
            title=title, height=600, margin=dict(l=10, r=10, t=40, b=10)
        )
    else:
        fig.update_layout(title=title, height=550, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------
# Utils — RSS
# ---------------------------
@st.cache_data(ttl=300)
def fetch_rss(feed_url: str, limit: int = 12) -> List[Dict]:
    parsed = feedparser.parse(feed_url)
    items = []
    for e in parsed.entries[:limit]:
        items.append({
            "title": e.get("title"),
            "link": e.get("link"),
            "published": e.get("published", ""),
            "summary": re.sub("<.*?>", "", e.get("summary", "")) if e.get("summary") else "",
        })
    return items

# ---------------------------
# Utils — SEC 13F (무료)
# ---------------------------
def _strip_xml_ns(xml_text: str) -> str:
    # 네임스페이스 제거 (파싱 호환성↑)
    xml_text = re.sub(r'\sxmlns(:\w+)?="[^"]+"', '', xml_text)
    return xml_text

def _to_int(x):
    try:
        return int(str(x).replace(",", "").strip())
    except Exception:
        return None

@st.cache_data(ttl=3600)
def sec_recent_filings(cik: str) -> pd.DataFrame:
    cik10 = str(cik).zfill(10)
    url = f"{SEC_BASE}/submissions/CIK{cik10}.json"
    r = requests.get(url, headers=SEC_UA, timeout=20)
    r.raise_for_status()
    js = r.json()
    rec = js.get("filings", {}).get("recent", {})
    df = pd.DataFrame(rec)
    return df

def _acc_nodash(acc: str) -> str:
    return acc.replace("-", "")

@st.cache_data(ttl=3600)
def sec_list_13f_accessions(cik: str, limit=3) -> List[Dict]:
    df = sec_recent_filings(cik)
    if df.empty:
        return []
    mask = df["form"].isin(["13F-HR", "13F-HR/A"])
    sdf = df[mask].head(limit)
    rows = []
    for _, r in sdf.iterrows():
        rows.append({
            "cik": cik,
            "accession": r["accessionNumber"],
            "reportDate": r.get("reportDate", ""),
            "primaryDocument": r.get("primaryDocument", ""),
        })
    return rows

@st.cache_data(ttl=3600)
def sec_find_infotable_url(cik: str, accession: str) -> Optional[str]:
    cik_nozero = str(int(cik))
    acc_nodash = _acc_nodash(accession)
    idx = f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc_nodash}/index.json"
    r = requests.get(idx, headers=SEC_UA, timeout=20)
    if not r.ok:
        return None
    files = r.json().get("directory", {}).get("item", [])
    # XML 우선
    for f in files:
        name = f.get("name", "").lower()
        if name.endswith(".xml") and ("infotable" in name or "informationtable" in name or "form13f" in name):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc_nodash}/{f['name']}"
    # TXT 백업 (XML이 txt 내부에 포함된 케이스)
    for f in files:
        name = f.get("name", "").lower()
        if name.endswith(".txt"):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc_nodash}/{f['name']}"
    return None

def _parse_infotable_xml(xml_text: str) -> pd.DataFrame:
    # TXT 안의 <informationTable>만 추출
    m = re.search(r"<informationTable[\s\S]*</informationTable>", xml_text, re.IGNORECASE)
    if m:
        xml_text = m.group(0)
    xml_text = _strip_xml_ns(xml_text)
    root = ET.fromstring(xml_text.encode("utf-8"))
    rows = []
    for it in root.iterfind(".//infoTable"):
        issuer = (it.findtext("nameOfIssuer", default="") or "").strip()
        cusip = (it.findtext("cusip", default="") or "").strip()
        # shares
        amt = it.find(".//shrsOrPrnAmt/sshPrnamt")
        shares = _to_int(amt.text) if amt is not None and amt.text else None
        # value (thousands USD)
        val = _to_int(it.findtext("value"))
        value_usd = val * 1000 if val is not None else None
        rows.append({"issuer": issuer, "cusip": cusip, "shares": shares, "value_usd": value_usd})
    return pd.DataFrame(rows)

@st.cache_data(ttl=3600)
def sec_tsla_position_from_13f(cik: str, accession: str) -> Optional[Dict]:
    url = sec_find_infotable_url(cik, accession)
    if not url:
        return None
    r = requests.get(url, headers=SEC_UA, timeout=30)
    if not r.ok or not r.text:
        return None
    try:
        df = _parse_infotable_xml(r.text)
    except Exception:
        return None
    if df.empty:
        return {"shares": 0, "value_usd": 0}
    m = df[(df["cusip"].str.startswith(CUSIP_TSLA_PREFIX, na=False)) |
           (df["issuer"].str.contains("TESLA", case=False, na=False))]
    if m.empty:
        return {"shares": 0, "value_usd": 0}
    return {
        "shares": int(m["shares"].fillna(0).sum()),
        "value_usd": int(m["value_usd"].fillna(0).sum())
    }

def build_13f_table(managers: Dict[str, str]) -> pd.DataFrame:
    out = []
    for name, cik in managers.items():
        try:
            accs = sec_list_13f_accessions(cik, limit=2)
            if not accs:
                continue
            latest = accs[0]
            prev = accs[1] if len(accs) > 1 else None
            latest_pos = sec_tsla_position_from_13f(cik, latest["accession"])
            time.sleep(0.4)  # SEC 예의상
            prev_pos = sec_tsla_position_from_13f(cik, prev["accession"]) if prev else None

            shares = latest_pos["shares"] if latest_pos else None
            value_usd = latest_pos["value_usd"] if latest_pos else None
            delta = None
            if latest_pos and prev_pos:
                delta = (latest_pos["shares"] or 0) - (prev_pos["shares"] or 0)

            out.append({
                "기관/펀드": name,
                "CIK": cik,
                "보고일(최근)": latest.get("reportDate", ""),
                "보유주수(최근)": shares,
                "평가액(USD, 최근)": value_usd,
                "보유주수 증감(qoq)": delta
            })
        except Exception:
            continue
    df = pd.DataFrame(out)
    if not df.empty:
        df = df.sort_values(by=["보유주수(최근)"], ascending=False)
    return df

# ---------------------------
# Utils — X API (선택, 폴링)
# ---------------------------
def _x_headers():
    if not X_BEARER:
        return None
    return {"Authorization": f"Bearer {X_BEARER}"}

def _x_api_get(url, params=None, timeout=15):
    """
    api.x.com -> 실패 시 api.twitter.com도 시도(일부 환경 호환)
    """
    h = _x_headers()
    if not h:
        return None
    for base in ("https://api.x.com", "https://api.twitter.com"):
        try:
            r = requests.get(base + url, headers=h, params=params, timeout=timeout)
            if r.ok:
                return r.json()
        except Exception:
            pass
    return None

@st.cache_data(ttl=24*3600)  # 하루 캐시
def x_get_user_id(username: str) -> Optional[str]:
    # 하드코딩 우선 사용
    if username in X_USER_IDS:
        return X_USER_IDS[username]
    js = _x_api_get(f"/2/users/by/username/{username}")
    if not js:
        return None
    return js.get("data", {}).get("id")

@st.cache_data(ttl=0)  # 폴링 주기마다 새 호출
def x_fetch_latest_tweets(user_id: str, since_id: Optional[str] = None, max_results: int = 5):
    params = {
        "max_results": str(max_results),
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,public_metrics,entities",
    }
    if since_id:
        params["since_id"] = since_id
    js = _x_api_get(f"/2/users/{user_id}/tweets", params=params)
    if not js:
        return [], since_id
    data = js.get("data", [])
    data.sort(key=lambda t: t.get("id"), reverse=False)
    new_since = data[-1]["id"] if data else since_id
    return data, new_since

def _format_tweet_text(t):
    txt = t.get("text", "")
    ents = (t.get("entities") or {}).get("urls", []) or []
    for url in ents:
        u = url.get("url"); ex = url.get("expanded_url") or u
        if u:
            txt = txt.replace(u, ex)
    return txt

# ---------------------------
# UI
# ---------------------------
st.sidebar.title("Tesla One-Stop (Free)")
page = st.sidebar.radio("메뉴", ["📈 차트", "📰 뉴스/코멘트", "🏦 지분 변동(13F, 무료)", "⚙️ 옵션/설정"])

# ---- Chart ----
if page == "📈 차트":
    st.title("📈 TSLA 차트 (안정화 버전)")
    c1, c2, c3 = st.columns(3)
    with c1:
        period = st.selectbox("기간", ["1d", "5d", "7d", "1mo", "3mo", "6mo", "1y"], index=0)
    with c2:
        interval = st.selectbox("봉 간격", ["1m", "5m", "15m", "1h", "1d"], index=0)
    with c3:
        if st.button("캐시 초기화", help="yfinance가 빈 값을 캐시에 저장했을 때 유용"):
            st.cache_data.clear()
            st.success("캐시 삭제 완료. 다시 불러오는 중…")

    df, used, note = safe_yf_download(TSLA, period, interval)
    if note:
        st.info(note)
    if used:
        st.caption(f"실제 요청: period={used[0]}, interval={used[1]}")

    if df.empty:
        st.error("데이터를 불러오지 못했습니다. 네트워크/회사망 차단 여부를 확인하거나 다른 조합을 선택해 보세요.")
    else:
        plot_candles(df, f"TSLA {used[0]}/{used[1]}")
        if "Close" in df.columns and len(df) > 1:
            last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
            st.metric("지연 현재가(소스 자동)", f"${last:,.2f}", f"{(last-prev):+.2f}")

# ---- News & Comments ----
elif page == "📰 뉴스/코멘트":
    st.title("📰 뉴스 & 코멘트")
    t1, t2, t3 = st.tabs(["뉴스 RSS", "유명인 코멘트 (임베드, 자동 새로고침)", "실시간 피드 (X API)"])

    # RSS
    with t1:
        cols = st.columns(2)
        keys = list(RSS_SOURCES.keys())
        left_keys, right_keys = keys[: (len(keys)+1)//2], keys[(len(keys)+1)//2 :]
        with cols[0]:
            for k in left_keys:
                st.subheader(k)
                for it in fetch_rss(RSS_SOURCES[k], limit=7):
                    st.markdown(f"- **[{it['title']}]({it['link']})**")
                    if it["published"]:
                        st.caption(it["published"])
                st.markdown("---")
        with cols[1]:
            for k in right_keys:
                st.subheader(k)
                for it in fetch_rss(RSS_SOURCES[k], limit=7):
                    st.markdown(f"- **[{it['title']}]({it['link']})**")
                    if it["published"]:
                        st.caption(it["published"])
                st.markdown("---")

    # X 임베드 + 자동 새로고침
    with t2:
        acct_label = st.selectbox("계정 선택", list(X_USERNAMES.keys()), key="embed_acct")
        handle = X_USERNAMES[acct_label]
        col_l, col_r = st.columns([1,3])
        with col_l:
            refresh_sec = st.slider("새로고침(초)", 15, 180, 60, step=15, help="타임라인을 주기적으로 다시 로드합니다.")
        st_autorefresh(interval=refresh_sec * 1000, key=f"x_refresh_{handle}")
        embed_html = f"""
        <a class="twitter-timeline" href="https://twitter.com/{handle}?ref_src=twsrc%5Etfw">
          Tweets by @{handle}
        </a>
        <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
        """
        st.components.v1.html(embed_html, height=800, scrolling=True)

    # X API 폴링 (선택)
    with t3:
        st.caption("선택 기능: `.streamlit/secrets.toml`에 X_BEARER_TOKEN 설정 필요")
        if not X_BEARER:
            st.info("X_BEARER_TOKEN이 비어 있습니다. 시크릿에 토큰을 추가하세요.")
        else:
            acct_username = st.selectbox("계정 선택", list(X_USERNAMES.values()), key="api_acct")
            # user_id: 하드코딩 우선, 없으면 API 조회
            user_id = X_USER_IDS.get(acct_username) or x_get_user_id(acct_username)
            if not user_id:
                st.error("유저 ID를 찾을 수 없습니다.")
            else:
                refresh_sec = st.slider("폴링 주기(초)", 20, 120, 45, step=5)
                st_autorefresh(interval=refresh_sec*1000, key=f"poll_{acct_username}")

                key_sid = f"since_{acct_username}"
                since_id = st.session_state.get(key_sid)

                tweets, new_since = x_fetch_latest_tweets(user_id, since_id=since_id, max_results=5)
                if new_since and new_since != since_id:
                    st.session_state[key_sid] = new_since

                if not tweets:
                    st.write("새 트윗이 아직 없습니다.")
                else:
                    for t in reversed(tweets):  # 최신이 위로 오도록
                        created = t.get("created_at", "")[:19].replace("T", " ")
                        text = _format_tweet_text(t)
                        url = f"https://twitter.com/{acct_username}/status/{t['id']}"
                        st.markdown(f"**@{acct_username}** · {created}  \n{text}\n\n[원문 보기]({url})")
                        st.markdown("---")

# ---- 13F (FREE) ----
elif page == "🏦 지분 변동(13F, 무료)":
    st.title("🏦 기관 보유/변동 — SEC 13F (완전 무료)")
    st.caption("분기 공시(13F-HR) 원문을 SEC EDGAR에서 파싱, 최신/직전 분기를 비교해 TSLA 보유 변화 계산")

    with st.expander("대상 기관(편집 가능)"):
        edit_df = pd.DataFrame([{"기관/펀드": k, "CIK": v} for k, v in DEFAULT_CIKS.items()])
        edited = st.data_editor(edit_df, num_rows="dynamic", key="mgr_edit")
        managers = {row["기관/펀드"]: str(row["CIK"]) for _, row in edited.iterrows()
                    if row.get("기관/펀드") and row.get("CIK")}
        st.caption("추가/삭제 후 아래 표는 새로고침 시 반영됩니다.")

    refresh = st.button("13F 새로 고침", type="primary")
    if refresh:
        st.cache_data.clear()

    with st.spinner("SEC에서 13F 불러오는 중..."):
        df = build_13f_table(managers)

    if df.empty:
        st.warning("데이터를 가져오지 못했습니다. CIK가 맞는지 확인하거나 잠시 후 재시도하세요.")
    else:
        fmt = df.copy()
        if "보유주수(최근)" in fmt:
            fmt["보유주수(최근)"] = fmt["보유주수(최근)"].map(lambda x: f"{x:,}" if pd.notna(x) else "")
        if "평가액(USD, 최근)" in fmt:
            fmt["평가액(USD, 최근)"] = fmt["평가액(USD, 최근)"].map(lambda x: f"${x:,}" if pd.notna(x) else "")
        if "보유주수 증감(qoq)" in fmt:
            fmt["보유주수 증감(qoq)"] = fmt["보유주수 증감(qoq)"].map(lambda x: f"{x:+,}" if pd.notna(x) else "")
        st.dataframe(fmt, use_container_width=True)

    st.info("참고: 13F는 **분기 단위** 공개이며, 일중·일일 변동을 실시간으로 제공하지 않습니다.")

# ---- Settings ----
elif page == "⚙️ 옵션/설정":
    st.title("⚙️ 옵션/설정")
    st.markdown("""
    - 본 앱은 **무료 소스**(Yahoo Finance, RSS, SEC EDGAR)를 기본으로 사용합니다.
    - 더 나은 실시간 시세가 필요하면 Alpha Vantage(무료 키), Polygon/IEX/Finnhub(유료/무료 혼합)를 고려하세요.
    - SEC 요청은 User-Agent에 **연락 가능한 이메일** 표기를 권장합니다. `your-email@example.com`을 본인 주소로 바꾸세요.
    - X API 폴링은 선택 기능입니다. `.streamlit/secrets.toml`에 `X_BEARER_TOKEN`을 넣으면 활성화됩니다.
    """)
