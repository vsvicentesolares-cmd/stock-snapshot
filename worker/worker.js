// Stock Snapshot data Worker (Cloudflare Workers, free tier).
// Fetches one stock's data from Yahoo Finance on demand and returns it as one normalized JSON.
//
//   GET /snapshot?symbol=ITX.MC&index=%5EIBEX   -> price history, profile, stats, financials, events, analysts, news
//   GET /search?q=inditex                        -> Yahoo symbol search (for tickers outside the index lists)
//
// Env vars (optional): ALLOWED_ORIGIN (e.g. https://<user>.github.io). Default "*".

const UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const Q1 = "https://query1.finance.yahoo.com";
const Q2 = "https://query2.finance.yahoo.com";
const CACHE_SECONDS = 900;

let session = null; // { cookie, crumb, at }

export default {
  async fetch(request, env, ctx) {
    const origin = env.ALLOWED_ORIGIN || "*";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });

    const url = new URL(request.url);
    const cache = caches.default;
    const cacheKey = new Request(url.toString(), { method: "GET" });
    if (url.searchParams.get("fresh") !== "1") {
      const hit = await cache.match(cacheKey);
      if (hit) {
        const r = new Response(hit.body, hit);
        Object.entries(cors).forEach(([k, v]) => r.headers.set(k, v));
        r.headers.set("X-Cache", "HIT");
        return r;
      }
    }

    let body, status = 200;
    try {
      if (url.pathname === "/snapshot") {
        const symbol = (url.searchParams.get("symbol") || "").trim();
        if (!/^[\w.\-^=&]{1,20}$/.test(symbol)) throw new HttpError(400, "bad symbol");
        body = await snapshot(symbol, (url.searchParams.get("index") || "").trim());
      } else if (url.pathname === "/search") {
        const q = (url.searchParams.get("q") || "").trim();
        if (!q || q.length > 40) throw new HttpError(400, "bad query");
        body = await search(q);
      } else if (url.pathname === "/" || url.pathname === "/health") {
        body = { ok: true, endpoints: ["/snapshot?symbol=&index=", "/search?q="] };
      } else {
        throw new HttpError(404, "not found");
      }
    } catch (e) {
      status = e.status || 502;
      body = { error: String(e.message || e) };
    }

    const res = new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": `public, max-age=${CACHE_SECONDS}`, ...cors },
    });
    if (status === 200) ctx.waitUntil(cache.put(cacheKey, res.clone()));
    return res;
  },
};

class HttpError extends Error {
  constructor(status, msg) { super(msg); this.status = status; }
}

// ------------------------------------------------------------------ Yahoo session (cookie + crumb)
async function getSession(force = false) {
  if (!force && session && Date.now() - session.at < 30 * 60 * 1000) return session;
  let cookie = "";
  for (const u of ["https://fc.yahoo.com", "https://finance.yahoo.com/"]) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA }, redirect: "manual" });
      const raw = typeof r.headers.getSetCookie === "function" ? r.headers.getSetCookie() : [r.headers.get("set-cookie") || ""];
      cookie = raw.map((c) => c.split(";")[0]).filter((c) => /^(A1|A3|B)=/.test(c)).join("; ");
      if (cookie) break;
    } catch (_) { /* try next */ }
  }
  if (!cookie) throw new Error("Yahoo cookie unavailable");
  const r = await fetch(`${Q2}/v1/test/getcrumb`, { headers: { "User-Agent": UA, Cookie: cookie } });
  const crumb = (await r.text()).trim();
  if (!r.ok || !crumb || crumb.length > 20 || crumb.includes("<")) throw new Error("Yahoo crumb unavailable");
  session = { cookie, crumb, at: Date.now() };
  return session;
}

async function yget(url, { auth = false } = {}) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { "User-Agent": UA, Accept: "application/json" };
    let u = url;
    if (auth) {
      const s = await getSession(attempt > 0);
      headers.Cookie = s.cookie;
      u += (u.includes("?") ? "&" : "?") + "crumb=" + encodeURIComponent(s.crumb);
    }
    const r = await fetch(u, { headers });
    if (r.ok) return r.json();
    if (!(auth && (r.status === 401 || r.status === 403))) throw new Error(`Yahoo ${r.status} for ${url.split("?")[0].split("/").slice(-2).join("/")}`);
  }
  throw new Error("Yahoo auth failed");
}

const raw = (x) => (x && typeof x === "object" ? (x.raw ?? null) : x ?? null);
const enc = encodeURIComponent;

// ------------------------------------------------------------------ snapshot
async function snapshot(symbol, indexSym) {
  const errors = [];
  const safe = (name, p) => p.catch((e) => { errors.push(`${name}: ${e.message || e}`); return null; });
  const now = Math.floor(Date.now() / 1000);

  const modules = [
    "price", "summaryDetail", "assetProfile", "defaultKeyStatistics", "financialData", "calendarEvents",
    "recommendationTrend", "upgradeDowngradeHistory", "earnings", "earningsHistory",
  ].join(",");
  const annual = ["TotalRevenue", "EBITDA", "OperatingIncome", "NetIncomeCommonStockholders", "DilutedEPS",
    "OperatingCashFlow", "CapitalExpenditure", "FreeCashFlow", "TotalDebt", "CashCashEquivalentsAndShortTermInvestments",
    "StockholdersEquity", "InterestExpense", "DilutedAverageShares"];
  const quarterly = ["TotalRevenue", "EBITDA", "OperatingIncome", "NetIncomeCommonStockholders", "DilutedEPS"];
  // Same request shape as yfinance: one call per timescale, period1 fixed at 2016-12-31, no padding/merge params.
  const tsUrl = (keys, scale) => `${Q2}/ws/fundamentals-timeseries/v1/finance/timeseries/${enc(symbol)}?symbol=${enc(symbol)}&type=${keys.map((k) => scale + k).join(",")}&period1=1483142400&period2=${now}`;

  const [qs, c1y, c5y, i1y, i5y, tsA, tsQ, news] = await Promise.all([
    safe("quoteSummary", yget(`${Q2}/v10/finance/quoteSummary/${enc(symbol)}?modules=${modules}`, { auth: true })),
    safe("chart", yget(`${Q1}/v8/finance/chart/${enc(symbol)}?range=1y&interval=1d&events=div%7Csplit`)),
    safe("chart5y", yget(`${Q1}/v8/finance/chart/${enc(symbol)}?range=5y&interval=1wk`)),
    indexSym ? safe("indexChart", yget(`${Q1}/v8/finance/chart/${enc(indexSym)}?range=1y&interval=1d`)) : null,
    indexSym ? safe("indexChart5y", yget(`${Q1}/v8/finance/chart/${enc(indexSym)}?range=5y&interval=1wk`)) : null,
    safe("financials", yget(tsUrl(annual, "annual"), { auth: true })),
    safe("financialsQ", yget(tsUrl(quarterly, "quarterly"), { auth: true })),
    safe("news", yget(`${Q2}/v1/finance/search?q=${enc(symbol)}&quotesCount=0&newsCount=10&enableFuzzyQuery=false`)),
  ]);

  const r = qs?.quoteSummary?.result?.[0] || {};
  const p = r.price || {}, sd = r.summaryDetail || {}, ap = r.assetProfile || {}, ks = r.defaultKeyStatistics || {},
    fd = r.financialData || {}, ce = r.calendarEvents || {};
  const chartMeta = c1y?.chart?.result?.[0]?.meta || {};

  if (!qs && !c1y) throw new HttpError(404, `No data for ${symbol}. ` + errors.join("; "));

  return {
    v: 1,
    symbol,
    index: indexSym || null,
    fetchedAt: new Date().toISOString(),
    quote: {
      name: p.longName || p.shortName || chartMeta.longName || chartMeta.shortName || symbol,
      exchange: p.exchangeName || chartMeta.exchangeName || null,
      currency: p.currency || chartMeta.currency || null,
      price: raw(p.regularMarketPrice) ?? chartMeta.regularMarketPrice ?? null,
      change: raw(p.regularMarketChange),
      changePct: raw(p.regularMarketChangePercent) != null ? raw(p.regularMarketChangePercent) * 100 : null,
      prevClose: raw(p.regularMarketPreviousClose) ?? chartMeta.chartPreviousClose ?? null,
      marketTime: raw(p.regularMarketTime) ?? chartMeta.regularMarketTime ?? null,
      marketState: p.marketState || null,
      marketCap: raw(p.marketCap) ?? raw(sd.marketCap),
      high52: raw(sd.fiftyTwoWeekHigh) ?? chartMeta.fiftyTwoWeekHigh ?? null,
      low52: raw(sd.fiftyTwoWeekLow) ?? chartMeta.fiftyTwoWeekLow ?? null,
      volume: raw(p.regularMarketVolume),
      avgVolume: raw(sd.averageVolume),
    },
    profile: {
      sector: ap.sector || null,
      industry: ap.industry || null,
      country: ap.country || null,
      city: ap.city || null,
      website: ap.website || null,
      employees: ap.fullTimeEmployees ?? null,
      summary: ap.longBusinessSummary || null,
      officers: (ap.companyOfficers || []).slice(0, 4).map((o) => ({ name: o.name, title: o.title })),
    },
    stats: {
      trailingPE: raw(sd.trailingPE),
      forwardPE: raw(sd.forwardPE) ?? raw(ks.forwardPE),
      priceToBook: raw(ks.priceToBook),
      evToEbitda: raw(ks.enterpriseToEbitda),
      evToRevenue: raw(ks.enterpriseToRevenue),
      enterpriseValue: raw(ks.enterpriseValue),
      dividendYield: raw(sd.dividendYield) ?? raw(sd.trailingAnnualDividendYield),
      payoutRatio: raw(sd.payoutRatio),
      beta: raw(sd.beta) ?? raw(ks.beta),
      sharesOutstanding: raw(ks.sharesOutstanding),
      floatShares: raw(ks.floatShares),
      totalDebt: raw(fd.totalDebt),
      totalCash: raw(fd.totalCash),
      ebitda: raw(fd.ebitda),
      revenue: raw(fd.totalRevenue),
      revenueGrowth: raw(fd.revenueGrowth),
      earningsGrowth: raw(fd.earningsGrowth),
      grossMargin: raw(fd.grossMargins),
      ebitdaMargin: raw(fd.ebitdaMargins),
      operatingMargin: raw(fd.operatingMargins),
      profitMargin: raw(fd.profitMargins),
      roe: raw(fd.returnOnEquity),
      debtToEquity: raw(fd.debtToEquity),
      financialCurrency: fd.financialCurrency || null,
    },
    chart: { "1y": series(c1y), "5y": series(c5y), dividends: dividends(c1y) },
    indexChart: { "1y": series(i1y), "5y": series(i5y), name: i1y?.chart?.result?.[0]?.meta?.shortName || indexSym || null },
    financials: timeseries([tsA, tsQ], annual, quarterly),
    events: {
      earningsDates: (ce.earnings?.earningsDate || []).map(raw).filter(Boolean),
      earningsCallDate: (ce.earnings?.earningsCallDate || []).map(raw).filter(Boolean)[0] || null,
      epsEstimate: raw(ce.earnings?.earningsAverage),
      revenueEstimate: raw(ce.earnings?.revenueAverage),
      exDividendDate: raw(ce.exDividendDate),
      dividendDate: raw(ce.dividendDate),
    },
    earnings: {
      history: (r.earningsHistory?.history || []).map((h) => ({
        quarter: raw(h.quarter), period: h.period || null,
        epsActual: raw(h.epsActual), epsEstimate: raw(h.epsEstimate), surprisePct: raw(h.surprisePercent),
      })),
      quarterly: (r.earnings?.financialsChart?.quarterly || []).map((q) => ({ date: q.date, revenue: raw(q.revenue), earnings: raw(q.earnings) })),
    },
    analysts: {
      recommendation: fd.recommendationKey || null,
      recommendationMean: raw(fd.recommendationMean),
      count: raw(fd.numberOfAnalystOpinions),
      targetMean: raw(fd.targetMeanPrice),
      targetHigh: raw(fd.targetHighPrice),
      targetLow: raw(fd.targetLowPrice),
      trend: (r.recommendationTrend?.trend || []).map((t) => ({
        period: t.period, strongBuy: t.strongBuy, buy: t.buy, hold: t.hold, sell: t.sell, strongSell: t.strongSell,
      })),
      actions: (r.upgradeDowngradeHistory?.history || []).slice(0, 12).map((a) => ({
        date: a.epochGradeDate, firm: a.firm, action: a.action, from: a.fromGrade, to: a.toGrade,
        priceTargetAction: a.priceTargetAction || null, currentPriceTarget: a.currentPriceTarget ?? null,
      })),
    },
    news: (news?.news || []).slice(0, 10).map((n) => ({
      title: n.title, publisher: n.publisher, link: n.link, time: n.providerPublishTime,
      related: n.relatedTickers || [],
    })),
    errors,
  };
}

function series(c) {
  const r = c?.chart?.result?.[0];
  if (!r || !r.timestamp) return null;
  const close = r.indicators?.adjclose?.[0]?.adjclose || r.indicators?.quote?.[0]?.close || [];
  const t = [], v = [];
  r.timestamp.forEach((ts, i) => { if (close[i] != null) { t.push(ts); v.push(+close[i].toFixed(4)); } });
  return { t, c: v, currency: r.meta?.currency || null };
}

function dividends(c) {
  const d = c?.chart?.result?.[0]?.events?.dividends || {};
  return Object.values(d).map((x) => ({ date: x.date, amount: x.amount })).sort((a, b) => a.date - b.date);
}

function timeseries(list, annual, quarterly) {
  const res = list.flatMap((ts) => ts?.timeseries?.result || []);
  const byType = {};
  let currency = null;
  for (const r of res) {
    const type = r.meta?.type?.[0];
    if (!type || !r[type]) continue;
    byType[type] = r[type].filter(Boolean).map((x) => {
      currency = currency || x.currencyCode || null;
      return { date: x.asOfDate, value: raw(x.reportedValue) };
    });
  }
  const build = (prefix, keys) => {
    const dates = new Set();
    keys.forEach((k) => (byType[prefix + k] || []).forEach((x) => dates.add(x.date)));
    return [...dates].sort().map((d) => {
      const row = { date: d };
      keys.forEach((k) => { const hit = (byType[prefix + k] || []).find((x) => x.date === d); row[k] = hit ? hit.value : null; });
      return row;
    });
  };
  return { currency, annual: build("annual", annual).slice(-5), quarterly: build("quarterly", quarterly).slice(-6) };
}

// ------------------------------------------------------------------ search
async function search(q) {
  const j = await yget(`${Q2}/v1/finance/search?q=${enc(q)}&quotesCount=10&newsCount=0&enableFuzzyQuery=false`);
  return {
    quotes: (j.quotes || []).filter((x) => x.quoteType === "EQUITY").map((x) => ({
      symbol: x.symbol, name: x.longname || x.shortname || x.symbol, exchange: x.exchDisp || x.exchange,
    })),
  };
}
