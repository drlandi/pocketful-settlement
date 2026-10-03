/* Test-only linker instrumentation: rendezvous at the ledger's lock call.
 * The real mutex and all ledger business logic remain unchanged. */
#include <pthread.h>
#include <time.h>
#include <errno.h>

int __real_pthread_mutex_lock(pthread_mutex_t *mutex);
static pthread_mutex_t probe_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready = PTHREAD_COND_INITIALIZER;
static unsigned target, arrived;
static int armed, timed_out;

void probe_arm(unsigned participants) {
    __real_pthread_mutex_lock(&probe_lock);
    target = participants;
    arrived = 0;
    timed_out = 0;
    armed = 1;
    pthread_mutex_unlock(&probe_lock);
}

int __wrap_pthread_mutex_lock(pthread_mutex_t *mutex) {
    __real_pthread_mutex_lock(&probe_lock);
    if (armed) {
        arrived++;
        if (arrived == target) {
            armed = 0;
            pthread_cond_broadcast(&ready);
        } else {
            struct timespec deadline;
            clock_gettime(CLOCK_REALTIME, &deadline);
            deadline.tv_sec += 5;
            while (armed) {
                if (pthread_cond_timedwait(&ready, &probe_lock, &deadline) == ETIMEDOUT) {
                    timed_out = 1;
                    armed = 0;
                    pthread_cond_broadcast(&ready);
                }
            }
        }
    }
    pthread_mutex_unlock(&probe_lock);
    return __real_pthread_mutex_lock(mutex);
}

unsigned probe_arrived(void) { return arrived; }
int probe_timed_out(void) { return timed_out; }
