# stock-snapshot

One-stock dashboard. Pick a market, type a ticker or name, Run. Returns price and valuation snapshot, price vs index, position in the country's market (index heatmap + rest-of-market block), sector peers, 5y financials and latest highlights (results, calendar, analysts, news).

## Architecture

| Piece | Path | Runs | Output |
|---|---|---|---|
| Universe builder | `scripts/build_universe.py`, `.github/workflows/universe.yml` | GitHub Actions, weekdays 22:37 UTC (+ 00:19 retry) and on demand | `universe/<CC>.json`, `universe/index.json` |
| Data Worker | `worker/worker.js` | Cloudflare Workers (free), on each Run | JSON per stock (`/snapshot`), symbol search (`/search`) |
| Page | `index.html`, `config.js` | GitHub Pages | Dashboard |

Data: Yahoo Finance (prices, profile, financials, analysts, news), constituents from Wikipedia / Nikkei Indexes / B3 / static list (IPC), market totals from WFE FOCUS (`config/market_totals.json`).

## v1 markets

| Code | Index | Constituents | Yahoo suffix |
|---|---|---|---|
| US | S&P 500 | Wikipedia | none |
| ES | IBEX 35 | Wikipedia | .MC |
| UK | FTSE 100 | Wikipedia | .L |
| MX | S&P/BMV IPC | static list in script | .MX |
| BR | Ibovespa | B3 portfolio API | .SA |
| DE | DAX 40 | Wikipedia | .DE |
| FR | CAC 40 | Wikipedia | .PA |
| NL | AEX | Wikipedia | .AS |
| CH | SMI | Wikipedia | .SW |
| JP | Nikkei 225 | Nikkei Indexes | .T |
| HK | Hang Seng | Wikipedia | .HK |

## Setup (once)

1. **Actions permissions**: Settings > Actions > General > Workflow permissions > Read and write.
2. **Build the universe**: Actions > universe > Run workflow (leave countries empty). First run 15-25 min (fetches shares, sectors, names for ~1,200 stocks); later runs are faster (shares refresh weekly).
3. **Worker**: dash.cloudflare.com > Workers & Pages > Create > Create Worker > name `stock-snapshot` > Deploy > Edit code > paste `worker/worker.js` > Deploy. Test `https://stock-snapshot.<sub>.workers.dev/snapshot?symbol=AAPL`.
   Optional: Settings > Variables > `ALLOWED_ORIGIN` = `https://vsvicentesolares-cmd.github.io`.
   CLI alternative: `cd worker && npx wrangler deploy`.
4. **Point the page at the Worker**: set `workerUrl` in `config.js`, commit.
5. **Pages**: Settings > Pages > Deploy from branch > `main` / root. URL: `https://vsvicentesolares-cmd.github.io/stock-snapshot/`.

Layout preview without the Worker: append `?demo=1` (synthetic data, ES and US only).
Deep link: `#ES/ITX.MC` runs that stock on load.

## Maintenance

| What | When | How |
|---|---|---|
| `config/market_totals.json` | Monthly | WFE FOCUS > Market statistics > domestic market cap (USD m). UK, FR, NL have no WFE split yet (null = no rest-of-market block). |
| IPC constituents (`MEX_STATIC`) | After Mar/Sep reviews | Edit list in `build_universe.py` |
| `config/share_classes.json` | After buybacks, capital increases, index reviews | Shares outstanding per class for companies with 2+ classes in an index (today Petrobras, Bradesco). Brazilian pairs missing from the file get an estimated split and a log line. |
| Failed country | When `universe/index.json` lists errors | Actions log shows the source that failed; the last good constituent list in `cache/members/` is reused |

## Known limits

- Yahoo endpoints are unofficial. The Worker handles the cookie/crumb flow; if Yahoo refuses it, quote, profile, financials and analysts come back empty while price charts and news still work (listed under "Missing pieces" at the bottom of the page).
- Constituent parsers for DAX, CAC, AEX, SMI, Nikkei, HSI and B3 are untested until the first Actions run.
- Market cap = shares outstanding x close (full, not float-adjusted). Weights approximate index weights.
- Companies with several share classes in an index (PETR3/PETR4, BBDC3/BBDC4, GOOGL/GOOG, FOXA/FOX, NWSA/NWS) are one heatmap block, one peer row and one rank. US class counts come from Yahoo per class; Alphabet's unlisted class B is not included.
- Banks and insurers have no EBITDA; those rows show "–".
- Worker responses are cached 15 min per symbol (`&fresh=1` bypasses).
