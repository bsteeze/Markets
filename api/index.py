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
TENNIS_KEYWORDS = ["tennis", "atp", "wta", "challenger"]
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
    """See README: verify real field name via /debug/raw on first live run,
    then swap this to read it directly instead of the regex fallback."""
    candidates = [
        market.get("live_score"),
        market.get("period_scores"),
        market.get("custom_strike"),
    ]
    for c in candidates:
        if c:
            return c
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
    events = client.list_events(status="open")
    tennis_events = [e for e in events if looks_like_tennis(e)]

    processed = []
    raw_debug = []
    for event in tennis_events:
        markets = event.get("markets") or client.list_markets(event_ticker=event.get("event_ticker"))
        for m in markets:
            price_yes = m.get("yes_bid") or m.get("last_price")
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
    """Diagnostic: list series (much shorter than raw events) so we can
    find the actual tennis-related series_tickers to query directly."""
    try:
        all_series = client.list_series()
        sample = [{
            "series_ticker": s.get("ticker") or s.get("series_ticker"),
            "title": s.get("title"),
            "category": s.get("category"),
        } for s in all_series[:40]]
        tennis_series = [s for s in sample if any(
            k in " ".join([str(s.get("title", "")), str(s.get("category", "")), str(s.get("series_ticker", ""))]).lower()
            for k in TENNIS_KEYWORDS
        )]
        return jsonify({
            "total_series_returned": len(all_series),
            "sample_of_first_40": sample,
            "tennis_matches_in_sample": tennis_series,
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
  .empty { color:var(--muted); text-align:center; padding:40px 0; }
</style>
</head>
<body>
  <h1>🎾 Kalshi Tennis Watch</h1>
  <div class="status" id="status">Loading...</div>
  <div class="error" id="error"></div>
  <div id="markets"></div>
<script>
async function refresh() {
  try {
    const res = await fetch('/api/markets');
    const data = await res.json();
    const statusEl = document.getElementById('status');
    const errorEl = document.getElementById('error');
    const container = document.getElementById('markets');
    statusEl.textContent = `Updated ${new Date().toLocaleTimeString()} - ${data.markets.length} markets`;
    errorEl.textContent = data.error ? `Error: ${data.error}` : '';
    if (!data.markets || data.markets.length === 0) {
      container.innerHTML = '<div class="empty">No live tennis markets found right now.</div>';
      return;
    }
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
