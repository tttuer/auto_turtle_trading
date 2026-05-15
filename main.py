import asyncio
import csv
import os
# pyrefly: ignore [missing-import]
import websockets
import json
import time
import requests
import schedule
from threading import Thread
from config import Config
from kis_api import KISClient
from strategy import TurtleStrategy

class TradingBot:
    def __init__(self):
        self.kis = KISClient()
        self.strategy = TurtleStrategy(self.kis)
        self.is_running = True
        self.ws_approval_key = None
        self._pending_symbols = set()  # 주문 처리 중인 종목 (중복 주문 방지)

    def _get_ws_approval_key(self):
        """웹소켓 접속용 Approval Key 발급"""
        url = f"{Config.REST_BASE_URL}/oauth2/Approval"
        headers = {"content-type": "application/json; utf-8"}
        body = {
            "grant_type": "client_credentials",
            "appkey": Config.APP_KEY,
            "secretkey": Config.APP_SECRET
        }
        res = requests.post(url, headers=headers, data=json.dumps(body))
        if res.status_code == 200:
            key = res.json().get("approval_key")
            print("[WebSocket] Approval key issued.")
            return key
        else:
            print(f"[WebSocket] Failed to issue approval key: {res.text}")
            return None

    def daily_init(self):
        print("\n[Bot] Running Daily Initialization...")
        self.kis.issue_token()
        self.ws_approval_key = self._get_ws_approval_key()
        # 장 시작 전 동적 자산 바탕으로 기준가 및 Unit 계산
        self.strategy.prepare_daily_data()

    async def ws_loop(self):
        if not self.ws_approval_key:
            print("[WebSocket] No approval key. Exiting WS loop.")
            return

        uri = f"{Config.WS_BASE_URL}/tryitout/H0STCNT0" 
        
        while self.is_running:
            try:
                # ping_interval=None to manually handle ping/pong if needed, 
                # but websockets lib handles standard pings automatically.
                async with websockets.connect(uri, ping_interval=60) as ws:
                    print("\n[WebSocket] Connected to KIS Real-time Server.")
                    
                    # 대상 종목별 구독(Subscribe) 요청 전송
                    for sym in Config.get_universe():
                        sub_req = {
                            "header": {
                                "approval_key": self.ws_approval_key,
                                "custtype": "P",
                                "tr_type": "1", # 1: 등록, 2: 해제
                                "content-type": "utf-8"
                            },
                            "body": {
                                "input": {
                                    "tr_id": "H0STCNT0", # 국내주식 실시간 체결가
                                    "tr_key": sym
                                }
                            }
                        }
                        await ws.send(json.dumps(sub_req))
                        print(f"[WebSocket] Subscribed to {sym}")
                    
                    while self.is_running:
                        msg = await ws.recv()
                        
                        # 응답 메시지 파싱 (KIS 웹소켓 규격: 0|tr_id|개수|데이터)
                        if isinstance(msg, str) and (msg.startswith('0|') or msg.startswith('1|')):
                            parts = msg.split('|')
                            if len(parts) >= 4:
                                data_str = parts[3]
                                data_fields = data_str.split('^')
                                
                                # data_fields[0]: 종목코드, [1]: 체결시간, [2]: 현재가
                                if len(data_fields) > 2:
                                    symbol = data_fields[0]
                                    try:
                                        current_price = float(data_fields[2])
                                    except ValueError:
                                        continue
                                    
                                    if symbol in self._pending_symbols:
                                        continue  # 이미 주문 처리 중인 종목은 스킵

                                    # 전략 모듈에 현재가 전달하여 매매 신호 확인
                                    signal = self.strategy.check_signals(symbol, current_price)
                                    if signal:
                                        action = signal['action']
                                        qty = signal['qty']
                                        reason = signal['reason']

                                        if qty > 0:
                                            print(f"\n[{time.strftime('%H:%M:%S')}] 🚨 [SIGNAL] {symbol} | {action} | Qty: {qty} | Price: {current_price} | {reason}")

                                            self._pending_symbols.add(symbol)
                                            try:
                                                # 시장가 주문 전송 (01: 시장가)
                                                order_res = self.kis.place_order(symbol, action, qty, order_type="01")
                                                if order_res and order_res.get('rt_cd') == '0':
                                                    # 주문 성공 시 내부 상태(수량, 진입가 등) 업데이트
                                                    self.strategy.update_position(symbol, action, qty, current_price)
                                                    self.log_trade(symbol, action, qty, current_price, reason)
                                                    self.send_telegram_alert(symbol, action, qty, current_price, reason)
                                            finally:
                                                self._pending_symbols.discard(symbol)

            except websockets.ConnectionClosed as e:
                print(f"\n[WebSocket] Connection Closed: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"\n[WebSocket] Unexpected Error: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    def send_telegram_alert(self, symbol, action, qty, price, reason):
        """매매 체결 시 텔레그램으로 알림을 전송합니다."""
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            return
        s = self.strategy.state.get(symbol, {})
        units_held = s.get('units_held', '-')
        stop_loss = s.get('stop_loss', 0)
        emoji = "🟢" if action == "BUY" else "🔴"
        action_str = "매수" if action == "BUY" else "매도"
        lines = [
            f"{emoji} {action_str} | {symbol}",
            f"수량: {qty}주 @ {price:,.0f}원",
            f"사유: {reason}",
        ]
        if action == "BUY":
            lines.append(f"유닛: {units_held}/{Config.MAX_UNITS} | 손절가: {stop_loss:,.0f}원")
        text = "\n".join(lines)
        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": Config.TELEGRAM_CHAT_ID, "text": text}, timeout=5)
        except Exception:
            pass

    def log_trade(self, symbol, action, qty, price, reason):
        """매매 체결 기록을 trade_history.csv에 저장합니다."""
        filepath = "trade_history.csv"
        file_exists = os.path.isfile(filepath)
        s = self.strategy.state.get(symbol, {})
        row = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol,
            'action': action,
            'qty': qty,
            'price': price,
            'reason': reason,
            'units_held': s.get('units_held', ''),
            'stop_loss': round(s.get('stop_loss', 0)),
        }
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)

    def log_balance(self):
        """주기적으로 계좌 잔액을 확인하고 CSV 파일로 기록합니다."""
        try:
            balance_info = self.kis.get_balance()
            if balance_info:
                total_equity = balance_info['total_equity']
                now_str = time.strftime('%Y-%m-%d %H:%M:%S')
                print(f"\n[{now_str}] 📈 현재 총 평가 자산: {total_equity:,.0f} 원")
                
                # CSV 파일로 기록 (추이 확인용)
                with open("balance_history.csv", "a", encoding="utf-8") as f:
                    f.write(f"{now_str},{total_equity}\n")
        except Exception as e:
            pass

    def schedule_loop(self):
        """매일 특정 시간에 초기화 작업을 실행하는 스케줄러"""
        # 한국 시간 08:30에 매일 실행
        schedule.every().day.at("08:30").do(self.daily_init)
        
        # 1시간마다 잔고 기록
        schedule.every(1).hours.do(self.log_balance)
        
        while self.is_running:
            schedule.run_pending()
            time.sleep(1)

    def start(self):
        print("=== Auto Turtle Trading Bot Started ===")
        # 봇 시작 시 1회 초기화 실행
        self.daily_init()
        
        # 스케줄러는 백그라운드 스레드에서 동작
        t = Thread(target=self.schedule_loop, daemon=True)
        t.start()
        
        # 메인 스레드는 웹소켓 이벤트 루프 블로킹 실행
        try:
            asyncio.run(self.ws_loop())
        except KeyboardInterrupt:
            print("\n[Bot] Shutting down...")
            self.is_running = False

if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
