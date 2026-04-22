"""
Blockfrost API Client

Shared Cardano chain query and transaction submission client.
Used by snekfun_client and available for any module that needs
UTXOs, protocol parameters, or raw tx submission.

API docs: https://docs.blockfrost.io
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BLOCKFROST_BASE_URL = os.environ.get(
    "BLOCKFROST_BASE_URL", "https://cardano-mainnet.blockfrost.io/api/v0"
)

MAX_RETRIES = 3
RETRY_BACKOFF = 2.0


class BlockfrostClient:
    """Lightweight Blockfrost client for Cardano mainnet."""

    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or os.environ.get("BLOCKFROST_PROJECT_ID", "")
        if not self.project_id:
            raise RuntimeError("BLOCKFROST_PROJECT_ID is required")
        self.base_url = BLOCKFROST_BASE_URL
        self.session = requests.Session()
        self.session.headers.update({"project_id": self.project_id})

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", 30)

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.request(method, url, **kwargs)

                if resp.status_code in (429, 500, 502, 503):
                    wait = RETRY_BACKOFF * (attempt + 1)
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

    def _get(self, path: str, params: Optional[dict] = None):
        return self._request("GET", path, params=params)

    # ------------------------------------------------------------------
    # Address queries
    # ------------------------------------------------------------------

    def get_utxos(self, address: str) -> list[dict]:
        """Fetch all UTXOs at an address (descending order)."""
        return self._get(f"/addresses/{address}/utxos?order=desc")

    def get_address_transactions(self, address: str, count: int = 5) -> list[dict]:
        return self._get(f"/addresses/{address}/transactions?order=desc&count={count}")

    # ------------------------------------------------------------------
    # Transaction queries
    # ------------------------------------------------------------------

    def get_tx_utxos(self, tx_hash: str) -> dict:
        """Get inputs and outputs for a transaction."""
        return self._get(f"/txs/{tx_hash}/utxos")

    # ------------------------------------------------------------------
    # Protocol parameters
    # ------------------------------------------------------------------

    def get_protocol_parameters(self) -> dict:
        return self._get("/epochs/latest/parameters")

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit_tx(self, tx_cbor_hex: str) -> str:
        """Submit a signed transaction (CBOR hex) to the network.

        Returns the transaction hash on success.
        """
        url = f"{self.base_url}/tx/submit"
        tx_bytes = bytes.fromhex(tx_cbor_hex)

        for attempt in range(MAX_RETRIES):
            try:
                resp = self.session.post(
                    url,
                    data=tx_bytes,
                    headers={
                        "Content-Type": "application/cbor",
                        "project_id": self.project_id,
                    },
                    timeout=30,
                )

                if resp.status_code in (429, 500, 502, 503):
                    wait = RETRY_BACKOFF * (attempt + 1)
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    logger.error("Tx submit %d response: %s", resp.status_code, resp.text[:500])
                resp.raise_for_status()
                return resp.json()

            except requests.exceptions.Timeout:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
                raise

        if resp.status_code >= 400:
            logger.error("Tx submit %d response (final): %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Asset queries
    # ------------------------------------------------------------------

    def get_asset_addresses(self, asset: str, count: int = 10) -> list[dict]:
        return self._get(f"/assets/{asset}/addresses?count={count}")

    def get_script_info(self, script_hash: str) -> dict:
        return self._get(f"/scripts/{script_hash}")
