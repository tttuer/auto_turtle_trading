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
from mean_reversion_strategy import MeanReversionStrategy

class TradingBot:
    def __init__(self):
        self.kis = KISClient()
        self.strategy = TurtleStrategy(self.kis)
        self.mr_strategy = MeanReversionStrategy(self.kis)
        self.is_running = True
        self.ws_approval_key = None
        self._pending_symbols = set()  # 주문 처리 중인 종목 (중복 주문 방지)
        self.symbol_names = {}
        self.available_cash = 0
        self._last_no_cash_log = {}

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
        
        # 관심 종목명 캐싱 (FDR 데이터 활용, API 호출 불필요)
        for sym in Config.get_universe():
            self.symbol_names[sym] = Config._name_cache.get(sym, sym)
            
        # 장 시작 전 동적 자산 바탕으로 기준가 및 Unit 계산
        balance_info = self.kis.get_balance()
        if balance_info:
            self.available_cash = balance_info.get('available_cash', 0)
        
        self.strategy.prepare_daily_data()
        self.mr_strategy.prepare_daily_data()

    def _parse_ws_message(self, msg):
        """웹소켓 메시지를 파싱하여 (종목코드, 현재가)를 반환합니다."""
        if not isinstance(msg, str) or not (msg.startswith('0|') or msg.startswith('1|')):
            return None, None
            
        parts = msg.split('|')
        if len(parts) < 4:
            return None, None
            
        data_fields = parts[3].split('^')
        if len(data_fields) <= 2:
            return None, None
            
        symbol = data_fields[0]
        try:
            current_price = float(data_fields[2])
            return symbol, current_price
        except ValueError:
            return None, None

    def _adjust_buy_qty(self, symbol, symbol_name, qty, current_price):
        """매수 시 현금 부족 여부를 확인하고 수량을 조절합니다."""
        if self.available_cash <= 0:
            adjusted_qty = 0
        else:
            est_cost = qty * current_price
            if est_cost > self.available_cash:
                adjusted_qty = int(self.available_cash // current_price)
            else:
                adjusted_qty = qty

        if adjusted_qty < qty:
            if adjusted_qty <= 0:
                now = time.time()
                if now - self._last_no_cash_log.get(symbol, 0) > 300: # 5분 제한
                    print(f"\n[{time.strftime('%H:%M:%S')}] ⚠️ 현금 부족으로 {symbol_name} ({symbol}) 매수 신호 스킵 (보유현금: {self.available_cash:,.0f}원)")
                    self._last_no_cash_log[symbol] = now
                return 0
            print(f"\n[{time.strftime('%H:%M:%S')}] ⚠️ 현금 부족으로 {symbol_name} 수량 조절 ({qty}주 -> {adjusted_qty}주)")
            
        return adjusted_qty

    def _execute_signal(self, symbol, current_price, signal, strategy_type="TURTLE"):
        """매매 신호에 따라 주문을 실행하고 상태를 업데이트합니다."""
        action = signal['action']
        qty = signal['qty']
        reason = signal['reason']

        if qty <= 0:
            return

        symbol_name = self.symbol_names.get(symbol, symbol)

        # 매수 시 현금 부족 방어 및 부분 매수 로직
        if action == "BUY":
            qty = self._adjust_buy_qty(symbol, symbol_name, qty, current_price)
            if qty <= 0:
                return

        print(f"\n[{time.strftime('%H:%M:%S')}] 🚨 [{strategy_type}] {symbol_name} ({symbol}) | {action} | Qty: {qty} | Price: {current_price} | {reason}")

        self._pending_symbols.add(symbol)
        try:
            # 시장가 주문 전송 (01: 시장가)
            order_res = self.kis.place_order(symbol, action, qty, order_type="01")
            if order_res and order_res.get('rt_cd') == '0':
                # 주문 성공 시 내부 상태(수량, 진입가 등) 업데이트
                if strategy_type == "TURTLE":
                    self.strategy.update_position(symbol, action, qty, current_price)
                else:
                    self.mr_strategy.update_position(symbol, action, qty, current_price)

                # 로컬 현금(available_cash) 동기화
                if action == "BUY":
                    self.available_cash -= (qty * current_price)
                elif action == "SELL":
                    self.available_cash += (qty * current_price)

                self.log_trade(symbol, action, qty, current_price, reason, strategy_type)
                self.send_telegram_alert(symbol, action, qty, current_price, reason, strategy_type)
        finally:
            self._pending_symbols.discard(symbol)

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
                        
                        symbol, current_price = self._parse_ws_message(msg)
                        if not symbol:
                            continue
                            
                        if symbol in self._pending_symbols:
                            continue  # 이미 주문 처리 중인 종목은 스킵

                        # 터틀 전략 신호 확인
                        signal = self.strategy.check_signals(symbol, current_price)
                        if signal:
                            self._execute_signal(symbol, current_price, signal, "TURTLE")

                        # 평균 회귀 전략 신호 확인 (터틀 포지션 보유 시 MR 신규 진입 스킵)
                        if symbol not in self._pending_symbols:
                            mr_signal = self.mr_strategy.check_signals(symbol, current_price)
                            if mr_signal:
                                turtle_has_position = self.strategy.state.get(symbol, {}).get('units_held', 0) > 0
                                if mr_signal['action'] == 'BUY' and turtle_has_position:
                                    mr_signal = None  # 터틀 포지션 중복 노출 방지
                            if mr_signal:
                                self._execute_signal(symbol, current_price, mr_signal, "MR")

            except websockets.ConnectionClosed as e:
                print(f"\n[WebSocket] Connection Closed: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"\n[WebSocket] Unexpected Error: {e}. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)

    def send_telegram_alert(self, symbol, action, qty, price, reason, strategy_type="TURTLE"):
        """매매 체결 시 텔레그램으로 알림을 전송합니다."""
        if not Config.TELEGRAM_BOT_TOKEN or not Config.TELEGRAM_CHAT_ID:
            return
        emoji = "🟢" if action == "BUY" else "🔴"
        action_str = "매수" if action == "BUY" else "매도"
        strategy_label = "🐢터틀" if strategy_type == "TURTLE" else "📊평균회귀"
        symbol_name = self.symbol_names.get(symbol, symbol)
        lines = [
            f"{emoji} [{strategy_label}] {action_str} | {symbol_name} ({symbol})",
            f"수량: {qty}주 @ {price:,.0f}원",
            f"사유: {reason}",
        ]
        if action == "BUY":
            if strategy_type == "TURTLE":
                s = self.strategy.state.get(symbol, {})
                units_held = s.get('units_held', '-')
                stop_loss = s.get('stop_loss', 0)
                lines.append(f"유닛: {units_held}/{Config.MAX_UNITS} | 손절가: {stop_loss:,.0f}원")
            else:
                s = self.mr_strategy.state.get(symbol, {})
                stop_loss = price * (1 - Config.MR_STOP_LOSS_PCT)
                bb_middle = s.get('bb_middle', 0)
                lines.append(f"손절: {stop_loss:,.0f}원 | 목표: {bb_middle:,.0f}원(BB중심)")
        text = "\n".join(lines)
        try:
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": Config.TELEGRAM_CHAT_ID, "text": text}, timeout=5)
        except Exception:
            pass

    def log_trade(self, symbol, action, qty, price, reason, strategy_type="TURTLE"):
        """매매 체결 기록을 trade_history.csv에 저장합니다."""
        filepath = "trade_history.csv"
        file_exists = os.path.isfile(filepath)
        if strategy_type == "TURTLE":
            s = self.strategy.state.get(symbol, {})
            units_held = s.get('units_held', '')
            stop_loss = round(s.get('stop_loss', 0))
        else:
            s = self.mr_strategy.state.get(symbol, {})
            units_held = s.get('units_held', '')
            stop_loss = round(price * (1 - Config.MR_STOP_LOSS_PCT)) if action == "BUY" else 0
        row = {
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'strategy': strategy_type,
            'symbol': symbol,
            'action': action,
            'qty': qty,
            'price': price,
            'reason': reason,
            'units_held': units_held,
            'stop_loss': stop_loss,
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
