"""BingX public market data (klines, funding, OI) + optional signed orders."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

log = logging.getLogger("bingx")
BASE = "https://open-api.bingx.com"
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_float(row: dict, keys: tuple[str, ...]) -> float | None:
    for k in keys:
        f = _to_float(row.get(k))
        if f is not None:
            return f
    return None


def _rows(data: Any) -> list[dict]:
    """Normaliza `data` de BingX (dict / lista / dict con lista dentro) a lista de dicts."""
    if isinstance(data, dict):
        for k in ("dataList", "list", "data"):
            if isinstance(data.get(k), list):
                data = data[k]
                break
        else:
            return [data]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


class BingX:
    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        timeout: float = 25.0,
        max_concurrency: int = 12,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "qulla-explosion/1.0"})
        # tope global de requests simultáneos (evita ráfagas que disparan el rate limit)
        self._sem = asyncio.Semaphore(max_concurrency)

    async def close(self) -> None:
        await self.client.aclose()

    async def _get(self, path: str, params: dict | None = None, retries: int = 3) -> Any:
        """GET con reintentos (429/5xx/timeouts/rate-limit de BingX) y backoff exponencial."""
        for attempt in range(retries + 1):
            last = attempt >= retries
            delay = 0.6 * (2**attempt)
            try:
                async with self._sem:
                    r = await self.client.get(f"{BASE}{path}", params=params or {})
                if r.status_code in _RETRY_STATUS and not last:
                    await asyncio.sleep(delay)
                    continue
                r.raise_for_status()
                data = r.json()
            except httpx.TransportError:
                if last:
                    raise
                await asyncio.sleep(delay)
                continue

            if isinstance(data, dict) and data.get("code") not in (None, 0, "0"):
                code, msg = str(data.get("code")), str(data.get("msg"))
                if not last and (code == "100410" or "limit" in msg.lower()):
                    await asyncio.sleep(delay)
                    continue
                raise RuntimeError(f"BingX {code}: {msg}")
            return data.get("data", data) if isinstance(data, dict) else data
        return None  # inalcanzable

    def _sign(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        qs = urlencode(params)
        params["signature"] = hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()
        return params

    # ------------------------------------------------------------------ mercado
    async def swap_tickers(self) -> list[dict]:
        data = await self._get("/openApi/swap/v2/quote/ticker")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("tickers", "list", "symbols", "data"):
                if isinstance(data.get(k), list):
                    return data[k]
        return []

    async def klines(self, symbol: str, interval: str = "4h", limit: int = 120) -> list[list]:
        data = await self._get(
            "/openApi/swap/v3/quote/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(data, list):
            return []
        out: list[list] = []
        for row in data:
            if isinstance(row, dict):
                out.append(
                    [
                        int(row.get("time") or row.get("t") or 0),
                        float(row.get("open") or row.get("o") or 0),
                        float(row.get("high") or row.get("h") or 0),
                        float(row.get("low") or row.get("l") or 0),
                        float(row.get("close") or row.get("c") or 0),
                        float(row.get("volume") or row.get("v") or 0),
                    ]
                )
            elif isinstance(row, (list, tuple)) and len(row) >= 6:
                out.append(
                    [int(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]
                )
        out.sort(key=lambda x: x[0])
        if not out:
            log.debug("klines vacías %s %s", symbol, interval)
        return out

    # ------------------------------------------------------------------ funding
    async def funding_history(self, symbol: str, limit: int = 21) -> list[float]:
        """Últimos funding rates (fracción), ordenados por tiempo ascendente. [] si falla."""
        for path in (
            "/openApi/swap/v2/quote/fundingRate",
            "/openApi/swap/v2/quote/fundingRate/history",
        ):
            try:
                data = await self._get(path, {"symbol": symbol, "limit": limit})
            except Exception as e:
                log.debug("funding hist %s %s: %s", path, symbol, e)
                continue
            pairs: list[tuple[float, float]] = []
            for row in _rows(data):
                v = _first_float(row, ("fundingRate", "lastFundingRate", "rate"))
                if v is None:
                    continue
                t = _first_float(row, ("fundingTime", "time")) or 0.0
                pairs.append((t, v))
            if pairs:
                pairs.sort(key=lambda x: x[0])
                return [v for _, v in pairs][-limit:]
        return []

    async def funding_stats(self, symbol: str, limit: int = 21) -> dict:
        """last (premiumIndex), avg (media del historial) y nº de prints. 3 llamadas máx."""
        last: float | None = None
        try:
            data = await self._get("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
            for row in _rows(data):
                last = _first_float(row, ("lastFundingRate", "fundingRate"))
                if last is not None:
                    break
        except Exception as e:
            log.debug("premiumIndex %s: %s", symbol, e)

        hist = await self.funding_history(symbol, limit=limit)
        if last is None and hist:
            last = hist[-1]
        avg = (sum(hist) / len(hist)) if hist else last
        return {"last": last, "avg": avg, "n_hist": len(hist)}

    async def funding_map(self, symbols: list[str], workers: int = 8) -> dict[str, dict]:
        """Funding por símbolo ({last, avg, n_hist}); omite los que fallan."""
        out: dict[str, dict] = {}
        sem = asyncio.Semaphore(workers)

        async def one(sym: str) -> None:
            async with sem:
                try:
                    st = await self.funding_stats(sym)
                except Exception as e:
                    log.debug("funding_stats %s: %s", sym, e)
                    return
            if st["last"] is not None or st["avg"] is not None:
                out[sym] = st

        await asyncio.gather(*[one(s) for s in symbols])
        return out

    # ------------------------------------------------------------------ open interest
    async def open_interest(self, symbol: str) -> float | None:
        """Open interest (contratos/notional según endpoint); None si no hay dato."""
        for path in (
            "/openApi/swap/v2/quote/openInterest",
            "/openApi/swap/v1/quote/openInterest",
        ):
            try:
                data = await self._get(path, {"symbol": symbol})
            except Exception as e:
                log.debug("OI %s %s: %s", path, symbol, e)
                continue
            for row in _rows(data):
                v = _first_float(
                    row,
                    ("openInterest", "openInterestAmount", "oi", "sumOpenInterest", "sumOpenInterestValue"),
                )
                if v is not None:
                    return v
        return None

    async def oi_map(self, symbols: list[str], workers: int = 10) -> dict[str, float]:
        """OI actual por símbolo; omite los que fallan."""
        out: dict[str, float] = {}
        sem = asyncio.Semaphore(workers)

        async def one(sym: str) -> None:
            async with sem:
                v = await self.open_interest(sym)
            if v is not None and v > 0:
                out[sym] = v

        await asyncio.gather(*[one(s) for s in symbols])
        return out

    # ------------------------------------------------------------------ órdenes
    async def place_market(
        self, symbol: str, side: str, quantity: float, position_side: str = "BOTH"
    ) -> dict:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("LIVE requiere API keys")
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "positionSide": position_side,
            "type": "MARKET",
            "quantity": quantity,
        }
        signed = self._sign(params)
        r = await self.client.post(
            f"{BASE}/openApi/swap/v2/trade/order",
            params=signed,
            headers={"X-BX-APIKEY": self.api_key},
        )
        r.raise_for_status()
        return r.json()
