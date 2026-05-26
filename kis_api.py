import requests
import json
import time
from config import Config

class KISClient:
    def __init__(self):
        self.access_token = None

    def issue_token(self):
        url = f"{Config.REST_BASE_URL}/oauth2/tokenP"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": Config.APP_KEY,
            "appsecret": Config.APP_SECRET
        }
        res = requests.post(url, headers=headers, data=json.dumps(body))
        if res.status_code == 200:
            self.access_token = res.json()["access_token"]
            print("[KIS] Token issued successfully.")
        else:
            raise Exception(f"Token issue failed: {res.text}")

    def get_common_headers(self, tr_id):
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.access_token}",
            "appkey": Config.APP_KEY,
            "appsecret": Config.APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P"
        }

    def get_balance(self):
        """총 평가 금액(동적 Unit 계산용)과 보유 종목 조회"""
        url = f"{Config.REST_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance"
        tr_id = "VTTC8434R" if Config.MODE == "VIRTUAL" else "TTTC8434R"
        headers = self.get_common_headers(tr_id)
        params = {
            "CANO": Config.ACCOUNT_PREFIX,
            "ACNT_PRDT_CD": Config.ACCOUNT_SUFFIX,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": ""
        }
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            data = res.json()
            if data['rt_cd'] != '0':
                print(f"[KIS] Balance error: {data['msg1']}")
                return None
            try:
                # tot_evlu_amt: 총 평가 금액 (예수금 + 주식평가금액)
                total_equity = float(data['output2'][0]['tot_evlu_amt'])
                holdings = data['output1'] # list of holdings
                return {"total_equity": total_equity, "holdings": holdings}
            except (KeyError, IndexError) as e:
                print(f"[KIS] Parsing balance failed: {e}")
                return None
        else:
            print(f"[KIS] Error fetching balance: {res.text}")
            return None

    def get_ohlcv(self, symbol, timeframe="D"):
        """N값 및 고점/저점 계산을 위한 과거 봉 데이터 조회 (수정주가)"""
        url = f"{Config.REST_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
        headers = self.get_common_headers("FHKST01010400")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": timeframe,
            "FID_ORG_ADJ_PRC": "1" # 1: 수정주가
        }
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            return res.json().get('output', [])
        else:
            print(f"[KIS] Error fetching ohlcv for {symbol}: {res.text}")
            return []

    def get_stock_name(self, symbol):
        """종목 코드로 종목명을 API에서 조회합니다."""
        url = f"{Config.REST_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        headers = self.get_common_headers("FHKST01010100")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol
        }
        res = requests.get(url, headers=headers, params=params)
        if res.status_code == 200:
            data = res.json()
            # hts_kor_isnm: HTS 한글 종목명
            name = data.get('output', {}).get('hts_kor_isnm')
            return name if name else symbol
        else:
            print(f"[KIS] Error fetching name for {symbol}: {res.text}")
            return symbol

    def place_order(self, symbol, side, qty, price=0, order_type="01"):
        """매수/매도 주문 (시장가 기본)"""
        url = f"{Config.REST_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash"
        if side == "BUY":
            tr_id = "VTTC0802U" if Config.MODE == "VIRTUAL" else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if Config.MODE == "VIRTUAL" else "TTTC0801U"
        
        headers = self.get_common_headers(tr_id)
        body = {
            "CANO": Config.ACCOUNT_PREFIX,
            "ACNT_PRDT_CD": Config.ACCOUNT_SUFFIX,
            "PDNO": symbol,
            "ORD_DVSN": order_type, # "01" = 시장가, "00" = 지정가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price)
        }
        res = requests.post(url, headers=headers, data=json.dumps(body))
        if res.status_code == 200:
            print(f"[KIS] Order Placed - {side} {qty} shares of {symbol}. Response: {res.json()['msg1']}")
            return res.json()
        else:
            print(f"[KIS] Order failed: {res.text}")
            return None
