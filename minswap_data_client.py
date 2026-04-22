"""
Minswap Data API Client

Token universe discovery and market metrics via Minswap's free data API.
Separate from minswap_client.py (which talks to the aggregator API for swaps).

No API key required. Rate limited — space requests ~1/second.

API base: https://api-mainnet-prod.minswap.org
Docs: https://docs.minswap.org/developer/minswap-apis
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MINSWAP_DATA_URL = "https://api-mainnet-prod.minswap.org"

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    url = f"{MINSWAP_DATA_URL}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(
                url, json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "Minswap data %d on %s, retrying in %.0fs (%d/%d)",
                    resp.status_code, path, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{MINSWAP_DATA_URL}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code in (429, 500, 502, 503):
                wait = RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    "Minswap data %d on %s, retrying in %.0fs (%d/%d)",
                    resp.status_code, path, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            raise
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AssetMetadata:
    name: str
    ticker: str
    decimals: int
    description: str = ""
    url: str = ""
    logo: str = ""


@dataclass
class AssetMetric:
    """Token with market metrics from Minswap data API."""
    currency_symbol: str
    token_name: str
    is_verified: bool
    metadata: Optional[AssetMetadata]
    price: float
    price_change_24h: float
    volume_1h: float
    volume_24h: float
    volume_7d: float
    liquidity: float
    market_cap: float
    fully_diluted: float
    total_supply: float
    circulating_supply: float

    @property
    def token_id(self) -> str:
        """Full token ID (policyId + assetNameHex) for use with DEX clients."""
        if not self.currency_symbol:
            return "lovelace"
        return self.currency_symbol + self.token_name

    @property
    def ticker(self) -> str:
        if self.metadata and self.metadata.ticker:
            return self.metadata.ticker
        return self.token_name[:8] or "???"


@dataclass
class PoolMetric:
    """Liquidity pool with metrics from Minswap data API."""
    lp_asset_id: str
    protocol: str
    asset_a_symbol: str
    asset_a_name: str
    asset_a_ticker: str
    asset_b_symbol: str
    asset_b_name: str
    asset_b_ticker: str
    liquidity: float
    volume_24h: float
    volume_7d: float
    trading_fee_apr: float
    trading_fee_tier: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Assets API
# ---------------------------------------------------------------------------

def get_assets_metrics(
    sort_field: str = "volume_24h",
    sort_direction: str = "desc",
    limit: int = 100,
    only_verified: bool = False,
    search_after: Optional[list] = None,
    term: str = "",
    currency: Optional[str] = None,
) -> tuple[list[AssetMetric], Optional[list]]:
    """Fetch paginated asset metrics.

    Returns:
        Tuple of (asset_metrics, next_search_after). next_search_after is None
        when there are no more pages.
    """
    payload = {
        "term": term,
        "limit": limit,
        "only_verified": only_verified,
        "sort_direction": sort_direction,
        "sort_field": sort_field,
    }
    if search_after:
        payload["search_after"] = search_after
    if currency:
        payload["currency"] = currency

    data = _post("/v1/assets/metrics", payload)

    assets = []
    for item in data.get("asset_metrics", []):
        asset_raw = item.get("asset", {})
        meta_raw = asset_raw.get("metadata")
        metadata = None
        if meta_raw:
            metadata = AssetMetadata(
                name=meta_raw.get("name", ""),
                ticker=meta_raw.get("ticker", ""),
                decimals=meta_raw.get("decimals", 0),
                description=meta_raw.get("description", ""),
                url=meta_raw.get("url", ""),
                logo=meta_raw.get("logo", ""),
            )
        assets.append(AssetMetric(
            currency_symbol=asset_raw.get("currency_symbol", ""),
            token_name=asset_raw.get("token_name", ""),
            is_verified=asset_raw.get("is_verified", False),
            metadata=metadata,
            price=float(item.get("price", 0)),
            price_change_24h=float(item.get("price_change_24h", 0)),
            volume_1h=float(item.get("volume_1h", 0)),
            volume_24h=float(item.get("volume_24h", 0)),
            volume_7d=float(item.get("volume_7d", 0)),
            liquidity=float(item.get("liquidity", 0)),
            market_cap=float(item.get("market_cap", 0)),
            fully_diluted=float(item.get("fully_diluted", 0)),
            total_supply=float(item.get("total_supply", 0)),
            circulating_supply=float(item.get("circulating_supply", 0)),
        ))

    next_cursor = data.get("search_after")
    return assets, next_cursor


def get_asset_metrics(asset_id: str, currency: Optional[str] = None) -> Optional[AssetMetric]:
    """Fetch metrics for a single asset by its full ID (policyId + tokenName)."""
    params = {}
    if currency:
        params["currency"] = currency
    try:
        data = _get(f"/v1/assets/{asset_id}/metrics", params=params)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise

    asset_raw = data.get("asset", {})
    meta_raw = asset_raw.get("metadata")
    metadata = None
    if meta_raw:
        metadata = AssetMetadata(
            name=meta_raw.get("name", ""),
            ticker=meta_raw.get("ticker", ""),
            decimals=meta_raw.get("decimals", 0),
            description=meta_raw.get("description", ""),
            url=meta_raw.get("url", ""),
            logo=meta_raw.get("logo", ""),
        )
    return AssetMetric(
        currency_symbol=asset_raw.get("currency_symbol", ""),
        token_name=asset_raw.get("token_name", ""),
        is_verified=asset_raw.get("is_verified", False),
        metadata=metadata,
        price=float(data.get("price", 0)),
        price_change_24h=float(data.get("price_change_24h", 0)),
        volume_1h=float(data.get("volume_1h", 0)),
        volume_24h=float(data.get("volume_24h", 0)),
        volume_7d=float(data.get("volume_7d", 0)),
        liquidity=float(data.get("liquidity", 0)),
        market_cap=float(data.get("market_cap", 0)),
        fully_diluted=float(data.get("fully_diluted", 0)),
        total_supply=float(data.get("total_supply", 0)),
        circulating_supply=float(data.get("circulating_supply", 0)),
    )


# ---------------------------------------------------------------------------
# Pools API
# ---------------------------------------------------------------------------

def get_pools_metrics(
    sort_field: str = "liquidity",
    sort_direction: str = "desc",
    limit: int = 100,
    only_verified: bool = False,
    search_after: Optional[list] = None,
    term: str = "",
    currency: Optional[str] = None,
) -> tuple[list[PoolMetric], Optional[list]]:
    """Fetch paginated pool metrics.

    Returns:
        Tuple of (pool_metrics, next_search_after).
    """
    payload = {
        "term": term,
        "limit": limit,
        "only_verified": only_verified,
        "sort_direction": sort_direction,
        "sort_field": sort_field,
    }
    if search_after:
        payload["search_after"] = search_after
    if currency:
        payload["currency"] = currency

    data = _post("/v1/pools/metrics", payload)

    pools = []
    for item in data.get("pool_metrics", []):
        lp = item.get("lp_asset", {})
        lp_id = lp.get("currency_symbol", "") + lp.get("token_name", "")
        a = item.get("asset_a", {})
        b = item.get("asset_b", {})
        a_meta = a.get("metadata", {}) or {}
        b_meta = b.get("metadata", {}) or {}
        pools.append(PoolMetric(
            lp_asset_id=lp_id,
            protocol=item.get("type", ""),
            asset_a_symbol=a.get("currency_symbol", ""),
            asset_a_name=a.get("token_name", ""),
            asset_a_ticker=a_meta.get("ticker", ""),
            asset_b_symbol=b.get("currency_symbol", ""),
            asset_b_name=b.get("token_name", ""),
            asset_b_ticker=b_meta.get("ticker", ""),
            liquidity=float(item.get("liquidity", 0)),
            volume_24h=float(item.get("volume_24h", 0)),
            volume_7d=float(item.get("volume_7d", 0)),
            trading_fee_apr=float(item.get("trading_fee_apr", 0)),
            trading_fee_tier=item.get("trading_fee_tier", []),
        ))

    next_cursor = data.get("search_after")
    return pools, next_cursor


# ---------------------------------------------------------------------------
# Universe builder
# ---------------------------------------------------------------------------

def build_arb_universe(
    min_volume_ada: float = 500,
    min_liquidity_ada: float = 5000,
    max_tokens: int = 200,
) -> list[AssetMetric]:
    """Build the set of tokens worth scanning for arbitrage.

    Paginates through Minswap's asset metrics sorted by 24h volume,
    filtering by minimum volume and liquidity thresholds. Skips ADA itself.

    Returns:
        List of AssetMetric objects meeting the criteria, up to max_tokens.
    """
    universe = []
    search_after = None
    pages = 0
    max_pages = 20

    logger.info(
        "Building arb universe: min_volume=%.0f ADA, min_liquidity=%.0f ADA, max=%d",
        min_volume_ada, min_liquidity_ada, max_tokens,
    )

    while pages < max_pages and len(universe) < max_tokens:
        assets, next_cursor = get_assets_metrics(
            sort_field="volume_24h",
            sort_direction="desc",
            limit=100,
            only_verified=False,
            search_after=search_after or [],
        )

        if not assets:
            break

        for asset in assets:
            if not asset.currency_symbol:
                continue
            if asset.volume_24h < min_volume_ada:
                logger.info(
                    "Stopping at page %d: volume %.0f below threshold %.0f",
                    pages + 1, asset.volume_24h, min_volume_ada,
                )
                next_cursor = None
                break
            if asset.liquidity >= min_liquidity_ada:
                universe.append(asset)
                if len(universe) >= max_tokens:
                    break

        pages += 1
        search_after = next_cursor
        if not search_after:
            break
        time.sleep(1)

    logger.info("Universe built: %d tokens across %d pages", len(universe), pages)
    return universe
