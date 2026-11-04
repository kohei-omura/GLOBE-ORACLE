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
import os
import re
import sys
import traceback
import warnings
from datetime import datetime, timezone, timedelta, date, time as dtime
from functools import lru_cache
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

# yfinance 内部が出す pandas の非推奨警告（inplace=）が1回の実行で数万行出てログを埋め、
# 肝心のエラーが読めなくなっていたため抑止する（動作には影響しない）。
warnings.filterwarnings("ignore", message=r".*inplace keyword.*")

JST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York") if ZoneInfo else timezone(timedelta(hours=-5))
DOCS = Path("docs")
DOCS.mkdir(exist_ok=True)

WATCH_FALLBACK: list[str] = []  # クライアント側 localStorage 管理（サーバーは空でOK）

# 分析がこの件数を下回ったら docs/ を上書きせず中止する（サイトを空で潰さないため）。
#   内蔵 FALLBACK_UNIVERSE だけでも100銘柄前後は残るので、ここを割るのは
#   ユニバース取得・日足取得の全経路が壊れたときだけ。前回比ではなく絶対下限にしているのは、
#   一度銘柄数が増えた後に一部の取得だけ失敗した際、更新が止まり続けるのを避けるため。
DASHBOARD_MIN_STOCKS = 50


class DashboardSkipped(RuntimeError):
    """分析結果が異常に少ないため、既存の docs/ を温存して更新を中止したことを表す。"""


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
GROWTH_CONFIRM_N = 40                  # Ticker.info で確認する上位候補数（時価総額・種別・業績）
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
# NYSEの休場日・短縮取引日は規則から毎年計算する（以前は2026〜27年のハードコードで、
# 2028年以降は祝日を営業日と誤判定するようになっていた）。
# 規則で決まらない臨時休場（国葬など）だけ US_EXTRA_CLOSED に手で追加する。
US_EXTRA_CLOSED: set[date] = set()


def _nth_weekday(y: int, m: int, wd: int, nth: int) -> date:
    """y年m月の第nth週の曜日wd（月=0）。nth=-1 で最終週。"""
    if nth > 0:
        d = date(y, m, 1)
        return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * (nth - 1))
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _easter(y: int) -> date:
    """グレゴリオ暦の復活祭（Anonymous Gregorian algorithm）。"""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(y, month, (h + l - 7 * m + 114) % 31 + 1)


def _observed(d: date) -> date:
    """土曜の祝日は前の金曜、日曜の祝日は翌月曜に振替。"""
    return d - timedelta(days=1) if d.weekday() == 5 else (
        d + timedelta(days=1) if d.weekday() == 6 else d)


@lru_cache(maxsize=None)
def _us_holidays(y: int) -> frozenset:
    hol = {
        _nth_weekday(y, 1, 0, 3),                    # キング牧師記念日（1月第3月曜）
        _nth_weekday(y, 2, 0, 3),                    # 大統領の日（2月第3月曜）
        _easter(y) - timedelta(days=2),              # 聖金曜日
        _nth_weekday(y, 5, 0, -1),                   # メモリアルデー（5月最終月曜）
        _observed(date(y, 7, 4)),                    # 独立記念日
        _nth_weekday(y, 9, 0, 1),                    # レイバーデー（9月第1月曜）
        _nth_weekday(y, 11, 3, 4),                   # 感謝祭（11月第4木曜）
        _observed(date(y, 12, 25)),                  # クリスマス
    }
    ny = date(y, 1, 1)
    if ny.weekday() != 5:                            # 元日が土曜なら前年12/31は休まない（NYSE規則）
        hol.add(_observed(ny))
    if y >= 2022:
        hol.add(_observed(date(y, 6, 19)))           # ジューンティーンス（2022年〜）
    return frozenset(hol | {d for d in US_EXTRA_CLOSED if d.year == y})


@lru_cache(maxsize=None)
def _us_half_days(y: int) -> frozenset:
    """13:00 ET 早引け：感謝祭翌日、平日(月〜木)の7/3とクリスマスイブ。"""
    half = {_nth_weekday(y, 11, 3, 4) + timedelta(days=1)}
    for d in (date(y, 7, 3), date(y, 12, 24)):
        if d.weekday() <= 3:
            half.add(d)
    return frozenset(half - _us_holidays(y))


def _et_now() -> datetime:
    return datetime.now(tz=ET)


def _is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _us_holidays(d.year)


def _session(d: date):
    half = d in _us_half_days(d.year)
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
                 "tgt", "stp", "rr", "ez", "fund", "bt", "gr", "atr")

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
        self.gr = None      # 大化け候補の特徴量と点数（growth_features＋rank_growth）
        self.atr = None     # ATR14（保有銘柄の買値基準ラインに使う）


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
    levels = {"tgt": tgt, "stp": stp, "rr": rr, "atr": round(a1, 4),
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


def growth_features(df: pd.DataFrame) -> dict | None:
    """2桁株の「大化け候補」判定に使う生の特徴量を日足から計算する（採点は rank_growth）。

    固定閾値で点数化すると強い銘柄が軒並み満点に張り付き順位が付かなくなる（実運用で
    TOP5が 85/85/85/84/84 に飽和）。ここでは特徴量だけを出し、母集団内の相対順位で採点する。
    根拠のある特徴だけを使う：
      rs_raw  3/6/9/12ヶ月の加重リターン（IBD型 RS。モメンタム継続効果）
      nh      52週高値への近さ（George & Hwang 2004：高値圏ほど継続しやすい）
      udv     上昇日出来高÷下落日出来高（50日）… 買い集め。単純な出来高急増は投げ売りでも立つ
      vsurge  直近20日出来高÷それ以前 … 資金流入の始まり
      atrp    ATR%（大化けには値幅を出せることが必要条件）
      trend   Minervini のトレンドテンプレート（超上昇株の事前条件）の充足率
      ext     50日線からの乖離（行き過ぎ＝既に走り切った可能性）
    """
    try:
        close = df["Close"].dropna()
        n = len(close)
        if n < 120:                               # 半年未満は判定材料が足りない
            return None
        price = float(close.iloc[-1])
        if not (GROWTH_PX_MIN <= price < GROWTH_PX_MAX):   # 2桁株のみ
            return None
        high = df["High"].reindex(close.index)
        low = df["Low"].reindex(close.index)

        def ret(nbars: int):
            if n <= nbars:
                return None
            base = float(close.iloc[-1 - nbars])
            return price / base - 1.0 if base > 0 else None

        r63, r126, r189, r252 = ret(63), ret(126), ret(189), ret(min(252, n - 1))
        parts = [(0.4, r63), (0.2, r126), (0.2, r189), (0.2, r252)]
        wsum = sum(w for w, r in parts if r is not None)
        rs_raw = sum(w * r for w, r in parts if r is not None) / wsum if wsum else None

        base0 = float(close.iloc[0])
        mom12 = (price / base0 - 1.0) * 100.0 if base0 > 0 else None

        hi52 = float(high.max()) if high.notna().any() else None
        lo52 = float(low.min()) if low.notna().any() else None
        nh = price / hi52 if hi52 and hi52 > 0 else None

        a1 = float(_atr(high, low, close, 14).iloc[-1])
        atrp = a1 / price * 100.0 if (a1 == a1 and price > 0) else None

        vsurge = udv = None
        if "Volume" in df.columns:
            v = df["Volume"].reindex(close.index).fillna(0.0)
            if n >= 126:
                v_base = float(v.iloc[-126:-20].mean())
                if v_base > 0:
                    vsurge = float(v.iloc[-20:].mean()) / v_base
            chg = close.diff().iloc[-50:]
            vv = v.iloc[-50:]
            up, dn = float(vv[chg > 0].sum()), float(vv[chg < 0].sum())
            if up + dn > 0:
                udv = min(up / dn, 5.0) if dn > 0 else 5.0

        sma = {k: close.rolling(k).mean() for k in (50, 150, 200)}
        s50, s150, s200 = (float(sma[k].iloc[-1]) if n >= k else None for k in (50, 150, 200))
        checks: list[bool] = []
        if s150 is not None and s200 is not None:
            checks += [price > s150 and price > s200, s150 > s200]
        if s200 is not None and n >= 221:
            checks.append(s200 > float(sma[200].iloc[-22]))          # 200日線が1ヶ月以上上向き
        if s50 is not None and s150 is not None and s200 is not None:
            checks.append(s50 > s150 and s50 > s200)
        if s50 is not None:
            checks.append(price > s50)
        if lo52:
            checks.append(price >= 1.30 * lo52)                       # 52週安値から+30%以上
        if hi52:
            checks.append(price >= 0.75 * hi52)                       # 52週高値から-25%以内
        trend_pass, trend_n = sum(checks), len(checks)
        ext = price / s50 - 1.0 if s50 else None

        rnd = lambda x, d=1: None if x is None else round(x, d)
        return {"price": round(price, 2), "bars": n, "short_hist": n < 240,
                "rs_raw": rs_raw, "mom3": rnd(r63 * 100 if r63 is not None else None),
                "mom6": rnd(r126 * 100 if r126 is not None else None), "mom12": rnd(mom12),
                "nh": rnd(nh * 100 if nh is not None else None), "udv": rnd(udv, 2),
                "vsurge": rnd(vsurge, 2), "atrp": rnd(atrp),
                "trend_pass": trend_pass, "trend_n": trend_n,
                "ext": rnd(ext * 100 if ext is not None else None)}
    except Exception as e:
        print(f"[globe] growth_features失敗: {e}", file=sys.stderr)
        return None


# テクニカル合成の重み（計85点）＋トレンドテンプレート15点
_GROWTH_W = {"rs_raw": 0.30, "nh": 0.20, "udv": 0.15, "vsurge": 0.10, "atrp": 0.10}


def rank_growth(feats: dict[str, dict]) -> None:
    """母集団内の相対順位（パーセンタイル）でテクニカル点を付ける。feats を直接更新する。

    欠損した特徴は中立(0.5)扱い。50日線から+50%を超える過熱は最大3割減点する。
    付与するキー: rs（1〜99のRSレーティング）, tech（0〜100）, score（=tech。確認段階で上書き）
    """
    if not feats:
        return
    codes = list(feats)

    def pct(key: str) -> pd.Series:
        v = pd.Series({c: feats[c].get(key) for c in codes}, dtype="float64")
        return v.rank(pct=True).fillna(0.5)

    tech = sum(w * pct(k) for k, w in _GROWTH_W.items()) * 100.0
    trend = pd.Series({c: (feats[c]["trend_pass"] / feats[c]["trend_n"]) if feats[c]["trend_n"]
                       else 0.5 for c in codes})
    tech = tech + 15.0 * trend
    ext = pd.Series({c: (feats[c].get("ext") or 0.0) / 100.0 for c in codes})
    tech = tech * (1.0 - (ext - 0.5).clip(lower=0.0) * 0.6).clip(lower=0.7)
    p_rs = pct("rs_raw")
    for c in codes:
        feats[c]["rs"] = int(round(1 + 98 * float(p_rs[c])))
        feats[c]["tech"] = round(float(tech[c]), 1)
        feats[c]["score"] = feats[c]["tech"]


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
# 普通株（ADR含む）以外を銘柄名から除外する。大化け候補の母集団に不要なもの：
#   ワラント/権利/ユニット/優先株/社債 … 普通株ではない
#   ファンド(CEF・BDC)/ETN/地方債 … ETFフラグが立たないため素通りしていた（VXX等）
#   SPAC（Acquisition Corp）… $10近辺で動かない箱
# ※「depositary」は ADR（American Depositary Shares）まで落とすため使わない。
#   優先株の預託証券は記号（ACTの$/NASDAQの-サフィックス）と "preferred" で除外できる。
_NON_COMMON_RE = re.compile(
    r"\b(warrants?|rights?|units?|preferred|preference|notes?\s+due|debentures?|"
    r"when[\s-]issued|fund|etns?|exchange[\s-]traded|municipal|"
    r"acquisition\s+(corp|corporation|co|company)|blank\s+check)\b|%",
    re.I)


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
    return not _NON_COMMON_RE.search(name or "")


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
        # 優先株：ACT Symbol は "MS$F"、NASDAQ統合シンボルは "MS-F"（クラス株は "BRK.B" のドット）。
        # 実運用で MS-F / MS-A / SCE-L / MER-K が混入し日足取得に失敗していた。
        if "$" in (row.get("ACT Symbol") or "") or "-" in raw:
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


def _reset_yf_session() -> bool:
    """yfinance のセッション・cookie・crumb を作り直す。

    yfinance はプロセス全体で1つのセッションと crumb を使い回し、cookie はディスクにも
    保存する。数千銘柄の日足取得のあとで Yahoo 側の cookie が失効すると、401 のたびに
    同じ失効 cookie をディスクから読み直すため、以降の Ticker.info がすべて
    Invalid Crumb で失敗する（2026-09-23 run #93: 逐次でも 0/40。同じ run の序盤、
    日足の大量取得前の info は成功していた）。保存済み cookie を消して新しい
    セッションから取り直させる。yfinance 内部に触れるため、失敗しても例外は外に出さない。
    """
    try:
        from yfinance import cache as _yc
        from yfinance.data import YfData
        from yfinance._http import new_session
        _yc.get_cookie_cache().store("curlCffi", None)       # ディスク上の失効 cookie を削除
        yd = YfData()
        yd._set_session(new_session())
        with yd._cookie_lock:
            yd._cookie = None
            yd._crumb = None
            yd._cookie_strategy = "basic"
        return True
    except Exception as e:
        print(f"[globe] yfinanceセッション再作成に失敗: {e}", file=sys.stderr)
        return False


GROWTH_CONFIRM_WAITS = (0, 30)         # 大量取得後の確認: 1回目＋30秒待って1回だけ再試行
GROWTH_CONFIRM_ABORT = 5               # 連続でこの件数失敗したらそのパスを打ち切る（叩き続けると制限が延びる）
GROWTH_INFO_FILE = DOCS / "growth_info.json"   # 先読みした info の保存先（dashboard と一緒にコミットされる）
GROWTH_INFO_TTL_DAYS = 14              # 保存した info の有効期間（時価総額・業績はこの程度なら大きく変わらない）
GROWTH_PREFETCH_N = 80                 # 次回の実行冒頭に先読みする件数（前回のテクニカル上位）


def _info_one(yf, code: str) -> dict | None:
    """1銘柄の info を取り、実データ（quoteType か marketCap）がある時だけ返す。"""
    try:
        info = yf.Ticker(code).info or {}
    except Exception:
        return None
    if not (info.get("quoteType") or info.get("marketCap")):
        return None                   # 401時の yfinance は例外にせず中身の乏しい dict を返す
    return {"mcap": info.get("marketCap"),
            "qtype": str(info.get("quoteType") or "").upper(),
            "sector": info.get("sector") or "",
            "rev_g": info.get("revenueGrowth"),
            "eps_g": info.get("earningsQuarterlyGrowth"),
            "name": info.get("shortName") or info.get("longName") or ""}


def _info_passes(codes: list[str], waits=GROWTH_CONFIRM_WAITS) -> dict[str, dict]:
    """codes の info を取る。各パス前にセッションを作り直し、連続失敗で打ち切って次のパスへ。"""
    if not codes:
        return {}
    import time as _t
    import yfinance as yf
    out: dict[str, dict] = {}
    pending = list(codes)
    for n, wait in enumerate(waits, 1):
        if not pending:
            break
        if wait:
            print(f"growth info: 未取得{len(pending)}銘柄 → {wait}秒待って再試行（{n}回目）", file=sys.stderr)
            _t.sleep(wait)
        _reset_yf_session()
        failed: list[str] = []
        streak = 0
        for i, c in enumerate(pending):
            d = _info_one(yf, c)
            if d:
                out[c] = d
                streak = 0
                continue
            failed.append(c)
            streak += 1
            if streak >= GROWTH_CONFIRM_ABORT:     # 制限中。残りは次のパスへ回す
                failed.extend(pending[i + 1:])
                break
        pending = failed
    return out


def _load_growth_info() -> dict:
    """保存済みの {shortlist, info} を読む。期限切れの info は捨てる。壊れていれば空。"""
    try:
        d = json.loads(GROWTH_INFO_FILE.read_text(encoding="utf-8"))
        cutoff = (datetime.now(tz=JST) - timedelta(days=GROWTH_INFO_TTL_DAYS)).timestamp()
        info = {c: v for c, v in (d.get("info") or {}).items()
                if isinstance(v, dict) and float(v.get("ts", 0)) >= cutoff}
        return {"shortlist": [c for c in (d.get("shortlist") or []) if isinstance(c, str)], "info": info}
    except Exception:
        return {"shortlist": [], "info": {}}


def _save_growth_info(shortlist: list[str], info: dict[str, dict]) -> None:
    try:
        GROWTH_INFO_FILE.write_text(json.dumps(
            {"asof": datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M"),
             "shortlist": shortlist, "info": info},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except Exception as e:
        print(f"[globe] {GROWTH_INFO_FILE.name} 保存失敗: {e}", file=sys.stderr)


def _prefetch_growth_info() -> dict[str, dict]:
    """前回のテクニカル上位の info を、実行の冒頭（日足の大量取得の前）に取っておく。

    数千銘柄の日足を取った直後は Yahoo がこの IP の crumb 取得を 429 で制限し、
    info がすべて 401 になる。30〜90秒待っても解けないことを確認した
    （2026-09-23 run #95: 3パスとも 0/40）。一方、実行冒頭の info は4回とも成功していた。
    上位候補は日ごとの入れ替わりが小さいので、前回の上位をここで取れば大半をまかなえる。
    """
    data = _load_growth_info()
    cache = data["info"]
    targets = [c for c in data["shortlist"][:GROWTH_PREFETCH_N]]
    got = _info_passes(targets, waits=(0,))
    now = datetime.now(tz=JST).timestamp()
    for c, d in got.items():
        cache[c] = {**d, "ts": now}
    print(f"growth prefetch: 前回上位{len(targets)}銘柄のうち{len(got)}銘柄の info を取得"
          f"（保存済み有効 {len(cache)}銘柄）", file=sys.stderr)
    return cache


def _growth_confirm(codes: list[str], cache: dict[str, dict] | None = None) -> dict[str, dict]:
    """上位候補の時価総額・証券種別・業績を確認する。先読み済み(cache)を優先し、足りない分だけ取りに行く。

    取得できなかった銘柄は結果に含めない。新たに取れた分は cache にも書き足す。
    """
    cache = cache if cache is not None else {}
    out = {c: cache[c] for c in codes if c in cache}
    missing = [c for c in codes if c not in out]
    got = _info_passes(missing)
    now = datetime.now(tz=JST).timestamp()
    for c, d in got.items():
        out[c] = d
        cache[c] = {**d, "ts": now}
    print(f"growth confirm: {len(out)}/{len(codes)} 銘柄を確認（保存済み {len(codes) - len(missing)}"
          f" ＋ 今回取得 {len(got)}）", file=sys.stderr)
    return out


def _select_growth(cands: list[str], feats: dict[str, dict], conf: dict[str, dict]) -> list[str]:
    """確認結果で除外・加点して最終順位を返す（cands はテクニカル順）。

    除外: info で「普通株以外」または「時価総額 GROWTH_MCAP_MIN 未満」と確認できた銘柄。
    info が取れなかった銘柄は判定できないので除外せず、verified=False を付けて残す
    （カードに「未確認」と出す）。以前は取得が過半数に届かないと取れた分まで捨てていた。
    業績点: 売上成長率(60%)・四半期EPS成長率(40%)の候補内パーセンタイル（未取得は中立）。
    最終 = 技術75% + 業績25%。
    """
    GROWTH_UNIVERSE_SRC["confirm_total"] = len(cands)
    GROWTH_UNIVERSE_SRC["confirm_n"] = sum(1 for c in cands if c in conf)
    keep = []
    for c in cands:
        d = conf.get(c)
        if d is not None:
            if d["qtype"] and d["qtype"] != "EQUITY":
                continue
            if d["mcap"] is not None and d["mcap"] < GROWTH_MCAP_MIN:
                continue
        keep.append(c)
    if not keep:
        return []

    def pct(key: str, lo: float, hi: float) -> pd.Series:
        v = pd.Series({c: (conf[c].get(key) if c in conf else None) for c in keep},
                      dtype="float64").clip(lo, hi)
        return v.rank(pct=True).fillna(0.5)

    fund = (0.6 * pct("rev_g", -1.0, 5.0) + 0.4 * pct("eps_g", -1.0, 10.0)) * 100.0
    for c in keep:
        f, d = feats[c], conf.get(c)
        f["fund"] = round(float(fund[c]), 1)
        f["score"] = round(0.75 * f["tech"] + 0.25 * f["fund"], 1)
        f["verified"] = d is not None
        if d is not None:
            f["mcap"] = d["mcap"] or 0
            f["sector"] = d["sector"]
            f["rev_g"] = None if d["rev_g"] is None else round(d["rev_g"] * 100.0, 0)
            f["name"] = d["name"]
    return sorted(keep, key=lambda c: (-feats[c]["score"], c))


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


def _analysis_from_frame(code: str, name: str, df: pd.DataFrame,
                        sector: str = "") -> Analysis | None:
    """取得済みの日足からテクニカル分析済みの Analysis を作る。判定不能ならNone。"""
    try:
        close = df["Close"].dropna()
        if len(close) < 60:
            return None
        sc, reasons, lv = technical_score(df)
        a = Analysis(code, name or code, sector, round(float(close.iloc[-1]), 2))
        a.sc, a.g, a.reasons = sc, signal_of(sc), reasons
        if lv:
            a.tgt, a.stp, a.rr, a.ez, a.atr = lv["tgt"], lv["stp"], lv["rr"], lv["ez"], lv.get("atr")
        return a
    except Exception as e:
        print(f"[globe] 分析失敗 {code}: {e}", file=sys.stderr)
        return None


def analyze_all() -> tuple[list[Analysis], dict, list[dict], list[dict], dict, list[Analysis]]:
    # 大化け候補の info は、日足の大量取得で Yahoo に制限される前に先読みしておく
    try:
        growth_info = _prefetch_growth_info()
    except Exception as e:
        print(f"[globe] growth prefetch 失敗: {e}", file=sys.stderr)
        growth_info = {}
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
                a.tgt, a.stp, a.rr, a.ez, a.atr = lv["tgt"], lv["stp"], lv["rr"], lv["ez"], lv.get("atr")
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
                    a.tgt, a.stp, a.rr, a.ez, a.atr = lv["tgt"], lv["stp"], lv["rr"], lv["ez"], lv.get("atr")
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
        feats: dict[str, dict] = {}
        for c in gmeta:
            df = frames.get(c)
            f = growth_features(df) if df is not None else None
            if f:
                feats[c] = f
        rank_growth(feats)                                   # 母集団内の相対順位で採点
        order = sorted(feats, key=lambda c: (-feats[c]["tech"], c))
        shortlist = order[:GROWTH_CONFIRM_N]
        top = _select_growth(shortlist, feats, _growth_confirm(shortlist, growth_info))[:GROWTH_TOP_N]
        _save_growth_info(order[:GROWTH_PREFETCH_N], growth_info)   # 次回の冒頭で先読みする対象

        # 採点した銘柄はすべて検索対象(analyses)に合流させる。1年日足は取得済みなので
        # 追加の通信は発生せず、テクニカル計算だけで検索できる銘柄が数倍になる。
        amap_g = {a.code: a for a in analyses}
        for c in order:
            if c in amap_g:
                continue
            nm = feats[c].get("name") or gmeta[c].get("n") or c
            a = _analysis_from_frame(c, nm, frames[c], feats[c].get("sector") or "")
            if a is not None:
                analyses.append(a)
                amap_g[c] = a
        for c in top:
            a = amap_g.get(c)
            if a is None:
                continue
            a.gr = feats[c]
            if not a.sector and feats[c].get("sector"):
                a.sector = feats[c]["sector"]
            growth.append(a)
        print(f"growth: 採点{len(order)}銘柄を検索対象に合流 / 確認{len(shortlist)} → TOP{len(growth)}",
              file=sys.stderr)
    except Exception as e:
        print(f"[globe] 大化け候補レーダー失敗: {e}", file=sys.stderr)
        traceback.print_exc()

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
        levels = (f'<div class="levels" data-tgt="{a.tgt}" data-stp="{a.stp}"><span class="lv tgt">利確 {_usd(a.tgt)}</span>'
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


def _keyed(section_html: str, key: str, label: str) -> str:
    """セクションに折りたたみ用の識別子（data-key）と目次用の短い名前（data-nav）を付ける。"""
    if not section_html:
        return ""
    return section_html.replace('<section class="sec"',
                                f'<section class="sec" id="sec-{key}" data-key="{key}" data-nav="{_esc(label)}"', 1)


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

    chips = [f'RS {gr["rs"]}' if gr.get("rs") else "RS —"]
    if gr.get("trend_n"):
        chips.append(f'トレンド ✓{gr["trend_pass"]}/{gr["trend_n"]}')
    # 上場1年未満は「1年」ではなく上場来の騰落率（ラベルを偽らない）
    chips.append(f'{"上場来" if gr.get("short_hist") else "1年"} {_pct(gr.get("mom12"))}')
    chips.append(f'6ヶ月 {_pct(gr.get("mom6"))}')
    if gr.get("rev_g") is not None:
        chips.append(f'売上 {_pct(gr["rev_g"])}')
    chips.append(f'買い集め {gr["udv"]:.1f}倍' if gr.get("udv") else "買い集め —")
    chips.append(f'出来高 {gr["vsurge"]:.1f}倍' if gr.get("vsurge") else "出来高 —")
    chips.append(f'ATR {gr["atrp"]:.1f}%' if gr.get("atrp") else "ATR —")
    chips.append(f'52週高値比 {gr["nh"]:.0f}%' if gr.get("nh") else "52週高値比 —")
    if gr.get("verified") is False:
        chips.append("時価総額・業績 未確認")
    chips_html = '<div class="reasons">' + "".join(
        f'<span class="chip">{_esc(c)}</span>' for c in chips) + "</div>"
    warn = ""
    if (gr.get("ext") or 0) > 50:
        warn = (f'<div class="pl dn">⚠ 50日線から+{gr["ext"]:.0f}%乖離（過熱・急落リスク大）</div>')
    seg = _sector_short(a.sector)
    tags = (f'<span class="seg">{_esc(seg)}</span>' if seg else "") + (
        f'<span class="seg">時価総額 ${gr["mcap"]/1e9:.1f}B</span>' if gr.get("mcap") else "")
    gsc = float(gr.get("score") or 0.0)
    return (
        f'<div class="card"><div class="row1"><span class="rank">{rank}</span>'
        f'<div class="title"><span class="code">{_esc(a.code)}</span>'
        f'<span class="name">{_esc(a.name)}</span>{tags}</div>{_badge(a.g)}</div>'
        f'<div class="row2">'
        f'<span class="price" data-px="{_esc(a.code)}" data-usd="{a.price}">{_usd(a.price)}</span>'
        f'<span class="gscore">爆発力 {gsc:.0f}</span>'
        f'<span class="bar"><span class="bar-g" style="width:{_clip(gsc, 0.0, 100.0):.0f}%"></span></span>'
        f'</div>{chips_html}{warn}</div>'
    )


def _growth_section(growth: list[Analysis] | None) -> str:
    if not growth:
        return ""
    cards = "".join(_growth_card(i + 1, a) for i, a in enumerate(growth))
    reduced = ('<span style="color:var(--dn)">⚠ 母集団縮小モード</span> '
               if GROWTH_POOL_REDUCED.get("reduced") else "")
    src = GROWTH_UNIVERSE_SRC.get("src") or "—"
    n_uni, n_sc = GROWTH_UNIVERSE_SRC.get("n_universe", 0), GROWTH_UNIVERSE_SRC.get("n_scored", 0)
    n_ok = GROWTH_UNIVERSE_SRC.get("confirm_n", 0)
    n_all = GROWTH_UNIVERSE_SRC.get("confirm_total", 0)
    if n_all and n_ok == n_all:
        conf = (f'上位{n_all}銘柄はすべて時価総額${GROWTH_MCAP_MIN/1e6:.0f}M以上・普通株であることを確認し、'
                f'売上・利益の伸びを25%加味しています。')
    elif n_ok:
        conf = (f'上位{n_all}銘柄のうち<b>{n_ok}銘柄</b>で時価総額・普通株・業績を確認しました'
                f'（確認できた銘柄のみ超小型株等を除外。「未確認」の銘柄は判定できていません）。')
    else:
        conf = ('<span style="color:var(--dn)">⚠ 銘柄情報を取得できず、時価総額・業績の確認を省略しています。</span>')
    foot = (f'<p class="cfoot">{reduced}母集団：{_esc(src)} <b>{n_uni:,}銘柄</b> → '
            f'株価 ${GROWTH_PX_MIN:.0f}〜${GROWTH_PX_MAX:.0f}未満（2桁株）・'
            f'平均出来高 {GROWTH_VOL_MIN:,}株超で <b>{n_sc:,}銘柄</b>に絞り、'
            f'RS（相対的強さ）・52週高値への近さ・買い集め・出来高急増・値幅(ATR)を母集団内の順位で採点し、'
            f'トレンドテンプレート充足度を加点、50日線から大きく乖離した過熱銘柄は減点。{conf}'
            f'<br><b style="color:var(--dn)">これは「1年で30〜40倍になる銘柄」を予測するものではありません。</b>'
            f'急騰局面に入った銘柄が事前に示しやすい特徴を並べているだけで、'
            f'同じ特徴を持つ銘柄の大半は大化けせず、高ボラティリティは下落幅も大きいことを意味します。'
            f'1銘柄への集中投資は避けてください。</p>')
    return (f'<section class="sec"><h2 class="find"><span>🚀 大化け候補レーダー（2桁株）TOP{GROWTH_TOP_N}</span>'
            f'<em>RS×高値圏×買い集め×業績</em></h2><div class="cards">{cards}</div>{foot}</section>')


# ─────────────────────────────────────────────
#  M&Aシグナル（SEC EDGAR の公開開示）
#   買収の「発表前」を確実に知る方法は無い（未公表の買収情報で売買すればインサイダー取引）。
#   ここで拾うのは公開開示のうち、買収の前兆になりやすいもの（13D・戦略的選択肢の検討）と、
#   買収合意・公開買付けそのものの開示。13D が出ても大半は買収に至らない点に注意。
# ─────────────────────────────────────────────
MA_FILE = DOCS / "ma_signals.json"       # 取得した開示の履歴（前兆→発表の検証に使う）
MA_LOOKBACK_DAYS = 30                    # 表示・取得する期間
MA_HISTORY_DAYS = 180                    # 履歴として残す期間
MA_ROWS_PER_GROUP = 12
# SEC はアクセス元の明示（User-Agent）を求める。連絡先を入れたい場合は
# GitHub の Secrets に SEC_USER_AGENT（例: "名前 you@example.com"）を設定する。
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT") or \
    "GLOBE-ORACLE dashboard (+https://github.com/kohei-omura/GLOBE-ORACLE)"
MA_QUERIES = (   # (種別, 全文検索の語句, 書類の種類)
    ("13d", '"Schedule 13D"', "SC 13D,SCHEDULE 13D"),
    ("strategic", '"strategic alternatives"', "8-K,6-K"),
    ("deal", '"Agreement and Plan of Merger"', "8-K,6-K"),
    # 英国企業の買収は合併契約ではなくスキーム・オブ・アレンジメント（例: SLN は
    # "Agreement and Plan of Merger" に掛からず、契約書の前文で戦略的選択肢側に入っていた）
    ("deal", '"scheme of arrangement"', "8-K,6-K"),
    ("tender", '"Offer to Purchase"', "SC TO-T"),
)
MA_LABEL = {"13d": "13D 5%超取得", "strategic": "戦略的選択肢の検討",
            "deal": "合併契約の開示", "tender": "公開買付け"}
# 8-K の Exhibit 2.1 は買収・合併契約書そのもの（ファイル名 d…dex21.htm / ex2-1.htm 等）
_MA_EX21_RE = re.compile(r"(?:^|[^0-9])ex-?0?2[._-]?0?1(?:[^0-9]|$)|dex21", re.I)
_MA_TICKERS_RE = re.compile(r"\(([A-Z0-9.\-]{1,6}(?:,\s*[A-Z0-9.\-]{1,6})*)\)\s*\(CIK", re.I)


def _sec_search(q: str, forms: str, start: str, end: str, pages: int = 2) -> list[dict]:
    """EDGAR 全文検索（efts.sec.gov）の hits を返す。失敗時は例外。"""
    if requests is None:
        raise RuntimeError("requests が無い")
    import time as _t
    out: list[dict] = []
    for page in range(pages):
        r = requests.get("https://efts.sec.gov/LATEST/search-index",
                         params={"q": q, "forms": forms, "dateRange": "custom",
                                 "startdt": start, "enddt": end, "from": page * 100},
                         headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"},
                         timeout=30)
        if r.status_code != 200:     # 理由を残す（SEC は User-Agent 不備を 403 で返す）
            raise RuntimeError(f"HTTP {r.status_code}: {' '.join(r.text.split())[:160]}")
        hits = ((r.json() or {}).get("hits") or {}).get("hits") or []
        out.extend(hits)
        if len(hits) < 100:
            break
        _t.sleep(0.3)                    # SEC のアクセス上限（毎秒10件）に十分余裕を持たせる
    return out


def _parse_ma_hits(kind: str, hits: list[dict]) -> list[dict]:
    """検索結果を (開示, ティッカー) 単位のレコードにする。ティッカーの無い当事者（ファンド等）は除く。"""
    out: list[dict] = []
    for h in hits:
        src = h.get("_source") or {}
        adsh, _, fname = str(h.get("_id") or "").partition(":")
        adsh = adsh or src.get("adsh") or ""
        names = src.get("display_names") or []
        ciks = src.get("ciks") or []
        for i, dn in enumerate(names):
            m = _MA_TICKERS_RE.search(dn or "")
            if not m:
                continue
            company = (dn.split("(")[0] or "").strip()
            if _NON_COMMON_RE.search(company):           # SPAC（Acquisition Corp）・ファンド等
                continue
            cik = str(ciks[i] if i < len(ciks) else (ciks[0] if ciks else "")).lstrip("0")
            url = (f"https://www.sec.gov/Archives/edgar/data/{cik}/{adsh.replace('-', '')}/{fname}"
                   if cik and adsh and fname else
                   f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")
            k = "deal" if (kind == "strategic" and _MA_EX21_RE.search(fname or "")) else kind
            for tk in m.group(1).split(","):
                tk = _norm_ticker(tk)
                # 優先株（PCG-PX）・SPAC のワラント/ユニット/権利（5文字目 W/U/R）は普通株ではない
                if not tk or "-" in tk or (len(tk) == 5 and tk[-1] in "WUR"):
                    continue
                if tk:
                    out.append({"id": f"{adsh}|{tk}|{k}", "kind": k, "ticker": tk,
                                "name": company, "form": src.get("form") or src.get("file_type") or "",
                                "date": str(src.get("file_date") or "")[:10], "url": url})
    return out


def _ma_keep(r: dict) -> bool:
    """保存済み履歴にも現在の基準を当てる（優先株・SPAC証券・Ex2.1 を戦略的選択肢に入れた旧分類を捨てる）。"""
    tk, name = r.get("ticker", ""), r.get("name", "")
    if "-" in tk or (len(tk) == 5 and tk[-1] in "WUR") or _NON_COMMON_RE.search(name):
        return False
    fname = str(r.get("url", "")).rsplit("/", 1)[-1]
    return not (r.get("kind") == "strategic" and _MA_EX21_RE.search(fname))   # 次の取得で買収合意として入り直す


def fetch_ma_signals() -> dict:
    """直近 MA_LOOKBACK_DAYS 日の M&A 関連開示を取得し、履歴に統合して保存する。

    返り値: {"ok": 取得できた種別数, "total": 種別数, "records": [...（履歴全体）]}
    取得に失敗した種別があっても、保存済みの履歴はそのまま使う。
    """
    try:
        hist = json.loads(MA_FILE.read_text(encoding="utf-8"))
        recs = {r["id"]: r for r in hist.get("records", []) if isinstance(r, dict) and r.get("id")
                and _ma_keep(r)}
    except Exception:
        recs = {}
    today = datetime.now(tz=ET).date()
    start = (today - timedelta(days=MA_LOOKBACK_DAYS)).isoformat()
    ok = 0
    errors: dict[str, str] = {}
    import time as _t
    for kind, q, forms in MA_QUERIES:
        try:
            rows = _parse_ma_hits(kind, _sec_search(q, forms, start, today.isoformat()))
            for r in rows:
                recs.setdefault(r["id"], r)
            ok += 1
            print(f"M&A {kind}: {len(rows)}件", file=sys.stderr)
        except Exception as e:
            errors[kind] = str(e)[:200]
            print(f"[globe] M&A {kind} 取得失敗: {e}", file=sys.stderr)
        _t.sleep(0.3)
    cutoff = (today - timedelta(days=MA_HISTORY_DAYS)).isoformat()
    records = sorted((r for r in recs.values() if r.get("date", "") >= cutoff),
                     key=lambda r: (r.get("date", ""), r["ticker"]), reverse=True)
    try:
        MA_FILE.write_text(json.dumps({"asof": datetime.now(tz=JST).strftime("%Y-%m-%d %H:%M"),
                                       "errors": errors, "records": records},
                                      ensure_ascii=False, separators=(",", ":")),
                           encoding="utf-8")
    except Exception as e:
        print(f"[globe] {MA_FILE.name} 保存失敗: {e}", file=sys.stderr)
    return {"ok": ok, "total": len(MA_QUERIES), "errors": errors, "records": records}


def _ma_section(ma: dict | None, amap: dict, focus: dict[str, str]) -> str:
    """M&A シグナル欄。focus は {ティッカー: 理由（保有中/大化け候補/買い候補）}。"""
    if not ma:
        return ""
    recs = ma.get("records") or []
    cutoff = (datetime.now(tz=ET).date() - timedelta(days=MA_LOOKBACK_DAYS)).isoformat()
    recent = [r for r in recs if r.get("date", "") >= cutoff]
    # 前兆（13D・戦略的選択肢）が先に出ていた銘柄 → 合併契約・公開買付けの行に「前兆」を付ける
    first_pre: dict[str, str] = {}
    for r in recs:
        if r["kind"] in ("13d", "strategic"):
            d = first_pre.get(r["ticker"])
            first_pre[r["ticker"]] = min(d, r["date"]) if d else r["date"]

    def row(r: dict) -> str:
        tk = r["ticker"]; a = amap.get(tk)
        tags = f'<span class="chip ma-{r["kind"]}">{_esc(MA_LABEL.get(r["kind"], r["kind"]))}</span>'
        if tk in focus:
            tags += f'<span class="chip ma-focus">{_esc(focus[tk])}</span>'
        if r["kind"] in ("deal", "tender") and first_pre.get(tk, "9") < r["date"]:
            tags += f'<span class="chip ma-pre">前兆 {first_pre[tk][5:].replace("-", "/")}〜</span>'
        px = f'<span class="ma-px">{_usd(a.price)}</span>' if a else ""
        return (f'<div class="ma-row" data-ma-c="{_esc(tk)}"><span class="ma-date">'
                f'{_esc(r["date"][5:].replace("-", "/"))}</span><span class="code">{_esc(tk)}</span>'
                f'<span class="name">{_esc(r["name"][:40])}</span>{px}{tags}'
                f'<a class="ma-link" href="{_esc(r["url"])}" target="_blank" rel="noopener">開示</a></div>')

    def pick(rows: list[dict]) -> list[dict]:
        """同じ銘柄・種別は最新1件。分析対象の銘柄を先に、各群は新しい順で最大 MA_ROWS_PER_GROUP 件。"""
        seen, uniq = set(), []
        for r in sorted(rows, key=lambda r: (r["date"], r["ticker"] in amap, -len(r["ticker"])), reverse=True):
            key_t, key_n = (r["ticker"], r["kind"]), (r["name"].lower(), r["kind"])
            if key_t not in seen and key_n not in seen:      # 同じ会社の別ティッカー（PERF/PERFF）は1行
                seen.add(key_t); seen.add(key_n); uniq.append(r)
        return sorted(uniq, key=lambda r: r["ticker"] not in amap)[:MA_ROWS_PER_GROUP]

    groups = [("あなたの保有・注目銘柄", [r for r in recent if r["ticker"] in focus]),
              ("13D（5%超取得・経営関与の表明）", [r for r in recent if r["kind"] == "13d"]),
              ("戦略的選択肢の検討（売却を含む）", [r for r in recent if r["kind"] == "strategic"]),
              ("買収合意・公開買付け（発表済み）", [r for r in recent if r["kind"] in ("deal", "tender")])]
    body = "".join(f'<h3 class="ma-h">{_esc(t)}</h3>' + "".join(row(r) for r in pick(rows))
                   for t, rows in groups if rows)
    if not body:
        body = f'<p class="empty">直近{MA_LOOKBACK_DAYS}日に該当する開示はありません。</p>'
    warn = ""
    if ma.get("ok", 0) < ma.get("total", 0):
        warn = (f'<span style="color:var(--dn)">⚠ SEC への問い合わせの一部が失敗しました'
                f'（{ma.get("ok", 0)}/{ma.get("total", 0)}種別のみ最新）'
                f'{"：" + _esc(next(iter((ma.get("errors") or {}).values()), ""))[:90] if ma.get("errors") else ""}'
                f'。</span> ')
    foot = (f'<p class="cfoot">{warn}SEC EDGAR の公開開示から直近{MA_LOOKBACK_DAYS}日分を毎日取得しています。'
            f'13D は投資家が5%超を取得し経営への関与を表明したもの、「戦略的選択肢の検討」は会社が売却を含む'
            f'選択肢を検討していると開示したもので、どちらも買収の前兆になりやすい一方、<b>大半は買収に至りません。</b>'
            f'合併契約の開示は買収側・被買収側の両方を含みます。発表後は株価が買収価格近くまで一気に動くため、'
            f'発表後の売買で得られる利益は通常わずかです。未公表の買収情報は扱いません。</p>')
    return (f'<section class="sec"><h2 class="find"><span>🕵️ M&amp;Aシグナル（SEC開示）</span>'
            f'<em>13D・戦略的選択肢・買収合意</em></h2><div class="ma">{body}</div>{foot}</section>')


CSS_STR = r"""
:root{
--bg:#060a17;--bg2:rgba(255,255,255,.04);--card:rgba(20,30,58,.62);--card2:rgba(26,38,72,.72);
--line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.12);
--fg:#eef2ff;--mut:#8b9bc0;--dim:#5e6e93;
--gold:#f0cf73;--gold2:#c9a54d;--goldglow:rgba(240,207,115,.18);
--buy:#3ddc84;--sell:#ff6b7a;--hold:#8b9bc0;--up:#3ddc84;--dn:#ff6b7a;
--r:18px;--shadow:0 10px 30px rgba(0,0,0,.35),0 1px 0 rgba(255,255,255,.04) inset}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:64px}
body{background:
 radial-gradient(900px 500px at 12% -10%,rgba(80,110,255,.22),transparent 60%),
 radial-gradient(700px 420px at 100% 0%,rgba(240,207,115,.12),transparent 60%),
 radial-gradient(800px 600px at 50% 120%,rgba(61,220,132,.06),transparent 60%),var(--bg);
 background-attachment:fixed;color:var(--fg);
 font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Hiragino Kaku Gothic ProN","Hiragino Sans","Noto Sans JP",sans-serif;
 line-height:1.55;padding-bottom:72px;-webkit-text-size-adjust:100%;-webkit-font-smoothing:antialiased;
 font-feature-settings:"palt" 1}
.wrap{max-width:640px;margin:0 auto;padding:0 16px}
.price,.score,.gscore,.lv,.pl,.idx .v,.idx .c,.ma-px,.hold-sum b,.ma-date,.rank{font-variant-numeric:tabular-nums}
/* ── ヘッダー ── */
header{padding:26px 0 8px;text-align:center}
.brand{font-family:"Didot","Bodoni 72","Times New Roman",serif;font-size:30px;font-weight:700;letter-spacing:.08em;
background:linear-gradient(120deg,#c9a54d 0%,#fff3cf 35%,#f0cf73 55%,#b8913a 100%);-webkit-background-clip:text;background-clip:text;
-webkit-text-fill-color:transparent;filter:drop-shadow(0 2px 14px rgba(240,207,115,.25))}
.brand small{display:block;font-family:-apple-system,sans-serif;font-size:10px;font-weight:600;letter-spacing:.42em;
color:var(--gold2);-webkit-text-fill-color:var(--gold2);margin-top:6px;opacity:.85}
.meta{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;align-items:center;margin-top:14px;font-size:12px;color:var(--mut)}
.mkt{padding:4px 12px;border-radius:999px;border:1px solid var(--line2);font-weight:700;background:var(--bg2)}
.mkt.open{color:var(--up);border-color:rgba(61,220,132,.45);background:rgba(61,220,132,.10);box-shadow:0 0 18px rgba(61,220,132,.18)}
.mkt.closed{color:var(--mut)}
.refresh,.jpy{background:var(--bg2);border:1px solid var(--line2);color:var(--fg);font-size:12px;font-weight:600;
padding:5px 13px;border-radius:999px;cursor:pointer;transition:transform .12s,background .2s}
.refresh:active,.jpy:active{transform:scale(.96)}
.jpy.on{background:var(--goldglow);border-color:rgba(240,207,115,.5);color:var(--gold)}
.idx{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:8px;overflow-x:auto;padding:14px 0 4px;
scrollbar-width:none;-webkit-overflow-scrolling:touch}
.idx::-webkit-scrollbar{display:none}
.idx .i{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 12px;text-align:left;
backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.idx .i .n{font-size:10.5px;color:var(--mut);letter-spacing:.04em}
.idx .i .v{font-size:16px;font-weight:800;margin-top:2px}
.idx .i .c{font-size:12px;font-weight:700}.idx .i .c.up{color:var(--up)}.idx .i .c.dn{color:var(--dn)}
/* ── 目次（固定） ── */
.nav{position:sticky;top:0;z-index:20;display:flex;gap:6px;align-items:center;overflow-x:auto;
margin:12px -16px 4px;padding:10px 16px;background:rgba(6,10,23,.72);backdrop-filter:blur(18px) saturate(1.4);
-webkit-backdrop-filter:blur(18px) saturate(1.4);border-bottom:1px solid var(--line);scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav:empty{display:none}
.nav a,.nav button{flex:0 0 auto;font-size:12px;font-weight:700;color:var(--fg);text-decoration:none;padding:6px 12px;
border-radius:999px;background:var(--bg2);border:1px solid var(--line2);cursor:pointer;font-family:inherit;transition:background .2s,transform .12s}
.nav a:active,.nav button:active{transform:scale(.95)}
.nav a .n{color:var(--gold);margin-left:4px;font-weight:800}
.nav button{color:var(--mut);margin-left:auto}
/* ── セクションと折りたたみ ── */
.sec{margin-top:18px;scroll-margin-top:60px}
h2.find{display:flex;align-items:center;gap:10px;margin:0 0 12px;padding:2px 2px}
h2.find span{font-size:17px;font-weight:800;letter-spacing:.01em}
h2.find em{font-style:normal;font-size:9.5px;font-weight:700;letter-spacing:.2em;color:var(--gold2);text-transform:uppercase}
.sec-h{all:unset;box-sizing:border-box;display:flex;align-items:center;gap:10px;width:100%;cursor:pointer;
padding:12px 14px;margin:0 0 10px;border-radius:14px;background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.02));
border:1px solid var(--line);-webkit-tap-highlight-color:transparent;transition:background .2s}
.sec-h:hover{background:rgba(255,255,255,.06)}
.sec-h:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
.sec-h h2.find{margin:0;flex:1;min-width:0;flex-wrap:wrap;row-gap:2px}
.sec-h .cnt{font-size:11px;font-weight:800;color:var(--gold);background:var(--goldglow);border-radius:999px;padding:2px 9px}
.chev{width:22px;height:22px;flex:0 0 22px;display:grid;place-items:center;border-radius:50%;background:var(--bg2);
color:var(--gold);font-size:11px;transition:transform .28s cubic-bezier(.2,.8,.2,1)}
.sec.collapsed .chev{transform:rotate(-90deg)}
.sec-body{display:grid;grid-template-rows:1fr;transition:grid-template-rows .32s cubic-bezier(.2,.8,.2,1),opacity .25s}
.sec-body>.sec-in{overflow:hidden;min-height:0}
.sec.collapsed .sec-body{grid-template-rows:0fr;opacity:0}
.sec.collapsed .sec-in{visibility:hidden;transition:visibility 0s .32s}
.sec.collapsed .sec-h{margin-bottom:0}
/* ── 検索 ── */
.searchbar{position:relative;margin:16px 0 6px}
#q{width:100%;padding:14px 16px;background:var(--card);border:1px solid var(--line2);border-radius:16px;color:var(--fg);
font-size:15px;outline:none;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);transition:border-color .2s,box-shadow .2s}
#q::placeholder{color:var(--dim)}
#q:focus{border-color:rgba(240,207,115,.55);box-shadow:0 0 0 4px var(--goldglow)}
#hint{font-size:12px;color:var(--mut);margin:4px 4px 0}
#hitcount{font-size:12px;color:var(--gold2);margin:8px 4px 0}
/* ── カード ── */
.cards{display:flex;flex-direction:column;gap:12px}
.card{position:relative;background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:var(--r);
padding:15px 16px;box-shadow:var(--shadow);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
animation:rise .45s cubic-bezier(.2,.8,.2,1) both}
.card::before{content:"";position:absolute;inset:0;border-radius:inherit;padding:1px;pointer-events:none;
background:linear-gradient(140deg,rgba(240,207,115,.28),rgba(255,255,255,.02) 40%,rgba(120,150,255,.12));
-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.row1{display:flex;align-items:center;gap:8px}
.rank{font-family:"Didot","Times New Roman",serif;font-weight:700;color:var(--gold);min-width:22px;font-size:17px}
.title{flex:1;min-width:0;display:flex;align-items:baseline;flex-wrap:wrap;gap:2px 6px}
.code{font-weight:800;font-size:16px;letter-spacing:.02em}
.name{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.seg{font-size:10px;font-weight:700;color:var(--gold2);border:1px solid rgba(240,207,115,.30);border-radius:999px;padding:1px 8px}
.badge{font-size:12px;font-weight:800;padding:3px 11px;border-radius:999px;letter-spacing:.04em}
.badge.buy{background:rgba(61,220,132,.16);color:var(--buy);box-shadow:inset 0 0 0 1px rgba(61,220,132,.35)}
.badge.sell{background:rgba(255,107,122,.16);color:var(--sell);box-shadow:inset 0 0 0 1px rgba(255,107,122,.35)}
.badge.hold{background:rgba(139,155,192,.14);color:var(--hold)}
.badge.src-auto{background:rgba(139,155,192,.14);color:var(--mut)}
.badge.src-media{background:var(--goldglow);color:var(--gold)}
.badge.src-both{background:rgba(61,220,132,.14);color:var(--up)}
.cmemo{font-size:11px;color:var(--mut);margin-left:6px}
.star,.rm{background:none;border:none;color:var(--gold);font-size:19px;cursor:pointer;padding:2px 4px;line-height:1;transition:transform .15s}
.star:active,.rm:active{transform:scale(1.25)}
.rm{color:var(--sell);font-size:16px}
.row2{display:flex;align-items:center;gap:12px;margin-top:10px}
.price{font-size:21px;font-weight:800;letter-spacing:-.01em}
.score{font-size:15px;font-weight:800}.score.pos{color:var(--up)}.score.neg{color:var(--dn)}
.bar{flex:1;height:6px;background:rgba(255,255,255,.07);border-radius:999px;position:relative;overflow:hidden}
.bar-pos{position:absolute;left:50%;top:0;bottom:0;background:linear-gradient(90deg,rgba(61,220,132,.6),var(--up));border-radius:999px}
.bar-neg{position:absolute;top:0;bottom:0;background:linear-gradient(90deg,var(--dn),rgba(255,107,122,.6));border-radius:999px}
.bar-g{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,var(--gold2),var(--gold));border-radius:999px;box-shadow:0 0 12px var(--goldglow)}
.gscore{font-size:14px;font-weight:800;color:var(--gold);white-space:nowrap}
.levels{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px;font-size:12px}
.lv{padding:4px 11px;border-radius:10px;border:1px solid var(--line2);font-weight:700;background:rgba(255,255,255,.02)}
.lv.tgt{color:var(--up);border-color:rgba(61,220,132,.30)}.lv.stp{color:var(--dn);border-color:rgba(255,107,122,.30)}.lv.rr{color:var(--gold)}
.analyst{margin-top:9px;font-size:12.5px;font-weight:800}
.analyst.up{color:var(--up)}.analyst.dn{color:var(--dn)}
.fair{margin-top:6px;font-size:12.5px;font-weight:700;color:var(--mut)}
.fair.up b{color:var(--up)}.fair.dn b{color:var(--dn)}.fair.hold b{color:var(--gold)}
.pl{margin-top:10px;font-size:13.5px;font-weight:800;padding:9px 12px;border-radius:12px;background:rgba(255,255,255,.03);border:1px solid var(--line)}
.pl.up,.pl .up{color:var(--up)}.pl.dn,.pl .dn{color:var(--dn)}
.pl .hit{margin-left:8px;font-size:12px}
.hnote{font-size:11px;color:var(--mut);margin-left:8px;font-weight:600}
.hbasis{display:block;margin:6px 2px 0;font-weight:600}
.hold-sum{margin:0 0 12px;padding:14px 16px;border:1px solid rgba(240,207,115,.22);border-radius:var(--r);
background:linear-gradient(135deg,rgba(240,207,115,.10),rgba(20,30,58,.6));font-size:13px;color:var(--fg);box-shadow:var(--shadow)}
.hold-sum b{font-size:16px}.hold-sum b.up{color:var(--up)}.hold-sum b.dn{color:var(--dn)}
.hold-sum-note{color:var(--mut);font-size:10.5px;margin-left:6px}
.ez{margin-top:10px;font-size:12.5px;font-weight:800;color:var(--fg);background:rgba(255,255,255,.035);border:1px solid var(--line);border-radius:12px;padding:8px 12px}
.ez.hit{background:rgba(61,220,132,.12);border-color:rgba(61,220,132,.5)}
.ez.hit b{color:var(--buy)}
.ezn{margin-left:8px;font-size:11px;font-weight:700;color:var(--mut)}
.fund,.bt,.reasons{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.fchip,.btchip,.chip{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;border:1px solid var(--line2);color:var(--mut);background:rgba(255,255,255,.02)}
.btchip.win{color:var(--up);border-color:rgba(61,220,132,.38)}
.chip{color:#d9be72;border-color:rgba(240,207,115,.22)}
/* ── M&A ── */
.ma{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:var(--r);padding:6px 14px 10px;box-shadow:var(--shadow)}
.ma-h{font-size:11.5px;font-weight:800;color:var(--gold2);letter-spacing:.08em;margin:14px 0 4px}
.ma-row{display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:9px 2px;border-top:1px solid var(--line);font-size:12px}
.ma-row .code{font-size:13.5px}
.ma-row .name{flex:1;min-width:110px}.ma-date{color:var(--dim);min-width:38px;font-weight:700}
.ma-px{font-weight:800}.ma-link{color:var(--gold);font-size:11px;font-weight:700;text-decoration:none;padding:2px 8px;border-radius:999px;border:1px solid rgba(240,207,115,.3)}
.chip.ma-focus,.chip.ma-watch{color:var(--up);border-color:rgba(61,220,132,.45)}
.chip.ma-pre{color:var(--dn);border-color:rgba(255,107,122,.45)}
.ma-row.watched{background:rgba(61,220,132,.06);border-radius:10px}
.cfoot{font-size:11px;color:var(--mut);line-height:1.75;margin:12px 2px 0;padding:10px 12px;background:rgba(255,255,255,.025);border:1px solid var(--line);border-radius:12px}
.empty{color:var(--mut);font-size:13px;padding:16px;text-align:center}
footer{margin-top:34px;padding:18px 0;text-align:center;font-size:11px;color:var(--dim);line-height:1.8;border-top:1px solid var(--line)}
/* ── 取引コーチ ── */
.coach{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:var(--r);padding:14px 16px;box-shadow:var(--shadow)}
.cset{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-end}
.cset label{display:flex;flex-direction:column;gap:4px;font-size:11px;font-weight:700;color:var(--mut)}
.cset .inp{display:flex;align-items:center;gap:6px;color:var(--fg);font-size:12px}
.cset input,.cset select{background:rgba(0,0,0,.25);border:1px solid var(--line2);border-radius:10px;color:var(--fg);font-size:15px;
padding:8px 10px;font-family:inherit;font-variant-numeric:tabular-nums;outline:none}
.cset input{width:140px}.cset input:focus,.cset select:focus{border-color:rgba(240,207,115,.55);box-shadow:0 0 0 3px var(--goldglow)}
.cset .cnote{flex-basis:100%;margin:0}
.rules{margin:14px 0 4px;padding:12px 14px 12px 32px;border-radius:12px;background:rgba(240,207,115,.06);border:1px solid rgba(240,207,115,.18);font-size:12.5px;line-height:1.75}
.rules b{color:var(--gold)}
.cimp{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.cbtn{position:relative;overflow:hidden;display:inline-flex;align-items:center;font-size:13px;font-weight:800;padding:9px 14px;border-radius:12px;cursor:pointer;
color:#1a1405;background:linear-gradient(135deg,#f6dd8f,#c9a54d);border:none;font-family:inherit;box-shadow:0 4px 16px rgba(240,207,115,.22)}
.cbtn input{position:absolute;inset:0;opacity:0;cursor:pointer}
.cbtn.ghost{background:var(--bg2);color:var(--mut);border:1px solid var(--line2);box-shadow:none}
.cnote{font-size:11px;color:var(--mut);margin:8px 2px 0;line-height:1.7}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:14px}
.kpi{background:rgba(255,255,255,.03);border:1px solid var(--line);border-radius:12px;padding:9px 10px}
.kl{font-size:10px;color:var(--mut);font-weight:700}
.kv{font-size:16px;font-weight:800;font-variant-numeric:tabular-nums;margin-top:2px;white-space:nowrap}
.kv small{font-size:10px;color:var(--mut);margin-left:4px;font-weight:700}
.kv.up{color:var(--up)}.kv.dn{color:var(--dn)}
.diag{list-style:none;margin-top:12px;display:flex;flex-direction:column;gap:6px}
.diag li{font-size:12.5px;line-height:1.6;padding:8px 12px 8px 34px;border-radius:12px;position:relative;border:1px solid var(--line)}
.diag li::before{position:absolute;left:11px;top:8px}
.diag li.warn{background:rgba(255,107,122,.07);border-color:rgba(255,107,122,.25)}.diag li.warn::before{content:"⚠"}
.diag li.good{background:rgba(61,220,132,.07);border-color:rgba(61,220,132,.25)}.diag li.good::before{content:"✓";color:var(--up);font-weight:900}
.trs{margin-top:12px}
.tr-row{display:grid;grid-template-columns:40px 1fr 42px 34px 56px 76px;align-items:center;gap:6px;padding:8px 2px;border-top:1px solid var(--line);font-size:12px;font-variant-numeric:tabular-nums}
.tr-row .code{font-size:13.5px}.tac{font-size:10px;font-weight:700;color:var(--mut);text-align:center;border:1px solid var(--line2);border-radius:999px;padding:1px 0}
.tac.nisa{color:var(--gold);border-color:rgba(240,207,115,.35)}.trd{color:var(--mut);text-align:right}
.trp,.trv{text-align:right;font-weight:800}.trp.up,.trv.up{color:var(--up)}.trp.dn,.trv.dn{color:var(--dn)}
.szw{margin-top:10px;display:flex;flex-direction:column;gap:5px}
.sz{font-size:12.5px;font-weight:700;padding:7px 12px;border-radius:12px;background:rgba(120,150,255,.08);border:1px solid rgba(120,150,255,.22)}
.sz b{color:var(--gold);font-size:14px}.szn{margin-left:8px;font-size:11px;color:var(--mut)}
.sz.warn{background:rgba(255,107,122,.08);border-color:rgba(255,107,122,.3);color:#ffb3bb}
.sz.mute{background:none;border:none;padding:0 2px;font-size:11px;color:var(--mut);font-weight:600}
@media (max-width:420px){.kpis{grid-template-columns:repeat(2,1fr)}}
@media (min-width:700px){.wrap{max-width:720px}.price{font-size:23px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}html{scroll-behavior:auto}}
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

  /* 銘柄名・理由などは外部データ由来。innerHTML に入れる前に必ずエスケープする */
  function esc(x) {
    return String(x == null ? '' : x).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
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
  function starBtn(c) { var on = inWatch(c); return '<button class="star' + (on ? ' on' : '') + '" data-star="' + esc(c) + '">' + (on ? '★' : '☆') + '</button>'; }

  function card(s, mode) {
    var scls = s.sc >= 0 ? 'pos' : 'neg';
    var seg = s.m ? '<span class="seg">' + esc(s.m) + '</span>' : '';
    var levels = '';
    if (s.t && s.st) {
      levels = '<div class="levels" data-tgt="' + esc(s.t) + '" data-stp="' + esc(s.st) + '"><span class="lv tgt">利確 ' + fmtMoney(s.t) + '</span>' +
        '<span class="lv stp">損切 ' + fmtMoney(s.st) + '</span>' +
        (s.rr ? '<span class="lv rr">RR ' + s.rr + '</span>' : '') + '</div>';
    }
    var an = (s.tp != null) ? '<div class="analyst ' + (s.tp >= 0 ? 'up' : 'dn') + '">プロ予想 ' + (s.tp >= 0 ? '+' : '') + s.tp + '%</div>' : '';
    var fair = (s.val) ? '<div class="fair ' + (s.val === '割安' ? 'up' : s.val === '割高' ? 'dn' : 'hold') + '">理論株価 <b>' + esc(s.val) + '</b>（' + (s.fg >= 0 ? '+' : '') + s.fg + '%）</div>' : '';
    var reasons = (s.r && s.r.length) ? '<div class="reasons">' + s.r.map(function (r) { return '<span class="chip">' + esc(r) + '</span>'; }).join('') + '</div>' : '';
    var c = esc(s.c);
    var rm = (mode === 'watch') ? '<button class="rm" data-rm="' + c + '">×</button>' : '';
    return '<div class="card"><div class="row1"><span class="rank">' + (s.rk || '-') + '</span>' +
      '<div class="title"><span class="code">' + c + '</span><span class="name">' + esc(s.n) + '</span>' + seg + '</div>' +
      badge(s.g) + starBtn(s.c) + rm + '</div>' +
      '<div class="row2"><span class="price" data-px="' + c + '" data-usd="' + esc(s.p) + '">' + fmtMoney(s.p) + '</span>' +
      '<span class="score ' + scls + '">' + (s.sc >= 0 ? '+' : '') + s.sc + '</span>' + bar(s.sc) + '</div>' +
      levels + an + fair + reasons +
      '<div class="reasons"><span class="chip">スコア順 ' + (s.rk || '-') + ' 位 / ' + TOTAL + ' 銘柄</span></div></div>';
  }
  /* 検索キー（社名＋ティッカー）はクライアントで生成してキャッシュ（stocks.json を軽くするため） */
  function skey(s) { return s._k || (s._k = norm(s.n + ' ' + s.c)); }
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
    /* 1,800銘柄超ではスコア順だけだと "MU" 検索で MU 自体が9位以下に埋もれる。
       ティッカー完全一致 → ティッカー前方一致 → 社名一致 の順に並べ、同グループ内はスコア順。
       全角入力（ＮＶＤＡ）も NFKC で半角に揃えて照合する。 */
    var v = norm(raw), hits = [];
    for (var i = 0; i < STOCKS.length; i++) {
      var s = STOCKS[i], c = s.c.toLowerCase(), g;
      if (c === v) g = 0; else if (c.indexOf(v) === 0) g = 1; else if (skey(s).indexOf(v) >= 0) g = 2; else continue;
      hits.push([g, s]);
    }
    hits.sort(function (a, b) { return a[0] - b[0] || b[1].sc - a[1].sc; });
    var m = hits.map(function (x) { return x[1]; });
    var shown = m.slice(0, 8);
    if (hitEl) hitEl.textContent = m.length ? (m.length + '件ヒット / 上位' + shown.length + '件') : '';
    results.innerHTML = shown.length ? shown.map(function (s) { return card(s, 'search'); }).join('') : '<p class="empty">該当なし。社名(apple)やティッカー(AAPL)で検索してください。</p>';
    renderSizing();
  }
  var deb = null; function runDebounced() { if (deb) clearTimeout(deb); deb = setTimeout(run, 150); }
  if (q) { q.addEventListener('focus', function () { ensureStocks(); }); q.addEventListener('input', function () { ensureStocks(); runDebounced(); }); }

  var watchSec = null, watchResults = null;
  function ensureWatchSec() {
    if (watchSec) return;
    watchSec = document.createElement('section'); watchSec.id = 'watch-sec'; watchSec.className = 'sec'; watchSec.style.display = 'none';
    watchSec.setAttribute('data-key', 'watch'); watchSec.setAttribute('data-nav', 'ウォッチ');
    watchSec.innerHTML = '<h2 class="find"><span>ウォッチリスト</span><em>WATCHLIST</em></h2><div id="watch-results" class="cards"></div>';
    if (searchSec && searchSec.parentNode) searchSec.parentNode.insertBefore(watchSec, searchSec.nextSibling);
    watchResults = watchSec.querySelector('#watch-results'); makeFoldable(watchSec);
  }
  function renderWatch() {
    ensureWatchSec(); var w = getWatch();
    if (!w.length) { watchSec.style.display = 'none'; if (watchResults) watchResults.innerHTML = ''; buildNav(); return; }
    if (!STOCKS) { ensureStocks(); return; }
    var cards = []; for (var i = 0; i < w.length; i++) { var s = byCode(w[i]); if (s) cards.push(card(s, 'watch')); }
    watchSec.style.display = cards.length ? '' : 'none'; if (watchResults) watchResults.innerHTML = cards.join('');
    updateCount(watchSec); buildNav(); renderSizing();
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
  /* M&Aシグナル欄でウォッチ中の銘柄を強調（ウォッチはこの端末のブラウザにだけ保存されている） */
  function markMaWatch() {
    document.querySelectorAll('[data-ma-c]').forEach(function (el) {
      var on = inWatch(el.getAttribute('data-ma-c')), tag = el.querySelector('.ma-watch');
      el.classList.toggle('watched', on);
      if (on && !tag) { var t = document.createElement('span'); t.className = 'chip ma-watch'; t.textContent = '★ウォッチ中'; el.insertBefore(t, el.querySelector('.ma-link')); }
      if (!on && tag) tag.remove();
    });
  }
  function syncStars() { var btns = document.querySelectorAll('[data-star]'); for (var i = 0; i < btns.length; i++) { var c = btns[i].getAttribute('data-star'), on = inWatch(c); btns[i].className = 'star' + (on ? ' on' : ''); btns[i].textContent = on ? '★' : '☆'; } }
  document.addEventListener('click', function (ev) {
    var t = ev.target; if (!t || !t.getAttribute) return;
    var sc = t.getAttribute('data-star'); if (sc) { toggleWatch(sc); syncStars(); renderWatch(); markMaWatch(); return; }
    var rm = t.getAttribute('data-rm'); if (rm) { toggleWatch(rm); syncStars(); renderWatch(); markMaWatch(); return; }
  });

  /* ---- USD⇄JPY トグル ---- */
  function reformatMoney() {
    document.querySelectorAll('[data-usd]').forEach(function (el) { el.textContent = fmtMoney(el.getAttribute('data-usd')); });
    if (q && q.value.trim()) run(); renderWatch(); renderSizing(); renderTrades();
  }
  function addJpyBtn() {
    var meta = document.querySelector('header .meta'); if (!meta || document.getElementById('jpybtn')) return;
    var b = document.createElement('button'); b.id = 'jpybtn'; b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.type = 'button';
    b.textContent = jpyOn() ? '¥ 円' : '$ ドル';
    b.addEventListener('click', function () { try { if (jpyOn()) localStorage.removeItem(JPY_KEY); else localStorage.setItem(JPY_KEY, '1'); } catch (e) {} b.className = 'jpy' + (jpyOn() ? ' on' : ''); b.textContent = jpyOn() ? '¥ 円' : '$ ドル'; reformatMoney(); });
    meta.appendChild(b);
  }

  /* ---- ライブ価格 ---- */
  /* 保有カードの損益と利確・損切の到達状況（サーバ側 _hold_pl_html と同じ表示） */
  function holdPl(price, avg, tgt, stp) {
    var pct = avg ? Math.round((price - avg) / avg * 1000) / 10 : 0, cls = pct >= 0 ? 'up' : 'dn', hit = '';
    if (stp && price <= stp) hit = '<b class="hit dn">⚠ 損切ライン到達</b>';
    else if (tgt && price >= tgt) hit = '<b class="hit up">✅ 利確ライン到達</b>';
    var dist = (!hit && price > 0 && tgt && stp) ? '<span class="hnote">利確まで +' + ((tgt - price) / price * 100).toFixed(1) +
      '% ／ 損切まで -' + ((price - stp) / price * 100).toFixed(1) + '%</span>' : '';
    return '<span class="' + cls + '">損益 ' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%</span>' + hit +
      '<span class="hnote">買値 ' + fmtMoney(avg) + ' → 現値 ' + fmtMoney(price) + '</span>' + dist;
  }
  function applyPrices(map) {
    /* 損益の行も現値に追従させる（以前は生成時の株価のままで、上の現値と食い違っていた） */
    document.querySelectorAll('[data-hold-c]').forEach(function (el) {
      var c = el.getAttribute('data-hold-c'); if (map[c] == null) return;
      el.innerHTML = holdPl(Number(map[c]), parseFloat(el.getAttribute('data-avg')) || 0,
        parseFloat(el.getAttribute('data-tgt')) || 0, parseFloat(el.getAttribute('data-stp')) || 0);
    });
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
        setMktStatus(); if (q && q.value.trim()) run(); renderWatch(); renderSizing();
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


  /* ── 取引コーチ：資金管理（推奨株数）・手数料チェック・取引成績（すべて端末内で完結） ──
     ※ 取引履歴は個人情報のため、リポジトリ（公開）には一切送らず localStorage にだけ保存する。 */
  var RISK_KEY = 'go_risk_v1', TR_KEY = 'go_trades_v1';
  var FEE = 0.00495, FEE_RT = 0.0099, MAX_POS = 0.25, MIN_EDGE = 2.0;   /* SBI米国株 0.495%(税込)/片道 */
  function rateNow() { return RATE > 0 ? RATE : 150; }
  function yen(v) { return '¥' + Math.round(v).toLocaleString(); }
  function getRisk() { try { var o = JSON.parse(localStorage.getItem(RISK_KEY) || 'null'); return (o && o.cap > 0) ? o : { cap: 0, pct: 1 }; } catch (e) { return { cap: 0, pct: 1 }; } }
  function setRisk(o) { try { localStorage.setItem(RISK_KEY, JSON.stringify(o)); } catch (e) {} }

  function sizingHtml(px, tgt, stp) {
    if (!(px > 0 && stp > 0 && stp < px)) return '';
    var r = getRisk(), R = rateNow(), gain = (tgt / px - 1) * 100, loss = (1 - stp / px) * 100, out = [];
    if (r.cap > 0) {
      var perShare = (px - stp) + px * FEE_RT, riskYen = r.cap * r.pct / 100;
      var byRisk = Math.floor(riskYen / R / perShare), byCap = Math.floor(r.cap * MAX_POS / R / px), n = Math.min(byRisk, byCap);
      if (n < 1) out.push('<div class="sz warn">📐 1株でも許容損失（' + yen(riskYen) + '）を超えます → 見送り推奨</div>');
      else out.push('<div class="sz">📐 推奨 <b>' + n + '株</b>（約' + yen(n * px * R) + '・資金の' + Math.round(n * px * R / r.cap * 100) + '%）' +
        '<span class="szn">損切り時 −' + yen(n * perShare * R) + (byCap < byRisk ? '・1銘柄25%上限' : '') + '</span></div>');
    } else out.push('<div class="sz mute">📐 「取引コーチ」で運用資金を入れると推奨株数を表示します</div>');
    if (gain > 0 && gain < MIN_EDGE) out.push('<div class="sz warn">⚠ 利確幅 +' + gain.toFixed(1) + '% は特定口座の往復手数料（約1%）で半分以上消えます</div>');
    else if (gain > 0) out.push('<div class="sz mute">手数料込み（特定）：利確 +' + (gain - FEE_RT * 100).toFixed(1) + '% ／ 損切 −' + (loss + FEE_RT * 100).toFixed(1) + '%</div>');
    return out.join('');
  }
  function renderSizing() {
    document.querySelectorAll('.card .levels[data-stp]').forEach(function (lv) {
      var cardEl = lv.closest('.card'); if (!cardEl || lv.closest('#sec-hold')) return;
      var pe = cardEl.querySelector('.price[data-usd]'); var px = pe ? parseFloat(pe.getAttribute('data-usd')) : 0;
      var old = cardEl.querySelector('.szw'); if (old) old.parentNode.removeChild(old);
      var h = sizingHtml(px, parseFloat(lv.getAttribute('data-tgt')) || 0, parseFloat(lv.getAttribute('data-stp')) || 0); if (!h) return;
      var w = document.createElement('div'); w.className = 'szw'; w.innerHTML = h; lv.parentNode.insertBefore(w, lv.nextSibling);
    });
  }

  /* ---- SBI 約定履歴CSV（Shift_JIS）の読み込み ---- */
  function csvLine(line) {
    var out = [], cur = '', inq = false;
    for (var i = 0; i < line.length; i++) {
      var ch = line[i];
      if (inq) { if (ch === '"') { if (line[i + 1] === '"') { cur += '"'; i++; } else inq = false; } else cur += ch; }
      else if (ch === '"') inq = true; else if (ch === ',') { out.push(cur); cur = ''; } else cur += ch;
    }
    out.push(cur); return out.map(function (x) { return x.trim(); });
  }
  function parseSbi(text) {
    var lines = String(text || '').split(/\r?\n/), hi = -1;
    for (var i = 0; i < lines.length; i++) { if (lines[i].indexOf('約定日') >= 0 && lines[i].indexOf('取引') >= 0) { hi = i; break; } }
    if (hi < 0) return null;
    var H = csvLine(lines[hi]);
    function col(names) { for (var j = 0; j < names.length; j++) { for (var k = 0; k < H.length; k++) { if (H[k].indexOf(names[j]) >= 0) return k; } } return -1; }
    var C = { d: col(['国内約定日', '約定日']), cur: col(['通貨']), nm: col(['銘柄']), side: col(['取引']), ac: col(['預り']),
      q: col(['約定数量', '数量']), px: col(['約定単価', '単価']), amt: col(['受渡金額']) };
    if (C.d < 0 || C.nm < 0 || C.side < 0 || C.q < 0 || C.px < 0) return null;
    var rows = [];
    for (var r = hi + 1; r < lines.length; r++) {
      if (!lines[r].trim()) continue;
      var f = csvLine(lines[r]), dm = (f[C.d] || '').match(/(\d{4})\D+(\d{1,2})\D+(\d{1,2})/);
      var tm = (f[C.nm] || '').match(/([A-Z][A-Z0-9.\-]{0,6})\s*\/\s*[A-Za-z][^\/]*$/);
      var sd = f[C.side] || '', side = sd.indexOf('買') >= 0 ? 'B' : (sd.indexOf('売') >= 0 ? 'S' : '');
      var qn = parseFloat(String(f[C.q]).replace(/,/g, '')), pn = parseFloat(String(f[C.px]).replace(/,/g, ''));
      if (!dm || !tm || !side || !(qn > 0) || !(pn > 0)) continue;
      rows.push({ d: dm[1] + '-' + ('0' + dm[2]).slice(-2) + '-' + ('0' + dm[3]).slice(-2), c: tm[1], s: side,
        a: (f[C.ac] || '').indexOf('NISA') >= 0 ? 'NISA' : ((f[C.ac] || '特定').replace(/預り/, '') || '特定'),
        j: (C.cur >= 0 && (f[C.cur] || '').indexOf('円') >= 0) ? 1 : 0, q: qn, p: pn,
        m: C.amt >= 0 ? parseFloat(String(f[C.amt]).replace(/,/g, '')) || 0 : 0 });
    }
    return rows;
  }
  function getTrades() { try { return JSON.parse(localStorage.getItem(TR_KEY) || '[]') || []; } catch (e) { return []; } }
  function rowKey(x) { return [x.d, x.c, x.s, x.a, x.q, x.p].join('|'); }
  function mergeTrades(add) {
    var cur = getTrades(), seen = {}; cur.forEach(function (x) { seen[rowKey(x)] = 1; });
    var n = 0; add.forEach(function (x) { if (!seen[rowKey(x)]) { seen[rowKey(x)] = 1; cur.push(x); n++; } });
    try { localStorage.setItem(TR_KEY, JSON.stringify(cur)); } catch (e) {}
    return n;
  }
  function daysBetween(a, b) { return Math.round((Date.parse(b) - Date.parse(a)) / 86400000); }
  /* 先入れ先出しで買いと売りを対応付け。ドル決済は受渡金額（手数料込み）、円貨決済は単価から推計。 */
  function pairTrades(rows) {
    var idx = rows.map(function (x, i) { return [x, i]; });
    idx.sort(function (A, B) { var a = A[0], b = B[0]; return a.d < b.d ? -1 : a.d > b.d ? 1 : (a.s === b.s ? B[1] - A[1] : (a.s === 'B' ? -1 : 1)); });
    var lots = {}, closed = [], unmatched = 0;
    idx.forEach(function (it) {
      var x = it[0], k = x.c + '|' + x.a, nisa = x.a === 'NISA';
      var feeUsd = x.j ? (nisa ? 0 : x.q * x.p * FEE) : (x.m > 0 ? Math.abs(x.m - x.q * x.p) : (nisa ? 0 : x.q * x.p * FEE));
      if (feeUsd > x.q * x.p * 0.02) feeUsd = nisa ? 0 : x.q * x.p * FEE;   /* 受渡金額の解釈違いに対する保険 */
      if (x.s === 'B') { (lots[k] = lots[k] || []).push({ d: x.d, q: x.q, cps: x.p + feeUsd / x.q, fps: feeUsd / x.q }); return; }
      var pps = x.p - feeUsd / x.q, rem = x.q, L = lots[k] || [];
      while (rem > 1e-9 && L.length) {
        var lot = L[0], m = Math.min(rem, lot.q);
        closed.push({ c: x.c, a: x.a, bd: lot.d, sd: x.d, q: m, cost: m * lot.cps, proc: m * pps, fee: m * (lot.fps + feeUsd / x.q), days: daysBetween(lot.d, x.d) });
        lot.q -= m; rem -= m; if (lot.q <= 1e-9) L.shift();
      }
      if (rem > 1e-9) unmatched++;
    });
    var open = []; Object.keys(lots).forEach(function (k) { lots[k].forEach(function (l) { if (l.q > 1e-9) open.push({ c: k.split('|')[0], a: k.split('|')[1], d: l.d, q: l.q, cps: l.cps }); }); });
    closed.forEach(function (t) { t.pl = t.proc - t.cost; t.pct = (t.proc / t.cost - 1) * 100; });
    return { closed: closed, open: open, unmatched: unmatched };
  }
  function avg(a) { return a.length ? a.reduce(function (s, v) { return s + v; }, 0) / a.length : 0; }
  function diagnose(cl) {
    var W = cl.filter(function (t) { return t.pct > 0.05; }), Ls = cl.filter(function (t) { return t.pct < -0.05; });
    var gw = W.reduce(function (s, t) { return s + t.pl; }, 0), fee = cl.reduce(function (s, t) { return s + t.fee; }, 0);
    var aw = avg(W.map(function (t) { return t.pct; })), al = avg(Ls.map(function (t) { return t.pct; }));
    var out = [];
    if (fee > 0 && gw > 0 && fee / (gw + fee) > 0.2) out.push(['warn', '手数料（' + fmtMoney(fee) + '）が勝ち取引の利益の ' + Math.round(fee / (gw + fee) * 100) + '% を食っています。特定口座は利確幅 +2% 以上の取引だけに絞りましょう。']);
    if (W.length && Ls.length && Math.abs(al) > aw) out.push(['warn', '平均の負け（' + al.toFixed(1) + '%）が平均の勝ち（+' + aw.toFixed(1) + '%）より大きい「損大利小」です。損切りは買う前に決めた価格で機械的に。']);
    var big = Ls.filter(function (t) { return t.pct <= -8; });
    if (big.length) out.push(['warn', '−8% を超えて持ち続けた負け：' + big.map(function (t) { return t.c + ' ' + t.pct.toFixed(1) + '%'; }).join('、') + '。−8%で損切りしていれば、損失を約 ' + fmtMoney(big.reduce(function (s, t) { return s + (-t.pct - 8) / 100 * t.cost; }, 0)) + ' 減らせました。']);
    var sw = avg(W.map(function (t) { return t.cost; })), sl = avg(Ls.map(function (t) { return t.cost; }));
    if (W.length && Ls.length && sl > sw * 1.3) out.push(['warn', '負けた取引の平均投資額（' + fmtMoney(sl) + '）が勝ちの ' + (sl / sw).toFixed(1) + ' 倍。大きく張った時ほど負けています → 推奨株数で金額をそろえましょう。']);
    var ns = cl.filter(function (t) { return t.a === 'NISA' && t.days < 60; });
    if (ns.length) out.push(['warn', 'NISAで ' + ns.length + ' 回の短期売買（60日未満）。NISAは売っても枠が年内に戻らず、損失は他と相殺できません。NISAは長期保有向けに。']);
    var dt = cl.filter(function (t) { return t.days <= 1; });
    if (dt.length) out.push(['warn', '買って1日以内に売った取引が ' + dt.length + ' 回（' + dt.map(function (t) { return t.c; }).join('、') + '）。手数料だけ払う結果になりやすいので避けましょう。']);
    var good = Ls.filter(function (t) { return t.pct > -6 && t.days > 1; });
    if (good.length) out.push(['good', '小さく負けて撤退できた取引：' + good.map(function (t) { return t.c + ' ' + t.pct.toFixed(1) + '%'; }).join('、') + '。この損切りは正解です。']);
    if (W.length) out.push(['good', '勝ちパターン：平均 +' + aw.toFixed(1) + '%・平均 ' + Math.round(avg(W.map(function (t) { return t.days; }))) + ' 日保有。']);
    return out;
  }
  function renderTrades() {
    var box = document.getElementById('trstats'); if (!box) return;
    var rows = getTrades();
    if (!rows.length) { box.innerHTML = '<p class="cnote">まだ取引データがありません。SBI証券の外国株式「約定履歴」でCSVをダウンロードし、上のボタンから読み込んでください。</p>'; return; }
    var P = pairTrades(rows), cl = P.closed.slice().sort(function (a, b) { return a.sd < b.sd ? 1 : -1; });
    var W = cl.filter(function (t) { return t.pct > 0.05; }), Ls = cl.filter(function (t) { return t.pct < -0.05; });
    var pl = cl.reduce(function (s, t) { return s + t.pl; }, 0), fee = cl.reduce(function (s, t) { return s + t.fee; }, 0);
    var aw = avg(W.map(function (t) { return t.pct; })), al = avg(Ls.map(function (t) { return t.pct; }));
    var wr = cl.length ? W.length / cl.length : 0, ev = wr * aw + (cl.length ? Ls.length / cl.length : 0) * al;
    function kpi(label, val, cls) { return '<div class="kpi"><div class="kl">' + label + '</div><div class="kv ' + (cls || '') + '">' + val + '</div></div>'; }
    var h = '<div class="kpis">' +
      kpi('実現損益', (pl >= 0 ? '+' : '−') + fmtMoney(Math.abs(pl)), pl >= 0 ? 'up' : 'dn') +
      kpi('勝率', Math.round(wr * 100) + '%<small>' + W.length + '勝' + Ls.length + '敗' + (cl.length - W.length - Ls.length ? (cl.length - W.length - Ls.length) + '分' : '') + '</small>') +
      kpi('平均の勝ち', W.length ? '+' + aw.toFixed(1) + '%' : '—', 'up') +
      kpi('平均の負け', Ls.length ? al.toFixed(1) + '%' : '—', 'dn') +
      kpi('損益比', (W.length && Ls.length) ? (aw / Math.abs(al)).toFixed(2) : '—', (W.length && Ls.length && aw / Math.abs(al) >= 1.5) ? 'up' : 'dn') +
      kpi('1回の期待値', (ev >= 0 ? '+' : '') + ev.toFixed(2) + '%', ev >= 0 ? 'up' : 'dn') +
      kpi('手数料合計', fmtMoney(fee)) +
      kpi('平均保有', Math.round(avg(cl.map(function (t) { return t.days; }))) + '日') + '</div>';
    var dg = diagnose(cl);
    if (dg.length) h += '<ul class="diag">' + dg.map(function (d) { return '<li class="' + d[0] + '">' + esc(d[1]) + '</li>'; }).join('') + '</ul>';
    h += '<div class="trs">' + cl.map(function (t) {
      return '<div class="tr-row"><span class="ma-date">' + t.sd.slice(5).replace('-', '/') + '</span><span class="code">' + esc(t.c) + '</span>' +
        '<span class="tac ' + (t.a === 'NISA' ? 'nisa' : '') + '">' + esc(t.a) + '</span><span class="trd">' + t.days + '日</span>' +
        '<span class="trp ' + (t.pl >= 0 ? 'up' : 'dn') + '">' + (t.pct >= 0 ? '+' : '') + t.pct.toFixed(1) + '%</span>' +
        '<span class="trv ' + (t.pl >= 0 ? 'up' : 'dn') + '">' + (t.pl >= 0 ? '+' : '−') + fmtMoney(Math.abs(t.pl)) + '</span></div>';
    }).join('') + '</div>';
    if (P.open.length) h += '<p class="cnote">保有中（未決済）：' + P.open.map(function (o) { return esc(o.c) + ' ' + o.q + '株 @' + fmtMoney(o.cps); }).join('、') + '</p>';
    h += '<p class="cnote">損益はドル建て（為替差損益を含まず、円貨決済は手数料を推計）。SBIの円建て損益とは為替の分だけ差が出ます。' +
      (P.unmatched ? ' 買付が期間外の売却 ' + P.unmatched + ' 件は集計対象外です。' : '') + '</p>';
    box.innerHTML = h;
  }
  function renderCoach() {
    var box = document.getElementById('coach'); if (!box) return;
    var r = getRisk();
    box.innerHTML =
      '<div class="cset"><label>運用資金<span class="inp"><input id="capin" type="text" inputmode="numeric" placeholder="例 1000000" value="' + (r.cap || '') + '">円</span></label>' +
      '<label>1回の許容損失<select id="riskin">' + [0.5, 1, 2].map(function (v) { return '<option value="' + v + '"' + (r.pct == v ? ' selected' : '') + '>' + v + '%</option>'; }).join('') + '</select></label>' +
      '<div id="riskout" class="cnote"></div></div>' +
      '<ol class="rules">' +
      '<li><b>1回の損失は資金の1%まで</b> — 各カードの「📐 推奨株数」で買う</li>' +
      '<li><b>損切りは買う前に決める</b> — 損切ライン到達で即売り。−8%を超えて持たない</li>' +
      '<li><b>特定口座は利確幅 +2% 以上だけ</b> — 往復手数料が約1%かかる</li>' +
      '<li><b>NISAは長期保有用</b> — 短期売買は枠を消費し、損失も通算できない</li>' +
      '<li><b>1銘柄は資金の25%まで</b>・<b>当日の往復売買はしない</b></li></ol>' +
      '<div class="cimp"><label class="cbtn">📥 SBIの約定履歴CSVを読み込む<input id="trfile" type="file"></label>' +
      '<button type="button" id="trclr" class="cbtn ghost">消去</button></div>' +
      '<p class="cnote">CSVはこの端末の中だけで処理・保存され、どこにも送信されません。</p><div id="trstats"></div>';
    function riskOut() {
      var o = getRisk(), el = document.getElementById('riskout'); if (!el) return;
      el.textContent = o.cap > 0 ? '→ 1回の最大損失 ' + yen(o.cap * o.pct / 100) + '／1銘柄の上限 ' + yen(o.cap * MAX_POS) : '資金を入れると、各カードに推奨株数が出ます。';
    }
    function save() {
      var cap = parseFloat(String(document.getElementById('capin').value).replace(/[^\d.]/g, '')) || 0;
      setRisk({ cap: cap, pct: parseFloat(document.getElementById('riskin').value) || 1 }); riskOut(); renderSizing();
    }
    document.getElementById('capin').addEventListener('change', save);
    document.getElementById('riskin').addEventListener('change', save);
    document.getElementById('trfile').addEventListener('change', function (e) {
      var f = e.target.files && e.target.files[0]; if (!f) return;
      var rd = new FileReader();
      rd.onload = function () {
        var txt; try { txt = new TextDecoder('shift_jis').decode(rd.result); } catch (er) { txt = ''; }
        var rows = parseSbi(txt);
        if (!rows || !rows.length) { try { rows = parseSbi(new TextDecoder('utf-8').decode(rd.result)); } catch (er) { rows = null; } }
        if (!rows || !rows.length) { alert('約定履歴のCSVとして読み取れませんでした。SBI証券の外国株式「約定履歴」のCSVを選んでください。'); return; }
        var n = mergeTrades(rows); renderTrades(); alert(rows.length + '件を読み込みました（新規 ' + n + '件）。');
        e.target.value = '';
      };
      rd.readAsArrayBuffer(f);
    });
    document.getElementById('trclr').addEventListener('click', function () {
      if (!confirm('この端末に保存した取引データを消去しますか？')) return;
      try { localStorage.removeItem(TR_KEY); } catch (er) {} renderTrades();
    });
    riskOut(); renderTrades();
  }

  /* ── 折りたたみ（▼）と目次 ── 開閉状態は端末(localStorage)に保存 */
  var FOLD_KEY = 'go_fold_v1', FOLD_DEFAULT = { ma: 1, cand: 1 };
  function getFold() { try { var o = JSON.parse(localStorage.getItem(FOLD_KEY) || 'null'); return (o && typeof o === 'object') ? o : null; } catch (e) { return null; } }
  function saveFold(k, on) { var o = getFold() || JSON.parse(JSON.stringify(FOLD_DEFAULT)); o[k] = on ? 1 : 0; try { localStorage.setItem(FOLD_KEY, JSON.stringify(o)); } catch (e) {} }
  function isFolded(k) { var o = getFold(); return o ? !!o[k] : !!FOLD_DEFAULT[k]; }
  function foldSecs() { return Array.prototype.slice.call(document.querySelectorAll('section.sec[data-key]')); }
  function setFolded(sec, on, save) {
    sec.classList.toggle('collapsed', !!on);
    var h = sec.querySelector('.sec-h'); if (h) h.setAttribute('aria-expanded', on ? 'false' : 'true');
    if (save) saveFold(sec.getAttribute('data-key'), on);
  }
  function secCount(sec) { return sec.querySelectorAll('.card, .ma-row').length; }
  function updateCount(sec) {
    var c = sec && sec.querySelector('.sec-h .cnt'); if (!c) return;
    var n = secCount(sec); c.textContent = n ? String(n) : ''; c.style.display = n ? '' : 'none';
  }
  function makeFoldable(sec) {
    if (!sec || sec.querySelector('.sec-h')) return;
    var h2 = sec.querySelector('h2.find'); if (!h2 || h2.parentNode !== sec) return;
    var k = sec.getAttribute('data-key');
    var h = document.createElement('div'); h.className = 'sec-h'; h.setAttribute('role', 'button'); h.tabIndex = 0;
    sec.insertBefore(h, h2); h.appendChild(h2);
    var cnt = document.createElement('span'); cnt.className = 'cnt'; h.appendChild(cnt);
    var ch = document.createElement('span'); ch.className = 'chev'; ch.setAttribute('aria-hidden', 'true'); ch.textContent = '▼'; h.appendChild(ch);
    var body = document.createElement('div'); body.className = 'sec-body'; body.id = 'body-' + k;
    var inn = document.createElement('div'); inn.className = 'sec-in'; body.appendChild(inn);
    while (h.nextSibling) inn.appendChild(h.nextSibling);
    sec.appendChild(body); h.setAttribute('aria-controls', body.id);
    function toggle() { setFolded(sec, !sec.classList.contains('collapsed'), true); syncAllBtn(); }
    h.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('a,button,input')) return; toggle(); });
    h.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    setFolded(sec, isFolded(k), false); updateCount(sec);
  }
  function syncAllBtn() {
    var b = document.getElementById('navall'); if (!b) return;
    var anyOpen = foldSecs().some(function (s) { return s.style.display !== 'none' && !s.classList.contains('collapsed'); });
    b.textContent = anyOpen ? '▲ すべて閉じる' : '▼ すべて開く';
  }
  function buildNav() {
    var nav = document.getElementById('nav'); if (!nav) return;
    var html = [];
    if (document.getElementById('q')) html.push('<a href="#search-sec" data-go="search">🔍 検索</a>');
    foldSecs().forEach(function (s) {
      if (s.style.display === 'none') return;
      var n = secCount(s);
      html.push('<a href="#' + esc(s.id) + '" data-go="' + esc(s.getAttribute('data-key')) + '">' + esc(s.getAttribute('data-nav') || '') +
        (n ? '<span class="n">' + n + '</span>' : '') + '</a>');
    });
    html.push('<button type="button" id="navall"></button>');
    nav.innerHTML = html.join(''); syncAllBtn();
  }
  function initFold() {
    foldSecs().forEach(makeFoldable); buildNav();
    var nav = document.getElementById('nav'); if (!nav || nav._bound) return; nav._bound = 1;
    nav.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target.closest('a,button') : null; if (!t) return;
      if (t.id === 'navall') {
        var anyOpen = foldSecs().some(function (s) { return s.style.display !== 'none' && !s.classList.contains('collapsed'); });
        foldSecs().forEach(function (s) { setFolded(s, anyOpen, true); }); syncAllBtn(); return;
      }
      var k = t.getAttribute('data-go');
      if (k === 'search') { e.preventDefault(); var q = document.getElementById('q'); var ss = document.getElementById('search-sec');
        if (ss) ss.scrollIntoView({ behavior: 'smooth', block: 'start' }); if (q) setTimeout(function () { q.focus({ preventScroll: true }); }, 350); return; }
      var sec = document.querySelector('section.sec[data-key="' + k + '"]');
      if (sec) { e.preventDefault(); if (sec.classList.contains('collapsed')) { setFolded(sec, false, true); syncAllBtn(); }
        sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });
  }

  function init() {
    renderCoach(); initFold(); addRefreshBtn(); addJpyBtn(); injectStars(); setMktStatus(); markMaWatch(); renderSizing();
    if (getWatch().length) ensureStocks(renderWatch); else ensureWatchSec();
    tick();   /* tick が即座に refreshPrices を呼ぶ（以前は二重に取得していた） */
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
        "c": a.code, "n": a.name,
        "p": a.price, "sc": a.sc, "g": a.g, "m": _sector_short(a.sector),
        "t": a.tgt, "st": a.stp, "rr": a.rr, "r": a.reasons, "tp": tp,
        "val": val, "fg": fg, "rk": rank,
    }


HOLDING_LEVELS_FILE = DOCS / "holding_levels.json"
HOLD_TGT_ATR = 2.0     # 利確 = 買値 + 2×ATR
HOLD_STP_ATR = 1.5     # 損切 = 買値 − 1.5×ATR


def _anchored_levels(avg: float, a: Analysis) -> dict | None:
    """買値を基準に利確・損切を決める（ATRは設定した日の値）。"""
    atr = a.atr or ((a.tgt - a.price) / 2.0 if a.tgt and a.price else None)
    if not (avg and atr and atr > 0):
        return None
    return {"avg": round(float(avg), 4), "atr": round(float(atr), 4),
            "tgt": round(avg + HOLD_TGT_ATR * atr, 2), "stp": round(avg - HOLD_STP_ATR * atr, 2),
            "set": datetime.now(tz=JST).strftime("%Y-%m-%d")}


def holding_levels(holdings: list[dict], amap: dict) -> dict[str, dict]:
    """保有銘柄の利確・損切を買値基準で決め、初回に保存して以後は固定する。

    以前は全銘柄共通の「現値±ATR」を保有にも使っていたため、株価が下がると損切ラインも
    一緒に下がり、損切が機能しなかった（例: DVN 買値$49.19 に対し現値$47.05基準の
    損切$44.85）。ここでは買値＋2ATR／買値−1.5ATRを初めて表示した日に固定し、
    買値が変わった（買い増し等で holdings.txt を書き換えた）ときだけ再計算する。
    holdings.txt で手動指定した利確・損切はそちらを優先する（_holding_card）。
    """
    try:
        saved = json.loads(HOLDING_LEVELS_FILE.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            saved = {}
    except Exception:
        saved = {}
    out: dict[str, dict] = {}
    for h in holdings:
        code, avg = h.get("code"), h.get("avg") or 0
        prev = saved.get(code)
        if isinstance(prev, dict) and abs(float(prev.get("avg", -1)) - float(avg)) < 1e-6:
            out[code] = prev                         # 買値が同じなら固定値を使い続ける
            continue
        a = amap.get(code)
        lv = _anchored_levels(avg, a) if a else None
        if lv:
            out[code] = lv
    try:
        HOLDING_LEVELS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print(f"[globe] {HOLDING_LEVELS_FILE.name} 保存失敗: {e}", file=sys.stderr)
    return out


def _holding_card(h: dict, amap: dict, valid=None, levels: dict | None = None) -> str:
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
    # 利確・損切は買値基準（保存済みの固定値）。手動指定があればそれを優先。
    # 共有の Analysis を書き換えると stocks.json や他セクションまで変わるのでコピーに適用する。
    lv = (levels or {}).get(code) or _anchored_levels(avg, a) or {}
    a = copy.copy(a)
    a.tgt = h.get("tgt") or lv.get("tgt") or a.tgt
    a.stp = h.get("stp") or lv.get("stp") or a.stp
    # 保有では RR（現値からの残り利幅÷残り損失幅）は損切に近づくと数十倍に跳ねて意味を失うので出さず、
    # 代わりに各ラインまでの距離を損益の行に出す。
    a.rr = None
    basis = "手動指定" if (h.get("tgt") or h.get("stp")) else (
        f'買値基準・{lv["set"]}設定' if lv.get("set") else "買値基準")
    pl = (f'<div class="pl" data-hold-c="{_esc(code)}" data-avg="{avg}" '
          f'data-tgt="{a.tgt or ""}" data-stp="{a.stp or ""}">{_hold_pl_html(a.price, avg, a.tgt, a.stp)}</div>'
          f'<div class="hnote hbasis">利確・損切は{basis}（株価が動いても変わりません）</div>')
    base = _card(0, a, True).replace('<span class="rank">0</span>', '<span class="rank">保有</span>')
    return base[:-6] + pl + "</div>"


def _hold_pl_html(price: float, avg: float, tgt, stp) -> str:
    """損益と利確・損切ラインの到達状況（JS の holdPl と同じ表示）。"""
    pl_pct = round((price - avg) / avg * 100.0, 1) if avg else 0.0
    cls = "up" if pl_pct >= 0 else "dn"
    hit = ""
    if stp and price <= stp:
        hit = '<b class="hit dn">⚠ 損切ライン到達</b>'
    elif tgt and price >= tgt:
        hit = '<b class="hit up">✅ 利確ライン到達</b>'
    dist = ""
    if not hit and price > 0 and tgt and stp:
        dist = (f'<span class="hnote">利確まで +{(tgt - price) / price * 100:.1f}% ／ '
                f'損切まで -{(price - stp) / price * 100:.1f}%</span>')
    return (f'<span class="{cls}">損益 {"+" if pl_pct >= 0 else ""}{pl_pct}%</span>{hit}'
            f'<span class="hnote">買値 {_usd(avg)} → 現値 {_usd(price)}</span>{dist}')


def build_dashboard(analyses: list[Analysis], meta: dict, usdjpy: float,
                    holdings: list[dict] | None = None,
                    cands: list[dict] | None = None,
                    cmeta: dict | None = None,
                    growth: list[Analysis] | None = None,
                    hlevels: dict | None = None,
                    ma: dict | None = None) -> tuple[str, dict]:
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
    hold_cards = hold_sum + "".join(_holding_card(h, amap, _valid_tk, hlevels) for h in holdings)
    hold_sec = _section("💼 保有銘柄", "MY HOLDINGS", hold_cards) if holdings else ""
    cand_sec = _candidate_section(cands, cmeta, amap)
    growth_sec = _growth_section(growth)
    focus = {a.code: "大化け候補" for a in growth}
    focus.update({a.code: "買い候補" for a in buy_rank})
    focus.update({h["code"]: "保有中" for h in holdings if h.get("code")})
    ma_sec = _ma_section(ma, amap, focus)

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
<meta name="theme-color" content="#060a17">
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

  <nav id="nav" class="nav" aria-label="目次"></nav>

  {_keyed(hold_sec, "hold", "保有")}
  <section class="sec" id="sec-coach" data-key="coach" data-nav="成績"><h2 class="find"><span>🧭 取引コーチ（成績・資金管理）</span><em>端末内だけに保存</em></h2><div id="coach" class="coach"><p class="empty">読み込み中…</p></div></section>

  <section id="search-sec" class="sec">
    <div class="searchbar"><input id="q" type="text" inputmode="search"
      placeholder="🔍 銘柄検索（例: apple / NVDA / micro）" autocomplete="off"></div>
    <p id="hint">社名（apple）またはティッカー（AAPL）で全{len(analyses)}銘柄を検索。⭐でウォッチ登録。{'<br><span style="color:var(--dn)">⚠ 縮小モード：ユニバース取得に失敗し内蔵リストで動作中（銘柄数が少なめ）</span>' if UNIVERSE_REDUCED.get('reduced') else ''}</p>
    <div id="results" class="cards"></div>
  </section>

  {_keyed(_section("📈 買い候補ランキング TOP10", "条件：アナリスト予想プラス × 割安判定", buy_cards), "buy", "買い候補")}
  {_keyed(growth_sec, "growth", "大化け")}
  {_keyed(ma_sec, "ma", "M&A")}
  {_keyed(cand_sec, "cand", "S&P候補")}

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
    if len(analyses) < DASHBOARD_MIN_STOCKS:
        # ここで書き込むと既存の index.html / stocks.json を空同然の内容で潰してしまう。
        # 何も書かずに中止し、呼び出し側（main.py）が非ゼロ終了してCIを赤にする。
        raise DashboardSkipped(
            f"分析できた銘柄が {len(analyses)} 件しかありません"
            f"（下限 {DASHBOARD_MIN_STOCKS} 件）。docs/ は一切変更していません")
    usdjpy = _fetch_usdjpy()
    hlevels = holding_levels(holdings, {a.code: a for a in analyses})   # 買値基準の利確・損切（初回に固定）
    try:
        ma = fetch_ma_signals()                                             # SEC の M&A 関連開示
    except Exception as e:
        print(f"[globe] M&Aシグナル失敗: {e}", file=sys.stderr)
        ma = None
    html, stocks = build_dashboard(analyses, meta, usdjpy, holdings, cands, cmeta, growth, hlevels, ma)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / "app.js").write_text(APP_JS, encoding="utf-8")
    # 区切りの空白を詰め、検索キーはクライアント生成にして転送量を削減（1,800銘柄超で約500KB）
    (DOCS / "stocks.json").write_text(json.dumps(stocks, ensure_ascii=False, separators=(",", ":")),
                                      encoding="utf-8")
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
