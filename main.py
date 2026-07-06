#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GLOBE ORACLE — 米国株オラクル CLI

使い方:
  python main.py report   # 日次: ユニバース取得→スコア→docs/ 一式生成
  python main.py prices   # ライブ価格: 表示中銘柄＋指数＋USDJPY を prices.json へ
"""
import sys
import report as R


def _usage() -> None:
    print(__doc__)


def main() -> int:
    if len(sys.argv) < 2:
        _usage()
        return 1
    cmd = sys.argv[1].strip().lower()
    if cmd == "report":
        path = R.write_dashboard()
        print(f"[globe] dashboard 生成完了: {path}")
        return 0
    if cmd == "prices":
        data = R.write_prices()
        n = len((data or {}).get("px", {}))
        print(f"[globe] prices 更新完了: {n} 銘柄 + 指数/為替")
        return 0
    _usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
