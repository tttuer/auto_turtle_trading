import json
import os
import time
from datetime import datetime

import numpy as np
import pandas as pd

from config import Config
from financial_data_provider import FinancialDataProvider

CORE_POSITIONS_FILE = "core_positions.json"


class QualityGarpStrategy:
    """Buffett/Lynch-inspired core strategy.

    This is intentionally slow-moving. It ranks durable, understandable
    businesses first, then uses price/trend data only to avoid obviously bad
    entry points.
    """

    def __init__(self, kis_client):
        self.kis = kis_client
        self.target_symbols = Config.get_universe()
        self.financials = FinancialDataProvider()
        self.state = {}
        self._new_buys_today = 0
        self._last_trade_day = ""

    def prepare_daily_data(self):
        balance_info = self.kis.get_balance()
        if not balance_info:
            print("[CORE] Failed to fetch balance.")
            return False

        today = time.strftime("%Y%m%d")
        if self._last_trade_day != today:
            self._new_buys_today = 0
            self._last_trade_day = today

        saved_positions = self._load_saved_positions()
        total_equity = balance_info["total_equity"]
        holdings = {
            h["pdno"]: int(h.get("hldg_qty", 0))
            for h in balance_info.get("holdings", [])
        }

        ranked = []
        for symbol in self.target_symbols:
            data = self.kis.get_ohlcv(symbol, "D")
            time.sleep(1.0)
            if not data:
                continue

            metrics = self._calculate_price_metrics(symbol, data)
            if not metrics:
                continue

            saved = saved_positions.get(symbol, {})
            saved_qty = int(saved.get("total_qty", 0))
            qty = holdings.get(symbol, saved_qty) if saved_qty > 0 else 0
            entry_price = float(saved.get("entry_price", 0.0))
            entry_date = saved.get("entry_date", "")
            cooldown_until = saved.get("cooldown_until", "")
            buy_stage = int(saved.get("buy_stage", 3 if qty > 0 else 0))

            financial_metrics = self.financials.get_metrics(symbol)
            score = self._score_candidate(symbol, metrics, financial_metrics)
            state = {
                **metrics,
                **financial_metrics,
                "score": score,
                "financial_score": self._financial_score(financial_metrics),
                "target_qty": self._calculate_target_qty(total_equity, metrics["close"]),
                "total_qty": qty,
                "entry_price": entry_price,
                "entry_date": entry_date,
                "cooldown_until": cooldown_until,
                "buy_stage": buy_stage,
            }
            self.state[symbol] = state
            ranked.append((score, symbol))

        ranked.sort(reverse=True)
        allowed = {symbol for _, symbol in ranked[: Config.CORE_MAX_POSITIONS * 2]}
        for symbol, state in self.state.items():
            state["rank_allowed"] = symbol in allowed

        print("[CORE] Top candidates:", [sym for _, sym in ranked[: Config.CORE_MAX_POSITIONS]])
        return True

    def _calculate_price_metrics(self, symbol, data):
        raw = pd.DataFrame(data)
        volume_col = next(
            (col for col in ["acml_vol", "stck_vol", "cntg_vol", "tvol"] if col in raw.columns),
            None,
        )
        df = raw[["stck_bsop_date", "stck_clpr", "stck_hgpr", "stck_lwpr"]].copy()
        df.columns = ["date", "close", "high", "low"]
        df["volume"] = raw[volume_col] if volume_col else 0
        df = df.astype({"close": float, "high": float, "low": float, "volume": float})
        df = df.sort_values("date").reset_index(drop=True)

        today_str = time.strftime("%Y%m%d")
        if len(df) > 1 and df.iloc[-1]["date"] == today_str:
            df = df.iloc[:-1]

        if len(df) < 60:
            return None

        closes = df["close"]
        close = float(df.iloc[-1]["close"])
        lookback = min(252, len(df))
        momentum_lookback = min(120, len(df) - 1)
        high_52w = float(df["high"].tail(lookback).max())
        low_52w = float(df["low"].tail(lookback).min())
        ma_20 = float(closes.tail(20).mean())
        ma_60 = float(closes.tail(60).mean())
        ma_120 = float(closes.tail(min(120, len(df))).mean())
        ma_200 = float(closes.tail(min(200, len(df))).mean())
        momentum_6m = close / float(closes.iloc[-momentum_lookback]) - 1
        discount_to_high = 1 - (close / high_52w) if high_52w > 0 else 0
        drawdown_from_high = discount_to_high
        volatility = float(closes.pct_change().tail(60).std() * np.sqrt(252))
        rsi = self._calculate_rsi(closes)
        macd, macd_signal = self._calculate_macd(closes)
        bb_upper, bb_middle, bb_lower = self._calculate_bollinger(closes)
        recent_bb_lower_touch = bool((closes.tail(10) <= self._bollinger_lower_series(closes).tail(10)).any())
        avg_volume_20 = float(df["volume"].tail(20).mean())
        latest_volume = float(df.iloc[-1]["volume"])
        volume_ok = avg_volume_20 <= 0 or latest_volume >= avg_volume_20

        return {
            "close": close,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "ma_20": ma_20,
            "ma_60": ma_60,
            "ma_120": ma_120,
            "ma_200": ma_200,
            "momentum_6m": momentum_6m,
            "discount_to_high": discount_to_high,
            "drawdown_from_high": drawdown_from_high,
            "volatility": volatility,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": macd_signal,
            "bb_upper": bb_upper,
            "bb_middle": bb_middle,
            "bb_lower": bb_lower,
            "recent_bb_lower_touch": recent_bb_lower_touch,
            "avg_volume_20": avg_volume_20,
            "latest_volume": latest_volume,
            "volume_ok": volume_ok,
        }

    def _calculate_rsi(self, prices, period=14):
        delta = prices.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(span=period, adjust=False).mean()
        avg_loss = loss.ewm(span=period, adjust=False).mean()
        last_gain = float(avg_gain.iloc[-1])
        last_loss = float(avg_loss.iloc[-1])
        if last_loss == 0:
            return 100.0 if last_gain > 0 else 50.0
        rs = last_gain / last_loss
        return float(100 - (100 / (1 + rs)))

    def _calculate_macd(self, prices):
        ema_12 = prices.ewm(span=12, adjust=False).mean()
        ema_26 = prices.ewm(span=26, adjust=False).mean()
        macd = ema_12 - ema_26
        signal = macd.ewm(span=9, adjust=False).mean()
        return float(macd.iloc[-1]), float(signal.iloc[-1])

    def _calculate_bollinger(self, prices, period=20, std_mult=2.0):
        middle = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        upper = middle + std_mult * std
        lower = middle - std_mult * std
        return float(upper.iloc[-1]), float(middle.iloc[-1]), float(lower.iloc[-1])

    def _bollinger_lower_series(self, prices, period=20, std_mult=2.0):
        middle = prices.rolling(window=period).mean()
        std = prices.rolling(window=period).std()
        return middle - std_mult * std

    def _manual_quality_score(self, symbol):
        return Config.QUALITY_MANUAL_SCORES.get(symbol, 65)

    def _score_candidate(self, symbol, metrics, financial_metrics=None):
        financial_metrics = financial_metrics or {}
        quality = self._manual_quality_score(symbol)
        if financial_metrics:
            quality = (quality * 0.35) + (self._financial_score(financial_metrics) * 0.65)

        valuation_proxy = min(metrics["discount_to_high"] / Config.CORE_BUY_DISCOUNT_TO_HIGH, 1.5) * 15
        trend_penalty = 15 if metrics["close"] < metrics["ma_120"] else 0
        volatility_penalty = min(metrics["volatility"] * 20, 10)
        momentum_bonus = max(min(metrics["momentum_6m"] * 20, 8), -8)
        return round(quality + valuation_proxy + momentum_bonus - trend_penalty - volatility_penalty, 2)

    def _financial_score(self, metrics):
        if not metrics:
            return 0

        score = 55
        score += self._range_score(metrics.get("roe"), 0.05, 0.20, 0, 18)
        score += self._range_score(metrics.get("operating_margin"), 0.03, 0.18, -5, 12)
        score += self._range_score(metrics.get("revenue_growth"), -0.05, 0.15, -8, 12)
        score += self._range_score(metrics.get("operating_income_growth"), -0.05, 0.18, -8, 12)
        score += self._range_score(metrics.get("net_income_growth"), -0.05, 0.18, -8, 10)
        score += self._range_score(metrics.get("dividend_yield"), 0.01, 0.04, 0, 5)

        debt_to_equity = metrics.get("debt_to_equity")
        if debt_to_equity is not None:
            if debt_to_equity <= 0.8:
                score += 8
            elif debt_to_equity <= 1.5:
                score += 2
            else:
                score -= 10

        per = metrics.get("per")
        if per and per > 0:
            if per <= 12:
                score += 8
            elif per <= 20:
                score += 4
            elif per > 35:
                score -= 8

        pbr = metrics.get("pbr")
        if pbr and pbr > 0:
            if pbr <= 1.0:
                score += 6
            elif pbr <= 2.5:
                score += 2
            elif pbr > 5:
                score -= 6

        growth_candidates = [
            metrics.get("revenue_growth"),
            metrics.get("operating_income_growth"),
            metrics.get("net_income_growth"),
        ]
        growth_values = [g for g in growth_candidates if g is not None and g > 0]
        growth = sum(growth_values) / len(growth_values) if growth_values else None
        if per and per > 0 and growth:
            peg = per / (growth * 100)
            if peg <= 1.0:
                score += 10
            elif peg <= 1.8:
                score += 5
            elif peg > 3.0:
                score -= 8
        elif per and per > 25 and not growth_values:
            score -= 5

        return round(max(0, min(score, 100)), 2)

    def _range_score(self, value, low, high, min_points, max_points):
        if value is None:
            return 0
        if value <= low:
            return min_points
        if value >= high:
            return max_points
        ratio = (value - low) / (high - low)
        return min_points + ratio * (max_points - min_points)

    def _calculate_target_qty(self, total_equity, price):
        if price <= 0:
            return 0
        equal_weight_value = total_equity * Config.QUALITY_GARP_CAPITAL_RATIO / Config.CORE_MAX_POSITIONS
        max_position_value = total_equity * Config.CORE_MAX_POSITION_PCT
        target_value = min(equal_weight_value, max_position_value)
        return int(target_value // price)

    def should_evaluate_now(self):
        now = datetime.now()
        return (
            now.hour > Config.CORE_REBALANCE_HOUR
            or (now.hour == Config.CORE_REBALANCE_HOUR and now.minute >= Config.CORE_REBALANCE_MINUTE)
        )

    def check_signals(self, symbol, current_price):
        if symbol not in self.state or not self.should_evaluate_now():
            return None

        s = self.state[symbol]
        if s["total_qty"] > 0:
            exit_signal = self._check_exit_signal(symbol, current_price)
            if exit_signal:
                return exit_signal
            if s.get("buy_stage", 0) < len(Config.CORE_STAGE_WEIGHTS):
                return self._check_entry_signal(symbol, current_price)
            return None
        return self._check_entry_signal(symbol, current_price)

    def _check_entry_signal(self, symbol, current_price):
        s = self.state[symbol]
        if self._new_buys_today >= Config.CORE_MAX_NEW_BUYS_PER_DAY:
            return None
        if not s["rank_allowed"]:
            return None
        if s["cooldown_until"] and time.strftime("%Y%m%d") < s["cooldown_until"]:
            return None
        if s["target_qty"] <= 0:
            return None

        reasonably_priced = s["discount_to_high"] >= Config.CORE_BUY_DISCOUNT_TO_HIGH
        not_broken = s["momentum_6m"] >= Config.CORE_MIN_MOMENTUM_6M
        trend_ok = current_price >= s["ma_120"] or current_price >= s["ma_60"]
        fundamentals_ok = self._fundamentals_ok(s)

        if not (reasonably_priced and not_broken and fundamentals_ok):
            return None

        next_stage = s.get("buy_stage", 0) + 1
        if next_stage == 1:
            stage_ok = (
                s["recent_bb_lower_touch"]
                and current_price > s["bb_middle"]
                and current_price > s["ma_20"]
                and 40 <= s["rsi"] <= 65
                and s["macd"] > s["macd_signal"]
                and s["volume_ok"]
            )
        elif next_stage == 2:
            stage_ok = (
                current_price > s["ma_20"]
                and s["ma_20"] > s["ma_60"]
                and 45 <= s["rsi"] <= 65
                and s["macd"] > s["macd_signal"]
                and s["volume_ok"]
            )
        elif next_stage == 3:
            stage_ok = (
                trend_ok
                and current_price > s["ma_60"]
                and s["ma_20"] > s["ma_60"]
                and 50 <= s["rsi"] <= 70
                and s["macd"] > 0
                and s["macd"] > s["macd_signal"]
            )
        else:
            return None

        if stage_ok:
            qty = self._calculate_stage_qty(s, next_stage)
            if qty <= 0:
                return None
            return {
                "action": "BUY",
                "reason": (
                    f"CORE Stage {next_stage} Quality/GARP score={s['score']}, "
                    f"fin={s.get('financial_score', 0)}, RSI={s['rsi']:.1f}, discount={s['discount_to_high']:.1%}"
                ),
                "qty": qty,
                "stage": next_stage,
            }
        return None

    def _calculate_stage_qty(self, state, stage):
        weights = Config.CORE_STAGE_WEIGHTS
        if stage < 1 or stage > len(weights):
            return 0
        target_qty = int(state["target_qty"])
        if target_qty <= 0 or state["total_qty"] >= target_qty:
            return 0
        stage_qty = int(target_qty * weights[stage - 1])
        if stage == len(weights):
            stage_qty = target_qty - int(state["total_qty"])
        return min(max(1, stage_qty), target_qty - int(state["total_qty"]))

    def _fundamentals_ok(self, state):
        if not Config.ENABLE_FINANCIAL_DATA:
            return True
        if state.get("financial_year"):
            if state.get("roe") is not None and state["roe"] < 0:
                return False
            if state.get("operating_margin") is not None and state["operating_margin"] < 0:
                return False
            if state.get("debt_to_equity") is not None and state["debt_to_equity"] > 2.5:
                return False
            if state.get("financial_score", 0) < Config.CORE_STRONG_FINANCIAL_SCORE:
                return False
        if state.get("per") is not None and state["per"] <= 0:
            return False
        return True

    def _check_exit_signal(self, symbol, current_price):
        s = self.state[symbol]
        hold_days = self._holding_days(s["entry_date"])
        if hold_days < Config.CORE_MIN_HOLD_DAYS:
            return None

        severe_break = current_price <= s["high_52w"] * (1 - Config.CORE_MAX_DRAWDOWN_EXIT)
        trend_broken = current_price < s["ma_120"] and s["momentum_6m"] < Config.CORE_MIN_MOMENTUM_6M
        no_longer_top_candidate = not s["rank_allowed"] and current_price < s["ma_120"]
        fundamentals_broken = not self._fundamentals_ok(s) and s.get("financial_year")
        technical_weakness = current_price < s["ma_60"] and s["macd"] < s["macd_signal"] and s["rsi"] < 45
        strong_break = (
            current_price < s["ma_200"]
            and s["ma_20"] < s["ma_60"]
            and s["macd"] < 0
            and s["rsi"] < 40
            and (s["avg_volume_20"] <= 0 or s["latest_volume"] > s["avg_volume_20"] * 1.5)
        )

        if fundamentals_broken or severe_break or strong_break:
            qty = max(1, int(s["total_qty"] * Config.CORE_STRONG_SELL_PCT))
            return {
                "action": "SELL",
                "reason": f"CORE Strong Exit (hold={hold_days}d, score={s['score']})",
                "qty": min(qty, s["total_qty"]),
            }
        if trend_broken or no_longer_top_candidate or technical_weakness:
            qty = max(1, int(s["total_qty"] * Config.CORE_PARTIAL_SELL_PCT))
            return {
                "action": "SELL",
                "reason": f"CORE Trim Weakness (hold={hold_days}d, score={s['score']})",
                "qty": min(qty, s["total_qty"]),
            }
        return None

    def _holding_days(self, entry_date):
        if not entry_date:
            return 0
        try:
            entry_iso = datetime.strptime(entry_date, "%Y%m%d").strftime("%Y-%m-%d")
            today_iso = datetime.strptime(time.strftime("%Y%m%d"), "%Y%m%d").strftime("%Y-%m-%d")
            return int(np.busday_count(entry_iso, today_iso))
        except Exception:
            return 0

    def update_position(self, symbol, action, qty, price):
        if symbol not in self.state:
            return
        s = self.state[symbol]

        if action == "BUY":
            s["total_qty"] += qty
            s["entry_price"] = price
            s["entry_date"] = time.strftime("%Y%m%d")
            s["cooldown_until"] = ""
            s["buy_stage"] = min(len(Config.CORE_STAGE_WEIGHTS), s.get("buy_stage", 0) + 1)
            self._new_buys_today += 1
        elif action == "SELL":
            s["total_qty"] = max(0, s["total_qty"] - qty)
            if s["total_qty"] == 0:
                s["entry_price"] = 0.0
                s["entry_date"] = ""
                s["buy_stage"] = 0
                s["cooldown_until"] = self._future_business_day(Config.CORE_COOLDOWN_DAYS)

        self._save_positions()

    def _future_business_day(self, days):
        today = np.datetime64(datetime.strptime(time.strftime("%Y%m%d"), "%Y%m%d").date())
        future = np.busday_offset(today, days, roll="forward")
        return str(future).replace("-", "")

    def _load_saved_positions(self):
        if not os.path.isfile(CORE_POSITIONS_FILE):
            return {}
        try:
            with open(CORE_POSITIONS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_positions(self):
        to_save = {}
        for sym, s in self.state.items():
            if s["total_qty"] > 0 or s.get("cooldown_until"):
                to_save[sym] = {
                    "total_qty": s["total_qty"],
                    "entry_price": s["entry_price"],
                    "entry_date": s["entry_date"],
                    "cooldown_until": s.get("cooldown_until", ""),
                    "buy_stage": s.get("buy_stage", 0),
                }
        with open(CORE_POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
