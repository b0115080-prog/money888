import os
import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import time
import datetime
from fugle_marketdata import RestClient
from dotenv import load_dotenv

# 1. 網頁基本配置 (必須放在第一行)
st.set_page_config(page_title="台股主力狙擊指揮所", page_icon="🎯", layout="wide")
load_dotenv()

# --- 🎯 統一優化 8：全域 Session 連線池管理 ---
GLOBAL_SESSION = requests.Session()
GLOBAL_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
})

def safe_val(val, default=0):
    try: return float(val) if not pd.isna(val) else default
    except: return default

@st.cache_data(ttl=60)
def get_clean_tickers():
    """抓取並淨化 Google 試算表追蹤清單 (防呆去重)"""
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        tickers = df.iloc[:, 0].dropna().astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip().tolist()
        raw_tickers = [t for t in tickers if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except Exception as e:
        st.error(f"❌ 讀取試算表失敗: {e}")
        return ["0050.TW", "0052.TW"]

@st.cache_data(ttl=300)
def fetch_market_daily_data():
    """🚀 官方時光機 (純上市版)：針對單日強制重試 2 次，共用 Session 連線"""
    legal_data = {}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 上市 OpenAPI
    for _ in range(2):
        try:
            res = GLOBAL_SESSION.get("https://openapi.twse.com.tw/v1/fund/T86", timeout=6)
            if res.status_code == 200 and len(res.json()) > 0:
                for item in res.json():
                    legal_data[item['Code'].strip()] = {
                        'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                        'sitc': item.get('InvestmentTrustBuyBuyOver', '0'),
                        'source': '官方盤後最新'
                    }
                break
        except: time.sleep(1)

    # 2. 上市 官方歷史資料庫救援
    if not legal_data:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue
            date_str = dt.strftime("%Y%m%d")
            
            day_success = False
            for _ in range(2): 
                try:
                    res = GLOBAL_SESSION.get(f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL", timeout=6)
                    data = res.json()
                    if data.get('stat') == 'OK' and data.get('data'):
                        fields = data['fields']
                        f_idx = next((idx for idx, f in enumerate(fields) if '外陸資買賣超股數(不含外資自營商)' in f or '外資及陸資買賣超股數' in f), -1)
                        fd_idx = next((idx for idx, f in enumerate(fields) if '外資自營商買賣超股數' in f), -1)
                        s_idx = next((idx for idx, f in enumerate(fields) if '投信買賣超股數' in f), -1)
                        for row in data['data']:
                            code = row[0].strip()
                            f_buy = int(row[f_idx].replace(',', '')) if f_idx != -1 else 0
                            if fd_idx != -1: f_buy += int(row[fd_idx].replace(',', ''))
                            s_buy = int(row[s_idx].replace(',', '')) if s_idx != -1 else 0
                            legal_data[code] = {'foreign': str(f_buy), 'sitc': str(s_buy), 'source': f'官方歷史({dt.strftime("%m/%d")})'}
                        day_success = True
                        break
                except: time.sleep(1)
            if day_success: break 

    return legal_data

# --- 🎯 統一優化 7：為 FinMind API 加上 30 分鐘快取，避免重複請求刷爆額度 ---
@st.cache_data(ttl=1800)
def fetch_finmind_chips(ticker_digits):
    """🚀 FinMind 終極備援：修正優化3 (排除中英重複加總)，加入智能非零追溯"""
    try:
        tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        start_date = (tw_now - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(ticker_digits), "start_date": start_date}
        
        res = GLOBAL_SESSION.get(url, params=params, timeout=6)
        data = res.json()
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            dates = sorted(df['date'].unique(), reverse=True)
            
            for date in dates:
                df_date = df[df['date'] == date]
                
                # 🎯 優化 3 修正：明確指定官方標籤標籤，防止含有 'Foreign|外資' 的多個欄位被重複加總
                f_df = df_date[df_date['name'].isin(['Foreign_Investor', 'Foreign_Dealer', '外資', '外資自營商'])]
                s_df = df_date[df_date['name'].isin(['Investment_Trust', '投信'])]
                
                foreign_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
                sitc_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
                
                # 🎯 優化 4 修正：只要其中一個非0，就代表有歷史成交數據，立刻觸發起漲備援
                if foreign_buy != 0 or sitc_buy != 0:
                    short_date = date[5:].replace('-', '/')
                    return int(foreign_buy), int(sitc_buy), f"FinMind歷史({short_date})"
            
            latest = dates[0]
            return 0, 0, f"FinMind歷史({latest[5:].replace('-', '/')})"
    except Exception as e:
        return "未取得", "未取得", f"⚠️ 備援異常: {str(e)[:15]}"
    return "未取得", "未取得", "⚠️ 查無資料"

# --- 3. 網頁視覺化標題區 ---
st.title("🎯 領航員風向觀測站")
st.subheader("隨時監控即時氣流、量能潮汐與大船暗流動向")

# 側邊欄控制
if st.sidebar.button("🧹 清除網頁快取 (重新抓取)", use_container_width=True):
    st.cache_data.clear()
    st.sidebar.success("✅ 快取已清除，請重新整理網頁！")

if st.sidebar.button("🚀 強迫 GitHub 核心立即突擊", use_container_width=True):
    st.sidebar.info("📡 正在向 GitHub 發射最高特權 204 暗號...")
    owner, repo, token = "b0115080-prog", "money888", os.getenv("GITHUB_TOKEN")
    try:
        res = GLOBAL_SESSION.post(f"https://api.github.com/repos/{owner}/{repo}/dispatches", json={"event_type": "google_track_trigger"}, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=5)
        if res.status_code == 204: st.sidebar.success("✅ GitHub 已經插隊開機！請靜待 45 秒查看 LINE 通知。")
        else: st.sidebar.error(f"❌ 呼叫失敗，狀態碼: {res.status_code}")
    except Exception as e: st.sidebar.error(f"❌ 連線異常: {e}")

# --- 4. 運算核心執行 ---
tickers = get_clean_tickers()
legal_data = fetch_market_daily_data()

summary_rows = []
errors = []

# 初始化 Fugle RestClient
try:
    fugle_client = RestClient(api_key=os.getenv("FUGLE_API_KEY"))
    fugle_stock = fugle_client.stock
except:
