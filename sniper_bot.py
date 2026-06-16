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

# --- 0. 工具函式 ---
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
    df_tickers = df_tickers.dropna(subset=[0])
    df_tickers[0] = df_tickers[0].astype(str).str.replace(r'[^\x00-\x7F]+', '', regex=True).str.strip()
    raw_tickers = [t for t in df_tickers[0].tolist() if t.lower() != 'nan' and t]
    target_tickers = list(dict.fromkeys(raw_tickers))
    print(f"✅ 成功從雲端讀取並全面淨化 {len(target_tickers)} 檔追蹤標的！")
except Exception as e:
    print(f"⚠️ 讀取 Google 表單失敗，使用備用清單。錯誤: {e}")
    target_tickers = ["0050.TW", "0052.TW"]

# --- 2. 外部數據爬蟲模組 ---
def fetch_stock_news_v2(ticker, company_name):
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

def fetch_market_daily_data():
    """🚀 終極重構版：加入 User-Agent 偽裝與多重 Retry，全面抓取上市櫃籌碼"""
    print("📥 正在從證交所與櫃買中心下載官方盤後籌碼數據...")
    legal_data, pe_data = {}, {}
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }

    # 1. 上市籌碼 (TWSE) - 3次Retry
    for _ in range(3):
        try:
            res = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=10)
            if res.status_code == 200:
                for item in res.json():
                    code = item.get('Code', '').strip()
                    if code:
                        f_buy = item.get('ForeignInvestmentIncludeForeignDealersBuyBuyOver', item.get('ForeignInvestmentBuyBuyOver', '0'))
                        s_buy = item.get('InvestmentTrustBuyBuyOver', '0')
                        legal_data[code] = {'foreign': f_buy, 'sitc': s_buy}
                break
        except:
            time.sleep(1)

    # 2. 上櫃籌碼 (TPEx) - 3次Retry
    for _ in range(3):
        try:
            res = requests.get("https://openapi.tpex.org.tw/v1/tpex_38", headers=headers, timeout=10)
            if res.status_code == 200:
                for item in res.json():
                    code = item.get('SecuritiesCompanyCode', '').strip()
                    if code:
                        f_buy = item.get('ForeignInvestorsNetBuySell', '0')
                        s_buy = item.get('InvestmentTrustsNetBuySell', '0')
                        legal_data[code] = {'foreign': f_buy, 'sitc': s_buy}
                break
        except:
            time.sleep(1)

    # 3. 本益比 (TWSE)
    for _ in range(2):
        try:
            pe_res = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", headers=headers, timeout=8)
            if pe_res.status_code == 200:
                for item in pe_res.json():
                    code = item.get('Code', '').strip()
                    if code: pe_data[code] = item
                break
        except: pass

    return legal_data, pe_data

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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
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
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    strategies = []
    if key1: strategies.append({"key": key1, "model": "gemini-3.1-flash-lite", "desc": "主要金鑰 + 3.1-Flash-Lite"})
    if key2: strategies.append({"key": key2, "model": "gemini-3.1-flash-lite", "desc": "備用金鑰 + 3.1-Flash-Lite"})
    if key1: strategies.append({"key": key1, "model": "gemini-3-flash", "desc": "主要金鑰 + Gemini-3-Flash"})
    if key2: strategies.append({"key": key2, "model": "gemini-3-flash", "desc": "備用金鑰 + Gemini-3-Flash"})
        
    if not strategies: return "⚠️ 未設定 Gemini API Key。"
        
    yahoo_text = "\n".join(f"- {n}" for n in yahoo_news) if yahoo_news else "暫無資料"
    google_text = "\n".join(f"- {n}" for n in google_news) if google_news else "暫無資料"
    ptt_text = f"標題：{ptt_post['title']} ({ptt_post['date']})" if ptt_post else "今日無相關專文討論"
    dcard_text = f"標題：{dcard_post['title']} ({dcard_post['date']})" if dcard_post else "今日無相關專文討論"
    
    # 🚀 痛點3修復：植入主力反市場心理學
    prompt = f"""
    你現在是精通台股基本面、籌碼面與社群輿情（PTT/Dcard）的頂尖量化操盤手。
    標的：{company_name} ({ticker})
    
    【目前技術與籌碼狀態】
    - KD指標：{tech_info.get('kd_signal', '無')} | MACD：{tech_info['macd_signal']} | 均線：{tech_info['ma_signal']}
    - RSI(14日)：{tech_info['rsi']:.1f} | 量能狀態：{tech_info['vol_signal']}
    - 近日單日外資買賣超：{tech_info.get('foreign_buy')} 
    - 近日單日投信買賣超：{tech_info.get('sitc_buy')}
    
    【最新新聞】
    {yahoo_text}\n{google_text}
    
    【社群討論】
    PTT: {ptt_text} | Dcard: {dcard_text}

    🚨【主力反市場心理學 - 終極判讀法則 (嚴格遵守)】🚨
    1. 籌碼定調：上方提供的張數為真實法人的「單日買賣超動向」，絕不可誤判為總持股。若顯示為「未取得」，請略過籌碼分析。
    2. 完美起漲點判斷：當「法人大舉買超」且「技術面翻多（如KD金叉、負柱收斂或爆量）」時，若「社群討論度極低（無專文或日期久遠）」，【嚴禁】判定為冷門或缺乏題材！這在台股代表「散戶尚未察覺、籌碼極度乾淨」，請給予『強勢買點』評級，視為主力偷偷吃貨的完美潛伏期。
    3. 主力出貨風險：反之，當技術與籌碼雖好，但「社群討論度爆表、散戶極度狂熱」時，反而必須提示主力可能趁機逢高出貨的風險。
    4. 聯網題材：請依據上方提供的新聞標題判斷題材，若無新聞請誠實回答無，不准捏造舊新聞。

    請依照以下格式輸出繁體中文（不要包含任何 markdown 粗體符號 **）：
    🗣️ 網路社群輿情觀點：
    - PTT 最新動向：(簡述PTT態度與日期，若無請寫無)
    - Dcard 最新動向：(簡述Dcard態度與日期，若無請寫無)
    - 散戶心理綜合研判：(依據反市場心理學總結)
    
    🤖 AI 綜合判讀報告：
    - 研判結論：(強勢買點 / 觀望 / 誘多陷阱 / 資訊不足)
    - 綜合判斷原因：(100字內精簡總結)
    - 潛在風險提示：(一句話警示)
    """

    for idx, strat in enumerate(strategies):
        try:
            print(f"   > 🛡️ 正在嘗試防線 {idx+1}: {strat['desc']}...")
            client = genai.Client(api_key=strat["key"].strip())
            response = client.models.generate_content(model=strat["model"], contents=prompt)
            return response.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str and idx < len(strategies) - 1:
                match = re.search(r"Please retry in ([\d\.]+)s", error_str)
                time.sleep(float(match.group(1)) + 1.5 if match else 8.0)
                continue
    return "❌ 經過矩陣交叉突襲，所有通道均已達上限，略過 AI 報告。"

# --- 3.5 LINE 傳播模組 ---
def send_line_notify(message):
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
    print("[主力狙擊機器人 - 高敏感度雷達旗艦版] 啟動！開始掃描...\n")
    legal_data, pe_data = fetch_market_daily_data()
    
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
            
            hist = stock.history(period="6mo", actions=True)
            if hist.empty or len(hist) < 30: continue
                
            fugle_symbol = ticker.replace('.TW', '').replace('.TWO', '').strip()
            stock_pe_info = pe_data.get(fugle_symbol, {})
            
            # 🚀 痛點1徹底修復：應用精準混合籌碼 (含上市上櫃)，並直接「拔除」瞎猜邏輯
            stock_legal_info = legal_data.get(fugle_symbol)
            
            if stock_legal_info:
                foreign_buy_vols = parse_vol(stock_legal_info.get('foreign', '0'))
                sitc_buy_vols = parse_vol(stock_legal_info.get('sitc', '0'))
                chip_source_msg = "官方盤後精準數據"
            else:
                # 🔪 完全刪除 12% 瞎猜，直接標記為未取得
                foreign_buy_vols = "未取得"
                sitc_buy_vols = "未取得"
                chip_source_msg = "⚠️ API未發布或連線異常"

            official_pe = stock_pe_info.get('PEratio', '') or '無'
            dividend_yield = stock_pe_info.get('DividendYield', '') or '無'
            
            # 🚀 痛點2徹底修復：單位轉換 (Fugle張數 -> Yahoo股數)
            try:
                quote = fugle_stock.intraday.quote(symbol=fugle_symbol)
                hist.iloc[-1, hist.columns.get_loc('Close')] = quote['lastPrice']
                fugle_vol = quote['total']['tradeVolume']
                if fugle_vol > 0:
                    # ⚠️ Fugle 給的是「張」，但 Yahoo 需要「股」，必須乘以 1000 才能正確計算倍數！
                    hist.iloc[-1, hist.columns.get_loc('Volume')] = fugle_vol * 1000
            except: pass

            # --- 🚀 四維技術指標高敏計算 ---
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
            
            # 🚀 痛點2防呆極限修復：保證不出現 0.0倍 或除以 0
            current_vol = today['Volume']
            if current_vol <= 0: current_vol = yesterday['Volume'] 
            if current_vol <= 0: current_vol = 1 # 終極防呆
            
            ma_vol = today['5Vol_MA']
            if ma_vol <= 0: ma_vol = 1
            
            vol_ratio = current_vol / ma_vol
            volume_surge = vol_ratio > 1.5
            
            # --- 高敏訊號邏輯 ---
            golden_cross = (yesterday['5MA'] <= yesterday['20MA']) and (today['5MA'] > today['20MA'])
            bullish_alignment = (today['Close'] > today['5MA']) and (today['5MA'] > today['20MA'])
            strong_surge = today['Close'] >= (yesterday['Close'] * 1.04)
            momentum_breakout = bullish_alignment and strong_surge
            kd_golden_cross = (yesterday['K'] <= yesterday['D']) and (today['K'] > today['D'])
            macd_zero_cross = (yesterday['MACD_Hist'] < 0) and (today['MACD_Hist'] >= 0)
            macd_converge = (today['MACD_Hist'] < 0) and (today['MACD_Hist'] > yesterday['MACD_Hist'])
            
            ma_msg = "黃金交叉" if golden_cross else ("多頭排列" if bullish_alignment else "無明顯交會")
            kd_msg = "🔥 KD金叉" if kd_golden_cross else ("🟢 K>D" if today['K'] > today['D'] else "🔴 K<D")
            
            if macd_zero_cross: macd_msg = "🔥 底部反轉"
            elif macd_converge: macd_msg = "🟡 負柱收斂"
            else: macd_msg = "🟢 柱狀正常" if today['MACD_Hist'] > 0 else "🔴 偏空下探"
                
            vol_msg = f"爆發量！({vol_ratio:.1f}倍)" if volume_surge else f"量能平穩({vol_ratio:.1f}倍)"
            
            # 字串處理 (給AI和LINE推播)
            f_str = f"{foreign_buy_vols} 張" if isinstance(foreign_buy_vols, int) else "未取得"
            s_str = f"{sitc_buy_vols} 張" if isinstance(sitc_buy_vols, int) else "未取得"
            
            tech_info = {
                "rsi": today['RSI'], "ma_signal": ma_msg, "macd_signal": macd_msg, "kd_signal": kd_msg, "vol_signal": vol_msg,
                "pe": official_pe if official_pe != '無' else info.get("trailingPE", "無資料"),
                "foreign_buy": f_str, "sitc_buy": s_str, "dividend_yield": dividend_yield
            }
            
            is_triggered = golden_cross or kd_golden_cross or macd_zero_cross or macd_converge or volume_surge or momentum_breakout
            
            if is_triggered: 
                print(f"\n[發現獵物] {company_name} ({ticker}) 觸發警報！")
                ticker_digits = ticker.replace('.TW', '').replace('.TWO', '')
                
                yahoo_news, google_news = fetch_stock_news_v2(ticker, company_name)
                ptt_post = fetch_latest_ptt_post(ticker_digits)
                dcard_post = fetch_latest_dcard_post(ticker_digits)
                
                ai_complete_report = analyze_stock_with_gemini_ultra_lean(ticker, company_name, yahoo_news, google_news, tech_info, ptt_post, dcard_post)
                
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

