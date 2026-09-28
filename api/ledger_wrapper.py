"""
api/ledger_wrapper.py
═════════════════════

Bridge between Python and C ledger (ledger.so).
Uses ctypes to call C functions, handles type conversions.

Stage 1: Basic transfer, balance check, ledger dump
Stage 2+: Add idempotency, retries, concurrency handling
"""

import ctypes
import os
from pathlib import Path
from typing import Dict, Tuple, Optional
import logging

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS & STATUS CODES (must match ledger.h)
# ═══════════════════════════════════════════════════════════════════════════

class LedgerStatus:
    """Status codes from ledger.h"""
    OK = 0
    INSUFFICIENT_BALANCE = 1
    DUPLICATE_TXN = 2
    INVALID_ACCOUNT = 3
    ACCOUNT_EXISTS = 4
    TRANSFER_FAILED = 5
    INVALID_AMOUNT = 6
    LEDGER_CORRUPTED = 7

STATUS_MESSAGES = {
    LedgerStatus.OK: "Success",
    LedgerStatus.INSUFFICIENT_BALANCE: "Insufficient balance",
    LedgerStatus.DUPLICATE_TXN: "Transaction already processed (idempotent)",
    LedgerStatus.INVALID_ACCOUNT: "Account does not exist",
    LedgerStatus.ACCOUNT_EXISTS: "Account already exists",
    LedgerStatus.TRANSFER_FAILED: "Transfer failed",
    LedgerStatus.INVALID_AMOUNT: "Invalid amount",
    LedgerStatus.LEDGER_CORRUPTED: "Ledger state corrupted",
}

# ═══════════════════════════════════════════════════════════════════════════
# CTYPES BINDINGS
# ═══════════════════════════════════════════════════════════════════════════

class LedgerWrapper:
    """
    Wraps C ledger library (ledger.so).
    
    Handles:
    - Loading .so file
    - Type conversions (Python ↔ C)
    - Error handling
    - Logging
    """
    
    def __init__(self, ledger_so_path: Optional[str] = None):
        """
        Initialize wrapper.
        
        Args:
            ledger_so_path: Path to ledger.so. If None, searches in:
                1. ./ledger/ledger.so
                2. ../ledger/ledger.so
                3. LD_LIBRARY_PATH
        """
        self.ledger_so_path = ledger_so_path or self._find_ledger_so()
        self.ledger_lib = None
        self.load_ledger()
    
    def _find_ledger_so(self) -> str:
        """Search for ledger.so in common locations."""
        candidates = [
            Path("./ledger/ledger.so"),
            Path("../ledger/ledger.so"),
            Path("./src/ledger.so"),
            Path("../src/ledger.so"),
        ]
        
        for path in candidates:
            if path.exists():
                logger.info(f"Found ledger.so at {path}")
                return str(path.absolute())
        
        # Last resort: try loading without path (LD_LIBRARY_PATH)
        logger.warning("ledger.so not found in candidates, will try LD_LIBRARY_PATH")
        return "ledger.so"
    
    def load_ledger(self):
        """Load C ledger library using ctypes."""
        try:
            self.ledger_lib = ctypes.CDLL(self.ledger_so_path)
            logger.info(f"✓ Loaded ledger from {self.ledger_so_path}")
        except OSError as e:
            logger.error(f"✗ Failed to load ledger.so: {e}")
            raise RuntimeError(f"Cannot load ledger.so from {self.ledger_so_path}: {e}")
        
        # Set up function signatures (stage 1 minimum)
        self._setup_signatures()
    
    def _setup_signatures(self):
        """Define C function signatures for ctypes."""
        
        # int ledger_init(void)
        self.ledger_lib.ledger_init.argtypes = []
        self.ledger_lib.ledger_init.restype = ctypes.c_int
        
        # void ledger_destroy(void)
        self.ledger_lib.ledger_destroy.argtypes = []
        self.ledger_lib.ledger_destroy.restype = None
        
        # int ledger_create_account(const char *account_id)
        self.ledger_lib.ledger_create_account.argtypes = [ctypes.c_char_p]
        self.ledger_lib.ledger_create_account.restype = ctypes.c_int
        
        # int ledger_transfer(
        #     const char *sender_id,
        #     const char *receiver_id,
        #     int64_t amount_cents,
        #     const char *txn_id,
        #     char *out_error_msg  // 512 bytes
        # )
        self.ledger_lib.ledger_transfer.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int64,
            ctypes.c_char_p,
            ctypes.c_char_p  # error message buffer
        ]
        self.ledger_lib.ledger_transfer.restype = ctypes.c_int
        
        # int64_t ledger_get_balance(const char *account_id, int *out_status)
        self.ledger_lib.ledger_get_balance.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int)
        ]
        self.ledger_lib.ledger_get_balance.restype = ctypes.c_int64
        
        # int ledger_get_account(
        #     const char *account_id,
        #     int64_t *out_balance,
        #     char *out_account_json  // 1024 bytes
        # )
        self.ledger_lib.ledger_get_account.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int64),
            ctypes.c_char_p
        ]
        self.ledger_lib.ledger_get_account.restype = ctypes.c_int
        
        # int ledger_verify_state(char *out_report)  // 2048 bytes
        self.ledger_lib.ledger_verify_state.argtypes = [ctypes.c_char_p]
        self.ledger_lib.ledger_verify_state.restype = ctypes.c_int
        
        # void ledger_dump_state(char *out_json)  // 8192 bytes
        self.ledger_lib.ledger_dump_state.argtypes = [ctypes.c_char_p]
        self.ledger_lib.ledger_dump_state.restype = None
    
    # ═══════════════════════════════════════════════════════════════════════
    # HIGH-LEVEL INTERFACE
    # ═══════════════════════════════════════════════════════════════════════
    
    def init(self) -> int:
        """Initialize ledger."""
        status = self.ledger_lib.ledger_init()
        if status == LedgerStatus.OK:
            logger.info("✓ Ledger initialized")
        else:
            logger.error(f"✗ Ledger init failed: {STATUS_MESSAGES.get(status, 'Unknown error')}")
        return status
    
    def destroy(self):
        """Clean up ledger."""
        self.ledger_lib.ledger_destroy()
        logger.info("✓ Ledger destroyed")
    
    def create_account(self, account_id: str, initial_balance_dollars: float = 0.0) -> Tuple[int, str]:
        """
        Create an account.
        
        Args:
            account_id: Unique account identifier (e.g., "alice", "bob")
            initial_balance_dollars: Starting balance (e.g., 5000.00)
        
        Returns:
            (status_code, message)
        """
        account_id_bytes = account_id.encode('utf-8')
        status = self.ledger_lib.ledger_create_account(account_id_bytes)
        
        message = STATUS_MESSAGES.get(status, "Unknown error")
        if status == LedgerStatus.OK:
            logger.info(f"✓ Created account: {account_id}")
        else:
            logger.warning(f"✗ Create account failed ({account_id}): {message}")
        
        return status, message
    
    def get_balance(self, account_id: str) -> Tuple[float, int, str]:
        """
        Get account balance.
        
        Args:
            account_id: Account to query
        
        Returns:
            (balance_dollars, status_code, message)
            
        Example:
            balance_usd, status, msg = wrapper.get_balance("alice")
            # balance_usd = 5000.00, status = 0 (OK)
        """
        account_id_bytes = account_id.encode('utf-8')
        status_ref = ctypes.c_int()
        balance_cents = self.ledger_lib.ledger_get_balance(account_id_bytes, ctypes.byref(status_ref))
        
        status = status_ref.value
        message = STATUS_MESSAGES.get(status, "Unknown error")
        balance_usd = balance_cents / 100.0
        
        return balance_usd, status, message
    
    def get_account(self, account_id: str) -> Tuple[Dict, int, str]:
        """
        Get full account info.
        
        Args:
            account_id: Account to query
        
        Returns:
            (account_dict, status_code, message)
            
        Example:
            account, status, msg = wrapper.get_account("alice")
            # account = {
            #   "account_id": "alice",
            #   "balance_dollars": 5000.00,
            #   "created_at": 1695875234,
            #   ...
            # }
        """
        account_id_bytes = account_id.encode('utf-8')
        balance_ref = ctypes.c_int64()
        account_json_buffer = ctypes.create_string_buffer(1024)
        
        status = self.ledger_lib.ledger_get_account(
            account_id_bytes,
            ctypes.byref(balance_ref),
            account_json_buffer
        )
        
        message = STATUS_MESSAGES.get(status, "Unknown error")
        
        if status == LedgerStatus.OK:
            account_json = account_json_buffer.value.decode('utf-8')
            import json
            account_dict = json.loads(account_json)
            return account_dict, status, message
        else:
            return {}, status, message
    
    def transfer(
        self,
        sender_id: str,
        receiver_id: str,
        amount_dollars: float,
        txn_id: str
    ) -> Tuple[int, str, Dict]:
        """
        Execute a transfer (atomic).
        
        Args:
            sender_id: Account to debit
            receiver_id: Account to credit
            amount_dollars: Amount to transfer (e.g., 50.00)
            txn_id: Unique transaction ID (for idempotency)
        
        Returns:
            (status_code, message, result_dict)
            
        Example:
            status, msg, result = wrapper.transfer("alice", "bob", 50.00, "txn_001")
            # status = 0 (OK)
            # msg = "Success"
            # result = {
            #   "txn_id": "txn_001",
            #   "sender_balance": 4950.00,
            #   "receiver_balance": 5050.00,
            #   "ledger_sum": 10000.00
            # }
        
        Error scenarios (status != 0):
            - INSUFFICIENT_BALANCE: sender balance too low
            - INVALID_ACCOUNT: sender or receiver doesn't exist
            - DUPLICATE_TXN: same txn_id already processed (idempotent response)
            - LEDGER_CORRUPTED: internal consistency error
        """
        sender_bytes = sender_id.encode('utf-8')
        receiver_bytes = receiver_id.encode('utf-8')
        amount_cents = int(amount_dollars * 100)  # Convert to cents
        txn_id_bytes = txn_id.encode('utf-8')
        error_msg_buffer = ctypes.create_string_buffer(512)
        
        # Call C function
        status = self.ledger_lib.ledger_transfer(
            sender_bytes,
            receiver_bytes,
            amount_cents,
            txn_id_bytes,
            error_msg_buffer
        )
        
        message = STATUS_MESSAGES.get(status, "Unknown error")
        error_detail = error_msg_buffer.value.decode('utf-8') if error_msg_buffer.value else ""
        
        result = {}
        if status == LedgerStatus.OK:
            # Get updated balances
            sender_balance, _, _ = self.get_balance(sender_id)
            receiver_balance, _, _ = self.get_balance(receiver_id)
            result = {
                "txn_id": txn_id,
                "sender_balance": sender_balance,
                "receiver_balance": receiver_balance,
                "transferred_amount": amount_dollars
            }
            logger.info(f"✓ Transfer {txn_id}: {sender_id}→{receiver_id} ${amount_dollars:.2f}")
        else:
            result = {"error": error_detail}
            logger.warning(f"✗ Transfer failed ({txn_id}): {message} — {error_detail}")
        
        return status, message, result
    
    def verify_state(self) -> Tuple[int, str, Dict]:
        """
        Verify ledger consistency.
        
        Returns:
            (status_code, message, verification_dict)
            
        Checks:
            - sum(all account balances) = constant
            - sum(debits) = sum(credits)
            - no negative balances
            - all transactions recorded
        
        Example:
            status, msg, report = wrapper.verify_state()
            # status = 0 (OK)
            # report = {
            #   "invariant_satisfied": true,
            #   "total_balance": 10000.00,
            #   "transaction_count": 5,
            #   "anomalies": []
            # }
        """
        report_buffer = ctypes.create_string_buffer(2048)
        status = self.ledger_lib.ledger_verify_state(report_buffer)
        
        message = STATUS_MESSAGES.get(status, "Unknown error")
        report_json = report_buffer.value.decode('utf-8') if report_buffer.value else "{}"
        
        import json
        try:
            report_dict = json.loads(report_json)
        except json.JSONDecodeError:
            report_dict = {"error": "Failed to parse verification report"}
        
        if status == LedgerStatus.OK:
            logger.info("✓ Ledger verification passed")
        else:
            logger.warning(f"✗ Ledger verification failed: {message}")
        
        return status, message, report_dict
    
    def get_ledger_state(self) -> Dict:
        """
        Dump complete ledger state (all accounts, all transactions).
        
        Returns:
            Full ledger state as dict
            
        Example:
            state = wrapper.get_ledger_state()
            # state = {
            #   "accounts": {
            #     "alice": {"balance": 4950.00, ...},
            #     "bob": {"balance": 5050.00, ...}
            #   },
            #   "transactions": [
            #     {"txn_id": "txn_001", "from": "alice", "to": "bob", ...}
            #   ],
            #   "metadata": {
            #     "total_balance": 10000.00,
            #     "transaction_count": 1
            #   }
            # }
        """
        state_buffer = ctypes.create_string_buffer(8192)
        self.ledger_lib.ledger_dump_state(state_buffer)
        
        state_json = state_buffer.value.decode('utf-8') if state_buffer.value else "{}"
        
        import json
        try:
            state_dict = json.loads(state_json)
        except json.JSONDecodeError:
            state_dict = {"error": "Failed to parse ledger state"}
        
        return state_dict


# ═══════════════════════════════════════════════════════════════════════════
# GLOBAL INSTANCE
# ═══════════════════════════════════════════════════════════════════════════

_wrapper_instance: Optional[LedgerWrapper] = None

def get_ledger_wrapper() -> LedgerWrapper:
    """Get or create global ledger wrapper instance."""
    global _wrapper_instance
    if _wrapper_instance is None:
        _wrapper_instance = LedgerWrapper()
    return _wrapper_instance

def init_ledger():
    """Initialize ledger on startup."""
    wrapper = get_ledger_wrapper()
    wrapper.init()

def shutdown_ledger():
    """Clean up ledger on shutdown."""
    global _wrapper_instance
    if _wrapper_instance:
        _wrapper_instance.destroy()
        _wrapper_instance = None


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    
    wrapper = LedgerWrapper()
    
    # Initialize
    wrapper.init()
    
    # Create accounts
    wrapper.create_account("alice")
    wrapper.create_account("bob")
    
    # Set initial balances (create with ctypes or via mock in stage-1)
    # For now, assume C ledger initializes with 0
    
    # Transfer
    status, msg, result = wrapper.transfer("alice", "bob", 50.00, "txn_001")
    print(f"Transfer: {msg}")
    print(f"Result: {result}")
    
    # Verify
    status, msg, report = wrapper.verify_state()
    print(f"Verification: {msg}")
    print(f"Report: {report}")
    
    # Get state
    state = wrapper.get_ledger_state()
    print(f"Ledger state: {state}")
    
    wrapper.destroy()
