import json
import sqlite3
import time
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import requests

from config import Config


class FinancialDataProvider:
    """Fetches and caches fundamentals from OpenDART and pykrx.

    OpenDART is the source of truth for accounting data. pykrx is used as a
    convenient daily valuation supplement for PER/PBR/EPS/BPS/DPS/DIV.
    All network calls are optional and cached so the trading bot can continue
    operating when data providers are down or credentials are missing.
    """

    DART_BASE_URL = "https://opendart.fss.or.kr/api"

    def __init__(self, db_path=None):
        self.db_path = Path(db_path or Config.FINANCIAL_CACHE_DB)
        self._ensure_schema()
        self._corp_code_cache = None

    def get_metrics(self, symbol):
        metrics = {}
        if not Config.ENABLE_FINANCIAL_DATA:
            return metrics

        metrics.update(self.get_dart_metrics(symbol))
        metrics.update(self.get_krx_metrics(symbol))
        return metrics

    def get_dart_metrics(self, symbol):
        cached = self._load_cache("dart", symbol, Config.DART_CACHE_DAYS)
        if cached is not None:
            return cached
        stale = self._load_cache("dart", symbol, None)
        if not Config.DART_API_KEY:
            return stale or {}

        try:
            corp_code = self._get_corp_code(symbol)
            if not corp_code:
                return {}
            statements = self._fetch_recent_annual_statements(corp_code)
            metrics = self._build_dart_metrics(statements)
            self._save_cache("dart", symbol, metrics)
            return metrics
        except Exception as exc:
            print(f"[FIN][DART] {symbol} fetch failed: {exc}")
            return stale or {}

    def get_krx_metrics(self, symbol):
        cached = self._load_cache("krx", symbol, Config.KRX_CACHE_DAYS)
        if cached is not None:
            return cached
        stale = self._load_cache("krx", symbol, None)

        try:
            from pykrx import stock

            end = datetime.now()
            start = end - timedelta(days=30)
            df = stock.get_market_fundamental_by_date(
                start.strftime("%Y%m%d"),
                end.strftime("%Y%m%d"),
                symbol,
            )
            if df is None or df.empty:
                return {}
            latest = df.sort_index().iloc[-1]
            metrics = {
                "bps": self._to_float(latest.get("BPS")),
                "per": self._to_float(latest.get("PER")),
                "pbr": self._to_float(latest.get("PBR")),
                "eps": self._to_float(latest.get("EPS")),
                "dividend_yield": self._to_float(latest.get("DIV")),
                "dps": self._to_float(latest.get("DPS")),
            }
            metrics = {k: v for k, v in metrics.items() if v is not None}
            self._save_cache("krx", symbol, metrics)
            return metrics
        except ImportError:
            print("[FIN][KRX] pykrx is not installed; skipping valuation data.")
            return stale or {}
        except Exception as exc:
            print(f"[FIN][KRX] {symbol} fetch failed: {exc}")
            return stale or {}

    def refresh_symbol(self, symbol):
        self._delete_cache("dart", symbol)
        self._delete_cache("krx", symbol)
        return self.get_metrics(symbol)

    def _fetch_recent_annual_statements(self, corp_code):
        current_year = datetime.now().year
        statements = []
        for year in range(current_year - 1, current_year - 5, -1):
            data = self._fetch_dart_statement(corp_code, year, "11011", "CFS")
            if not data:
                data = self._fetch_dart_statement(corp_code, year, "11011", "OFS")
            if data:
                statements.append({"year": year, "rows": data})
            time.sleep(0.2)
        return statements

    def _fetch_dart_statement(self, corp_code, year, report_code, fs_div):
        params = {
            "crtfc_key": Config.DART_API_KEY,
            "corp_code": corp_code,
            "bsns_year": str(year),
            "reprt_code": report_code,
            "fs_div": fs_div,
        }
        res = requests.get(f"{self.DART_BASE_URL}/fnlttSinglAcnt.json", params=params, timeout=10)
        res.raise_for_status()
        payload = res.json()
        if payload.get("status") != "000":
            return []
        return payload.get("list", [])

    def _build_dart_metrics(self, statements):
        yearly = []
        for statement in statements:
            rows = statement["rows"]
            parsed = {
                "year": statement["year"],
                "revenue": self._find_account(rows, ["매출액", "영업수익"]),
                "operating_income": self._find_account(rows, ["영업이익"]),
                "net_income": self._find_account(rows, ["당기순이익", "분기순이익"]),
                "assets": self._find_account(rows, ["자산총계"]),
                "liabilities": self._find_account(rows, ["부채총계"]),
                "equity": self._find_account(rows, ["자본총계"]),
            }
            yearly.append(parsed)

        latest = next((row for row in yearly if row.get("equity")), {})
        metrics = {
            "financial_year": latest.get("year"),
            "revenue": latest.get("revenue"),
            "operating_income": latest.get("operating_income"),
            "net_income": latest.get("net_income"),
            "assets": latest.get("assets"),
            "liabilities": latest.get("liabilities"),
            "equity": latest.get("equity"),
        }

        equity = metrics.get("equity")
        liabilities = metrics.get("liabilities")
        revenue = metrics.get("revenue")
        operating_income = metrics.get("operating_income")
        net_income = metrics.get("net_income")

        if equity and net_income is not None:
            metrics["roe"] = net_income / equity
        if assets := metrics.get("assets"):
            if net_income is not None:
                metrics["roa"] = net_income / assets
        if equity and liabilities is not None:
            metrics["debt_to_equity"] = liabilities / equity
        if revenue and operating_income is not None:
            metrics["operating_margin"] = operating_income / revenue
        if revenue and net_income is not None:
            metrics["net_margin"] = net_income / revenue

        metrics.update(self._growth_metrics(yearly))
        return {k: v for k, v in metrics.items() if v is not None}

    def _growth_metrics(self, yearly):
        sorted_rows = sorted(yearly, key=lambda row: row["year"])
        if len(sorted_rows) < 2:
            return {}

        def growth(key):
            first = next((row.get(key) for row in sorted_rows if row.get(key)), None)
            last = next((row.get(key) for row in reversed(sorted_rows) if row.get(key)), None)
            if not first or not last or first <= 0:
                return None
            years = max(1, sorted_rows[-1]["year"] - sorted_rows[0]["year"])
            return (last / first) ** (1 / years) - 1

        return {
            "revenue_growth": growth("revenue"),
            "operating_income_growth": growth("operating_income"),
            "net_income_growth": growth("net_income"),
        }

    def _find_account(self, rows, names):
        for row in rows:
            account_nm = row.get("account_nm", "")
            if any(name in account_nm for name in names):
                value = self._to_float(row.get("thstrm_amount"))
                if value is not None:
                    return value
        return None

    def _get_corp_code(self, symbol):
        if self._corp_code_cache is None:
            self._corp_code_cache = self._load_corp_codes()
        return self._corp_code_cache.get(symbol)

    def _load_corp_codes(self):
        cached = self._load_cache("dart_corp_codes", "all", 30)
        if cached:
            return cached

        params = {"crtfc_key": Config.DART_API_KEY}
        res = requests.get(f"{self.DART_BASE_URL}/corpCode.xml", params=params, timeout=20)
        res.raise_for_status()

        with zipfile.ZipFile(BytesIO(res.content)) as zipped:
            xml_name = zipped.namelist()[0]
            root = ElementTree.fromstring(zipped.read(xml_name))

        mapping = {}
        for item in root.findall("list"):
            stock_code = item.findtext("stock_code", "").strip()
            corp_code = item.findtext("corp_code", "").strip()
            if stock_code and corp_code:
                mapping[stock_code] = corp_code

        self._save_cache("dart_corp_codes", "all", mapping)
        return mapping

    def _ensure_schema(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS financial_cache (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (source, symbol)
                )
                """
            )

    def _load_cache(self, source, symbol, max_age_days):
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT fetched_at, payload FROM financial_cache WHERE source = ? AND symbol = ?",
                (source, symbol),
            ).fetchone()
        if not row:
            return None

        if max_age_days is not None:
            fetched_at = datetime.fromisoformat(row[0])
            if datetime.now() - fetched_at > timedelta(days=max_age_days):
                return None
        return json.loads(row[1])

    def _save_cache(self, source, symbol, payload):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO financial_cache (source, symbol, fetched_at, payload)
                VALUES (?, ?, ?, ?)
                """,
                (source, symbol, datetime.now().isoformat(timespec="seconds"), json.dumps(payload)),
            )

    def _delete_cache(self, source, symbol):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM financial_cache WHERE source = ? AND symbol = ?",
                (source, symbol),
            )

    def _to_float(self, value):
        if value is None:
            return None
        try:
            if isinstance(value, str):
                value = value.replace(",", "").strip()
                if not value or value == "-":
                    return None
            return float(value)
        except (TypeError, ValueError):
            return None
