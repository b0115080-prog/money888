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
    
    請綜合上述精簡數據，精準研判
