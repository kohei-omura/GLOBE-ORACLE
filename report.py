#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLOBE ORACLE — 米国株オラクル 本体（report.py）

- yfinance のみでS&P500＋NASDAQ100を日次スコアリングし docs/ にダッシュボード生成
- スコア: EMA/RSI/MACD/BB/ADX の複合（-100..100）＋ファンダ補正
- 市場時間: zoneinfo(America/New_York)でET基準に算出し、JSTのポーリング窓(epoch)を埋め込む
- 出力: docs/index.html, app.js, stocks.json, prices.json, manifest.json, icon-*.png
免責: 投資判断は自己責任。データはyfinance由来で遅延・欠損があり得ます。
"""
from __future__ import annotations

import json
import math
import sys
import traceback
from datetime import datetime, timezone, timedelta, date, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

import numpy as np
import pandas as pd

import io
try:
    import requests
except Exception:  # pragma: no cover
    requests = None

JST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York") if ZoneInfo else timezone(timedelta(hours=-5))
DOCS = Path("docs")
DOCS.mkdir(exist_ok=True)

WATCH_FALLBACK: list[str] = []  # クライアント側 localStorage 管理（サーバーは空でOK）

HOLDINGS_FILE = Path("holdings.txt")


def load_holdings() -> list[dict]:
    """holdings.txt を読む。1行 = 「ティッカー,買値[,利確,損切]」。#はコメント。
    例: SOFI,18.68  /  AAPL,180,210,165"""
    out: list[dict] = []
    if not HOLDINGS_FILE.exists():
        return out
    try:
        for line in HOLDINGS_FILE.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.replace("\t", ",").split(",") if p.strip() != ""]
            if len(parts) < 2:
                continue
            try:
                rec = {"code": _norm_ticker(parts[0]), "avg": float(parts[1])}
                if len(parts) >= 3:
                    rec["tgt"] = float(parts[2])
                if len(parts) >= 4:
                    rec["stp"] = float(parts[3])
                out.append(rec)
            except Exception:
                print(f"[globe] holdings行を無視: {s}", file=sys.stderr)
    except Exception as e:
        print(f"[globe] holdings.txt読込失敗: {e}", file=sys.stderr)
    return out

# ─────────────────────────────────────────────
#  ユニバース: S&P500 ＋ NASDAQ100（重複除去）
# ─────────────────────────────────────────────
FALLBACK_UNIVERSE = [
    ("AAPL", "Apple Inc.", "Technology"), ("MSFT", "Microsoft Corp.", "Technology"),
    ("NVDA", "NVIDIA Corp.", "Technology"), ("AMZN", "Amazon.com Inc.", "Consumer Discretionary"),
    ("GOOGL", "Alphabet Inc. A", "Communication"), ("GOOG", "Alphabet Inc. C", "Communication"),
    ("META", "Meta Platforms", "Communication"), ("TSLA", "Tesla Inc.", "Consumer Discretionary"),
    ("BRK-B", "Berkshire Hathaway B", "Financials"), ("AVGO", "Broadcom Inc.", "Technology"),
    ("JPM", "JPMorgan Chase", "Financials"), ("LLY", "Eli Lilly", "Health Care"),
    ("V", "Visa Inc.", "Financials"), ("XOM", "Exxon Mobil", "Energy"),
    ("UNH", "UnitedHealth", "Health Care"), ("MA", "Mastercard", "Financials"),
    ("COST", "Costco", "Consumer Staples"), ("HD", "Home Depot", "Consumer Discretionary"),
    ("PG", "Procter & Gamble", "Consumer Staples"), ("JNJ", "Johnson & Johnson", "Health Care"),
    ("NFLX", "Netflix", "Communication"), ("BAC", "Bank of America", "Financials"),
    ("ABBV", "AbbVie", "Health Care"), ("CRM", "Salesforce", "Technology"),
    ("ORCL", "Oracle Corp.", "Technology"), ("KO", "Coca-Cola", "Consumer Staples"),
    ("CVX", "Chevron", "Energy"), ("WMT", "Walmart", "Consumer Staples"),
    ("AMD", "Advanced Micro Devices", "Technology"), ("PEP", "PepsiCo", "Consumer Staples"),
    ("ADBE", "Adobe Inc.", "Technology"), ("QCOM", "Qualcomm", "Technology"),
    ("TMO", "Thermo Fisher", "Health Care"), ("MCD", "McDonald's", "Consumer Discretionary"),
    ("CSCO", "Cisco Systems", "Technology"), ("INTC", "Intel Corp.", "Technology"),
    ("TXN", "Texas Instruments", "Technology"), ("AMAT", "Applied Materials", "Technology"),
    ("INTU", "Intuit Inc.", "Technology"), ("IBM", "IBM Corp.", "Technology"),
    ("PFE", "Pfizer", "Health Care"), ("GE", "GE Aerospace", "Industrials"),
    ("CAT", "Caterpillar", "Industrials"), ("NOW", "ServiceNow", "Technology"),
    ("DIS", "Walt Disney", "Communication"), ("VZ", "Verizon", "Communication"),
    ("BA", "Boeing", "Industrials"), ("GS", "Goldman Sachs", "Financials"),
    ("HON", "Honeywell", "Industrials"), ("AMGN", "Amgen", "Health Care"),
    ("BKNG", "Booking Holdings", "Consumer Discretionary"), ("SBUX", "Starbucks", "Consumer Discretionary"),
    ("PLTR", "Palantir", "Technology"), ("MU", "Micron", "Technology"),
    ("ISRG", "Intuitive Surgical", "Health Care"), ("LRCX", "Lam Research", "Technology"),
    ("ADP", "ADP", "Industrials"), ("GILD", "Gilead Sciences", "Health Care"),
    ("REGN", "Regeneron", "Health Care"), ("VRTX", "Vertex Pharma", "Health Care"),
    ("PANW", "Palo Alto Networks", "Technology"), ("KLAC", "KLA Corp.", "Technology"),
    ("SNPS", "Synopsys", "Technology"), ("CDNS", "Cadence", "Technology"),
    ("MRVL", "Marvell", "Technology"), ("FTNT", "Fortinet", "Technology"),
    ("ABNB", "Airbnb", "Consumer Discretionary"), ("PYPL", "PayPal", "Financials"),
    ("MELI", "MercadoLibre", "Consumer Discretionary"), ("CMCSA", "Comcast", "Communication"),
    ("T", "AT&T", "Communication"), ("NKE", "Nike", "Consumer Discretionary"),
    ("LIN", "Linde plc", "Materials"), ("MDLZ", "Mondelez", "Consumer Staples"),
    ("CME", "CME Group", "Financials"), ("AXP", "American Express", "Financials"),
    ("MS", "Morgan Stanley", "Financials"), ("BLK", "BlackRock", "Financials"),
    ("SPGI", "S&P Global", "Financials"), ("UNP", "Union Pacific", "Industrials"),
    ("RTX", "RTX Corp.", "Industrials"), ("LOW", "Lowe's", "Consumer Discretionary"),
    ("ELV", "Elevance Health", "Health Care"), ("SCHW", "Charles Schwab", "Financials"),
    ("PGR", "Progressive", "Financials"), ("C", "Citigroup", "Financials"),
    ("BSX", "Boston Scientific", "Health Care"), ("SYK", "Stryker", "Health Care"),
    ("DE", "Deere & Co.", "Industrials"), ("ADI", "Analog Devices", "Technology"),
    ("MMC", "Marsh McLennan", "Financials"), ("TJX", "TJX Companies", "Consumer Discretionary"),
    ("CB", "Chubb", "Financials"), ("MO", "Altria", "Consumer Staples"),
    ("PLD", "Prologis", "Real Estate"), ("FI", "Fiserv", "Financials"),
    ("ZTS", "Zoetis", "Health Care"), ("SO", "Southern Co.", "Utilities"),
    ("DUK", "Duke Energy", "Utilities"), ("APH", "Amphenol", "Technology"),
]


def _norm_ticker(t: str) -> str:
    """Wikipedia表記(BRK.B)→yfinance表記(BRK-B)。"""
    return (t or "").strip().upper().replace(".", "-")


UNIVERSE_REDUCED = {"reduced": False}  # フォールバック（縮小モード）フラグ


def _read_wiki_tables(url: str):
    """UA付きrequestsでWikipediaを取得しread_html。403回避・リトライ2回。"""
    headers = {"User-Agent": "Mozilla/5.0 (GLOBE-ORACLE bot)"}
    for attempt in range(2):
        try:
            if requests is not None:
                r = requests.get(url, headers=headers, timeout=30)
                r.raise_for_status()
                return pd.read_html(io.StringIO(r.text))
            return pd.read_html(url)  # requests無ければ直叩き
        except Exception as e:
            if attempt == 1:
                print(f"[globe] wiki取得失敗 {url}: {e}", file=sys.stderr)
    return []


def fetch_universe() -> list[tuple[str, str, str]]:
    """S&P500＋NASDAQ100をWikipedia(UA付き)から取得。失敗時は内蔵フォールバック。
    戻り値: [(ticker, name, sector), ...]（重複除去）。"""
    rows: dict[str, tuple[str, str, str]] = {}
    # S&P 500
    for df in _read_wiki_tables("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"):
        if "Symbol" in df.columns and "Security" in df.columns:
            for _, r in df.iterrows():
                tk = _norm_ticker(str(r.get("Symbol", "")))
                nm = str(r.get("Security", tk)).strip()
                sec = str(r.get("GICS Sector", "")).strip()
                if tk:
                    rows[tk] = (tk, nm, sec)
            break
    # NASDAQ-100
    for t in _read_wiki_tables("https://en.wikipedia.org/wiki/Nasdaq-100"):
        cols = [str(c) for c in t.columns]
        if any("Ticker" in c or "Symbol" in c for c in cols) and any("Company" in c or "Name" in c for c in cols):
            tcol = "Ticker" if "Ticker" in t.columns else ("Symbol" if "Symbol" in t.columns else t.columns[0])
            ncol = "Company" if "Company" in t.columns else ("Name" if "Name" in t.columns else t.columns[1])
            for _, r in t.iterrows():
                tk = _norm_ticker(str(r.get(tcol, "")))
                nm = str(r.get(ncol, tk)).strip()
                if tk and tk not in rows:
                    rows[tk] = (tk, nm, "")
            break

    n_wiki = len(rows)
    used_fallback = False
    if n_wiki < 100:
        used_fallback = True
        for tk, nm, sec in FALLBACK_UNIVERSE:
            rows.setdefault(tk, (tk, nm, sec))
    UNIVERSE_REDUCED["reduced"] = used_fallback
    print(f"universe: wikipedia={n_wiki} fallback={used_fallback} total={len(rows)}", file=sys.stderr)
    return list(rows.values())


# ─────────────────────────────────────────────
#  市場カレンダー（US Eastern基準・DSTはzoneinfoが処理）
# ─────────────────────────────────────────────
US_HOLIDAYS = {
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
US_HALF_DAYS = {  # 13:00 ET 早引け
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26), date(2027, 12, 24),
}


def _et_now() -> datetime:
    return datetime.now(tz=ET)


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def _session(d: date):
    half = d in US_HALF_DAYS
    o = datetime.combine(d, dtime(9, 30), tzinfo=ET)
    c = datetime.combine(d, dtime(13, 0) if half else dtime(16, 0), tzinfo=ET)
    return o, c, half


def market_window() -> dict:
    """現在のETを基準に「アクティブな窓（開場前/開場中）」または閉場後は
    「次営業日の窓」を返す。market_openは now が窓内かで判定。半日は13:00 ET終了。"""
    now_et = _et_now()
    today = now_et.date()
    use_day = None
    if _is_trading_day(today):
        _o, _c, _ = _session(today)
        if now_et < _c:          # 当日の引け前（＝開場前 or 開場中）
            use_day = today
    if use_day is None:          # 引け後 or 非営業日 → 次営業日
        d = today
        while True:
            d = d + timedelta(days=1)
            if _is_trading_day(d):
                use_day = d
                break
    o, c, half = _session(use_day)
    now_ms = int(now_et.timestamp() * 1000)
    open_ms = int(o.timestamp() * 1000)
    close_ms = int(c.timestamp() * 1000)
    market_open = open_ms <= now_ms < close_ms
    o_jst, c_jst = o.astimezone(JST), c.astimezone(JST)
    return {
        "market_open": market_open,
        "half_day": half,
        "open_ms": open_ms,
        "close_ms": close_ms,
        "next_open_jst": o_jst.strftime("%m/%d %H:%M"),
        "next_close_jst": c_jst.strftime("%H:%M"),
        "asof_jst": now_et.astimezone(JST).strftime("%Y-%m-%d %H:%M"),
    }


# ─────────────────────────────────────────────
#  テクニカル指標（EMA/RSI/MACD/BB/ADX）
# ─────────────────────────────────────────────
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0.0).ewm(alpha=1.0 / n, adjust=False).mean()
    dn = (-d.clip(upper=0.0)).ewm(alpha=1.0 / n, adjust=False).mean()
    rs = up / dn.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _macd(s: pd.Series):
    macd = _ema(s, 12) - _ema(s, 26)
    sig = _ema(macd, 9)
    return macd, sig, macd - sig


def _bollinger(s: pd.Series, n: int = 20, k: float = 2.0):
    ma = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    return ma, ma + k * sd, ma - k * sd


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([(high - low),
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / n, adjust=False).mean().replace(0.0, np.nan)
    plus_di = 100.0 * pd.Series(plus_dm, index=high.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
    minus_di = 100.0 * pd.Series(minus_dm, index=high.index).ewm(alpha=1.0 / n, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    return dx.ewm(alpha=1.0 / n, adjust=False).mean().fillna(0.0)


def _atr(high, low, close, n: int = 14) -> pd.Series:
    tr = pd.concat([(high - low),
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


class Analysis:
    __slots__ = ("code", "name", "sector", "price", "sc", "g", "reasons",
                 "tgt", "stp", "rr", "ez", "fund", "bt")

    def __init__(self, code, name, sector, price):
        self.code = code
        self.name = name
        self.sector = sector
        self.price = price
        self.sc = 0
        self.g = "HOLD"
        self.reasons: list[str] = []
        self.tgt = None
        self.stp = None
        self.rr = None
        self.ez = None
        self.fund = None
        self.bt = None


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def technical_score(df: pd.DataFrame) -> tuple[int, list[str], dict]:
    """日足OHLCから複合スコア(-100..100)と理由・水準を算出。"""
    close = df["Close"].dropna()
    if len(close) < 60:
        return 0, [], {}
    high, low = df["High"], df["Low"]
    price = float(close.iloc[-1])

    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200) if len(close) >= 200 else _ema(close, min(len(close), 120))
    rsi = _rsi(close, 14)
    macd, sig, hist = _macd(close)
    bmid, bup, blo = _bollinger(close, 20, 2.0)
    adx = _adx(high, low, close, 14)
    atr = _atr(high, low, close, 14)

    reasons: list[str] = []
    score = 0.0

    # 1) トレンド（EMA配列）: 最大 ±34
    e20, e50, e200 = float(ema20.iloc[-1]), float(ema50.iloc[-1]), float(ema200.iloc[-1])
    if price > e20 > e50 > e200:
        score += 34; reasons.append("パーフェクトオーダー(強気)")
    elif price > e50 > e200:
        score += 20; reasons.append("中長期トレンド上向き")
    elif price < e20 < e50 < e200:
        score -= 34; reasons.append("下降トレンド継続")
    elif price < e50:
        score -= 14; reasons.append("50日線を下回る")
    else:
        score += 4

    # 2) MACD: 最大 ±20
    m, s_, h = float(macd.iloc[-1]), float(sig.iloc[-1]), float(hist.iloc[-1])
    hp = float(hist.iloc[-2]) if len(hist) >= 2 else h
    if m > s_ and h > 0 and h >= hp:
        score += 20; reasons.append("MACD拡大(買い)")
    elif m > s_:
        score += 10; reasons.append("MACD上向き")
    elif m < s_ and h < 0 and h <= hp:
        score -= 20; reasons.append("MACD下向き(売り)")
    else:
        score -= 6

    # 3) RSI: 最大 ±18（逆張り成分）
    r = float(rsi.iloc[-1])
    if r <= 30:
        score += 18; reasons.append(f"RSI {r:.0f} 売られすぎ")
    elif r <= 45:
        score += 8
    elif r >= 70:
        score -= 18; reasons.append(f"RSI {r:.0f} 買われすぎ")
    elif r >= 60:
        score -= 6
    else:
        score += 2

    # 4) ボリンジャー位置: 最大 ±14
    bl, bu = float(blo.iloc[-1]), float(bup.iloc[-1])
    if price <= bl:
        score += 14; reasons.append("BB下限タッチ(反発期待)")
    elif price >= bu:
        score -= 14; reasons.append("BB上限(過熱)")

    # 5) ADX（トレンド強度でスコアを増幅）: 最大 ±14
    a = float(adx.iloc[-1])
    if a >= 25:
        amp = 14 if score >= 0 else -14
        score += amp
        reasons.append(f"ADX {a:.0f} 強トレンド")
    elif a < 15:
        score *= 0.85  # 方向感薄→減衰

    sc = int(_clip(round(score), -100, 100))

    # 水準（ATRベース）: 利確 +2ATR / 損切 -1.5ATR
    a1 = float(atr.iloc[-1]) if not math.isnan(float(atr.iloc[-1])) else price * 0.02
    tgt = round(price + 2.0 * a1, 2)
    stp = round(price - 1.5 * a1, 2)
    rr = round((tgt - price) / max(1e-9, (price - stp)), 1)
    # 狙い目（押し目）指値: 直近安値圏 or -1ATR
    dip = round(price - 1.0 * a1, 2)
    gap = round((price - dip) / price * 100.0, 0)
    levels = {"tgt": tgt, "stp": stp, "rr": rr,
              "ez": {"dip": dip, "hi": round(price, 2), "gap": gap}}
    return sc, reasons[:4], levels


def signal_of(sc: int) -> str:
    if sc >= 35:
        return "BUY"
    if sc <= -30:
        return "SELL"
    return "HOLD"


def barrier_stats(df: pd.DataFrame, price: float, tgt: float, stp: float) -> dict | None:
    """過去1年日足で、各日を起点にTP/SL到達を先に迎えた割合（バリア法勝率）。"""
    try:
        close = df["Close"].dropna()
        high, low = df["High"], df["Low"]
        if len(close) < 60 or price <= 0:
            return None
        tp_pct = (tgt - price) / price
        sl_pct = (stp - price) / price  # 負
        wins = losses = neither = 0
        days_to_win = []
        n = len(close)
        horizon = 20  # 20営業日以内
        vals = close.values
        hv, lv = high.values, low.values
        for i in range(max(0, n - 252), n - 1):
            entry = vals[i]
            tp = entry * (1 + tp_pct)
            sl = entry * (1 + sl_pct)
            hit = None
            for j in range(i + 1, min(n, i + 1 + horizon)):
                if lv[j] <= sl:
                    hit = "L"; break
                if hv[j] >= tp:
                    hit = "W"; days_to_win.append(j - i); break
            if hit == "W":
                wins += 1
            elif hit == "L":
                losses += 1
            else:
                neither += 1
        total = wins + losses + neither
        if total == 0:
            return None
        win_rate = round(wins / total * 100, 1)
        avg_days = round(float(np.mean(days_to_win)), 1) if days_to_win else None
        return {"win_rate": win_rate, "loss_rate": round(losses / total * 100, 1),
                "n": total, "avg_days": avg_days}
    except Exception as e:
        print(f"[globe] barrier_stats失敗: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────
#  データ取得（一括バッチ）＋ファンダ（上位のみ）
# ─────────────────────────────────────────────
def _download_batch(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    import yfinance as yf
    out: dict[str, pd.DataFrame] = {}
    CHUNK = 100
    for i in range(0, len(tickers), CHUNK):
        part = tickers[i:i + CHUNK]
        for attempt in range(2):
            try:
                data = yf.download(part, period=period, interval="1d",
                                   auto_adjust=True, threads=True, group_by="ticker",
                                   progress=False)
                if data is None or len(data) == 0:
                    raise RuntimeError("empty")
                for t in part:
                    try:
                        if len(part) == 1:
                            df = data
                        else:
                            df = data[t] if t in data.columns.get_level_values(0) else None
                        if df is None or df.dropna(how="all").empty:
                            continue
                        df = df.rename(columns=str.title)
                        need = {"Open", "High", "Low", "Close"}
                        if not need.issubset(set(df.columns)):
                            continue
                        out[t] = df.dropna(how="all")
                    except Exception:
                        continue
                break
            except Exception as e:
                if attempt == 1:
                    print(f"[globe] chunk {i//CHUNK} 取得失敗: {e}", file=sys.stderr)
    return out


def _fetch_fundamentals(codes: list[str]) -> dict[str, dict]:
    """上位＋ウォッチ銘柄のみ Ticker.info を叩く（全銘柄は禁止）。"""
    import yfinance as yf
    res: dict[str, dict] = {}
    for c in codes:
        try:
            info = yf.Ticker(c).info or {}
            res[c] = {
                "per": info.get("trailingPE"),
                "pbr": info.get("priceToBook"),
                "roe": info.get("returnOnEquity"),
                "div": info.get("dividendYield"),
                "target_mean": info.get("targetMeanPrice"),
                "reco": info.get("recommendationKey"),
                "eps": info.get("trailingEps"),
                "book": info.get("bookValue"),
                "sector": info.get("sector"),
                "name": info.get("shortName") or info.get("longName"),
            }
        except Exception as e:
            print(f"[globe] info失敗 {c}: {e}", file=sys.stderr)
            res[c] = {}
    return res


def _fund_adjust(a: Analysis, f: dict) -> None:
    """ファンダでスコアを微調整＋アナリスト乖離を格納。"""
    if not f:
        return
    adj = 0
    per, pbr, roe, div = f.get("per"), f.get("pbr"), f.get("roe"), f.get("div")
    try:
        if per and 0 < per <= 15:
            adj += 4
        elif per and per >= 40:
            adj -= 3
        if pbr and 0 < pbr <= 1.5:
            adj += 3
        if roe and roe >= 0.15:
            adj += 3
        if div and div >= 0.03:
            adj += 2
    except Exception:
        pass
    a.sc = int(_clip(a.sc + adj, -100, 100))
    a.g = signal_of(a.sc)
    tm = f.get("target_mean")
    tpct = None
    try:
        if tm and a.price:
            tpct = round((tm - a.price) / a.price * 100.0, 1)
    except Exception:
        tpct = None
    # 理論株価（3法ブレンド: PER18 / PBR2.5 / アナリスト目標 の中央値）→ 割安/割高判定
    fair = fair_gap = valuation = None
    cands = []
    try:
        eps, book = f.get("eps"), f.get("book")
        if eps and eps > 0:
            cands.append(eps * 18.0)
        if book and book > 0:
            cands.append(book * 2.5)
        if tm and tm > 0:
            cands.append(float(tm))
        if cands and a.price:
            fair = round(float(np.median(cands)), 2)
            fair_gap = round((fair - a.price) / a.price * 100.0, 1)
            valuation = "割安" if fair_gap >= 10 else ("割高" if fair_gap <= -10 else "適正")
    except Exception:
        pass
    a.fund = {"per": per, "pbr": pbr, "roe": roe, "div": div,
              "target_mean": tm, "target_pct": tpct, "reco": f.get("reco"),
              "fair": fair, "fair_gap": fair_gap, "valuation": valuation}


def analyze_all() -> tuple[list[Analysis], dict, list[dict]]:
    holdings = load_holdings()
    hold_codes = [h["code"] for h in holdings]
    uni = fetch_universe()
    # 保有銘柄をユニバースに必ず合流（S&P500/NASDAQ100外でも表示・検索可能に）
    have = {c for c, _, _ in uni}
    for hc in hold_codes:
        if hc not in have:
            uni.append((hc, hc, ""))
            have.add(hc)
    code_meta = {c: (n, s) for c, n, s in uni}
    codes = [c for c, _, _ in uni]
    print(f"[globe] ユニバース {len(codes)} 銘柄（保有{len(hold_codes)}含む）を取得中…", file=sys.stderr)
    frames = _download_batch(codes, "1y")
    # 取得漏れの保有銘柄は個別リトライ
    for hc in hold_codes:
        if hc not in frames:
            one = _download_batch([hc], "1y")
            frames.update(one)
    print(f"[globe] 取得成功 {len(frames)} 銘柄", file=sys.stderr)

    analyses: list[Analysis] = []
    for c in codes:
        df = frames.get(c)
        if df is None or df["Close"].dropna().shape[0] < 60:
            continue
        try:
            price = float(df["Close"].dropna().iloc[-1])
            sc, reasons, lv = technical_score(df)
            name, sector = code_meta.get(c, (c, ""))
            a = Analysis(c, name, sector, round(price, 2))
            a.sc = sc
            a.g = signal_of(sc)
            a.reasons = reasons
            if lv:
                a.tgt, a.stp, a.rr, a.ez = lv["tgt"], lv["stp"], lv["rr"], lv["ez"]
            analyses.append(a)
        except Exception as e:
            print(f"[globe] 分析失敗 {c}: {e}", file=sys.stderr)
            continue

    analyses.sort(key=lambda x: x.sc, reverse=True)

    # 上位30＋ウォッチ＋保有のみ info 取得
    top_codes = [a.code for a in analyses[:30]]
    fund_codes = list(dict.fromkeys(top_codes + WATCH_FALLBACK + hold_codes))
    funds = _fetch_fundamentals(fund_codes)
    bt_targets = set(top_codes) | set(hold_codes)
    for a in analyses:
        if a.code in funds:
            _fund_adjust(a, funds[a.code])
            # 保有銘柄は取得した正式名で上書き
            if not a.name or a.name == a.code:
                nm = funds[a.code].get("name")
                if nm:
                    a.name = nm
            if a.code in bt_targets and a.tgt and a.stp and a.code in frames:
                a.bt = barrier_stats(frames[a.code], a.price, a.tgt, a.stp)

    analyses.sort(key=lambda x: x.sc, reverse=True)
    meta = market_window()
    return analyses, meta, holdings


# ─────────────────────────────────────────────
#  表示ヘルパ-＆サーバーカード
# ─────────────────────────────────────────────
def _esc(s) -> str:
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _search_key(name: str, code: str) -> str:
    return f"{name} {code}".lower()


def _sector_short(sec: str) -> str:
    m = {
        "Information Technology": "Tech", "Technology": "Tech",
        "Communication Services": "Comm", "Communication": "Comm",
        "Consumer Discretionary": "Cons.D", "Consumer Staples": "Cons.S",
        "Health Care": "Health", "Financials": "Fin", "Industrials": "Indust",
        "Energy": "Energy", "Materials": "Matr", "Utilities": "Util",
        "Real Estate": "REIT",
    }
    return m.get(sec, (sec[:5] if sec else ""))


def _usd(v) -> str:
    try:
        return "${:,.2f}".format(float(v))
    except Exception:
        return "$-"


def _badge(g: str) -> str:
    x = {"BUY": ("買", "buy"), "SELL": ("売", "sell"), "HOLD": ("待", "hold")}.get(g, ("待", "hold"))
    return f'<span class="badge {x[1]}">{x[0]}</span>'


def _score_bar(sc: int) -> str:
    p = max(-100, min(100, sc)) / 100.0
    if p >= 0:
        return f'<span class="bar"><span class="bar-pos" style="width:{p*50:.0f}%"></span></span>'
    return (f'<span class="bar"><span class="bar-neg" style="width:{abs(p)*50:.0f}%;'
            f'margin-left:{50-abs(p)*50:.0f}%"></span></span>')


def _card(rank: int, a: Analysis, show_levels: bool) -> str:
    scls = "pos" if a.sc >= 0 else "neg"
    seg = _sector_short(a.sector)
    seg_html = f'<span class="seg">{_esc(seg)}</span>' if seg else ""
    levels = ""
    if show_levels and a.tgt and a.stp:
        rr = f'<span class="lv rr">RR {a.rr}</span>' if a.rr else ""
        levels = (f'<div class="levels"><span class="lv tgt">利確 {_usd(a.tgt)}</span>'
                  f'<span class="lv stp">損切 {_usd(a.stp)}</span>{rr}</div>')
    # アナリストコンセンサス
    an = ""
    if a.fund and a.fund.get("target_pct") is not None:
        tp = a.fund["target_pct"]
        cls = "up" if tp >= 0 else "dn"
        an = (f'<div class="analyst {cls}">プロ予想 {"+" if tp>=0 else ""}{tp:.0f}%'
              f'（目標 {_usd(a.fund.get("target_mean"))}）</div>')
    # 理論株価（割安/割高）
    fair = ""
    if a.fund and a.fund.get("fair"):
        val = a.fund["valuation"]
        gap = a.fund["fair_gap"]
        vcls = {"割安": "up", "割高": "dn", "適正": "hold"}.get(val, "hold")
        fair = (f'<div class="fair {vcls}">理論株価 {_usd(a.fund["fair"])} → '
                f'<b>{val}</b>（{"+" if gap>=0 else ""}{gap:.0f}%）</div>')
    # ファンダ
    fund = ""
    if a.fund:
        chips = []
        if a.fund.get("per"):
            chips.append(f'PER {a.fund["per"]:.0f}')
        if a.fund.get("roe"):
            chips.append(f'ROE {a.fund["roe"]*100:.0f}%')
        if a.fund.get("div"):
            chips.append(f'配当 {a.fund["div"]*100:.1f}%')
        if chips:
            fund = '<div class="fund">' + "".join(f'<span class="fchip">{c}</span>' for c in chips) + "</div>"
    # 狙い目ゾーン（ライブ再計算）
    ez_html = ""
    if a.ez:
        gap = a.ez["gap"]
        inner = (f'🎯 狙い目 指値 {_usd(a.ez["dip"])} 〜 現値 {_usd(a.ez["hi"])}'
                 f'<span class="ezn">-{gap:.0f}% の押し目</span>')
        ez_html = (f'<div class="ez" data-ez-c="{_esc(a.code)}" data-ez-limit="{a.ez["dip"]}" '
                   f'data-ez-pct="{gap:.0f}">{inner}</div>')
    # バリア法勝率
    bt = ""
    if a.bt:
        avg = f'・想定{a.bt["avg_days"]}日' if a.bt.get("avg_days") else ""
        bt = (f'<div class="bt"><span class="btchip win">利確勝率 {a.bt["win_rate"]}%</span>'
              f'<span class="btchip">母数 {a.bt["n"]}{avg}</span></div>')
    reasons = ""
    if a.reasons:
        reasons = '<div class="reasons">' + "".join(
            f'<span class="chip">{_esc(r)}</span>' for r in a.reasons) + "</div>"
    return (
        f'<div class="card">'
        f'<div class="row1"><span class="rank">{rank}</span>'
        f'<div class="title"><span class="code">{_esc(a.code)}</span>'
        f'<span class="name">{_esc(a.name)}</span>{seg_html}</div>{_badge(a.g)}</div>'
        f'<div class="row2"><span class="price" data-px="{_esc(a.code)}" data-usd="{a.price}">{_usd(a.price)}</span>'
        f'<span class="score {scls}">{"+" if a.sc>=0 else ""}{a.sc}</span>{_score_bar(a.sc)}</div>'
        f'{levels}{an}{fair}{ez_html}{fund}{bt}{reasons}</div>'
    )


def _section(title: str, sub: str, cards_html: str) -> str:
    return (f'<section class="sec"><h2 class="find"><span>{title}</span><em>{sub}</em></h2>'
            f'<div class="cards">{cards_html}</div></section>')


CSS_STR = r"""
:root{--bg:#0a0f1e;--bg2:#0f1730;--card:#111c38;--line:rgba(255,255,255,.08);
--fg:#eaf0ff;--mut:#8ea3c8;--gold:#e8c96a;--gold2:#caa64c;
--buy:#46c46a;--sell:#f0616d;--hold:#8ea3c8;--up:#46c46a;--dn:#f0616d}
*{box-sizing:border-box;margin:0;padding:0}
body{background:radial-gradient(ellipse at 20% 0%,#132043,transparent 60%),var(--bg);
color:var(--fg);font-family:-apple-system,"Hiragino Kaku Gothic ProN",sans-serif;
line-height:1.5;padding-bottom:64px;-webkit-text-size-adjust:100%}
.wrap{max-width:620px;margin:0 auto;padding:0 14px}
header{padding:22px 0 10px;text-align:center}
.brand{font-family:"Times New Roman",serif;font-size:26px;font-weight:800;letter-spacing:.04em;
background:linear-gradient(135deg,var(--gold),#fff5d6,var(--gold2));-webkit-background-clip:text;
-webkit-text-fill-color:transparent}
.brand small{display:block;font-size:11px;letter-spacing:.32em;color:var(--gold2);
-webkit-text-fill-color:var(--gold2);margin-top:4px}
.meta{display:flex;flex-wrap:wrap;gap:6px 10px;justify-content:center;align-items:center;
margin-top:10px;font-size:12px;color:var(--mut)}
.mkt{padding:3px 12px;border-radius:20px;border:1px solid var(--line);font-weight:700}
.mkt.open{color:var(--up);border-color:rgba(70,196,106,.4);background:rgba(70,196,106,.08)}
.mkt.closed{color:var(--mut)}
.refresh,.jpy{background:rgba(255,255,255,.06);border:1px solid var(--line);color:var(--fg);
font-size:12px;padding:4px 12px;border-radius:20px;cursor:pointer}
.jpy.on{background:rgba(232,201,106,.16);border-color:rgba(232,201,106,.5);color:var(--gold)}
.idx{display:flex;gap:8px;overflow-x:auto;padding:10px 0;margin-top:4px}
.idx .i{flex:0 0 auto;background:var(--bg2);border:1px solid var(--line);border-radius:12px;
padding:8px 12px;min-width:120px}
.idx .i .n{font-size:11px;color:var(--mut)}
.idx .i .v{font-size:15px;font-weight:800}
.idx .i .c.up{color:var(--up)}.idx .i .c.dn{color:var(--dn)}.idx .i .c{font-size:12px;font-weight:700}
.searchbar{position:relative;margin:14px 0 6px}
#q{width:100%;padding:13px 14px;background:var(--bg2);border:1px solid var(--line);
border-radius:14px;color:var(--fg);font-size:15px;outline:none}
#q:focus{border-color:rgba(232,201,106,.5)}
#hint{font-size:12px;color:var(--mut);margin:2px 2px 0}
#hitcount{font-size:12px;color:var(--gold2);margin:8px 2px 0}
h2.find{display:flex;align-items:baseline;gap:10px;margin:20px 2px 10px}
h2.find span{font-size:16px;font-weight:800}
h2.find em{font-style:normal;font-size:10px;letter-spacing:.24em;color:var(--gold2)}
.cards{display:flex;flex-direction:column;gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:13px 14px}
.row1{display:flex;align-items:center;gap:8px}
.rank{font-family:"Times New Roman",serif;font-weight:800;color:var(--gold);min-width:20px}
.title{flex:1;min-width:0}
.code{font-weight:800;font-size:15px}.name{color:var(--mut);font-size:12px;margin-left:6px}
.seg{font-size:10px;color:var(--gold2);border:1px solid rgba(232,201,106,.35);
border-radius:8px;padding:1px 6px;margin-left:6px}
.badge{font-size:12px;font-weight:800;padding:2px 10px;border-radius:10px}
.badge.buy{background:rgba(70,196,106,.18);color:var(--buy)}
.badge.sell{background:rgba(240,97,109,.18);color:var(--sell)}
.badge.hold{background:rgba(142,163,200,.16);color:var(--hold)}
.star,.rm{background:none;border:none;color:var(--gold);font-size:18px;cursor:pointer;padding:0 2px}
.rm{color:var(--sell);font-size:16px}
.row2{display:flex;align-items:center;gap:10px;margin-top:8px}
.price{font-size:18px;font-weight:800}
.score{font-size:15px;font-weight:800}.score.pos{color:var(--up)}.score.neg{color:var(--dn)}
.bar{flex:1;height:6px;background:rgba(255,255,255,.07);border-radius:6px;position:relative;overflow:hidden}
.bar-pos{position:absolute;left:50%;top:0;bottom:0;background:var(--up)}
.bar-neg{position:absolute;top:0;bottom:0;background:var(--dn)}
.levels{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px;font-size:12px}
.lv{padding:2px 9px;border-radius:8px;border:1px solid var(--line)}
.lv.tgt{color:var(--up)}.lv.stp{color:var(--dn)}.lv.rr{color:var(--gold)}
.analyst{margin-top:8px;font-size:12px;font-weight:800}
.analyst.up{color:var(--up)}.analyst.dn{color:var(--dn)}
.fair{margin-top:6px;font-size:12px;font-weight:700;color:var(--mut)}
.fair.up b{color:var(--up)}.fair.dn b{color:var(--dn)}.fair.hold b{color:var(--gold)}
.pl{margin-top:8px;font-size:13px;font-weight:800}
.pl.up{color:var(--up)}.pl.dn{color:var(--dn)}
.hnote{font-size:11px;color:var(--mut);margin-left:8px;font-weight:600}
.ez{margin-top:8px;font-size:12px;font-weight:800;color:var(--fg);
background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:10px;padding:7px 10px}
.ez.hit{background:rgba(70,196,106,.16);border-color:rgba(70,196,106,.55)}
.ez.hit b{color:var(--buy)}
.ezn{margin-left:8px;font-size:11px;font-weight:700;color:var(--mut)}
.fund,.bt,.reasons{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.fchip,.btchip,.chip{font-size:11px;padding:2px 8px;border-radius:8px;border:1px solid var(--line);color:var(--mut)}
.btchip.win{color:var(--up);border-color:rgba(70,196,106,.4)}
.chip{color:var(--gold2);border-color:rgba(232,201,106,.28)}
.empty{color:var(--mut);font-size:13px;padding:14px;text-align:center}
footer{margin-top:26px;padding:16px 0;text-align:center;font-size:11px;color:var(--mut);line-height:1.7}
"""


APP_JS = r"""
/* GLOBE ORACLE app.js — 検索/ウォッチ/ライブ価格/市場時間(埋め込み窓)/USD⇄JPY
   素のJS(ES2018)・iOS Safari動作。DST計算はサーバー埋め込み窓を読むだけ。 */
(function () {
  'use strict';
  var B = document.body;
  var TOTAL = parseInt((B && B.getAttribute('data-total')) || '0', 10) || 0;
  var WATCH_KEY = 'globe_watch', JPY_KEY = 'globe_jpy';
  var STOCKS = null, loading = false, loadTries = 0;
  var RATE = parseFloat((B && B.getAttribute('data-usdjpy')) || '0') || 0;
  var MKT = {
    open_ms: parseFloat((B && B.getAttribute('data-open-ms')) || '0') || 0,
    close_ms: parseFloat((B && B.getAttribute('data-close-ms')) || '0') || 0,
    market_open: (B && B.getAttribute('data-mopen')) === '1',
    next_open: (B && B.getAttribute('data-nopen')) || '',
    next_close: (B && B.getAttribute('data-nclose')) || ''
  };

  var q = document.getElementById('q');
  var results = document.getElementById('results');
  var hint = document.getElementById('hint');
  var searchSec = document.getElementById('search-sec');

  function jpyOn() { try { return localStorage.getItem(JPY_KEY) === '1'; } catch (e) { return false; } }
  function fmtMoney(usd) {
    var v = Number(usd);
    if (jpyOn() && RATE > 0) return '¥' + Math.round(v * RATE).toLocaleString();
    return '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function norm(s) {
    s = (s == null ? '' : String(s));
    try { s = s.normalize('NFKC'); } catch (e) {}
    return s.toLowerCase();
  }

  /* ---- ウォッチリスト ---- */
  function getWatch() { try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '[]') || []; } catch (e) { return []; } }
  function setWatch(a) { try { localStorage.setItem(WATCH_KEY, JSON.stringify(a)); } catch (e) {} }
  function inWatch(c) { return getWatch().indexOf(c) >= 0; }
  function toggleWatch(c) { var w = getWatch(), i = w.indexOf(c); if (i >= 0) w.splice(i, 1); else w.push(c); setWatch(w); }

  function badge(g) { var m = { BUY: ['買', 'buy'], SELL: ['売', 'sell'], HOLD: ['待', 'hold'] }; var x = m[g] || m.HOLD; return '<span class="badge ' + x[1] + '">' + x[0] + '</span>'; }
  function bar(sc) { var p = Math.max(-100, Math.min(100, sc)) / 100; if (p >= 0) return '<span class="bar"><span class="bar-pos" style="width:' + (p * 50) + '%"></span></span>'; return '<span class="bar"><span class="bar-neg" style="width:' + (Math.abs(p) * 50) + '%;margin-left:' + (50 - Math.abs(p) * 50) + '%"></span></span>'; }
  function starBtn(c) { var on = inWatch(c); return '<button class="star' + (on ? ' on' : '') + '" data-star="' + c + '">' + (on ? '★' : '☆') + '</button>'; }

  function card(s, mode) {
    var scls = s.sc >= 0 ? 'pos' : 'neg';
    var seg = s.m ? '<span class="seg">' + s.m + '</span>' : '';
    var levels = '';
    if (s.t && s.st) {
      levels = '<div class="levels"><span class="lv tgt">利確 ' + fmtMoney(s.t) + '</span>' +
        '<span class="lv stp">損切 ' + fmtMoney(s.st) + '</span>' +
        (s.rr ? '<span class="lv rr">RR ' + s.rr + '</span>' : '') + '</div>';
    }
    var an = (s.tp != null) ? '<div class="analyst ' + (s.tp >= 0 ? 'up' : 'dn') + '">プロ予想 ' + (s.tp >= 0 ? '+' : '') + s.tp + '%</div>' : '';
    var fair = (s.val) ? '<div class="fair ' + (s.val === '割安' ? 'up' : s.val === '割高' ? 'dn' : 'hold') + '">理論株価 <b>' + s.val + '</b>（' + (s.fg >= 0 ? '+' : '') + s.fg + '%）</div>' : '';
    var reasons = (s.r && s.r.length) ? '<div class="reasons">' + s.r.map(function (r) { return '<span class="chip">' + r + '</span>'; }).join('') + '</div>' : '';
    var rm = (mode === 'watch') ? '<button class="rm" data-rm="' + s.c + '">×</button>' : '';
    return '<div class="card"><div class="row1"><span class="rank">' + (s.rk || '-') + '</span>' +
      '<div class="title"><span class="code">' + s.c + '</span><span class="name">' + s.n + '</span>' + seg + '</div>' +
      badge(s.g) + starBtn(s.c) + rm + '</div>' +
      '<div class="row2"><span class="price" data-px="' + s.c + '" data-usd="' + s.p + '">' + fmtMoney(s.p) + '</span>' +
      '<span class="score ' + scls + '">' + (s.sc >= 0 ? '+' : '') + s.sc + '</span>' + bar(s.sc) + '</div>' +
      levels + an + fair + reasons +
      '<div class="reasons"><span class="chip">スコア順 ' + (s.rk || '-') + ' 位 / ' + TOTAL + ' 銘柄</span></div></div>';
  }
  function byCode(c) { if (!STOCKS) return null; var v = String(c).toLowerCase(); for (var i = 0; i < STOCKS.length; i++) { if (STOCKS[i].c.toLowerCase() === v) return STOCKS[i]; } return null; }

  function ensureStocks(cb) {
    if (STOCKS) { if (cb) cb(); return; }
    if (loading) return; loading = true;
    (function attempt() {
      fetch('stocks.json?t=' + Date.now())
        .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
        .then(function (j) { STOCKS = j.stocks || j; loading = false; loadTries = 0; if (cb) cb(); renderWatch(); if (q && q.value.trim()) run(); })
        .catch(function (e) {
          loadTries++; console.warn('[globe] stocks.json 読込失敗(' + loadTries + ')', e);
          if (loadTries < 3) setTimeout(attempt, 3000);
          else { loading = false; if (results && q && q.value.trim()) results.innerHTML = '<p class="empty">銘柄データの読込に失敗しました。通信を確認して再度お試しください。</p>'; }
        });
    })();
  }

  function ensureHitEl() { var el = document.getElementById('hitcount'); if (!el && results && results.parentNode) { el = document.createElement('p'); el.id = 'hitcount'; results.parentNode.insertBefore(el, results); } return el; }
  function run() {
    if (!q || !results) return;
    var raw = q.value.trim(), hitEl = ensureHitEl();
    if (!raw) { results.innerHTML = ''; if (hint) hint.style.display = ''; if (hitEl) hitEl.textContent = ''; return; }
    if (hint) hint.style.display = 'none';
    if (!STOCKS) { if (hitEl) hitEl.textContent = ''; results.innerHTML = '<p class="empty">銘柄データを読込中…</p>'; ensureStocks(); return; }
    var v = norm(raw), code = raw.toLowerCase();
    var m = STOCKS.filter(function (s) { return (s.k && s.k.indexOf(v) >= 0) || s.c.toLowerCase().indexOf(code) === 0; }).sort(function (a, b) { return b.sc - a.sc; });
    var shown = m.slice(0, 8);
    if (hitEl) hitEl.textContent = m.length ? (m.length + '件ヒット / 上位' + shown.length + '件') : '';
    results.innerHTML = shown.length ? shown.map(function (s) { return card(s, 'search'); }).join('') : '<p class="empty">該当なし。社名(apple)やティッカー(AAPL)で検索してください。</p>';
  }
  var deb = null; function runDebounced() { if (deb) clearTimeout(deb); deb = setTimeout(run, 150); }
  if (q) { q.addEventListener('focus', function () { ensureStocks(); }); q.addEventListener('input', function () { ensureStocks(); runDebounced(); }); }

  var watchSec = null, watchResults = null;
  function ensureWatchSec() {
    if (watchSec) return;
    watchSec = document.createElement('section'); watchSec.id = 'watch-sec'; watchSec.style.display = 'none';
    watchSec.innerHTML = '<h2 class="find"><span>ウォッチリスト</span><em>WATCHLIST</em></h2><div id="watch-results" class="cards"></div>';
    if (searchSec && searchSec.parentNode) searchSec.parentNode.insertBefore(watchSec, searchSec.nextSibling);
    watchResults = watchSec.querySelector('#watch-results');
  }
  function renderWatch() {
    ensureWatchSec(); var w = getWatch();
    if (!w.length) { watchSec.style.display = 'none'; if (watchResults) watchResults.innerHTML = ''; return; }
    if (!STOCKS) { ensureStocks(); return; }
    var cards = []; for (var i = 0; i < w.length; i++) { var s = byCode(w[i]); if (s) cards.push(card(s, 'watch')); }
    watchSec.style.display = cards.length ? '' : 'none'; if (watchResults) watchResults.innerHTML = cards.join('');
  }
  function injectStars() {
    var cards = document.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      var cd = cards[i]; if (cd.querySelector('[data-star]')) continue;
      var px = cd.querySelector('[data-px]'); if (!px) continue;
      var c = px.getAttribute('data-px'); var r1 = cd.querySelector('.row1'); if (!r1) continue;
      var b = document.createElement('button'); b.className = 'star' + (inWatch(c) ? ' on' : ''); b.setAttribute('data-star', c); b.textContent = inWatch(c) ? '★' : '☆'; r1.appendChild(b);
    }
  }
  function syncStars() { var btns = document.querySelectorAll('[data-star]'); for (var i = 0; i < btns.length; i++) { var c = btns[i].getAttribute('data-star'), on = inWatch(c); btns[i].className = 'star' + (on ? ' on' : ''); btns[i].textContent = on ? '★' : '☆'; } }
  document.addEventListener('click', function (ev) {
    var t = ev.target; if (!t || !t.getAttribute) return;
    var sc = t.getAttribute('data-star'); if (sc) { toggleWatch(sc); syncStars(); renderWatch(); return; }
    var rm = t.getAttribute('data-rm'); if (rm) { toggleWatch(rm); syncStars(); renderWatch(); return; }
  });

  /* ---- USD⇄JPY トグル ---- */
  function reformatMoney() {
    document.querySelectorAll('[data-usd]').forEach(function (el) { el.textContent = fmtMoney(el.getAttribute('data-usd')); });
    if (q && q.value.trim()) run(); renderWatch();
  }
  function addJpyBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('jpybtn')) return;
    var b = document.createElement('button'); b.id = 'jpybtn'; b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.type = 'button';
    b.textContent = jpyOn() ? '¥ 円' : '$ ドル';
    b.addEventListener('click', function () { try { if (jpyOn()) localStorage.removeItem(JPY_KEY); else localStorage.setItem(JPY_KEY, '1'); } catch (e) {} b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.textContent = jpyOn() ? '¥ 円' : '$ ドル'; reformatMoney(); });
    meta.appendChild(b);
  }

  /* ---- ライブ価格 ---- */
  function applyPrices(map) {
    document.querySelectorAll('[data-px]').forEach(function (el) {
      var c = el.getAttribute('data-px'); if (map[c] != null) { el.setAttribute('data-usd', map[c]); el.textContent = fmtMoney(map[c]); }
    });
    document.querySelectorAll('.ez[data-ez-c]').forEach(function (el) {
      var c = el.getAttribute('data-ez-c'), limit = parseFloat(el.getAttribute('data-ez-limit'));
      if (map[c] == null || !limit) return; var pr = Number(map[c]);
      if (pr <= limit) { el.className = 'ez hit'; el.innerHTML = '🎯 狙い目 指値 ' + fmtMoney(limit) + ' <b>✅ 指値到達</b>（現値 ' + fmtMoney(pr) + '）'; }
      else { var pct = Math.round((pr - limit) / pr * 100); el.className = 'ez'; el.innerHTML = '🎯 狙い目 指値 ' + fmtMoney(limit) + ' 〜 現値 ' + fmtMoney(pr) + '<span class="ezn">-' + pct + '% の押し目</span>'; }
    });
    if (STOCKS) { for (var i = 0; i < STOCKS.length; i++) { if (map[STOCKS[i].c] != null) STOCKS[i].p = Number(map[STOCKS[i].c]); } }
  }
  function applyIndices(idx) {
    if (!idx) return;
    document.querySelectorAll('[data-idx]').forEach(function (el) {
      var k = el.getAttribute('data-idx'), d = idx[k]; if (!d) return;
      var v = el.querySelector('.v'), c = el.querySelector('.c');
      if (v) v.textContent = (k === 'USDJPY' ? '¥' : '') + Number(d.price).toLocaleString();
      if (c) { c.textContent = (d.chg >= 0 ? '+' : '') + d.chg + '%'; c.className = 'c ' + (d.chg >= 0 ? 'up' : 'dn'); }
    });
    if (idx.USDJPY && idx.USDJPY.price) RATE = Number(idx.USDJPY.price);
  }
  function setMktStatus() {
    var el = document.getElementById('mkt'); if (!el) return;
    var now = Date.now(), open = MKT.market_open && now >= MKT.open_ms && now < MKT.close_ms;
    if (open) { el.className = 'mkt open'; el.textContent = 'NY市場：開場中🟢'; }
    else { el.className = 'mkt closed'; el.textContent = 'NY市場：閉場⚫（次回 JST ' + MKT.next_open + '〜' + MKT.next_close + '）'; }
    return open;
  }
  function refreshPrices() {
    return fetch('prices.json?t=' + Date.now())
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        if (d.mkt) { MKT.open_ms = d.mkt.open_ms; MKT.close_ms = d.mkt.close_ms; MKT.market_open = d.mkt.market_open; MKT.next_open = d.mkt.next_open_jst || MKT.next_open; MKT.next_close = d.mkt.next_close_jst || MKT.next_close; }
        if (d.px) applyPrices(d.px);
        if (d.idx) applyIndices(d.idx);
        var lab = document.getElementById('pxasof'); if (lab && d.asof) lab.textContent = '株価 ' + d.asof + ' 時点（約15分遅延）';
        setMktStatus(); if (q && q.value.trim()) run(); renderWatch();
      })
      .catch(function (e) { console.warn('[globe] prices取得失敗', e); });
  }

  /* ---- 市場時間ポーリング：埋め込み窓(JST epoch)を使用 ---- */
  function tick() {
    var open = setMktStatus();
    if (open) { refreshPrices(); setTimeout(tick, 5 * 60 * 1000); }
    else {
      // 閉場中でも60分毎に更新（窓の再取得＝翌営業日窓へ自動追従・デッドロック回避）
      refreshPrices();
      var now = Date.now(), wait;
      if (MKT.open_ms && now < MKT.open_ms) wait = Math.min(MKT.open_ms - now, 60 * 60 * 1000);
      else wait = 60 * 60 * 1000;
      setTimeout(tick, Math.max(60000, wait));
    }
  }

  function addRefreshBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('pxrefresh')) return;
    var b = document.createElement('button'); b.id = 'pxrefresh'; b.className = 'refresh'; b.type = 'button'; b.textContent = '⟳ 更新';
    b.addEventListener('click', function () { b.disabled = true; refreshPrices().then(function () { setTimeout(function () { b.disabled = false; }, 1500); }); });
    meta.appendChild(b);
  }

  function init() {
    addRefreshBtn(); addJpyBtn(); injectStars(); setMktStatus();
    if (getWatch().length) ensureStocks(renderWatch); else ensureWatchSec();
    refreshPrices(); tick();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
"""


# ─────────────────────────────────────────────
#  USDJPY / 指数 / PWAアイコン
# ─────────────────────────────────────────────
def _fetch_quote(ticker: str) -> tuple[float | None, float | None]:
    """直近終値と前日比%を返す。"""
    import yfinance as yf
    try:
        df = yf.download(ticker, period="5d", interval="1d",
                         auto_adjust=True, progress=False, threads=False)
        if df is None or df.empty:
            return None, None
        cl = df["Close"].dropna()
        if isinstance(cl, pd.DataFrame):
            cl = cl.iloc[:, 0]
        if len(cl) < 1:
            return None, None
        price = float(cl.iloc[-1])
        chg = None
        if len(cl) >= 2:
            prev = float(cl.iloc[-2])
            if prev:
                chg = round((price - prev) / prev * 100.0, 2)
        return round(price, 2), chg
    except Exception as e:
        print(f"[globe] quote失敗 {ticker}: {e}", file=sys.stderr)
        return None, None


def _fetch_usdjpy() -> float:
    p, _ = _fetch_quote("JPY=X")
    return p or 0.0


def _gen_icons() -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        print(f"[globe] Pillow無しでアイコン生成skip: {e}", file=sys.stderr)
        return
    for size, path in [(192, "icon-192.png"), (512, "icon-512.png"), (180, "apple-touch-icon.png")]:
        p = DOCS / path
        img = Image.new("RGB", (size, size), (10, 15, 30))
        d = ImageDraw.Draw(img)
        cx = cy = size / 2
        r = size * 0.36
        # 地球儀（金の輪）
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(232, 201, 106), width=max(3, size // 40))
        d.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r], outline=(202, 166, 76), width=max(2, size // 60))
        d.line([cx - r, cy, cx + r, cy], fill=(202, 166, 76), width=max(2, size // 60))
        d.line([cx - r, cy - r * 0.5, cx + r, cy - r * 0.5], fill=(202, 166, 76), width=max(1, size // 90))
        d.line([cx - r, cy + r * 0.5, cx + r, cy + r * 0.5], fill=(202, 166, 76), width=max(1, size // 90))
        img.save(p)
    print("[globe] PWAアイコン生成完了", file=sys.stderr)


def _manifest() -> str:
    return json.dumps({
        "name": "GLOBE ORACLE — 米国株オラクル",
        "short_name": "GLOBE",
        "start_url": "./index.html",
        "display": "standalone",
        "background_color": "#0a0f1e",
        "theme_color": "#0a0f1e",
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
#  ダッシュボードHTML
# ─────────────────────────────────────────────
def _to_stock_json(a: Analysis, rank: int) -> dict:
    tp = a.fund.get("target_pct") if a.fund else None
    val = a.fund.get("valuation") if a.fund else None
    fg = a.fund.get("fair_gap") if a.fund else None
    return {
        "c": a.code, "n": a.name, "k": _search_key(a.name, a.code),
        "p": a.price, "sc": a.sc, "g": a.g, "m": _sector_short(a.sector),
        "t": a.tgt, "st": a.stp, "rr": a.rr, "r": a.reasons, "tp": tp,
        "val": val, "fg": fg, "rk": rank,
    }


def _holding_card(h: dict, amap: dict) -> str:
    code = h.get("code")
    a = amap.get(code)
    avg = h.get("avg", 0) or 0
    if not a:
        # データ取得不可でも保有として必ず表示（サイレント脱落させない）
        return (f'<div class="card"><div class="row1"><span class="rank">保有</span>'
                f'<div class="title"><span class="code">{_esc(code)}</span>'
                f'<span class="name">買値 {_usd(avg)}</span></div>'
                f'<span class="badge hold">?</span></div>'
                f'<div class="pl dn">⚠ データ取得不可（ティッカー確認 or 一時的な取得失敗）</div></div>')
    if h.get("tgt"):
        a.tgt = h["tgt"]
    if h.get("stp"):
        a.stp = h["stp"]
    pl_pct = round((a.price - avg) / avg * 100.0, 1) if avg else 0.0
    cls = "up" if pl_pct >= 0 else "dn"
    pl = (f'<div class="pl {cls}">損益 {"+" if pl_pct>=0 else ""}{pl_pct}%'
          f'<span class="hnote">買値 {_usd(avg)} → 現値 {_usd(a.price)}</span></div>')
    base = _card(0, a, True).replace('<span class="rank">0</span>', '<span class="rank">保有</span>')
    return base[:-6] + pl + "</div>"


def build_dashboard(analyses: list[Analysis], meta: dict, usdjpy: float,
                    holdings: list[dict] | None = None) -> tuple[str, dict]:
    holdings = holdings or []
    buys = [a for a in analyses if a.g == "BUY"][:20]
    amap = {a.code: a for a in analyses}
    ver = datetime.now(tz=JST).strftime("%Y%m%d%H%M")

    buy_cards = "".join(_card(i + 1, a, True) for i, a in enumerate(buys)) or '<p class="empty">本日は買いシグナル（スコア35以上）がありません。</p>'
    hold_cards = "".join(_holding_card(h, amap) for h in holdings)
    hold_sec = _section("💼 保有銘柄", "MY HOLDINGS", hold_cards) if holdings else ""

    shown = [a.code for a in buys] + [h.get("code") for h in holdings]

    idx_row = (
        '<div class="idx">'
        '<div class="i" data-idx="SP500"><div class="n">S&amp;P 500</div><div class="v">—</div><div class="c">—</div></div>'
        '<div class="i" data-idx="NASDAQ"><div class="n">NASDAQ</div><div class="v">—</div><div class="c">—</div></div>'
        '<div class="i" data-idx="DOW"><div class="n">NYダウ</div><div class="v">—</div><div class="c">—</div></div>'
        '<div class="i" data-idx="USDJPY"><div class="n">USD/JPY</div><div class="v">—</div><div class="c">—</div></div>'
        '</div>'
    )

    mkt_open = "1" if meta["market_open"] else "0"
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>GLOBE ORACLE — 米国株オラクル</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#0a0f1e">
<link rel="apple-touch-icon" href="apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<style>{CSS_STR}</style>
</head>
<body data-total="{len(analyses)}" data-usdjpy="{usdjpy}" data-open-ms="{meta['open_ms']}"
 data-close-ms="{meta['close_ms']}" data-mopen="{mkt_open}"
 data-nopen="{_esc(meta['next_open_jst'])}" data-nclose="{_esc(meta['next_close_jst'])}">
<div class="wrap">
  <header>
    <div class="brand">GLOBE ORACLE<small>US STOCK ORACLE</small></div>
    <div class="meta">
      <span id="mkt" class="mkt closed">NY市場：—</span>
      <span id="pxasof">株価 {_esc(meta['asof_jst'])} 時点</span>
    </div>
    {idx_row}
  </header>

  <section id="search-sec" class="sec">
    <div class="searchbar"><input id="q" type="text" inputmode="search"
      placeholder="🔍 銘柄検索（例: apple / NVDA / micro）" autocomplete="off"></div>
    <p id="hint">社名（apple）またはティッカー（AAPL）で全{len(analyses)}銘柄を検索。⭐でウォッチ登録。{'<br><span style="color:var(--dn)">⚠ 縮小モード：ユニバース取得に失敗し内蔵リストで動作中（銘柄数が少なめ）</span>' if UNIVERSE_REDUCED.get('reduced') else ''}</p>
    <div id="results" class="cards"></div>
  </section>

  {_section("買いシグナル TOP20", "BUY SIGNALS ・ スコア35以上のみ", buy_cards)}
  {hold_sec}

  <footer>
    データ源：yfinance（約15分遅延・欠損があり得ます）／スコアはテクニカル＋ファンダの独自複合値<br>
    投資判断は自己責任でお願いします。本サイトは投資助言ではありません。<br>
    GLOBE ORACLE ・ 生成 {_esc(meta['asof_jst'])} JST
  </footer>
</div>
<script>window.__SHOWN__={json.dumps(shown)};</script>
<script src="app.js?v={ver}" defer></script>
</body>
</html>"""

    stocks = {
        "stocks": [_to_stock_json(a, i + 1) for i, a in enumerate(analyses)],
        "shown": shown,
        "usdjpy": usdjpy,
        "asof": meta["asof_jst"],
        "mkt": meta,
    }
    return html, stocks


def write_dashboard() -> Path:
    try:
        analyses, meta, holdings = analyze_all()
    except Exception as e:
        print(f"[globe] analyze致命的失敗: {e}", file=sys.stderr)
        traceback.print_exc()
        analyses, meta, holdings = [], market_window(), load_holdings()
    usdjpy = _fetch_usdjpy()
    html, stocks = build_dashboard(analyses, meta, usdjpy, holdings)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "app.js").write_text(APP_JS, encoding="utf-8")
    (DOCS / "stocks.json").write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
    (DOCS / "manifest.json").write_text(_manifest(), encoding="utf-8")
    _gen_icons()
    # 初回 prices も生成（開場前でも指数を出す）
    try:
        write_prices()
    except Exception as e:
        print(f"[globe] 初回prices失敗: {e}", file=sys.stderr)
    return DOCS / "index.html"


def write_prices() -> dict:
    """stocks.jsonのshown銘柄＋指数＋USDJPYの最新値を prices.json に書く。"""
    import yfinance as yf
    meta = market_window()
    shown: list[str] = []
    try:
        sj = json.loads((DOCS / "stocks.json").read_text(encoding="utf-8"))
        shown = sj.get("shown", [])[:40]
    except Exception as e:
        print(f"[globe] stocks.json読込失敗: {e}", file=sys.stderr)

    px: dict[str, float] = {}
    if shown:
        try:
            data = yf.download(shown, period="2d", interval="1d",
                               auto_adjust=True, threads=True, group_by="ticker", progress=False)
            for c in shown:
                try:
                    df = data if len(shown) == 1 else (data[c] if c in data.columns.get_level_values(0) else None)
                    if df is None:
                        continue
                    df = df.rename(columns=str.title)
                    cl = df["Close"].dropna()
                    if len(cl):
                        px[c] = round(float(cl.iloc[-1]), 2)
                except Exception:
                    continue
        except Exception as e:
            print(f"[globe] prices一括取得失敗: {e}", file=sys.stderr)

    idx = {}
    for key, tk in [("SP500", "^GSPC"), ("NASDAQ", "^IXIC"), ("DOW", "^DJI"), ("USDJPY", "JPY=X")]:
        p, chg = _fetch_quote(tk)
        if p is not None:
            idx[key] = {"price": p, "chg": chg if chg is not None else 0.0}

    out = {
        "px": px,
        "idx": idx,
        "asof": datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M"),
        "mkt": meta,
    }
    (DOCS / "prices.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    write_dashboard()
