"""
Kalshi API client (serverless-friendly version).

Same RSA-PSS signing scheme as before, but the private key is read from
an environment variable containing the raw PEM text (KALSHI_PRIVATE_KEY_PEM)
rather than a file path — because on Vercel there's no persistent
filesystem to keep a .pem file on, and you should NOT commit your
private key into a public GitHub repo.

Set KALSHI_PRIVATE_KEY_PEM in Vercel's dashboard (Project Settings ->
Environment Variables). Paste the full contents of the .pem file,
including the "-----BEGIN PRIVATE KEY-----" / "-----END PRIVATE KEY-----"
lines. Vercel supports multi-line env var values fine.
"""

import base64
import os
import time
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

API_BASE_HOST = "https://api.elections.kalshi.com"

KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID")
KALSHI_PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM")


class KalshiClient:
    def __init__(self):
        self.key_id = KALSHI_KEY_ID
        self.private_key = None
        if KALSHI_PRIVATE_KEY_PEM:
            try:
                self.private_key = serialization.load_pem_private_key(
                    KALSHI_PRIVATE_KEY_PEM.encode("utf-8"), password=None
                )
            except Exception as e:
                # A malformed/incomplete key must NEVER crash the whole app.
                # Fall back to unauthenticated requests (fine for public
                # market-listing endpoints) and surface the problem via
                # /api/markets' error field instead of a hard 500 at import time.
                print(f"WARNING: could not load KALSHI_PRIVATE_KEY_PEM, "
                      f"falling back to unauthenticated requests: {e}")
                self.private_key = None
                self.key_load_error = str(e)

    def _auth_headers(self, method: str, path: str) -> dict:
        if not (self.key_id and self.private_key):
            return {}
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method}{path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    def get(self, path: str, params: dict = None) -> dict:
        full_path = path if path.startswith("/trade-api") else f"/trade-api/v2{path}"
        url = f"{API_BASE_HOST}{full_path}"
        headers = self._auth_headers("GET", full_path)
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def list_events(self, series_ticker: str = None, status: str = "open", limit: int = 200) -> list:
        params = {"status": status, "limit": limit, "with_nested_markets": "true"}
        if series_ticker:
            params["series_ticker"] = series_ticker
        data = self.get("/events", params=params)
        return data.get("events", [])

    def list_markets(self, event_ticker: str = None, status: str = "open", limit: int = 200) -> list:
        params = {"status": status, "limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        data = self.get("/markets", params=params)
        return data.get("markets", [])

    def list_series(self, category: str = None) -> list:
        """List series (tournament/market-family metadata) - a much shorter
        list than raw events, so we can find tennis-related series_tickers
        first, then fetch only THEIR events instead of paging through
        thousands of unrelated events."""
        params = {}
        if category:
            params["category"] = category
        data = self.get("/series", params=params)
        return data.get("series", data if isinstance(data, list) else [])
