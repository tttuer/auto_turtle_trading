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
            print("[Config] Fetching KOSPI 200 Sector Top 40 stocks using Hybrid method (FDR + Sector Map)...")
            
            # KOSPI 200 주요 10대 섹터별 후보 종목 매핑 (약 70여 개 우량주)
            SECTOR_CANDIDATES = {
                "IT_반도체": ["005930", "000660", "011070", "226320", "009150", "066570", "018260"], # 삼성전자, SK하이닉스, LG이노텍, 한미반도체, 삼성전기, LG전자, 삼성SDS
                "자동차_모빌리티": ["005380", "000270", "012330", "011210", "018880", "005385", "005387"], # 현대차, 기아, 현대모비스, 현대위아, 한온시스템, 현대차우 등
                "2차전지_화학": ["373220", "006400", "051910", "003670", "096770", "010950", "009830"], # LG엔솔, 삼성SDI, LG화학, 포스코퓨처엠, SK이노베이션, S-Oil, 한화솔루션
                "금융_은행": ["105560", "055550", "086790", "316140", "323410", "024110", "377300", "032830"], # KB, 신한, 하나, 우리, 카카오뱅크, 기은, 카카오페이, 삼성생명
                "바이오_제약": ["207940", "068270", "000100", "128940", "008930", "302440", "326030"], # 삼바, 셀트리온, 유한양행, 한미약품, 한미사이언스, SK바사, SK바이오팜
                "플랫폼_통신": ["035420", "035720", "036570", "259960", "030200", "017670", "032640"], # NAVER, 카카오, 엔씨, 크래프톤, KT, SKT, LGU+
                "조선_해운_기계": ["329180", "042660", "010140", "034020", "241560", "011200", "028670"], # HD한국조선해양, 한화오션, 삼성중공업, 두산에너빌리티, 두산로보틱스, HMM, 팬오션
                "철강_소재": ["005490", "004020", "010130", "001430", "000670", "001040"], # POSCO홀딩스, 현대제철, 고려아연, 풍산, 영풍, CJ
                "소비재_유통": ["051900", "090430", "023530", "007070", "028150", "139480", "027410"], # LG생건, 아모레, 롯데쇼핑, GS리테일, BGF리테일, 이마트
                "지주_기타": ["028260", "034730", "003550", "000880", "000120", "004370"] # 삼성물산, SK, LG, 한화, CJ대한통운, 농심
            }
            
            # KOSPI 전체 목록 (시가총액 순 정렬 상태) 가져오기
            kospi_df = fdr.StockListing('KOSPI')
            # 결측치나 이상치가 있을 수 있으니 안전하게 순서 리스트화
            kospi_sorted_codes = kospi_df['Code'].tolist()
            
            target_symbols = []
            
            for sector, candidates in SECTOR_CANDIDATES.items():
                # 후보 종목들 중 현재 KOSPI에 상장되어 있는 종목만 필터링 (상장폐지 등 예방)
                valid_candidates = [code for code in candidates if code in kospi_sorted_codes]
                
                # KOSPI 시가총액 순서(kospi_sorted_codes)를 기준으로 정렬 (인덱스가 작을수록 시가총액 큼)
                valid_candidates.sort(key=lambda x: kospi_sorted_codes.index(x))
                
                # 각 섹터에서 시가총액 상위 4개 종목 추출
                top4 = valid_candidates[:4]
                target_symbols.extend(top4)
                
                print(f"[Config] Sector {sector} Top 4: {top4}")
            
            # 중복 제거 (혹시나 겹치는 종목이 있을 경우 대비) 및 40개 맞춤 보장
            # 리스트 순서 유지를 위해 dict.fromkeys 사용
            target_symbols = list(dict.fromkeys(target_symbols))[:40]
            
            cls._universe_cache = target_symbols
            print(f"[Config] Selected 40 Universe Stocks: {cls._universe_cache}")
            return cls._universe_cache
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            return []
