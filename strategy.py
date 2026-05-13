import pandas as pd
import time
from config import Config

class TurtleStrategy:
    def __init__(self, kis_client):
        self.kis = kis_client
        self.target_symbols = Config.get_universe()
        self.state = {} # 종목별 N, 돌파가, Unit 사이즈 등을 관리

    def calculate_indicators(self, symbol):
        """과거 봉 데이터를 받아 N, 20일 고점, 10일 저점을 계산합니다."""
        data = self.kis.get_ohlcv(symbol, "D")
        if not data:
            return None
        
        # KIS API는 최신 데이터가 인덱스 0에 오도록 내림차순 반환함
        df = pd.DataFrame(data)
        df = df[['stck_bsop_date', 'stck_clpr', 'stck_hgpr', 'stck_lwpr']]
        df.columns = ['date', 'close', 'high', 'low']
        df = df.astype({'close': float, 'high': float, 'low': float})
        df = df.sort_values('date').reset_index(drop=True) # 오름차순으로 변경 (오래된 날짜가 위로)
        
        # TR (True Range) 계산
        df['prev_close'] = df['close'].shift(1)
        df['h_minus_l'] = df['high'] - df['low']
        df['h_minus_pc'] = abs(df['high'] - df['prev_close'])
        df['l_minus_pc'] = abs(df['low'] - df['prev_close'])
        df['tr'] = df[['h_minus_l', 'h_minus_pc', 'l_minus_pc']].max(axis=1)
        
        # N (20일 ATR) 계산
        df['n'] = df['tr'].ewm(span=20, adjust=False).mean()
        
        # 20일 고점 및 10일 저점 (당일 장중 돌파를 확인하기 위해 shift(1) 적용하여 전일까지의 고/저점 사용)
        df['high_20'] = df['high'].shift(1).rolling(window=20).max()
        df['low_10'] = df['low'].shift(1).rolling(window=10).min()
        
        latest = df.iloc[-1] # 어제 종가 기준 (당일 데이터는 아직 미완성)
        return {
            'n': latest['n'],
            'high_20': latest['high_20'],
            'low_10': latest['low_10'],
            'close': latest['close']
        }

    def calculate_unit_size(self, total_equity, n_value):
        """현재 총 자산을 기준으로 1 Unit 수량을 계산합니다. (1% 룰)"""
        if pd.isna(n_value) or n_value <= 0:
            return 0
        risk_amt = total_equity * Config.RISK_PERCENT
        unit_shares = int(risk_amt / n_value)
        return unit_shares

    def prepare_daily_data(self):
        """장 시작 전(또는 봇 시작 시) 현재 자산을 확인하고, 종목별 기준가 및 Unit을 세팅합니다."""
        balance_info = self.kis.get_balance()
        if not balance_info:
            print("[Strategy] Failed to fetch balance.")
            return False
        
        total_equity = balance_info['total_equity']
        print(f"[Strategy] Current Total Equity: {total_equity:,.0f} KRW")
        time.sleep(1.0) # API 제한 방지

        # 1. 종목별 기본 지표 및 Unit 설정
        for symbol in self.target_symbols:
            inds = self.calculate_indicators(symbol)
            time.sleep(1.0) # API 초당 호출 횟수 제한(TPS) 방지
            if inds:
                unit_size = self.calculate_unit_size(total_equity, inds['n'])
                self.state[symbol] = {
                    'n': inds['n'],
                    'high_20': inds['high_20'],
                    'low_10': inds['low_10'],
                    'unit_size': unit_size,
                    'total_qty': 0,          # 보유 수량
                    'units_held': 0,         # 피라미딩 차수 (최대 4)
                    'last_entry_price': 0,   # 마지막 매수 가격
                    'stop_loss': 0           # 손절가
                }
                print(f"[{symbol}] N: {inds['n']:.2f}, 20H: {inds['high_20']}, 10L: {inds['low_10']}, 1 Unit: {unit_size}주")
        
        # 2. 현재 보유 중인 종목 상태 동기화
        for holding in balance_info['holdings']:
            sym = holding['pdno']
            if sym in self.state:
                qty = int(holding['hldg_qty'])
                avg_price = float(holding['pchs_avg_pric'])
                if qty > 0:
                    unit_size = self.state[sym]['unit_size']
                    units_held = max(1, qty // unit_size) if unit_size > 0 else 1
                    
                    self.state[sym]['total_qty'] = qty
                    self.state[sym]['units_held'] = units_held
                    self.state[sym]['last_entry_price'] = avg_price
                    # 손절매: 마지막 진입가 - 2N
                    self.state[sym]['stop_loss'] = avg_price - (2 * self.state[sym]['n'])
                    print(f"[{sym}] Sync: Holding {qty} shares ({units_held} units). Avg: {avg_price}, SL: {self.state[sym]['stop_loss']:.0f}")
        
        return True

    def update_position(self, symbol, action, qty, price):
        """주문 체결 후 내부 상태 업데이트"""
        if symbol not in self.state: return
        s = self.state[symbol]
        
        if action == "BUY":
            s['total_qty'] += qty
            s['units_held'] += 1
            s['last_entry_price'] = price
            s['stop_loss'] = price - (2 * s['n'])
        elif action == "SELL":
            s['total_qty'] -= qty
            # 청산 또는 손절 시 보유 수량을 0으로 초기화
            if s['total_qty'] <= 0:
                s['total_qty'] = 0
                s['units_held'] = 0
                s['last_entry_price'] = 0
                s['stop_loss'] = 0

    def check_signals(self, symbol, current_price):
        """실시간 현재가를 받아 매매 신호를 발생시킵니다."""
        if symbol not in self.state:
            return None
        
        s = self.state[symbol]
        
        # 1. 청산 (Exit) 및 손절 (Stop Loss) 확인
        if s['units_held'] > 0:
            if current_price <= s['low_10']:
                return {'action': 'SELL', 'reason': '10-Day Low Breakdown', 'qty': s['total_qty']}
            if current_price <= s['stop_loss']:
                return {'action': 'SELL', 'reason': 'Stop Loss (2N Drop)', 'qty': s['total_qty']}
            
            # 피라미딩 (Pyramiding) - 0.5N 상승 시 추가 진입
            next_entry = s['last_entry_price'] + (0.5 * s['n'])
            if current_price >= next_entry and s['units_held'] < Config.MAX_UNITS:
                # 잔고 체크 등은 실제 주문 로직에서 수행하므로 여기선 신호만 반환
                return {'action': 'BUY', 'reason': 'Pyramiding (0.5N Rise)', 'qty': s['unit_size']}
                
        # 2. 최초 진입 (Entry) 확인
        else:
            if current_price >= s['high_20']:
                return {'action': 'BUY', 'reason': '20-Day High Breakout', 'qty': s['unit_size']}
                
        return None
