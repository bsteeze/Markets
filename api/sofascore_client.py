"""
Unofficial Sofascore API client.

This is NOT an official/documented API — it's Sofascore's own internal
endpoints that power their website and app, reverse-engineered by the
community. Known risks, going in with eyes open:
  - Can be rate-limited or blocked (Cloudflare) with no warning
  - Field names/structure can change without notice, since it's not a
    supported product
  - Likely against Sofascore's ToS for automated/programmatic access,
    even though the underlying data is publicly viewable on their site

Used here for one specific purpose: pulling the "Points won" stat
(raw total points won per player so far in the match) to compute the
point-win-% signal, since Kalshi's own market data has no such field.

If this breaks or gets blocked, the app degrades gracefully — point-stat
augmentation just gets skipped, the rest of the dashboard keeps working.
"""

import requests

BASE = "https://api.sofascore.com/api/v1"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 8


class SofascoreClient:
    def list_live_tennis_events(self) -> list:
        """Returns raw list of currently live tennis events from Sofascore,
        each with homeTeam/awayTeam names and an id."""
        resp = requests.get(f"{BASE}/sport/tennis/events/live", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.json().get("events", [])

    def get_points_won(self, event_id) -> dict | None:
        """Returns {'home': int, 'away': int} total points won so far,
        or None if the stat isn't present (e.g. match hasn't started
        serving yet, or structure differs)."""
        resp = requests.get(f"{BASE}/event/{event_id}/statistics", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for period in data.get("statistics", []):
            for group in period.get("groups", []):
                for item in group.get("statisticsItems", []):
                    if item.get("key") == "pointsWon":
                        home = item.get("homeValue")
                        away = item.get("awayValue")
                        if home is not None and away is not None:
                            return {"home": home, "away": away}
        return None
