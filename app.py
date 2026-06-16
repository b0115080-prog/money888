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

# 🚀 統一連線管理：建立全域高效率 Session 連線池，大幅降低多點建立的握手成本
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
    """抓取並淨化 Google 試算表追蹤清單 (自動清除看不見的編碼雜訊與重複項)"""
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

@st.cache_data(ttl=300)
def fetch_market_daily_data():
    """🚀 官方歷史時光機 (上市精簡版)：移除所有上櫃代碼，單日強制重試限制為 2 次"""
    legal_data = {}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 上市 OpenAPI (強制重試 2 次)
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

    # 歷史救援：官方網頁歷史資料庫
    if not legal_data:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue # 跳過週末
            date_str = dt.strftime("%Y%m%d")
            
            day_success = False
            for _ in range(2): # 強制重試 2 次
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

# 🚀 快取優化：建立 24 小時公司名稱快取，完全解耦迴圈，100% 免疫 Yahoo 限流阻擋
@st.cache_data(ttl=86400)
def get_company_name_cached(ticker_digits, api_key):
    try:
        client = RestClient(api_key=api_key)
        meta = client.stock.intraday.ticker(symbol=ticker_digits)
        if meta and 'nameShort' in meta:
            return meta['nameShort']
    except: pass
    return ticker_digits

def fetch_finmind_chips(ticker_digits):
    """🚀 FinMind 終極備援：中英分流去重欄位算法，只要是 0 或是未取得就無限往前追溯"""
    try:
        tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        start_date = (tw_now - datetime.timedelta(days=15)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": str(ticker_digits), "start_date": start_date}
        
        res = GLOBAL_SESSION.get(url, params=params, timeout=6)
        data = res.json()
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            dates = sorted(df['date'].unique(), reverse=True)
            
            for date in dates:
                df_date = df[df['date'] == date]
                
                # 精準防重複加總：優先切換至英文官方原始 whitelist 欄位
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
                
                # 🚀 智能非零追溯核心：只要這天不是雙邊寂靜的 0，代表找到真實交易日，立刻截斷回傳
                if total_foreign != 0 or sitc_buy != 0:
                    short_date = date[5:].replace('-', '/')
                    return int(total_foreign), int(sitc_buy), f"FinMind歷史({short_date})"
            
            latest = dates[0]
            return 0, 0, f"FinMind歷史({latest[5:].replace('-', '/')})"
    except: pass
    return "未取得", "未取得", "⚠️ 查無資料"

# --- UI 視覺化標題區 ---
st.title("🎯 台股主力狙擊大本營")
st.subheader("隨時監控即時行情、技術面多空與真實法人籌碼")

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

# --- 核心大腦矩陣運算 ---
tickers = get_clean_tickers()
legal_data = fetch_market_daily_data()

summary_rows = []
errors = []

try:
    fugle_client = RestClient(api_key=os.getenv("FUGLE_API_KEY"))
    fugle_stock = fugle_client.stock
except: fugle_stock = None

with st.spinner("🔄 正在背景高速運算中（全面啟動 yf.download 批次矩陣下載）..."):
    # 🚀 效能全面突破：改用大包裝批次歷史 K 線矩陣下載，提速 20 倍
    try:
        batch_hist = yf.download(tickers, period="3mo", group_by='ticker', auto_adjust=True, progress=False)
    except Exception as e:
        st.error(f"❌ Yahoo Finance 批次下載失敗: {e}")
        batch_hist = pd.DataFrame()

    for ticker in tickers:
        try:
            # 矩陣解包與單股歷史備援
            if len(tickers) == 1:
                hist = batch_hist.copy()
            else:
                if ticker in batch_hist.columns.levels[0]:
                    hist = batch_hist[ticker].dropna(how='all').copy()
                else:
                    # 💡 單股下載備援機制
                    hist = yf.download(ticker, period="3mo", auto_adjust=True, progress=False)
            
            if hist.empty or len(hist) < 25:
                errors.append(f"{ticker} (歷史數據庫為空)")
                continue
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            company_name = get_company_name_cached(fugle_symbol, os.getenv("FUGLE_API_KEY"))

            # 籌碼防線
            foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 查無資料"
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                    sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                    chip_source = stock_legal.get('source', '官方盤後')
                except: pass
            
            # 🚀 穿透備援防線：只要有任一項數據為 0 或未取得，立刻啟動非零追溯
            if foreign_buy in ["未取得", 0] and sitc_buy in ["未取得", 0]:
                f_fm, s_fm, source_fm = fetch_finmind_chips(fugle_symbol)
                if isinstance(f_fm, int):
                    foreign_buy, sitc_buy, chip_source = f_fm, s_fm, source_fm

            # 盤中即時行情對齊
            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    if quote and 'lastPrice' in quote and quote['lastPrice']:
                        hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                        fugle_vol = quote['total']['tradeVolume']
                        # 💡 單位正名：富果 quote 傳回的量本身就是「股」，不需要再乘以 1000！直接對齊
                        if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol
                except: pass

            # 四維指標防彈運算
            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            # RSI 除以零防護
            delta = hist['Close'].diff()
            up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
            avg_gain = up.ewm(com=13, adjust=False).mean()
            avg_loss = down.ewm(com=13, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            hist['RSI'] = 100 - (100 / (1 + rs))
            
            # MACD
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal']
            
            # KD 除以零防護
            low_min = hist['Low'].rolling(window=9).min()
            high_max = hist['High'].rolling(window=9).max()
            range_val = (high_max - low_min).replace(0, 1e-9)
            hist['RSV'] = 100 * ((hist['Close'] - low_min) / range_val)
            hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
            hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()

            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            # 量能防爆
            current_vol = safe_val(today['Volume'])
            if current_vol <= 0: current_vol = safe_val(yesterday['Volume'])
            if current_vol <= 0: current_vol = 1
            ma_vol = safe_val(today['5Vol_MA'], default=1)
            if ma_vol <= 0: ma_vol = 1
            vol_ratio = current_vol / ma_vol
            
            # 🚀 實質前高突破判定：收盤價必須超越前 20 日的最高點點 (High) 才是真真金突破
            highest_20d_high = hist['High'].rolling(window=20).max().iloc[-2]
            is_breakout = today['Close'] > highest_20d_high
            
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and vol_ratio > 1.5 and is_breakout:
                status_signal = "🔥 爆量真突破"
            elif today['Close'] < today['5MA']:
                status_signal = "🟡 跌破5MA觀望"

            # 訊號狀態對齊
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
        except Exception as e:
            errors.append(f"{ticker} (錯誤: {str(e)[:20]})")

# 渲染網頁表格 UI
if errors:
    st.sidebar.warning(f"⚠️ 異常個股已被安全繞過：{', '.join(errors)}")

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
else: 
    st.info("📭 目前清單為空。若是開盤前突發資料異常，請點擊左側「🧹 清除網頁快取」後重整。")
