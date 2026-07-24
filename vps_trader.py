#!/usr/bin/env python3
# ══════════════════════════════════════════════════════════════
# deploy/vps_trader.py · 云 VPS 自包含交易器（确定性核·零 LLM·零DB·纸面）
# ──────────────────────────────────────────────────────────────
# 为"电脑关机也 24×7 跑"而生。只跑赚钱的核，砍掉一切重的东西：
#   · 依赖仅 numpy + pandas + requests（最小免费 VPS 就能跑）
#   · 数据：黄金 GC=F(Yahoo 1h) + BTC/ETH(Binance 1h)——纯 HTTP，无需 DB
#   · 信号：4 策略共识(Awesome+Strategy001+Ichimoku+布林突破·全体一致才动)
#           ——与主仓库 agents/fused_signal 逐字同源，两段回测过筛的唯一幸存配置
#   · 风控（硬·不可越）：单笔风险 1%、ATR 止损、每品种最多 1 仓、日内亏损熔断
#   · 纸面结算·JSONL 留痕·崩溃由 systemd 自动重启（见 install.sh）
#   · AI 成本 $0（不接任何大模型）；真钱永不自动。
#
# 跑：python3 vps_trader.py           # 前台
#     由 systemd 常驻（trading-agent.service）
# ══════════════════════════════════════════════════════════════

from __future__ import annotations

import json
import math
import os
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# ── 配置（改这里）──────────────────────────────────────────────
SYMBOLS = ["gold", "btc", "eth"]     # 要跑的品种
POLL_SEC = 600                       # 轮询间隔(秒)·1h信号不用太勤
RISK_PCT = 0.01                      # 单笔风险=权益1%
ATR_MULT = 2.0                       # 止损=入场 ± ATR×2
ATR_N = 14
MAX_DAILY_LOSS = 0.05                # 日内权益回撤达5%当日停手
COST_PCT = 0.0006                    # 往返成本(点差+滑点)·风险度量与结算用
_DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(_DIR, "vps_account.json")
LOG = os.path.join(_DIR, "vps_trades.jsonl")
_UA = {"User-Agent": "Mozilla/5.0"}
_MIN_BARS = 220                      # Ichimoku(52+26) + 余量


# ── 数据（1h·纯HTTP·fail-safe 返回 [] 绝不抛）──────────────────
def _get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout) as r:
        return r.read()


def fetch_1h(symbol):
    try:
        if symbol == "gold":
            d = json.loads(_get("https://query1.finance.yahoo.com/v8/finance/chart/GC=F?interval=1h&range=60d"))
            r = d["chart"]["result"][0]; ts = r["timestamp"]; q = r["indicators"]["quote"][0]
            out = []
            for i, t in enumerate(ts):
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                if None not in (o, h, l, c):
                    out.append((int(t), float(o), float(h), float(l), float(c)))
            return out
        sym = {"btc": "BTCUSDT", "eth": "ETHUSDT"}[symbol]
        d = json.loads(_get(f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=500"))
        return [(int(k[0]) // 1000, float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in d]
    except Exception as e:
        print(f"[{_now()}] 拉 {symbol} 失败(下轮重试): {type(e).__name__}")
        return []


# ── 指标 + 4 策略（逐字同 agents/strat_zoo 的幸存者）───────────
def _ema(s, n): return s.ewm(span=n, adjust=False).mean()


def _ha(df):
    ha_c = (df["o"] + df["h"] + df["l"] + df["c"]) / 4
    ha_o = ha_c.copy()
    for i in range(1, len(df)):
        ha_o.iloc[i] = (ha_o.iloc[i - 1] + ha_c.iloc[i - 1]) / 2
    return ha_o, ha_c


def s_awesome(df):
    mid = (df["h"] + df["l"]) / 2
    ao = mid.rolling(5).mean() - mid.rolling(34).mean()
    return np.where(ao > 0, 1, -1)


def s_strategy001(df):
    c = df["c"]; e20, e50 = _ema(c, 20), _ema(c, 50); ho, hc = _ha(df); green = hc > ho
    pos = np.zeros(len(df)); cur = 0
    for i in range(50, len(df)):
        if e20.iloc[i] > e50.iloc[i] and green.iloc[i] and hc.iloc[i] > e20.iloc[i]:
            cur = 1
        elif e20.iloc[i] < e50.iloc[i] and (not green.iloc[i]) and hc.iloc[i] < e20.iloc[i]:
            cur = -1
        pos[i] = cur
    return pos


def s_ichimoku(df, t=9, k=26, s=52):
    h, l, c = df["h"], df["l"], df["c"]
    tenkan = (h.rolling(t).max() + l.rolling(t).min()) / 2
    kijun = (h.rolling(k).max() + l.rolling(k).min()) / 2
    sa = ((tenkan + kijun) / 2).shift(k)
    sb = ((h.rolling(s).max() + l.rolling(s).min()) / 2).shift(k)
    top = pd.concat([sa, sb], axis=1).max(axis=1); bot = pd.concat([sa, sb], axis=1).min(axis=1)
    return np.where((tenkan > kijun) & (c > top), 1, np.where((tenkan < kijun) & (c < bot), -1, 0))


def s_boll_break(df, n=20, k=2.0):
    c = df["c"]; ma = c.rolling(n).mean(); sd = c.rolling(n).std()
    pos = np.zeros(len(c)); cur = 0
    for i in range(n, len(c)):
        if c.iloc[i] > ma.iloc[i] + k * sd.iloc[i]:
            cur = 1
        elif c.iloc[i] < ma.iloc[i] - k * sd.iloc[i]:
            cur = -1
        elif abs(c.iloc[i] - ma.iloc[i]) < 0.3 * sd.iloc[i]:
            cur = 0
        pos[i] = cur
    return pos


def consensus(bars):
    """4/4 全体一致才出信号。返回 (signal, votes, atr)。样本不足→(0,..)。"""
    if len(bars) < _MIN_BARS:
        return 0, {}, 0.0
    df = pd.DataFrame([(b[0], b[1], b[2], b[3], b[4]) for b in bars],
                      columns=["ts", "o", "h", "l", "c"])
    if df[["o", "h", "l", "c"]].isna().any().any():
        return 0, {}, 0.0
    votes = {}
    for name, fn in (("awesome", s_awesome), ("strategy001", s_strategy001),
                     ("ichimoku", s_ichimoku), ("boll_break", s_boll_break)):
        votes[name] = int(np.sign(np.nan_to_num(np.asarray(fn(df), float))[-1]))
    nl = sum(1 for v in votes.values() if v > 0); ns = sum(1 for v in votes.values() if v < 0)
    sig = 1 if nl == 4 else (-1 if ns == 4 else 0)
    # ATR
    h, l, c = df["h"], df["l"], df["c"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(ATR_N).mean().iloc[-1])
    return sig, votes, atr


# ── 纸面账户 + 硬风控 ─────────────────────────────────────────
def _load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"balance": 10000.0, "day": "", "day_start_eq": 10000.0, "positions": {}, "closed": []}


def _save(a):
    tmp = STATE + ".tmp"
    json.dump(a, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, STATE)


def _now(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _rec(row):
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def step(symbol, acc):
    bars = fetch_1h(symbol)
    if not bars:
        return
    price = bars[-1][4]
    sig, votes, atr = consensus(bars)
    pos = acc["positions"].get(symbol)

    # ① 管已有仓：止损 / 共识反向 → 平
    if pos:
        side = pos["side"]; hit_stop = (price <= pos["stop"]) if side == 1 else (price >= pos["stop"])
        flip = (sig != 0 and sig != side)
        if hit_stop or flip:
            pnl = (price - pos["entry"]) * side * pos["vol"] - COST_PCT * price * pos["vol"]
            acc["balance"] += pnl
            r = pnl / (pos["risk"] * pos["vol"]) if pos["risk"] * pos["vol"] > 1e-9 else 0
            acc["closed"].append({"sym": symbol, "pnl_r": round(r, 3), "reason": "SL" if hit_stop else "flip"})
            _rec({"ts": _now(), "sym": symbol, "act": "CLOSE", "reason": "止损" if hit_stop else "共识反向",
                  "price": round(price, 2), "pnl_r": round(r, 3), "bal": round(acc["balance"], 2)})
            print(f"[{_now()}] {symbol} 平仓 {'止损' if hit_stop else '共识反向'} {r:+.2f}R 余额{acc['balance']:.0f}")
            del acc["positions"][symbol]
            _save(acc); return

    # ② 日内熔断
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if acc.get("day") != today:
        acc["day"] = today; acc["day_start_eq"] = acc["balance"]
    if acc["balance"] < acc["day_start_eq"] * (1 - MAX_DAILY_LOSS):
        return  # 当日停手

    # ③ 空仓 + 共识出信号 → 开（硬风控:1%风险·ATR止损）
    if not pos and sig != 0 and atr > 0:
        risk_per_unit = ATR_MULT * atr
        vol = (acc["balance"] * RISK_PCT) / risk_per_unit if risk_per_unit > 1e-9 else 0
        if vol <= 0:
            return
        stop = price - risk_per_unit if sig == 1 else price + risk_per_unit
        acc["positions"][symbol] = {"side": sig, "entry": price, "stop": round(stop, 4),
                                    "vol": round(vol, 6), "risk": risk_per_unit}
        _rec({"ts": _now(), "sym": symbol, "act": "OPEN", "side": "多" if sig == 1 else "空",
              "price": round(price, 2), "stop": round(stop, 2), "votes": votes})
        print(f"[{_now()}] {symbol} 开{'多' if sig==1 else '空'}@{price:.2f} 止损{stop:.2f} (4/4共识)")
        _save(acc)


def run_cycle(acc):
    """跑一轮全品种。返回心跳字符串。"""
    for sym in SYMBOLS:
        try:
            step(sym, acc)
        except Exception as e:
            print(f"[{_now()}] {sym} 单轮异常(隔离): {type(e).__name__}: {str(e)[:80]}")
    cl = acc.get("closed", [])
    wr = (sum(1 for c in cl if c["pnl_r"] > 0) / len(cl) * 100) if cl else 0
    return (f"[{_now()}] 心跳 余额{acc['balance']:.0f} 持仓{len(acc['positions'])} "
            f"已平{len(cl)}单 胜率{wr:.0f}%")


def main():
    import sys
    once = "--once" in sys.argv       # GitHub Actions 定时模式:跑一轮就退出
    acc = _load()
    if once:
        print(run_cycle(acc))
        _save(acc)                    # 确保落盘(供 workflow 提交回仓库)
        return
    print(f"[{_now()}] ☁️ VPS 交易器启动 · 品种{SYMBOLS} · 1h共识 · 风险{RISK_PCT*100}% · 纸面·真钱永不自动")
    while True:
        print(run_cycle(acc))
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
