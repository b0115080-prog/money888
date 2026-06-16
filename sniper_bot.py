import os
import time
import datetime
import requests
import yfinance as yf
import pandas as pd
import feedparser
import re
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

# 🚀 [功能註解] 數值防彈衣：避免抓到空值 NaN 導致後續數學運算崩潰當機
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
    
    # 🚀 [功能註解] 幽靈清道夫：移除試算表裡的空行 (NaN) 與隱形空白，並確保代號不重複
    df_tickers = df_tickers.dropna(subset=[0])
    df_tickers[0] = df_tickers[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
    tickers = df_tickers.iloc[:, 0].tolist()
    target_tickers = list(dict.fromkeys([t for t in tickers if t.lower() != 'nan' and t]))
    print(f"✅ 成功讀取並淨化 {len(target_tickers)} 檔追蹤標的！")
except Exception as e:
    print(f"⚠️ 讀取 Google 表單失敗，使用備用清單。錯誤: {e}")
    target_tickers = ["0050.TW", "0052.TW"]

# --- 2. 外部數據爬蟲模組 ---
def fetch_stock_news_v2(ticker, company_name):
    """🚀 [功能註解] 雙雷達新聞：結合 Yahoo 與 Google News，讓 AI 看懂最新題材"""
    yahoo_news, google_news = [], []
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0"})
        stock = yf.Ticker(ticker, session=session)
        for item in stock.news[:3]:
            title = item['content']['title'] if 'content' in item and 'title' in item['content'] else item.get('title', '')
            if title: yahoo_news.append(title)
    except: pass

    try:
        ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
        url = f"https://news.google.com/rss/search?q={ticker_digits}+{company_name}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]: google_news.append(entry.title)
    except: pass
    return yahoo_news[:3], google_news[:3]

def fetch_market_daily_data():
    """🚀 [功能註解] 官方防滑坡時光機：強制鎖定台灣時區 (UTC+8)，避免抓錯日期，並且針對單日強制重試 3 次防超時"""
    legal_data = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    
    # 強制台灣時間，避免 GitHub 主機時差問題
    tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)

    # 第一道防線：上市 OpenAPI
    try:
        res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=8)
        if res.status_code == 200 and len(res.json()) > 0:
            for item in res.json():
                legal_data[item['Code'].strip()] = {
                    'foreign': item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0')),
                    'sitc': item.get('InvestmentTrustBuyBuyOver', '0'),
                    'source': '官方盤後最新'
                }
    except: pass

    # 歷史救援：上市官方時光機
    if not legal_data:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue # 跳過週末
            date_str = dt.strftime("%Y%m%d")
            
            day_success = False
            for _ in range(3): # 單日強制重試，防連線超時
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
            if day_success: break # 抓到就停，不往更舊的日期找

    # 第一道防線：上櫃 OpenAPI
    tpex_found = False
    try:
        res = requests.get("https://openapi.tpex.org.tw/v1/tpex_38", headers=headers, timeout=8)
        if res.status_code == 200 and len(res.json()) > 0:
            for item in res.json():
                legal_data[item['SecuritiesCompanyCode'].strip()] = {
                    'foreign': item.get('ForeignInvestorsNetBuySell', '0'),
                    'sitc': item.get('InvestmentTrustsNetBuySell', '0'),
                    'source': '官方盤後最新'
                }
            tpex_found = True
    except: pass

    # 歷史救援：上櫃官方時光機
    if not tpex_found:
        for i in range(1, 6):
            dt = tw_now - datetime.timedelta(days=i)
            if dt.weekday() >= 5: continue
            date_str = f"{dt.year-1911}/{(dt).strftime('%m/%d')}"
            
            day_success = False
            for _ in range(3):
                try:
                    res = requests.get(f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&d={date_str}&se=EW&t=D", headers=headers, timeout=5)
                    data = res.json()
                    if data.get('iTotalRecords', 0) > 0:
                        for row in data['aaData']:
                            code = row[0].strip()
                            f_buy = int(row[4].replace(',', '')) + int(row[7].replace(',', ''))
                            s_buy = int(row[10].replace(',', ''))
                            if code not in legal_data:
                                legal_data[code] = {'foreign': str(f_buy), 'sitc': str(s_buy), 'source': f'官方歷史({dt.strftime("%m/%d")})'}
                        day_success = True
                        break
                except: time.sleep(1)
            if day_success: break

    return legal_data

def fetch_finmind_chips(ticker_digits):
    """🚀 [功能註解] 終極三備援：如果官方 OpenAPI 當機、官方歷史也封鎖你，啟動開源 FinMind 挖出籌碼"""
    try:
        tw_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        start_date = (tw_now - datetime.timedelta(days=10)).strftime("%Y-%m-%d")
        url = "https://api.finmindtrade.com/api/v4/data"
        params = {
            "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "data_id": str(ticker_digits),
            "start_date": start_date
        }
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        
        if data.get("msg") == "success" and len(data.get("data", [])) > 0:
            df = pd.DataFrame(data["data"])
            latest_date = df['date'].max()
            df_latest = df[df['date'] == latest_date]
            
            f_df = df_latest[df_latest['name'].str.contains('外資')]
            foreign_buy = (f_df['buy'].sum() - f_df['sell'].sum()) // 1000
            
            s_df = df_latest[df_latest['name'].str.contains('投信')]
            sitc_buy = (s_df['buy'].sum() - s_df['sell'].sum()) // 1000
            
            short_date = latest_date[5:].replace('-', '/')
            return int(foreign_buy), int(sitc_buy), f"FinMind歷史({short_date})"
    except Exception as e:
        print(f"⚠️ FinMind 連線失敗: {e}")
    return "未取得", "未取得", "⚠️ 查無資料"

def fetch_latest_ptt_post(ticker_digits):
    try:
        url = f"https://news.google.com/rss/search?q=site:ptt.cc/bbs/Stock+{ticker_digits}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        if feed.entries:
            latest_entry = feed.entries[0]
            clean_title = latest_entry.title.split(" - 看板")[0].strip()
            date_str = "未知"
            if hasattr(latest_entry, 'published_parsed') and latest_entry.published_parsed:
                tm = latest_entry.published_parsed
                date_str = f"{tm.tm_mon:02d}/{tm.tm_mday:02d}"
            return {"title": clean_title, "date": date_str}
    except: pass
    return None

def fetch_latest_dcard_post(ticker_digits):
    url = f"https://www.dcard.tw/_api/search/posts?query={ticker_digits}&forum=stock&limit=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200 and res.json():
            post = res.json()[0]
            raw_date = post.get('createdAt', '')
            date_str = f"{raw_date[5:7]}/{raw_date[8:10]}" if len(raw_date) >= 10 else "未知"
            return {"title": post.get('title', ''), "date": date_str}
    except: pass
    return None

# --- 3. AI 判讀模組 ---
def analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post):
    """🚀 [功能註解] AI 交叉防禦池：擁有備用金鑰與反市場心理學邏輯"""
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    strategies = []
    if key1: strategies.append({"key": key1, "model": "gemini-3.1-flash-lite", "desc": "主要金鑰 + 3.1-Flash-Lite"})
    if key2: strategies.append({"key": key2, "model": "gemini-3.1-flash-lite", "desc": "備用金鑰 + 3.1-Flash-Lite"})
        
    if not strategies: return "⚠️ 未設定 Gemini API Key。"
        
    yahoo_text = "\n".join(f"- {n}" for n in yahoo_news) if yahoo_news else "暫無資料"
    google_text = "\n".join(f"- {n}" for n in google_news) if google_news else "暫無資料"
    ptt_text = f"標題：{ptt_post['title']} ({ptt_post['date']})" if ptt_post else "今日無專文"
    dcard_text = f"標題：{dcard_post['title']} ({dcard_post['date']})" if dcard_post else "今日無專文"
    
    prompt = f"""
    你現在是精通台股基本面、籌碼面與社群輿情的頂尖量化操盤手。
    標的：{company_name} ({ticker})
    
    【目前狀態】
    - KD指標：{tech_info.get('kd_signal')} | MACD：{tech_info['macd_signal']} | 均線：{tech_info['ma_signal']}
    - RSI(14日)：{tech_info['rsi']:.1f} | 量能狀態：{tech_info['vol_signal']}
    - 近日單日外資買賣超：{tech_info.get('foreign_buy')} 
    - 近日單日投信買賣超：{tech_info.get('sitc_buy')}
    
    【最新新聞】
    {yahoo_text}\n{google_text}
    
    【社群討論】
    PTT: {ptt_text} | Dcard: {dcard_text}

    🚨【主力反市場心理學法則】🚨
    1. 當「法人大舉買超」且「技術面翻多」時，若「社群討論極低」，【嚴禁】判定為冷門！這代表「散戶尚未察覺」，請給予『強勢買點』評級，視為潛伏期。
    2. 反之，當籌碼好但「社群極度狂熱」時，必須提示主力出貨風險。

    請依照以下格式輸出繁體中文（不要包含任何 markdown 粗體符號 **）：
    🗣️ 網路社群輿情觀點：
    - PTT 最新動向：
    - Dcard 最新動向：
    - 散戶心理綜合研判：
    
    🤖 AI 綜合判讀報告：
    - 研判結論：(強勢買點 / 觀望 / 誘多陷阱 / 資訊不足)
    - 綜合判斷原因：(100字內)
    - 潛在風險提示：
    """

    for idx, strat in enumerate(strategies):
        try:
            print(f"   > 🛡️ 正在嘗試 AI 防線 {idx+1}: {strat['desc']}...")
            client = genai.Client(api_key=strat["key"].strip())
            response = client.models.generate_content(model=strat["model"], contents=prompt)
            return response.text
        except Exception as e:
            if "429" in str(e) and idx < len(strategies) - 1:
                time.sleep(5)
                continue
    return "❌ 所有 AI 通道均已達上限，略過 AI 報告。"

# --- 3.5 LINE 傳播模組 ---
def send_line_notify(message):
    """🚀 [功能註解] LINE 推播防線：不僅能發送，還會印出明確的成功與失敗報錯，方便 GitHub 除錯"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID: 
        print("⚠️ 未設定 LINE 金鑰，略過推播。")
        return
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)]))
        print("📲 LINE 通知發送成功！") # ✅ 你遺漏的關鍵提示已經加回
    except Exception as e:
        print(f"⚠️ LINE 通知發送失敗: {e}")

# --- 4. 核心掃描策略 ---
def run_sniper_bot():
    print("[主力狙擊機器人 - 官方時光機防彈版] 啟動！\n")
    legal_data = fetch_market_daily_data()
    
    try: fugle_stock = RestClient(api_key=FUGLE_API_KEY).stock
    except: fugle_stock = None
    
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    for ticker in target_tickers:
        try:
            stock = yf.Ticker(ticker, session=session)
            hist = stock.history(period="6mo", actions=True)
            if hist.empty or len(hist) < 30: continue
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            company_name = stock.info.get("shortName", ticker)
            
            # 🚀 [功能註解] 籌碼決策樹：官方 ➔ FinMind ➔ 放棄
            stock_legal = legal_data.get(fugle_symbol)
            if stock_legal:
                try:
                    f_buy_raw = str(stock_legal.get('foreign', '0')).replace(',', '')
                    s_buy_raw = str(stock_legal.get('sitc', '0')).replace(',', '')
                    foreign_buy = int(float(f_buy_raw)) // 1000
                    sitc_buy = int(float(s_buy_raw)) // 1000
                    chip_source_msg = stock_legal.get('source', '官方盤後')
                    f_str, s_str = f"{foreign_buy} 張", f"{sitc_buy} 張"
                except:
                    f_str, s_str, chip_source_msg = "未取得", "未取得", "解析異常"
            else:
                # 啟動 FinMind 備援 (✅ 補回前一版遺漏的功能)
                f_fm, s_fm, source_fm = fetch_finmind_chips(fugle_symbol)
                if isinstance(f_fm, int):
                    f_str, s_str, chip_source_msg = f"{f_fm} 張", f"{s_fm} 張", source_fm
                else:
                    f_str, s_str, chip_source_msg = "未取得", "未取得", "⚠️ 查無資料"
            
            # 🚀 [功能註解] 即時量能單位同步 (*1000防呆)
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
            
            # 🚀 [功能註解] 防止 NaN 與 0 崩潰的量能計算
            current_vol = safe_val(today['Volume'])
            if current_vol <= 0: current_vol = safe_val(yesterday['Volume']) 
            if current_vol <= 0: current_vol = 1 
            ma_vol = safe_val(today['5Vol_MA'], 1)
            if ma_vol <= 0: ma_vol = 1
            vol_ratio = current_vol / ma_vol
            
            golden_cross = (safe_val(yesterday['5MA']) <= safe_val(yesterday['20MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
            bullish_alignment = (safe_val(today['Close']) > safe_val(today['5MA'])) and (safe_val(today['5MA']) > safe_val(today['20MA']))
            volume_surge = vol_ratio > 1.5
            momentum_breakout = bullish_alignment and (safe_val(today['Close']) >= safe_val(yesterday['Close']) * 1.04)
            kd_golden_cross = (safe_val(yesterday['K']) <= safe_val(yesterday['D'])) and (safe_val(today['K']) > safe_val(today['D']))
            
            macd_yest, macd_today = safe_val(yesterday['MACD_Hist']), safe_val(today['MACD_Hist'])
            macd_zero_cross = (macd_yest < 0) and (macd_today >= 0)
            macd_converge = (macd_today < 0) and (macd_today > macd_yest)
            
            ma_msg = "黃金交叉" if golden_cross else ("多頭排列" if bullish_alignment else "無明顯交會")
            kd_msg = "🔥 KD金叉" if kd_golden_cross else ("🟢 K>D" if safe_val(today['K']) > safe_val(today['D']) else "🔴 K<D")
            if macd_zero_cross: macd_msg = "🔥 底部反轉"
            elif macd_converge: macd_msg = "🟡 負柱收斂"
            else: macd_msg = "🟢 柱狀正常" if macd_today > 0 else "🔴 偏空下探"
                
            vol_msg = f"爆發量！({vol_ratio:.1f}倍)" if volume_surge else f"量能平穩({vol_ratio:.1f}倍)"
            
            # 🚀 [功能註解] 確保 AI 收到精準籌碼 (已修正上一版未傳入的 Bug)
            tech_info = {
                "rsi": safe_val(today['RSI'], 50), "ma_signal": ma_msg, "macd_signal": macd_msg, "kd_signal": kd_msg, "vol_signal": vol_msg,
                "foreign_buy": f_str, "sitc_buy": s_str
            }
            
            is_triggered = golden_cross or kd_golden_cross or macd_zero_cross or macd_converge or volume_surge or momentum_breakout
            
            if is_triggered: 
                print(f"\n[發現獵物] {company_name} ({ticker}) 觸發警報！")
                ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
                yahoo_news, google_news = fetch_stock_news_v2(ticker, company_name)
                ptt_post = fetch_latest_ptt_post(ticker_digits)
                dcard_post = fetch_latest_dcard_post(ticker_digits)
                
                ai_complete_report = analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post)
                
                # 🚀 [功能註解] 組合推播訊息，確保排版清晰與單位正確
                line_msg = f"\n🎯 發現獵物：{company_name} ({ticker})\n股價：{today['Close']:.2f} / {vol_msg}\n技術面：{kd_msg} | {macd_msg} | {ma_msg}\n籌碼 ({chip_source_msg})：外資買賣 {f_str} | 投信買賣 {s_str}\n----------------------\n{ai_complete_report}\n----------------------"
                send_line_notify(line_msg)
                time.sleep(15)
            else:
                print(f"- {company_name} ({ticker}) 指標平淡，繼續潛伏。")
            time.sleep(0.5)
        except Exception as e:
            print(f"X 處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    run_sniper_bot()
