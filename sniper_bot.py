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
    """【大局觀進化版】嚴格規範籌碼定義與新聞判讀邏輯，杜絕 AI 幻覺"""
    key1 = os.getenv("GEMINI_API_KEY")
    key2 = os.getenv("GEMINI_API_KEY_BACKUP")
    
    strategies = []
    if key1:
        strategies.append({"key": key1, "model": "gemini-3.1-flash-lite", "desc": "主要金鑰 + 3.1-Flash-Lite"})
    if key2:
        strategies.append({"key": key2, "model": "gemini-3.1-flash-lite", "desc": "備用金鑰 +
