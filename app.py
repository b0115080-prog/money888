import os
import streamlit as st
import pandas as pd
import yfinance as yf
import requests
import time
import datetime
from fugle_marketdata import RestClient
from dotenv import load_dotenv

st.set_page_config(page_title="台股主力狙擊指揮所", page_icon="🎯", layout="wide")
load_dotenv()

def safe_val(val, default=0):
    try: return float(val) if not pd.isna(val) else default
    except: return default

@st.cache_data(ttl=60)
def get_clean_tickers():
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        tickers = df.iloc[:, 0].dropna().astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip().tolist()
        raw_tickers = [t for t in tickers if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except: return ["0050.TW", "0052.TW"]

@st.cache_data(ttl=300)
def fetch_market_daily_data():
    """🚀 官方時光機 (純上市版)：針對單日強制重試 2 次，徹底移除上櫃邏輯"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 上市 OpenAPI (強制重試 2 次)
    for _ in range(2):
        try:
            res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=5)
            if res.status_code == 200 and len(res.json()) > 0:
                for item in res.json():
                    legal_data[item['Code'].strip()] = {
                        'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                        'sitc': item.get('InvestmentTrustBuyBuyOver', '0'),
                        'source': '官方盤後最新'
                    }
                break
        except: time.sleep(1)

    # 2. 上市 官方歷史時光機備援
    if not legal_data:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue # 避開週末
            date_str = dt.strftime("%Y%m%d")
            
            day_success = False
            for _ in range(2): # 🚀 強制重試 2 次
                try:
                    res = requests.get(f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL", headers=headers, timeout=5)
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

def fetch_finmind_chips(ticker_digits):
    """🚀 終極備援防線：中英雙語防彈解析，只要是 0 就自動找前一天！"""
    try:
        tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        start_date = (tw_now - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(ticker_digits), "start_date": start_date}
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            
            # 依日期降冪排列 (從最新到最舊)
            dates = sorted(df['date'].unique(), reverse=True)
            
            for date in dates:
                df_date = df[df['date'] == date]
                
                # 💡 核心修正：同時捕捉中英文，破解 FinMind 語言陷阱
                f_df = df_date[df_date['name'].str.contains('Foreign|外資', case=False, na=False)]
                s_df = df_date[df_date['name'].str.contains('Investment|投信', case=False, na=False)]
                
                foreign_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
                sitc_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
                
                # 💡 智能非零追溯：只要這天不是雙0，就代表找到真實資料，立刻回傳！
                if foreign_buy != 0 or sitc_buy != 0:
                    short_date = date[5:].replace('-', '/')
                    return int(foreign_buy), int(sitc_buy), f"FinMind歷史({short_date})"
            
            # 如果這10天真的全是0 (例如極度冷門ETF)，回傳最新日期的0
            latest = dates[0]
            return 0, 0, f"FinMind歷史({latest[5:].replace('-', '/')})"
    except: pass
    return "未取得", "未取得", "⚠️ 查無資料"

@st.cache_data(ttl=60, show_spinner=False)
def generate_dashboard_data(tickers, legal_data, fugle_api_key):
    summary_rows = []
    errors = []
    
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    try: fugle_stock = RestClient(api_key=fugle_api_key).stock
    except: fugle_stock = None

    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker, session=session)
            hist = stock.history(period="3mo", actions=True)
            if hist.empty or len(hist) < 25: 
                errors.append(f"{ticker} (無歷史資料或遭Yahoo阻擋)")
                continue
            
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            
            foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 查無資料"
            
            # 優先檢查官方籌碼
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                    sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                    chip_source = stock_legal.get('source', '官方盤後')
                except: pass
            
            # 🚀 若官方沒資料或為 0 (盤中尚未結算)，自動往回撈前一個非零交易日！
            if foreign_buy in ["未取得", 0] and sitc_buy in ["未取得", 0]:
                f_fm, s_fm, source_fm = fetch_finmind_chips(fugle_symbol)
                if isinstance(f_fm, int):
                    foreign_buy, sitc_buy, chip_source = f_fm, s_fm, source_fm

            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                    fugle_vol = quote['total']['tradeVolume']
                    if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000 
                except: pass

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
            
            current_vol = safe_val(today['Volume'])
            if current_vol <= 0: current_vol = safe_val(yesterday['Volume'])
            if current_vol <= 0: current_vol = 1
            ma_vol = safe_val(today['5Vol_MA'], default=1)
            if ma_vol <= 0: ma_vol = 1
            
            vol_ratio = current_vol / ma_vol
            
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and vol_ratio > 1.5: status_signal = "🔥 爆量真突破"
            elif today['Close'] < today['5MA']: status_signal = "🟡 跌破5MA觀望"

            macd_val, macd_yest = safe_val(today['MACD_Hist']), safe_val(yesterday['MACD_Hist'])
            if macd_yest < 0 and macd_val >= 0: macd_status = "🔥 底部反轉"
            elif macd_val > 0: macd_status = "🟢 柱狀圖正常"
            elif macd_val < 0 and macd_val > macd_yest: macd_status = "🟡 負柱收斂"
            else: macd_status = "🔴 偏空下探"

            if safe_val(yesterday['K']) <= safe_val(yesterday['D']) and safe_val(today['K']) > safe_val(today['D']): kd_status = "🔥 黃金交叉"
            elif safe_val(today['K']) > safe_val(today['D']): kd_status = "🟢 K>D"
            else: kd_status = "🔴 K<D"

            summary_rows.append({
                "股票代號": ticker, "公司名稱": ticker, "即時股價": round(today['Close'], 2),
                "當日量(張)": int(current_vol // 1000), "5日均量(張)": int(ma_vol // 1000), "RSI(14日)": round(safe_val(today['RSI'], 50), 1),
                "KD訊號": kd_status, "MACD訊號": macd_status, "外資(張)": foreign_buy, "投信(張)": sitc_buy,
                "籌碼來源": chip_source, "即時狀態": status_signal
            })
        except Exception as e:
            errors.append(f"{ticker} (運算錯誤: {str(e)[:30]})")
            
    return summary_rows, errors

st.title("🎯 台股主力狙擊大本營")
st.subheader("隨時監控即時行情、技術面多空與真實法人籌碼")

if st.sidebar.button("🧹 清除網頁快取 (重新抓取)", use_container_width=True):
    st.cache_data.clear()
    st.sidebar.success("✅ 快取已清除，請重新整理網頁！")

if st.sidebar.button("🚀 強迫 GitHub 核心立即突擊", use_container_width=True):
    st.sidebar.info("📡 正在向 GitHub 發射最高特權 204 暗號...")
    owner, repo, token = "b0115080-prog", "money888", os.getenv("GITHUB_TOKEN")
    try:
        res = requests.post(f"https://api.github.com/repos/{owner}/{repo}/dispatches", json={"event_type": "google_track_trigger"}, headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=5)
        if res.status_code == 204: st.sidebar.success("✅ GitHub 已經插隊開機！請靜待 45 秒查看 LINE 通知。")
        else: st.sidebar.error(f"❌ 呼叫失敗，狀態碼: {res.status_code}")
    except: st.sidebar.error("❌ 連線異常，請稍後再試。")

tickers = get_clean_tickers()
legal_data = fetch_market_daily_data()

with st.spinner("🔄 正在背景高速運算中（純上市防彈引擎）..."):
    summary_rows, errors = generate_dashboard_data(tickers, legal_data, os.getenv("FUGLE_API_KEY"))

if errors:
    st.warning(f"⚠️ 部份標的解析發生異常或遭 Yahoo 阻擋：{', '.join(errors)}")

df_display = pd.DataFrame(summary_rows)
if not df_display.empty:
    col1, col2, col3 = st.columns(3)
    col1.metric("📊 總監控標的", f"{len(df_display)} 檔")
    col2.metric("🔥 盤中強勢突破", f"{len(df_display[df_display['即時狀態'] == '🔥 爆量真突破'])} 檔")
    col3.metric("📈 KD黃金交叉", f"{len(df_display[df_display['KD訊號'] == '🔥 黃金交叉'])} 檔")
    st.write("---")
    st.dataframe(
        df_display.style.map(
            lambda v: 'background-color: #ff4b4b; color: white;' if v in ["🔥 爆量真突破", "🔥 黃金交叉", "🔥 底部反轉"] else ('background-color: #262730; color: #a3a8b4;' if v in ["🟡 跌破5MA觀望", "🔴 偏空下探", "🔴 K<D", "⚠️ 查無資料"] else ''),
            subset=['即時狀態', 'KD訊號', 'MACD訊號', '籌碼來源']
        ), use_container_width=True, hide_index=True
    )
else: 
    st.info("📭 目前清單為空。若是盤中突發異常，請點擊左側「🧹 清除網頁快取」後重新整理。")
