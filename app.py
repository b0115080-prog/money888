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
    """抓取並淨化 Google 試算表追蹤清單"""
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        df = df.dropna(subset=[0])
        df[0] = df[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
        raw_tickers = [t for t in df[0].tolist() if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except:
        return ["0050.TW", "0052.TW"]

@st.cache_data(ttl=300) # 🚀 300秒快取：保護證交所API，避免被官方封鎖IP
def fetch_market_daily_data():
    """全面涵蓋上市與上櫃，精準抓取官方盤後籌碼數據"""
    legal_data, pe_data = {}, {}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    # 1. 上市籌碼
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code', '').strip()
                if code:
                    legal_data[code] = {
                        'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                        'sitc': item.get('InvestmentTrustBuyBuyOver', '0')
                    }
    except: pass

    # 2. 上櫃籌碼
    try:
        res = requests.get("https://openapi.tpex.org.tw/v1/tpex_38", headers=headers, timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('SecuritiesCompanyCode', '').strip()
                if code:
                    legal_data[code] = {
                        'foreign': item.get('ForeignInvestorsNetBuySell', '0'),
                        'sitc': item.get('InvestmentTrustsNetBuySell', '0')
                    }
    except: pass

    return legal_data, pe_data

# ==========================================
# 🚀 終極提速核心：將所有重度運算封裝並快取 60 秒！
# ==========================================
@st.cache_data(ttl=60, show_spinner=False)
def generate_dashboard_data(tickers, legal_data, fugle_api_key):
    """將所有 API 下載與技術指標計算，包裝進快取記憶體中"""
    summary_rows = []
    
    try:
        fugle_client = RestClient(api_key=fugle_api_key)
        fugle_stock = fugle_client.stock
    except:
        fugle_stock = None

    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="3mo", actions=True)
            if hist.empty or len(hist) < 25: continue
            
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            
            # --- 籌碼邏輯 ---
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    f_buy_raw = str(stock_legal.get('foreign', '0')).replace(',', '')
                    s_buy_raw = str(stock_legal.get('sitc', '0')).replace(',', '')
                    foreign_buy = int(float(f_buy_raw)) // 1000
                    sitc_buy = int(float(s_buy_raw)) // 1000
                    chip_source = "官方盤後"
                except:
                    foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "解析異常"
            else:
                foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 等待發布"

            # --- 即時報價 ---
            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                    fugle_vol = quote['total']['tradeVolume']
                    if fugle_vol > 0:
                        hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000 
                except: pass

            # --- 四維技術指標 ---
            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            delta = hist['Close'].diff()
            up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
            hist['RSI'] = 100 - (100 / (1 + (up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean())))
            
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal']
            
            low_min = hist['Low'].rolling(window=9).min()
            high_max = hist['High'].rolling(window=9).max()
            hist['RSV'] = 100 * ((hist['Close'] - low_min) / (high_max - low_min))
            hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
            hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()

            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            # --- 綜合狀態與技術指標研判 ---
            current_vol = today['Volume']
            if current_vol <= 0: current_vol = yesterday['Volume']
            if current_vol <= 0: current_vol = 1
            ma_vol = today['5Vol_MA'] if today['5Vol_MA'] > 0 else 1
            
            vol_ratio = current_vol / ma_vol
            
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and vol_ratio > 1.5:
                status_signal = "🔥 爆量真突破"
            elif today['Close'] < today['5MA']:
                status_signal = "🟡 跌破5MA觀望"

            if yesterday['MACD_Hist'] < 0 and today['MACD_Hist'] >= 0:
                macd_status = "🔥 底部反轉"
            elif today['MACD_Hist'] > 0:
                macd_status = "🟢 柱狀圖正常"
            elif today['MACD_Hist'] < 0 and today['MACD_Hist'] > yesterday['MACD_Hist']:
                macd_status = "🟡 負柱收斂"
            else:
                macd_status = "🔴 偏空下探"

            if yesterday['K'] <= yesterday['D'] and today['K'] > today['D']:
                kd_status = "🔥 黃金交叉"
            elif today['K'] > today['D']:
                kd_status = "🟢 K>D"
            else:
                kd_status = "🔴 K<D"

            summary_rows.append({
                "股票代號": ticker,
                "公司名稱": stock.info.get("shortName", ticker),
                "即時股價": round(today['Close'], 2),
                "當日量(張)": int(current_vol // 1000),
                "5日均量(張)": int(ma_vol // 1000),
                "RSI(14日)": round(today['RSI'], 1),
                "KD訊號": kd_status,
                "MACD訊號": macd_status,
                "外資(張)": foreign_buy,
                "投信(張)": sitc_buy,
                "籌碼來源": chip_source,
                "即時狀態": status_signal
            })
        except:
            pass
            
    return summary_rows

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
    except:
        st.sidebar.error("❌ 連線異常，請稍後再試。")

# ==========================================
# --- 4. 運算核心執行 ---
# ==========================================
tickers = get_clean_tickers()
legal_data, pe_data = fetch_market_daily_data()

with st.spinner("🔄 正在背景高速運算中（每分鐘自動更新一次）..."):
    # 將繁重的迴圈任務交給 60 秒的快取大腦
    summary_rows = generate_dashboard_data(tickers, legal_data, os.getenv("FUGLE_API_KEY"))

# --- 5. 渲染網頁表格 UI ---
df_display = pd.DataFrame(summary_rows)

if not df_display.empty:
    col1, col2, col3 = st.columns(3)
    col1.metric("📊 總監控標的", f"{len(df_display)} 檔")
    col2.metric("🔥 盤中強勢突破", f"{len(df_display[df_display['即時狀態'] == '🔥 爆量真突破'])} 檔")
    col3.metric("📈 KD黃金交叉", f"{len(df_display[df_display['KD訊號'] == '🔥 黃金交叉'])} 檔")
    
    st.write("---")
    st.dataframe(
        df_display.style.map(
            lambda v: 'background-color: #ff4b4b; color: white;' if v in ["🔥 爆量真突破", "🔥 黃金交叉", "🔥 底部反轉"] else ('background-color: #262730; color: #a3a8b4;' if v in ["🟡 跌破5MA觀望", "🔴 偏空下探", "🔴 K<D", "⚠️ 等待發布"] else ''),
            subset=['即時狀態', 'KD訊號', 'MACD訊號', '籌碼來源']
        ),
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("📭 目前清單為空，或正在等待開盤數據中。")

