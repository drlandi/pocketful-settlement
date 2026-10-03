#ifndef LEDGER_H
#define LEDGER_H

#include <time.h>
#include <stdint.h>

/*
 * settlement-agents: Ledger Core Interface (C)
 * ============================================
 *
 * This header defines the core ledger interface for the pocketful
 * payment settlement system. The ledger guarantees:
 *
 * 1. ATOMICITY: Transfers debit/credit atomically or fail completely
 * 2. CONSISTENCY: The sum of all balances always equals the sum of all
 *    initial balances (money is neither created nor destroyed).
 *    ledger_verify_state() checks this.
 * 3. ISOLATION: Concurrent transfers don't interfere (one global mutex
 *    serializes every operation, reads included)
 *
 * NOT provided: DURABILITY. State lives in process memory only and is lost
 * on restart. Transactions are recorded for the lifetime of the process.
 *
 * Thread-safety: All functions are thread-safe via one internal mutex.
 * Error handling: Functions return status codes; details in error_msg buffer.
 */

/* ============================================================================
 * DATA STRUCTURES
 * ============================================================================ */

/* Status codes for operations */
typedef enum {
    LEDGER_OK = 0,                  /* Success */
    LEDGER_INVALID_ACCOUNT = 1,     /* Account doesn't exist */
    LEDGER_INSUFFICIENT_BALANCE = 2,/* Not enough funds */
    LEDGER_DUPLICATE_TXN = 3,       /* Transaction ID already recorded */
    LEDGER_INVALID_AMOUNT = 4,      /* Amount <= 0 or precision issue */
    LEDGER_CONCURRENT_CONFLICT = 5, /* Reserved: never returned (one global lock
                                       serializes all operations) */
    LEDGER_DB_ERROR = 6,            /* Ledger storage/memory error */
    LEDGER_UNKNOWN_ERROR = 7        /* Unexpected error */
} ledger_status_t;

/* Maximum sizes. ACCOUNTS and TRANSACTIONS can be overridden at compile time
 * (-DLEDGER_MAX_TRANSACTIONS=50) so tests can exercise the full-table paths. */
#define LEDGER_MAX_ACCOUNT_ID_LEN 256
#define LEDGER_MAX_TXN_ID_LEN 64
#define LEDGER_MAX_ERROR_MSG_LEN 512
#ifndef LEDGER_MAX_ACCOUNTS
#define LEDGER_MAX_ACCOUNTS 10000
#endif
#ifndef LEDGER_MAX_TRANSACTIONS
#define LEDGER_MAX_TRANSACTIONS 100000
#endif

/* Largest initial balance one account may be created with: 10^14 cents
 * ($1 trillion). With at most 10,000 accounts the total stays <= 10^18, which
 * fits in int64 (max ~9.22 * 10^18), so no balance sum can overflow. */
#define LEDGER_MAX_INITIAL_BALANCE ((int64_t)100000000000000LL)

/* Money is stored as integer cents to avoid floating-point rounding errors.
 * Example: $100.50 = 10050 (cents)
 */
typedef int64_t money_t;  /* Amount in cents */

/* Account snapshot (read-only view) */
typedef struct {
    char account_id[LEDGER_MAX_ACCOUNT_ID_LEN];
    money_t balance;           /* Current balance in cents */
    time_t updated_at;         /* When balance was last updated */
} ledger_account_t;

/* Transaction record (immutable once written) */
typedef struct {
    char txn_id[LEDGER_MAX_TXN_ID_LEN];
    char sender_id[LEDGER_MAX_ACCOUNT_ID_LEN];
    char receiver_id[LEDGER_MAX_ACCOUNT_ID_LEN];
    money_t amount;            /* Amount transferred in cents */
    time_t timestamp;          /* When transaction was created */
    int8_t status;             /* 0=planned, 1=executed, 2=rejected */
    char error_reason[256];    /* Why it failed (if status=rejected) */
} ledger_transaction_t;

/* Ledger state snapshot for verification */
typedef struct {
    uint32_t num_accounts;
    uint32_t num_transactions;
    money_t total_debits;      /* Sum of all outgoing transfers */
    money_t total_credits;     /* Sum of all incoming transfers */
    int8_t is_balanced;        /* 1 if debits == credits AND balances conserved */
    int8_t has_anomalies;      /* 1 if inconsistencies detected */
    char anomaly_details[512]; /* Description if has_anomalies */
} ledger_state_t;

/* ============================================================================
 * INITIALIZATION
 * ============================================================================ */

/*
 * ledger_init()
 * Initialize the ledger system. Must be called once at startup.
 * 
 * Returns:
 *   LEDGER_OK on success
 *   LEDGER_DB_ERROR if memory allocation fails
 *
 * Thread-safety: NOT thread-safe. Call this before any other thread
 *                accesses the ledger.
 */
ledger_status_t ledger_init(void);

/*
 * ledger_destroy()
 * Clean up ledger resources. Call once at shutdown.
 *
 * Thread-safety: NOT thread-safe. Ensure no threads are using the ledger
 *                before calling.
 */
void ledger_destroy(void);

/*
 * ledger_create_account(account_id, initial_balance)
 * Create a new account with initial balance.
 *
 * Args:
 *   account_id: Unique account identifier (e.g., "alice")
 *   initial_balance: Starting balance in cents
 *
 * Returns:
 *   LEDGER_OK on success
 *   LEDGER_INVALID_ACCOUNT if account_id is NULL, empty, or >= 256 bytes
 *   LEDGER_INVALID_AMOUNT if initial_balance < 0 or > LEDGER_MAX_INITIAL_BALANCE
 *   LEDGER_DB_ERROR if account already exists or the account table is full
 *
 * Thread-safety: Thread-safe (uses internal locks)
 */
ledger_status_t ledger_create_account(
    const char *account_id,
    money_t initial_balance,
    char *error_msg  /* Out: error details if status != LEDGER_OK */
);

/* ============================================================================
 * TRANSFER OPERATIONS
 * ============================================================================ */

/*
 * ledger_transfer(sender_id, receiver_id, amount, txn_id)
 * Execute an atomic transfer. Debits sender, credits receiver, records txn.
 *
 * ATOMICITY GUARANTEE:
 *   - If function returns LEDGER_OK: Both debit AND credit were recorded.
 *   - If function returns error: NEITHER debit nor credit occurred (rollback).
 *   - Replaying a recorded txn_id moves no money and returns LEDGER_DUPLICATE_TXN.
 *
 * Args:
 *   sender_id: Account to debit
 *   receiver_id: Account to credit
 *   amount: Transfer amount in cents (must be > 0)
 *   txn_id: Unique transaction identifier
 *   error_msg: Out parameter for error details
 *
 * Returns:
 *   LEDGER_OK if transfer completed
 *   LEDGER_INVALID_ACCOUNT if sender or receiver doesn't exist
 *   LEDGER_INSUFFICIENT_BALANCE if sender balance < amount
 *   LEDGER_INVALID_AMOUNT if amount <= 0, or txn_id is empty or >= 64 bytes
 *   LEDGER_DUPLICATE_TXN if txn_id already recorded
 *   LEDGER_DB_ERROR if the transaction table is full (nothing is changed)
 *
 * Thread-safety: Thread-safe. Concurrent calls are serialized (locked).
 *
 * Example:
 *   ledger_transfer("alice", "bob", 5000, "txn_001", error_msg);
 *   // Transfers $50.00 from alice to bob with unique ID
 */
ledger_status_t ledger_transfer(
    const char *sender_id,
    const char *receiver_id,
    money_t amount,
    const char *txn_id,
    char *error_msg  /* Out: error details if status != LEDGER_OK */
);

/*
 * ledger_get_balance(account_id, balance)
 * Get current balance of an account.
 *
 * Args:
 *   account_id: Account identifier
 *   balance: Out parameter for balance in cents
 *
 * Returns:
 *   LEDGER_OK on success
 *   LEDGER_INVALID_ACCOUNT if account doesn't exist
 *
 * Thread-safety: Thread-safe (reads take the same global lock as writes)
 */
ledger_status_t ledger_get_balance(
    const char *account_id,
    money_t *balance,
    char *error_msg
);

/* ============================================================================
 * VERIFICATION & AUDITING
 * ============================================================================ */

/*
 * ledger_verify_state(state)
 * Verify ledger consistency and return current state.
 *
 * Checks performed:
 *   - CONSERVATION: the sum of all account balances equals the sum of all
 *     initial balances. This is the real invariant; it fails if money was
 *     created or destroyed.
 *   - No account has a negative balance.
 *   - Tally of executed transactions (total_debits == total_credits). Both
 *     sides are summed from the same records, so this alone cannot fail;
 *     it is kept for compatibility and does not prove conservation.
 *
 * NOT checked (would need O(n^2) work under the global lock): duplicate
 * transaction IDs, orphaned transactions.
 *
 * Args:
 *   state: Out parameter with ledger snapshot. is_balanced is 1 only when
 *          the tally AND the conservation check both pass.
 *
 * Returns:
 *   LEDGER_OK whenever verification ran, EVEN IF anomalies were found.
 *     Inspect state->has_anomalies and state->anomaly_details.
 *   LEDGER_DB_ERROR if the ledger is not initialized or arguments are NULL
 *
 * Thread-safety: Thread-safe (locks ledger during verification)
 *
 * Example:
 *   ledger_state_t state;
 *   ledger_verify_state(&state, err);
 *   if (state.has_anomalies) {
 *       printf("Anomaly: %s\n", state.anomaly_details);
 *   }
 */
ledger_status_t ledger_verify_state(
    ledger_state_t *state,
    char *error_msg
);

/*
 * ledger_get_account(account_id, account)
 * Get snapshot of an account (balance + metadata).
 *
 * Args:
 *   account_id: Account identifier
 *   account: Out parameter with account snapshot
 *
 * Returns:
 *   LEDGER_OK on success
 *   LEDGER_INVALID_ACCOUNT if doesn't exist
 *
 * Thread-safety: Thread-safe
 */
ledger_status_t ledger_get_account(
    const char *account_id,
    ledger_account_t *account,
    char *error_msg
);

/*
 * ledger_get_transaction(txn_id, txn)
 * Retrieve a transaction record by ID.
 *
 * Args:
 *   txn_id: Transaction identifier
 *   txn: Out parameter with transaction details
 *
 * Returns:
 *   LEDGER_OK on success
 *   LEDGER_INVALID_ACCOUNT if txn_id not found (reuse for "not found")
 *
 * Thread-safety: Thread-safe
 */
ledger_status_t ledger_get_transaction(
    const char *txn_id,
    ledger_transaction_t *txn,
    char *error_msg
);

/* ============================================================================
 * DEBUGGING / TESTING
 * ============================================================================ */

/*
 * ledger_dump_accounts()
 * Print all accounts and balances to stdout (for debugging).
 *
 * Thread-safety: Thread-safe
 */
void ledger_dump_accounts(void);

/*
 * ledger_dump_transactions()
 * Print all transactions to stdout (for debugging).
 *
 * Thread-safety: Thread-safe
 */
void ledger_dump_transactions(void);

/*
 * ledger_get_stats(num_accounts, num_txns, total_value)
 * Get ledger statistics.
 *
 * Thread-safety: Thread-safe
 */
ledger_status_t ledger_get_stats(
    uint32_t *num_accounts,
    uint32_t *num_txns,
    money_t *total_value,
    char *error_msg
);

#endif /* LEDGER_H */
