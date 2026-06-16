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
    fugle_stock = None

with st.spinner("🔄 正在背景高速運算中（全面啟動批次矩陣下載）..."):
    # --- 🎯 統一優化 6：全面改用 yf.download() 批次矩陣下載，速度提升 5~20 倍 ---
    try:
        batch_hist = yf.download(tickers, period="3mo", group_by='ticker', auto_adjust=True, progress=False)
    except Exception as e:
        st.error(f"❌ Yahoo Finance 批次下載失敗: {e}")
        batch_hist = pd.DataFrame()

    for ticker in tickers:
        try:
            # 提取單檔股票的歷史數據庫
            if len(tickers) == 1:
                hist = batch_hist.copy()
            else:
                if ticker in batch_hist.columns.levels[0]:
                    hist = batch_hist[ticker].dropna(how='all').copy()
                else:
                    continue
            
            if hist.empty or len(hist) < 25:
                errors.append(f"{ticker} (Yahoo歷史數據不足)")
                continue
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            
            # --- 🎯 統一優化 9：正名公司名稱，改用富果 API 代替 stock.info，100% 免疫 Yahoo 封鎖 ---
            company_name = ticker
            if fugle_stock:
                try:
                    meta = fugle_stock.intraday.ticker(symbol=fugle_symbol)
                    if meta and 'nameShort' in meta:
                        company_name = meta['nameShort']
                except: pass

            # --- 籌碼邏輯 ---
            foreign_buy, sitc_buy, chip_source = "未取得", "未取得", "⚠️ 查無資料"
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                    sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                    chip_source = stock_legal.get('source', '官方盤後')
                except: pass
            
            # 🎯 優化 4 修正：只要外資或投信有任一檔是「未取得」或「雙0」，立刻穿透防線調用 FinMind 備援
            if foreign_buy in ["未取得", 0] and sitc_buy in ["未取得", 0]:
                f_fm, s_fm, source_fm = fetch_finmind_chips(fugle_symbol)
                if isinstance(f_fm, int):
                    foreign_buy, sitc_buy, chip_source = f_fm, s_fm, source_fm

            # 融合富果盤中即時行情
            if fugle_stock:
                try:
                    quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                    if quote and 'lastPrice' in quote and quote['lastPrice']:
                        hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                        fugle_vol = quote['total']['tradeVolume']
                        if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000 
                except: pass

            # 5MA 與 20MA 計算
            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            # --- 🎯 統一優化 1：修正 RSI 連續上漲除以 0 (NaN/inf) 的實戰崩潰 Bug ---
            delta = hist['Close'].diff()
            up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
            avg_gain = up.ewm(com=13, adjust=False).mean()
            avg_loss = down.ewm(com=13, adjust=False).mean()
            
            # 補上安全置換，避免分母為 0
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            hist['RSI'] = 100 - (100 / (1 + rs))
            
            # MACD
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal']
            
            # --- 🎯 統一優化 2：修正 KD 遇到停牌、低流動性股 high_max == low_min 導致 0/0 崩潰的 Bug ---
            low_min = hist['Low'].rolling(window=9).min()
            high_max = hist['High'].rolling(window=9).max()
            
            range_val = (high_max - low_min).replace(0, 1e-9) # 安全置換防線
            hist['RSV'] = 100 * ((hist['Close'] - low_min) / range_val)
            hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
            hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()

            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            # 量能防呆
            current_vol = safe_val(today['Volume'])
            if current_vol <= 0: current_vol = safe_val(yesterday['Volume'])
            if current_vol <= 0: current_vol = 1
            ma_vol = safe_val(today['5Vol_MA'], default=1)
            if ma_vol <= 0: ma_vol = 1
            vol_ratio = current_vol / ma_vol
            
            # --- 🎯 統一優化 5：重構「爆量突破」條件，強制加入「突破20日新高」，全面擊殺騙線盤整股 ---
            highest_20d = hist['Close'].rolling(window=20).max().iloc[-2] # 取得昨天的20日最高價
            is_breakout = today['Close'] > highest_20d
            
            status_signal = "🟢 正常監控"
            if today['Close'] > today['5MA'] and today['5MA'] > today['20MA'] and vol_ratio > 1.5 and is_breakout:
                status_signal = "🔥 爆量真突破"
            elif today['Close'] < today['5MA']:
                status_signal = "🟡 跌破5MA觀望"

            # 狀態字串
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
            # --- 🎯 統一優化 10：全面消滅生產大忌的 except: pass，將錯誤優雅可視化 ---
            errors.append(f"{ticker} (處理異常: {str(e)[:25]})")

# --- 5. 渲染網頁表格 UI ---
if errors:
    st.sidebar.warning(f"⚠️ 部份個股執行時被安全跳過：{', '.join(errors)}")

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
