# Kalshi Tennis Watch

Scanner: polls Kalshi for live tennis markets, shows score + price, and
flags matches where the current set looks close but the market price
is lopsided. Two deployment paths are included:

- **`api/index.py`** — Vercel serverless version (for zephyr.guru via
  Vercel + GitHub). This is the one to deploy there.
- **`app.py`** — plain Flask version with a background poller, for if
  you ever run this on a VPS or home server instead. Not needed for
  the Vercel path.

## Deploying to Vercel (via GitHub)

1. Push this folder to a **GitHub repo** (can be private — Vercel can
   still deploy from a private repo).
2. In Vercel: New Project → import that repo. Vercel will detect
   `vercel.json` and use the Python builder automatically.
3. Before or right after the first deploy, set environment variables
   in **Vercel → Project → Settings → Environment Variables**:

   | Variable | Required | Value |
   |---|---|---|
   | `WATCH_USER` | yes | pick a username |
   | `WATCH_PASS` | yes | pick a password |
   | `KALSHI_KEY_ID` | optional | your Kalshi API key id |
   | `KALSHI_PRIVATE_KEY_PEM` | optional | full contents of your Kalshi private key `.pem` file, pasted as-is |

   **`WATCH_USER`/`WATCH_PASS` are required** — the app fails closed
   (returns 401) if they're not set, rather than running wide open.
   This is HTTP Basic Auth: your phone browser will prompt for
   username/password the first time you visit, then remember it.

4. Redeploy after setting env vars (Vercel → Deployments → ⋯ → Redeploy)
   so the function picks them up.
5. Visit `https://<your-vercel-domain-or-zephyr.guru-if-connected>/` —
   log in with the username/password from step 3.

**Never commit your `.pem` file to GitHub.** The `.gitignore` here
already excludes `*.pem`, but double check before pushing. The private
key lives only in Vercel's encrypted env var, not in the repo.

## Local testing before you deploy

```bash
cd kalshi-tennis-watch/api
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export WATCH_USER=test
export WATCH_PASS=test
export KALSHI_KEY_ID="your-key-id"          # optional
export KALSHI_PRIVATE_KEY_PEM="$(cat /path/to/key.pem)"  # optional
python -c "from index import app; app.run(port=8420, debug=True)"
```

Then visit `http://127.0.0.1:8420` locally.

## First run: fixing score parsing (important)

I could not verify Kalshi's exact live-score field names against a real
authenticated response while building this. Once you've got it running
against a live tennis match:

1. Visit `http://<host>:8420/debug/raw` — this dumps the last few raw
   market payloads Kalshi returned.
2. Look for whatever field actually holds the live set/game score
   (something like `"games": [7, 5]` or similar).
3. Tell me what that field looks like and I'll fix `extract_score()`
   in `app.py` to read it directly instead of the regex fallback.

Until that's fixed, the "close score" side of the flag logic will only
fire off the regex fallback (scraping "N-N" patterns from text fields),
which is a rough approximation — the price-skew detection works fine
regardless.

## Tuning the flag logic

In `app.py`:

- `PRICE_SKEW_THRESHOLD` — how far from 50¢ counts as "lopsided" (default 15,
  i.e. 65/35 or further)
- `CLOSE_GAME_MARGIN` — max game difference in the current set to count as
  "close" (default 1)
- `POLL_INTERVAL_SECONDS` — how often to re-poll Kalshi (default 15s)
- `TENNIS_KEYWORDS` — how events get matched as tennis (title/category text)

## Notes

- `API_BASE` in `kalshi_client.py` is set to Kalshi's current documented
  base URL as of this writing — double check against docs.kalshi.com if
  anything 404s, since Kalshi has changed hosts before.
- This polls rather than using Kalshi's websocket feed. If you want
  sub-second updates instead of a 15s poll, the next step is swapping
  the poll loop for their websocket API — happy to build that once the
  REST version is confirmed working end to end.
