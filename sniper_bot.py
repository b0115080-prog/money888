import requests
import yfinance as yf
import pandas as pd
import feedparser
import os
from dotenv import load_dotenv
from google import genai
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage

# 載入 API 金鑰
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
FUGLE_API_KEY = os.getenv("FUGLE_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.getenv("LINE_USER_ID")
# --- 1. 設定你的中小型觀察清單 ---
target_tickers = [
    "0050.TW", 
    "0056.TW", 
    "0052.TW", 
    "3138.TW", # 耀登
    "00405A.TW",
    "2344.TW", # 華邦電
    "2537.TW", #聯上發
    "3008.TW", #大立光
    "2327.TW" # 國巨

]

# --- 2. 新聞爬蟲模組 ---
def fetch_stock_news(ticker, company_name, limit=3):
    """綜合 Yahoo 與 Google News 的新聞"""
    news_list = []
    
    try:
        stock = yf.Ticker(ticker)
        for item in stock.news[:limit]:
            if 'content' in item and 'title' in item['content']:
                news_list.append(item['content']['title'])
            else:
                news_list.append(item.get('title', ''))
    except Exception:
        pass

    try:
        url = f"https://news.google.com/rss/search?q={company_name}+股市&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit]:
            news_list.append(entry.title)
    except Exception:
        pass
        
    return [n for n in news_list if n]

# --- 3. AI 判讀模組 ---
def analyze_breakout_real_or_fake(ticker, company_name, headlines, tech_info):
    """讓 Gemini 結合技術指標與新聞判斷真偽"""
    if not GEMINI_API_KEY:
        return "!! 未設定 Gemini API Key，跳過分析。"
        
    client = genai.Client(api_key=GEMINI_API_KEY)
    headlines_text = "\n".join(f"- {h}" for h in headlines)
    
    # 將技術指標文字化，餵給 AI
    prompt = f"""
    你是專業的台股籌碼與基本面分析師。
    標的：{company_name} ({ticker})
    
    【目前技術面狀態】
    - RSI(14日)：{tech_info['rsi']:.1f} (大於70為過熱，小於30為超賣)
    - 均線訊號：{tech_info['ma_signal']}
    - MACD訊號：{tech_info['macd_signal']}
    - 量能狀態：{tech_info['vol_signal']}
    - 本益比：{tech_info['pe']}
    
    請閱讀以下最新新聞與市場討論，並綜合上述技術面數據，幫我研判：
    這是『實質訂單/營收成長的真突破』，還是『主力誘多/技術面騙線』？
    
    請給出：
    1. 研判結論 (強勢買點 / 觀望 / 誘多陷阱 / 資訊不足)
    2. 綜合判斷原因 (100字以內，需結合新聞基本面與技術面)
    3. 潛在風險提示
    
    近期新聞：
    {headlines_text}
    """
    
    try:
        response = client.models.generate_content(
            model='gemini-2.0-flash', 
            contents=prompt
        )
        return response.text
    except Exception as e:
        return f"X AI 分析失敗：{e}"

# --- 3.5 LINE Messaging API 傳播模組 ---
def send_line_notify(message):
    """將文字訊息推播至 LINE Messaging API"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("⚠️ 未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，略過推播。")
        return
        
    try:
        configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(
                PushMessageRequest(
                    to=LINE_USER_ID,
                    messages=[TextMessage(text=message)]
                )
            )
        print("📲 LINE 通知發送成功！")
    except Exception as e:
        print(f"⚠️ LINE 通知發送失敗: {e}")


from fugle_marketdata import RestClient
import time

# --- 4. 核心掃描策略 (雙軌資料源升級版) ---
def run_sniper_bot():
    print("[主力狙擊機器人 - 雙軌即時旗艦版] 啟動！開始掃描...\n")
    
    # 初始化富果客戶端 (負責抓即時)
    try:
        fugle_client = RestClient(api_key=FUGLE_API_KEY)
        fugle_stock = fugle_client.stock
    except Exception as e:
        print(f"富果 API 初始化失敗，請檢查金鑰：{e}")
        return

    for ticker in target_tickers:
        try:
            # 1. 歷史資料：交給 yfinance
            stock = yf.Ticker(ticker)
            info = stock.info
            company_name = info.get("shortName", ticker)
            
            hist = stock.history(period="6mo")
            if hist.empty or len(hist) < 30:
                print(f"- {company_name} ({ticker}) 歷史資料不足，跳過。")
                continue
                
            # 2. 即時資料：交給 Fugle (精準到當下秒數)
            # 富果的代號不需要 .TW，所以要做字串替換
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '')
            
            try:
                quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                realtime_price = quote['lastPrice']
                realtime_volume = quote['total']['tradeVolume']
                
                # 【雙軌合璧】將富果的「即時價格與總量」覆寫進 yfinance 的最後一筆(今天)
                # 這樣等一下算出來的均線和 MACD 才會是包含即時跳動的最新數值！
                hist.iloc[-1, hist.columns.get_loc('Close')] = realtime_price
                hist.iloc[-1, hist.columns.get_loc('Volume')] = realtime_volume
            except Exception as fugle_e:
                print(f"⚠️ 無法取得 {ticker} 即時報價，退回使用 yfinance 延遲資料: {fugle_e}")

            
            # === 計算技術指標 (此時 hist 的最後一筆已經是富果的即時數字了！) ===
            # 1. 均線與均量
            hist['5MA'] = hist['Close'].rolling(window=5).mean()
            hist['20MA'] = hist['Close'].rolling(window=20).mean()
            hist['5Vol_MA'] = hist['Volume'].rolling(window=5).mean()
            
            # 2. RSI (14日)
            delta = hist['Close'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            ema_up = up.ewm(com=13, adjust=False).mean()
            ema_down = down.ewm(com=13, adjust=False).mean()
            rs = ema_up / ema_down
            hist['RSI'] = 100 - (100 / (1 + rs))
            
            # 3. MACD (12, 26, 9)
            exp1 = hist['Close'].ewm(span=12, adjust=False).mean()
            exp2 = hist['Close'].ewm(span=26, adjust=False).mean()
            hist['MACD'] = exp1 - exp2
            hist['Signal'] = hist['MACD'].ewm(span=9, adjust=False).mean()
            hist['MACD_Hist'] = hist['MACD'] - hist['Signal']
            
            # 取得近兩日資料進行邏輯判斷
            today = hist.iloc[-1]
            yesterday = hist.iloc[-2]
            
            # === 判斷訊號 ===
            # 訊號 A：黃金交叉 (精準抓取轉折第一天)
            golden_cross = (yesterday['5MA'] <= yesterday['20MA']) and (today['5MA'] > today['20MA'])
            
            # 訊號 B：MACD 多頭反轉 (精準抓取柱狀圖翻紅第一天)
            macd_reversal = (yesterday['MACD'] <= yesterday['Signal']) and (today['MACD'] > today['Signal'])
            
            # 訊號 C：爆量突破 
            volume_surge = today['Volume'] > (today['5Vol_MA'] * 2)

            # 🚀【新增】訊號 D：強勢動能 (專抓川湖這種多頭飆股)
            # 條件 1：均線多頭排列 (目前股價站上5MA，且5MA大於20MA)
            bullish_alignment = (today['Close'] > today['5MA']) and (today['5MA'] > today['20MA'])
            # 條件 2：今天股價強勢大漲 (比昨天收盤價上漲超過 4%)
            strong_surge = today['Close'] >= (yesterday['Close'] * 1.04)
            momentum_breakout = bullish_alignment and strong_surge
            
            # === 整理要傳給 AI 的技術狀態 ===
            ma_msg = "黃金交叉！" if golden_cross else ("多頭強勢排列！" if bullish_alignment else "無明顯交會")
            macd_msg = "底部反轉！" if macd_reversal else ("MACD紅柱維持" if today['MACD'] > today['Signal'] else "柱狀圖正常")
            vol_msg = f"爆發量！({today['Volume']/today['5Vol_MA']:.1f}倍)" if volume_surge else "量能平穩"
            pe_ratio = info.get("trailingPE", "無資料")
            
            tech_info = {
                "rsi": today['RSI'],
                "ma_signal": ma_msg,
                "macd_signal": macd_msg,
                "vol_signal": vol_msg,
                "pe": pe_ratio
            }
            
            # === 觸發條件 (加入 momentum_breakout) ===
            # 只要符合任一條件，立刻啟動 AI 判讀
            if golden_cross or macd_reversal or volume_surge or momentum_breakout: 
                print(f"\n[發現獵物] {company_name} ({ticker}) 觸發即時技術指標警報！")
                print(f"   - 即時股價: {today['Close']}")
                print(f"   - RSI: {today['RSI']:.1f}")
                print(f"   - 均線: {ma_msg}")
                print(f"   - MACD: {macd_msg}")
                print(f"   - 量能: {vol_msg}")
                
                print(f"   > 正在派出 AI 爬蟲蒐集情報並進行交叉驗證...")
                news = fetch_stock_news(ticker, company_name, limit=5)

                # 💡 第一步：先把「一定會發送」的基礎技術面訊息準備好
                line_msg = f"\n🎯 發現獵物：{company_name} ({ticker})\n"
                line_msg += f"股價：{today['Close']} / 爆發量：{today['Volume']/today['5Vol_MA']:.1f}倍\n"
                line_msg += f"技術面：{ma_msg} | {macd_msg}\n"
                line_msg += "----------------------\n"
                
                # 💡 第二步：判斷有沒有新聞，決定 AI 要說什麼
                if news:
                    ai_report = analyze_breakout_real_or_fake(ticker, company_name, news, tech_info)
                    print("\n========== AI 狙擊手分析報告 ==========")
                    print(ai_report)
                    print("==========================================\n")
                    # 將 AI 報告附加到訊息底部
                    line_msg += f"🤖 AI 綜合判讀報告：\n{ai_report}"
                else:
                    msg = "找不到相關近期新聞，屬於純技術面強勢突破。"
                    print(f"   > {msg}")
                    # 沒新聞也要加上警示語
                    line_msg += "🤖 AI 報告：目前查無近期新聞。此為「純技術面強勢突破」或「潛在利多尚未見報」，請留意追高與籌碼風險！"
                
                # 💡 第三步：不管有沒有新聞，最後「絕對」要觸發 LINE 警報！
                send_line_notify(line_msg)
            else:
                print(f"- {company_name} ({ticker}) 目前即時指標平淡 ({today['Close']})，繼續潛伏。")
                
            # 保護機制：避免撞到富果 API 頻率限制 (每分鐘 600 次)
            time.sleep(0.5)
                
        except Exception as e:
            print(f"X 處理 {ticker} 時發生錯誤: {e}")

if __name__ == "__main__":
    run_sniper_bot()
