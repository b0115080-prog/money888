import os
import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import time
from fugle_marketdata import RestClient
from dotenv import load_dotenv

# 1. 網頁基本配置 (必須放在第一行)
st.set_page_config(page_title="台股主力狙擊指揮所", page_icon="🎯", layout="wide")
load_dotenv()

# --- 2. 核心數據抓取函式 ---
@st.cache_data(ttl=60) # 🚀 60秒快取：保護試算表連線
def get_clean_tickers():
    """抓取並淨化 Google 試算表追蹤清單 (防呆去重版)"""
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        df = df.dropna(subset=[0])
        df[0] = df[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
        raw_tickers = [t for t in df[0].tolist() if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except:
        return ["0050.TW", "0052.TW"]

@st.cache_data(ttl=300) # 🚀 300秒快取：保護證交所API不被你刷爆，網頁秒開！
def fetch_market_daily_data():
    """全面涵蓋上市與上櫃，精準抓取官方盤後籌碼數據 (網頁升級版)"""
    legal_data, pe_data = {}, {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    # 1. 上市 (TWSE) 籌碼
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code', '').strip()
                if code:
                    f_buy = item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0'))
                    s_buy = item.get('InvestmentTrustBuyBuyOver', '0')
                    legal_data[code] = {'foreign': f_buy, 'sitc': s_buy}
    except: pass

    # 2. 上櫃 (TPEx) 籌碼
    try:
        res = requests.get("https://openapi.tpex.org.tw/v1/tpex_38", headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('SecuritiesCompanyCode', '').strip()
                if code:
                    f_buy = item.get('ForeignInvestorsNetBuySell', '0')
                    s_buy = item.get('InvestmentTrustsNetBuySell', '0')
                    legal_data[code] = {'foreign': f_buy, 'sitc': s_buy}
    except: pass

    # 3. 本益比
    try:
        pe_res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", headers=headers, timeout=5)
        if pe_res.status_code == 200:
            for item in pe_res.json():
                code = item.get('Code', '').strip()
                if code: pe_data[code] = item
    except: pass

    return legal_data, pe_data

# --- 3. 網頁視覺化標題區 ---
st.title("🎯 台股主力狙擊大本營")
st.subheader("隨時監控即時行情、技術面多空與真實法人籌碼")

# ==========================================
# 🚀 側邊欄控制中心 (按鈕區)
# ==========================================
if st.sidebar.button("🚀 強迫 GitHub 核心立即突擊", use_container_width=True):
    st.sidebar.info("📡 正在向 GitHub 發射最高特權 204 暗號...")
    owner, repo = "b0115080-prog", "money888"
    token = os.getenv("GITHUB_TOKEN")
    dispatch_url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
    
    try:
        res = requests.post(dispatch_url, json={"event_type": "google_track_trigger"}, 
                            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=5)
        if res.status_code == 204:
            st.sidebar.success("✅ GitHub 已經插隊開機！請靜待 45 秒查看 LINE 通知。")
        else:
            st.sidebar.error(f"❌ 呼叫失敗，狀態碼: {res.status_code}")
    except Exception as e:
        st.sidebar.error("❌ 連線異常，請稍後再試。")

# ==========================================
# --- 4. 運算核心開始 ---
# ==========================================
tickers = get_clean_tickers()
legal_data, pe_data = fetch_market_daily_data()

summary_rows = []

# 🚀 確實初始化 Fugle API
try:
    fugle_client = RestClient(api_key=os.getenv

