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

# --- 2. 外部數據爬蟲模組 (完全線程安全解耦版) ---
def fetch_stock_news_v2_with_session(session, ticker, company_name):
    """🚀 複用執行緒獨立連線池，並修正 OR 語法以防搜尋條件汙染"""
    yahoo_news, google_news = [], []
    try:
        stock = yf.Ticker(ticker, session=session)
        for item in stock.news[:3]:
            title = item['content']['title'] if 'content' in item and 'title' in item['content'] else item.get('title', '')
            if title: yahoo_news.append(title)
    except: pass

    try:
        ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
        # 🎯 語法修正：將舊版的 | 改為標準括號聯網 (A OR B OR C)，精準過濾權威財經媒體
        url = f"https://news.google.com/rss/search?q={ticker_digits}+{company_name}+(site:cnyes.com+OR+site:moneydj.com+OR+site:udn.com)&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        res = session.get(url, timeout=5)
        feed = feedparser.parse(res.text)
        for entry in feed.entries[:3]: google_news.append(entry.title)
    except: pass
    return yahoo_news[:3], google_news[:3]

def fetch_market_daily_data():
    """🚀 集中下載官方 OpenAPI 數據，做為多執行緒唯讀共享字典 (重試次數修正為 2 次)"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 1. 上市 OpenAPI
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
                break
        except: time.sleep(1)

    # 2. 歷史救援時光機
    if not legal_data:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue
            date_str = dt.strftime("%Y%m%d")
            
            day_success = False
            for _ in range(2): # 強制重試 2 次
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

def fetch_finmind_chips_with_session(session, ticker_digits):
    """🚀 中英分流去重欄位解耦演算法，徹底擊殺重複累加 Bug"""
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
                
                # 🎯 欄位正名：中英雙語版本獨立拆解加總，絕不模糊 contain
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
                    short_date = date[5:].replace('-', '/')
                    return int(total_foreign), int(sitc_buy), f"FinMind歷史({short_date})"
            
            latest_date = dates[0][5:].replace('-', '/')
            return 0, 0, f"FinMind歷史({latest_date})"
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
            date_str = f"{raw_date[5:7]}/{raw_date[8:10]}" if len(raw_date) >= 10 else "未知"
            return {"title": post.get('title', ''), "date": date_str}
    except: pass
    return None

# --- 3. AI 判讀模組 ---
def analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post):
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    strategies = []
    if key1: strategies.append({"key": key1, "model": "gemini-2.5-flash", "desc": "主要防線"})
    if key2: strategies.append({"key": key2, "model": "gemini-2.5-flash", "desc": "備用防線"})
        
    if not strategies: return "⚠️ 未設定 Gemini API Key。"
        
    yahoo_text = "\n".join(f"- {n}" for n in yahoo_news) if yahoo_news else "暫無資料"
    google_text = "\n".join(f"- {n}" for n in google_news) if google_news else "暫無資料"
    ptt_text = f"標題：{ptt_post['title']} ({ptt_post['date']})" if ptt_post else "今日無專文"
    dcard_text = f"標題：{dcard_post['title']} ({dcard_post['date']})" if dcard_post else "今日無專文"
    
    # 🎯 操盤手反市場加權提示詞大腦
    prompt = f"""
    你現在是精通台股基本面、籌碼面與社群輿情的頂尖量化操盤手。請幫我對以下標的進行深度的反市場心理學研判。

    標的：{company_name} ({ticker})

    【目前狀態】
    - KD指標：{tech_info.get('kd_signal')} | MACD：{tech_info['macd_signal']} | 均線：{tech_info['ma_signal']}
    - RSI(14日)：{tech_info['rsi']:.1f} | 量能狀態：{tech_info['vol_signal']}
    - 近日單日外資買賣超：{tech_info.get('foreign_buy')} 
    - 近日單日投信買賣超：{tech_info.get('sitc_buy')}

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
    - 潛在風險提示：
    """

    for idx, strat in enumerate(strategies):
        try:
            client = genai.Client(api_key=strat["key"].strip())
            response = client.models.generate_content(model=strat["model"], contents=prompt)
            return response.text
        except:
            if idx < len(strategies) - 1: continue
    return "❌ 所有 AI 通道均已達上限，略過 AI 報告。"

# --- 3.5 LINE 傳播模組 ---
def send_line_notify(message):
    """🚀 4500字安全防震截斷裝甲，確保徹底阻斷 LINE 的 400 報錯"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID: 
        print("⚠️ 未設定 LINE 金鑰，略過推播。")
        return
        
    MAX_LEN = 4500
    if len(message) > MAX_LEN:
        message = message[:MAX_LEN] + "\n\n[⚠️ 內文長度觸發安全限制，已自動截斷]"
        
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)]))
        print("📲 LINE 通知發送成功！") 
    except Exception as e:
        print(f"⚠️ LINE 通知發送失敗: {e}")

# --- 🚀 多線程矩陣並行運算核心 (100% 執行緒隔離安全版) ---
def process_single_ticker_thread_safe(ticker, legal_data, fugle_api_key):
    # 每個 Worker 執行緒建立自己專屬的 Session 獨立連線池與獨立 FugleClient
    thread_session = requests.Session()
    thread_session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    })
    
    try: thread_fugle = RestClient(api_key=fugle_api_key).stock
    except: thread_fugle = None

    try:
        # [物件快取提速] 同一隻股票只連線一次 Yahoo，同時獲取歷史 K 線
        stock = yf.Ticker(ticker, session=thread_session)
        hist = stock.history(period="6mo", auto_adjust=True, actions=False)
        if hist.empty or len(hist) < 30: return None
            
        fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
        
        # 公司名稱改走 Fugle，徹底移除會造成 429 封鎖的 stock.info 毒瘤
        company_name = ticker
        if thread_fugle:
            try:
                meta = thread_fugle.intraday.ticker(symbol=fugle_symbol)
                if meta and 'nameShort' in meta: company_name = meta['nameShort']
            except: pass
        
        # 籌碼決策樹
        foreign_buy, sitc_buy, chip_source_msg = "未取得", "未取得", "⚠️ 查無資料"
        stock_legal = legal_data.get(fugle_symbol)
        if stock_legal:
            try:
                foreign_buy = int(float(str(stock_legal.get('foreign', '0')).replace(',', ''))) // 1000
                sitc_buy = int(float(str(stock_legal.get('sitc', '0')).replace(',', ''))) // 1000
                chip_source_msg = stock_legal.get('source', '官方盤後')
            except: pass
        
        # 智能穿透備援：改為 OR 判斷，任一資料出缺口（盤中尚未發布）立刻時光追溯
        if foreign_buy in ["未取得", 0] or sitc_buy in ["未取得", 0]:
            f_fm, s_fm, source_fm = fetch_finmind_chips_with_session(thread_session, fugle_symbol)
            if isinstance(f_fm, int):
                foreign_buy, sitc_buy, chip_source_msg = f_fm, s_fm, source_fm
        
        f_str = f"{foreign_buy} 張" if isinstance(foreign_buy, int) else "未取得"
        s_str = f"{sitc_buy} 張" if isinstance(sitc_buy, int) else "未取得"

        # 對齊即時行情與成交量單位正名（不乘1000）
        if thread_fugle:
            try:
                quote = thread_fugle.intraday.quote(symbol=fugle_symbol)
                if quote and 'lastPrice' in quote and quote['lastPrice']:
                    hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                    fugle_vol = quote['total']['tradeVolume']
                    if fugle_vol > 0: hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol
            except: pass

        # 技術指標計算
        hist['5MA'] = hist['Close'].rolling(window=5).mean()
        hist['20MA'] = hist['Close'].rolling(window=20).mean()
        hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
        
        # RSI 零分母防護罩
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
        
        # KD 零分母防護罩
        low_min = hist['Low'].rolling(window=9).min()
        high_max = hist['High'].rolling(window=9).max()
        denom = (high_max - low_min).replace(0, 1e-9)
        hist['RSV'] = ((hist['Close'] - low_min) / denom) * 100
        hist['K'] = hist['RSV'].ewm(com=2, adjust=False).mean()
        hist['D'] = hist['K'].ewm(com=2, adjust=False).mean()
        
        today, yesterday = hist.iloc[-1], hist.iloc[-2]
        
        # 爆量防呆
        current_vol = safe_val(today['Volume'])
        if current_vol <= 0: current_vol = safe_val(yesterday['Volume']) 
        if current_vol <= 0: current_vol = 1 
        ma_vol = safe_val(today['5Vol_MA'], 1)
        if ma_vol <= 0: ma_vol = 1
        vol_ratio = current_vol / ma_vol
        
        # 實質前高突破判定：最高價 (High) 的 20 日滾動前高
        highest_20d_high = hist['High'].rolling(window=20).max().iloc[-2]
        momentum_breakout = safe_val(today['Close']) > highest_20d_high
        
        golden_cross = (safe_val(yesterday['5MA']) <= safe_val(yesterday['20MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
        bullish_alignment = (safe_val(today['Close']) > safe_val(today['5MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
        volume_surge = vol_ratio > 1.5
        kd_golden_cross = (safe_val(yesterday['K']) <= safe_val(yesterday['D'])) and (safe_val(today['K']) > safe_val(today['D']))
        
        macd_yest, macd_today = safe_val(yesterday['MACD_Hist']), safe_val(today['MACD_Hist'])
        macd_zero_cross = (macd_yest < 0) and (macd_today >= 0)
        macd_converge = (macd_today < 0) and (macd_today > macd_yest)
        
        # 🚀 比例占比算法：外資買超占均量百分比大於 5% 才能加分，徹底解決權值股天天霸榜Bug
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
        
        tech_info = {
            "rsi": safe_val(today['RSI'], 50), "ma_signal": ma_msg, "macd_signal": macd_msg, "kd_signal": kd_msg, "vol_signal": vol_msg,
            "foreign_buy": f_str, "sitc_buy": s_str
        }
        
        return {
            "ticker": ticker, "company_name": company_name, "score": score, "tech_info": tech_info,
            "today_close": today['Close'], "vol_msg": vol_msg, "kd_msg": kd_msg, "macd_msg": macd_msg, "ma_msg": ma_msg,
            "chip_source_msg": chip_source_msg, "f_str": f_str, "s_str": s_str, "session": thread_session
        }
    except Exception as e:
        print(f"❌ 執行緒安全 Worker 處理 {ticker} 異常: {e}")
        return None

# --- 4. 核心主程式掃描區 ---
def run_sniper_bot():
    print("[主力狙擊機器人 - 純上市多執行緒防彈版] 啟動！\n")
    legal_data = fetch_market_daily_data()
    
    results_pool = []
    print(f"📡 正在調度 6 組獨立並行線程，對 {len(target_tickers)} 檔上市標的展開同步突襲...")
    
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(process_single_ticker_thread_safe, ticker, legal_data, FUGLE_API_KEY): ticker for ticker in target_tickers}
        for future in as_completed(futures):
            res = future.result()
            if res: results_pool.append(res)
            
    print("\n📊 矩陣量化初篩完成，正在進行高勝率排雷防轟炸過濾...")
    
    # 🎯 限制放行門檻：計分大於等於 3 分才放行新聞與 AI 大腦
    triggered_stocks = [r for r in results_pool if r["score"] >= 3]
    triggered_stocks.sort(key=lambda x: x['score'], reverse=True)
    
    # 🚀 只篩選出分數最高的前 10 名核心飆股，進行重度資源調用，杜絕被 LINE 列為 Spam 的風險
    final_winners = triggered_stocks[:10]
    print(f"🎯 本次發現 {len(triggered_stocks)} 檔達標，最終對最精準的 Top {len(final_winners)} 發射火炮：\n")
    
    for res in final_winners:
        ticker = res["ticker"]
        company_name = res["company_name"]
        score = res["score"]
        session = res["session"] # 複用該執行緒內部健康的 Session 物件，省去再次連線建立成本
        
        print(f"🔥 [精準擊發] -> {company_name} ({ticker}) 初篩分數: {score} 分！拉取輿情與 Gemini 大腦決策...")
        
        # 複用 Session 直接由快取讀取
        yahoo_news, google_news = fetch_stock_news_v2_with_session(session, ticker, company_name)
        ptt_post = fetch_latest_ptt_post_with_session(session, ticker.replace('.TW', ''))
        dcard_post = fetch_latest_dcard_post_with_session(session, ticker.replace('.TW', ''))
        
        ai_complete_report = analyze_stock_with_gemini_ultra_lean(
            ticker, company_name, yahoo_news, google_news, res["tech_info"], ptt_post, dcard_post
        )
        
        line_msg = (
            f"\n🎯 發現獵物：{company_name} ({ticker}) [量化評級: {score}分]\n"
            f"股價：{res['today_close']:.2f} / {res['vol_msg']}\n"
            f"技術面：{res['kd_msg']} | {res['macd_msg']} | {res['ma_msg']}\n"
            f"籌碼 ({res['chip_source_msg']})：外資買賣 {res['f_str']} | 投信買賣 {res['s_str']}\n"
            f"----------------------\n"
            f"{ai_complete_report}\n"
            f"----------------------"
        )
        send_line_notify(line_msg)
        time.sleep(2) # 溫和冷卻緩衝，優雅應對 LINE API 頻率控制

if __name__ == "__main__":
    run_sniper_bot()
