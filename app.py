# app.py — Tesla One-Stop (FREE) + YouTube 라이브/최신 영상
import os
import time
import re
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import pandas as pd
import requests
import yfinance as yf
import feedparser
import plotly.graph_objects as go
import streamlit as st
from xml.etree import ElementTree as ET

st.set_page_config(page_title="Tesla One-Stop (Free)", page_icon="🚗", layout="wide")

# ---------------------------
# 공통 설정/상수
# ---------------------------
TSLA = "TSLA"
CUSIP_TSLA_PREFIX = "88160R"
INTRADAY = {"1m","2m","5m","15m","30m","60m","90m"}

# 기본 기관 CIK (13F)
DEFAULT_CIKS = {
    "BlackRock Inc.": "0001364742",
    "The Vanguard Group, Inc.": "0000102909",
    "FMR LLC (Fidelity)": "0000315066",
    "State Street Corp.": "0000093751",
    "ARK Investment Management LLC": "0001697747",
    "T. Rowe Price Associates, Inc.": "0000080255",
}

# 뉴스 RSS
RSS_SOURCES = {
    "Tesla IR (Press)": "https://ir.tesla.com/press-releases/rss",
    "Electrek – Tesla": "https://electrek.co/guides/tesla/feed/",
    "Reuters – Tesla": "https://feeds.reuters.com/reuters/businessNews?query=tesla",
    "CNBC – Tesla": "https://www.cnbc.com/id/15839135/device/rss/rss.html?query=tesla",
    "Google News – Tesla": "https://news.google.com/rss/search?q=Tesla%20OR%20TSLA&hl=en-US&gl=US&ceid=US:en",
}

# X(트위터) 계정
X_USERNAMES = {
    "Elon Musk": "elonmusk",
    "Donald Trump": "realDonaldTrump",
    "Tesla": "Tesla",
}
X_USER_IDS = {  # 호출 절약용 하드코딩
    "elonmusk": "44196397",
    "realDonaldTrump": "25073877",
    "Tesla": "13298072",
}

# 유튜브 채널 (채널ID는 UI에서 수정 가능; 여기 기본 예시/자리표시)
DEFAULT_YT_CHANNELS = [
    {"채널명": "오늘의 테슬라 뉴스", "channel_id": "UCXq7NNALDnqafn3KFvIyJKA"},
    {"채널명": "마피디 미국주식", "channel_id": "UCjp7GHSUKx9Joji3tIz1jCg"},
    {"채널명": "엔지니어TV", "channel_id": "UCnvCYORRLMYMQNhrlBILdCg"},
    {"채널명": "올랜도 킴 미국주식", "channel_id": "UCwSSqi-s0wcH6pJbH3YPZqQ"},
]

# SEC
SEC_BASE = "https://data.sec.gov"
SEC_UA = {"User-Agent": "TeslaDash/1.0 (your-email@example.com)"}  # 본인 이메일로 교체 권장

# 안전한 시크릿 접근 (secrets.toml 없을 때도 안전)
def get_secret(key: str, default: str = "") -> str:
    try:
        return st.secrets.get(key, default)
    except Exception:
        return os.getenv(key, default) or default

X_BEARER = get_secret("X_BEARER_TOKEN", "")
YOUTUBE_API_KEY = get_secret("YOUTUBE_API_KEY", "")

# ---------------------------
# 페이지 자동 새로고침(JS, 추가 의존성 無)
# ---------------------------
def auto_refresh_html(seconds: int, key: str):
    ms = int(seconds * 1000)
    st.components.v1.html(
        f"""
        <script>
        (function(){{
            const KEY="{key}";
            if(!window.__autoRefreshTimers) window.__autoRefreshTimers = {{}};
            if(!window.__autoRefreshTimers[KEY]) {{
                window.__autoRefreshTimers[KEY] = setTimeout(function(){{
                    const url = new URL(window.location.href);
                    url.searchParams.set(KEY, Date.now().toString());
                    window.location.href = url.toString();
                }}, {ms});
            }}
        }})();
        </script>
        """, height=0
    )

# ---------------------------
# 가격/차트 (Yahoo + Stooq fallback)
# ---------------------------
@st.cache_data(ttl=120)
def safe_yf_download(symbol: str, period: str, interval: str):
    """
    강인한 가격 수집:
    1) yfinance.Ticker().history (권장)
    2) yfinance.download (threads=False)
    3) interval/period 자동 보정
    4) Stooq 일봉 최종 백업
    """
    import requests, pandas as pd, yfinance as yf
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # 🔧 간격 보정: 일부 환경에서 '1h' 대신 '60m'가 안정적
    interval_fixed = {"1h": "60m"}.get(interval, interval)

    # 요청 후보(우선순위)
    combos = [(period, interval_fixed)]
    if interval_fixed == "1m" and period not in {"1d","5d","7d"}:
        combos.append(("5d","1m"))
    if interval_fixed in INTRADAY and period not in {"1d","5d","7d","1mo","3mo","6mo","60d","90d"}:
        combos.append(("1mo","5m"))
    combos.append(("1y","1d"))  # 최후의 야후 데일리

    last_err = None

    # 1) Ticker().history (가장 안정)
    try:
        tkr = yf.Ticker(symbol, session=session)
        df = tkr.history(period=period, interval=interval_fixed, prepost=False, auto_adjust=False)
        if not df.empty:
            return df, (period, interval_fixed), "yfinance.history"
    except Exception as e:
        last_err = f"history: {e}"

    # 2) download 재시도 (threads=False가 오류 줄임)
    for p, i in combos:
        try:
            df = yf.download(
                symbol, period=p, interval=i,
                auto_adjust=False, prepost=False,
                progress=False, threads=False, session=session
            )
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            if not df.empty:
                return df, (p, i), "yfinance.download"
        except Exception as e:
            last_err = f"download {p}/{i}: {e}"

    # 3) Stooq 일봉 백업
    try:
        import pandas as pd
        stooq = pd.read_csv("https://stooq.com/q/d/l/?s=tsla.us&i=d")
        stooq.rename(columns={"Date":"Datetime"}, inplace=True)
        stooq["Datetime"] = pd.to_datetime(stooq["Datetime"])
        stooq.set_index("Datetime", inplace=True)
        stooq = stooq.dropna()
        if not stooq.empty:
            return stooq, ("stooq-daily","1d"), "Yahoo empty → Stooq daily fallback"
    except Exception as e:
        last_err = f"Stooq: {e}"

    # 모두 실패
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
            yaxis=dict(domain=[0.3,1.0], title="Price"),
            yaxis2=dict(domain=[0.0,0.25], title="Volume"),
            xaxis=dict(title="Time"),
            title=title, height=600, margin=dict(l=10, r=10, t=40, b=10)
        )
    else:
        fig.update_layout(title=title, height=550, margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

# ---------------------------
# 뉴스 RSS
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
            "summary": re.sub("<.*?>","", e.get("summary","")) if e.get("summary") else "",
        })
    return items

# ---------------------------
# SEC 13F (무료)
# ---------------------------
def _strip_xml_ns(xml_text: str) -> str:
    return re.sub(r'\sxmlns(:\w+)?="[^"]+"','', xml_text)

def _to_int(x):
    try:
        return int(str(x).replace(",","").strip())
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
    return pd.DataFrame(rec)

def _acc_nodash(acc: str) -> str:
    return acc.replace("-","")

@st.cache_data(ttl=3600)
def sec_list_13f_accessions(cik: str, limit=3) -> List[Dict]:
    df = sec_recent_filings(cik)
    if df.empty: return []
    mask = df["form"].isin(["13F-HR","13F-HR/A"])
    sdf = df[mask].head(limit)
    rows = []
    for _, r in sdf.iterrows():
        rows.append({
            "cik": cik,
            "accession": r["accessionNumber"],
            "reportDate": r.get("reportDate",""),
            "primaryDocument": r.get("primaryDocument",""),
        })
    return rows

@st.cache_data(ttl=3600)
def sec_find_infotable_url(cik: str, accession: str) -> Optional[str]:
    cik_nozero = str(int(cik)); acc = _acc_nodash(accession)
    idx = f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc}/index.json"
    r = requests.get(idx, headers=SEC_UA, timeout=20)
    if not r.ok: return None
    files = r.json().get("directory",{}).get("item",[])
    for f in files:
        name = f.get("name","").lower()
        if name.endswith(".xml") and ("infotable" in name or "informationtable" in name or "form13f" in name):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc}/{f['name']}"
    for f in files:
        name = f.get("name","").lower()
        if name.endswith(".txt"):
            return f"https://www.sec.gov/Archives/edgar/data/{cik_nozero}/{acc}/{f['name']}"
    return None

def _parse_infotable_xml(xml_text: str) -> pd.DataFrame:
    m = re.search(r"<informationTable[\s\S]*</informationTable>", xml_text, re.IGNORECASE)
    if m: xml_text = m.group(0)
    xml_text = _strip_xml_ns(xml_text)
    root = ET.fromstring(xml_text.encode("utf-8"))
    rows = []
    for it in root.iterfind(".//infoTable"):
        issuer = (it.findtext("nameOfIssuer","") or "").strip()
        cusip = (it.findtext("cusip","") or "").strip()
        amt = it.find(".//shrsOrPrnAmt/sshPrnamt")
        shares = _to_int(amt.text) if (amt is not None and amt.text) else None
        val = _to_int(it.findtext("value"))
        value_usd = val*1000 if val is not None else None
        rows.append({"issuer":issuer,"cusip":cusip,"shares":shares,"value_usd":value_usd})
    return pd.DataFrame(rows)

@st.cache_data(ttl=3600)
def sec_tsla_position_from_13f(cik: str, accession: str) -> Optional[Dict]:
    url = sec_find_infotable_url(cik, accession)
    if not url: return None
    r = requests.get(url, headers=SEC_UA, timeout=30)
    if not r.ok or not r.text: return None
    try:
        df = _parse_infotable_xml(r.text)
    except Exception:
        return None
    if df.empty:
        return {"shares":0, "value_usd":0}
    m = df[(df["cusip"].str.startswith(CUSIP_TSLA_PREFIX, na=False)) |
           (df["issuer"].str.contains("TESLA", case=False, na=False))]
    if m.empty:
        return {"shares":0, "value_usd":0}
    return {
        "shares": int(m["shares"].fillna(0).sum()),
        "value_usd": int(m["value_usd"].fillna(0).sum())
    }

def build_13f_table(managers: Dict[str, str]) -> pd.DataFrame:
    out = []
    for name, cik in managers.items():
        try:
            accs = sec_list_13f_accessions(cik, limit=2)
            if not accs: continue
            latest = accs[0]; prev = accs[1] if len(accs)>1 else None
            latest_pos = sec_tsla_position_from_13f(cik, latest["accession"])
            time.sleep(0.4)
            prev_pos = sec_tsla_position_from_13f(cik, prev["accession"]) if prev else None
            shares = latest_pos["shares"] if latest_pos else None
            value_usd = latest_pos["value_usd"] if latest_pos else None
            delta = None
            if latest_pos and prev_pos:
                delta = (latest_pos["shares"] or 0) - (prev_pos["shares"] or 0)
            out.append({
                "기관/펀드": name,
                "CIK": cik,
                "보고일(최근)": latest.get("reportDate",""),
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
# X API (선택, 이미 사용 중인 구조 유지)
# ---------------------------
def _x_headers():
    if not X_BEARER: return None
    return {"Authorization": f"Bearer {X_BEARER}"}

def _x_api_get(url, params=None, timeout=15):
    h = _x_headers()
    if not h: return None
    for base in ("https://api.x.com", "https://api.twitter.com"):
        try:
            r = requests.get(base+url, headers=h, params=params, timeout=timeout)
            if r.ok: return r.json()
        except Exception:
            pass
    return None

@st.cache_data(ttl=24*3600)
def x_get_user_id(username: str) -> Optional[str]:
    if username in X_USER_IDS: return X_USER_IDS[username]
    js = _x_api_get(f"/2/users/by/username/{username}")
    if not js: return None
    return js.get("data",{}).get("id")

@st.cache_data(ttl=0)
def x_fetch_latest_tweets(user_id: str, since_id: Optional[str]=None, max_results: int=5):
    params = {"max_results":str(max_results), "exclude":"retweets,replies",
              "tweet.fields":"created_at,public_metrics,entities"}
    if since_id: params["since_id"] = since_id
    js = _x_api_get(f"/2/users/{user_id}/tweets", params=params)
    if not js: return [], since_id
    data = js.get("data", [])
    data.sort(key=lambda t: t.get("id"), reverse=False)
    new_since = data[-1]["id"] if data else since_id
    return data, new_since

def _format_tweet_text(t):
    txt = t.get("text","")
    ents = (t.get("entities") or {}).get("urls", []) or []
    for url in ents:
        u = url.get("url"); ex = url.get("expanded_url") or u
        if u: txt = txt.replace(u, ex)
    return txt

# ---------------------------
# YouTube (RSS + Data API v3)
# ---------------------------
@st.cache_data(ttl=300)
def yt_rss_latest(channel_id: str, limit: int = 6):
    """API 키 없이 최신 영상 리스트"""
    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    parsed = feedparser.parse(feed)
    items = []
    for e in parsed.entries[:limit]:
        vid = e.get("yt_videoid")
        if not vid:
            # 링크에서 v 파라미터 추출
            try:
                q = parse_qs(urlparse(e.get("link","")).query)
                vid = q.get("v",[None])[0]
            except Exception:
                vid = None
        items.append({
            "video_id": vid,
            "title": e.get("title",""),
            "link": e.get("link",""),
            "published": e.get("published",""),
        })
    return items

@st.cache_data(ttl=60)
def yt_api_live_videos(channel_id: str, max_results: int = 3):
    """API 키가 있을 때 해당 채널의 라이브 중인 영상 검색"""
    if not YOUTUBE_API_KEY:
        return []
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "eventType": "live",
        "type": "video",
        "order": "date",
        "maxResults": str(max_results),
        "key": YOUTUBE_API_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    if not r.ok:
        return []
    data = r.json().get("items", [])
    out = []
    for it in data:
        vid = it.get("id",{}).get("videoId")
        sn = it.get("snippet",{})
        out.append({
            "video_id": vid,
            "title": sn.get("title","(live)"),
            "published": sn.get("publishedAt",""),
            "link": f"https://www.youtube.com/watch?v={vid}" if vid else "",
        })
    return out

@st.cache_data(ttl=300)
def yt_api_latest_videos(channel_id: str, max_results: int = 6):
    """API 키가 있으면 검색 API로 최신 영상(업로드) 조회; 없으면 RSS를 쓰는 게 낫다."""
    if not YOUTUBE_API_KEY:
        return yt_rss_latest(channel_id, max_results)
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "type": "video",
        "order": "date",
        "maxResults": str(max_results),
        "key": YOUTUBE_API_KEY,
    }
    r = requests.get(url, params=params, timeout=15)
    if not r.ok:
        return yt_rss_latest(channel_id, max_results)
    data = r.json().get("items", [])
    out = []
    for it in data:
        vid = it.get("id",{}).get("videoId")
        sn = it.get("snippet",{})
        out.append({
            "video_id": vid,
            "title": sn.get("title",""),
            "published": sn.get("publishedAt",""),
            "link": f"https://www.youtube.com/watch?v={vid}" if vid else "",
        })
    return out

def yt_embed(video_id: str, height: int = 315):
    if not video_id:
        return
    st.components.v1.html(
        f"""
        <div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;">
          <iframe src="https://www.youtube.com/embed/{video_id}"
                  title="YouTube video" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                  allowfullscreen style="position:absolute;top:0;left:0;width:100%;height:100%;"></iframe>
        </div>
        """,
        height=height+60
    )

# ---------------------------
# UI
# ---------------------------
st.sidebar.title("Tesla One-Stop (Free)")
page = st.sidebar.radio(
    "메뉴",
    ["📈 차트", "📰 뉴스/코멘트", "📺 유튜브", "🏦 지분 변동(13F, 무료)", "⚙️ 옵션/설정"]
)

# ---- 차트 ----
if page == "📈 차트":
    st.title("📈 TSLA 차트 (안정화 버전)")
    c1,c2,c3 = st.columns(3)
    with c1:
        period = st.selectbox("기간", ["1d","5d","7d","1mo","3mo","6mo","1y"], index=0)
    with c2:
        interval = st.selectbox("봉 간격", ["1m","5m","15m","1h","1d"], index=0)
    with c3:
        if st.button("캐시 초기화"):
            st.cache_data.clear()
            st.success("캐시 삭제 완료. 다시 불러오는 중…")
    df, used, note = safe_yf_download("TSLA", period, interval)
    if note: st.caption(f"소스: {note}")
    if df.empty:
        st.error("시세를 가져오지 못했어요.")
        with st.expander("네트워크 진단"):
            import requests
            tests = [
                "https://query2.finance.yahoo.com/v1/finance/trending/US?count=1",
                "https://query2.finance.yahoo.com/v8/finance/chart/TSLA?range=1d&interval=1m",
                "https://stooq.com/q/d/l/?s=tsla.us&i=d",
            ]
            for u in tests:
                try:
                    r = requests.get(u, headers={"User-Agent":"Mozilla/5.0"}, timeout=6)
                    st.write(u, "→", r.status_code, f"{len(r.content)} bytes")
                except Exception as e:
                    st.write(u, "→", str(e))
    else:
        plot_candles(df, f"TSLA {used[0]}/{used[1]}")
# ---- 뉴스/X ----
elif page == "📰 뉴스/코멘트":
    st.title("📰 뉴스 & 코멘트")
    t1,t2 = st.tabs(["뉴스 RSS","유명인 코멘트 (X 임베드 리프레시)"])
    with t1:
        cols = st.columns(2)
        keys = list(RSS_SOURCES.keys())
        left, right = keys[:(len(keys)+1)//2], keys[(len(keys)+1)//2:]
        for col, group in zip(cols, [left, right]):
            with col:
                for k in group:
                    st.subheader(k)
                    for it in fetch_rss(RSS_SOURCES[k], limit=7):
                        st.markdown(f"- **[{it['title']}]({it['link']})**")
                        if it["published"]: st.caption(it["published"])
                    st.markdown("---")
    with t2:
        acct_label = st.selectbox("계정 선택", list(X_USERNAMES.keys()))
        handle = X_USERNAMES[acct_label]
        refresh_sec = st.slider("새로고침(초)", 15, 180, 60, step=15)
        auto_refresh_html(refresh_sec, key=f"x_refresh_{handle}")
        embed_html = f"""
        <a class="twitter-timeline" href="https://twitter.com/{handle}?ref_src=twsrc%5Etfw">
          Tweets by @{handle}
        </a>
        <script async src="https://platform.twitter.com/widgets.js" charset="utf-8"></script>
        """
        st.components.v1.html(embed_html, height=800, scrolling=True)

# ---- 유튜브 ----
elif page == "📺 유튜브":
    st.title("📺 테슬라 유튜버 — 라이브/최신 영상")

    # 1) 채널 목록 편집
    st.subheader("채널 목록 편집")
    df_channels = st.session_state.get("yt_channels_df") or pd.DataFrame(DEFAULT_YT_CHANNELS)
    df_channels = st.data_editor(df_channels, num_rows="dynamic", key="yt_channels_editor")
    st.session_state["yt_channels_df"] = df_channels

    st.markdown("---")

    # 2) 라이브 체크(선택)
    st.subheader("지금 라이브 중 🔴")
    refresh_live = st.checkbox("자동 새로고침(초) 설정", value=True)
    live_interval = st.slider("라이브 체크 주기(초)", 30, 180, 60, step=15, disabled=not refresh_live)
    if refresh_live:
        auto_refresh_html(live_interval, key="yt_live_refresh")

    if df_channels.empty:
        st.info("채널을 추가하세요. 예: channel_id = UC_x5XG1OV2P6uZZ5FSM9TtQ")
    else:
        if not YOUTUBE_API_KEY:
            st.info("YOUTUBE_API_KEY가 없어서 라이브 상태는 API 없이 확인합니다. 각 채널의 `/live` 링크를 눌러 확인하세요.")
        live_cols = st.columns(3)
        idx = 0
        for _, row in df_channels.iterrows():
            name = str(row.get("채널명","")).strip()
            cid  = str(row.get("channel_id","")).strip()
            if not cid: continue
            # API가 있으면 라이브 검색
            lives = yt_api_live_videos(cid, max_results=2) if YOUTUBE_API_KEY else []
            with live_cols[idx % 3]:
                if lives:
                    for lv in lives:
                        st.markdown(f"**{name}** — 🔴 LIVE: [{lv['title']}]({lv['link']})")
                        yt_embed(lv["video_id"])
                else:
                    st.markdown(f"**{name}** — 현재 라이브 감지 없음")
                    st.caption(f"[채널 라이브 페이지 바로가기](https://www.youtube.com/channel/{cid}/live)")
            idx += 1

    st.markdown("---")

    # 3) 최신 업로드
    st.subheader("최신 업로드")
    per_channel = st.slider("채널별 표시 개수", 1, 8, 3)
    refresh_latest = st.checkbox("최신 영상 자동 새로고침", value=False)
    if refresh_latest:
        auto_refresh_html(90, key="yt_latest_refresh")

    if not df_channels.empty:
        for _, row in df_channels.iterrows():
            name = str(row.get("채널명","")).strip()
            cid  = str(row.get("channel_id","")).strip()
            if not cid: continue
            st.markdown(f"### {name}")
            vids = yt_api_latest_videos(cid, max_results=per_channel)
            if not vids:
                st.write("영상 정보를 가져오지 못했습니다.")
                continue
            cards = st.columns(len(vids))
            for col, v in zip(cards, vids):
                with col:
                    st.markdown(f"**[{v['title']}]({v['link']})**")
                    if v["video_id"]:
                        yt_embed(v["video_id"], height=200)
                    if v.get("published"):
                        st.caption(v["published"])
            st.markdown("---")

    st.caption("팁: 채널 ID는 채널 페이지 URL에서 `channel/` 뒤 24자 ID입니다. 커스텀 핸들이면 '채널 정보 > 공유 > 채널 링크'로 실제 ID 확인.")

# ---- 13F (무료)
elif page == "🏦 지분 변동(13F, 무료)":
    st.title("🏦 기관 보유/변동 — SEC 13F (완전 무료)")
    with st.expander("대상 기관(편집 가능)"):
        edit_df = pd.DataFrame([{"기관/펀드": k, "CIK": v} for k,v in DEFAULT_CIKS.items()])
        edited = st.data_editor(edit_df, num_rows="dynamic", key="mgr_edit")
        managers = {row["기관/펀드"]: str(row["CIK"]) for _, row in edited.iterrows()
                    if row.get("기관/펀드") and row.get("CIK")}
    if st.button("13F 새로 고침", type="primary"):
        st.cache_data.clear()
    with st.spinner("SEC에서 13F 불러오는 중..."):
        df = build_13f_table(managers)
    if df.empty:
        st.warning("데이터를 가져오지 못했습니다.")
    else:
        fmt = df.copy()
        if "보유주수(최근)" in fmt:
            fmt["보유주수(최근)"] = fmt["보유주수(최근)"].map(lambda x: f"{x:,}" if pd.notna(x) else "")
        if "평가액(USD, 최근)" in fmt:
            fmt["평가액(USD, 최근)"] = fmt["평가액(USD, 최근)"].map(lambda x: f"${x:,}" if pd.notna(x) else "")
        if "보유주수 증감(qoq)" in fmt:
            fmt["보유주수 증감(qoq)"] = fmt["보유주수 증감(qoq)"].map(lambda x: f"{x:+,}" if pd.notna(x) else "")
        st.dataframe(fmt, use_container_width=True)
    st.info("참고: 13F는 분기 단위 공개이며, 일중 변동은 제공되지 않습니다.")

# ---- 설정
elif page == "⚙️ 옵션/설정":
    st.title("⚙️ 옵션/설정")
    st.markdown("""
    - 유튜브: **API 키 없이도** 최신 영상은 RSS로 표시됩니다. 라이브 감지는 **YOUTUBE_API_KEY**가 있을 때 자동화됩니다.
    - SEC: User-Agent에 **연락 가능한 이메일** 표기를 권장합니다.
    - X API 폴링을 쓰면 X_BEARER_TOKEN이 필요합니다(없어도 임베드는 동작).
    """)
