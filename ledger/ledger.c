#include "ledger.h"
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdio.h>
#include <inttypes.h>

/*
 * settlement-agents: Ledger Implementation (C)
 * =============================================
 *
 * Design:
 * - One global mutex (g_ledger.lock) guards all state. Every public function
 *   takes it, reads included, and releases it on every return path.
 * - A transfer validates, checks for a replayed txn_id, checks balance and
 *   table capacity, then debits, credits and records, all in one critical
 *   section. Every check that can fail runs BEFORE the first mutation, so
 *   there is nothing to roll back.
 * - Only executed transfers are recorded, so a transaction record exists if
 *   and only if the transfer took effect. Executor recovery relies on this.
 * - Callers allocate error_msg with LEDGER_MAX_ERROR_MSG_LEN bytes.
 * - Memory only: nothing survives a restart.
 */

/* ============================================================================
 * INTERNAL DATA STRUCTURES & LOCKS
 * ============================================================================ */

/* Global ledger state */
static struct {
    ledger_account_t *accounts;
    uint32_t num_accounts;
    uint32_t max_accounts;

    ledger_transaction_t *transactions;
    uint32_t num_transactions;
    uint32_t max_transactions;

    money_t total_seeded;  /* Sum of all initial balances: the conservation target */

    pthread_mutex_t lock;  /* Protects all ledger state */
    int initialized;       /* 1 if ledger_init() was called */
} g_ledger = {
    .accounts = NULL,
    .num_accounts = 0,
    .max_accounts = LEDGER_MAX_ACCOUNTS,
    .transactions = NULL,
    .num_transactions = 0,
    .max_transactions = LEDGER_MAX_TRANSACTIONS,
    .total_seeded = 0,
    .lock = PTHREAD_MUTEX_INITIALIZER,
    .initialized = 0
};

/* ============================================================================
 * HELPER FUNCTIONS (Internal)
 * ============================================================================ */

/*
 * find_account(account_id)
 * Find account by ID. Returns index in g_ledger.accounts, or -1 if not found.
 *
 * MUST be called with g_ledger.lock held.
 */
static int find_account(const char *account_id) {
    if (!account_id || !account_id[0]) {
        return -1;
    }

    for (uint32_t i = 0; i < g_ledger.num_accounts; i++) {
        if (strcmp(g_ledger.accounts[i].account_id, account_id) == 0) {
            return i;
        }
    }
    return -1;
}

/*
 * find_transaction(txn_id)
 * Find transaction by ID. Returns index, or -1 if not found.
 *
 * MUST be called with g_ledger.lock held.
 */
static int find_transaction(const char *txn_id) {
    if (!txn_id || !txn_id[0]) {
        return -1;
    }

    for (uint32_t i = 0; i < g_ledger.num_transactions; i++) {
        if (strcmp(g_ledger.transactions[i].txn_id, txn_id) == 0) {
            return i;
        }
    }
    return -1;
}

/*
 * validate_inputs(sender_id, receiver_id, amount, txn_id, error_msg)
 * Basic validation of transfer inputs.
 * Returns LEDGER_OK if valid, error code otherwise.
 */
static ledger_status_t validate_inputs(
    const char *sender_id,
    const char *receiver_id,
    money_t amount,
    const char *txn_id,
    char *error_msg
) {
    /* Validate sender_id */
    if (!sender_id || !sender_id[0] || strlen(sender_id) >= LEDGER_MAX_ACCOUNT_ID_LEN) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Invalid sender_id");
        return LEDGER_INVALID_ACCOUNT;
    }

    /* Validate receiver_id */
    if (!receiver_id || !receiver_id[0] || strlen(receiver_id) >= LEDGER_MAX_ACCOUNT_ID_LEN) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Invalid receiver_id");
        return LEDGER_INVALID_ACCOUNT;
    }

    /* Validate amount */
    if (amount <= 0) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Amount must be > 0");
        return LEDGER_INVALID_AMOUNT;
    }

    /* Validate txn_id */
    if (!txn_id || !txn_id[0] || strlen(txn_id) >= LEDGER_MAX_TXN_ID_LEN) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Invalid txn_id");
        return LEDGER_INVALID_AMOUNT;
    }

    /* Sender and receiver must be different */
    if (strcmp(sender_id, receiver_id) == 0) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Sender and receiver must be different");
        return LEDGER_INVALID_ACCOUNT;
    }

    return LEDGER_OK;
}

/* ============================================================================
 * PUBLIC API IMPLEMENTATION
 * ============================================================================ */

ledger_status_t ledger_init(void) {
    pthread_mutex_lock(&g_ledger.lock);

    if (g_ledger.initialized) {
        pthread_mutex_unlock(&g_ledger.lock);
        return LEDGER_OK;
    }

    /* Allocate account storage */
    g_ledger.accounts = (ledger_account_t *)malloc(
        sizeof(ledger_account_t) * g_ledger.max_accounts
    );
    if (!g_ledger.accounts) {
        pthread_mutex_unlock(&g_ledger.lock);
        return LEDGER_DB_ERROR;
    }

    /* Allocate transaction storage */
    g_ledger.transactions = (ledger_transaction_t *)malloc(
        sizeof(ledger_transaction_t) * g_ledger.max_transactions
    );
    if (!g_ledger.transactions) {
        free(g_ledger.accounts);
        g_ledger.accounts = NULL;
        pthread_mutex_unlock(&g_ledger.lock);
        return LEDGER_DB_ERROR;
    }

    g_ledger.num_accounts = 0;
    g_ledger.num_transactions = 0;
    g_ledger.total_seeded = 0;
    g_ledger.initialized = 1;

    pthread_mutex_unlock(&g_ledger.lock);
    return LEDGER_OK;
}

void ledger_destroy(void) {
    pthread_mutex_lock(&g_ledger.lock);

    if (g_ledger.accounts) {
        free(g_ledger.accounts);
        g_ledger.accounts = NULL;
    }

    if (g_ledger.transactions) {
        free(g_ledger.transactions);
        g_ledger.transactions = NULL;
    }

    g_ledger.num_accounts = 0;
    g_ledger.num_transactions = 0;
    g_ledger.total_seeded = 0;
    g_ledger.initialized = 0;

    pthread_mutex_unlock(&g_ledger.lock);
}

ledger_status_t ledger_create_account(
    const char *account_id,
    money_t initial_balance,
    char *error_msg
) {
    if (!error_msg) return LEDGER_DB_ERROR;

    /* Validate inputs. Over-long IDs are rejected, never truncated: a
     * truncated ID could collide with a different account. */
    if (!account_id || !account_id[0] || strlen(account_id) >= LEDGER_MAX_ACCOUNT_ID_LEN) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Invalid account_id");
        return LEDGER_INVALID_ACCOUNT;
    }

    if (initial_balance < 0) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Initial balance cannot be negative");
        return LEDGER_INVALID_AMOUNT;
    }

    if (initial_balance > LEDGER_MAX_INITIAL_BALANCE) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Initial balance exceeds maximum");
        return LEDGER_INVALID_AMOUNT;
    }

    pthread_mutex_lock(&g_ledger.lock);

    if (!g_ledger.initialized) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger not initialized");
        return LEDGER_DB_ERROR;
    }

    /* Check if account already exists */
    if (find_account(account_id) != -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Account already exists");
        return LEDGER_DB_ERROR;
    }

    /* Check space */
    if (g_ledger.num_accounts >= g_ledger.max_accounts) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger full (max accounts reached)");
        return LEDGER_DB_ERROR;
    }

    /* Create account */
    ledger_account_t *new_account = &g_ledger.accounts[g_ledger.num_accounts];
    strncpy(new_account->account_id, account_id, LEDGER_MAX_ACCOUNT_ID_LEN - 1);
    new_account->account_id[LEDGER_MAX_ACCOUNT_ID_LEN - 1] = '\0';
    new_account->balance = initial_balance;
    new_account->updated_at = time(NULL);

    g_ledger.total_seeded += initial_balance;
    g_ledger.num_accounts++;

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

ledger_status_t ledger_transfer(
    const char *sender_id,
    const char *receiver_id,
    money_t amount,
    const char *txn_id,
    char *error_msg
) {
    if (!error_msg) return LEDGER_DB_ERROR;

    /* Validate inputs */
    ledger_status_t validation_result = validate_inputs(
        sender_id, receiver_id, amount, txn_id, error_msg
    );
    if (validation_result != LEDGER_OK) {
        return validation_result;
    }

    pthread_mutex_lock(&g_ledger.lock);

    if (!g_ledger.initialized) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger not initialized");
        return LEDGER_DB_ERROR;
    }

    /* Check if transaction already exists (idempotency). Done under the lock so
     * two concurrent attempts with the same txn_id cannot both pass. A replay
     * moves no money and is reported as DUPLICATE_TXN, never as OK, so callers
     * can tell a retry from a first attempt. */
    if (find_transaction(txn_id) != -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN,
                 "Transaction already recorded; no funds moved");
        return LEDGER_DUPLICATE_TXN;
    }

    /* Find sender and receiver */
    int sender_idx = find_account(sender_id);
    int receiver_idx = find_account(receiver_id);

    if (sender_idx == -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Sender account not found");
        return LEDGER_INVALID_ACCOUNT;
    }

    if (receiver_idx == -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Receiver account not found");
        return LEDGER_INVALID_ACCOUNT;
    }

    /* Check balance */
    if (g_ledger.accounts[sender_idx].balance < amount) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Insufficient balance");
        return LEDGER_INSUFFICIENT_BALANCE;
    }

    /* Check capacity BEFORE touching any balance, so a full table leaves the
     * ledger exactly as it was and no rollback is needed. */
    if (g_ledger.num_transactions >= g_ledger.max_transactions) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger full (max transactions reached)");
        return LEDGER_DB_ERROR;
    }

    /* ATOMIC TRANSFER: Debit + Credit + Record. Nothing below can fail, and
     * the lock is held throughout, so no thread observes a half-applied
     * transfer. The receiver cannot overflow: balances sum to total_seeded,
     * which is bounded by LEDGER_MAX_INITIAL_BALANCE * LEDGER_MAX_ACCOUNTS. */
    g_ledger.accounts[sender_idx].balance -= amount;
    g_ledger.accounts[sender_idx].updated_at = time(NULL);

    g_ledger.accounts[receiver_idx].balance += amount;
    g_ledger.accounts[receiver_idx].updated_at = time(NULL);

    ledger_transaction_t *new_txn = &g_ledger.transactions[g_ledger.num_transactions];
    strncpy(new_txn->txn_id, txn_id, LEDGER_MAX_TXN_ID_LEN - 1);
    new_txn->txn_id[LEDGER_MAX_TXN_ID_LEN - 1] = '\0';
    strncpy(new_txn->sender_id, sender_id, LEDGER_MAX_ACCOUNT_ID_LEN - 1);
    new_txn->sender_id[LEDGER_MAX_ACCOUNT_ID_LEN - 1] = '\0';
    strncpy(new_txn->receiver_id, receiver_id, LEDGER_MAX_ACCOUNT_ID_LEN - 1);
    new_txn->receiver_id[LEDGER_MAX_ACCOUNT_ID_LEN - 1] = '\0';
    new_txn->amount = amount;
    new_txn->timestamp = time(NULL);
    new_txn->status = 1;  /* executed */
    new_txn->error_reason[0] = '\0';

    g_ledger.num_transactions++;

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

ledger_status_t ledger_get_balance(
    const char *account_id,
    money_t *balance,
    char *error_msg
) {
    if (!error_msg || !balance) return LEDGER_DB_ERROR;

    pthread_mutex_lock(&g_ledger.lock);

    int idx = find_account(account_id);
    if (idx == -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Account not found");
        return LEDGER_INVALID_ACCOUNT;
    }

    *balance = g_ledger.accounts[idx].balance;

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

ledger_status_t ledger_verify_state(
    ledger_state_t *state,
    char *error_msg
) {
    if (!error_msg || !state) return LEDGER_DB_ERROR;

    pthread_mutex_lock(&g_ledger.lock);

    if (!g_ledger.initialized) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger not initialized");
        return LEDGER_DB_ERROR;
    }

    /* Tally of executed transactions. Both sides come from the same records,
     * so this cannot detect a mismatch; see the conservation check below. */
    money_t total_debits = 0;
    money_t total_credits = 0;

    for (uint32_t i = 0; i < g_ledger.num_transactions; i++) {
        if (g_ledger.transactions[i].status == 1) {  /* executed */
            total_debits += g_ledger.transactions[i].amount;
            total_credits += g_ledger.transactions[i].amount;
        }
    }

    int has_anomalies = 0;
    char anomaly_details[512] = {0};
    size_t off = 0;
#define ADD_ANOMALY(...) do { \
        has_anomalies = 1; \
        if (off < sizeof(anomaly_details)) { \
            int n_ = snprintf(anomaly_details + off, sizeof(anomaly_details) - off, __VA_ARGS__); \
            if (n_ > 0) off += (size_t)n_; \
        } \
    } while (0)

    if (total_debits != total_credits) {
        ADD_ANOMALY("Debit/credit mismatch: debits=%" PRId64 ", credits=%" PRId64 ". ",
                    total_debits, total_credits);
    }

    /* Negative balances, and the sum of all balances for conservation. */
    money_t sum_balances = 0;
    int sum_overflow = 0;
    for (uint32_t i = 0; i < g_ledger.num_accounts; i++) {
        money_t b = g_ledger.accounts[i].balance;
        if (b < 0) {
            ADD_ANOMALY("Negative balance on account %s: %" PRId64 ". ",
                        g_ledger.accounts[i].account_id, b);
        }
        if (__builtin_add_overflow(sum_balances, b, &sum_balances)) {
            sum_overflow = 1;
        }
    }

    /* CONSERVATION: the real invariant. Balances must sum to what was seeded. */
    int conserved = !sum_overflow && (sum_balances == g_ledger.total_seeded);
    if (sum_overflow) {
        ADD_ANOMALY("Balance sum overflowed int64. ");
    } else if (!conserved) {
        ADD_ANOMALY("Conservation violated: balances sum to %" PRId64
                    " but %" PRId64 " was seeded. ",
                    sum_balances, g_ledger.total_seeded);
    }
#undef ADD_ANOMALY

    /* Populate state */
    state->num_accounts = g_ledger.num_accounts;
    state->num_transactions = g_ledger.num_transactions;
    state->total_debits = total_debits;
    state->total_credits = total_credits;
    state->is_balanced = (total_debits == total_credits && conserved) ? 1 : 0;
    state->has_anomalies = has_anomalies;
    strncpy(state->anomaly_details, anomaly_details, sizeof(state->anomaly_details) - 1);
    state->anomaly_details[sizeof(state->anomaly_details) - 1] = '\0';

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

ledger_status_t ledger_get_account(
    const char *account_id,
    ledger_account_t *account,
    char *error_msg
) {
    if (!error_msg || !account) return LEDGER_DB_ERROR;

    pthread_mutex_lock(&g_ledger.lock);

    int idx = find_account(account_id);
    if (idx == -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Account not found");
        return LEDGER_INVALID_ACCOUNT;
    }

    *account = g_ledger.accounts[idx];

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

ledger_status_t ledger_get_transaction(
    const char *txn_id,
    ledger_transaction_t *txn,
    char *error_msg
) {
    if (!error_msg || !txn) return LEDGER_DB_ERROR;

    pthread_mutex_lock(&g_ledger.lock);

    int idx = find_transaction(txn_id);
    if (idx == -1) {
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Transaction not found");
        return LEDGER_INVALID_ACCOUNT;
    }

    *txn = g_ledger.transactions[idx];

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}

void ledger_dump_accounts(void) {
    pthread_mutex_lock(&g_ledger.lock);

    printf("\n=== ACCOUNTS ===\n");
    for (uint32_t i = 0; i < g_ledger.num_accounts; i++) {
        char tbuf[32] = {0};
        ctime_r(&g_ledger.accounts[i].updated_at, tbuf);
        tbuf[strcspn(tbuf, "\n")] = '\0';
        printf("%s: %" PRId64 " cents (updated: %s)\n",
            g_ledger.accounts[i].account_id,
            g_ledger.accounts[i].balance,
            tbuf);
    }
    printf("\n");

    pthread_mutex_unlock(&g_ledger.lock);
}

void ledger_dump_transactions(void) {
    pthread_mutex_lock(&g_ledger.lock);

    printf("\n=== TRANSACTIONS ===\n");
    for (uint32_t i = 0; i < g_ledger.num_transactions; i++) {
        ledger_transaction_t *txn = &g_ledger.transactions[i];
        const char *status_str = (txn->status == 0) ? "planned" :
                                 (txn->status == 1) ? "executed" : "rejected";
        char tbuf[32] = {0};
        ctime_r(&txn->timestamp, tbuf);
        tbuf[strcspn(tbuf, "\n")] = '\0';
        printf("%s: %s -> %s: %" PRId64 " cents [%s] (time: %s)\n",
            txn->txn_id, txn->sender_id, txn->receiver_id,
            txn->amount, status_str, tbuf);
    }
    printf("\n");

    pthread_mutex_unlock(&g_ledger.lock);
}

ledger_status_t ledger_get_stats(
    uint32_t *num_accounts,
    uint32_t *num_txns,
    money_t *total_value,
    char *error_msg
) {
    if (!error_msg) return LEDGER_DB_ERROR;

    pthread_mutex_lock(&g_ledger.lock);

    if (num_accounts) *num_accounts = g_ledger.num_accounts;
    if (num_txns) *num_txns = g_ledger.num_transactions;

    money_t total = 0;
    for (uint32_t i = 0; i < g_ledger.num_accounts; i++) {
        total += g_ledger.accounts[i].balance;
    }
    if (total_value) *total_value = total;

    pthread_mutex_unlock(&g_ledger.lock);

    snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "OK");
    return LEDGER_OK;
}
