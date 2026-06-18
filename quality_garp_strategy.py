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

            financial_metrics = self.financials.get_metrics(symbol)
            score = self._score_candidate(symbol, metrics, financial_metrics)
            state = {
                **metrics,
                **financial_metrics,
                "score": score,
                "financial_score": self._financial_score(financial_metrics),
                "unit_size": self._calculate_target_qty(total_equity, metrics["close"]),
                "total_qty": qty,
                "entry_price": entry_price,
                "entry_date": entry_date,
                "cooldown_until": cooldown_until,
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
        df = pd.DataFrame(data)
        df = df[["stck_bsop_date", "stck_clpr", "stck_hgpr", "stck_lwpr"]]
        df.columns = ["date", "close", "high", "low"]
        df = df.astype({"close": float, "high": float, "low": float})
        df = df.sort_values("date").reset_index(drop=True)

        today_str = time.strftime("%Y%m%d")
        if len(df) > 1 and df.iloc[-1]["date"] == today_str:
            df = df.iloc[:-1]

        if len(df) < 60:
            return None

        close = float(df.iloc[-1]["close"])
        lookback = min(252, len(df))
        momentum_lookback = min(120, len(df) - 1)
        high_52w = float(df["high"].tail(lookback).max())
        low_52w = float(df["low"].tail(lookback).min())
        ma_60 = float(df["close"].tail(60).mean())
        ma_120 = float(df["close"].tail(min(120, len(df))).mean())
        momentum_6m = close / float(df["close"].iloc[-momentum_lookback]) - 1
        discount_to_high = 1 - (close / high_52w) if high_52w > 0 else 0
        drawdown_from_high = discount_to_high
        volatility = float(df["close"].pct_change().tail(60).std() * np.sqrt(252))

        return {
            "close": close,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "ma_60": ma_60,
            "ma_120": ma_120,
            "momentum_6m": momentum_6m,
            "discount_to_high": discount_to_high,
            "drawdown_from_high": drawdown_from_high,
            "volatility": volatility,
        }

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
            return self._check_exit_signal(symbol, current_price)
        return self._check_entry_signal(symbol, current_price)

    def _check_entry_signal(self, symbol, current_price):
        s = self.state[symbol]
        if self._new_buys_today >= Config.CORE_MAX_NEW_BUYS_PER_DAY:
            return None
        if not s["rank_allowed"]:
            return None
        if s["cooldown_until"] and time.strftime("%Y%m%d") < s["cooldown_until"]:
            return None
        if s["unit_size"] <= 0:
            return None

        reasonably_priced = s["discount_to_high"] >= Config.CORE_BUY_DISCOUNT_TO_HIGH
        not_broken = s["momentum_6m"] >= Config.CORE_MIN_MOMENTUM_6M
        trend_ok = current_price >= s["ma_120"] or current_price >= s["ma_60"]
        fundamentals_ok = self._fundamentals_ok(s)

        if reasonably_priced and not_broken and trend_ok and fundamentals_ok:
            return {
                "action": "BUY",
                "reason": f"CORE Quality/GARP score={s['score']}, fin={s.get('financial_score', 0)}, discount={s['discount_to_high']:.1%}",
                "qty": s["unit_size"],
            }
        return None

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

        if severe_break or trend_broken or no_longer_top_candidate or fundamentals_broken:
            return {
                "action": "SELL",
                "reason": f"CORE Thesis Proxy Broken (hold={hold_days}d, score={s['score']})",
                "qty": s["total_qty"],
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
            self._new_buys_today += 1
        elif action == "SELL":
            s["total_qty"] = 0
            s["entry_price"] = 0.0
            s["entry_date"] = ""
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
                }
        with open(CORE_POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
