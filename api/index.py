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
from sofascore_client import SofascoreClient

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
sofa = SofascoreClient()

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


# ---- Point-win-% signal (via Sofascore) ----
# Based on the well-established stat: winning 51% of total points in a
# match correlates to roughly an 85% match-win probability; 52%+ pushes
# that above 95%. If Kalshi's price hasn't caught up to that yet, that's
# the "underpriced given points scored" signal you're after.
POINT_PCT_UNDERPRICED_GAP = 15  # min gap between implied-by-points prob and Kalshi price to flag


def implied_prob_from_point_pct(pct: float):
    if pct is None:
        return None
    if pct >= 52:
        return 95
    if pct >= 51:
        return 85
    return None


def event_surname_parts(event_title: str):
    """'Nakashima vs Tiafoe' -> ('nakashima', 'tiafoe'). Returns (None, None)
    if the title doesn't parse cleanly."""
    if not event_title:
        return None, None
    parts = re.split(r"\s+vs\.?\s+", event_title, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None, None
    surname_a = parts[0].strip().lower().split()[-1] if parts[0].strip() else ""
    surname_b = parts[1].strip().lower().split()[-1] if parts[1].strip() else ""
    return surname_a, surname_b


def find_sofascore_match(surname_a: str, surname_b: str, sofa_events: list):
    """Search Sofascore's live tennis events for one whose home/away team
    names contain these two surnames (in either order). Returns
    {'event': <raw sofa event dict>, 'a_is_home': bool} or None."""
    if not surname_a or not surname_b:
        return None
    for ev in sofa_events:
        home = ((ev.get("homeTeam") or {}).get("name") or "").lower()
        away = ((ev.get("awayTeam") or {}).get("name") or "").lower()
        if surname_a in home and surname_b in away:
            return {"event": ev, "a_is_home": True}
        if surname_a in away and surname_b in home:
            return {"event": ev, "a_is_home": False}
    return None


def parse_set_progress(sofa_event: dict):
    """Pull sets won and current-set games from Sofascore's homeScore/
    awayScore fields (periodN = games won in set N). Returns
    {'sets_home': int, 'sets_away': int, 'current_set': int,
     'games_home': int, 'games_away': int} or None if unparseable."""
    try:
        home_score = sofa_event.get("homeScore") or {}
        away_score = sofa_event.get("awayScore") or {}
        sets_home = home_score.get("current")
        sets_away = away_score.get("current")
        # Find the highest period number that has data on either side —
        # that's the set currently being played (or just finished).
        current_set = 0
        games_home, games_away = 0, 0
        for n in range(1, 6):  # tennis maxes out at 5 sets
            key = f"period{n}"
            h, a = home_score.get(key), away_score.get(key)
            if h is not None or a is not None:
                current_set = n
                games_home, games_away = h or 0, a or 0
        if sets_home is None or sets_away is None or current_set == 0:
            return None
        return {
            "sets_home": sets_home, "sets_away": sets_away,
            "current_set": current_set,
            "games_home": games_home, "games_away": games_away,
        }
    except Exception:
        return None


def estimate_sets_remaining(sets_home: int, sets_away: int, best_of: int = 3):
    """Max sets left if the match goes the distance for whichever side is
    ahead. ASSUMES BEST-OF-3 by default — true for standard ATP/WTA tour
    and Challenger matches, but WRONG for men's Grand Slam singles
    (best-of-5). We don't have a reliable signal to distinguish these from
    Kalshi/Sofascore data alone, so this is a known simplification —
    treat the "time left" read with extra skepticism during Slams."""
    sets_to_win = (best_of // 2) + 1
    leader_sets = max(sets_home or 0, sets_away or 0)
    return max(sets_to_win - leader_sets, 0)


MIN_VOLUME_FOR_ENTRY = 500  # don't suggest entries on markets too thin to actually execute in


def compute_opportunity(price_yes_cents, point_win_pct, volume, sets_remaining):
    """Composite score: real edge (from points) x confidence x time-left x
    liquidity gate. Returns (score, reasons_dict) — score is 0 if any gate
    fails (not live, no edge, or too thin to trade)."""
    implied = implied_prob_from_point_pct(point_win_pct)
    if implied is None or price_yes_cents is None:
        return 0, None
    edge = implied - price_yes_cents
    if edge <= 0:
        return 0, None
    if volume is None or volume < MIN_VOLUME_FOR_ENTRY:
        return 0, None
    if sets_remaining is None:
        time_factor = 0.5  # unknown progress — don't fully zero it out, but don't reward it either
    elif sets_remaining <= 0:
        time_factor = 0.15  # match could end any point now — real edge, very little runway
    else:
        time_factor = min(sets_remaining / 2, 1.0)  # more sets left = more time to work the edge
    score = round(edge * time_factor, 1)
    return score, {"edge": edge, "time_factor": time_factor, "sets_remaining": sets_remaining}


def augment_event_with_points(event_title: str, sofa_events: list):
    """For one Kalshi event, try to find the matching live Sofascore match
    and pull total points won per side + set progress. Returns a dict like
    {'a_pct': 54.2, 'b_pct': 45.8, 'a_surname':..., 'b_surname':...,
     'sets_remaining': 1} or None if no match / no data. Never raises —
    this is a nice-to-have augmentation, not core functionality.
    """
    try:
        surname_a, surname_b = event_surname_parts(event_title)
        if not surname_a or not surname_b:
            return None
        match = find_sofascore_match(surname_a, surname_b, sofa_events)
        if not match:
            return None
        points = sofa.get_points_won(match["event"].get("id"))
        if not points:
            return None
        home, away = points.get("home"), points.get("away")
        total = (home or 0) + (away or 0)
        if total <= 0:
            return None
        a_points = home if match["a_is_home"] else away
        b_points = away if match["a_is_home"] else home

        progress = parse_set_progress(match["event"])
        sets_remaining = None
        if progress:
            leader_sets_a = progress["sets_home"] if match["a_is_home"] else progress["sets_away"]
            leader_sets_b = progress["sets_away"] if match["a_is_home"] else progress["sets_home"]
            sets_remaining = estimate_sets_remaining(leader_sets_a, leader_sets_b)

        return {
            "a_pct": round(a_points / total * 100, 1),
            "b_pct": round(b_points / total * 100, 1),
            "a_surname": surname_a,
            "b_surname": surname_b,
            "sets_remaining": sets_remaining,
        }
    except Exception:
        return None


def fetch_tennis_markets():
    # Query only the specific series tickers known to be live match-winner
    # markets (see TENNIS_MATCH_SERIES) instead of scanning by keyword,
    # which also catches annual futures, props, and doubles markets that
    # have no live score/price to flag on.

    # Fetch Sofascore's live tennis events ONCE per call (not once per
    # Kalshi event) — this is the expensive/fragile call, so minimize it.
    # If Sofascore is down/blocked, we just skip the points-based signal
    # entirely rather than breaking the whole markets fetch.
    try:
        sofa_events = sofa.list_live_tennis_events()
    except Exception:
        sofa_events = []

    processed = []
    raw_debug = []
    for series_ticker in TENNIS_MATCH_SERIES:
        events = client.list_events(series_ticker=series_ticker, status="open")
        for event in events:
            markets = event.get("markets") or client.list_markets(event_ticker=event.get("event_ticker"))
            event_title = event.get("title")
            points_signal = augment_event_with_points(event_title, sofa_events)

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

                point_win_pct = None
                underpriced_by_points = False
                sets_remaining = None
                opportunity_score = 0
                opportunity_detail = None
                if points_signal:
                    market_surname = (m.get("yes_sub_title") or m.get("subtitle") or "").strip().lower().split()
                    market_surname = market_surname[-1] if market_surname else ""
                    if market_surname == points_signal["a_surname"]:
                        point_win_pct = points_signal["a_pct"]
                    elif market_surname == points_signal["b_surname"]:
                        point_win_pct = points_signal["b_pct"]
                    implied = implied_prob_from_point_pct(point_win_pct)
                    if implied is not None and (implied - price_yes) >= POINT_PCT_UNDERPRICED_GAP:
                        underpriced_by_points = True
                    sets_remaining = points_signal.get("sets_remaining")
                    opportunity_score, opportunity_detail = compute_opportunity(
                        price_yes, point_win_pct, m.get("volume"), sets_remaining
                    )

                processed.append({
                    "event_title": event_title,
                    "ticker": m.get("ticker"),
                    "yes_sub_title": m.get("yes_sub_title") or m.get("subtitle"),
                    "price_yes_cents": price_yes,
                    "score_raw": score,
                    "volume": m.get("volume"),
                    "flagged": flagged,
                    "kalshi_url": kalshi_market_url(series_ticker, event.get("event_ticker")),
                    "point_win_pct": point_win_pct,
                    "underpriced_by_points": underpriced_by_points,
                    "sets_remaining": sets_remaining,
                    "opportunity_score": opportunity_score,
                    "opportunity_detail": opportunity_detail,
                    # True only if Sofascore confirms this match has actually
                    # started (i.e. we got real points data for it) — NOT
                    # just that Kalshi marked the market "open" for trading,
                    # which happens well before the match actually begins.
                    "is_live": points_signal is not None,
                })
                if len(raw_debug) < 5:
                    raw_debug.append(m)
    return processed, raw_debug


@app.route("/debug/sofascore")
@require_auth
def debug_sofascore():
    """Diagnostic: confirm Sofascore's unofficial API is reachable and show
    the raw shape of a few live tennis events, so we can verify the
    home/away team name fields match what our matching logic expects."""
    try:
        events = sofa.list_live_tennis_events()
        sample = []
        for ev in events[:10]:
            sample.append({
                "id": ev.get("id"),
                "home": (ev.get("homeTeam") or {}).get("name"),
                "away": (ev.get("awayTeam") or {}).get("name"),
                "tournament": (ev.get("tournament") or {}).get("name"),
            })
        return jsonify({"total_live_events": len(events), "sample": sample})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/debug/points/<int:event_id>")
@require_auth
def debug_points(event_id):
    """Diagnostic: dump the points-won stat for one specific Sofascore
    event id (find the id via /debug/sofascore first)."""
    try:
        points = sofa.get_points_won(event_id)
        return jsonify({"event_id": event_id, "points": points})
    except Exception as e:
        return jsonify({"error": str(e)})


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
  .points-badge { position:absolute; top:12px; right:14px; background:#e8b923;
    color:#241d00; font-size:11px; font-weight:700; padding:3px 8px; border-radius:20px; }
  .points-row { color:#e8b923; font-size:12px; margin-top:4px; }
  .prematch-tag { display:inline-block; background:#3a3a3a; color:var(--muted);
    font-size:10px; font-weight:700; padding:2px 6px; border-radius:10px; margin-left:6px;
    text-transform:uppercase; letter-spacing:.03em; }
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
  .entry-card { border-color:#0a3d24; background:#0f1f17; }
  .entry-rank { display:inline-block; background:var(--green); color:#06231a; font-size:11px;
    font-weight:800; padding:2px 7px; border-radius:20px; margin-right:6px; }
  .entry-detail { color:var(--muted); font-size:12px; margin-top:4px; line-height:1.4; }
  .entry-detail b { color:var(--green); }
</style>
</head>
<body>
  <h1>🎾 Kalshi Tennis Watch</h1>
  <div class="status" id="status">Loading...</div>
  <div class="error" id="error"></div>

  <div class="section-title">💰 Enter Now</div>
  <div class="hint">Real edge (from live points won) × how much match is left to work it × enough volume to actually execute. Only shows live matches with a genuine mispricing — not just volatility. "Time left" assumes best-of-3 (wrong for men's Slam matches, which are best-of-5 — treat those with extra caution).</div>
  <div id="entry-picks"></div>

  <div class="section-title">🎯 Best for Scalping</div>
  <div class="hint">Requires genuine back-and-forth reversals in the live price, not just a big range — a large one-directional move (price rushing toward 100 or 0) means the match is resolving, which is the worst time to enter, not the best. Needs a live match with real two-way price action to populate.</div>
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

  // Count direction reversals — a single one-directional run (e.g. price
  // rushing toward 100 or 0 as the match resolves) has ZERO reversals
  // despite a big range, and that's actually the WORST case to enter a
  // scalp on: there's less two-way action left, not more. Genuine chop
  // (price bouncing back and forth) has multiple reversals — that's the
  // real signal, matching your "choppy vs smooth" read.
  let reversals = 0;
  for (let i = 1; i < prices.length - 1; i++) {
    const diff1 = prices[i] - prices[i - 1];
    const diff2 = prices[i + 1] - prices[i];
    if (diff1 !== 0 && diff2 !== 0 && Math.sign(diff1) !== Math.sign(diff2)) {
      reversals++;
    }
  }

  const moves = hist.length - 1;
  return { range, moves, ticks: hist.length, reversals };
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
    // Only rank matches confirmed LIVE by Sofascore (is_live=true) — Kalshi
    // marks markets "open" well before a match actually starts, and thin
    // pre-match order books can show fake "volatility" that's really just
    // bid/ask noise on near-zero volume, not real live match action.
    //
    // Require at least 1 reversal — a big one-directional move (price
    // running toward 100 or 0 as the match resolves) is the WORST case to
    // enter on, not the best, even though it has a big range. Genuine
    // chop is what you're after.
    const scored = data.markets
      .filter(m => m.is_live)
      .map(m => ({ ...m, scalp: scalpMetrics(m.ticker) }))
      .filter(m => m.scalp && m.scalp.range >= 2 && m.scalp.reversals >= 1)
      .sort((a, b) => (b.scalp.reversals - a.scalp.reversals) || (b.scalp.range - a.scalp.range))
      .slice(0, 5);

    if (scored.length === 0) {
      scalpContainer.innerHTML = '<div class="empty">No confirmed-live matches showing genuine back-and-forth chop yet — a big one-directional move does not count (that is trending toward resolution, not scalpable). Check back once a match is showing real two-way action.</div>';
    } else {
      scalpContainer.innerHTML = scored.map((m, i) => `
        <div class="card scalp-card">
          <div class="event-title">${m.event_title || ''}</div>
          <div class="market-name"><span class="scalp-rank">#${i + 1}</span>${m.yes_sub_title || m.ticker || ''}</div>
          <div class="row">
            <span>±${m.scalp.range}¢, ${m.scalp.reversals} reversal${m.scalp.reversals === 1 ? '' : 's'} / ${m.scalp.ticks} ticks</span>
            <span class="price">${m.price_yes_cents ?? '-'}¢</span>
          </div>
          ${m.kalshi_url ? `<a href="${m.kalshi_url}" target="_blank" rel="noopener" class="kalshi-link">View on Kalshi &rarr;</a>` : ''}
        </div>
      `).join('');
    }

    // ---- Full list (existing behavior) ----
    // Sort priority: underpriced-by-points signal first, then the
    // close-score/skewed-price flag, then — importantly — live matches
    // above pre-match ones regardless of flags, since a live match is
    // always more relevant to you than one that hasn't started yet.
    const sorted = [...data.markets].sort((a, b) =>
      (b.underpriced_by_points - a.underpriced_by_points) ||
      (b.flagged - a.flagged) ||
      (b.is_live - a.is_live)
    );
    container.innerHTML = sorted.map(m => `
      <div class="card ${m.flagged || m.underpriced_by_points ? 'flagged' : ''}">
        ${m.underpriced_by_points
          ? '<div class="points-badge">UNDERPRICED (POINTS)</div>'
          : (m.flagged ? '<div class="flag-badge">CLOSE SCORE / SKEWED PRICE</div>' : '')}
        <div class="event-title">${m.event_title || ''}${!m.is_live ? '<span class="prematch-tag">Pre-match</span>' : ''}</div>
        <div class="market-name">${m.yes_sub_title || m.ticker || ''}</div>
        <div class="row">
          <span>Vol: ${m.volume ?? '-'}</span>
          <span class="price">${m.price_yes_cents ?? '-'}¢</span>
        </div>
        ${m.point_win_pct != null ? `<div class="points-row">Points won: ${m.point_win_pct}%</div>` : ''}
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
