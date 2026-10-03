"""
main.py
═══════

FastAPI service over the C ledger. This is the surface BAND agents call.

Endpoint design follows what the C API can actually answer:
  - ledger.h has no "dump whole ledger as JSON" call, so GET /ledger returns
    aggregate stats plus the verification result rather than a full dump. Agents
    fetch individual accounts and transactions by id.
  - ledger_dump_accounts/ledger_dump_transactions print to stdout only; they are
    exposed through /debug/dump for container logs, not as response bodies.

HTTP status mapping is deliberate: a rejected transfer is a 4xx with a machine-
readable code so the executor agent can distinguish "retry" (concurrent conflict)
from "never going to work" (insufficient balance).
"""

import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ledger_wrapper import (  # noqa: E402
    STATUS_NAMES,
    LedgerStatus,
    LedgerWrapper,
    dollars_to_cents,
    get_ledger_wrapper,
    init_ledger,
    shutdown_ledger,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("pocketful.api")

# Seed accounts so a fresh container is immediately testable. Judges clone, run,
# and curl without a setup step. Override with SEED_ACCOUNTS=0.
SEED_ACCOUNTS = os.environ.get("SEED_ACCOUNTS", "1") == "1"
SEED_BALANCE = Decimal(os.environ.get("SEED_BALANCE", "5000"))
SEED_IDS = ["alice", "bob", "carol"]


# ═══════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════

class TransferRequest(BaseModel):
    sender_id: str = Field(..., min_length=1, max_length=255, description="Account to debit")
    receiver_id: str = Field(..., min_length=1, max_length=255, description="Account to credit")
    # Decimal, not float: amounts must be exact to the cent. The wrapper rejects anything
    # else (more than two decimal places, out of int64 range) as LEDGER_INVALID_AMOUNT.
    # Positivity is enforced by the ledger (LEDGER_INVALID_AMOUNT -> 400), not here, so every
    # bad amount reaches an agent in the same shape: HTTP 400 with a machine-readable code.
    amount_dollars: Decimal = Field(..., description="Amount in dollars, at most two decimal places, e.g. 50.00")
    txn_id: str = Field(..., min_length=1, max_length=63, description="Unique transaction id")


class CreateAccountRequest(BaseModel):
    account_id: str = Field(..., min_length=1, max_length=255)
    initial_balance_dollars: Decimal = Field(Decimal("0"))  # sign and range checked by the ledger


class AccountResponse(BaseModel):
    account_id: str
    balance_dollars: float
    balance_cents: int
    updated_at: int
    timestamp: str


class TransferResponse(BaseModel):
    status: str
    code: str
    message: str
    txn_id: str
    timestamp: str
    sender_balance: Optional[float] = None
    receiver_balance: Optional[float] = None
    transferred_amount: Optional[float] = None


class TransactionResponse(BaseModel):
    txn_id: str
    sender_id: str
    receiver_id: str
    amount_dollars: float
    amount_cents: int
    timestamp: int
    status: str
    error_reason: str


class VerificationResponse(BaseModel):
    status: str
    is_balanced: bool
    has_anomalies: bool
    num_accounts: int
    num_transactions: int
    total_debits_dollars: float
    total_credits_dollars: float
    anomaly_details: str
    timestamp: str


class LedgerResponse(BaseModel):
    stats: Dict[str, Any]
    verification: Dict[str, Any]
    accounts: List[Dict[str, Any]]
    timestamp: str


# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Pocketful Settlement API",
    description="Atomic payment settlement over a C ledger core, driven by BAND agents.",
    version="1.0.0",
)

# Accounts created through this service, tracked so GET /ledger can enumerate
# them — the C API offers lookup by id but no listing.
_known_accounts: List[str] = []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_wrapper() -> LedgerWrapper:
    return get_ledger_wrapper()


def _http_status_for(ledger_status: int) -> int:
    """Map ledger status onto HTTP so agents can branch on the response code."""
    return {
        LedgerStatus.INVALID_ACCOUNT: 404,
        LedgerStatus.INSUFFICIENT_BALANCE: 409,
        LedgerStatus.DUPLICATE_TXN: 409,
        LedgerStatus.INVALID_AMOUNT: 400,
        LedgerStatus.CONCURRENT_CONFLICT: 503,  # retryable
        LedgerStatus.DB_ERROR: 500,
        LedgerStatus.UNKNOWN_ERROR: 500,
    }.get(ledger_status, 500)


@app.on_event("startup")
async def startup() -> None:
    logger.info("Starting Pocketful Settlement API")
    init_ledger()

    if SEED_ACCOUNTS:
        wrapper = get_ledger_wrapper()
        for account_id in SEED_IDS:
            status, message = wrapper.create_account(account_id, SEED_BALANCE)
            if status == LedgerStatus.OK:
                _known_accounts.append(account_id)
            else:
                logger.warning("Seed account %s not created: %s", account_id, message)
        logger.info("Seeded %d accounts at $%.2f each", len(_known_accounts), SEED_BALANCE)


@app.on_event("shutdown")
async def shutdown() -> None:
    logger.info("Shutting down")
    shutdown_ledger()


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health(wrapper: LedgerWrapper = Depends(get_wrapper)) -> Dict[str, Any]:
    """Liveness plus a real ledger read, so a loaded-but-broken .so fails the check."""
    status, message, stats = wrapper.get_stats()
    healthy = status == LedgerStatus.OK
    return {
        "status": "healthy" if healthy else "degraded",
        "service": "pocketful-settlement",
        "version": "1.0.0",
        "ledger": stats if healthy else {"error": message},
        "timestamp": _now(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# ACCOUNTS
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/account", response_model=AccountResponse, status_code=201)
async def create_account(
    req: CreateAccountRequest,
    wrapper: LedgerWrapper = Depends(get_wrapper),
) -> AccountResponse:
    """Create an account with a starting balance."""
    status, message = wrapper.create_account(req.account_id, req.initial_balance_dollars)
    if status != LedgerStatus.OK:
        raise HTTPException(
            status_code=_http_status_for(status),
            detail={"code": STATUS_NAMES.get(status), "message": message},
        )

    if req.account_id not in _known_accounts:
        _known_accounts.append(req.account_id)

    _, _, account = wrapper.get_account(req.account_id)
    return AccountResponse(timestamp=_now(), **account)


@app.get("/account/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str,
    wrapper: LedgerWrapper = Depends(get_wrapper),
) -> AccountResponse:
    """Account snapshot. The planner agent calls this to validate a transfer."""
    status, message, account = wrapper.get_account(account_id)
    if status != LedgerStatus.OK:
        raise HTTPException(
            status_code=_http_status_for(status),
            detail={"code": STATUS_NAMES.get(status), "message": message},
        )
    return AccountResponse(timestamp=_now(), **account)


# ═══════════════════════════════════════════════════════════════════════════
# TRANSFERS
# ═══════════════════════════════════════════════════════════════════════════

def _do_transfer(req: TransferRequest, wrapper: LedgerWrapper) -> TransferResponse:
    status, message, result = wrapper.transfer(
        req.sender_id, req.receiver_id, req.amount_dollars, req.txn_id
    )

    if status == LedgerStatus.OK:
        return TransferResponse(
            status="success",
            code=STATUS_NAMES[LedgerStatus.OK],
            message=message,
            txn_id=req.txn_id,
            sender_balance=result.get("sender_balance"),
            receiver_balance=result.get("receiver_balance"),
            transferred_amount=result.get("transferred_amount"),
            timestamp=_now(),
        )

    if status == LedgerStatus.DUPLICATE_TXN:
        # Idempotent replay: the ledger already holds this txn_id, so the retry is
        # not an error. Return the recorded transaction and current balances.
        _, _, txn = wrapper.get_transaction(req.txn_id)

        # ...but only if it is the SAME request. Reusing an id for a different
        # sender, receiver or amount is a caller bug; answering "duplicate" would
        # tell it that its request took effect when it did not. Records are
        # immutable once written, so reading it after the ledger's answer is safe.
        if txn is not None and (
            txn["sender_id"] != req.sender_id
            or txn["receiver_id"] != req.receiver_id
            or txn["amount_cents"] != dollars_to_cents(req.amount_dollars)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "status": "failed",
                    "code": "TXN_ID_CONFLICT",
                    "message": "txn_id is already recorded for a different transfer; no funds moved",
                    "txn_id": req.txn_id,
                    "retryable": False,
                    "recorded": {
                        "sender_id": txn["sender_id"],
                        "receiver_id": txn["receiver_id"],
                        "amount_dollars": txn["amount_dollars"],
                    },
                },
            )

        sender_balance = receiver_balance = None
        if txn:
            _, _, sender_balance = wrapper.get_balance(txn["sender_id"])
            _, _, receiver_balance = wrapper.get_balance(txn["receiver_id"])
        return TransferResponse(
            status="duplicate",
            code=STATUS_NAMES[LedgerStatus.DUPLICATE_TXN],
            message="Transaction already recorded; no second transfer performed",
            txn_id=req.txn_id,
            sender_balance=sender_balance,
            receiver_balance=receiver_balance,
            transferred_amount=txn["amount_dollars"] if txn else None,
            timestamp=_now(),
        )

    raise HTTPException(
        status_code=_http_status_for(status),
        detail={
            "status": "failed",
            "code": STATUS_NAMES.get(status),
            "message": message,
            "txn_id": req.txn_id,
            "retryable": status == LedgerStatus.CONCURRENT_CONFLICT,
        },
    )


@app.post("/transfer", response_model=TransferResponse)
async def transfer(
    req: TransferRequest,
    wrapper: LedgerWrapper = Depends(get_wrapper),
) -> TransferResponse:
    """Submit a transfer. Atomic: both legs land, or neither does."""
    logger.info("POST /transfer %s -> %s $%.2f [%s]",
                req.sender_id, req.receiver_id, req.amount_dollars, req.txn_id)
    return _do_transfer(req, wrapper)


@app.post("/execute", response_model=TransferResponse)
async def execute(
    req: TransferRequest,
    wrapper: LedgerWrapper = Depends(get_wrapper),
) -> TransferResponse:
    """Executor-agent entry point. Same semantics as /transfer; separate route so
    the two agent roles show up distinctly in logs and the BAND room trace."""
    logger.info("POST /execute [%s]", req.txn_id)
    return _do_transfer(req, wrapper)


@app.get("/transaction/{txn_id}", response_model=TransactionResponse)
async def get_transaction(
    txn_id: str,
    wrapper: LedgerWrapper = Depends(get_wrapper),
) -> TransactionResponse:
    """Fetch a recorded transaction by id."""
    status, message, txn = wrapper.get_transaction(txn_id)
    if status != LedgerStatus.OK:
        raise HTTPException(
            status_code=404,
            detail={"code": STATUS_NAMES.get(status), "message": f"Transaction not found: {message}"},
        )
    return TransactionResponse(**txn)


# ═══════════════════════════════════════════════════════════════════════════
# VERIFICATION — reconciler agent
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/verify", response_model=VerificationResponse)
async def verify(wrapper: LedgerWrapper = Depends(get_wrapper)) -> VerificationResponse:
    """
    Consistency check. Returns 200 even when the ledger is unbalanced — the
    reconciler agent needs to read the anomaly, not catch an exception.
    """
    status, message, report = wrapper.verify_state()
    if status != LedgerStatus.OK:
        raise HTTPException(
            status_code=500,
            detail={"code": STATUS_NAMES.get(status), "message": message},
        )
    return VerificationResponse(
        status="verified" if report["is_balanced"] and not report["has_anomalies"] else "anomaly",
        timestamp=_now(),
        **report,
    )


@app.get("/ledger", response_model=LedgerResponse)
async def ledger(wrapper: LedgerWrapper = Depends(get_wrapper)) -> LedgerResponse:
    """
    Ledger overview: stats, verification result, and every account this service
    created. Not a full transaction dump — the C API exposes transactions only by
    id, so agents fetch those individually via /transaction/{txn_id}.
    """
    stats_status, stats_msg, stats = wrapper.get_stats()
    if stats_status != LedgerStatus.OK:
        raise HTTPException(status_code=500, detail={"message": stats_msg})

    _, _, verification = wrapper.verify_state()

    accounts = []
    for account_id in _known_accounts:
        status, _, account = wrapper.get_account(account_id)
        if status == LedgerStatus.OK:
            accounts.append(account)

    return LedgerResponse(
        stats=stats,
        verification=verification,
        accounts=accounts,
        timestamp=_now(),
    )


@app.post("/debug/dump")
async def debug_dump(wrapper: LedgerWrapper = Depends(get_wrapper)) -> Dict[str, str]:
    """Print full ledger contents to stdout — read it with `docker logs`."""
    wrapper.dump_to_stdout()
    return {"status": "dumped to stdout", "timestamp": _now()}


# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLING
# ═══════════════════════════════════════════════════════════════════════════

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={**detail, "timestamp": _now()})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"code": "INTERNAL_ERROR", "message": str(exc), "timestamp": _now()},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
