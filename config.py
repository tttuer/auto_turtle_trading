import os
import json
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
    @classmethod
    def get_universe(cls):
        try:
            with open("universe.json", "r") as f:
                data = json.load(f)
                return data.get("target_symbols", [])
        except FileNotFoundError:
            return []
