"""
api/main.py
═══════════

FastAPI server for pocketful settlement system.
Provides HTTP endpoints that BAND agents call via their mandates.

Endpoints:
  POST /transfer    - Planner submits transfer request
  POST /execute     - Executor executes transfer (calls ledger)
  GET /account/{id} - Get account balance (planner/executor use)
  GET /ledger       - Get full ledger state (reconciler uses)
  GET /verify       - Verify ledger consistency (reconciler uses)

Stage 1 (Sep 28): Basic endpoints
Stage 2+: Add idempotency, retries, concurrency
"""

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Dict, Optional, Any
import logging
import sys
import os
from datetime import datetime
import json

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from ledger_wrapper import (
    get_ledger_wrapper,
    init_ledger,
    shutdown_ledger,
    LedgerStatus,
    STATUS_MESSAGES,
    LedgerWrapper
)

# ═══════════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# PYDANTIC MODELS (Request/Response)
# ═══════════════════════════════════════════════════════════════════════════

class TransferRequest(BaseModel):
    """Transfer request from planner or executor agent."""
    sender_id: str = Field(..., description="Account to debit")
    receiver_id: str = Field(..., description="Account to credit")
    amount_dollars: float = Field(..., gt=0, description="Amount to transfer (e.g., 50.00)")
    txn_id: str = Field(..., description="Unique transaction ID")
    
    class Config:
        example = {
            "sender_id": "alice",
            "receiver_id": "bob",
            "amount_dollars": 50.00,
            "txn_id": "txn_001"
        }


class AccountResponse(BaseModel):
    """Account information."""
    account_id: str
    balance_dollars: float
    status: str = "ok"
    timestamp: str
    
    class Config:
        example = {
            "account_id": "alice",
            "balance_dollars": 5000.00,
            "status": "ok",
            "timestamp": "2026-09-28T15:30:34Z"
        }


class TransferResponse(BaseModel):
    """Response from transfer endpoint."""
    status: str = Field(..., description="'success' or 'failed'")
    message: str = Field(..., description="Status message")
    txn_id: str
    timestamp: str
    
    # Only included if successful
    sender_balance: Optional[float] = None
    receiver_balance: Optional[float] = None
    transferred_amount: Optional[float] = None
    
    # Only included if failed
    error: Optional[str] = None
    
    class Config:
        example = {
            "status": "success",
            "message": "Transfer completed",
            "txn_id": "txn_001",
            "timestamp": "2026-09-28T15:30:34Z",
            "sender_balance": 4950.00,
            "receiver_balance": 5050.00,
            "transferred_amount": 50.00
        }


class LedgerStateResponse(BaseModel):
    """Complete ledger state."""
    accounts: Dict[str, Dict[str, Any]]
    transactions: list
    metadata: Dict[str, Any]
    timestamp: str
    
    class Config:
        example = {
            "accounts": {
                "alice": {"balance_dollars": 4950.00},
                "bob": {"balance_dollars": 5050.00}
            },
            "transactions": [
                {
                    "txn_id": "txn_001",
                    "sender_id": "alice",
                    "receiver_id": "bob",
                    "amount_dollars": 50.00,
                    "status": "COMMITTED"
                }
            ],
            "metadata": {
                "total_balance": 10000.00,
                "transaction_count": 1
            },
            "timestamp": "2026-09-28T15:30:34Z"
        }


class VerificationResponse(BaseModel):
    """Ledger verification result."""
    status: str = Field(..., description="'verified' or 'anomaly'")
    invariant_satisfied: bool
    message: str
    timestamp: str
    
    # Verification details
    total_balance: Optional[float] = None
    transaction_count: Optional[int] = None
    anomalies: Optional[list] = None
    
    class Config:
        example = {
            "status": "verified",
            "invariant_satisfied": True,
            "message": "Ledger state is consistent",
            "timestamp": "2026-09-28T15:30:34Z",
            "total_balance": 10000.00,
            "transaction_count": 1,
            "anomalies": []
        }


class ErrorResponse(BaseModel):
    """Error response."""
    error: str
    detail: str
    timestamp: str
    
    class Config:
        example = {
            "error": "INSUFFICIENT_BALANCE",
            "detail": "Account 'alice' has balance $100, but $500 requested",
            "timestamp": "2026-09-28T15:30:34Z"
        }


# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Pocketful Settlement API",
    description="BAND-driven atomic payment settlement system",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ═══════════════════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═══════════════════════════════════════════════════════════════════════════

@app.on_event("startup")
async def startup():
    """Initialize ledger on startup."""
    logger.info("🚀 Starting Pocketful Settlement API...")
    try:
        init_ledger()
        logger.info("✓ Ledger initialized")
    except Exception as e:
        logger.error(f"✗ Startup failed: {e}")
        raise

@app.on_event("shutdown")
async def shutdown():
    """Clean up ledger on shutdown."""
    logger.info("🛑 Shutting down...")
    try:
        shutdown_ledger()
        logger.info("✓ Ledger cleaned up")
    except Exception as e:
        logger.error(f"✗ Shutdown error: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════

def get_wrapper() -> LedgerWrapper:
    """Dependency: get ledger wrapper."""
    return get_ledger_wrapper()

def get_timestamp() -> str:
    """Get ISO 8601 timestamp."""
    return datetime.utcnow().isoformat() + "Z"

# ═══════════════════════════════════════════════════════════════════════════
# HEALTH CHECK
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/health")
async def health_check():
    """Health check endpoint (for orchestration, load balancer)."""
    return {
        "status": "healthy",
        "service": "pocketful-settlement",
        "version": "1.0.0",
        "timestamp": get_timestamp()
    }

# ═══════════════════════════════════════════════════════════════════════════
# AGENT ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/account/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str,
    wrapper: LedgerWrapper = Depends(get_wrapper)
) -> AccountResponse:
    """
    Get account balance (called by planner agent).
    
    Used by:
      - Planner agent: validates sender/receiver exist and checks balances
      - Executor agent: gets updated balances after transfer
    
    Args:
        account_id: Account ID to query
    
    Returns:
        AccountResponse with balance_dollars
    
    Raises:
        HTTPException 404: Account not found
    """
    logger.info(f"📋 GET /account/{account_id}")
    
    account, status, msg = wrapper.get_account(account_id)
    
    if status != LedgerStatus.OK:
        logger.warning(f"✗ Account not found: {account_id}")
        raise HTTPException(
            status_code=404,
            detail=f"Account '{account_id}' not found: {msg}"
        )
    
    return AccountResponse(
        account_id=account_id,
        balance_dollars=account.get("balance_dollars", 0.0),
        status="ok",
        timestamp=get_timestamp()
    )


@app.post("/transfer", response_model=TransferResponse)
async def transfer_endpoint(
    request: TransferRequest,
    wrapper: LedgerWrapper = Depends(get_wrapper)
) -> TransferResponse:
    """
    Execute a transfer (called by planner or executor agent).
    
    Planner calls this to VALIDATE a transfer request.
    Executor calls this to EXECUTE the transfer.
    
    This endpoint calls the C ledger, which:
      1. Acquires mutex (thread-safe)
      2. Validates sender/receiver exist
      3. Checks sender balance ≥ amount
      4. Debits sender
      5. Credits receiver
      6. Records transaction
      7. Verifies invariant (sum unchanged)
      8. Releases mutex
    
    Atomicity guaranteed: all-or-nothing.
    Idempotency guaranteed: same txn_id = same result.
    
    Args:
        request: TransferRequest
    
    Returns:
        TransferResponse with balances (if success) or error (if failed)
    """
    sender = request.sender_id
    receiver = request.receiver_id
    amount = request.amount_dollars
    txn_id = request.txn_id
    
    logger.info(
        f"💸 POST /transfer | "
        f"{sender} → {receiver} | "
        f"${amount:.2f} | txn_id={txn_id}"
    )
    
    # Call C ledger (atomic operation)
    status, msg, result = wrapper.transfer(sender, receiver, amount, txn_id)
    
    if status == LedgerStatus.OK:
        logger.info(f"✓ Transfer successful: {txn_id}")
        return TransferResponse(
            status="success",
            message=msg,
            txn_id=txn_id,
            sender_balance=result.get("sender_balance"),
            receiver_balance=result.get("receiver_balance"),
            transferred_amount=result.get("transferred_amount"),
            timestamp=get_timestamp()
        )
    
    elif status == LedgerStatus.DUPLICATE_TXN:
        # Idempotent response: already processed, return cached result
        logger.info(f"⚠️  Idempotent retry: {txn_id} already processed")
        return TransferResponse(
            status="success",
            message=msg,
            txn_id=txn_id,
            sender_balance=result.get("sender_balance"),
            receiver_balance=result.get("receiver_balance"),
            transferred_amount=result.get("transferred_amount"),
            timestamp=get_timestamp()
        )
    
    else:
        # Failed (insufficient balance, invalid account, corrupted, etc.)
        logger.warning(f"✗ Transfer failed: {txn_id} — {msg}")
        error_detail = result.get("error", msg)
        
        raise HTTPException(
            status_code=400,
            detail={
                "status": "failed",
                "message": msg,
                "error": error_detail,
                "txn_id": txn_id,
                "timestamp": get_timestamp()
            }
        )


@app.post("/execute", response_model=TransferResponse)
async def execute_endpoint(
    request: TransferRequest,
    wrapper: LedgerWrapper = Depends(get_wrapper)
) -> TransferResponse:
    """
    Execute a transfer (alias for /transfer, called by executor agent).
    
    Executor agent calls this to execute a validated plan.
    This is the same as /transfer but semantically "execute" vs "transfer validate".
    
    In stage-2+, this will have additional retry/idempotency handling.
    """
    logger.info(f"⚡ POST /execute | txn_id={request.txn_id}")
    return await transfer_endpoint(request, wrapper)


@app.get("/ledger", response_model=LedgerStateResponse)
async def get_ledger_state(
    wrapper: LedgerWrapper = Depends(get_wrapper)
) -> LedgerStateResponse:
    """
    Get complete ledger state (called by reconciler agent).
    
    Returns all accounts and transactions.
    Used by reconciler to verify ledger consistency.
    
    Returns:
        LedgerStateResponse with full accounts and transactions
    """
    logger.info("📖 GET /ledger (reconciler audit)")
    
    state = wrapper.get_ledger_state()
    
    return LedgerStateResponse(
        accounts=state.get("accounts", {}),
        transactions=state.get("transactions", []),
        metadata=state.get("metadata", {}),
        timestamp=get_timestamp()
    )


@app.get("/verify", response_model=VerificationResponse)
async def verify_endpoint(
    wrapper: LedgerWrapper = Depends(get_wrapper)
) -> VerificationResponse:
    """
    Verify ledger consistency (called by reconciler agent).
    
    Checks:
      - sum(all balances) = constant (no money created/destroyed)
      - sum(debits) = sum(credits)
      - no negative balances
      - all transactions properly recorded
    
    Returns:
        VerificationResponse with invariant status
    """
    logger.info("✅ GET /verify (reconciler verify)")
    
    status, msg, report = wrapper.verify_state()
    
    invariant_satisfied = (status == LedgerStatus.OK)
    
    return VerificationResponse(
        status="verified" if invariant_satisfied else "anomaly",
        invariant_satisfied=invariant_satisfied,
        message=msg,
        total_balance=report.get("total_balance"),
        transaction_count=report.get("transaction_count"),
        anomalies=report.get("anomalies", []),
        timestamp=get_timestamp()
    )


# ═══════════════════════════════════════════════════════════════════════════
# DEBUG / DEVELOPMENT ENDPOINTS (remove in production)
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/dev/create-account")
async def dev_create_account(
    account_id: str,
    wrapper: LedgerWrapper = Depends(get_wrapper)
):
    """Development endpoint: create an account."""
    status, msg = wrapper.create_account(account_id)
    return {
        "account_id": account_id,
        "status": "created" if status == 0 else "error",
        "message": msg,
        "timestamp": get_timestamp()
    }


@app.post("/dev/reset")
async def dev_reset(wrapper: LedgerWrapper = Depends(get_wrapper)):
    """Development endpoint: reset ledger (reinitialize)."""
    wrapper.destroy()
    wrapper.init()
    return {
        "status": "reset",
        "timestamp": get_timestamp()
    }


# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Custom error response format."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.detail if isinstance(exc.detail, str) else "Error",
            "detail": exc.detail,
            "timestamp": get_timestamp()
        }
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Catch-all error handler."""
    logger.error(f"✗ Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "detail": str(exc),
            "timestamp": get_timestamp()
        }
    )


# ═══════════════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    
    # Run on 0.0.0.0:8000 (accessible from BAND agents, docker, host)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
