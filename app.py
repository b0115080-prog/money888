import os
import streamlit as st
import pandas as pd
import yfinance as yf
import requests
from fugle_marketdata import RestClient
from dotenv import load_dotenv

# 1. 網頁基本配置 (必須放在第一行)
st.set_page_config(page_title="台股主力狙擊指揮所", page_icon="🎯", layout="wide")
load_dotenv()

# --- 2. 核心數據抓取函式 ---
@st.cache_data(ttl=60) 
def get_clean_tickers():
    """抓取並淨化 Google 試算表追蹤清單"""
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        df = df.dropna(subset=[0])
        df[0] = df[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
        raw_tickers = [t for t in df[0].tolist() if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers)) # 回傳去重複的乾淨清單
    except:
        return ["0050.TW", "0052.TW"]
def fetch_twse_data():
    """抓取證交所官方財報與籌碼"""
    legal_data, pe_data = {}, {}
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code' or 'StockNo', '').strip()
                if code: legal_data[code] = item
    except: pass
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code', '').strip()
                if code: pe_data[code] = item
    except: pass
    return legal_data, pe_data

# --- 3. 網頁視覺化標題區 ---
st.title("🎯 台股主力狙擊大本營")
st.subheader("隨時監控即時行情、技術面均線與昨日法人核心籌碼")

# ==========================================
# 🚀 側邊欄控制中心 (按鈕區)
# ==========================================

# 1. 強迫 GitHub 執行按鈕
if st.sidebar.button("🚀 強迫 GitHub 核心立即突擊", use_container_width=True):
    st.sidebar.info("📡 正在向 GitHub 發射最高特權 204 暗號...")
    owner, repo = "b0115080-prog", "money888"
    # 從 Secrets 讀取金鑰，若無則抓取預設值
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

# (已移除暫停通知按鈕，保持介面極簡)

# ==========================================
# --- 4. 運算核心開始 ---
# ==========================================
tickers = get_clean_tickers()
legal_data, pe_data = fetch_twse_data()

summary_rows = []

with st.spinner("🔄 正在跨海同步 Yahoo 歷史資料、技術指標與富果即時行情..."):
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="3mo", actions=True)
            if hist.empty or len(hist) < 25: continue
            
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            
            # --- 基礎均線與量能計算 ---
            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            # --- 🚀 新增：RSI (14日) 計算 ---
            delta = hist['Close'].diff()
            up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
            hist['RSI'] = 100 - (100 / (1 + (up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean())))
            
            # --- 🚀 新增：MACD 計算 ---
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal'] # 柱狀圖
            
            # --- 🚀 新增：KD (9日) 計算 ---
            low_min = hist['Low'].rolling(window=9).min()
            high_max = hist['High'].rolling(window=9).max()
            hist['RSV'] = 100 * ((hist['Close'] - low_min) / (high_max - low_min))
            hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean() # 平滑近似值
            hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()

            # 抓取今日與昨日數值
            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            # --- 籌碼邏輯 ---
            stock_legal = legal_data.get(fugle_symbol, {})
            try:
                foreign_buy = int(stock_legal.get('ForeignInvestmentBuyBuyOver', '0').replace(',', '')) // 1000
                sitc_buy = int(stock_legal.get('InvestmentTrustBuyBuyOver', '0').replace(',', '')) // 1000
                chip_source = "今日盤後" if stock_legal else "昨日歷史"
            except:
                foreign_buy = int(hist['Foreign_Net'].iloc[-2]) // 1000 if 'Foreign_Net' in hist.columns else int((yesterday['Volume'] // 1000) * 0.12)
                sitc_buy = int(hist['SITC_Net'].iloc[-2]) // 1000 if 'SITC_Net' in hist.columns else int((yesterday['Volume'] // 1000) * 0.03)
                chip_source = "昨日估算"

            # --- 綜合狀態與技術指標研判 ---
            # 1. 均線與爆量狀態
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and today['Volume'] > (today['5Vol_MA'] * 1.5):
                status_signal = "🔥 爆量真突破"
            elif today['Close'] < today['5MA']:
                status_signal = "🟡 跌破5MA觀望"

            # 2. MACD 狀態判讀
            if yesterday['MACD_Hist'] < 0 and today['MACD_Hist'] > 0:
                macd_status = "🔥 底部反轉"
            elif today['MACD_Hist'] > 0:
                macd_status = "🟢 柱狀圖正常"
            elif today['MACD_Hist'] < 0 and today['MACD_Hist'] > yesterday['MACD_Hist']:
                macd_status = "🟡 負柱收斂"
            else:
                macd_status = "🔴 偏空下探"

            # 3. KD 狀態判讀
            if yesterday['K'] <= yesterday['D'] and today['K'] > today['D']:
                kd_status = "🔥 黃金交叉"
            elif today['K'] > today['D']:
                kd_status = "🟢 K>D"
            else:
                kd_status = "🔴 K<D"

            # 將所有數據寫入表格列
            summary_rows.append({
                "股票代號": ticker,
                "公司名稱": stock.info.get("shortName", ticker),
                "即時股價": round(today['Close'], 2),
                "當日量(張)": int(today['Volume'] // 1000),
                "5日均量(張)": int(today['5Vol_MA'] // 1000),
                "RSI(14日)": round(today['RSI'], 1),
                "KD訊號": kd_status,
                "MACD訊號": macd_status,
                "外資(張)": foreign_buy,
                "投信(張)": sitc_buy,
                "籌碼來源": chip_source,
                "即時狀態": status_signal
            })
        except Exception as e:
            pass

# --- 5. 渲染網頁表格 UI ---
df_display = pd.DataFrame(summary_rows)

if not df_display.empty:
    # 建立精美計數儀表板
    col1, col2, col3 = st.columns(3)
    col1.metric("📊 總監控標的", f"{len(df_display)} 檔")
    col2.metric("🔥 盤中強勢突破", f"{len(df_display[df_display['即時狀態'] == '🔥 爆量真突破'])} 檔")
    col3.metric("📈 KD黃金交叉", f"{len(df_display[df_display['KD訊號'] == '🔥 黃金交叉'])} 檔")
    
    st.write("---")
    # 高清互動式大表格
    st.dataframe(
        df_display.style.map(
            lambda v: 'background-color: #ff4b4b; color: white;' if v in ["🔥 爆量真突破", "🔥 黃金交叉", "🔥 底部反轉"] else ('background-color: #262730; color: #a3a8b4;' if v in ["🟡 跌破5MA觀望", "🔴 偏空下探", "🔴 K<D"] else ''),
            subset=['即時狀態', 'KD訊號', 'MACD訊號']
        ),
        use_container_width=True,
        hide_index=True
    )
else:
    st.info("📭 目前清單為空，或正在等待開盤數據中。")
