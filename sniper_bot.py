import os
import time
import requests
import yfinance as yf
import pandas as pd
import feedparser
import re
from bs4 import BeautifulSoup
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

# --- 0. 工具函式（移至全域，避免迴圈內重複宣告消耗效能） ---
def parse_vol(val):
    """將證交所原始股數轉換為張數"""
    if isinstance(val, str):
        try:
            return int(val.replace(',', '')) // 1000
        except ValueError:
            return 0
    elif isinstance(val, (int, float)):
        return int(val) // 1000
    return 0

# --- 1. 從 Google 試算表自動抓取最新觀察清單 ---
try:
    SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQz7MmTCJQAkMs8qpyLtpQOuZF4LpW3f3or51CH0USOIFLgEATnjUcX4lP6JfKl7RPTciy4-cEDPYmg/pub?output=csv"
    df_tickers = pd.read_csv(SHEET_CSV_URL, header=None) 
    
    # 🚀 核心防禦：強制洗掉從網頁複製時可能夾帶的 \u200b 等看不見的隱形幽靈字元雜訊
    df_tickers[0] = df_tickers[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
    target_tickers = [t for t in df_tickers[0].dropna().tolist() if t and t.strip()]
    
    print(f"✅ 成功從雲端讀取並全面淨化 {len(target_tickers)} 檔追蹤標的！")
except Exception as e:
    print(f"⚠️ 讀取 Google 表單失敗，使用備用清單。錯誤: {e}")
    target_tickers = ["0050.TW", "0052.TW"]

# --- 2. 外部數據爬蟲模組 ---
def fetch_stock_news_v2(ticker, company_name):
    """【精準分流】嚴格抓取 Yahoo 3 則 + Google News 3 則最新相關新聞"""
    yahoo_news, google_news = [], []
    try:
        stock = yf.Ticker(ticker)
        for item in stock.news[:3]:
            title = item['content']['title'] if 'content' in item and 'title' in item['content'] else item.get('title', '')
            if title: yahoo_news.append(title)
    except Exception: pass

    try:
        ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
        url = f"https://news.google.com/rss/search?q={ticker_digits}+{company_name}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]: google_news.append(entry.title)
    except Exception: pass
        
    return yahoo_news[:3], google_news[:3]

def fetch_twse_daily_data():
    """從證交所 OpenAPI 抓取法人買賣超與本益比，並強制對 Key 進行去空白淨化"""
    print("📥 正在從證交所下載官方盤後籌碼與財報數據...")
    legal_data = {}
    try:
        legal_entity_res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", timeout=8)
        if legal_entity_res.status_code == 200:
            for item in legal_entity_res.json():
                code = item.get('Code' or 'StockNo', '').strip()
                if code:
                    legal_data[code] = item
    except Exception as e:
        print(f"⚠️ 證交所籌碼下載失敗: {e}")

    pe_data = {}
    try:
        pe_res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", timeout=8)
        if pe_res.status_code == 200:
            for item in pe_res.json():
                code = item.get('Code', '').strip()
                if code:
                    pe_data[code] = item
    except Exception as e:
        print(f"⚠️ 證交所本益比下載失敗: {e}")

    return legal_data, pe_data

def fetch_fugle_yesterday_chips(symbol):
    """【富果籌碼救援】當今日官方未開牌時，直接去富果撈上一個交易日的精準法人買賣超張數"""
    try:
        req_url = f"https://api.fugle.tw/marketdata/v1.0/stock/snapshot/{symbol}"
        headers = {"X-API-KEY": os.getenv("FUGLE_API_KEY")}
        res = requests.get(req_url, headers=headers, timeout=5)
        
        if res.status_code == 200:
            data = res.json()
            legal_info = data.get('legalEntityBuySellOver', {})
            
            # 富果官方單位為股，除以 1000 轉換為張數
            foreign_buy = int(legal_info.get('foreignInvestor', 0)) // 1000
            sitc_buy = int(legal_info.get('investmentTrust', 0)) // 1000
            return foreign_buy, sitc_buy
    except Exception as e:
        print(f"⚠️ 富果籌碼備援庫調用失敗: {e}")
    return 0, 0

def fetch_latest_ptt_post(ticker_digits):
    """【黑科技繞過版】利用 Google RSS 間接搜尋 PTT 股版文章，100% 免疫 PTT 官方的 10054 封鎖"""
    try:
        url = f"https://news.google.com/rss/search?q=site:ptt.cc/bbs/Stock+{ticker_digits}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        if feed.entries:
            latest_entry = feed.entries[0]
            raw_title = latest_entry.title
            clean_title = raw_title.split(" - 看板")[0].strip()
            
            date_str = "未知"
            if hasattr(latest_entry, 'published_parsed') and latest_entry.published_parsed:
                tm = latest_entry.published_parsed
                date_str = f"{tm.tm_mon:02d}/{tm.tm_mday:02d}"
            
            return {"title": clean_title, "date": date_str}
    except Exception as e:
        print(f"⚠️ PTT 間接搜尋失敗: {e}")
    return None

def fetch_latest_dcard_post(ticker_digits):
    """【精準搜尋】透過 Dcard 搜尋 API 鎖定股市版該股代號，只取最新一則與日期"""
    url = f"https://www.dcard.tw/_api/search/posts?query={ticker_digits}&forum=stock&limit=1"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    try:
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            posts = res.json()
            if posts:
                post = posts[0]
                raw_date = post.get('createdAt', '')
                date_str = "未知"
                if len(raw_date) >= 10:
                    parts = raw_date[:10].split('-')
                    if len(parts) == 3: date_str = f"{parts[1]}/{parts[2]}"
                return {"title": post.get('title', ''), "date": date_str}
    except Exception as e:
        print(f"⚠️ Dcard 搜尋失敗: {e}")
    return None

# --- 3. AI 判讀模組 (雙金鑰 X 雙模型 4階段交叉防禦版) ---
def analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post):
    """【交叉防禦矩陣版】若 3.1 額度耗盡，自動動態避難並同時切換備用金鑰與 Gemini 3 模型"""
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    strategies = []
    if key1:
        strategies.append({"key": key1, "model": "gemini-3.1-flash-lite", "desc": "主要金鑰 + 3.1-Flash-Lite (500次核心池)"})
    if key2:
        strategies.append({"key": key2, "model": "gemini-3.1-flash-lite", "desc": "備用金鑰 + 3.1-Flash-Lite (500次核心池)"})
    if key1:
        strategies.append({"key": key1, "model": "gemini-3-flash", "desc": "主要金鑰 + Gemini-3-Flash (20次備用池)"})
    if key2:
        strategies.append({"key": key2, "model": "gemini-3-flash", "desc": "備用金鑰 + Gemini-3-Flash (20次備用池)"})
        
    if not strategies: 
        return "⚠️ 未設定任何 Gemini API Key，跳過 AI 判讀。"
        
    if key1 and key2 and key1.strip() != key2.strip():
        print(f"📢【除錯資訊】雙金鑰交叉驗證通過（主要長度: {len(key1)} / 備用長度: {len(key2)}）。")
        
    yahoo_text = "\n".join(f"- {n}" for n in yahoo_news) if yahoo_news else "暫無資料"
    google_text = "\n".join(f"- {n}" for n in google_news) if google_news else "暫無資料"
    ptt_text = f"標題：{ptt_post['title']} (發文日期：{ptt_post['date']})" if ptt_post else "今日無相關專文討論"
    dcard_text = f"標題：{dcard_post['title']} (發文日期：{dcard_post['date']})" if dcard_post else "今日無相關專文討論"
    
    prompt = f"""
    你現在是精通台股基本面、籌碼面與社群輿情（PTT/Dcard）的量化交易員。
    標的：{company_name} ({ticker})
    
    【目前技術與籌碼狀態】
    - RSI(14日)：{tech_info['rsi']:.1f} | 均線：{tech_info['ma_signal']} | MACD：{tech_info['macd_signal']}
    - 官方本益比：{tech_info['pe']} | 外資：{tech_info.get('foreign_buy', 0)}張 | 投信：{tech_info.get('sitc_buy', 0)}張
    
    【最新 Yahoo 新聞 (最多3則)】
    {yahoo_text}
    
    【最新 Google News (最多3則)】
    {google_text}
    
    【PTT 股版最新個股討論】
    {ptt_text}
    
    【Dcard 股市版最新個股討論】
    {dcard_text}
    
    請綜合上述精簡數據，精準研判這是『實質利多真突破』還是『主力騙線陷阱』？
    嚴格依照以下格式輸出繁體中文（不要包含任何 markdown 粗體符號 `**`）：
    
    🗣️ 網路社群輿情觀點：
    - PTT 最新動向：(簡述PTT那則文章的態度與日期)
    - Dcard 最新動向：(簡述Dcard那則文章的態度與日期)
    - 散戶心理綜合研判：(一句話總結市場散戶是樂觀還是恐慌)
    
    🤖 AI 綜合判讀報告：
    - 研判結論：(強勢買點 / 觀望 / 誘多陷阱 / 資訊不足)
    - 綜合判斷原因：(100字內精簡總結)
    - 潛在風險提示：(一句話警示)
    """

    for idx, strat in enumerate(strategies):
        current_key = strat["key"]
        model_name = strat["model"]
        description = strat["desc"]
        
        try:
            print(f"   > 🛡️ 正在嘗試防線 {idx+1}: {description}...")
            client = genai.Client(api_key=current_key.strip())
            response = client.models.generate_content(model=model_name, contents=prompt)
            
            if idx > 0: 
                print(f"🔄 [交叉陣列救援成功] 成功透過【{description}】突襲通關，取得 AI 報告！")
            return response.text
            
        except Exception as e:
            error_str = str(e)
            print(f"❌ 防線 {idx+1} ({model_name}) 宣告失守。")
            
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                if idx < len(strategies) - 1:
                    wait_time = 8.0  
                    match = re.search(r"Please retry in ([\d\.]+)s", error_str)
                    if match:
                        wait_time = float(match.group(1)) + 1.5
                    
                    print(f"⏳ [矩陣避難] 主要管道撞牆。依據官方指示原地安全休眠 {wait_time:.2f} 秒...")
                    time.sleep(wait_time)
                    print(f"⚡ 緩衝期結束，立刻切換至下一道防線！")
                    continue 
                else:
                    print("⚠️ 警告：已經耗盡所有交叉組合，後面已無防線。")
            else:
                if idx < len(strategies) - 1:
                    print("⚠️ 遭遇非常規錯誤，立即無縫更換至下一套組合方案...")
                    continue
                
    return "❌ 經過雙金鑰與雙模型的 4 輪矩陣交叉突襲，所有免費通道（含備用池）均已達上限，本次內文略過 AI 報告。"

# --- 3.5 LINE 傳播模組 ---
def send_line_notify(message):
    """將文字訊息推播至 LINE Messaging API"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("⚠️ 未設定 LINE 金鑰，略過推播。")
        return
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(PushMessageRequest(to=LINE_USER_ID, messages=[TextMessage(text=message)]))
        print("📲 LINE 通知發送成功！")
    except Exception as e:
        print(f"⚠️ LINE 通知發送失敗: {e}")

# --- 4. 核心掃描策略 ---
def run_sniper_bot():
    print("[主力狙擊機器人 - 雙軌即時旗艦版] 啟動！開始掃描...\n")
    legal_data, pe_data = fetch_twse_daily_data()
    
    try:
        fugle_client = RestClient(api_key=FUGLE_API_KEY)
        fugle_stock = fugle_client.stock
    except Exception as e:
        print(f"富果 API 初始化失敗：{e}")
        return

    for ticker in target_tickers:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            company_name = info.get("shortName", ticker)
            
            hist = stock.history(period="6mo")
            if hist.empty or len(hist) < 30:
                print(f"- {company_name} ({ticker}) 歷史資料不足，跳過。")
                continue
                
            # 確保富果代號乾淨
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            stock_pe_info = pe_data.get(fugle_symbol, {})
            stock_legal_info = legal_data.get(fugle_symbol, {})

            official_pe = stock_pe_info.get('PEratio', '') or '無'
            dividend_yield = stock_pe_info.get('DividendYield', '') or '無'
            
            # === 🚀 終極籌碼防線：證交所 OpenAPI + 富果大數據雙軌制 ===
            chip_source_msg = "今日最新盤後"
            
            # 1. 優先從今日證交所官方查表 (適用下午 14:30 之後)
            foreign_buy_vols = parse_vol(stock_legal_info.get('ForeignInvestmentBuyBuyOver', '0'))
            sitc_buy_vols = parse_vol(stock_legal_info.get('InvestmentTrustBuyBuyOver', '0'))
            
            # 💡 2. 盤中 Fallback：如果都是 0 張（通常是下午兩點半前，官方今日數據尚未誕生）
            if foreign_buy_vols == 0 and sitc_buy_vols == 0:
                # 直接調用富果大數據，精準取得昨日盤後定格籌碼張數！
                foreign_buy_vols, sitc_buy_vols = fetch_fugle_yesterday_chips(fugle_symbol)
                chip_source_msg = "昨日盤後定格"
            # ========================================================

            yield_msg = f"{dividend_yield}%" if dividend_yield != '無' else '無'
            print(f"📊 官方財報 -> 本益比: {official_pe} / 殖利率: {yield_msg}")
            print(f"🔥 法人籌碼 -> [{chip_source_msg}] 外資: {foreign_buy_vols} 張 / 投信: {sitc_buy_vols} 張")
            
            try:
                quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                hist.iloc[-1, hist.columns.get_loc('Volume')] = quote['total']['tradeVolume']
            except Exception as fugle_e:
                print(f"⚠️ 無法取得 {ticker} 即時報價，使用延遲資料: {fugle_e}")

            # 技術指標計算
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
            
            today, yesterday = hist.iloc[-1], hist.iloc[-2]
            
            # 訊號邏輯
            golden_cross = (yesterday['5MA'] <= yesterday['20MA']) and (today['5MA'] > today['20MA'])
            macd_reversal = (yesterday['MACD'] <= yesterday['Signal']) and (today['MACD'] > today['Signal'])
            volume_surge = today['Volume'] > (today['5Vol_MA'] * 2)
            bullish_alignment = (today['Close'] > today['5MA']) and (today['5MA'] > today['20MA'])
            strong_surge = today['Close'] >= (yesterday['Close'] * 1.04)
            momentum_breakout = bullish_alignment and strong_surge
            
            ma_msg = "黃金交叉！" if golden_cross else ("多頭強勢排列！" if bullish_alignment else "無明顯交會")
            macd_msg = "底部反轉！" if macd_reversal else ("MACD紅柱維持" if today['MACD'] > today['Signal'] else "柱狀圖正常")
            vol_msg = f"爆發量！({today['Volume']/today['5Vol_MA']:.1f}倍)" if volume_surge else "量能平穩"
            
            tech_info = {
                "rsi": today['RSI'], "ma_signal": ma_msg, "macd_signal": macd_msg, "vol_signal": vol_msg,
                "pe": official_pe if official_pe != '無' else info.get("trailingPE", "無資料"),
                "foreign_buy": foreign_buy_vols, "sitc_buy": sitc_buy_vols, "dividend_yield": dividend_yield
            }
            
            if golden_cross or macd_reversal or volume_surge or momentum_breakout: 
                print(f"\n[發現獵物] {company_name} ({ticker}) 觸發警報！")
                ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
                
                print("   > 正在精準抓取 Yahoo/Google 各 3 則新聞...")
                yahoo_news, google_news = fetch_stock_news_v2(ticker, company_name)
                print("   > 正在搜尋 PTT 股版最新 1 則討論與日期...")
                ptt_post = fetch_latest_ptt_post(ticker_digits)
                print("   > 正在搜尋 Dcard 股市版最新 1 則討論與日期...")
                dcard_post = fetch_latest_dcard_post(ticker_digits)
                
                print("   > 正在派出輕量化 AI 進行決策分析...")
                ai_complete_report = analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post)
                
                # 📲 動態標籤排版：將真實籌碼來源（今日盤後 / 昨日定格）同步灌進 LINE 訊息中
                line_msg = f"\n🎯 發現獵物：{company_name} ({ticker})\n股價：{today['Close']} / 爆發量：{today['Volume']/today['5Vol_MA']:.1f}倍\n技術面：{ma_msg} | {macd_msg}\n籌碼面 ({chip_source_msg})：外資 {foreign_buy_vols} 張 | 投信 {sitc_buy_vols} 張\n----------------------\n{ai_complete_report}\n----------------------"
                send_line_notify(line_msg)
                
                print("   > [防禦機制] 進入 15 秒冷卻緩衝區...")
                time.sleep(15)
            else:
                print(f"- {company_name} ({ticker}) 目前即時指標平淡 ({today['Close']})，繼續潛伏。")
                
            time.sleep(0.5)
        except Exception as e:
            print(f"X 處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    run_sniper_bot()

