/* ThreadSanitizer stress test for the ledger core.
 *
 *   gcc -g -O1 -fsanitize=thread -pthread -I../ledger ledger_stress.c ../ledger/ledger.c -o /tmp/ledger_stress
 *   /tmp/ledger_stress
 *
 * Mixes transfers (with replayed ids), reads, verify and stats from many
 * threads at once. TSan reports any data race; the program also checks that
 * conservation holds at the end. Exit code 0 means no failure was observed.
 */
#include "ledger.h"
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NTHREADS 8
#define OPS 3000
#define NACCTS 6
#define SEED_CENTS 1000000

static volatile int g_failures = 0;
static long g_ok[NTHREADS];

static void *worker(void *arg) {
    long id = (long)arg;
    unsigned seed = (unsigned)id * 7919u + 1u;
    char err[LEDGER_MAX_ERROR_MSG_LEN], s[16], r[16], txn[64];
    for (int k = 0; k < OPS; k++) {
        int a = rand_r(&seed) % NACCTS, b = rand_r(&seed) % NACCTS;
        snprintf(s, sizeof s, "acct%d", a);
        snprintf(r, sizeof r, "acct%d", b);
        /* every 10th op replays a txn id some other thread may also use */
        if (k % 10 == 0) snprintf(txn, sizeof txn, "shared_%d", k);
        else snprintf(txn, sizeof txn, "t%ld_%d", id, k);
        if (ledger_transfer(s, r, 1 + rand_r(&seed) % 5000, txn, err) == LEDGER_OK) g_ok[id]++;

        if (k % 7 == 0) {
            ledger_state_t st;
            if (ledger_verify_state(&st, err) != LEDGER_OK || st.has_anomalies) {
                fprintf(stderr, "verify anomaly: %s\n", st.anomaly_details);
                g_failures++;
            }
        }
        if (k % 5 == 0) {
            money_t bal; ledger_get_balance(s, &bal, err);
            uint32_t na, nt; money_t tv;
            ledger_get_stats(&na, &nt, &tv, err);
            if (tv != (money_t)NACCTS * SEED_CENTS) { g_failures++; }
        }
    }
    return NULL;
}

int main(void) {
    char err[LEDGER_MAX_ERROR_MSG_LEN], name[16];
    if (ledger_init() != LEDGER_OK) return 2;
    for (int i = 0; i < NACCTS; i++) {
        snprintf(name, sizeof name, "acct%d", i);
        ledger_create_account(name, SEED_CENTS, err);
    }
    pthread_t th[NTHREADS];
    for (long i = 0; i < NTHREADS; i++) pthread_create(&th[i], NULL, worker, (void *)i);
    for (int i = 0; i < NTHREADS; i++) pthread_join(th[i], NULL);

    uint32_t na, nt; money_t tv;
    ledger_get_stats(&na, &nt, &tv, err);
    long ok = 0; for (int i = 0; i < NTHREADS; i++) ok += g_ok[i];
    ledger_state_t st; ledger_verify_state(&st, err);
    printf("accounts=%u txns=%u ok_transfers=%ld total=%lld failures=%d anomalies=%d\n",
           na, nt, ok, (long long)tv, g_failures, st.has_anomalies);
    int bad = g_failures || st.has_anomalies || tv != (money_t)NACCTS * SEED_CENTS || (long)nt != ok;
    ledger_destroy();
    puts(bad ? "FAIL" : "PASS");
    return bad;
}
