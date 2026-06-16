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
st.set_page_config(page_title="領航員風向觀測站", page_icon="🎯", layout="wide")
load_dotenv()

# 全域高效率 Session 連線池
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
    try:
        url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
        df = pd.read_csv(url, header=None)
        df = df.dropna(subset=[0])
        df[0] = df[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
        raw_tickers = [t for t in df[0].tolist() if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except: return ["0050.TW", "0052.TW"]

# 🚀 核心優化：引入中英雙語精準去重特定日期抓取模組
def fetch_finmind_chips_for_date(session, ticker_digits, target_date):
    try:
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(ticker_digits), "start_date": target_date, "end_date": target_date}
        res = session.get(url, params=params, timeout=5)
        data = res.json()
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            if df.empty: return 0, 0, f"FinMind({target_date[5:].replace('-', '/')})"
            if df['name'].str.contains('Foreign_Investor').any():
                f_df = df[df['name'] == 'Foreign_Investor']
                fd_df = df[df['name'] == 'Foreign_Dealer']
                s_df = df[df['name'] == 'Investment_Trust']
            else:
                f_df = df[df['name'] == '外資']
                fd_df = df[df['name'] == '外資自營商']
                s_df = df[df['name'] == '投信']
            foreign_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
            fd_buy = (fd_df['buy'].sum() - fd_df['sell'].sum()) // 1000
            total_foreign = foreign_buy + fd_buy
            sitc_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
            return int(total_foreign), int(sitc_buy), f"FinMind({target_date[5:].replace('-', '/')})"
    except: pass
    return "未取得", "未取得", "⚠️ 查無資料"

def fetch_finmind_chips_fallback(session, ticker_digits):
    try:
        tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        start_date = (tw_now - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(ticker_digits), "start_date": start_date}
        res = session.get(url, params=params, timeout=5)
        data = res.json()
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            dates = sorted(df['date'].unique(), reverse=True)
            for date in dates:
                df_date = df[df['date'] == date]
                if df_date['name'].str.contains('Foreign_Investor').any():
                    f_df = df_date[df_date['name'] == 'Foreign_Investor']
                    fd_df = df_date[df_date['name'] == 'Foreign_Dealer']
                    s_df = df_date[df_date['name'] == 'Investment_Trust']
                else:
                    f_df = df_date[df_date['name'] == '外資']
                    fd_df = df_date[df_date['name'] == '外資自營商']
                    s_df = df_date[df_date['name'] == '投信']
                foreign_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
                fd_buy = (fd_df['buy'].sum() - fd_df['sell'].sum()) // 1000
                total_foreign = foreign_buy + fd_buy
                sitc_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
                if total_foreign != 0 or sitc_buy != 0:
                    return int(total_foreign), int(sitc_buy), f"FinMind歷史({date[5:].replace('-', '/')})"
            return 0, 0, f"FinMind歷史({dates[0][5:].replace('-', '/')})"
    except: pass
    return "未取得", "未取得", "⚠️ 查無資料"

@st.cache_data(ttl=300)
def fetch_market_daily_data(tickers_tuple):
    """🚀 終極重構：日期最優先決策矩陣！當日官方失敗，立刻逼迫 FinMind 備援同日資料，絕不盲目滑坡"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 先測試當日上市 OpenAPI
    for _ in range(2):
        try:
            res = GLOBAL_SESSION.get("https://openapi.twse.com.tw/v1/fund/T86", timeout=5)
            if res.status_code == 200 and len(res.json()) > 0:
                for item in res.json():
                    legal_data[item['Code'].strip()] = {
                        'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                        'sitc': item.get('InvestmentTrustBuyBuyOver', '0'),
                        'source': '官方盤後最新'
                    }
                return legal_data
        except: time.sleep(0.5)

    # 2. 核心大腦：日期優先迴圈，逐日進行跨庫夾擊
    for i in range(1, 6):
        dt = tw_now - datetime.timedelta(days=i)
        if dt.weekday() >= 5: continue
        date_str = dt.strftime("%Y%m%d")
        target_date_fm = dt.strftime("%Y-%m-%d")
        display_date = dt.strftime("%m/%d")

        # 戰術 A：嘗試敲擊官方歷史網頁
        day_success = False
        for _ in range(2):
            try:
                res = GLOBAL_SESSION.get(f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALL", timeout=5)
                data = res.json()
                if data.get('stat') == 'OK' and data.get('data'):
                    fields = data['fields']
                    f_idx = next((idx for idx, f in enumerate(fields) if '外陸資買賣超股數' in f or '外資及陸資買賣超股數' in f), -1)
                    fd_idx = next((idx for idx, f in enumerate(fields) if '外資自營商買賣超股數' in f), -1)
                    s_idx = next((idx for idx, f in enumerate(fields) if '投信買賣超股數' in f), -1)
                    for row in data['data']:
                        code = row[0].strip()
                        f_buy = int(row[f_idx].replace(',', '')) if f_idx != -1 else 0
                        if fd_idx != -1: f_buy += int(row[fd_idx].replace(',', ''))
                        s_buy = int(row[s_idx].replace(',', '')) if s_idx != -1 else 0
                        legal_data[code] = {'foreign': str(f_buy), 'sitc': str(s_buy), 'source': f'官方歷史({display_date})'}
                    day_success = True
                    break
            except: time.sleep(0.5)
        
        if day_success: return legal_data

        # 🚀 戰術 B [核心Bug修復點]：如果官方 06/15 失敗，不准跳到 06/12！立刻呼叫 FinMind 攔截 06/15 的籌碼！
        if tickers_tuple:
            benchmark = tickers_tuple[0].replace('.TW', '').replace('.TWO', '').strip()
            try:
                url = "https://api.finmindtrade.com/api/v4/data"
                params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": benchmark, "start_date": target_date_fm, "end_date": target_date_fm}
                res = GLOBAL_SESSION.get(url, params=params, timeout=4)
                fm_json = res.json()
                if fm_json.get("msg") == "success" and len(fm_json.get("data", [])) > 0:
                    # 抓到了！FinMind 在這一天有真實交易數據，發射信號鎖定此日期，不准再往下回溯！
                    return {"__USE_FINMIND__": True, "__TARGET_DATE__": target_date_fm}
            except: pass

    return {}

@st.cache_data(ttl=86400)
def get_company_name_cached(ticker_digits, api_key):
    try:
        client = RestClient(api_key=api_key)
        meta = client.stock.intraday.ticker(symbol=ticker_digits)
        if meta and 'nameShort' in meta: return meta['nameShort']
    except: pass
    return ticker_digits

@st.cache_data(ttl=60, show_spinner=False)
def generate_dashboard_data(tickers, legal_data, fugle_api_key):
    summary_rows = []
    errors = []
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

    try: fugle_stock = RestClient(api_key=fugle_api_key).stock
    except: fugle_stock = None

    try: batch_hist = yf.download(tickers, period="3mo", group_by='ticker', auto_adjust=True, progress=False)
    except: batch_hist = pd.DataFrame()

    for ticker in tickers:
        try:
            if len(tickers) == 1: hist = batch_hist.copy()
            else:
                if ticker in batch_hist.columns.levels[0]: hist = batch_hist[ticker].dropna(how='all').copy()
                else: hist = yf.download(ticker, period="3mo", auto_adjust=True, progress=False)
            
            if hist.empty or len(hist) < 25: continue
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            company_name = get_company_name_cached(fugle_symbol, fugle_api_key)

            # 🚀 智能非零籌碼判斷分配器
            foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 查無資料"
            if legal_data.get("__USE_FINMIND__"):
                target_date = legal_data["__TARGET_DATE__"]
                foreign_buy, sitc_buy, chip_source = fetch_finmind_chips_for_date(session, fugle_symbol, target_date)
            else:
                stock_legal = legal_data.get(fugle_symbol)
                if stock_legal:
                    try:
                        foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                        sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                        chip_source = stock_legal.get('source', '官方盤後')
                    except: pass

            if foreign_buy in ["未取得", 0] and sitc_buy in ["未取得", 0] and not legal_data.get("__USE_FINMIND__"):
                foreign_buy, sitc_buy, chip_source = fetch_finmind_chips_fallback(session, fugle_symbol)

            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    if quote and 'lastPrice' in quote and quote['lastPrice']:
                        hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                        fugle_vol = quote['total']['tradeVolume']
                        if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol
                except: pass

            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            delta = hist['Close'].diff()
            up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
            avg_gain = up.ewm(com=13, adjust=False).mean()
            avg_loss = down.ewm(com=13, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            hist['RSI'] = 100 - (100 / (1 + rs))
            
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal']
            
            low_min = hist['Low'].rolling(window=9).min()
            high_max = hist['High'].rolling(window=9).max()
            range_val = (high_max - low_min).replace(0, 1e-9)
            hist['RSV'] = 100 * ((hist['Close'] - low_min) / range_val)
            hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
            hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()

            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            current_vol = safe_val(today['Volume'])
            if current_vol <= 0: current_vol = safe_val(yesterday['Volume'])
            if current_vol <= 0: current_vol = 1
            ma_vol = safe_val(today['5Vol_MA'], default=1)
            if ma_vol <= 0: ma_vol = 1
            vol_ratio = current_vol / ma_vol
            
            highest_20d_high = hist['High'].rolling(window=20).max().iloc[-2]
            is_breakout = today['Close'] > highest_20d_high
            
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and vol_ratio > 1.5 and is_breakout: status_signal = "🔥 爆量真突破"
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
                "股票代號": ticker, "公司名稱": company_name, "即時股價": round(today['Close'], 2),
                "當日量(張)": int(current_vol // 1000), "5日均量(張)": int(ma_vol // 1000), "RSI(14日)": round(safe_val(today['RSI'], 50), 1),
                "KD訊號": kd_status, "MACD訊號": macd_status, "外資(張)": foreign_buy, "投信(張)": sitc_buy,
                "籌碼來源": chip_source, "即時狀態": status_signal
            })
        except Exception as e: errors.append(f"{ticker} ({str(e)[:15]})")
            
    return summary_rows, errors

# 渲染網頁
tickers = get_clean_tickers()
legal_data = fetch_market_daily_data(tuple(tickers)) # 轉換成tuple快取

with st.spinner("🔄 正在背景高速運算中（日期最優先戰術雷達啟動）..."):
    summary_rows, errors = generate_dashboard_data(tickers, legal_data, os.getenv("FUGLE_API_KEY"))

if errors: st.sidebar.warning(f"⚠️ 異常提示：{', '.join(errors)}")

df_display = pd.DataFrame(summary_rows)
if not df_display.empty:
    col1, col2, col3 = st.columns(3)
    col1.metric("📊 總監控標的", f"{len(df_display)} 檔")
    col2.metric("🔥 實質放量真突破", f"{len(df_display[df_display['即時狀態'] == '🔥 爆量真突破'])} 檔")
    col3.metric("📈 KD黃金交叉", f"{len(df_display[df_display['KD訊號'] == '🔥 黃金交叉'])} 檔")
    st.write("---")
    st.dataframe(
        df_display.style.map(
            lambda v: 'background-color: #ff4b4b; color: white;' if v in ["🔥 爆量真突破", "🔥 黃金交叉", "🔥 底部反轉"] else ('background-color: #262730; color: #a3a8b4;' if v in ["🟡 跌破5MA觀望", "🔴 偏空下探", "🔴 K<D", "⚠️ 查無資料"] else ''),
            subset=['即時狀態', 'KD訊號', 'MACD訊號', '籌碼來源']
        ), use_container_width=True, hide_index=True
    )
