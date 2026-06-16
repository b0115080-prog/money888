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

# --- 🚀 全域 Session 連線池管理 ---
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
        df = df.dropna(subset=[0])
        df[0] = df[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
        raw_tickers = [t for t in df[0].tolist() if t.lower() != 'nan' and t]
        return list(dict.fromkeys(raw_tickers))
    except Exception as e:
        st.error(f"❌ 讀取試算表失敗: {e}")
        return ["0050.TW", "0052.TW"]

# 🚀 修正：將 fetch_stock_names_map 改為包含 ETF 清單的超級映射表
@st.cache_data(ttl=86400)
def fetch_stock_names_map():
    name_map = {}
    # 個股清單
    try:
        res = GLOBAL_SESSION.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code', '').strip()
                name = item.get('Name', '').strip()
                if code and name: name_map[code] = name
    except: pass
    # ETF 清單 (補全 0050, 0052 等名稱)
    try:
        res = GLOBAL_SESSION.get("https://openapi.twse.com.tw/v1/fund/ETFIOP", timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('InstrumentID', '').strip()
                name = item.get('InstrumentName', '').strip()
                if code and name: name_map[code] = name
    except: pass
    return name_map

@st.cache_data(ttl=300)
def fetch_market_daily_data(tickers_list):
    """🚀 【日期優先交叉天網核心】：當日官方失憶或失敗，在同一個日期內立刻逼迫 FinMind 備援，絕不跨日滑坡！"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 優先試探：當日最即時的官方 OpenAPI
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
                return legal_data
        except: time.sleep(0.5)

    # 2. 日期優先交叉天網：由近到遠，一天一天突破
    for i in range(0, 6): 
        dt = tw_now - datetime.timedelta(days=i)
        if dt.weekday() >= 5: continue 
        date_str = dt.strftime("%Y%m%d")
        target_date_fm = dt.strftime("%Y-%m-%d")
        display_date = dt.strftime("%m/%d")
        
        official_success = False
        # 戰術 A：強攻證交所官方最新 RWD 歷史端點
        for _ in range(2):
            try:
                res = GLOBAL_SESSION.get(f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALL", headers=headers, timeout=5)
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
                    official_success = True
                    break
            except: time.sleep(0.5)
            
        if official_success and len(legal_data) > 0:
            return legal_data

        # 🚀 戰術 B 【同日 FinMind 攔截機制】
        if tickers_list:
            benchmark = tickers_list[0].replace('.TW', '').replace('.TWO', '').strip()
            try:
                url = "https://api.finmindtrade.com/api/v4/data"
                params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": benchmark, "start_date": target_date_fm, "end_date": target_date_fm}
                res = GLOBAL_SESSION.get(url, params=params, timeout=4)
                if res.json().get("msg") == "success" and len(res.json().get("data", [])) > 0:
                    return {"__USE_FINMIND__": True, "__TARGET_DATE__": target_date_fm}
            except: pass

    return {}

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

# --- 3. 網頁視覺化側邊欄與標題區 ---
with st.sidebar:
    st.write("### 🧭 控制大本營")
    if st.button("🧹 清除網頁快取 (重新抓取)", use_container_width=True):
        st.cache_data.clear()
        st.success("✅ 快取已清除，請重新整理網頁！")

    if st.button("🚀 強迫 GitHub 核心立即突擊", use_container_width=True):
        st.info("📡 正在向 GitHub 發射最高特權 204 暗號...")
        owner, repo = "b0115080-prog", "money888"
        token = os.getenv("GITHUB_TOKEN", "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")
        dispatch_url = f"https://api.github.com/repos/{owner}/{repo}/dispatches"
        try:
            res = GLOBAL_SESSION.post(dispatch_url, json={"event_type": "google_track_trigger"}, 
                                headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}, timeout=5)
            if res.status_code == 204: st.success("✅ GitHub 已經插隊開機！請靜待 45 秒查看 LINE 通知。")
            else: st.error(f"❌ 呼叫失敗，狀態碼: {res.status_code}")
        except Exception as e: st.error(f"❌ 連線異常: {e}")

st.title("🎯 領航員風向觀測站")
st.subheader("隨時監控即時氣流、量能潮汐與大船暗流動向")

# --- 4. 運算核心開始 ---
tickers = get_clean_tickers()
legal_data = fetch_market_daily_data(tickers)
names_map = fetch_stock_names_map() # 🚀 載入證交所繁體中文正名對照表

summary_rows = []
errors = []

try:
    fugle_client = RestClient(api_key=os.getenv("FUGLE_API_KEY"))
    fugle_stock = fugle_client.stock
except: fugle_stock = None

with st.spinner("🔄 正在背景高速運算中（全面啟動批次天網比對）..."):
    try:
        batch_hist = yf.download(tickers, period="3mo", group_by='ticker', auto_adjust=True, progress=False)
    except Exception as e:
        st.error(f"❌ Yahoo Finance 批次下載失敗: {e}")
        batch_hist = pd.DataFrame()

    for ticker in tickers:
        try:
            if len(tickers) == 1: hist = batch_hist.copy()
            else:
                if ticker in batch_hist.columns.levels[0]: hist = batch_hist[ticker].dropna(how='all').copy()
                else: hist = yf.download(ticker, period="3mo", auto_adjust=True, progress=False)
            
            if hist.empty or len(hist) < 25:
                errors.append(f"{ticker} (Yahoo歷史數據不足)")
                continue
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            
            # 🎯 解決痛點2：直接從官方映射表抓取中文名稱，杜絕代碼霸榜
            company_name = names_map.get(fugle_symbol, ticker)

            # 籌碼匹配
            foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 查無資料"
            if legal_data.get("__USE_FINMIND__"):
                target_date = legal_data["__TARGET_DATE__"]
                foreign_buy, sitc_buy, chip_source = fetch_finmind_chips_for_date(GLOBAL_SESSION, fugle_symbol, target_date)
            else:
                stock_legal = legal_data.get(fugle_symbol)
                if stock_legal:
                    try:
                        foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                        sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                        chip_source = stock_legal.get('source', '官方盤後')
                    except: pass

            # 富果盤中行情即時對齊
            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    if quote and 'lastPrice' in quote and quote['lastPrice']:
                        hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                        fugle_vol = quote['total']['tradeVolume']
                        # 🎯 解決痛點1：富果傳回的是張數，必須乘以 1000 變成「股」塞入資料庫，對齊 yfinance！
                        if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000
                except: pass

            # 四維指標防彈運算
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
            
            # 真突破前高判定
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

if errors: st.sidebar.warning(f"⚠️ 異常提示：{', '.join(errors)}")

# 表格渲染
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
else: st.info("📭 目前清單為空。請點擊左側「🧹 清除網頁快取」後重新整理。")
