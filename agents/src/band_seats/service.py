"""HTTP tools for the settlement service.

Endpoint paths, field names and status semantics live here, in seat code.
The mandates stay generic and never mention them.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from band.runtime.custom_tools import CustomToolDef

DEFAULT_URL = "http://localhost:8000"
TIMEOUT_SECONDS = 10.0


def _client() -> httpx.AsyncClient:
    base_url = os.environ.get("SETTLEMENT_API_URL", DEFAULT_URL)
    return httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT_SECONDS)


async def _call(method: str, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
    """Make one request and return the result as data, never as an exception.

    A 4xx/5xx is a normal outcome the seat has to classify, so it comes back as
    http_status + body. A transport failure is reported with request_sent, so
    the executor can tell "never left" from "sent, reply lost".
    """
    async with _client() as client:
        try:
            resp = await client.request(method, path, json=json)
        except httpx.ConnectError as e:
            return {"transport_error": "connect_failed", "request_sent": False, "detail": str(e)}
        except httpx.TimeoutException as e:
            # A timeout after connecting may mean the service acted and the reply was lost.
            sent = not isinstance(e, (httpx.ConnectTimeout, httpx.PoolTimeout))
            return {"transport_error": "timeout", "request_sent": sent, "detail": str(e)}
        except httpx.HTTPError as e:
            return {"transport_error": type(e).__name__, "request_sent": None, "detail": str(e)}
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    return {"http_status": resp.status_code, "body": body}


# ── Input models. Tool name = class name minus "Input", lowercased. ─────────


class ServiceHealthInput(BaseModel):
    """Check the settlement service is up and its ledger is readable. Returns
    status "healthy" or "degraded" plus ledger stats."""


class LookupAccountInput(BaseModel):
    """Read one account's current balance (GET /account/{account_id}).
    HTTP 404 means the account does not exist. Read-only."""

    account_id: str = Field(..., min_length=1, max_length=255)


class LookupTransactionInput(BaseModel):
    """Read the recorded transaction for a txn_id (GET /transaction/{txn_id}).
    A record exists if and only if that transfer took effect; HTTP 404 means
    no transfer under this id has been applied. Read-only."""

    txn_id: str = Field(..., min_length=1, max_length=63)


class ExecuteTransferInput(BaseModel):
    """Submit an approved transfer (POST /execute). Atomic: both legs apply or
    neither does. Outcomes:
      200 + body.status "success"   -> applied; body carries resulting balances.
      200 + body.status "duplicate" -> txn_id already recorded; nothing new applied.
      503 + body.retryable true     -> concurrent conflict, nothing committed; retry with the SAME txn_id.
      400 invalid amount, 404 unknown account, 409 insufficient balance -> permanent; do not retry.
      transport_error with request_sent true -> outcome unknown; look up the txn_id before retrying.
    Always pass the txn_id from the approval unchanged."""

    sender_id: str = Field(..., min_length=1, max_length=255, description="Account to debit")
    receiver_id: str = Field(..., min_length=1, max_length=255, description="Account to credit")
    amount_dollars: float = Field(..., gt=0, description="Amount in dollars, e.g. 50.00")
    txn_id: str = Field(..., min_length=1, max_length=63, description="Unique transaction id from the approval")


class VerifyLedgerInput(BaseModel):
    """Run the service's consistency check (GET /verify). Always HTTP 200, even
    when unbalanced: read body.status ("verified" | "anomaly"), is_balanced,
    has_anomalies, num_accounts, num_transactions, total_debits_dollars,
    total_credits_dollars and anomaly_details. Note: is_balanced is true by
    construction, so check the totals yourself. Read-only."""


class LedgerOverviewInput(BaseModel):
    """Ledger overview (GET /ledger): stats, the verification result, and every
    account the service knows with its balance. It does not list transactions;
    look those up one at a time by txn_id. Read-only."""


# ── Handlers ────────────────────────────────────────────────────────────────


async def service_health(_: ServiceHealthInput) -> dict[str, Any]:
    return await _call("GET", "/health")


async def lookup_account(args: LookupAccountInput) -> dict[str, Any]:
    return await _call("GET", f"/account/{quote(args.account_id, safe='')}")


async def lookup_transaction(args: LookupTransactionInput) -> dict[str, Any]:
    return await _call("GET", f"/transaction/{quote(args.txn_id, safe='')}")


async def execute_transfer(args: ExecuteTransferInput) -> dict[str, Any]:
    return await _call("POST", "/execute", json=args.model_dump())


async def verify_ledger(_: VerifyLedgerInput) -> dict[str, Any]:
    return await _call("GET", "/verify")


async def ledger_overview(_: LedgerOverviewInput) -> dict[str, Any]:
    return await _call("GET", "/ledger")


HEALTH: CustomToolDef = (ServiceHealthInput, service_health)
ACCOUNT: CustomToolDef = (LookupAccountInput, lookup_account)
TRANSACTION: CustomToolDef = (LookupTransactionInput, lookup_transaction)
EXECUTE: CustomToolDef = (ExecuteTransferInput, execute_transfer)
VERIFY: CustomToolDef = (VerifyLedgerInput, verify_ledger)
LEDGER: CustomToolDef = (LedgerOverviewInput, ledger_overview)
