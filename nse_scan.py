#!/usr/bin/env python3
"""
NSE stock signal scanner. Runs once per call (GitHub Actions calls it on a schedule),
scores Nifty 50 stocks and sends LONG / SHORT ideas to Telegram. It never places trades.

    python nse_scan.py --mode intraday     15m entries, 1h trend, VWAP, opening range
    python nse_scan.py --mode mtf          daily entries, weekly trend, buys only
    python nse_scan.py --test              sends one test message to Telegram
    python nse_scan.py --dry-run           prints signals instead of sending them

Every alert is also saved to journal.json / journal.csv / summary.md and later marked as
target hit, stop-loss hit, or expired, so you can see the real win rate of the signals.

Needs: pip install requests
Secrets (environment): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Not investment advice. Signals are setups, not guarantees, and many will lose.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from urllib.parse import quote

import requests

NIFTY50 = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "SBIN", "LT", "ITC", "HINDUNILVR",
           "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "ASIANPAINT", "NTPC", "ONGC",
           "POWERGRID", "M&M", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS", "WIPRO", "HCLTECH", "TECHM", "BAJAJFINSV",
           "NESTLEIND", "COALINDIA", "HINDALCO", "GRASIM", "CIPLA", "DRREDDY", "EICHERMOT", "HEROMOTOCO", "BRITANNIA", "APOLLOHOSP",
           "DIVISLAB", "INDUSINDBK", "SBILIFE", "HDFCLIFE", "BPCL", "TATACONSUM", "SHRIRAMFIN", "TRENT", "BEL"]

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
YF_BASE = os.environ.get("YF_BASE")          # testing only
TG_API = os.environ.get("TG_API_BASE", "https://api.telegram.org")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


def now():
    return float(os.environ.get("NOW_OVERRIDE") or time.time())


# ------------------------------------------------------------------ indicators
def ema(a, n):
    k = 2 / (n + 1)
    out = [a[0]]
    for v in a[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def wilder(vals, n):
    al = 1 / n
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * al + out[-1] * (1 - al))
    return out


def rsi_last(close, n=14):
    g, l = [0.0], [0.0]
    for i in range(1, len(close)):
        d = close[i] - close[i - 1]
        g.append(max(d, 0.0))
        l.append(max(-d, 0.0))
    ag, al = wilder(g, n), wilder(l, n)
    if al[-1] == 0:
        return 100.0
    return 100 - 100 / (1 + ag[-1] / al[-1])


def macd_hist(close):
    f, s = ema(close, 12), ema(close, 26)
    line = [a - b for a, b in zip(f, s)]
    sig = ema(line, 9)
    return [a - b for a, b in zip(line, sig)]


def atr_last(k, n=14):
    tr = []
    for i, c in enumerate(k):
        if i == 0:
            tr.append(c["high"] - c["low"])
        else:
            pc = k[i - 1]["close"]
            tr.append(max(c["high"] - c["low"], abs(c["high"] - pc), abs(c["low"] - pc)))
    return wilder(tr, n)[-1]


def adx_last(k, n=14):
    pdm, mdm, tr = [0.0], [0.0], [k[0]["high"] - k[0]["low"]]
    for i in range(1, len(k)):
        up = k[i]["high"] - k[i - 1]["high"]
        dn = k[i - 1]["low"] - k[i]["low"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        pc = k[i - 1]["close"]
        tr.append(max(k[i]["high"] - k[i]["low"], abs(k[i]["high"] - pc), abs(k[i]["low"] - pc)))
    sp, sm, st = wilder(pdm, n), wilder(mdm, n), wilder(tr, n)
    dx = []
    for i in range(len(sp)):
        pi = 100 * sp[i] / st[i] if st[i] else 0.0
        mi = 100 * sm[i] / st[i] if st[i] else 0.0
        dx.append(0.0 if pi + mi == 0 else 100 * abs(pi - mi) / (pi + mi))
    return wilder(dx, n)[-1]


def swings(k, w=3):
    highs, lows = [], []
    for i in range(w, len(k) - w):
        is_h = is_l = True
        for j in range(1, w + 1):
            if k[i]["high"] <= k[i - j]["high"] or k[i]["high"] <= k[i + j]["high"]:
                is_h = False
            if k[i]["low"] >= k[i - j]["low"] or k[i]["low"] >= k[i + j]["low"]:
                is_l = False
        if is_h:
            highs.append((i, k[i]["high"]))
        if is_l:
            lows.append((i, k[i]["low"]))
    return highs, lows


def obv_trend(k):
    obv = [0.0]
    for i in range(1, len(k)):
        if k[i]["close"] > k[i - 1]["close"]:
            obv.append(obv[-1] + k[i]["volume"])
        elif k[i]["close"] < k[i - 1]["close"]:
            obv.append(obv[-1] - k[i]["volume"])
        else:
            obv.append(obv[-1])
    e, o, o5 = ema(obv, 20)[-1], obv[-1], obv[-6]
    return 1 if (o > e and o > o5) else -1 if (o < e and o < o5) else 0


def structure(k, sw):
    H, L = sw
    n = len(k)
    if len(H) < 2 or len(L) < 2:
        return None
    h1, h2, l1, l2 = H[-2][1], H[-1][1], L[-2][1], L[-1][1]
    prior = -1 if (h2 < h1 and l2 < l1) else 1 if (h2 > h1 and l2 > l1) else 0
    c = [x["close"] for x in k]
    up = max(c[n - 1], c[n - 2], c[n - 3]) > h2 and c[n - 4] <= h2
    dn = min(c[n - 1], c[n - 2], c[n - 3]) < l2 and c[n - 4] >= l2
    if up:
        if prior == -1:
            return {"score": 2, "note": "Bullish CHoCH", "why": "Bullish change of character: price broke above the last swing high after lower highs and lower lows"}
        return {"score": 1, "note": "Bullish BOS", "why": "Bullish break of structure above the last swing high"}
    if dn:
        if prior == 1:
            return {"score": -2, "note": "Bearish CHoCH", "why": "Bearish change of character: price broke below the last swing low after higher highs and higher lows"}
        return {"score": -1, "note": "Bearish BOS", "why": "Bearish break of structure below the last swing low"}
    return {"score": 0, "note": "HH/HL structure" if prior == 1 else "LH/LL structure" if prior == -1 else "", "why": ""}


def candle_pattern(k, e20):
    a, b, c = k[-1], k[-2], k[-3]
    body = lambda x: abs(x["close"] - x["open"])
    rng = lambda x: x["high"] - x["low"]
    green = lambda x: x["close"] > x["open"]
    red = lambda x: x["close"] < x["open"]
    if red(b) and green(a) and a["open"] <= b["close"] and a["close"] >= b["open"] and body(a) > body(b):
        return ("Bullish engulfing", 1)
    if green(b) and red(a) and a["open"] >= b["close"] and a["close"] <= b["open"] and body(a) > body(b):
        return ("Bearish engulfing", -1)
    bd = body(a)
    lw = min(a["open"], a["close"]) - a["low"]
    uw = a["high"] - max(a["open"], a["close"])
    if bd > 0 and lw >= 2 * bd and uw <= 0.25 * rng(a) and a["close"] < e20:
        return ("Hammer", 1)
    if bd > 0 and uw >= 2 * bd and lw <= 0.25 * rng(a) and a["close"] > e20:
        return ("Shooting star", -1)
    if red(c) and body(c) > 0.6 * rng(c) and body(b) < 0.3 * body(c) and green(a) and a["close"] > (c["open"] + c["close"]) / 2:
        return ("Morning star", 1)
    if green(c) and body(c) > 0.6 * rng(c) and body(b) < 0.3 * body(c) and red(a) and a["close"] < (c["open"] + c["close"]) / 2:
        return ("Evening star", -1)
    return None


def double_patterns(k, sw, atr):
    n = len(k)
    c = [x["close"] for x in k]
    H, L = sw
    out = []
    if len(L) >= 2:
        l1, l2 = L[-2], L[-1]
        mid = [h for h in H if l1[0] < h[0] < l2[0]]
        if mid and abs(l1[1] - l2[1]) / l1[1] <= 0.01 and l2[0] - l1[0] >= 5 and n - l2[0] <= 40:
            neck = max(h[1] for h in mid)
            if neck - max(l1[1], l2[1]) >= 2 * atr:
                if max(c[n - 1], c[n - 2], c[n - 3]) > neck and c[n - 4] <= neck:
                    out.append(("Double bottom breakout", 1, 1))
                elif c[n - 1] < neck:
                    out.append(("Double bottom forming", 1, 0))
    if len(H) >= 2:
        h1, h2 = H[-2], H[-1]
        mid = [lo for lo in L if h1[0] < lo[0] < h2[0]]
        if mid and abs(h1[1] - h2[1]) / h1[1] <= 0.01 and h2[0] - h1[0] >= 5 and n - h2[0] <= 40:
            neck = min(lo[1] for lo in mid)
            if min(h1[1], h2[1]) - neck >= 2 * atr:
                if min(c[n - 1], c[n - 2], c[n - 3]) < neck and c[n - 4] >= neck:
                    out.append(("Double top breakdown", -1, 1))
                elif c[n - 1] > neck:
                    out.append(("Double top forming", -1, 0))
    return out


def squeeze(k):
    c = [x["close"] for x in k]
    bw = []
    for i in range(19, len(c)):
        w = c[i - 19:i + 1]
        m = sum(w) / 20
        sd = (sum((x - m) ** 2 for x in w) / 20) ** 0.5
        bw.append(4 * sd / m)
    rec = bw[-120:]
    return rec[-1] <= min(rec) * 1.1


# ------------------------------------------------------------------ scoring
def score_setup(k1, k4, mode):
    score, why = 0, []
    intraday = mode == "intraday"
    tf, htf = ("15m", "1h") if intraday else ("Daily", "Weekly")

    c4 = [x["close"] for x in k4]
    p4 = c4[-1]
    e50, e200 = ema(c4, 50)[-1], ema(c4, 200)[-1]
    if p4 > e50 and e50 > e200:
        score += 2
        why.append(htf + " uptrend (price > EMA50 > EMA200)")
    elif p4 < e50 and e50 < e200:
        score -= 2
        why.append(htf + " downtrend (price < EMA50 < EMA200)")

    c = [x["close"] for x in k1]
    p = c[-1]
    e20 = ema(c, 20)
    if e20[-1] > ema(c, 50)[-1]:
        score += 1
        why.append(tf + " EMA20 above EMA50")
    else:
        score -= 1
        why.append(tf + " EMA20 below EMA50")

    h = macd_hist(c)
    h0, h1 = h[-1], h[-2]
    if h0 > 0 and h0 > h1:
        score += 1
        why.append("MACD histogram positive and rising")
    elif h0 < 0 and h0 < h1:
        score -= 1
        why.append("MACD histogram negative and falling")

    r = rsi_last(c)
    if r > 75:
        score -= 1
        why.append(f"RSI {r:.0f} overbought")
    elif r < 25:
        score += 1
        why.append(f"RSI {r:.0f} oversold")
    elif 52 < r <= 70:
        score += 1
        why.append(f"RSI {r:.0f} bullish momentum")
    elif 30 <= r < 48:
        score -= 1
        why.append(f"RSI {r:.0f} bearish momentum")

    prev = k1[-21:-1]
    lk = k1[-1]
    same = [x for x in k1[:-1] if x.get("slot") is not None and x["slot"] == lk.get("slot")][-20:]
    base = same if len(same) >= 5 else prev
    avg_vol = sum(x["volume"] for x in base) / len(base)
    vr = lk["volume"] / avg_vol if avg_vol else 0.0
    green = lk["close"] > lk["open"]
    if vr > 1.5:
        score += 1 if green else -1
        why.append(f"Volume {vr:.1f}x average on a {'green' if green else 'red'} candle")

    if intraday and lk.get("date") is not None:
        td = [x for x in k1 if x.get("date") == lk["date"]]
        tv = sum(x["volume"] for x in td)
        if tv > 0:
            vwap = sum((x["high"] + x["low"] + x["close"]) / 3 * x["volume"] for x in td) / tv
            if p > vwap:
                score += 1
                why.append(f"Price above VWAP ({vwap:.2f}): buyers in control today")
            else:
                score -= 1
                why.append(f"Price below VWAP ({vwap:.2f}): sellers in control today")
        if len(td) >= 3 and td[0]["slot"] == 555:
            or_h = max(td[0]["high"], td[1]["high"])
            or_l = min(td[0]["low"], td[1]["low"])
            if p > or_h:
                score += 1
                why.append("Above the opening range high (first 30 minutes)")
            elif p < or_l:
                score -= 1
                why.append("Below the opening range low (first 30 minutes)")

    hh, ll = max(x["high"] for x in prev), min(x["low"] for x in prev)
    if p > hh:
        score += 1
        why.append("Broke above 20-candle high")
    elif p < ll:
        score -= 1
        why.append("Broke below 20-candle low")

    a = atr_last(k1)
    notes = []
    sw = swings(k1)

    ob = obv_trend(k1)
    if ob > 0:
        score += 1
        why.append("OBV rising: volume is flowing in")
    elif ob < 0:
        score -= 1
        why.append("OBV falling: volume is flowing out")

    ms = structure(k1, sw)
    if ms:
        score += ms["score"]
        if ms["why"]:
            why.append(ms["why"])
        if ms["note"]:
            notes.append(ms["note"])

    cp = candle_pattern(k1, e20[-1])
    if cp:
        score += cp[1]
        why.append(f"{cp[0]} candle on the {tf} chart")
        notes.append(cp[0])

    for name, direction, pts in double_patterns(k1, sw, a):
        score += pts * direction
        notes.append(name)
        why.append(name + ("" if pts else " (not confirmed yet, needs a close through the neckline)"))

    if squeeze(k1):
        notes.append("Volatility squeeze")
        why.append("Volatility squeeze: quiet market, a breakout may be building")

    return {"score": score, "why": why, "notes": notes, "vol_ratio": vr, "price": p, "atr": a,
            "adx": adx_last(k1), "stretch": (p - e20[-1]) / a,
            "trend4": 1 if p4 > e50 else -1 if p4 < e50 else 0}


def apply_filters(r, o):
    d = 1 if r["score"] > 0 else -1
    miss = []
    if o["adx_min"] > 0 and r["adx"] < o["adx_min"]:
        miss.append(f"sideways market (ADX {r['adx']:.0f} below {o['adx_min']:g})")
    if o["trend_align"] and r["trend4"] != d:
        miss.append("against the higher-timeframe trend")
    if o["max_stretch"] > 0 and d * r["stretch"] > o["max_stretch"]:
        miss.append(f"overextended ({abs(r['stretch']):.1f} ATR from EMA20)")
    return miss


def fmt(x):
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.4g}"


TIPS = {
    "intraday": "Intraday: close this position before 3:15 PM IST. Most brokers auto square off between 3:15 and 3:25 PM.",
    "mtf": "MTF / positional: a delivery buy you may hold for days. Interest is charged daily on the funded amount, and the broker can sell if the price falls too far.",
}


def build_message(sym, r, sl_mult, mode):
    side = "LONG" if r["score"] > 0 else "SHORT"
    sg = 1 if side == "LONG" else -1
    risk = sl_mult * r["atr"]
    p = r["price"]
    reasons = "\n".join("- " + w for w in r["why"])
    return (f"{side} {sym}  (score {r['score']:+d}, {mode})\n"
            f"Entry ~ Rs {fmt(p)}\nStop-loss: Rs {fmt(p - sg * risk)}\n"
            f"TP1 (1.5R): Rs {fmt(p + sg * 1.5 * risk)}\nTP2 (3R): Rs {fmt(p + sg * 3 * risk)}\n\n"
            f"Why:\n{reasons}\n\n{TIPS[mode]}\n"
            "Data can be up to 15 minutes late, so check the live price first. Not investment advice.")


# ------------------------------------------------------------------ data
def ist_date_and_min(ts):
    ist = int(ts) + 330 * 60
    return ist // 86400, (ist % 86400) // 60


def yahoo_candles(sym, interval, rng, always_drop_last=False):
    ticker = sym if ("." in sym or sym.startswith("^")) else sym + ".NS"
    err = None
    for host in ("query1", "query2"):
        base = YF_BASE or f"https://{host}.finance.yahoo.com"
        for attempt in range(2):
            try:
                resp = SESSION.get(f"{base}/v8/finance/chart/{quote(ticker, safe='')}",
                                   params={"interval": interval, "range": rng}, timeout=(5, 10))
                if resp.status_code == 429:
                    err = RuntimeError(f"{sym}: HTTP 429 (Yahoo is rate limiting)")
                    time.sleep(1 + attempt)
                    continue
                if resp.status_code != 200:
                    raise RuntimeError(f"{sym}: HTTP {resp.status_code}")
                d = resp.json()
                res = ((d.get("chart") or {}).get("result") or [None])[0]
                if not res or not res.get("timestamp"):
                    raise RuntimeError(f"{sym}: no data")
                q = res["indicators"]["quote"][0]
                out = []
                for i, t in enumerate(res["timestamp"]):
                    o, hi, lo, cl = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
                    if None in (o, hi, lo, cl):
                        continue
                    day, mins = ist_date_and_min(t)
                    out.append({"open": o, "high": hi, "low": lo, "close": cl,
                                "volume": q["volume"][i] or 0, "slot": mins, "date": day, "ts": t})
                if out:
                    t_now = now()
                    today, mins_now = ist_date_and_min(t_now)
                    if always_drop_last:
                        out.pop()
                    elif interval in ("15m", "1h"):
                        if out[-1]["ts"] + (900 if interval == "15m" else 3600) > t_now:
                            out.pop()
                    elif interval == "1d" and out[-1]["date"] == today and mins_now < 930:
                        out.pop()
                return out
            except Exception as e:  # try the other host
                err = e
                break
    raise err


def klines(sym, iv):
    if iv == "15m":
        c = yahoo_candles(sym, "15m", "30d")[-400:]
    elif iv == "1h":
        c = yahoo_candles(sym, "1h", "6mo")[-400:]
    elif iv == "1wk":
        c = yahoo_candles(sym, "1wk", "5y", always_drop_last=True)
    else:
        c = yahoo_candles(sym, "1d", "2y")
    if len(c) < 60:
        raise RuntimeError(f"{sym} has too little history")
    return c


# ------------------------------------------------------------------ telegram + state
def send_telegram(text, dry):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if dry:
        print("\n--- would send ---\n" + text + "\n------------------")
        return True
    resp = requests.post(f"{TG_API}/bot{token}/sendMessage", data={"chat_id": chat, "text": text}, timeout=20)
    if not resp.ok:
        print("Telegram error:", resp.status_code, resp.text[:200])
    return resp.ok


def load_state(path):
    try:
        with open(path) as f:
            s = json.load(f)
        cutoff = now() - 3 * 86400
        return {"sent": {k: v for k, v in s.get("sent", {}).items() if v > cutoff}}
    except Exception:
        return {"sent": {}}


def env_num(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return float(default)


# ------------------------------------------------------------------ journal
def ist_str(ts):
    return dt.datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M")


def load_journal(d):
    try:
        with open(os.path.join(d, "journal.json")) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def new_record(mode, sym, r, sl_mult, k1, t_now):
    side = "LONG" if r["score"] > 0 else "SHORT"
    sg = 1 if side == "LONG" else -1
    risk = sl_mult * r["atr"]
    p = r["price"]
    return {"id": f"{mode}|{sym}|{side}|{int(t_now)}", "mode": mode, "symbol": sym, "side": side,
            "score": r["score"], "ts": int(t_now), "time": ist_str(t_now),
            "candle_ts": k1[-1]["ts"], "date": k1[-1]["date"],
            "entry": p, "sl": p - sg * risk, "tp1": p + sg * 1.5 * risk, "tp2": p + sg * 3 * risk, "risk": risk,
            "status": "open", "exit_price": None, "exit_time": None, "r": None}


def close_signal(s, status, price, ts):
    sg = 1 if s["side"] == "LONG" else -1
    s["status"], s["exit_price"], s["exit_time"] = status, price, ist_str(ts)
    s["r"] = round(sg * (price - s["entry"]) / s["risk"], 2) if s["risk"] else 0.0


def resolve_signal(s, k):
    """Walks the candles after the signal. First level touched decides: stop-loss (-1R) or target 1 (+1.5R).
    If both are touched inside one candle it counts as a loss. Returns True if the signal was closed."""
    sg = 1 if s["side"] == "LONG" else -1
    after = [c for c in k if c["ts"] > s["candle_ts"]]
    for c in after:
        hit_sl = c["low"] <= s["sl"] if sg == 1 else c["high"] >= s["sl"]
        hit_tp = c["high"] >= s["tp1"] if sg == 1 else c["low"] <= s["tp1"]
        if hit_sl:
            close_signal(s, "sl", s["sl"], c["ts"])
            return True
        if hit_tp:
            close_signal(s, "tp1", s["tp1"], c["ts"])
            return True
    if s["mode"] == "intraday":
        same = [c for c in after if c["date"] == s["date"]]
        later = [c for c in after if c["date"] > s["date"]]
        if same and (later or same[-1]["slot"] >= 915):  # session is over
            close_signal(s, "expired", same[-1]["close"], same[-1]["ts"])
            return True
        if later and not same:
            close_signal(s, "expired", later[0]["open"], later[0]["ts"])
            return True
    elif len(after) >= 10:  # positional signals are given 10 trading days
        close_signal(s, "expired", after[9]["close"], after[9]["ts"])
        return True
    return False


def result_message(s):
    head = f"RESULT: {s['symbol']} {s['side']} ({s['mode']}, signal from {s['time']})"
    if s["status"] == "tp1":
        return f"{head}\nTarget 1 hit: {s['r']:+.1f}R"
    if s["status"] == "sl":
        return f"{head}\nStop-loss hit: {s['r']:+.1f}R"
    return f"{head}\nClosed without hitting either level: {s['r']:+.2f}R"


def update_open_signals(journal, dry):
    opens = [s for s in journal if s["status"] == "open"]
    cache, changed = {}, False
    for s in opens:
        iv = "15m" if s["mode"] == "intraday" else "1d"
        key = (s["symbol"], iv)
        if key not in cache:
            try:
                cache[key] = klines(s["symbol"], iv)
            except Exception as e:
                print(f"Journal: could not update {s['symbol']}: {e}")
                cache[key] = None
        if cache[key] and resolve_signal(s, cache[key]):
            changed = True
            if os.environ.get("NOTIFY_RESULTS", "on").lower() != "off":
                send_telegram(result_message(s), dry)
    return changed


def journal_stats(journal, mode=None):
    rows = [s for s in journal if s["status"] != "open" and (mode is None or s["mode"] == mode)]
    wins = [s for s in rows if s["r"] > 0]
    total = sum(s["r"] for s in rows)
    return {"closed": len(rows), "wins": len(wins), "losses": len(rows) - len(wins),
            "rate": (100 * len(wins) / len(rows)) if rows else None,
            "avg": (total / len(rows)) if rows else None, "total": total}


def result_text(s):
    if s["status"] == "open":
        return "Open"
    if s["status"] == "tp1":
        return "Target 1 hit"
    if s["status"] == "sl":
        return "Stop-loss hit"
    return f"Closed {s['r']:+.2f}R"


def save_journal(d, journal, t_now):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "journal.json"), "w") as f:
        json.dump(journal, f, indent=1)
    with open(os.path.join(d, "journal.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_ist", "mode", "symbol", "side", "score", "entry", "stop_loss", "target1", "target2",
                    "status", "exit_time_ist", "exit_price", "r_multiple"])
        for s in reversed(journal):
            w.writerow([s["time"], s["mode"], s["symbol"], s["side"], s["score"], round(s["entry"], 2),
                        round(s["sl"], 2), round(s["tp1"], 2), round(s["tp2"], 2), s["status"],
                        s["exit_time"] or "", round(s["exit_price"], 2) if s["exit_price"] else "", s["r"] if s["r"] is not None else ""])
    lines = ["# Signal journal", "",
             f"Updated {ist_str(t_now)} IST. Every alert is logged and marked when it hits target 1 (+1.5R) or the stop-loss (-1R).",
             "R means one unit of risk, the distance from entry to stop-loss. If both levels are touched inside one candle it counts as a loss. "
             "Intraday signals close at the end of the session; positional signals are given 10 trading days. "
             "Entry is taken at the signal candle's close, so real fills will differ. Not investment advice.", "",
             "## Results", "", "| Mode | Closed | Wins | Losses | Win rate | Average R | Total R |", "|---|---|---|---|---|---|---|"]
    for label, m in (("All", None), ("Intraday", "intraday"), ("MTF", "mtf")):
        st = journal_stats(journal, m)
        rate = f"{st['rate']:.0f}%" if st["rate"] is not None else "-"
        avg = f"{st['avg']:+.2f}" if st["avg"] is not None else "-"
        lines.append(f"| {label} | {st['closed']} | {st['wins']} | {st['losses']} | {rate} | {avg} | {st['total']:+.1f} |")
    n_open = sum(1 for s in journal if s["status"] == "open")
    lines += ["", f"Open signals: {n_open}."]
    if journal_stats(journal)["closed"] < 50:
        lines.append("Fewer than 50 closed signals so far, so the win rate is not reliable yet.")
    lines += ["", "## Latest signals", "", "| Time (IST) | Mode | Stock | Side | Entry | Stop-loss | Target 1 | Result |", "|---|---|---|---|---|---|---|---|"]
    for s in reversed(journal[-40:]):
        lines.append(f"| {s['time']} | {s['mode']} | {s['symbol']} | {s['side']} | {fmt(s['entry'])} | {fmt(s['sl'])} | {fmt(s['tp1'])} | {result_text(s)} |")
    with open(os.path.join(d, "summary.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------ main
def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show log lines while the scan runs
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["intraday", "mtf"], default=os.environ.get("MODE") or "intraday")
    ap.add_argument("--test", action="store_true", help="send one test message and exit")
    ap.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    ap.add_argument("--force", action="store_true", help="ignore the intraday time window")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--journal-dir", default=".")
    args = ap.parse_args()
    mode, dry = args.mode, args.dry_run

    if not dry and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set. Add them as repository secrets.")
        return 2

    if args.test:
        ok = send_telegram("Test alert from your NSE scanner. If you can read this, Telegram alerts are working.", dry)
        return 0 if ok else 3

    t_now = now()
    ist = dt.datetime.fromtimestamp(t_now, IST)
    mins = ist.hour * 60 + ist.minute

    journal = load_journal(args.journal_dir)
    changed = update_open_signals(journal, dry) if journal else False

    if mode == "intraday" and not args.force and (ist.weekday() >= 5 or mins < 570 or mins > 885):
        if changed:
            save_journal(args.journal_dir, journal, t_now)
        print(f"Outside the intraday window ({ist:%a %H:%M} IST). Nothing new to scan.")
        return 0

    opts = {
        "min_score": env_num("MIN_SCORE", 5),
        "adx_min": env_num("ADX_MIN", 20),
        "trend_align": (os.environ.get("TREND_ALIGN", "on").lower() != "off"),
        "max_stretch": env_num("MAX_STRETCH", 3),
    }
    sl_mult = env_num("SL_MULT", 1.5)
    cooldown_h = env_num("COOLDOWN_HOURS", 1 if mode == "intraday" else 20)
    long_only = mode == "mtf" or os.environ.get("DIRECTION", "both").lower() == "long"
    wl = [s.strip().upper() for s in os.environ.get("WATCHLIST", "").split(",") if s.strip()]
    symbols = wl or NIFTY50[:int(env_num("TOP_N", 50))]

    state = load_state(args.state)
    today, _ = ist_date_and_min(t_now)
    fails = stale = sent = 0
    first_err = ""
    rows = []

    started = time.time()
    for n, sym in enumerate(symbols, 1):
        if time.time() - started > 360:
            print(f"Time limit reached after {n - 1} stocks. Stopping this scan early.")
            break
        if n == 6 and fails >= 5 and not rows and not stale:
            print("The first 5 stocks all failed, so Yahoo is probably blocking this server. Stopping early.")
            break
        try:
            k1 = klines(sym, "15m" if mode == "intraday" else "1d")
            if mode == "intraday" and k1[-1]["date"] != today:
                stale += 1  # no candles today, probably a market holiday
                continue
            k4 = klines(sym, "1h" if mode == "intraday" else "1wk")
            r = score_setup(k1, k4, mode)
        except Exception as e:
            fails += 1
            first_err = first_err or str(e)
            print(f"{sym}: failed ({str(e)[:120]})")
            continue
        rows.append((sym, r["score"], r["price"]))
        if abs(r["score"]) >= opts["min_score"]:
            miss = apply_filters(r, opts)
            if long_only and r["score"] < 0:
                miss.append("short signals are off")
            if miss:
                print(f"{sym}: score {r['score']:+d} skipped: {', '.join(miss)}")
            else:
                key = f"{mode}|{sym}|{'L' if r['score'] > 0 else 'S'}"
                if t_now - state["sent"].get(key, 0) > cooldown_h * 3600:
                    r["why"].append(f"Filters passed: ADX {r['adx']:.0f}, {abs(r['stretch']):.1f} ATR from EMA20")
                    if send_telegram(build_message(sym, r, sl_mult, mode), dry):
                        state["sent"][key] = t_now
                        journal.append(new_record(mode, sym, r, sl_mult, k1, t_now))
                        changed = True
                        sent += 1
        time.sleep(0.3)

    with open(args.state, "w") as f:
        json.dump(state, f)
    if changed:
        save_journal(args.journal_dir, journal, t_now)

    rows.sort(key=lambda x: -abs(x[1]))
    print(f"Scanned {len(rows)} of {len(symbols)} stocks in {mode} mode. Signals sent: {sent}.")
    for sym, sc, px in rows[:10]:
        print(f"  {sym:<12} score {sc:+d}  price {fmt(px)}")
    if stale and not rows:
        print("No candles for today. The market may be closed (holiday).")
        return 0
    if fails and not rows:
        print("ERROR: every stock failed. First error:", first_err)
        return 1
    if fails:
        print(f"{fails} stock(s) skipped. First error: {first_err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
