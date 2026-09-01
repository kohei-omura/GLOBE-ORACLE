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

import copy
import csv
import json
import math
import re
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
    """Wikipedia表記(BRK.B)→yfinance表記(BRK-B)。trim＋大文字化。"""
    return (t or "").strip().upper().replace(".", "-")


def _edit_distance(a: str, b: str) -> int:
    """レーベンシュタイン距離（差が3以上は早期に99）。"""
    a = a or ""
    b = b or ""
    if abs(len(a) - len(b)) > 2:
        return 99
    lb = len(b)
    dp = list(range(lb + 1))
    for i in range(1, len(a) + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, lb + 1):
            cur = dp[j]
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev + (0 if a[i - 1] == b[j - 1] else 1))
            prev = cur
    return dp[lb]


def _suggest_ticker(code: str, valid) -> str | None:
    """編集距離1〜2の最も近い既知ティッカーを返す（無ければNone）。"""
    code = (code or "").upper()
    best, bd = None, 3
    for t in (valid or ()):
        if not t or t == code:
            continue
        d = _edit_distance(code, t)
        if d < bd or (d == bd and best is not None and len(t) < len(best)):
            best, bd = t, d
    return best if (best is not None and bd <= 2) else None


UNIVERSE_REDUCED = {"reduced": False}  # フォールバック（縮小モード）フラグ
SP500_SET: set[str] = set()            # 現S&P500構成銘柄（候補レーダーの除外用）
POOL_REDUCED = {"reduced": False}      # 候補プール縮小モードフラグ

# 候補レーダー定数
SP500_MCAP_MIN = 22_700_000_000        # S&P採用の時価総額目安 $22.7B（改定するため定数化）
SCREEN_MCAP_MIN = 15_000_000_000       # スクリーナー候補プールの下限 $15B
CANDIDATES_FILE = DOCS / "candidates.json"
# 機能1: メディア注目リスト（candidates_media.txt が無ければ内蔵デフォルトを使用）
#   内蔵デフォルト asof 2026-07 ／ 出典＝アナリスト予想・予測市場
MEDIA_FILE = Path("candidates_media.txt")
MEDIA_DEFAULT = ["SOFI", "ALNY", "RDDT", "PSTG", "CVNA", "FIX", "CIEN", "AFRM", "ARES", "MSTR"]


def _load_media() -> list[tuple[str, str]]:
    """candidates_media.txt（1行=ティッカー[,メモ]・#はコメント）を読む。無ければ内蔵デフォルト。"""
    out: list[tuple[str, str]] = []
    if MEDIA_FILE.exists():
        try:
            for line in MEDIA_FILE.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = [p.strip() for p in s.replace("\t", ",").split(",")]
                tk = _norm_ticker(parts[0])
                if tk:
                    out.append((tk, parts[1] if len(parts) > 1 and parts[1] else ""))
        except Exception as e:
            print(f"[globe] candidates_media.txt読込失敗: {e}", file=sys.stderr)
    if not out:
        out = [(t, "") for t in MEDIA_DEFAULT]
    return out
CANDIDATE_SEED = [  # スクリーナー失敗時のフォールバック（SOFI級の準大型・成長株）
    "SOFI", "RDDT", "ALNY", "PSTG", "CVNA", "FIX", "CIEN", "AFRM", "ARES", "MSTR",
    "HOOD", "DKNG", "TOST", "CELH", "SNOW",
]

# ─────────────────────────────────────────────
#  大化け候補レーダー（2桁株）定数
#   ※「1年で30〜40倍」を当てるスクリーニングは存在しない。ここでやるのは
#     急騰局面に入った銘柄が事前に示していた計測可能な特徴での相対順位付け。
# ─────────────────────────────────────────────
GROWTH_PX_MIN = 10.0                   # 2桁株の下限 $10
GROWTH_PX_MAX = 100.0                  # 2桁株の上限（$100未満＝2桁）
GROWTH_MCAP_MIN = 300_000_000          # 時価総額 $300M以上（超小型の仕手株を除外）
GROWTH_VOL_MIN = 300_000               # 3ヶ月平均出来高 30万株以上（流動性）
GROWTH_POOL_PER_SORT = 150             # スクリーナー（フォールバック時）1軸あたりの取得件数
GROWTH_TOP_N = 5                       # 表示するランキング件数（1〜5位）
GROWTH_MAX_UNIVERSE = 9000             # 母集団の安全上限
GROWTH_MAX_SCORE = 2500                # 1年日足を取って採点する上限（足切り後）
GROWTH_POOL_REDUCED = {"reduced": False}
GROWTH_UNIVERSE_SRC = {"src": "", "n_universe": 0, "n_scored": 0}
# SBI証券の取扱銘柄一覧を置くファイル（任意）。あれば最優先で母集団に使う。
#   SBIには取扱銘柄一覧の公開APIが無いため、正確に一致させたい場合だけ手動で用意する。
GROWTH_UNIVERSE_FILE = Path("universe_sbi.txt")
# 無い場合の自動生成元：NASDAQ Trader の公式シンボルディレクトリ（NYSE/NASDAQ/AMEX全上場）
GROWTH_SYMBOL_DIR_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)
GROWTH_SEED = [  # スクリーナー失敗時のフォールバック（高ボラの2桁株になりやすい銘柄群）
    "SOFI", "AFRM", "HOOD", "DKNG", "CELH", "IONQ", "RGTI", "OKLO", "SMR", "ACHR",
    "JOBY", "LUNR", "RKLB", "PLUG", "RIOT", "MARA", "CLSK", "WULF", "CIFR", "BTDR",
    "AI", "BBAI", "SOUN", "TEM", "HIMS", "OSCR", "RXRX", "CRSP", "NTLA", "BEAM",
]


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
                    SP500_SET.add(tk)
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
                 "tgt", "stp", "rr", "ez", "fund", "bt", "gr")

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
        self.gr = None      # 大化け候補スコア（growth_score の戻り値）


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


def growth_score(df: pd.DataFrame) -> dict | None:
    """2桁株の「大化け候補」スコア（0〜100）と内訳を返す。band外・データ不足はNone。

    将来の上昇率を予測するものではない。SanDisk型の急騰局面に入った銘柄が
    “入る前”に共通して示していた、日足だけで計測できる特徴を合成した相対値。
      mom6   6ヶ月騰落率         … トレンドが既に効いているか  (0〜30点)
      accel  3ヶ月 vs 6ヶ月      … 上昇が加速しているか        (0〜15点)
      vsurge 直近20日÷以前5ヶ月  … 資金が入り始めているか      (0〜20点)
      atrp   ATR14 ÷ 株価        … そもそも値幅を出せる銘柄か  (0〜15点)
      nh     52週高値からの位置  … ブレイクアウト圏にいるか    (0〜20点)
    """
    try:
        close = df["Close"].dropna()
        if len(close) < 120:                      # 半年未満は判定材料が足りない
            return None
        price = float(close.iloc[-1])
        if not (GROWTH_PX_MIN <= price < GROWTH_PX_MAX):   # 2桁株のみ
            return None

        def _chg(nbars: int):
            if len(close) <= nbars:
                return None
            base = float(close.iloc[-1 - nbars])
            return (price / base - 1.0) * 100.0 if base > 0 else None

        mom3, mom6 = _chg(63), _chg(126)
        base1y = float(close.iloc[0])
        mom12 = (price / base1y - 1.0) * 100.0 if base1y > 0 else None

        atrp = None
        a1 = float(_atr(df["High"], df["Low"], df["Close"], 14).iloc[-1])
        if a1 == a1 and price > 0:                # NaNチェック
            atrp = a1 / price * 100.0

        vsurge = None
        if "Volume" in df.columns:
            v = df["Volume"].dropna()
            if len(v) >= 126:
                # 直近20日 ÷ それ以前の約5ヶ月平均。基準側に直近を含めると倍率が薄まるため除外。
                v_base = float(v.iloc[-126:-20].mean())
                if v_base > 0:
                    vsurge = float(v.iloc[-20:].mean()) / v_base

        nh = None
        hi = df["High"].dropna()
        if len(hi):
            hi52 = float(hi.max())                # 1年分の日足＝実質52週高値
            if hi52 > 0:
                nh = price / hi52 * 100.0

        sc = 0.0
        if mom6 is not None:                      # 6ヶ月+100%で満点
            sc += _clip(mom6 / 100.0, 0.0, 1.0) * 30.0
        if mom3 is not None and mom6 is not None and mom6 > 0:
            sc += _clip((mom3 / mom6 - 0.5) / 0.5, 0.0, 1.0) * 15.0
        if vsurge is not None:                    # 出来高2倍で満点
            sc += _clip(vsurge - 1.0, 0.0, 1.0) * 20.0
        if atrp is not None:                      # ATR 2%→0点 / 6%で満点
            sc += _clip((atrp - 2.0) / 4.0, 0.0, 1.0) * 15.0
        if nh is not None:                        # 52週高値の95%以上で満点
            sc += _clip((nh - 70.0) / 25.0, 0.0, 1.0) * 20.0

        return {"score": round(sc, 1), "price": round(price, 2),
                "mom3": None if mom3 is None else round(mom3, 1),
                "mom6": None if mom6 is None else round(mom6, 1),
                "mom12": None if mom12 is None else round(mom12, 1),
                "vsurge": None if vsurge is None else round(vsurge, 2),
                "atrp": None if atrp is None else round(atrp, 1),
                "nh": None if nh is None else round(nh, 1)}
    except Exception as e:
        print(f"[globe] growth_score失敗: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────
#  データ取得（一括バッチ）＋ファンダ（上位のみ）
# ─────────────────────────────────────────────
def _download_batch(tickers: list[str], period: str = "1y",
                    chunk: int = 100, pause: float = 0.0) -> dict[str, pd.DataFrame]:
    """日足を一括取得。chunk=1回のリクエスト銘柄数、pause=チャンク間の待機秒（レート制限対策）。"""
    import yfinance as yf
    import time as _t
    out: dict[str, pd.DataFrame] = {}
    CHUNK = max(1, chunk)
    for i in range(0, len(tickers), CHUNK):
        if i and pause > 0:
            _t.sleep(pause)
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


def _div_yield(info: dict) -> float | None:
    """配当利回りを小数（0.025 = 2.5%）に正規化して返す。

    yfinance 0.2.51 以降 `dividendYield` は「％値」（2.5 = 2.5%）を返すようになったため、
    そのまま小数として扱うと表示・判定が100倍ズレる。dividendRate÷株価 で検算できるときは
    それに近い解釈を採り、できないときは requirements.txt が要求する 0.2.54+ の仕様
    （＝％値）とみなして100で割る。
    """
    try:
        v = float(info.get("dividendYield"))
    except (TypeError, ValueError):
        return None
    if not (v > 0):          # 0/負/NaN は配当なし扱い
        return None
    ref = None
    try:
        rate = float(info.get("dividendRate"))
        px = float(info.get("currentPrice") or info.get("regularMarketPrice")
                   or info.get("previousClose") or 0)
        if rate > 0 and px > 0:
            ref = rate / px          # 小数の実測値
    except (TypeError, ValueError):
        ref = None
    if ref:
        return v / 100.0 if abs(v / 100.0 - ref) <= abs(v - ref) else v
    return v / 100.0


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
                "div": _div_yield(info),
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


def _extract_quotes(res) -> list:
    if not res:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        if isinstance(res.get("quotes"), list):
            return res["quotes"]
        fin = res.get("finance") or {}
        result = fin.get("result") or []
        if result and isinstance(result, list):
            return result[0].get("quotes", []) or []
    return []


def _screen_pool() -> list[dict]:
    """yf.screenで米国・時価総額$15B以上を降順最大500件。失敗時はシードにフォールバック。"""
    import yfinance as yf
    pool: list[dict] = []
    try:
        q = yf.EquityQuery("and", [
            yf.EquityQuery("gt", ["intradaymarketcap", SCREEN_MCAP_MIN]),
            yf.EquityQuery("eq", ["region", "us"]),
        ])
        seen: set[str] = set()
        for off in (0, 250):
            res = yf.screen(q, size=250, offset=off, sortField="intradaymarketcap", sortAsc=False)
            quotes = _extract_quotes(res)
            for qt in quotes:
                c = _norm_ticker(str(qt.get("symbol", "")))
                if not c or c in seen:
                    continue
                seen.add(c)
                pool.append({
                    "c": c,
                    "n": qt.get("shortName") or qt.get("longName") or c,
                    "mcap": qt.get("marketCap") or 0,
                    "qtype": str(qt.get("quoteType") or "").upper(),
                    "exch": qt.get("exchange") or qt.get("fullExchangeName") or "",
                })
            if len(quotes) < 250:
                break
    except Exception as e:
        print(f"[globe] yf.screen失敗: {e}", file=sys.stderr)
    if len(pool) < 20:
        POOL_REDUCED["reduced"] = True
        pool = [{"c": c, "n": c, "mcap": 0, "qtype": "EQUITY", "exch": ""} for c in CANDIDATE_SEED]
    else:
        POOL_REDUCED["reduced"] = False
    print(f"candidate pool: {len(pool)} reduced={POOL_REDUCED['reduced']}", file=sys.stderr)
    return pool


_TICKER_RE = re.compile(r"[A-Z]{1,5}([.-][A-Z]{1,2})?$")
# 普通株以外（ワラント／新株予約権／ユニット／優先株／預託証券／社債）を名称から除外する語
_NON_COMMON_WORDS = ("warrant", " right", "rights", " unit", "units", "preferred",
                     "depositary", "notes due", "debenture", "when issued", "%")


def _http_text(url: str, timeout: int = 30) -> str | None:
    """UA付きでテキストを取得（リトライ2回）。requests が無ければNone。"""
    if requests is None:
        return None
    headers = {"User-Agent": "Mozilla/5.0 (GLOBE-ORACLE bot)"}
    for attempt in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if attempt == 1:
                print(f"[globe] 取得失敗 {url}: {e}", file=sys.stderr)
    return None


def _is_common_stock(sym: str, name: str) -> bool:
    """ティッカーと銘柄名から「普通株らしさ」を判定（ワラント/ユニット/優先株等を除外）。"""
    if not sym or not _TICKER_RE.fullmatch(sym.strip().upper()):
        return False
    low = f" {(name or '').lower()} "
    return not any(w in low for w in _NON_COMMON_WORDS)


def _parse_symbol_dir(text: str) -> list[tuple[str, str]]:
    """NASDAQ Trader のパイプ区切りシンボルディレクトリを (ticker, name) に変換。

    nasdaqlisted.txt : Symbol|Security Name|Market Category|Test Issue|...|ETF|NextShares
    otherlisted.txt  : ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|...|NASDAQ Symbol
    末尾の "File Creation Time: ..." 行は読み飛ばす。
    """
    out: list[tuple[str, str]] = []
    for row in csv.DictReader(io.StringIO(text), delimiter="|"):
        raw = (row.get("NASDAQ Symbol") or row.get("Symbol") or row.get("ACT Symbol") or "").strip()
        if not raw or raw.startswith("File Creation Time"):
            continue
        if (row.get("Test Issue") or "N").strip().upper() == "Y":     # テスト銘柄
            continue
        if (row.get("ETF") or "N").strip().upper() == "Y":            # ETFは大化け対象外
            continue
        name = (row.get("Security Name") or "").strip()
        if not _is_common_stock(raw, name):
            continue
        out.append((_norm_ticker(raw), name.split(" - ")[0].strip() or raw))
    return out


def _load_universe_file() -> list[tuple[str, str]]:
    """universe_sbi.txt（SBIの取扱銘柄一覧）を読む。CSVを貼っただけでも動くよう寛容に解釈。

    各行から「ティッカーに見える最初のフィールド」を拾い、残りの最長フィールドを銘柄名とみなす。
    文字コードは UTF-8 →（ダメなら）CP932 の順で試す（SBIのCSVはShift-JISのことがある）。
    """
    if not GROWTH_UNIVERSE_FILE.exists():
        return []
    text = None
    for enc in ("utf-8", "utf-8-sig", "cp932"):
        try:
            text = GROWTH_UNIVERSE_FILE.read_text(encoding=enc)
            break
        except Exception:
            continue
    if text is None:
        print(f"[globe] {GROWTH_UNIVERSE_FILE} の文字コードを判別できません", file=sys.stderr)
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        fields = [f.strip().strip('"') for f in s.replace("\t", ",").split(",") if f.strip()]
        tk, idx = "", -1
        for i, f in enumerate(fields):
            cand = _norm_ticker(f)
            if _TICKER_RE.fullmatch(cand):
                tk, idx = cand, i
                break
        if not tk or tk in seen:
            continue
        seen.add(tk)
        # 銘柄名はティッカーの次の列（CSVのほぼ共通レイアウト）。無ければティッカーで代用。
        nm = fields[idx + 1] if idx + 1 < len(fields) else ""
        out.append((tk, nm or tk))
    return out


def _growth_universe() -> list[dict]:
    """大化け候補の母集団（SBIで買える米国株の全体像）を作る。

    1) universe_sbi.txt があればそれを使う（SBIの取扱銘柄一覧そのもの＝最も正確）
    2) 無ければ NASDAQ Trader の公式シンボルディレクトリから全米上場の普通株を抽出
       （SBIの米国株ラインナップの実質スーパーセット。5,000〜6,500銘柄規模）
    3) どちらも失敗したら Yahooスクリーナー、さらに失敗したら GROWTH_SEED
    """
    rows = _load_universe_file()
    src = f"{GROWTH_UNIVERSE_FILE.name}（SBI取扱銘柄一覧）"
    if not rows:
        merged: dict[str, str] = {}
        for url in GROWTH_SYMBOL_DIR_URLS:
            text = _http_text(url)
            if not text:
                continue
            for tk, nm in _parse_symbol_dir(text):
                merged.setdefault(tk, nm)
        rows = sorted(merged.items())
        src = "全米上場（NASDAQ Trader公式リスト）"
    if not rows:
        pool = _screen_growth_pool()
        GROWTH_UNIVERSE_SRC.update(src="Yahooスクリーナー", n_universe=len(pool))
        return pool
    rows = rows[:GROWTH_MAX_UNIVERSE]
    GROWTH_POOL_REDUCED["reduced"] = False
    GROWTH_UNIVERSE_SRC.update(src=src, n_universe=len(rows))
    print(f"growth universe: {len(rows)} 銘柄 src={src}", file=sys.stderr)
    return [{"c": tk, "n": nm, "mcap": 0} for tk, nm in rows]


def _growth_prefilter(pool: list[dict]) -> list[dict]:
    """段階1: 1ヶ月足だけを一括取得し「2桁株 × 流動性」で足切りする。

    5,000銘柄超の1年日足をいきなり取ると重すぎるため、軽い1ヶ月足でふるいにかけ、
    生き残りだけを段階2（1年日足＋採点）に渡す。上限を超える場合は
    直近1ヶ月の伸びが大きい順に GROWTH_MAX_SCORE 件へ切り詰める。
    """
    if not pool:
        return []
    meta = {p["c"]: p for p in pool}
    frames = _download_batch(list(meta), "1mo", chunk=200, pause=0.2)
    keep: list[tuple[float, str]] = []
    for c, df in frames.items():
        try:
            cl = df["Close"].dropna()
            if len(cl) < 10:
                continue
            px = float(cl.iloc[-1])
            if not (GROWTH_PX_MIN <= px < GROWTH_PX_MAX):     # 2桁株のみ
                continue
            if "Volume" in df.columns:
                v = df["Volume"].dropna()
                if len(v) and float(v.mean()) < GROWTH_VOL_MIN:
                    continue
            first = float(cl.iloc[0])
            keep.append(((px / first - 1.0) if first > 0 else 0.0, c))
        except Exception:
            continue
    keep.sort(key=lambda x: (-x[0], x[1]))   # 直近1ヶ月の伸び順（切り詰め時に候補性の高い方を残す）
    out = [meta[c] for _, c in keep[:GROWTH_MAX_SCORE]]
    GROWTH_UNIVERSE_SRC["n_scored"] = len(out)
    print(f"growth prefilter: {len(meta)} -> {len(out)} 銘柄", file=sys.stderr)
    return out


def _screen_growth_pool() -> list[dict]:
    """フォールバック用スクリーナー：米国・株価$10〜$100未満・時価総額$300M超・出来高30万株超。

    Yahooスクリーナーは1リクエスト1ソート軸なので、性格の違う2軸から上位を取り
    和集合にする（片方だけだと「もう上がりきった銘柄」or「株価と無関係な増収株」
    に寄るため）。失敗時は GROWTH_SEED にフォールバック。
    """
    import yfinance as yf
    pool: dict[str, dict] = {}
    try:
        q = yf.EquityQuery("and", [
            yf.EquityQuery("eq", ["region", "us"]),
            yf.EquityQuery("btwn", ["intradayprice", GROWTH_PX_MIN, GROWTH_PX_MAX]),
            yf.EquityQuery("gt", ["intradaymarketcap", GROWTH_MCAP_MIN]),
            yf.EquityQuery("gt", ["avgdailyvol3m", GROWTH_VOL_MIN]),
        ])
        for field in ("fiftytwowkpercentchange",                     # 直近1年で既に走っている
                      "totalrevenues1yrgrowth.lasttwelvemonths"):    # 業績が跳ねている
            try:
                res = yf.screen(q, size=GROWTH_POOL_PER_SORT, sortField=field, sortAsc=False)
            except Exception as e:
                print(f"[globe] growth screen失敗 {field}: {e}", file=sys.stderr)
                continue
            for qt in _extract_quotes(res):
                c = _norm_ticker(str(qt.get("symbol", "")))
                if not c or c in pool:
                    continue
                if str(qt.get("quoteType") or "EQUITY").upper() != "EQUITY":
                    continue
                pool[c] = {"c": c,
                           "n": qt.get("shortName") or qt.get("longName") or c,
                           "mcap": qt.get("marketCap") or 0}
    except Exception as e:
        print(f"[globe] growth pool失敗: {e}", file=sys.stderr)
    if len(pool) < 10:
        GROWTH_POOL_REDUCED["reduced"] = True
        pool = {c: {"c": c, "n": c, "mcap": 0} for c in GROWTH_SEED}
    else:
        GROWTH_POOL_REDUCED["reduced"] = False
    print(f"growth pool: {len(pool)} reduced={GROWTH_POOL_REDUCED['reduced']}", file=sys.stderr)
    return list(pool.values())


def _growth_pool() -> list[dict]:
    """大化け候補の母集団を作って足切りまで済ませる（段階1）。

    母集団は universe_sbi.txt → 全米上場リスト → Yahooスクリーナー → GROWTH_SEED の順で決まる。
    そこから 2桁株×流動性 で絞った結果を返し、段階2（1年日足＋採点）は analyze_all が行う。
    """
    pool = _growth_prefilter(_growth_universe())
    if len(pool) < 10:      # 母集団が壊れている＝ネットワーク不調。種リストで最低限動かす
        GROWTH_POOL_REDUCED["reduced"] = True
        pool = [{"c": c, "n": c, "mcap": 0} for c in GROWTH_SEED]
        GROWTH_UNIVERSE_SRC.update(src="内蔵シードリスト", n_universe=len(pool), n_scored=len(pool))
    print(f"growth pool: {len(pool)} reduced={GROWTH_POOL_REDUCED['reduced']}", file=sys.stderr)
    return pool


def _eval_criteria(code: str, mcap_hint: float) -> tuple[dict, float, str]:
    """5基準チェックを判定。返り値: (crit, mcap, name)。"""
    import yfinance as yf
    import time as _t
    crit = {"mc": False, "ttm": False, "q": False, "us": False, "age": False}
    mcap = mcap_hint or 0
    name = code
    try:
        tk = yf.Ticker(code)
        info = tk.info or {}
        mcap = info.get("marketCap") or mcap
        name = info.get("shortName") or info.get("longName") or code
        crit["mc"] = bool(mcap and mcap >= SP500_MCAP_MIN)
        nitc = info.get("netIncomeToCommon")
        crit["ttm"] = bool(nitc and nitc > 0)
        crit["us"] = info.get("country") == "United States"
        ftd = info.get("firstTradeDateEpochUtc")
        if ftd:
            crit["age"] = (_t.time() - float(ftd)) >= 365 * 24 * 3600
        try:
            qis = tk.quarterly_income_stmt
            if qis is not None and not qis.empty:
                for key in ("Net Income", "NetIncome", "Net Income Common Stockholders"):
                    if key in qis.index:
                        val = qis.loc[key].dropna()
                        if len(val):
                            crit["q"] = float(val.iloc[0]) > 0
                        break
        except Exception:
            pass
    except Exception as e:
        print(f"[globe] 候補info失敗 {code}: {e}", file=sys.stderr)
    return crit, mcap, name


def screen_candidates() -> tuple[list[dict], dict]:
    """S&P500入り候補レーダー。プール取得→現構成除外→上位60判定→スコア上位10。"""
    pool = _screen_pool()
    filt = [p for p in pool if p["c"] not in SP500_SET and p.get("qtype", "") in ("", "EQUITY")]
    filt.sort(key=lambda x: x.get("mcap") or 0, reverse=True)
    top = filt[:60]  # info呼び出しは最大60銘柄に制限（節約）
    cands: list[dict] = []
    for p in top:
        crit, mcap, name = _eval_criteria(p["c"], p.get("mcap") or 0)
        met = sum(1 for v in crit.values() if v)
        headroom = (mcap / SP500_MCAP_MIN) if (SP500_MCAP_MIN and mcap) else 0.0
        score = round(met + min(headroom, 3.0) * 0.1, 3)
        cands.append({"c": p["c"], "n": name, "mcap": mcap, "crit": crit,
                      "met": met, "score": score})
    cands.sort(key=lambda x: x["score"], reverse=True)
    cands = cands[:10]
    prev_codes: set[str] = set()
    try:
        if CANDIDATES_FILE.exists():
            prev = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
            prev_codes = {x["c"] for x in prev.get("list", [])}
    except Exception:
        pass
    for x in cands:
        x["status"] = "NEW" if x["c"] not in prev_codes else "OK"
        x["src"] = "auto"
    # 機能1: メディア注目銘柄をマージ（重複除去・既存の基準チェックを同様に判定・S&P500採用済みは除外）
    auto_codes = {x["c"]: x for x in cands}
    for tk, memo in _load_media():
        if tk in SP500_SET:      # 現S&P500構成銘柄は除外（採用されたら自動的に消える）
            continue
        if tk in auto_codes:     # 自動判定にも入っている＝両方
            auto_codes[tk]["src"] = "both"
            if memo:
                auto_codes[tk]["memo"] = memo
            continue
        try:
            crit, mcap, name = _eval_criteria(tk, 0.0)
        except Exception as e:  # noqa
            print(f"[globe] media基準判定失敗 {tk}: {e}", file=sys.stderr)
            crit, mcap, name = {}, 0.0, tk
        met = sum(1 for v in crit.values() if v)
        cands.append({"c": tk, "n": name or tk, "mcap": mcap, "crit": crit,
                      "met": met, "score": round(met, 3),
                      "status": "OK", "src": "media", "memo": memo})
    meta = {"asof": datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M"),
            "threshold": SP500_MCAP_MIN, "reduced": POOL_REDUCED["reduced"]}
    return cands, meta


def analyze_all() -> tuple[list[Analysis], dict, list[dict], list[dict], dict, list[Analysis]]:
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

    # ── 2-E: S&P500入り候補レーダー ──
    cands, cmeta = screen_candidates()
    cand_codes = [x["c"] for x in cands]
    have = {a.code for a in analyses}
    missing = [c for c in cand_codes if c not in have]
    if missing:
        cframes = _download_batch(missing, "1y")
        frames.update(cframes)   # 後段（大化け候補レーダー）で再ダウンロードしないよう合流
        for c in missing:
            df = cframes.get(c)
            if df is None:
                one = _download_batch([c], "1y")
                df = one.get(c)
            if df is None or df["Close"].dropna().shape[0] < 60:
                continue
            try:
                price = float(df["Close"].dropna().iloc[-1])
                sc, reasons, lv = technical_score(df)
                nm = next((x["n"] for x in cands if x["c"] == c), c)
                a = Analysis(c, nm, "", round(price, 2))
                a.sc = sc
                a.g = signal_of(sc)
                a.reasons = reasons
                if lv:
                    a.tgt, a.stp, a.rr, a.ez = lv["tgt"], lv["stp"], lv["rr"], lv["ez"]
                analyses.append(a)
            except Exception as e:
                print(f"[globe] 候補分析失敗 {c}: {e}", file=sys.stderr)

    # ── 大化け候補レーダー（2桁株）TOP5 ──
    growth: list[Analysis] = []
    try:
        gmeta = {p["c"]: p for p in _growth_pool()}
        need = [c for c in gmeta if c not in frames]
        if need:
            frames.update(_download_batch(need, "1y"))
        scored: list[tuple[float, str, dict]] = []
        for c in gmeta:
            df = frames.get(c)
            if df is None:
                continue
            gr = growth_score(df)
            if gr:
                scored.append((gr["score"], c, gr))
        # 同点はティッカー順で決定的に（日々の並びが無意味に入れ替わらないように）
        scored.sort(key=lambda x: (-x[0], x[1]))
        amap_g = {a.code: a for a in analyses}
        for _sc, c, gr in scored[:GROWTH_TOP_N]:
            a = amap_g.get(c)
            if a is None:   # ユニバース外の銘柄は上位入りした分だけ分析して合流
                try:
                    df = frames[c]
                    price = float(df["Close"].dropna().iloc[-1])
                    tsc, reasons, lv = technical_score(df)
                    a = Analysis(c, gmeta[c].get("n") or c, "", round(price, 2))
                    a.sc, a.g, a.reasons = tsc, signal_of(tsc), reasons
                    if lv:
                        a.tgt, a.stp, a.rr, a.ez = lv["tgt"], lv["stp"], lv["rr"], lv["ez"]
                    analyses.append(a)
                except Exception as e:
                    print(f"[globe] 大化け候補分析失敗 {c}: {e}", file=sys.stderr)
                    continue
            gr["mcap"] = gmeta[c].get("mcap") or 0
            a.gr = gr
            growth.append(a)
    except Exception as e:
        print(f"[globe] 大化け候補レーダー失敗: {e}", file=sys.stderr)

    analyses.sort(key=lambda x: x.sc, reverse=True)
    meta = market_window()
    return analyses, meta, holdings, cands, cmeta, growth


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


def _candidate_card(x: dict, amap: dict) -> str:
    a = amap.get(x["c"])
    crit = x["crit"]
    checks = "".join("✅" if crit.get(k) else "❌" for k in ("mc", "ttm", "q", "us", "age"))
    tech = _badge(a.g) if a else '<span class="badge hold">?</span>'
    newb = '<span class="badge buy">NEW</span>' if x.get("status") == "NEW" else ""
    _src = x.get("src", "auto")
    srcb = {"auto": '<span class="badge src-auto">🤖 自動判定</span>',
            "media": '<span class="badge src-media">📰 メディア注目</span>',
            "both": '<span class="badge src-both">🤖📰 両方</span>'}.get(_src, "")
    memo = f'<span class="cmemo">{_esc(x.get("memo",""))}</span>' if x.get("memo") else ""
    mcap_b = f"${x['mcap']/1e9:.1f}B" if x.get("mcap") else "—"
    px = (f'<span class="price" data-px="{_esc(x["c"])}" data-usd="{a.price}">{_usd(a.price)}</span>'
          if a else '<span class="price">—</span>')
    return (
        f'<div class="card"><div class="row1">'
        f'<div class="title"><span class="code">{_esc(x["c"])}</span>'
        f'<span class="name">{_esc(x["n"])}</span></div>{srcb}{newb}{tech}</div>'
        f'<div class="row2">{px}'
        f'<span class="seg">時価総額 {mcap_b}</span>'
        f'<span class="chip">基準 {checks} ({x["met"]}/5)</span>{memo}</div></div>'
    )


def _candidate_section(cands: list[dict], cmeta: dict, amap: dict) -> str:
    if not cands:
        return ""
    cards = "".join(_candidate_card(x, amap) for x in cands)
    reduced = '<span style="color:var(--dn)">⚠ 候補プール縮小モード</span> ' if cmeta.get("reduced") else ""
    thr = SP500_MCAP_MIN / 1e9
    foot = (f'<p class="cfoot">{reduced}S&amp;P公式基準（時価総額 ${thr:.1f}B・GAAP黒字等）による自動判定です。'
            f'採用は委員会の裁量であり、本表示は採用を保証するものではありません。次回リバランス：毎四半期第3金曜。'
            f'<br>判定閾値 ${thr:.1f}B ／ 判定日時 {_esc(cmeta.get("asof",""))} JST</p>')
    return (f'<section class="sec"><h2 class="find"><span>🎯 S&amp;P500入り候補レーダー</span>'
            f'<em>毎日自動判定</em></h2><div class="cards">{cards}</div>{foot}</section>')


def _growth_card(rank: int, a: Analysis) -> str:
    gr = a.gr or {}

    def _pct(v) -> str:
        return "—" if v is None else f'{"+" if v >= 0 else ""}{v:.0f}%'

    chips = [f'1年 {_pct(gr.get("mom12"))}', f'6ヶ月 {_pct(gr.get("mom6"))}']
    chips.append(f'出来高 {gr["vsurge"]:.1f}倍' if gr.get("vsurge") else "出来高 —")
    chips.append(f'ATR {gr["atrp"]:.1f}%' if gr.get("atrp") else "ATR —")
    chips.append(f'52週高値比 {gr["nh"]:.0f}%' if gr.get("nh") else "52週高値比 —")
    chips_html = '<div class="reasons">' + "".join(
        f'<span class="chip">{_esc(c)}</span>' for c in chips) + "</div>"
    mcap = f'<span class="seg">時価総額 ${gr["mcap"]/1e9:.1f}B</span>' if gr.get("mcap") else ""
    gsc = float(gr.get("score") or 0.0)
    return (
        f'<div class="card"><div class="row1"><span class="rank">{rank}</span>'
        f'<div class="title"><span class="code">{_esc(a.code)}</span>'
        f'<span class="name">{_esc(a.name)}</span>{mcap}</div>{_badge(a.g)}</div>'
        f'<div class="row2">'
        f'<span class="price" data-px="{_esc(a.code)}" data-usd="{a.price}">{_usd(a.price)}</span>'
        f'<span class="gscore">爆発力 {gsc:.0f}</span>'
        f'<span class="bar"><span class="bar-g" style="width:{_clip(gsc, 0.0, 100.0):.0f}%"></span></span>'
        f'</div>{chips_html}</div>'
    )


def _growth_section(growth: list[Analysis] | None) -> str:
    if not growth:
        return ""
    cards = "".join(_growth_card(i + 1, a) for i, a in enumerate(growth))
    reduced = ('<span style="color:var(--dn)">⚠ 母集団縮小モード</span> '
               if GROWTH_POOL_REDUCED.get("reduced") else "")
    src = GROWTH_UNIVERSE_SRC.get("src") or "—"
    n_uni, n_sc = GROWTH_UNIVERSE_SRC.get("n_universe", 0), GROWTH_UNIVERSE_SRC.get("n_scored", 0)
    foot = (f'<p class="cfoot">{reduced}母集団：{_esc(src)} <b>{n_uni:,}銘柄</b> → '
            f'株価 ${GROWTH_PX_MIN:.0f}〜${GROWTH_PX_MAX:.0f}未満（2桁株）・'
            f'平均出来高 {GROWTH_VOL_MIN:,}株超で <b>{n_sc:,}銘柄</b>に絞り、'
            f'「6ヶ月モメンタム／上昇の加速／出来高急増／ATR（値幅）／52週高値からの位置」の5要素で'
            f'採点した上位{GROWTH_TOP_N}銘柄です。'
            f'<br><b style="color:var(--dn)">これは「1年で30〜40倍になる銘柄」を予測するものではありません。</b>'
            f'SanDisk型の急騰局面に入った銘柄が事前に示していた特徴を並べているだけで、'
            f'同じ特徴を持つ銘柄の大半は大化けせず、高ボラティリティは下落幅も大きいことを意味します。'
            f'1銘柄への集中投資は避けてください。</p>')
    return (f'<section class="sec"><h2 class="find"><span>🚀 大化け候補レーダー（2桁株）TOP{GROWTH_TOP_N}</span>'
            f'<em>高ボラ×出来高急増×高値圏</em></h2><div class="cards">{cards}</div>{foot}</section>')


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
.badge.src-auto{background:rgba(142,163,200,.16);color:var(--mut)}
.badge.src-media{background:rgba(232,201,106,.16);color:var(--gold)}
.badge.src-both{background:rgba(70,196,106,.16);color:var(--up)}
.cmemo{font-size:11px;color:var(--mut);margin-left:6px}
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
.hold-sum{margin:0 0 10px;padding:10px 13px;border:1px solid var(--line);border-radius:12px;background:var(--card);font-size:13px;color:var(--ink)}
.hold-sum b.up{color:var(--up)}.hold-sum b.dn{color:var(--dn)}
.hold-sum-note{color:var(--mut);font-size:10.5px;margin-left:6px}
.hnote{font-size:11px;color:var(--mut);margin-left:8px;font-weight:600}
.cfoot{font-size:11px;color:var(--mut);line-height:1.7;margin:10px 2px 0;padding:8px 10px;background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:10px}
.ez{margin-top:8px;font-size:12px;font-weight:800;color:var(--fg);
background:rgba(255,255,255,.04);border:1px solid var(--line);border-radius:10px;padding:7px 10px}
.ez.hit{background:rgba(70,196,106,.16);border-color:rgba(70,196,106,.55)}
.ez.hit b{color:var(--buy)}
.ezn{margin-left:8px;font-size:11px;font-weight:700;color:var(--mut)}
.fund,.bt,.reasons{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.fchip,.btchip,.chip{font-size:11px;padding:2px 8px;border-radius:8px;border:1px solid var(--line);color:var(--mut)}
.btchip.win{color:var(--up);border-color:rgba(70,196,106,.4)}
.chip{color:var(--gold2);border-color:rgba(232,201,106,.28)}
.gscore{font-size:14px;font-weight:800;color:var(--gold);white-space:nowrap}
.bar-g{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--gold2),var(--gold))}
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
    var now = Date.now();
    /* 開場判定は埋め込み窓(epoch)で行う。market_openはHTML/prices.json生成時点の
       スナップショットなので、窓内に入っても閉場のまま固まる（＝5分ポーリングに
       切り替わらない）。窓が無いときだけフラグにフォールバックする。 */
    var open = (MKT.open_ms && MKT.close_ms)
      ? (now >= MKT.open_ms && now < MKT.close_ms)
      : MKT.market_open;
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


def _holding_card(h: dict, amap: dict, valid=None) -> str:
    code = h.get("code")
    a = amap.get(code)
    avg = h.get("avg", 0) or 0
    if not a:
        # データ取得不可でも保有として必ず表示（サイレント脱落させない）＋タイポ候補を提示
        sug = _suggest_ticker(code, valid or set())
        if sug:
            hint = (f'⚠ {_esc(code)}：データ取得不可 — もしかして <b>{_esc(sug)}</b>？'
                    f'（holdings.txtを修正してください）')
        else:
            hint = f'⚠ {_esc(code)}：データ取得不可（ティッカー確認 or 一時的な取得失敗）'
        return (f'<div class="card"><div class="row1"><span class="rank">保有</span>'
                f'<div class="title"><span class="code">{_esc(code)}</span>'
                f'<span class="name">買値 {_usd(avg)}</span></div>'
                f'<span class="badge hold">?</span></div>'
                f'<div class="pl dn">{hint}</div></div>')
    if h.get("tgt") or h.get("stp"):
        # holdings.txt の手動指定はこのカード限定。共有のAnalysisを書き換えると
        # stocks.json や他セクションの自動算出水準まで上書きされてしまう。
        a = copy.copy(a)
        if h.get("tgt"):
            a.tgt = h["tgt"]
        if h.get("stp"):
            a.stp = h["stp"]
        # 水準が変わったのでRRも手動値ベースで再計算（自動算出のRRが残ると矛盾する）
        a.rr = (round((a.tgt - a.price) / (a.price - a.stp), 1)
                if (a.tgt and a.stp and a.price > a.stp) else None)
    pl_pct = round((a.price - avg) / avg * 100.0, 1) if avg else 0.0
    cls = "up" if pl_pct >= 0 else "dn"
    pl = (f'<div class="pl {cls}">損益 {"+" if pl_pct>=0 else ""}{pl_pct}%'
          f'<span class="hnote">買値 {_usd(avg)} → 現値 {_usd(a.price)}</span></div>')
    base = _card(0, a, True).replace('<span class="rank">0</span>', '<span class="rank">保有</span>')
    return base[:-6] + pl + "</div>"


def build_dashboard(analyses: list[Analysis], meta: dict, usdjpy: float,
                    holdings: list[dict] | None = None,
                    cands: list[dict] | None = None,
                    cmeta: dict | None = None,
                    growth: list[Analysis] | None = None) -> tuple[str, dict]:
    holdings = holdings or []
    cands = cands or []
    cmeta = cmeta or {}
    growth = growth or []
    amap = {a.code: a for a in analyses}
    ver = datetime.now(tz=JST).strftime("%Y%m%d%H%M")

    # 機能2: 買い候補ランキング＝「プロ予想乖離 tp>0」かつ「val=割安」の両方を満たす銘柄のみ・スコア降順・最大10件
    #   （analyses はスコア降順のため出現順を維持。検索/バックテストは不変）
    buy_rank = [a for a in analyses
                if a.fund and a.fund.get("target_pct") is not None
                and a.fund["target_pct"] > 0 and a.fund.get("valuation") == "割安"][:10]
    if buy_rank:
        buy_cards = "".join(_card(i + 1, a, True) for i, a in enumerate(buy_rank))
        if len(buy_rank) < 10:
            buy_cards += f'<p class="empty">本日、条件を満たすのは{len(buy_rank)}銘柄です。</p>'
    else:
        buy_cards = '<p class="empty">本日は条件を満たす銘柄がありません。</p>'
    _valid_tk = set(amap.keys()) | SP500_SET
    # 機能4: 保有サマリー行（合計評価損益・¥換算・含み益/含み損 銘柄数）※USDJPYはprices.jsonと同レート
    _res = [(h, amap.get(h.get("code"))) for h in holdings]
    _rv = [(h, a) for h, a in _res if a]
    hold_sum = ""
    if _rv:
        _tot = sum((a.price - (h.get("avg") or 0)) for h, a in _rv)
        _win = sum(1 for h, a in _rv if a.price >= (h.get("avg") or 0))
        _los = sum(1 for h, a in _rv if a.price < (h.get("avg") or 0))
        _yen = f"¥{round(_tot * usdjpy):,}" if usdjpy else "¥—"
        hold_sum = (f'<div class="hold-sum">💼 合計評価損益 '
                    f'<b class="{"up" if _tot>=0 else "dn"}">{"+" if _tot>=0 else ""}{_usd(_tot)}</b>'
                    f'（¥換算 {_yen}）／ 含み益{_win}銘柄・含み損{_los}銘柄'
                    f'<span class="hold-sum-note">（1株あたり合算）</span></div>')
    hold_cards = hold_sum + "".join(_holding_card(h, amap, _valid_tk) for h in holdings)
    hold_sec = _section("💼 保有銘柄", "MY HOLDINGS", hold_cards) if holdings else ""
    cand_sec = _candidate_section(cands, cmeta, amap)
    growth_sec = _growth_section(growth)

    # ライブ価格(prices.json)の対象＝実際にカード表示している銘柄。
    #   保有 → 買い候補 → 大化け候補 → 候補レーダー の順（write_pricesが先頭40件に切るため優先度順）。
    #   ここが表示内容とズレると prices.json に載らずカードの株価が更新されない。
    shown = list(dict.fromkeys(
        [h["code"] for h in holdings if h.get("code")]
        + [a.code for a in buy_rank]
        + [a.code for a in growth]
        + [x["c"] for x in cands if x.get("c") in amap]
    ))

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

  {cand_sec}

  <section id="search-sec" class="sec">
    <div class="searchbar"><input id="q" type="text" inputmode="search"
      placeholder="🔍 銘柄検索（例: apple / NVDA / micro）" autocomplete="off"></div>
    <p id="hint">社名（apple）またはティッカー（AAPL）で全{len(analyses)}銘柄を検索。⭐でウォッチ登録。{'<br><span style="color:var(--dn)">⚠ 縮小モード：ユニバース取得に失敗し内蔵リストで動作中（銘柄数が少なめ）</span>' if UNIVERSE_REDUCED.get('reduced') else ''}</p>
    <div id="results" class="cards"></div>
  </section>

  {_section("買い候補ランキング TOP10", "条件：アナリスト予想プラス × 割安判定", buy_cards)}
  {growth_sec}
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
        analyses, meta, holdings, cands, cmeta, growth = analyze_all()
    except Exception as e:
        print(f"[globe] analyze致命的失敗: {e}", file=sys.stderr)
        traceback.print_exc()
        analyses, meta, holdings, cands, cmeta, growth = [], market_window(), load_holdings(), [], {}, []
    usdjpy = _fetch_usdjpy()
    html, stocks = build_dashboard(analyses, meta, usdjpy, holdings, cands, cmeta, growth)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "app.js").write_text(APP_JS, encoding="utf-8")
    (DOCS / "stocks.json").write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
    # 候補レーダー: candidates.json（NEW差分は screen_candidates で付与済み）
    cand_out = {"asof": cmeta.get("asof"), "threshold": cmeta.get("threshold", SP500_MCAP_MIN),
                "reduced": cmeta.get("reduced", False),
                "list": [{"c": x["c"], "n": x["n"], "mcap": x["mcap"], "crit": x["crit"],
                          "score": x["score"], "status": x.get("status", "OK")} for x in cands]}
    (CANDIDATES_FILE).write_text(json.dumps(cand_out, ensure_ascii=False), encoding="utf-8")
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
