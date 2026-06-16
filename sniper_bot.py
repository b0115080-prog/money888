import os
import time
import datetime
import requests
import yfinance as yf
import pandas as pd
import feedparser
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from google import genai
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage
from fugle_marketdata import RestClient

# 載入 API 金鑰
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_KEY_BACKUP = os.getenv("GEMINI_API_KEY_BACKUP")
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")

def safe_val(val, default=0):
    try: return float(val) if not pd.isna(val) else default
    except: return default

def parse_vol(val):
    if isinstance(val, str):
        try: return int(val.replace(',', '')) // 1000
        except ValueError: return 0
    elif isinstance(val, (int, float)): 
        return int(val) // 1000
    return 0

# --- 1. 從 Google 試算表自動抓取最新觀察清單 ---
try:
    SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
    df_tickers = pd.read_csv(SHEET_CSV_URL, header=None) 
    df_tickers = df_tickers.dropna(subset=[0])
    df_tickers[0] = df_tickers[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
    tickers = df_tickers.iloc[:, 0].tolist()
    target_tickers = list(dict.fromkeys([t for t in tickers if t.lower() != 'nan' and t]))
    print(f"✅ 成功雲端讀取並全面淨化 {len(target_tickers)} 檔追蹤標的！")
except Exception as e:
    print(f"⚠️ 讀取 Google 表單失敗，使用備用清單。錯誤: {e}")
    target_tickers = ["0050.TW", "0052.TW"]

# 🚀 【功能補齊：證交所中文正名對照表】從官方獲取最權威的繁體中文名稱映射，徹底解決代碼霸榜問題
def fetch_stock_names_map(session):
    name_map = {}
    try:
        res = session.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=5)
        if res.status_code == 200:
            for item in res.json():
                code = item.get('Code', '').strip()
                name = item.get('Name', '').strip()
                if code and name: name_map[code] = name
    except: pass
    return name_map

# --- 2. 外部數據爬蟲模組 (完全線程安全解耦版) ---
def fetch_stock_news_v2_with_session(session, ticker, company_name):
    """🚀 [效能優化]：複用傳入的 session 抓取新聞，並採用標準 OR 語法限定財經媒體天網"""
    yahoo_news, google_news = [], []
    try:
        stock = yf.Ticker(ticker, session=session)
        for item in stock.news[:3]:
            title = item['content']['title'] if 'content' in item and 'title' in item['content'] else item.get('title', '')
            if title: yahoo_news.append(title)
    except: pass

    try:
        ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
        url = f"https://news.google.com/rss/search?q={ticker_digits}+{company_name}+(site:cnyes.com+OR+site:moneydj.com+OR+site:udn.com)&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        res = session.get(url, timeout=5)
        feed = feedparser.parse(res.text)
        for entry in feed.entries[:3]: google_news.append(entry.title)
    except: pass
    return yahoo_news[:3], google_news[:3]

def fetch_market_daily_data(tickers_list):
    """🚀 【背景機器人同步對齊：日期最優先決策天網核心】"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 先探測最新當日 OpenAPI
    for _ in range(2):
        try:
            res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=8)
            if res.status_code == 200 and len(res.json()) > 0:
                for item in res.json():
                    legal_data[item['Code'].strip()] = {
                        'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                        'sitc': item.get('InvestmentTrustBuyBuyOver', '0'),
                        'source': '官方盤後最新'
                    }
                return legal_data
        except: time.sleep(1)

    # 2. 跨日天網精準比對 (全面換裝最新 RWD 歷史端點，防止連線超時滑坡)
    for i in range(0, 6):
        dt = tw_now - datetime.timedelta(days=i)
        if dt.weekday() >= 5: continue
        date_str = dt.strftime("%Y%m%d")
        target_date_fm = dt.strftime("%Y-%m-%d")
        display_date = dt.strftime("%m/%d")
        
        day_success = False
        for _ in range(2):
            try:
                res = requests.get(f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date_str}&selectType=ALL", headers=headers, timeout=5)
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
            except: time.sleep(1)
        
        if day_success: return legal_data

        # 🚀 跨日同日攔截：若官方 06/15 連線中斷或尚未結算，原地強制轉向 FinMind 進行當日攔截
        if tickers_list:
            benchmark = tickers_list[0].replace('.TW', '').replace('.TWO', '').strip()
            try:
                url = "https://api.finmindtrade.com/api/v4/data"
                params = {"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": benchmark, "start_date": target_date_fm, "end_date": target_date_fm}
                res = requests.get(url, params=params, timeout=4)
                if res.json().get("msg") == "success" and len(res.json().get("data", [])) > 0:
                    return {"__USE_FINMIND__": True, "__TARGET_DATE__": target_date_fm}
            except: pass

    return legal_data

def fetch_finmind_chips_for_date(session, ticker_digits, target_date):
    """🚀 中英分流特定日期精準抓取模組"""
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
            return int(foreign_buy + fd_buy), int((s_df['buy'].sum() - s_df['sell'].sum()) // 1000), f"FinMind({target_date[5:].replace('-', '/')})"
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
                f_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
                fd_buy = (fd_df['buy'].sum() - fd_df['sell'].sum()) // 1000
                s_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
                if (f_buy + fd_buy) != 0 or s_buy != 0: return int(f_buy + fd_buy), int(s_buy), f"FinMind歷史({date[5:].replace('-', '/')})"
            return 0, 0, f"FinMind歷史({dates[0][5:].replace('-', '/')})"
    except: pass
    return "未取得", "未取得", "⚠️ 查無資料"

def fetch_latest_ptt_post_with_session(session, ticker_digits):
    try:
        url = f"https://news.google.com/rss/search?q=site:ptt.cc/bbs/Stock+{ticker_digits}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        res = session.get(url, timeout=5)
        feed = feedparser.parse(res.text)
        if feed.entries:
            latest_entry = feed.entries[0]
            clean_title = latest_entry.title.split(" - 看板")[0].strip()
            date_str = f"{latest_entry.published_parsed.tm_mon:02d}/{latest_entry.published_parsed.tm_mday:02d}" if hasattr(latest_entry, 'published_parsed') else "未知"
            return {"title": clean_title, "date": date_str}
    except: pass
    return None

def fetch_latest_dcard_post_with_session(session, ticker_digits):
    url = f"https://www.dcard.tw/_api/search/posts?query={ticker_digits}&forum=stock&limit=1"
    try:
        res = session.get(url, timeout=5)
        if res.status_code == 200 and res.json():
            post = res.json()[0]
            raw_date = post.get('createdAt', '')
            return {"title": post.get('title', ''), "date": f"{raw_date[5:7]}/{raw_date[8:10]}" if len(raw_date) >= 10 else "未知"}
    except: pass
    return None

# --- 3. AI 判讀模組 ---
def analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post):
    """🚀 [提示詞全面補齊]：完美放回權重加權大腦、心法矩陣與潛伏期判讀邏輯"""
    key1, key2 = os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_API_KEY_BACKUP")
    strategies = []
    if key1: strategies.append({"key": key1, "model": "gemini-2.5-flash"})
    if key2: strategies.append({"key": key2, "model": "gemini-2.5-flash"})
    if not strategies: return "⚠️ 未設定 Gemini API Key。"
    
    yahoo_text = "\n".join(f"- {n}" for n in yahoo_news) if yahoo_news else "暫無資料"
    google_text = "\n".join(f"- {n}" for n in google_news) if google_news else "暫無資料"
    ptt_text = f"標題：{ptt_post['title']} ({ptt_post['date']})" if ptt_post else "今日無專文討論"
    dcard_text = f"標題：{dcard_post['title']} ({dcard_post['date']})" if dcard_post else "今日無專文討論"
    
    # 🎯 操盤手反市場心理學權重提示詞完全體
    prompt = f"""請幫我對以下標的進行深度的反市場心理學研判。
    標的：{company_name} ({ticker})

    【目前狀態】
    - KD指標：{tech_info.get('kd_signal')} | MACD：{tech_info['macd_signal']} | 均線：{tech_info['ma_signal']}
    - RSI(14日)：{tech_info['rsi']:.1f} | 量能狀態：{tech_info['vol_signal']}
    - 近日單日外資買賣超：{tech_info.get('foreign_raw_buy')} 
    - 近日單日投信買賣超：{tech_info.get('sitc_raw_buy')}

    【最新新聞】
    {yahoo_text}
    {google_text}

    【社群討論】
    PTT: {ptt_text}
    Dcard: {dcard_text}

    🚨【操盤手核心研判思維】🚨
    1. 籌碼與技術權重高於輿情：外資與投信的真金白銀流入、以及帶量突破關鍵均線，是股價上漲的真動能。
    2. 潛伏期法則（極重要）：當「法人大舉買超/轉買」且「技術面低檔翻多/突破月線」時，若「社群討論極度冷清」，嚴禁判定為觀望或冷門股！這代表主力正在悄悄吃貨、散戶尚未察覺，請給予【強勢買點】評級。
    3. 誘多陷阱法則：反之，當股價處於高位階，雖然籌碼看似很好，但新聞瘋狂大放利多、且「社群討論極度狂熱/少年股神湧現」，必須給予【誘多陷阱】或【觀望】評級，提示主力出貨與利多出盡的系統性風險。

    🛑【輸出限制與格式】🛑
    1. 嚴格禁止使用任何 Markdown 粗體符號（絕對不要出現 ** ）。
    2. 在輸出最終報告前，請在心裡（不輸出）先進行三面向的權重計分：技術(30%)、籌碼(50%)、輿情(20%)，綜合加權後再給出結論。
    3. 綜合判斷原因請精煉至 100 字內，但必須包含籌碼與技術的交叉印證。

    請嚴格依照以下格式輸出繁體中文：

    🗣️ 網路社群輿情觀點：
    - PTT 最新動向：
    - Dcard 最新動向：
    - 散戶心理綜合研判：

    🤖 AI 綜合判讀報告：
    - 研判結論：(請從中精確選擇一項填入：強勢買點 / 觀望 / 誘多陷阱 / 資訊不足)
    - 綜合判斷原因：
    - 潛在風險提示："""

    for idx, strat in enumerate(strategies):
        try:
            client = genai.Client(api_key=strat["key"].strip())
            response = client.models.generate_content(model=strat["model"], contents=prompt)
            return response.text
        except:
            if idx < len(strategies) - 1: continue
    return "❌ 所有 AI 通道均已達上限，略過 AI 報告。"

def send_line_notify(message):
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID: return
    if len(message) > 4500: message = message[:4500] + "\n\n[⚠️ 內文長度觸發安全限制，已自動截斷]"
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            MessagingApi(api_client).push_message(PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)]))
        print("📲 LINE 通知發送成功！") 
    except Exception as e: print(f"⚠️ LINE 通知發送失敗: {e}")

# --- 🚀 多線程矩陣並行運算核心 ---
def process_single_ticker_thread_safe(ticker, legal_data, names_map, fugle_api_key):
    thread_session = requests.Session()
    thread_session.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try: thread_fugle = RestClient(api_key=fugle_api_key).stock
    except: thread_fugle = None

    try:
        stock = yf.Ticker(ticker, session=thread_session)
        hist = stock.history(period="6mo", auto_adjust=True, actions=False)
        if hist.empty or len(hist) < 30: return None
        fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
        
        # 🎯 [功能補齊]：綁定繁體中文正名，拒絕用乾癟的代碼敷衍
        company_name = names_map.get(fugle_symbol, ticker)

        foreign_buy, sitc_buy, chip_source_msg = "未取得", "未取得", "⚠️ 查無資料"
        if legal_data.get("__USE_FINMIND__"):
            target_date = legal_data["__TARGET_DATE__"]
            foreign_buy, sitc_buy, chip_source_msg = fetch_finmind_chips_for_date(thread_session, fugle_symbol, target_date)
        else:
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                    sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                    chip_source_msg = stock_legal.get('source', '官方盤後')
                except: pass

        if (foreign_buy in ["未取得", 0] or sitc_buy in ["未取得", 0]) and not legal_data.get("__USE_FINMIND__"):
            foreign_buy, sitc_buy, chip_source_msg = fetch_finmind_chips_fallback(thread_session, fugle_symbol)
        
        f_str = f"{foreign_buy} 張" if isinstance(foreign_buy, int) else "未取得"
        s_str = f"{sitc_buy} 張" if isinstance(sitc_buy, int) else "未取得"

        # 對齊盤中即時量能 (張數 * 1000 轉換為股數)
        if thread_fugle:
            try:
                quote = thread_fugle.intraday.quote(symbol=fugle_symbol)
                if quote and 'lastPrice' in quote and quote['lastPrice']:
                    hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                    fugle_vol = quote['total']['tradeVolume']
                    if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000
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
        denom = (high_max - low_min).replace(0, 1e-9)
        hist['RSV'] = ((hist['Close'] - low_min) / denom) * 100
        hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
        hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()
        
        today, yesterday = hist.iloc[-1], hist.iloc[-2]
        
        current_vol = safe_val(today['Volume'])
        if current_vol <= 0: current_vol = safe_val(yesterday['Volume']) 
        if current_vol <= 0: current_vol = 1 
        ma_vol = safe_val(today['5Vol_MA'], 1)
        if ma_vol <= 0: ma_vol = 1
        vol_ratio = current_vol / ma_vol
        
        # 實質 High 前高真突破算法
        highest_20d_high = hist['High'].rolling(window=20).max().iloc[-2]
        momentum_breakout = safe_val(today['Close']) > highest_20d_high
        
        golden_cross = (safe_val(yesterday['5MA']) <= safe_val(yesterday['20MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
        # 🎯 [公式對齊修復]：多頭強勢排列對齊
        bullish_alignment = (safe_val(today['Close']) > safe_val(today['5MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
        volume_surge = vol_ratio > 1.5
        kd_golden_cross = (safe_val(yesterday['K']) <= safe_val(yesterday['D'])) and (safe_val(today['K']) > safe_val(today['D']))
        
        macd_yest, macd_today = safe_val(yesterday['MACD_Hist']), safe_val(today['MACD_Hist'])
        macd_zero_cross = (macd_yest < 0) and (macd_today >= 0)
        macd_converge = (macd_today < 0) and (macd_today > macd_yest)
        
        avg_daily_volume_lots = ma_vol / 1000
        foreign_ratio = (foreign_buy / avg_daily_volume_lots) if (isinstance(foreign_buy, int) and avg_daily_volume_lots > 0) else 0
        sitc_ratio = (sitc_buy / avg_daily_volume_lots) if (isinstance(sitc_buy, int) and avg_daily_volume_lots > 0) else 0
        
        # --- 🚀 量化計分矩陣 ---
        score = 0
        if kd_golden_cross: score += 2
        if macd_zero_cross: score += 2
        if macd_converge: score += 1
        if golden_cross: score += 2
        if volume_surge: score += 1
        if momentum_breakout: score += 2
        if foreign_ratio > 0.05: score += 3
        if sitc_ratio > 0.02: score += 2
        
        ma_msg = "黃金交叉" if golden_cross else ("多頭排列" if bullish_alignment else "無明顯交會")
        kd_msg = "🔥 KD金叉" if kd_golden_cross else ("🟢 K>D" if safe_val(today['K']) > safe_val(today['D']) else "🔴 K<D")
        if macd_zero_cross: macd_msg = "🔥 底部反轉"
        elif macd_converge: macd_msg = "🟡 負柱收斂"
        else: macd_msg = "🟢 柱狀正常" if macd_today > 0 else "🔴 偏空下探"
        vol_msg = f"爆發量！({vol_ratio:.1f}倍)" if volume_surge else f"量能平穩({vol_ratio:.1f}倍)"
        
        # 🎯 [Bug徹底修正點]：將真實數值的張數(整數或未取得)獨立回傳，不要拿 f_str 這種含有中文字的字串去餵 AI 判斷！
        tech_info = {
            "rsi": safe_val(today['RSI'], 50), "ma_signal": ma_msg, "macd_signal": macd_msg, "kd_signal": kd_msg, "vol_signal": vol_msg,
            "foreign_buy": f_str, "sitc_buy": s_str,
            "foreign_raw_buy": f"{foreign_buy}張" if isinstance(foreign_buy, int) else "未取得",
            "sitc_raw_buy": f"{sitc_buy}張" if isinstance(sitc_buy, int) else "未取得"
        }
        
        return {
            "ticker": ticker, "company_name": company_name, "score": score, "tech_info": tech_info,
            "today_close": today['Close'], "vol_msg": vol_msg, "kd_msg": kd_msg, "macd_msg": macd_msg, "ma_msg": ma_msg,
            "chip_source_msg": chip_source_msg, "f_str": f_str, "s_str": s_str, "session": thread_session
        }
    except Exception as e: return None

def run_sniper_bot():
    print("[主力狙擊機器人 - 純上市多執行緒防彈完全體] 啟動！\n")
    legal_data = fetch_market_daily_data(target_tickers)
    
    # 調度單一臨時連線，取得證交所最權威的中文正名對照表
    temp_s = requests.Session()
    names_map = fetch_stock_names_map(temp_s)
    
    results_pool = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process_single_ticker_thread_safe, ticker, legal_data, names_map, FUGLE_API_KEY): ticker for ticker in target_tickers}
        for future in as_completed(futures):
            res = future.result()
            if res: results_pool.append(res)
            
    triggered_stocks = [r for r in results_pool if r["score"] >= 3]
    triggered_stocks.sort(key=lambda x: x['score'], reverse=True)
    final_winners = triggered_stocks[:10]
    
    print(f"🎯 矩陣初篩完成，本次共計 {len(triggered_stocks)} 檔達標，最終對最精準的 Top {len(final_winners)} 發射火炮。")
    
    for res in final_winners:
        ticker, company_name, score, session = res["ticker"], res["company_name"], res["score"], res["session"]
        yahoo_news, google_news = fetch_stock_news_v2_with_session(session, ticker, company_name)
        ptt_post = fetch_latest_ptt_post_with_session(session, ticker.replace('.TW', ''))
        dcard_post = fetch_latest_dcard_post_with_session(session, ticker.replace('.TW', ''))
        
        # 調用加權提示詞完全體的大腦報告
        ai_complete_report = analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, res["tech_info"], ptt_post, dcard_post)
        
        line_msg = f"\n🎯 發現獵物：{company_name} ({ticker}) [量化評級: {score}分]\n股價：{res['today_close']:.2f} / {res['vol_msg']}\n技術面：{res['kd_msg']} | {res['macd_msg']} | {res['ma_msg']}\n籌碼 ({res['chip_source_msg']})：外資買賣 {res['f_str']} | 投信買賣 {res['s_str']}\n----------------------\n{ai_complete_report}\n----------------------"
        send_line_notify(line_msg)
        time.sleep(2)

if __name__ == "__main__":
    run_sniper_bot()
