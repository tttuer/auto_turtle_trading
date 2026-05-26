import os
import json
import FinanceDataReader as fdr
from dotenv import load_dotenv

load_dotenv()

class Config:
    # KIS API
    APP_KEY = os.environ.get("KIS_APP_KEY", "")
    APP_SECRET = os.environ.get("KIS_APP_SECRET", "")
    ACCOUNT_NO = os.environ.get("KIS_ACCOUNT_NO", "")
    ACCOUNT_PREFIX = ACCOUNT_NO.split("-")[0] if "-" in ACCOUNT_NO else ACCOUNT_NO
    ACCOUNT_SUFFIX = ACCOUNT_NO.split("-")[1] if "-" in ACCOUNT_NO else "01"
    
    # Environment
    MODE = os.environ.get("MODE", "VIRTUAL").upper()
    
    if MODE == "VIRTUAL":
        REST_BASE_URL = "https://openapivts.koreainvestment.com:29443"
        WS_BASE_URL = "ws://ops.koreainvestment.com:31000"
    else:
        REST_BASE_URL = "https://openapi.koreainvestment.com:9443"
        WS_BASE_URL = "ws://ops.koreainvestment.com:21000"

    # Strategy Params (System 1)
    ENTRY_DAYS = 20
    EXIT_DAYS = 10
    RISK_PERCENT = 0.01  # 1%
    MAX_UNITS = 4
    
    # Telegram
    TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

    # Universe
    _universe_cache = None

    @classmethod
    def get_universe(cls):
        if cls._universe_cache is not None:
            return cls._universe_cache
            
        try:
            print("[Config] Fetching KOSPI top 10 stocks...")
            df = fdr.StockListing('KOSPI')
            
            # 우선주 필터링 (종목명에 '우' 또는 '우B' 등이 포함된 것 제외)
            # 종목코드가 '0'으로 끝나지 않는 경우도 주로 우선주/스팩/펀드 등
            target_symbols = []
            for _, row in df.iterrows():
                code = row['Code']
                name = row['Name']
                
                # 이름 끝이 '우', '우B', '스팩' 등인 경우 제외
                if name.endswith('우') or name.endswith('우B') or '스팩' in name:
                    continue
                    
                target_symbols.append(code)
                
                if len(target_symbols) >= 10:
                    break
                    
            cls._universe_cache = target_symbols
            print(f"[Config] Selected Top 10 Universe: {cls._universe_cache}")
            return cls._universe_cache
            
        except Exception as e:
            print(f"[Config] Error fetching universe: {e}")
            return []
