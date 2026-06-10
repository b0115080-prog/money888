import os
import time
import requests
import yfinance as yf
import pandas as pd
import feedparser
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
    target_tickers = df_tickers[0].dropna().astype(str).tolist()
    print(f"✅ 成功從雲端讀取 {len(target_tickers)} 檔追蹤標的！")
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
    """直接從證交所 OpenAPI 抓取今日全台股『法人買賣超』與『本益比』"""
    print("📥 正在從證交所下載官方盤後籌碼與財報數據...")
    try:
        legal_entity_res = requests.get("https://openapi.twse.com.tw/v1/fund/T86")
        legal_data = {item['Code']: item for item in legal_entity_res.json()}
    except Exception: legal_data = {}

    try:
        pe_res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL")
        pe_data = {item['Code']: item for item in pe_res.json()}
    except Exception: pe_data = {}

    return legal_data, pe_data

def fetch_latest_ptt_post(ticker_digits):
    """【黑科技繞過版】利用 Google RSS 間接搜尋 PTT 股版文章，100% 免疫 PTT 官方的 10054 封鎖"""
    try:
        # 💡 利用 Google 新聞搜尋引擎鎖定 PTT 股版，神不知鬼不覺
        url = f"https://news.google.com/rss/search?q=site:ptt.cc/bbs/Stock+{ticker_digits}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        
        # 沿用現有的 feedparser 解析，速度極快且代碼乾淨
        feed = feedparser.parse(url)
        
        if feed.entries:
            latest_entry = feed.entries[0]
            
            # Google RSS 抓到的標題通常結尾會夾帶 " - 看板 Stock - 批踢踢實業坊"
            # 我們用 .split() 把後面乾淨地切掉，只保留純文章標題
            raw_title = latest_entry.title
            clean_title = raw_title.split(" - 看板")[0].strip()
            
            # 處理發文日期：將 Google 的 GMT 時間轉換為我們要的 MM/DD 格式
            date_str = "未知"
            if hasattr(latest_entry, 'published_parsed') and latest_entry.published_parsed:
                tm = latest_entry.published_parsed
                date_str = f"{tm.tm_mon:02d}/{tm.tm_mday:02d}"
            
            return {
                "title": clean_title,
                "date": date_str
            }
            
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

import re

import re

# --- 3. AI 判讀模組 (精準換手、絕不拖泥帶水版) ---
def analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post):
    """【金鑰一擊換手版】主要金鑰若撞 429 絕不原地重試，精準冷卻 IP 後直接切換備用金鑰"""
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    valid_keys = [k for k in [key1, key2] if k]
    if not valid_keys: return "⚠️ 未設定任何 Gemini API Key，跳過 AI 判讀。"
        
    if key1 and key2:
        if key1.strip() == key2.strip():
            print("📢【除錯警報】主要與備用金鑰內容『一模一樣』，請檢查設定！")
        else:
            print(f"📢【除錯資訊】主要與備用金鑰驗證通過（主要長度: {len(key1)} / 備用長度: {len(key2)}）。")
        
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

    for index, current_key in enumerate(valid_keys):
        model_name = 'gemini-2.0-flash'
        try:
            print(f"   > 正在嘗試第 {index+1} 把金鑰 (模型: {model_name})...")
            client = genai.Client(api_key=current_key.strip())
            response = client.models.generate_content(model=model_name, contents=prompt)
            
            if index > 0: 
                print(f"🔄 [備用金鑰突擊成功] 順利繞過流量管制，成功取得 AI 判讀報告！")
            return response.text
            
        except Exception as e:
            error_str = str(e)
            print(f"❌ 第 {index+1} 把金鑰呼叫遭拒。")
            
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                if index < len(valid_keys) - 1:
                    # 💡 偷出 Google 要求倒數的精準秒數
                    wait_time = 10.0  
                    match = re.search(r"Please retry in ([\d\.]+)s", error_str)
                    if match:
                        wait_time = float(match.group(1)) + 1.5
                    
                    print(f"⏳ [快速換手機制] 偵測到主要金鑰額度耗盡。")
                    print(f"⏳ 為避免 IP 遭連帶懲罰，主程式將精準休眠 {wait_time:.2f} 秒，隨後『直接切換』至下一把金鑰...")
                    time.sleep(wait_time)
                    continue  # 毫不留戀，直接進入下一輪迴圈換鑰匙！
                else:
                    print("⚠️ 已經是最後一把可用金鑰，無後續通道可供切換。")
            else:
                # 遇到其他非 429 錯誤（如連線問題），不用等，直接換手
                if index < len(valid_keys) - 1:
                    print("⚠️ 遭遇非常規錯誤，立即無縫切換至下一把備用管道...")
                    continue
                
    return "❌ 由於今日盤中觸發極其頻繁，所有免費金鑰通道均已達上限，本次內文略過 AI 報告。"

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
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '')
            stock_pe_info = pe_data.get(fugle_symbol, {})
            stock_legal_info = legal_data.get(fugle_symbol, {})

            official_pe = stock_pe_info.get('PEratio', '') or '無'
            dividend_yield = stock_pe_info.get('DividendYield', '') or '無'
            foreign_buy_vols = parse_vol(stock_legal_info.get('ForeignInvestmentBuyBuyOver', '0'))
            sitc_buy_vols = parse_vol(stock_legal_info.get('InvestmentTrustBuyBuyOver', '0'))

            yield_msg = f"{dividend_yield}%" if dividend_yield != '無' else '無'
            print(f"📊 官方財報 -> 本益比: {official_pe} / 殖利率: {yield_msg}")
            print(f"🔥 法人籌碼 -> 外資買賣超: {foreign_buy_vols} 張 / 投信買賣超: {sitc_buy_vols} 張")
            
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
                
                line_msg = f"\n🎯 發現獵物：{company_name} ({ticker})\n股價：{today['Close']} / 爆發量：{today['Volume']/today['5Vol_MA']:.1f}倍\n技術面：{ma_msg} | {macd_msg}\n籌碼面：外資 {foreign_buy_vols} 張 | 投信 {sitc_buy_vols} 張\n----------------------\n{ai_complete_report}\n----------------------"
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
