#!/usr/bin/env python3
"""
PMI Dashboard - Script de mise à jour des données
Appelé par GitHub Actions toutes les heures.
Récupère les données depuis Yahoo Finance et calcule les indicateurs techniques.
"""
import requests
import json
import time
import sys
import os
from datetime import datetime, timezone, timedelta

TZ_PARIS = timezone(timedelta(hours=2))
now_utc = datetime.now(timezone.utc)
now_paris = datetime.now(TZ_PARIS)
print(f"[{now_paris.strftime('%d/%m/%Y %H:%M')}] Début mise à jour PMI Dashboard")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}
FALLBACK_PRICE = 173.75

# ─────────────────────────────────────────────────────────────
# FONCTIONS UTILITAIRES YAHOO FINANCE
# ─────────────────────────────────────────────────────────────

def yahoo_chart(ticker, range_="5d", interval="1d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": interval, "range": range_}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] Yahoo chart {ticker}: {e}")
        return None


def get_quote(ticker):
    data = yahoo_chart(ticker, "5d", "1d")
    if not data:
        return {}
    try:
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0)
        prev  = float(meta.get("regularMarketPreviousClose") or price)
        if price == 0:
            return {}
        return {
            "price":        round(price, 2),
            "open":         round(float(meta.get("regularMarketOpen") or price), 2),
            "high":         round(float(meta.get("regularMarketDayHigh") or price), 2),
            "low":          round(float(meta.get("regularMarketDayLow") or price), 2),
            "prev":         round(prev, 2),
            "volume":       f"{int(meta.get('regularMarketVolume') or 0)/1e6:.1f}M",
            "chg_abs":      round(price - prev, 2),
            "chg_pct":      round((price - prev) / prev * 100, 2) if prev else 0,
            "market_status": "open" if meta.get("marketState") == "REGULAR" else "closed",
        }
    except Exception as e:
        print(f"  [WARN] quote parse {ticker}: {e}")
        return {}


def get_history(ticker, days=260):
    data = yahoo_chart(ticker, f"{days}d", "1d")
    if not data:
        return [], []
    try:
        result = data["chart"]["result"][0]
        closes     = result["indicators"]["quote"][0]["close"]
        timestamps = result["timestamps"]
        pairs = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        return [c for _, c in pairs], [t for t, _ in pairs]
    except Exception as e:
        print(f"  [WARN] history parse {ticker}: {e}")
        return [], []


def get_competitor_info(ticker):
    q = get_quote(ticker)
    if not q:
        return {}
    try:
        url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        params = {"modules": "summaryDetail"}
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        sd = r.json()["quoteSummary"]["result"][0]["summaryDetail"]
        per  = sd.get("trailingPE", {}).get("raw")
        rend = sd.get("dividendYield", {}).get("raw")
        cap  = sd.get("marketCap", {}).get("raw", 0)
        return {
            "price":         q["price"],
            "chg_pct":       q["chg_pct"],
            "per":           round(per, 1) if per else None,
            "rendement":     round(rend * 100, 2) if rend else None,
            "capitalisation": f"{cap/1e9:.0f} Mrd$" if cap else None,
        }
    except Exception as e:
        print(f"  [WARN] summary {ticker}: {e}")
        return {"price": q.get("price"), "chg_pct": q.get("chg_pct"),
                "per": None, "rendement": None, "capitalisation": None}


# ─────────────────────────────────────────────────────────────
# INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────

def sma(closes, n):
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 2)


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 2)


def ema_series(closes, n):
    if len(closes) < n:
        return []
    k = 2 / (n + 1)
    e = sum(closes[:n]) / n
    series = [e]
    for p in closes[n:]:
        e = p * k + e * (1 - k)
        series.append(e)
    return series


def macd_calc(closes):
    if len(closes) < 35:
        return None
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    n   = min(len(e12), len(e26))
    macd_line = [e12[-(n-i)] - e26[-(n-i)] for i in range(n-1, -1, -1)]
    if len(macd_line) < 9:
        return None
    k9 = 2 / 10
    sig = sum(macd_line[:9]) / 9
    sig_series = [sig]
    for v in macd_line[9:]:
        sig = v * k9 + sig * (1 - k9)
        sig_series.append(sig)
    hist = [m - s for m, s in zip(macd_line[-len(sig_series):], sig_series)]
    hist7 = [round(h, 3) for h in hist[-7:]]
    lv = hist7[-1] if hist7 else 0
    return {
        "hist":       hist7,
        "labels":     ["J-30", "J-25", "J-20", "J-15", "J-10", "J-5", "J"],
        "signal":     "haussier" if lv > 0 else "baissier",
        "last_value": lv,
    }


def calc_perfs(closes, price):
    result = {}
    for key, days in [("pm_week_chg", 5), ("pm_month_chg", 22),
                      ("pm_ytd_chg", 110), ("pm_1y_chg", 252)]:
        if len(closes) >= days + 1:
            ref = closes[-(days + 1)]
            result[key] = round((price - ref) / ref * 100, 2)
    return result


def build_chart_data(closes, timestamps):
    if not closes:
        return {}
    result = {}
    periods = {"5d": 5, "1m": 22, "3m": 66, "6m": 130, "1y": 252}
    for key, trading_days in periods.items():
        n = min(trading_days, len(closes))
        if n < 2:
            continue
        c_slice = closes[-n:]
        t_slice = timestamps[-n:] if len(timestamps) >= n else []
        dates = [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%d/%m")
                 for t in t_slice] if t_slice else []
        result[key] = {
            "closes":  [round(v, 2) for v in c_slice],
            "dates":   dates,
            "min":     round(min(c_slice), 2),
            "max":     round(max(c_slice), 2),
            "start":   round(c_slice[0], 2),
            "end":     round(c_slice[-1], 2),
            "chg_pct": round((c_slice[-1] - c_slice[0]) / c_slice[0] * 100, 2),
        }
    return result


# ─────────────────────────────────────────────────────────────
# EUR/USD ET S&P 500
# ─────────────────────────────────────────────────────────────

def get_eurusd():
    data = yahoo_chart("EURUSD=X", "5d", "1d")
    if data:
        try:
            rate = float(data["chart"]["result"][0]["meta"]["regularMarketPrice"])
            if 0.5 < rate < 3.0:  # sanity check
                return {"eurusd": round(rate, 4), "usdeur": round(1 / rate, 4)}
        except Exception as e:
            print(f"  [WARN] eurusd parse: {e}")
    print("  [WARN] EUR/USD fallback")
    return {"eurusd": 1.1540, "usdeur": 0.8665}


def get_sp500():
    data = yahoo_chart("%5EGSPC", "5d", "1d")
    if data:
        try:
            meta  = data["chart"]["result"][0]["meta"]
            price = float(meta["regularMarketPrice"])
            prev  = float(meta["regularMarketPreviousClose"])
            return round((price - prev) / prev * 100, 2)
        except Exception as e:
            print(f"  [WARN] sp500 parse: {e}")
    return None


# ─────────────────────────────────────────────────────────────
# EXÉCUTION PRINCIPALE
# ─────────────────────────────────────────────────────────────

print("\n[1/5] Cours PM (historique 260 jours)...")
pm_closes, pm_ts = get_history("PM", 260)
pm_quote = get_quote("PM")
price = pm_quote.get("price", FALLBACK_PRICE)
print(f"      Prix: {price}$  Variation: {pm_quote.get('chg_pct', '?'):+}%  Points: {len(pm_closes)}")

print("\n[2/5] EUR/USD et S&P 500...")
fx     = get_eurusd()
sp500  = get_sp500()
print(f"      EUR/USD: {fx['eurusd']}  USD/EUR: {fx['usdeur']}  S&P500: {sp500}")

print("\n[3/5] Calculs techniques...")
sma50_v  = sma(pm_closes, 50)
sma200_v = sma(pm_closes, 200)
rsi_v    = rsi(pm_closes, 14)
macd_v   = macd_calc(pm_closes)
perf_v   = calc_perfs(pm_closes, price)
chart_v  = build_chart_data(pm_closes, pm_ts)
print(f"      SMA50={sma50_v}  SMA200={sma200_v}  RSI={rsi_v}")
if macd_v:
    print(f"      MACD signal={macd_v['signal']}  last={macd_v['last_value']}")

print("\n[4/5] Cours concurrents...")
competitors = {}
for ticker, name in [("MO", "Altria Group"), ("BTI", "British Am. Tobacco"), ("2914.T", "Japan Tobacco")]:
    print(f"      {ticker}...", end=" ")
    comp = get_competitor_info(ticker)
    competitors[ticker] = {"name": name, **comp}
    print(f"OK ({comp.get('price', '?')}$)" if comp.get('price') else "non disponible")
    time.sleep(0.8)

print("\n[5/5] Assemblage et sauvegarde data.json...")

# Charger l'existant pour préserver les données manuelles
try:
    with open("data.json", "r", encoding="utf-8") as f:
        existing = json.load(f)
    print("      data.json existant chargé")
except Exception:
    existing = {}
    print("      Nouveau data.json créé")

# Données automatiques
auto_data = {
    "pm_price":       pm_quote.get("price", FALLBACK_PRICE),
    "pm_chg_pct":     pm_quote.get("chg_pct", 0),
    "pm_chg_abs":     pm_quote.get("chg_abs", 0),
    "pm_open":        pm_quote.get("open", 0),
    "pm_high":        pm_quote.get("high", 0),
    "pm_low":         pm_quote.get("low", 0),
    "pm_volume":      pm_quote.get("volume", "—"),
    "pm_prev_close":  pm_quote.get("prev", FALLBACK_PRICE),
    "market_status":  pm_quote.get("market_status", "closed"),
    **fx,
    **perf_v,
    "sp500_chg":      sp500,
    "technicals": {
        "sma50":         sma50_v,
        "sma200":        sma200_v,
        "rsi14":         rsi_v,
        "macd":          macd_v,
        "sma50_signal":  "above" if sma50_v  and price > sma50_v  else "below",
        "sma200_signal": "above" if sma200_v and price > sma200_v else "below",
        "rsi_zone":      "suracheté" if rsi_v and rsi_v > 70
                         else "survendu" if rsi_v and rsi_v < 30
                         else "neutre",
    },
    "chart_data":  chart_v,
    "competitors": competitors,
    "updated_at":     now_utc.isoformat(),
    "updated_paris":  now_paris.strftime("%d/%m/%Y à %H:%M"),
    "is_fallback":    False,
}

# Fusionner : conserver les clés manuelles de l'existant
MANUAL_KEYS = ["manuel_analystes", "manuel_resultats", "manuel_calendrier"]
final = {**existing, **auto_data}
for k in MANUAL_KEYS:
    if k in existing:
        final[k] = existing[k]

with open("data.json", "w", encoding="utf-8") as f:
    json.dump(final, f, ensure_ascii=False, indent=2)

print(f"\n[OK] data.json sauvegardé")
print(f"     PM={price}$  EUR/USD={fx['eurusd']}  SMA50={sma50_v}  RSI={rsi_v}")
print(f"     Concurrents: {', '.join(k for k,v in competitors.items() if v.get('price'))}")
