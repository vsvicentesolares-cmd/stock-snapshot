#!/usr/bin/env python3
"""Daily universe for the Stock Snapshot dashboard.

For each v1 country: benchmark index constituents (ticker, name, sector), last close, 1-day change and
market cap (shares outstanding x close, converted to USD), plus the rest-of-market figure from
config/market_totals.json. Feeds the country dropdown, the ticker suggestions and the heatmap.

Output: universe/<COUNTRY>.json and universe/index.json
Cache:  cache/members/<INDEX>.json (constituents, Yahoo symbol, shares, Yahoo sector/name; shares refreshed weekly)

Usage: python scripts/build_universe.py [--only ES,US]
Designed to run after all markets have closed (22:30 UTC), so the last daily bar is a complete session.
"""
import base64
import datetime as dt
import json
import os
import re
import sys
import time
from collections import Counter
from io import StringIO

import pandas as pd
import requests
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "universe")
MEM = os.path.join(ROOT, "cache", "members")
CFG = os.path.join(ROOT, "config", "market_totals.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; stock-snapshot/1.0; personal dashboard)"}
SHARES_MAX_AGE_DAYS = 7


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- symbol candidates (first one with data wins)
def has_suffix(t):
    return bool(re.search(r"\.[A-Z]{1,3}$", t))


def c_us(t):
    return [t.replace(".", "-")]


def c_suffix(*sfx):
    def f(t):
        t = t.strip().replace(" ", "")
        if has_suffix(t):
            return [t]
        return [t + s for s in sfx]
    return f


def c_ukx(t):
    if t.endswith(".L"):
        return [t]
    return [t.rstrip(".").replace(".", "-") + ".L"]


def c_hk(t):
    d = re.sub(r"\D", "", t)
    return [d.zfill(4) + ".HK"] if d else []


def c_mex(t):
    t = re.sub(r"[\s\*]", "", t)
    return [t if t.endswith(".MX") else t + ".MX"]


# ---------------------------------------------------------------- sector mapping for indices with odd labels
ICB = {
    "Technology": ["Software and Computer Services", "Technology Hardware and Equipment"],
    "Telecommunications": ["Telecommunications Equipment", "Telecommunications Service Providers"],
    "Health Care": ["Health Care Providers", "Medical Equipment and Services",
                    "Pharmaceuticals, Biotechnology and Marijuana Producers", "Pharmaceuticals and Biotechnology"],
    "Financials": ["Banks", "Finance and Credit Services", "Investment Banking and Brokerage Services",
                   "Mortgage Real Estate Investment Trusts", "Closed End Investments",
                   "Open End and Miscellaneous Investment Vehicles", "Life Insurance", "Non-life Insurance"],
    "Real Estate": ["Real Estate Investment and Services", "Real Estate Investment Trusts"],
    "Consumer Discretionary": ["Automobiles and Parts", "Consumer Services", "Household Goods and Home Construction",
                               "Leisure Goods", "Personal Goods", "Media", "Retailers", "Travel and Leisure"],
    "Consumer Staples": ["Beverages", "Food Producers", "Tobacco", "Personal Care, Drug and Grocery Stores"],
    "Industrials": ["Construction and Materials", "Aerospace and Defense", "Electronic and Electrical Equipment",
                    "General Industrials", "Industrial Engineering", "Industrial Support Services",
                    "Industrial Transportation"],
    "Basic Materials": ["Industrial Materials", "Industrial Metals and Mining", "Precious Metals and Mining", "Chemicals"],
    "Energy": ["Non-Renewable Energy", "Renewable Energy", "Oil, Gas and Coal", "Alternative Energy"],
    "Utilities": ["Electricity", "Gas, Water and Multi-utilities", "Waste and Disposal Services"],
}
ICB_MAP = {sec.lower(): ind for ind, secs in ICB.items() for sec in secs}
IBEX_GROUPS = {
    "Financials": ["financial services", "insurance", "banking", "banks"],
    "Utilities and energy": ["energy", "solar energy", "oil and gas", "electricity", "renewable energy", "utilities"],
    "Industrials and infrastructure": ["construction", "infrastructure", "aviation", "logistics", "manufacturing",
                                       "engineering", "defence", "defense"],
    "Materials": ["steel", "materials", "chemicals"],
    "Consumer": ["textile", "clothes and cosmetics", "tourism", "food", "retail", "hotels", "leisure"],
    "Telecom and tech": ["telecommunications", "information technology", "technology", "media"],
    "Health care": ["pharmaceuticals", "health care", "healthcare"],
    "Real estate": ["real estate"],
}
IBEX_MAP = {lab: g for g, labs in IBEX_GROUPS.items() for lab in labs}

# S&P/BMV IPC: no free machine-readable list. Composition after the 21 Sep 2026 review. Update each Mar/Sep.
MEX_STATIC = [
    ("AC", "Arca Continental", "Consumer Staples", ["AC.MX"]),
    ("ALPEK A", "Alpek", "Materials", ["ALPEKA.MX"]),
    ("ALSEA", "Alsea", "Consumer Discretionary", ["ALSEA.MX"]),
    ("AMX B", "America Movil", "Communication Services", ["AMXB.MX"]),
    ("ASUR B", "Grupo Aeroportuario del Sureste", "Industrials", ["ASURB.MX"]),
    ("BBAJIO O", "Banco del Bajio", "Financials", ["BBAJIOO.MX"]),
    ("BIMBO A", "Grupo Bimbo", "Consumer Staples", ["BIMBOA.MX"]),
    ("BOLSA A", "Bolsa Mexicana de Valores", "Financials", ["BOLSAA.MX"]),
    ("CEMEX CPO", "Cemex", "Materials", ["CEMEXCPO.MX"]),
    ("CHDRAUI B", "Grupo Comercial Chedraui", "Consumer Staples", ["CHDRAUIB.MX"]),
    ("FEMSA UBD", "FEMSA", "Consumer Staples", ["FEMSAUBD.MX"]),
    ("GAP B", "Grupo Aeroportuario del Pacifico", "Industrials", ["GAPB.MX"]),
    ("GCARSO A1", "Grupo Carso", "Industrials", ["GCARSOA1.MX"]),
    ("GCC", "GCC", "Materials", ["GCC.MX"]),
    ("GENTERA", "Gentera", "Financials", ["GENTERA.MX"]),
    ("GFINBUR O", "Grupo Financiero Inbursa", "Financials", ["GFINBURO.MX"]),
    ("GFNORTE O", "Grupo Financiero Banorte", "Financials", ["GFNORTEO.MX"]),
    ("GMEXICO B", "Grupo Mexico", "Materials", ["GMEXICOB.MX"]),
    ("GRUMA B", "Gruma", "Consumer Staples", ["GRUMAB.MX"]),
    ("KIMBER A", "Kimberly-Clark de Mexico", "Consumer Staples", ["KIMBERA.MX"]),
    ("KOF UBL", "Coca-Cola FEMSA", "Consumer Staples", ["KOFUBL.MX"]),
    ("LAB B", "Genomma Lab", "Health Care", ["LABB.MX"]),
    ("LACOMER UBC", "La Comer", "Consumer Staples", ["LACOMERUBC.MX"]),
    ("LIVEPOL C-1", "El Puerto de Liverpool", "Consumer Discretionary", ["LIVEPOLC-1.MX"]),
    ("MEGA CPO", "Megacable", "Communication Services", ["MEGACPO.MX"]),
    ("OMA B", "Grupo Aeroportuario Centro Norte", "Industrials", ["OMAB.MX"]),
    ("ORBIA", "Orbia", "Materials", ["ORBIA.MX"]),
    ("PE&OLES", "Penoles", "Materials", ["PE&OLES.MX"]),
    ("PINFRA", "Promotora y Operadora de Infraestructura", "Industrials", ["PINFRA.MX"]),
    ("Q", "Qualitas", "Financials", ["Q.MX"]),
    ("R A", "Regional", "Financials", ["RA.MX"]),
    ("SIGMAF", "Sigma Foods", "Consumer Staples", ["SIGMAF.MX", "SIGMAFA.MX", "ALFAA.MX"]),
    ("VESTA", "Corporacion Inmobiliaria Vesta", "Real Estate", ["VESTA.MX"]),
    ("VOLAR A", "Volaris", "Industrials", ["VOLARA.MX"]),
    ("WALMEX", "Walmart de Mexico", "Consumer Staples", ["WALMEX.MX"]),
]

# ---------------------------------------------------------------- countries
# src: "wiki" (Wikipedia table), "nikkei" (official component page), "b3" (official portfolio API), "static"
# sector: "source" = use the source's label (optionally mapped), "yahoo" = Yahoo sector (consistent, 11 groups)
# div: divide Yahoo close by this to get the currency unit (LSE quotes in pence)
COUNTRIES = [
    dict(code="US", name="United States", index="S&P 500", id="SPX", isym="^GSPC", cur="USD", src="wiki",
         url="https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
         tick=["symbol", "ticker"], ncol=["security", "company"], scol=["gics sector", "sector"],
         lo=490, hi=510, cands=c_us, sector="source"),
    dict(code="ES", name="Spain", index="IBEX 35", id="IBEX", isym="^IBEX", cur="EUR", src="wiki",
         url="https://en.wikipedia.org/wiki/IBEX_35",
         tick=["ticker", "symbol"], ncol=["company", "name"], scol=["sector", "industry"],
         lo=33, hi=37, cands=c_suffix(".MC"), sector="source", smap=lambda s: IBEX_MAP.get(s.lower(), s or "Other")),
    dict(code="UK", name="United Kingdom", index="FTSE 100", id="UKX", isym="^FTSE", cur="GBP", div=100, src="wiki",
         url="https://en.wikipedia.org/wiki/FTSE_100_Index",
         tick=["ticker", "epic", "symbol"], ncol=["company", "name"],
         scol=["ftse industry classification benchmark sector", "icb sector", "sector", "industry"],
         lo=98, hi=102, cands=c_ukx, sector="source", smap=lambda s: ICB_MAP.get(s.lower(), s or "Other")),
    dict(code="MX", name="Mexico", index="S&P/BMV IPC", id="MEXBOL", isym="^MXX", cur="MXN", src="static",
         static=MEX_STATIC, cands=c_mex, sector="source"),
    dict(code="BR", name="Brazil", index="Ibovespa", id="IBOV", isym="^BVSP", cur="BRL", src="b3",
         lo=70, hi=100, cands=c_suffix(".SA"), sector="yahoo"),
    dict(code="DE", name="Germany", index="DAX 40", id="DAX", isym="^GDAXI", cur="EUR", src="wiki",
         url="https://en.wikipedia.org/wiki/DAX",
         tick=["ticker", "symbol"], ncol=["company", "name"], scol=["prime standard sector", "sector", "industry"],
         lo=38, hi=42, cands=c_suffix(".DE"), sector="yahoo"),
    dict(code="FR", name="France", index="CAC 40", id="CAC", isym="^FCHI", cur="EUR", src="wiki",
         url="https://en.wikipedia.org/wiki/CAC_40",
         tick=["ticker", "symbol"], ncol=["company", "name"], scol=["sector", "gics sector", "industry"],
         lo=38, hi=42, cands=c_suffix(".PA", ".AS"), sector="yahoo"),
    dict(code="NL", name="Netherlands", index="AEX", id="AEX", isym="^AEX", cur="EUR", src="wiki",
         url="https://en.wikipedia.org/wiki/AEX_index",
         tick=["ticker symbol", "ticker", "symbol"], ncol=["company", "name"], scol=["icb sector", "sector", "industry"],
         lo=20, hi=32, cands=c_suffix(".AS"), sector="yahoo"),
    dict(code="CH", name="Switzerland", index="SMI", id="SMI", isym="^SSMI", cur="CHF", src="wiki",
         url="https://en.wikipedia.org/wiki/Swiss_Market_Index",
         tick=["ticker", "symbol"], ncol=["name", "company"], scol=["industry", "sector"],
         lo=18, hi=22, cands=c_suffix(".SW"), sector="yahoo"),
    dict(code="JP", name="Japan", index="Nikkei 225", id="N225", isym="^N225", cur="JPY", src="nikkei",
         url="https://indexes.nikkei.co.jp/en/nkave/index/component?idx=nk225",
         lo=220, hi=230, cands=c_suffix(".T"), sector="yahoo"),
    dict(code="HK", name="Hong Kong", index="Hang Seng", id="HSI", isym="^HSI", cur="HKD", src="wiki",
         url="https://en.wikipedia.org/wiki/Hang_Seng_Index",
         tick=["ticker", "stock code", "code", "symbol"], ncol=["name", "company", "constituent"],
         scol=["sub-index", "sector", "industry"], lo=60, hi=110, cands=c_hk, sector="yahoo"),
]

# USD per 1 unit of local currency: (Yahoo symbol, invert?)
FX = {"USD": None, "EUR": ("EURUSD=X", False), "GBP": ("GBPUSD=X", False), "CHF": ("CHF=X", True),
      "JPY": ("JPY=X", True), "HKD": ("HKD=X", True), "BRL": ("BRL=X", True), "MXN": ("MXN=X", True)}


# ---------------------------------------------------------------- constituent sources
def clean(x):
    s = re.sub(r"\[.*?\]", "", str(x)).strip()
    return "" if s.lower() in ("nan", "none") else s


def flat_cols(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join(clean(c) for c in col if clean(c)) for col in df.columns]
    df.columns = [clean(c) for c in df.columns]
    return df


def pick(cols, wanted):
    low = {c.lower(): c for c in cols}
    for w in wanted:
        if w in low:
            return low[w]
    for w in wanted:
        for lc, c in low.items():
            if w in lc:
                return c
    return None


def from_wikipedia(cx):
    html = requests.get(cx["url"], headers=UA, timeout=30).text
    for df in pd.read_html(StringIO(html)):
        df = flat_cols(df)
        tc = pick(df.columns, cx["tick"])
        if not tc or not (cx["lo"] <= len(df) <= cx["hi"]):
            continue
        nc = pick(df.columns, cx["ncol"])
        sc = pick(df.columns, cx["scol"])
        out = []
        for _, r in df.iterrows():
            t = clean(r[tc])
            if ":" in t:
                t = t.split(":")[-1].strip()
            if not t:
                continue
            out.append({"t": t, "n": clean(r[nc]) if nc else t, "s": clean(r[sc]) if sc else ""})
        if cx["lo"] <= len(out) <= cx["hi"]:
            return out
    raise ValueError("no constituents table matched")


def from_nikkei(cx):
    html = requests.get(cx["url"], headers=UA, timeout=30).text
    out = []
    for df in pd.read_html(StringIO(html)):
        df = flat_cols(df)
        cc = pick(df.columns, ["code"])
        nc = pick(df.columns, ["company name", "company", "name"])
        if not cc:
            continue
        for _, r in df.iterrows():
            code = re.sub(r"\D", "", clean(r[cc]))
            if code:
                out.append({"t": code, "n": clean(r[nc]) if nc else code, "s": ""})
    if not (cx["lo"] <= len(out) <= cx["hi"]):
        raise ValueError(f"nikkei page gave {len(out)} rows")
    return out


def from_b3(cx):
    q = {"language": "en-us", "pageNumber": 1, "pageSize": 120, "index": "IBOV", "segment": "1"}
    enc = base64.b64encode(json.dumps(q).encode()).decode()
    url = "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall/GetPortfolioDay/" + enc
    data = requests.get(url, headers=UA, timeout=30).json()
    out = [{"t": clean(r.get("cod")), "n": clean(r.get("asset")), "s": ""} for r in data.get("results", [])]
    out = [m for m in out if m["t"]]
    if not (cx["lo"] <= len(out) <= cx["hi"]):
        raise ValueError(f"b3 gave {len(out)} rows")
    return out


def load_cache(cx):
    p = os.path.join(MEM, cx["id"] + ".json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return []


def save_cache(cx, members):
    os.makedirs(MEM, exist_ok=True)
    with open(os.path.join(MEM, cx["id"] + ".json"), "w") as f:
        json.dump(members, f, ensure_ascii=False, indent=0)


def get_members(cx):
    cache = {m["t"]: m for m in load_cache(cx)}
    if cx["src"] == "static":
        fresh, src = [{"t": t, "n": n, "s": s, "alts": alts} for t, n, s, alts in cx["static"]], "static list"
    else:
        try:
            fresh = {"wiki": from_wikipedia, "nikkei": from_nikkei, "b3": from_b3}[cx["src"]](cx)
            src = cx["src"]
        except Exception as e:  # keep the last good list
            log(cx["id"], "constituents failed:", repr(e)[:200])
            return list(cache.values()), "cache" if cache else "none"
    for m in fresh:  # carry cached Yahoo symbol, shares, sector and name forward
        old = cache.get(m["t"], {})
        for k in ("y", "sh", "shAt", "ys", "yn"):
            if k in old:
                m[k] = old[k]
        if not m.get("alts"):
            m["alts"] = cx["cands"](m["t"])
    return [m for m in fresh if m.get("alts")], src


# ---------------------------------------------------------------- prices, shares, FX
def closes_for(symbols):
    df = yf.download(symbols, period="1mo", interval="1d", auto_adjust=False,
                     group_by="column", threads=True, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()
    c = df["Close"] if "Close" in df.columns.get_level_values(0) else df
    if isinstance(c, pd.Series):
        c = c.to_frame(symbols[0])
    c.index = pd.to_datetime(c.index).tz_localize(None).normalize() if getattr(c.index, "tz", None) else \
        pd.to_datetime(c.index).normalize()
    return c


def refetch_missing(members, closes, target):
    """Batch downloads sometimes return NaN for the latest bar of some tickers; retry those one by one."""
    def last(sym):
        if sym not in closes.columns:
            return ""
        s = closes[sym].dropna()
        return s.index[-1].date().isoformat() if len(s) else ""
    stale = [m["y"] for m in members if m.get("y") and last(m["y"]) < target]
    if stale:
        log("refetching", len(stale), "tickers")
    for sym in stale:
        try:
            h = yf.Ticker(sym).history(period="1mo", interval="1d", auto_adjust=False)["Close"].dropna()
            idx = pd.to_datetime(h.index)
            h.index = (idx.tz_localize(None) if getattr(idx, "tz", None) else idx).normalize()
            h = h[~h.index.duplicated(keep="last")]
            if len(h):
                closes = closes.reindex(closes.index.union(h.index))
                closes[sym] = h.reindex(closes.index)
        except Exception as e:
            log("refetch", sym, repr(e)[:120])
        time.sleep(0.3)
    return closes


def refresh_meta(members, cx, today):
    """Shares outstanding (weekly) plus Yahoo sector and long name (once)."""
    for m in members:
        at = m.get("shAt")
        fresh = at and (today - dt.date.fromisoformat(at)).days < SHARES_MAX_AGE_DAYS
        need_info = (cx["sector"] == "yahoo" and not m.get("ys")) or not m.get("yn")
        if fresh and m.get("sh") and not need_info:
            continue
        try:
            tk = yf.Ticker(m["y"])
            sh = None
            if not (fresh and m.get("sh")):
                try:
                    sh = tk.fast_info.get("shares")
                except Exception:
                    sh = None
            info = {}
            if need_info or not sh:
                try:
                    info = tk.info or {}
                except Exception:
                    info = {}
            sh = sh or info.get("sharesOutstanding")
            if sh:
                m["sh"], m["shAt"] = float(sh), today.isoformat()
            if info.get("sector"):
                m["ys"] = info["sector"]
            if info.get("longName") or info.get("shortName"):
                m["yn"] = info.get("longName") or info.get("shortName")
        except Exception as e:
            log("meta", m.get("y"), repr(e)[:120])
        time.sleep(0.15)


def fx_usd(cur):
    spec = FX.get(cur)
    if spec is None:
        return 1.0
    sym, inv = spec
    s = yf.Ticker(sym).history(period="5d", interval="1d")["Close"].dropna()
    v = float(s.iloc[-1])
    return 1 / v if inv else v


def index_change(isym):
    try:
        s = yf.Ticker(isym).history(period="1mo", interval="1d")["Close"].dropna()
        idx = pd.to_datetime(s.index)
        s.index = (idx.tz_localize(None) if getattr(idx, "tz", None) else idx).normalize()
        return s.index[-1].date().isoformat(), round((s.iloc[-1] / s.iloc[-2] - 1) * 100, 2), float(s.iloc[-1])
    except Exception as e:
        log("index", isym, repr(e)[:120])
        return None, None, None


# ---------------------------------------------------------------- build one country
def build(cx, totals, today):
    members, msrc = get_members(cx)
    if not members:
        raise RuntimeError("no constituents")
    syms = sorted({a for m in members for a in ([m["y"]] if m.get("y") else m["alts"])})
    closes = closes_for(syms)
    for m in members:  # pick the first candidate symbol with data
        if m.get("y") and m["y"] in closes.columns and closes[m["y"]].dropna().size:
            continue
        for a in m["alts"]:
            if a in closes.columns and closes[a].dropna().size:
                if m.get("y") != a:
                    for k in ("sh", "shAt", "ys", "yn"):
                        m.pop(k, None)
                m["y"] = a
                break
    members = [m for m in members if m.get("y")]
    idx_date, idx_d1, idx_level = index_change(cx["isym"])
    dates = [closes[m["y"]].dropna().index[-1].date().isoformat() for m in members
             if m["y"] in closes.columns and closes[m["y"]].dropna().size]
    cands = [d for d in (idx_date, Counter(dates).most_common(1)[0][0] if dates else None) if d]
    if not cands:
        raise RuntimeError("no prices")
    as_of = max(cands)
    closes = refetch_missing(members, closes, as_of)
    refresh_meta(members, cx, today)
    fx = fx_usd(cx["cur"])
    div = cx.get("div", 1)

    items = []
    for m in members:
        s = closes[m["y"]].dropna() if m["y"] in closes.columns else pd.Series(dtype=float)
        close = float(s.iloc[-1]) / div if len(s) else None
        last_d = s.index[-1].date().isoformat() if len(s) else None
        d1, flag = None, ""
        if last_d is None:
            flag = "no data"
        elif last_d != as_of:
            flag = "last close " + last_d
        elif len(s) > 1:
            d1 = round((float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100, 2)
        cap = m["sh"] * close if (m.get("sh") and close) else None
        sec = (m.get("ys") if cx["sector"] == "yahoo" else m.get("s")) or m.get("s") or m.get("ys") or "Other"
        if cx.get("smap") and cx["sector"] == "source":
            sec = cx["smap"](sec)
        name = m.get("n") if cx["src"] != "b3" else (m.get("yn") or m.get("n"))
        items.append({"y": m["y"], "t": m["t"], "n": name or m["t"], "s": sec, "cap": cap,
                      "d1": d1, "c": round(close, 4) if close else None, "f": flag})

    tot = sum(i["cap"] for i in items if i["cap"])
    sectors = sorted({i["s"] for i in items})
    wsum = wd = 0.0
    for i in items:
        i["w"] = round(i["cap"] / tot * 100, 4) if (i["cap"] and tot) else None
        i["capUSDm"] = round(i["cap"] * fx / 1e6, 1) if i["cap"] else None
        i["cap"] = round(i["cap"] / 1e6, 1) if i["cap"] else None  # local currency, millions
        if i["w"] is not None and i["d1"] is not None:
            wsum += i["w"]
            wd += i["w"] * i["d1"]
        i["s"] = sectors.index(i["s"])
        if not i["f"]:
            del i["f"]
    items.sort(key=lambda r: -(r["w"] or 0))
    for k, i in enumerate(items, 1):
        i["rank"] = k

    idx_usd = tot * fx / 1e6
    total_usd = (totals.get("totalsUSDm") or {}).get(cx["code"])
    market = None
    if total_usd:
        market = {"totalUSDm": round(total_usd, 1), "restUSDm": round(max(total_usd - idx_usd, 0), 1),
                  "indexShare": round(min(idx_usd / total_usd, 1) * 100, 1),
                  "asOf": totals.get("asOf"), "source": totals.get("source"),
                  "exchange": (totals.get("exchanges") or {}).get(cx["code"]),
                  "note": "index cap exceeds reported total" if idx_usd > total_usd else None}
    block = {
        "v": 1, "country": cx["code"], "name": cx["name"], "index": cx["index"], "indexId": cx["id"],
        "isym": cx["isym"], "currency": cx["cur"], "fxUSD": round(fx, 6), "asOf": as_of,
        "generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "members": msrc, "n": len(items), "priced": sum(1 for i in items if i["d1"] is not None),
        "idxLevel": idx_level, "idxD1": idx_d1, "capWeightedD1": round(wd / wsum, 2) if wsum else None,
        "idxCapUSDm": round(idx_usd, 1), "market": market, "sectors": sectors, "items": items,
        "source": "Prices, shares, sectors: Yahoo Finance (yfinance). Constituents: " +
                  {"wiki": "Wikipedia", "nikkei": "Nikkei Indexes", "b3": "B3", "static": "static list"}[cx["src"]] +
                  ". Market cap = shares outstanding x last close.",
    }
    save_cache(cx, [{k: m[k] for k in ("t", "n", "s", "y", "sh", "shAt", "ys", "yn", "alts") if k in m}
                    for m in members])
    return block


def main():
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1].upper().split(","))
    today = dt.datetime.now(dt.timezone.utc).date()
    totals = json.load(open(CFG)) if os.path.exists(CFG) else {}
    os.makedirs(OUT, exist_ok=True)
    idx_path = os.path.join(OUT, "index.json")
    index = json.load(open(idx_path)) if os.path.exists(idx_path) else {"countries": []}
    have = {c["code"]: c for c in index.get("countries", [])}
    errors = []
    for cx in COUNTRIES:
        if only and cx["code"] not in only:
            continue
        try:
            b = build(cx, totals, today)
            with open(os.path.join(OUT, cx["code"] + ".json"), "w") as f:
                json.dump(b, f, ensure_ascii=False, separators=(",", ":"))
            have[cx["code"]] = {"code": cx["code"], "name": cx["name"], "index": cx["index"],
                                "file": cx["code"] + ".json", "asOf": b["asOf"], "n": b["n"], "priced": b["priced"]}
            log(cx["code"], "ok", b["asOf"], b["priced"], "/", b["n"], "members:", b["members"])
        except Exception as e:
            log(cx["code"], "failed:", repr(e)[:300])
            errors.append(f"{cx['code']}: {repr(e)[:200]}")
    order = [c["code"] for c in COUNTRIES]
    index = {"generatedAt": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
             "countries": sorted(have.values(), key=lambda c: order.index(c["code"]) if c["code"] in order else 99),
             "errors": errors}
    with open(idx_path, "w") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    if not have:
        sys.exit(1)


if __name__ == "__main__":
    main()
