"""
ledger_wrapper.py
═════════════════

ctypes bridge to the C ledger (ledger.so).

Written against ledger.h directly. Every signature here mirrors the header:
the C API uses out-parameters for all data and returns ledger_status_t as the
result, so each wrapper method returns a (status, message, value) triple rather
than raising — the FastAPI layer decides what becomes an HTTP error.

Money is int64 cents end to end. Dollars only appear at the HTTP boundary.

IDEMPOTENCY NOTE
----------------
Replays are detected inside ledger_transfer(), under the ledger lock, which
returns LEDGER_DUPLICATE_TXN and moves no money. There is deliberately no
Python-side pre-check: it would run outside the lock and could race.

AMOUNT NOTE
-----------
Dollars become cents exactly once, in dollars_to_cents(). It never rounds: an
amount with more than two decimal places, or one that does not fit in int64
cents, is rejected as LEDGER_INVALID_AMOUNT. Silently rounding or wrapping an
amount would move money the caller did not ask for.
"""

import ctypes
import logging
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Dict, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS — mirror ledger.h #defines
# ═══════════════════════════════════════════════════════════════════════════

MAX_ACCOUNT_ID_LEN = 256
MAX_TXN_ID_LEN = 64
MAX_ERROR_MSG_LEN = 512


class LedgerStatus:
    """ledger_status_t enum — values taken verbatim from ledger.h."""
    OK = 0
    INVALID_ACCOUNT = 1
    INSUFFICIENT_BALANCE = 2
    DUPLICATE_TXN = 3
    INVALID_AMOUNT = 4
    CONCURRENT_CONFLICT = 5
    DB_ERROR = 6
    UNKNOWN_ERROR = 7


STATUS_MESSAGES = {
    LedgerStatus.OK: "Success",
    LedgerStatus.INVALID_ACCOUNT: "Account does not exist",
    LedgerStatus.INSUFFICIENT_BALANCE: "Insufficient balance",
    LedgerStatus.DUPLICATE_TXN: "Transaction already recorded",
    LedgerStatus.INVALID_AMOUNT: "Invalid amount",
    LedgerStatus.CONCURRENT_CONFLICT: "Concurrent conflict — retry suggested",
    LedgerStatus.DB_ERROR: "Ledger storage error",
    LedgerStatus.UNKNOWN_ERROR: "Unexpected error",
}

STATUS_NAMES = {
    LedgerStatus.OK: "LEDGER_OK",
    LedgerStatus.INVALID_ACCOUNT: "LEDGER_INVALID_ACCOUNT",
    LedgerStatus.INSUFFICIENT_BALANCE: "LEDGER_INSUFFICIENT_BALANCE",
    LedgerStatus.DUPLICATE_TXN: "LEDGER_DUPLICATE_TXN",
    LedgerStatus.INVALID_AMOUNT: "LEDGER_INVALID_AMOUNT",
    LedgerStatus.CONCURRENT_CONFLICT: "LEDGER_CONCURRENT_CONFLICT",
    LedgerStatus.DB_ERROR: "LEDGER_DB_ERROR",
    LedgerStatus.UNKNOWN_ERROR: "LEDGER_UNKNOWN_ERROR",
}

# Transaction status field (int8_t in ledger_transaction_t)
TXN_STATUS_NAMES = {0: "planned", 1: "executed", 2: "rejected"}


# ═══════════════════════════════════════════════════════════════════════════
# STRUCTURES — field order and sizes mirror ledger.h exactly
# ═══════════════════════════════════════════════════════════════════════════

class LedgerAccount(ctypes.Structure):
    """ledger_account_t"""
    _fields_ = [
        ("account_id", ctypes.c_char * MAX_ACCOUNT_ID_LEN),
        ("balance", ctypes.c_int64),
        ("updated_at", ctypes.c_long),  # time_t, 64-bit on x86-64 Linux
    ]


class LedgerTransaction(ctypes.Structure):
    """ledger_transaction_t"""
    _fields_ = [
        ("txn_id", ctypes.c_char * MAX_TXN_ID_LEN),
        ("sender_id", ctypes.c_char * MAX_ACCOUNT_ID_LEN),
        ("receiver_id", ctypes.c_char * MAX_ACCOUNT_ID_LEN),
        ("amount", ctypes.c_int64),
        ("timestamp", ctypes.c_long),
        ("status", ctypes.c_int8),
        ("error_reason", ctypes.c_char * 256),
    ]


class LedgerState(ctypes.Structure):
    """ledger_state_t"""
    _fields_ = [
        ("num_accounts", ctypes.c_uint32),
        ("num_transactions", ctypes.c_uint32),
        ("total_debits", ctypes.c_int64),
        ("total_credits", ctypes.c_int64),
        ("is_balanced", ctypes.c_int8),
        ("has_anomalies", ctypes.c_int8),
        ("anomaly_details", ctypes.c_char * 512),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _err_buf() -> ctypes.Array:
    """Allocate an error message buffer of the size the C side expects."""
    return ctypes.create_string_buffer(MAX_ERROR_MSG_LEN)


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace") if raw else ""


def cents_to_dollars(cents: int) -> float:
    return cents / 100.0


INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)


class AmountError(ValueError):
    """An amount that cannot be represented exactly as whole int64 cents."""


def dollars_to_cents(dollars: Union[Decimal, int, str, float]) -> int:
    """Convert dollars to integer cents EXACTLY, or raise AmountError.

    Never rounds and never wraps:
      * more than two decimal places (0.285, 1.005) is rejected, because binary
        floats cannot hold such values faithfully and rounding would move a
        different amount than the one requested;
      * a result outside int64 is rejected, because ctypes would otherwise
        truncate it to 64 bits and the C ledger would act on a different number;
      * NaN and infinity are rejected.
    Floats are read through their shortest repr, i.e. the digits the caller
    wrote. Prefer passing Decimal or str.
    """
    if isinstance(dollars, bool):
        raise AmountError("Amount must be a number")
    try:
        d = Decimal(repr(dollars)) if isinstance(dollars, float) else Decimal(dollars)
    except (InvalidOperation, TypeError, ValueError):
        raise AmountError("Amount is not a valid number") from None
    if not d.is_finite():
        raise AmountError("Amount must be finite")
    if d.adjusted() > 17:  # cheap bound before arithmetic; int64 cents tops out near 9.2e16 dollars
        raise AmountError("Amount is out of range")
    with localcontext() as ctx:
        ctx.prec = 80
        cents_exact = d * 100
        if cents_exact != cents_exact.to_integral_value():
            raise AmountError("Amount must be a whole number of cents (at most two decimal places)")
        cents = int(cents_exact)
    if not INT64_MIN <= cents <= INT64_MAX:
        raise AmountError("Amount is out of range")
    return cents


# ═══════════════════════════════════════════════════════════════════════════
# WRAPPER
# ═══════════════════════════════════════════════════════════════════════════

class LedgerWrapper:
    """Thin, faithful binding over ledger.so."""

    def __init__(self, ledger_so_path: Optional[str] = None):
        self.ledger_so_path = ledger_so_path or self._find_ledger_so()
        self.ledger_lib: Optional[ctypes.CDLL] = None
        self._initialized = False
        self.load_ledger()

    # ─────────────────────────────────────────────────────────────────────
    # Loading
    # ─────────────────────────────────────────────────────────────────────

    def _find_ledger_so(self) -> str:
        candidates = [
            Path("/app/ledger/ledger.so"),
            Path("./ledger/ledger.so"),
            Path("../ledger/ledger.so"),
            Path(__file__).resolve().parent.parent / "ledger" / "ledger.so",
        ]
        for path in candidates:
            if path.exists():
                logger.info("Found ledger.so at %s", path)
                return str(path)
        logger.warning("ledger.so not found in known paths; falling back to loader search")
        return "ledger.so"

    def load_ledger(self) -> None:
        try:
            self.ledger_lib = ctypes.CDLL(self.ledger_so_path)
            logger.info("Loaded ledger from %s", self.ledger_so_path)
        except OSError as exc:
            logger.error("Failed to load ledger.so: %s", exc)
            raise RuntimeError(f"Cannot load ledger.so from {self.ledger_so_path}: {exc}") from exc
        self._setup_signatures()

    def _setup_signatures(self) -> None:
        """Declare every exported function. Names verified against `nm -D ledger.so`."""
        lib = self.ledger_lib

        # ledger_status_t ledger_init(void)
        lib.ledger_init.argtypes = []
        lib.ledger_init.restype = ctypes.c_int

        # void ledger_destroy(void)
        lib.ledger_destroy.argtypes = []
        lib.ledger_destroy.restype = None

        # ledger_status_t ledger_create_account(const char*, money_t, char*)
        lib.ledger_create_account.argtypes = [ctypes.c_char_p, ctypes.c_int64, ctypes.c_char_p]
        lib.ledger_create_account.restype = ctypes.c_int

        # ledger_status_t ledger_transfer(const char*, const char*, money_t, const char*, char*)
        lib.ledger_transfer.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int64,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.ledger_transfer.restype = ctypes.c_int

        # ledger_status_t ledger_get_balance(const char*, money_t*, char*)
        lib.ledger_get_balance.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_char_p,
        ]
        lib.ledger_get_balance.restype = ctypes.c_int

        # ledger_status_t ledger_verify_state(ledger_state_t*, char*)
        lib.ledger_verify_state.argtypes = [ctypes.POINTER(LedgerState), ctypes.c_char_p]
        lib.ledger_verify_state.restype = ctypes.c_int

        # ledger_status_t ledger_get_account(const char*, ledger_account_t*, char*)
        lib.ledger_get_account.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(LedgerAccount),
            ctypes.c_char_p,
        ]
        lib.ledger_get_account.restype = ctypes.c_int

        # ledger_status_t ledger_get_transaction(const char*, ledger_transaction_t*, char*)
        lib.ledger_get_transaction.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(LedgerTransaction),
            ctypes.c_char_p,
        ]
        lib.ledger_get_transaction.restype = ctypes.c_int

        # void ledger_dump_accounts(void) / ledger_dump_transactions(void)
        # These print to stdout — useful in container logs, not as API responses.
        lib.ledger_dump_accounts.argtypes = []
        lib.ledger_dump_accounts.restype = None
        lib.ledger_dump_transactions.argtypes = []
        lib.ledger_dump_transactions.restype = None

        # ledger_status_t ledger_get_stats(uint32_t*, uint32_t*, money_t*, char*)
        lib.ledger_get_stats.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_char_p,
        ]
        lib.ledger_get_stats.restype = ctypes.c_int

        logger.info("ctypes signatures bound (9 functions)")

    # ─────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────

    def init(self) -> Tuple[int, str]:
        status = self.ledger_lib.ledger_init()
        message = STATUS_MESSAGES.get(status, "Unknown error")
        if status == LedgerStatus.OK:
            self._initialized = True
            logger.info("Ledger initialized")
        else:
            logger.error("Ledger init failed: %s", message)
        return status, message

    def destroy(self) -> None:
        if self._initialized:
            self.ledger_lib.ledger_destroy()
            self._initialized = False
            logger.info("Ledger destroyed")

    # ─────────────────────────────────────────────────────────────────────
    # Accounts
    # ─────────────────────────────────────────────────────────────────────

    def create_account(self, account_id: str, initial_balance_dollars: float = 0.0) -> Tuple[int, str]:
        """Create an account with a starting balance (dollars in, cents stored)."""
        try:
            initial_cents = dollars_to_cents(initial_balance_dollars)
        except AmountError as exc:
            logger.warning("Create account %s rejected: %s", account_id, exc)
            return LedgerStatus.INVALID_AMOUNT, str(exc)

        err = _err_buf()
        status = self.ledger_lib.ledger_create_account(
            account_id.encode("utf-8"),
            initial_cents,
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")
        if status == LedgerStatus.OK:
            logger.info("Created account %s with $%.2f", account_id, initial_balance_dollars)
        else:
            logger.warning("Create account %s failed: %s", account_id, message)
        return status, message

    def get_balance(self, account_id: str) -> Tuple[int, str, Optional[float]]:
        """Returns (status, message, balance_dollars). balance is None on failure."""
        balance = ctypes.c_int64(0)
        err = _err_buf()
        status = self.ledger_lib.ledger_get_balance(
            account_id.encode("utf-8"),
            ctypes.byref(balance),
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")
        if status != LedgerStatus.OK:
            return status, message, None
        return status, message, cents_to_dollars(balance.value)

    def get_account(self, account_id: str) -> Tuple[int, str, Optional[Dict]]:
        """Returns (status, message, account_dict)."""
        account = LedgerAccount()
        err = _err_buf()
        status = self.ledger_lib.ledger_get_account(
            account_id.encode("utf-8"),
            ctypes.byref(account),
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")
        if status != LedgerStatus.OK:
            return status, message, None
        return status, message, {
            "account_id": _decode(account.account_id),
            "balance_dollars": cents_to_dollars(account.balance),
            "balance_cents": account.balance,
            "updated_at": int(account.updated_at),
        }

    # ─────────────────────────────────────────────────────────────────────
    # Transfers
    # ─────────────────────────────────────────────────────────────────────

    def transfer(
        self,
        sender_id: str,
        receiver_id: str,
        amount_dollars: float,
        txn_id: str,
    ) -> Tuple[int, str, Dict]:
        """
        Atomic transfer. On LEDGER_OK both legs landed; on any error neither did.

        Returns (status, message, result). On success `result` carries the post-transfer
        balances, read back from the ledger rather than computed here — if the C side
        did something unexpected, the response shows what the ledger actually holds.
        """
        try:
            amount_cents = dollars_to_cents(amount_dollars)
        except AmountError as exc:
            logger.warning("Transfer %s rejected: %s", txn_id, exc)
            return (LedgerStatus.INVALID_AMOUNT, str(exc),
                    {"error": str(exc), "code": STATUS_NAMES.get(LedgerStatus.INVALID_AMOUNT)})

        err = _err_buf()
        status = self.ledger_lib.ledger_transfer(
            sender_id.encode("utf-8"),
            receiver_id.encode("utf-8"),
            amount_cents,
            txn_id.encode("utf-8"),
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")

        if status == LedgerStatus.DUPLICATE_TXN:
            # Replay detected under the ledger lock; no money moved.
            logger.info("Replay of %s — no transfer performed", txn_id)
            return status, message, {"error": message, "code": STATUS_NAMES.get(status)}

        if status != LedgerStatus.OK:
            logger.warning("Transfer %s failed (%s): %s", txn_id, STATUS_NAMES.get(status), message)
            return status, message, {"error": message, "code": STATUS_NAMES.get(status)}

        _, _, sender_balance = self.get_balance(sender_id)
        _, _, receiver_balance = self.get_balance(receiver_id)

        logger.info(
            "Transfer %s: %s -> %s $%.2f", txn_id, sender_id, receiver_id, amount_dollars
        )
        return status, message, {
            "txn_id": txn_id,
            "sender_balance": sender_balance,
            "receiver_balance": receiver_balance,
            # Report the amount the ledger was given, not the caller's raw input.
            "transferred_amount": cents_to_dollars(amount_cents),
        }

    def get_transaction(self, txn_id: str) -> Tuple[int, str, Optional[Dict]]:
        """Returns (status, message, transaction_dict)."""
        txn = LedgerTransaction()
        err = _err_buf()
        status = self.ledger_lib.ledger_get_transaction(
            txn_id.encode("utf-8"),
            ctypes.byref(txn),
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")
        if status != LedgerStatus.OK:
            return status, message, None
        return status, message, {
            "txn_id": _decode(txn.txn_id),
            "sender_id": _decode(txn.sender_id),
            "receiver_id": _decode(txn.receiver_id),
            "amount_dollars": cents_to_dollars(txn.amount),
            "amount_cents": txn.amount,
            "timestamp": int(txn.timestamp),
            "status": TXN_STATUS_NAMES.get(txn.status, "unknown"),
            "error_reason": _decode(txn.error_reason),
        }

    # ─────────────────────────────────────────────────────────────────────
    # Verification & stats
    # ─────────────────────────────────────────────────────────────────────

    def verify_state(self) -> Tuple[int, str, Dict]:
        """
        Run the ledger's own consistency check.

        Note the two distinct failure modes: `status != OK` means verification itself
        could not run, while `is_balanced == 0` means it ran and found the books wrong.
        The reconciler agent cares about the second one.
        """
        state = LedgerState()
        err = _err_buf()
        status = self.ledger_lib.ledger_verify_state(ctypes.byref(state), err)
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")

        report = {
            "num_accounts": state.num_accounts,
            "num_transactions": state.num_transactions,
            "total_debits_dollars": cents_to_dollars(state.total_debits),
            "total_credits_dollars": cents_to_dollars(state.total_credits),
            "is_balanced": bool(state.is_balanced),
            "has_anomalies": bool(state.has_anomalies),
            "anomaly_details": _decode(state.anomaly_details),
        }

        if status == LedgerStatus.OK and state.is_balanced and not state.has_anomalies:
            logger.info("Ledger verification passed (%d txns)", state.num_transactions)
        else:
            logger.warning("Ledger verification concern: %s", report.get("anomaly_details") or message)

        return status, message, report

    def get_stats(self) -> Tuple[int, str, Dict]:
        """Account count, transaction count, and total value held."""
        num_accounts = ctypes.c_uint32(0)
        num_txns = ctypes.c_uint32(0)
        total_value = ctypes.c_int64(0)
        err = _err_buf()

        status = self.ledger_lib.ledger_get_stats(
            ctypes.byref(num_accounts),
            ctypes.byref(num_txns),
            ctypes.byref(total_value),
            err,
        )
        detail = _decode(err.value)
        message = detail or STATUS_MESSAGES.get(status, "Unknown error")

        if status != LedgerStatus.OK:
            return status, message, {}

        return status, message, {
            "num_accounts": num_accounts.value,
            "num_transactions": num_txns.value,
            "total_value_dollars": cents_to_dollars(total_value.value),
            "total_value_cents": total_value.value,
        }

    def dump_to_stdout(self) -> None:
        """Print accounts and transactions to stdout — visible in `docker logs`."""
        self.ledger_lib.ledger_dump_accounts()
        self.ledger_lib.ledger_dump_transactions()


# ═══════════════════════════════════════════════════════════════════════════
# MODULE-LEVEL SINGLETON
# ═══════════════════════════════════════════════════════════════════════════

_wrapper_instance: Optional[LedgerWrapper] = None


def get_ledger_wrapper() -> LedgerWrapper:
    global _wrapper_instance
    if _wrapper_instance is None:
        _wrapper_instance = LedgerWrapper()
    return _wrapper_instance


def init_ledger() -> None:
    wrapper = get_ledger_wrapper()
    status, message = wrapper.init()
    if status != LedgerStatus.OK:
        raise RuntimeError(f"ledger_init failed: {message}")


def shutdown_ledger() -> None:
    global _wrapper_instance
    if _wrapper_instance is not None:
        _wrapper_instance.destroy()
        _wrapper_instance = None


# ═══════════════════════════════════════════════════════════════════════════
# SMOKE TEST — python ledger_wrapper.py
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    w = LedgerWrapper()
    w.init()

    w.create_account("alice", 5000.00)
    w.create_account("bob", 5000.00)

    print("\n--- transfer $50 alice -> bob ---")
    st, msg, res = w.transfer("alice", "bob", 50.00, "txn_001")
    print(STATUS_NAMES.get(st), msg, res)

    print("\n--- same txn_id again (expect LEDGER_DUPLICATE_TXN) ---")
    st, msg, res = w.transfer("alice", "bob", 50.00, "txn_001")
    print(STATUS_NAMES.get(st), msg, res)

    print("\n--- overdraft attempt ---")
    st, msg, res = w.transfer("alice", "bob", 999999.00, "txn_002")
    print(STATUS_NAMES.get(st), msg, res)

    print("\n--- verify ---")
    st, msg, report = w.verify_state()
    print(STATUS_NAMES.get(st), report)

    print("\n--- stats ---")
    st, msg, stats = w.get_stats()
    print(STATUS_NAMES.get(st), stats)

    w.destroy()
