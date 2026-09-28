#include "ledger.h"
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdio.h>

/*
 * settlement-agents: Ledger Implementation (C)
 * =============================================
 *
 * SKELETON for you to implement.
 * 
 * Key points:
 * - All functions MUST be thread-safe (use ledger_lock for writes)
 * - Transfers MUST be atomic (both debit AND credit, or neither)
 * - Idempotency: Same txn_id always produces same result
 * - Error messages go in error_msg buffer (caller allocates)
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
    
    pthread_mutex_t lock;  /* Protects all ledger state */
    int initialized;       /* 1 if ledger_init() was called */
} g_ledger = {
    .accounts = NULL,
    .num_accounts = 0,
    .max_accounts = LEDGER_MAX_ACCOUNTS,
    .transactions = NULL,
    .num_transactions = 0,
    .max_transactions = LEDGER_MAX_TRANSACTIONS,
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
    g_ledger.initialized = 0;
    
    pthread_mutex_unlock(&g_ledger.lock);
}

ledger_status_t ledger_create_account(
    const char *account_id,
    money_t initial_balance,
    char *error_msg
) {
    if (!error_msg) return LEDGER_DB_ERROR;
    
    /* Validate inputs */
    if (!account_id || !account_id[0]) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Invalid account_id");
        return LEDGER_INVALID_ACCOUNT;
    }
    
    if (initial_balance < 0) {
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Initial balance cannot be negative");
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
    
    /* ATOMIC TRANSFER: Debit + Credit + Record */
    g_ledger.accounts[sender_idx].balance -= amount;
    g_ledger.accounts[sender_idx].updated_at = time(NULL);
    
    g_ledger.accounts[receiver_idx].balance += amount;
    g_ledger.accounts[receiver_idx].updated_at = time(NULL);
    
    /* Record transaction */
    if (g_ledger.num_transactions >= g_ledger.max_transactions) {
        /* Rollback on failure to record */
        g_ledger.accounts[sender_idx].balance += amount;
        g_ledger.accounts[receiver_idx].balance -= amount;
        pthread_mutex_unlock(&g_ledger.lock);
        snprintf(error_msg, LEDGER_MAX_ERROR_MSG_LEN, "Ledger full (max transactions reached)");
        return LEDGER_DB_ERROR;
    }
    
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
    
    /* Calculate totals */
    money_t total_debits = 0;
    money_t total_credits = 0;
    
    for (uint32_t i = 0; i < g_ledger.num_transactions; i++) {
        if (g_ledger.transactions[i].status == 1) {  /* executed */
            total_debits += g_ledger.transactions[i].amount;
            total_credits += g_ledger.transactions[i].amount;
        }
    }
    
    /* Check for anomalies */
    int has_anomalies = 0;
    char anomaly_details[512] = {0};
    
    if (total_debits != total_credits) {
        has_anomalies = 1;
        snprintf(anomaly_details, sizeof(anomaly_details),
            "Debit/credit mismatch: debits=%ld, credits=%ld",
            total_debits, total_credits);
    }
    
    /* Check for negative balances */
    for (uint32_t i = 0; i < g_ledger.num_accounts; i++) {
        if (g_ledger.accounts[i].balance < 0) {
            has_anomalies = 1;
            snprintf(anomaly_details, sizeof(anomaly_details),
                "Negative balance on account %s: %ld",
                g_ledger.accounts[i].account_id, g_ledger.accounts[i].balance);
            break;
        }
    }
    
    /* Populate state */
    state->num_accounts = g_ledger.num_accounts;
    state->num_transactions = g_ledger.num_transactions;
    state->total_debits = total_debits;
    state->total_credits = total_credits;
    state->is_balanced = (total_debits == total_credits) ? 1 : 0;
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
        printf("%s: %.2f cents (updated: %s)\n",
            g_ledger.accounts[i].account_id,
            (double)g_ledger.accounts[i].balance / 100.0,
            ctime(&g_ledger.accounts[i].updated_at));
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
        printf("%s: %s -> %s: %.2f cents [%s] (time: %s)\n",
            txn->txn_id, txn->sender_id, txn->receiver_id,
            (double)txn->amount / 100.0, status_str, ctime(&txn->timestamp));
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
