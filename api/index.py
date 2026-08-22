"""
Kalshi Tennis Watch — Vercel serverless version.

No background thread here (Vercel functions are stateless / short-lived —
a persistent poll loop won't survive between invocations). Instead:
  - Your phone's browser polls /api/markets every ~10-15s
  - Each hit to /api/markets fetches fresh data from Kalshi right then
  - Basic Auth protects every route so this isn't wide open on the internet

Env vars to set in Vercel dashboard (Project Settings -> Environment Variables):
    WATCH_USER               - username for basic auth
    WATCH_PASS                - password for basic auth
    KALSHI_KEY_ID             - (optional) your Kalshi API key id
    KALSHI_PRIVATE_KEY_PEM    - (optional) full PEM text of your private key

If KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM aren't set, requests go out
unauthenticated — fine for public market-listing endpoints, until/unless
Kalshi requires auth for those too.
"""

import os
import re
import secrets
import sys
from functools import wraps

from flask import Flask, jsonify, request, Response

# Vercel's Python runtime doesn't always add this file's own directory to
# sys.path, so a plain "from kalshi_client import ..." can fail with
# ModuleNotFoundError even though the file sits right next to it. Force it
# onto the path explicitly so the sibling-module import works regardless of
# how the runtime invokes this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kalshi_client import KalshiClient

# ---- tune these ----
PRICE_SKEW_THRESHOLD = 15   # cents from 50 to count as "lopsided"
CLOSE_GAME_MARGIN = 1       # current-set game diff <= this counts as "close"
TENNIS_KEYWORDS = ["tennis", "atp", "wta", "challenger"]  # kept for debug/series matching display
# The keyword filter above is too broad on its own — it also catches annual
# futures ("Men's Tournament Winner"), prop bets, doubles, etc. For the
# actual live match-winner markets (the ones with real-time score + price),
# Kalshi uses these specific series tickers:
TENNIS_MATCH_SERIES = [
    "KXATPMATCH",            # ATP Tennis Match (tour-level match winner)
    "KXWTAMATCH",            # WTA Tennis Match (tour-level match winner)
    "KXATPCHALLENGERMATCH",  # Challenger ATP match winner
    "KXWTACHALLENGERMATCH",  # Challenger WTA match winner
    "KXCHALLENGERMATCH",     # Challenger ATP (older/duplicate ticker)
]

# Kalshi's real market page URL pattern is:
#   https://kalshi.com/markets/{series_ticker_lower}/{display-slug}/{event_ticker_lower}
# Confirmed via live pages for these two; the display-slug for the
# Challenger tickers isn't confirmed, so those fall back to the series
# landing page (https://kalshi.com/markets/{series_ticker_lower}), which
# is a real working page even without the exact slug.
KALSHI_SERIES_SLUG = {
    "KXATPMATCH": "atp-tennis-match",
    "KXWTAMATCH": "wta-tennis-match",
}


def kalshi_market_url(series_ticker: str, event_ticker: str) -> str:
    series_lower = series_ticker.lower()
    slug = KALSHI_SERIES_SLUG.get(series_ticker)
    if slug and event_ticker:
        return f"https://kalshi.com/markets/{series_lower}/{slug}/{event_ticker.lower()}"
    return f"https://kalshi.com/markets/{series_lower}"
# ---------------------

app = Flask(__name__)
client = KalshiClient()

WATCH_USER = os.environ.get("WATCH_USER", "")
WATCH_PASS = os.environ.get("WATCH_PASS", "")
# For multiple logins, set WATCH_USERS as comma-separated "user:pass" pairs,
# e.g. "brian:hunter2,partner:otherpass". WATCH_USER/WATCH_PASS above still
# work too (as one more pair) so existing single-login setups don't break.
WATCH_USERS_RAW = os.environ.get("WATCH_USERS", "")


def _load_credentials() -> dict:
    creds = {}
    if WATCH_USER and WATCH_PASS:
        creds[WATCH_USER] = WATCH_PASS
    for pair in WATCH_USERS_RAW.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        user, _, pw = pair.partition(":")
        user, pw = user.strip(), pw.strip()
        if user and pw:
            creds[user] = pw
    return creds


CREDENTIALS = _load_credentials()


def check_auth(username, password):
    # secrets.compare_digest avoids leaking timing info about the password
    stored_pw = CREDENTIALS.get(username or "")
    if stored_pw is None:
        # still run compare_digest against something to avoid a timing
        # difference between "unknown user" and "wrong password"
        secrets.compare_digest(password or "", "")
        return False
    return secrets.compare_digest(password or "", stored_pw)


def require_auth(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not CREDENTIALS:
            # no credentials configured yet -> fail closed, don't run wide open
            return Response(
                "Server not configured: set WATCH_USER/WATCH_PASS or WATCH_USERS env vars.",
                401,
            )
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Login required", 401, {"WWW-Authenticate": 'Basic realm="Kalshi Tennis Watch"'}
            )
        return f(*args, **kwargs)
    return wrapped


def looks_like_tennis(event: dict) -> bool:
    text = " ".join([
        str(event.get("title", "")),
        str(event.get("category", "")),
        str(event.get("series_ticker", "")),
    ]).lower()
    return any(k in text for k in TENNIS_KEYWORDS)


def extract_score(market: dict):
    """
    IMPORTANT: verified against a real live match payload (Pegula vs
    Swiatek, WTA Cincinnati) — Kalshi's market data does NOT include a
    live set/game score field. "custom_strike" only carries an internal
    player ID (tennis_competitor), not a score.

    This means the "close score vs skewed price" flag currently has no
    real score data to work with from Kalshi alone — it will only ever
    fire via the regex fallback below (which won't match anything
    meaningful, since there's no score text in the payload either).

    To make the flag logic actually work as designed, you'd need a
    second data source for live score (e.g. a sports-data API) and
    cross-reference it with this market's event title / player names.
    Keeping this function as a no-op placeholder for now so the app
    doesn't crash — flag will just stay off until a score source exists.
    """
    text = " ".join([
        str(market.get("subtitle", "")),
        str(market.get("yes_sub_title", "")),
        str(market.get("title", "")),
    ])
    m = re.findall(r"\d+-\d+", text)
    if m:
        return {"raw_match": m}
    return None


def compute_flag(price_yes_cents, score) -> bool:
    if price_yes_cents is None:
        return False
    if abs(price_yes_cents - 50) < PRICE_SKEW_THRESHOLD:
        return False
    if isinstance(score, dict) and "games" in score:
        try:
            g1, g2 = score["games"]
            if abs(int(g1) - int(g2)) <= CLOSE_GAME_MARGIN:
                return True
        except Exception:
            pass
    return False


def fetch_tennis_markets():
    # Query only the specific series tickers known to be live match-winner
    # markets (see TENNIS_MATCH_SERIES) instead of scanning by keyword,
    # which also catches annual futures, props, and doubles markets that
    # have no live score/price to flag on.
    processed = []
    raw_debug = []
    for series_ticker in TENNIS_MATCH_SERIES:
        events = client.list_events(series_ticker=series_ticker, status="open")
        for event in events:
            markets = event.get("markets") or client.list_markets(event_ticker=event.get("event_ticker"))
            for m in markets:
                # Kalshi returns prices as dollar-string fields like "0.5900",
                # not integer cents under "yes_bid" as originally assumed.
                price_yes = None
                raw_price = m.get("yes_bid_dollars") or m.get("last_price_dollars")
                if raw_price not in (None, ""):
                    try:
                        price_yes = round(float(raw_price) * 100)
                    except (TypeError, ValueError):
                        price_yes = None
                if price_yes is None:
                    continue
                score = extract_score(m)
                flagged = compute_flag(price_yes, score)
                processed.append({
                    "event_title": event.get("title"),
                    "ticker": m.get("ticker"),
                    "yes_sub_title": m.get("yes_sub_title") or m.get("subtitle"),
                    "price_yes_cents": price_yes,
                    "score_raw": score,
                    "volume": m.get("volume"),
                    "flagged": flagged,
                    "kalshi_url": kalshi_market_url(series_ticker, event.get("event_ticker")),
                })
                if len(raw_debug) < 5:
                    raw_debug.append(m)
    return processed, raw_debug


@app.route("/")
@require_auth
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/markets")
@require_auth
def api_markets():
    try:
        processed, _ = fetch_tennis_markets()
        return jsonify({"markets": processed, "error": None})
    except Exception as e:
        return jsonify({"markets": [], "error": str(e)})


@app.route("/debug/raw")
@require_auth
def debug_raw():
    """
    Full diagnostic dump: shows the raw events Kalshi returns BEFORE our
    tennis filter is applied, so we can see actual category/title/series_ticker
    values and figure out why a known-live match isn't matching.
    """
    try:
        events = client.list_events(status="open")
        sample = []
        for e in events[:15]:
            sample.append({
                "title": e.get("title"),
                "category": e.get("category"),
                "series_ticker": e.get("series_ticker"),
                "event_ticker": e.get("event_ticker"),
                "status": e.get("status"),
            })
        tennis_matches = [e for e in events if looks_like_tennis(e)]
        return jsonify({
            "total_events_returned": len(events),
            "sample_of_first_15_events": sample,
            "tennis_keyword_matches": len(tennis_matches),
            "tennis_match_titles": [e.get("title") for e in tennis_matches[:10]],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug/series")
@require_auth
def debug_series():
    """Diagnostic: check each whitelisted match-level series ticker for
    live/open events right now, so we can confirm which ones are active."""
    try:
        results = {}
        for series_ticker in TENNIS_MATCH_SERIES:
            events = client.list_events(series_ticker=series_ticker, status="open")
            results[series_ticker] = {
                "open_event_count": len(events),
                "sample_titles": [e.get("title") for e in events[:5]],
            }
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug/event/<series_ticker>")
@require_auth
def debug_one_event(series_ticker):
    """Dump the FULL raw structure of the first open event for a given
    series ticker, so we can see actual field names (event_ticker vs ticker,
    whether 'markets' is nested, etc) instead of guessing."""
    try:
        events = client.list_events(series_ticker=series_ticker, status="open")
        if not events:
            return jsonify({"error": f"No open events found for {series_ticker}"})
        return jsonify({
            "event_count": len(events),
            "first_event_full": events[0],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug/market/<ticker>")
@require_auth
def debug_one_market(ticker):
    """Fetch and dump one specific market's full raw payload by ticker,
    for inspecting exact score field names on a known-live match."""
    try:
        data = client.get(f"/markets/{ticker}")
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)})


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kalshi Tennis Watch</title>
<style>
  :root {
    --bg: #0b0b0d; --card: #17171b; --border: #2a2a30;
    --text: #f2f2f2; --muted: #8a8a92; --green: #21c37a; --red: #ff4d5e;
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; padding:16px; }
  h1 { font-size:20px; margin:0 0 4px 0; }
  .status { color:var(--muted); font-size:13px; margin-bottom:16px; }
  .error { color:var(--red); font-size:13px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:14px;
    padding:14px 16px; margin-bottom:12px; position:relative; }
  .card.flagged { border-color:var(--green); box-shadow:0 0 0 1px var(--green); }
  .flag-badge { position:absolute; top:12px; right:14px; background:var(--green);
    color:#06231a; font-size:11px; font-weight:700; padding:3px 8px; border-radius:20px; }
  .event-title { color:var(--muted); font-size:12px; text-transform:uppercase;
    letter-spacing:.04em; margin-bottom:4px; }
  .market-name { font-size:16px; font-weight:600; margin-bottom:8px; }
  .row { display:flex; justify-content:space-between; align-items:center;
    font-size:13px; color:var(--muted); }
  .price { font-size:18px; font-weight:700; color:var(--green); }
  .kalshi-link { display:block; margin-top:10px; font-size:12px; color:var(--green);
    text-decoration:none; font-weight:600; }
  .kalshi-link:hover { text-decoration:underline; }
  .empty { color:var(--muted); text-align:center; padding:40px 0; }
  .section-title { font-size:13px; font-weight:700; color:var(--muted);
    text-transform:uppercase; letter-spacing:.06em; margin:20px 0 10px 0; }
  .section-title:first-of-type { margin-top:0; }
  .scalp-card { border-color:#3a3a10; background:#1c1c12; }
  .scalp-rank { display:inline-block; background:#e8b923; color:#241d00; font-size:11px;
    font-weight:800; padding:2px 7px; border-radius:20px; margin-right:6px; }
  .scalp-range { color:#e8b923; font-weight:700; }
  .hint { color:var(--muted); font-size:12px; margin-bottom:16px; line-height:1.4; }
</style>
</head>
<body>
  <h1>🎾 Kalshi Tennis Watch</h1>
  <div class="status" id="status">Loading...</div>
  <div class="error" id="error"></div>

  <div class="section-title">🎯 Best for Scalping</div>
  <div class="hint">Ranked by how much the price has actually moved while this page has been open — bigger swings mean more two-way action to work, same as your live-scalp read on a choppy vs. smooth graph. Needs a few minutes of live data to populate.</div>
  <div id="scalp-picks"></div>

  <div class="section-title">All Live Markets</div>
  <div id="markets"></div>

<script>
// Client-side price history — this app has no database, so "choppy vs
// smooth" volatility is tracked in-memory in the browser for as long as
// this tab stays open. Resets on reload; needs a few poll cycles to be
// meaningful (each poll is ~10s, so give it 2-3 minutes for a real read).
let priceHistory = {}; // ticker -> [{t, price}]
const HISTORY_WINDOW_MS = 6 * 60 * 1000; // keep last 6 minutes of ticks
const MIN_TICKS_FOR_SCORE = 4;           // need at least this many polls before ranking

function updateHistory(markets) {
  const now = Date.now();
  markets.forEach(m => {
    if (m.price_yes_cents == null || !m.ticker) return;
    if (!priceHistory[m.ticker]) priceHistory[m.ticker] = [];
    const hist = priceHistory[m.ticker];
    const last = hist[hist.length - 1];
    if (!last || last.price !== m.price_yes_cents) {
      hist.push({ t: now, price: m.price_yes_cents });
    }
    priceHistory[m.ticker] = hist.filter(p => now - p.t <= HISTORY_WINDOW_MS);
  });
}

function scalpMetrics(ticker) {
  const hist = priceHistory[ticker] || [];
  if (hist.length < MIN_TICKS_FOR_SCORE) return null;
  const prices = hist.map(p => p.price);
  const range = Math.max(...prices) - Math.min(...prices);
  const moves = hist.length - 1; // number of actual price changes seen
  return { range, moves, ticks: hist.length };
}

async function refresh() {
  try {
    const res = await fetch('/api/markets');
    const data = await res.json();
    const statusEl = document.getElementById('status');
    const errorEl = document.getElementById('error');
    const container = document.getElementById('markets');
    const scalpContainer = document.getElementById('scalp-picks');

    statusEl.textContent = `Updated ${new Date().toLocaleTimeString()} - ${data.markets.length} markets`;
    errorEl.textContent = data.error ? `Error: ${data.error}` : '';

    if (!data.markets || data.markets.length === 0) {
      container.innerHTML = '<div class="empty">No live tennis markets found right now.</div>';
      scalpContainer.innerHTML = '';
      return;
    }

    updateHistory(data.markets);

    // ---- Best for Scalping section ----
    const scored = data.markets
      .map(m => ({ ...m, scalp: scalpMetrics(m.ticker) }))
      .filter(m => m.scalp && m.scalp.range >= 2) // require some real movement
      .sort((a, b) => b.scalp.range - a.scalp.range)
      .slice(0, 5);

    if (scored.length === 0) {
      scalpContainer.innerHTML = '<div class="empty">Building price history — check back in a couple minutes once markets have ticked a few times.</div>';
    } else {
      scalpContainer.innerHTML = scored.map((m, i) => `
        <div class="card scalp-card">
          <div class="event-title">${m.event_title || ''}</div>
          <div class="market-name"><span class="scalp-rank">#${i + 1}</span>${m.yes_sub_title || m.ticker || ''}</div>
          <div class="row">
            <span>Moved <span class="scalp-range">±${m.scalp.range}¢</span> over ${m.scalp.ticks} ticks</span>
            <span class="price">${m.price_yes_cents ?? '-'}¢</span>
          </div>
          ${m.kalshi_url ? `<a href="${m.kalshi_url}" target="_blank" rel="noopener" class="kalshi-link">View on Kalshi &rarr;</a>` : ''}
        </div>
      `).join('');
    }

    // ---- Full list (existing behavior) ----
    const sorted = [...data.markets].sort((a, b) => (b.flagged - a.flagged));
    container.innerHTML = sorted.map(m => `
      <div class="card ${m.flagged ? 'flagged' : ''}">
        ${m.flagged ? '<div class="flag-badge">CLOSE SCORE / SKEWED PRICE</div>' : ''}
        <div class="event-title">${m.event_title || ''}</div>
        <div class="market-name">${m.yes_sub_title || m.ticker || ''}</div>
        <div class="row">
          <span>Vol: ${m.volume ?? '-'}</span>
          <span class="price">${m.price_yes_cents ?? '-'}¢</span>
        </div>
        ${m.kalshi_url ? `<a href="${m.kalshi_url}" target="_blank" rel="noopener" class="kalshi-link">View on Kalshi &rarr;</a>` : ''}
      </div>
    `).join('');
  } catch (e) {
    document.getElementById('error').textContent = 'Failed to reach server: ' + e;
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
