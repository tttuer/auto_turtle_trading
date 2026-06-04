import json
import os
import time
from datetime import datetime
import numpy as np
import pandas as pd
from config import Config

MR_POSITIONS_FILE = "mr_positions.json"


class MeanReversionStrategy:
    def __init__(self, kis_client):
        self.kis = kis_client
        self.target_symbols = Config.get_universe()
        self.state = {}

    def _calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float('inf'))
        rsi = 100 - (100 / (1 + rs))
        return float(rsi.iloc[-1])

    def _calculate_bollinger(self, prices: pd.Series, period: int = 20, std_mult: float = 2.0):
        ma = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        upper = (ma + std_mult * std).iloc[-1]
        middle = ma.iloc[-1]
        lower = (ma - std_mult * std).iloc[-1]
        return float(upper), float(middle), float(lower)

    def _calculate_unit_size(self, total_equity: float, close_price: float) -> int:
        if close_price <= 0:
            return 0
        allocated = total_equity * Config.MR_CAPITAL_RATIO
        risk_amt = allocated * Config.RISK_PERCENT
        stop_loss_per_share = close_price * Config.MR_STOP_LOSS_PCT
        return int(risk_amt / stop_loss_per_share) if stop_loss_per_share > 0 else 0

    def prepare_daily_data(self):
        balance_info = self.kis.get_balance()
        if not balance_info:
            print("[MR] Failed to fetch balance.")
            return False

        total_equity = balance_info['total_equity']
        allocated = total_equity * Config.MR_CAPITAL_RATIO
        print(f"[MR] Allocated Capital: {allocated:,.0f} KRW ({Config.MR_CAPITAL_RATIO*100:.0f}%)")
        time.sleep(1.0)

        today_str = time.strftime('%Y%m%d')
        saved_positions = self._load_saved_positions()

        for symbol in self.target_symbols:
            data = self.kis.get_ohlcv(symbol, "D")
            time.sleep(1.0)
            if not data:
                continue

            df = pd.DataFrame(data)
            df = df[['stck_bsop_date', 'stck_clpr', 'stck_hgpr', 'stck_lwpr']]
            df.columns = ['date', 'close', 'high', 'low']
            df = df.astype({'close': float, 'high': float, 'low': float})
            df = df.sort_values('date').reset_index(drop=True)

            # 당일 장중 데이터가 포함된 경우 제외 (전일 기준으로 지표 계산)
            if df.iloc[-1]['date'] == today_str:
                df = df.iloc[:-1]

            if len(df) < Config.BB_PERIOD + 5:
                continue

            closes = df['close']
            rsi = self._calculate_rsi(closes, Config.RSI_PERIOD)
            bb_upper, bb_middle, bb_lower = self._calculate_bollinger(closes, Config.BB_PERIOD, Config.BB_STD)
            close_price = float(closes.iloc[-1])
            unit_size = self._calculate_unit_size(total_equity, close_price)

            saved = saved_positions.get(symbol, {})
            entry_price = float(saved.get('entry_price', 0.0))
            entry_date = saved.get('entry_date', '')
            total_qty = int(saved.get('total_qty', 0))
            units_held = int(saved.get('units_held', 0))

            should_force_exit = False
            if units_held > 0 and entry_date:
                try:
                    entry_iso = datetime.strptime(entry_date, '%Y%m%d').strftime('%Y-%m-%d')
                    today_iso = datetime.strptime(today_str, '%Y%m%d').strftime('%Y-%m-%d')
                    # 영업일 기준 보유일 계산 (달력일 아님)
                    hold_days = int(np.busday_count(entry_iso, today_iso))
                    if hold_days >= Config.MR_MAX_HOLD_DAYS:
                        should_force_exit = True
                        print(f"[MR] {symbol} held {hold_days} trading days >= {Config.MR_MAX_HOLD_DAYS}d, force exit queued")
                except (ValueError, Exception):
                    pass

            self.state[symbol] = {
                'rsi': rsi,
                'bb_upper': bb_upper,
                'bb_middle': bb_middle,
                'bb_lower': bb_lower,
                'unit_size': unit_size,
                'total_qty': total_qty,
                'units_held': units_held,
                'entry_price': entry_price,
                'entry_date': entry_date,
                'should_force_exit': should_force_exit,
            }
            print(f"[MR][{symbol}] RSI: {rsi:.1f}, BB: {bb_lower:.0f}/{bb_middle:.0f}/{bb_upper:.0f}, Unit: {unit_size}주")

        return True

    def check_signals(self, symbol, current_price):
        if symbol not in self.state:
            return None

        s = self.state[symbol]

        if s['units_held'] > 0:
            if s['should_force_exit']:
                return {'action': 'SELL', 'reason': f'MR Max Hold ({Config.MR_MAX_HOLD_DAYS}d)', 'qty': s['total_qty']}
            if current_price >= s['bb_middle']:
                return {'action': 'SELL', 'reason': 'MR Mean Reversion (BB Middle)', 'qty': s['total_qty']}
            if s['entry_price'] > 0 and current_price <= s['entry_price'] * (1 - Config.MR_STOP_LOSS_PCT):
                return {'action': 'SELL', 'reason': f'MR Stop Loss ({Config.MR_STOP_LOSS_PCT*100:.0f}%)', 'qty': s['total_qty']}
        else:
            if s['rsi'] < Config.MR_RSI_OVERSOLD and current_price <= s['bb_lower'] and s['unit_size'] > 0:
                return {'action': 'BUY', 'reason': f'MR Oversold (RSI={s["rsi"]:.1f}, BB Lower={s["bb_lower"]:.0f})', 'qty': s['unit_size']}

        return None

    def update_position(self, symbol, action, qty, price):
        if symbol not in self.state:
            return
        s = self.state[symbol]

        if action == 'BUY':
            s['total_qty'] += qty
            s['units_held'] = 1
            s['entry_price'] = price
            s['entry_date'] = time.strftime('%Y%m%d')
            s['should_force_exit'] = False
        elif action == 'SELL':
            s['total_qty'] = 0
            s['units_held'] = 0
            s['entry_price'] = 0.0
            s['entry_date'] = ''
            s['should_force_exit'] = False

        self._save_positions()

    def _load_saved_positions(self):
        if not os.path.isfile(MR_POSITIONS_FILE):
            return {}
        try:
            with open(MR_POSITIONS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_positions(self):
        to_save = {
            sym: {
                'units_held': s['units_held'],
                'total_qty': s['total_qty'],
                'entry_price': s['entry_price'],
                'entry_date': s['entry_date'],
            }
            for sym, s in self.state.items()
            if s['units_held'] > 0
        }
        with open(MR_POSITIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
