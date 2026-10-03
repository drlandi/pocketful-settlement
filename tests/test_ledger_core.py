"""Transaction-safety tests for the C ledger core.

These tests load ledger/ledger.c directly (no HTTP, no FastAPI), because that is
where atomicity, idempotency and concurrency are actually guaranteed. Each run
compiles the current source into a temporary .so, so a stale ledger.so can never
be what gets tested.

Run from the repo root:  python -m pytest tests/test_ledger_core.py -v

Two things need privileged access that the public API deliberately lacks, so
they use a throwaway test build (the product code is not modified):
  * making ledger_verify_state fail, by corrupting a balance behind its back
  * shrinking the account / transaction tables, so the "table full" paths run
    without creating 100,000 transactions
"""
import ctypes
import random
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

LEDGER_DIR = Path(__file__).resolve().parent.parent / "ledger"

# Mirror of ledger_status_t in ledger.h
OK, INVALID_ACCOUNT, INSUFFICIENT, DUPLICATE, INVALID_AMOUNT = 0, 1, 2, 3, 4
CONFLICT, DB_ERROR = 5, 6

MAX_INITIAL_BALANCE = 100_000_000_000_000  # LEDGER_MAX_INITIAL_BALANCE


class Account(ctypes.Structure):
    _fields_ = [("account_id", ctypes.c_char * 256),
                ("balance", ctypes.c_int64),
                ("updated_at", ctypes.c_int64)]


class Txn(ctypes.Structure):
    _fields_ = [("txn_id", ctypes.c_char * 64),
                ("sender_id", ctypes.c_char * 256),
                ("receiver_id", ctypes.c_char * 256),
                ("amount", ctypes.c_int64),
                ("timestamp", ctypes.c_int64),
                ("status", ctypes.c_int8),
                ("error_reason", ctypes.c_char * 256)]


class State(ctypes.Structure):
    _fields_ = [("num_accounts", ctypes.c_uint32),
                ("num_transactions", ctypes.c_uint32),
                ("total_debits", ctypes.c_int64),
                ("total_credits", ctypes.c_int64),
                ("is_balanced", ctypes.c_int8),
                ("has_anomalies", ctypes.c_int8),
                ("anomaly_details", ctypes.c_char * 512)]


TEST_HOOKS = r"""
#include "ledger.c"
/* Test-only hooks. Compiled into the throwaway test build, never the product. */
void test_corrupt_balance(const char *id, long long delta) {
    pthread_mutex_lock(&g_ledger.lock);
    int i = find_account(id);
    if (i >= 0) g_ledger.accounts[i].balance += delta;
    pthread_mutex_unlock(&g_ledger.lock);
}
unsigned long test_sizeof_account(void) { return sizeof(ledger_account_t); }
unsigned long test_sizeof_txn(void)     { return sizeof(ledger_transaction_t); }
unsigned long test_sizeof_state(void)   { return sizeof(ledger_state_t); }
"""


class Ledger:
    """Thin ctypes binding used only by the tests."""

    def __init__(self, so_path):
        self.lib = ctypes.CDLL(str(so_path))
        L = self.lib
        for fn in ("ledger_init", "ledger_destroy"):
            getattr(L, fn).argtypes = []
        L.ledger_init.restype = ctypes.c_int
        L.ledger_destroy.restype = None
        L.ledger_create_account.argtypes = [ctypes.c_char_p, ctypes.c_int64, ctypes.c_char_p]
        L.ledger_transfer.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int64,
                                      ctypes.c_char_p, ctypes.c_char_p]
        L.ledger_get_balance.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int64), ctypes.c_char_p]
        L.ledger_verify_state.argtypes = [ctypes.POINTER(State), ctypes.c_char_p]
        L.ledger_get_transaction.argtypes = [ctypes.c_char_p, ctypes.POINTER(Txn), ctypes.c_char_p]
        L.ledger_get_stats.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.POINTER(ctypes.c_int64), ctypes.c_char_p]
        for fn in ("ledger_create_account", "ledger_transfer", "ledger_get_balance",
                   "ledger_verify_state", "ledger_get_transaction", "ledger_get_stats"):
            getattr(L, fn).restype = ctypes.c_int

    @staticmethod
    def _err():
        return ctypes.create_string_buffer(512)  # fresh per call: safe across threads

    def reset(self):
        self.lib.ledger_destroy()
        assert self.lib.ledger_init() == OK

    def create(self, account_id, cents):
        return self.lib.ledger_create_account(account_id.encode(), cents, self._err())

    def transfer(self, sender, receiver, cents, txn_id):
        return self.lib.ledger_transfer(sender.encode(), receiver.encode(), cents,
                                        txn_id.encode(), self._err())

    def balance(self, account_id):
        out = ctypes.c_int64()
        st = self.lib.ledger_get_balance(account_id.encode(), ctypes.byref(out), self._err())
        assert st == OK, f"balance({account_id}) -> {st}"
        return out.value

    def total(self):
        n, t, v = ctypes.c_uint32(), ctypes.c_uint32(), ctypes.c_int64()
        assert self.lib.ledger_get_stats(ctypes.byref(n), ctypes.byref(t), ctypes.byref(v), self._err()) == OK
        return n.value, t.value, v.value

    def verify(self):
        s = State()
        assert self.lib.ledger_verify_state(ctypes.byref(s), self._err()) == OK
        return s

    def get_txn(self, txn_id):
        t = Txn()
        st = self.lib.ledger_get_transaction(txn_id.encode(), ctypes.byref(t), self._err())
        return st, t


def _build(tmp_path_factory, name, extra_flags=(), source=None):
    gcc = shutil.which("gcc") or shutil.which("cc")
    if not gcc:
        pytest.skip("no C compiler available")
    out_dir = tmp_path_factory.mktemp(name)
    src = LEDGER_DIR / "ledger.c"
    if source is not None:
        src = out_dir / "build.c"
        src.write_text(source)
    so = out_dir / f"{name}.so"
    cmd = [gcc, "-O2", "-fPIC", "-pthread", "-shared", "-Wall", "-Wextra",
           f"-I{LEDGER_DIR}", *extra_flags, str(src), "-o", str(so)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return so


@pytest.fixture(scope="module")
def default_so(tmp_path_factory):
    return _build(tmp_path_factory, "ledger_default")


@pytest.fixture(scope="module")
def hooks_so(tmp_path_factory):
    return _build(tmp_path_factory, "ledger_hooks", source=TEST_HOOKS)


@pytest.fixture(scope="module")
def small_so(tmp_path_factory):
    return _build(tmp_path_factory, "ledger_small", source=TEST_HOOKS,
                  extra_flags=("-DLEDGER_MAX_TRANSACTIONS=5", "-DLEDGER_MAX_ACCOUNTS=3"))


@pytest.fixture
def ledger(default_so):
    lg = Ledger(default_so)
    lg.reset()
    yield lg
    lg.lib.ledger_destroy()


@pytest.fixture
def hooks(hooks_so):
    lg = Ledger(hooks_so)
    lg.reset()
    lg.lib.test_corrupt_balance.argtypes = [ctypes.c_char_p, ctypes.c_longlong]
    yield lg
    lg.lib.ledger_destroy()


@pytest.fixture
def small(small_so):
    lg = Ledger(small_so)
    lg.reset()
    yield lg
    lg.lib.ledger_destroy()


def run_parallel(n_threads, fn):
    """Run fn(i) on n_threads threads released at the same instant."""
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads
    errors = []

    def worker(i):
        try:
            barrier.wait()
            results[i] = fn(i)
        except Exception as exc:  # surface thread failures in the test
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    return results


# --------------------------------------------------------------------------
# Binding sanity: the test structs must match the C structs byte for byte.
# --------------------------------------------------------------------------
def test_ctypes_structs_match_c_layout(hooks):
    assert hooks.lib.test_sizeof_account() == ctypes.sizeof(Account)
    assert hooks.lib.test_sizeof_txn() == ctypes.sizeof(Txn)
    assert hooks.lib.test_sizeof_state() == ctypes.sizeof(State)


# --------------------------------------------------------------------------
# Atomicity
# --------------------------------------------------------------------------
def test_transfer_moves_money_and_records_once(ledger):
    ledger.create("alice", 500_000)
    ledger.create("bob", 500_000)
    assert ledger.transfer("alice", "bob", 5_000, "txn_1") == OK
    assert ledger.balance("alice") == 495_000
    assert ledger.balance("bob") == 505_000
    st, rec = ledger.get_txn("txn_1")
    assert st == OK and rec.amount == 5_000 and rec.status == 1
    assert rec.sender_id == b"alice" and rec.receiver_id == b"bob"
    assert ledger.total()[1] == 1


@pytest.mark.parametrize("sender,receiver,amount,expected", [
    ("alice", "bob", 1_000_001, INSUFFICIENT),   # overdraft
    ("ghost", "bob", 100, INVALID_ACCOUNT),      # unknown sender
    ("alice", "ghost", 100, INVALID_ACCOUNT),    # unknown receiver
    ("alice", "alice", 100, INVALID_ACCOUNT),    # self-transfer
    ("alice", "bob", 0, INVALID_AMOUNT),         # zero
    ("alice", "bob", -5, INVALID_AMOUNT),        # negative
])
def test_rejected_transfer_changes_nothing_and_leaves_no_record(ledger, sender, receiver, amount, expected):
    ledger.create("alice", 1_000_000)
    ledger.create("bob", 1_000_000)
    before = (ledger.balance("alice"), ledger.balance("bob"), ledger.total())

    assert ledger.transfer(sender, receiver, amount, "txn_bad") == expected

    assert (ledger.balance("alice"), ledger.balance("bob"), ledger.total()) == before
    # A rejected attempt must not consume the id: a record exists iff money moved.
    assert ledger.get_txn("txn_bad")[0] == INVALID_ACCOUNT  # "not found"
    assert ledger.transfer("alice", "bob", 100, "txn_bad") == OK


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------
def test_replay_reports_duplicate_and_moves_nothing(ledger):
    ledger.create("alice", 500_000)
    ledger.create("bob", 500_000)
    assert ledger.transfer("alice", "bob", 5_000, "txn_100") == OK
    for _ in range(5):
        assert ledger.transfer("alice", "bob", 5_000, "txn_100") == DUPLICATE
    assert ledger.balance("alice") == 495_000
    assert ledger.balance("bob") == 505_000
    assert ledger.total()[1] == 1


def test_parallel_same_txn_id_takes_effect_exactly_once(ledger):
    ledger.create("alice", 1_000_000)
    ledger.create("bob", 0)
    results = run_parallel(64, lambda i: ledger.transfer("alice", "bob", 1_000, "txn_same"))
    assert results.count(OK) == 1
    assert results.count(DUPLICATE) == 63
    assert ledger.balance("alice") == 999_000
    assert ledger.balance("bob") == 1_000
    assert ledger.total()[1] == 1


# --------------------------------------------------------------------------
# Concurrency and conservation
# --------------------------------------------------------------------------
def test_parallel_drain_never_overdraws(ledger):
    ledger.create("alice", 10_000)   # exactly enough for 100 transfers of 100
    ledger.create("bob", 0)
    results = run_parallel(300, lambda i: ledger.transfer("alice", "bob", 100, f"drain_{i}"))
    assert results.count(OK) == 100
    assert results.count(INSUFFICIENT) == 200
    assert ledger.balance("alice") == 0
    assert ledger.balance("bob") == 10_000
    state = ledger.verify()
    assert not state.has_anomalies, state.anomaly_details
    assert ledger.total()[1] == 100


def test_random_parallel_transfers_conserve_total(ledger):
    names = [f"acct{i}" for i in range(8)]
    for n in names:
        ledger.create(n, 1_000_000)
    seeded = 8 * 1_000_000

    def worker(i):
        rng = random.Random(i)
        ok = 0
        for k in range(400):
            s, r = rng.sample(names, 2)
            if ledger.transfer(s, r, rng.randint(1, 50_000), f"t{i}_{k}") == OK:
                ok += 1
        return ok

    ok_total = sum(run_parallel(16, worker))
    assert ok_total > 0
    assert sum(ledger.balance(n) for n in names) == seeded
    assert all(ledger.balance(n) >= 0 for n in names)
    assert ledger.total()[1] == ok_total          # one record per successful transfer
    state = ledger.verify()
    assert not state.has_anomalies, state.anomaly_details
    assert state.is_balanced == 1


# --------------------------------------------------------------------------
# Verification must be able to fail
# --------------------------------------------------------------------------
def test_verify_is_clean_on_healthy_ledger(ledger):
    ledger.create("alice", 500_000)
    ledger.create("bob", 500_000)
    ledger.transfer("alice", "bob", 1_234, "txn_1")
    state = ledger.verify()
    assert state.has_anomalies == 0 and state.is_balanced == 1


def test_verify_detects_money_created_from_nothing(hooks):
    hooks.create("alice", 500_000)
    hooks.create("bob", 500_000)
    hooks.transfer("alice", "bob", 1_000, "txn_1")
    hooks.lib.test_corrupt_balance(b"bob", 777)     # 7.77 appears out of thin air
    state = hooks.verify()
    assert state.has_anomalies == 1
    assert state.is_balanced == 0
    assert b"Conservation violated" in state.anomaly_details


def test_verify_detects_money_destroyed_and_negative_balance(hooks):
    hooks.create("alice", 500)
    hooks.create("bob", 500)
    hooks.lib.test_corrupt_balance(b"alice", -900)   # alice goes to -400
    state = hooks.verify()
    assert state.has_anomalies == 1 and state.is_balanced == 0
    details = state.anomaly_details
    assert b"Negative balance on account alice" in details
    assert b"Conservation violated" in details       # both findings reported


# --------------------------------------------------------------------------
# Input limits: reject, never truncate
# --------------------------------------------------------------------------
def test_txn_id_length_limit(ledger):
    ledger.create("alice", 500_000)
    ledger.create("bob", 500_000)
    assert ledger.transfer("alice", "bob", 100, "x" * 64) == INVALID_AMOUNT   # >= 64 rejected
    assert ledger.transfer("alice", "bob", 100, "x" * 63) == OK               # 63 is the max
    assert ledger.transfer("alice", "bob", 100, "") == INVALID_AMOUNT


def test_account_id_length_limit(ledger):
    assert ledger.create("a" * 256, 100) == INVALID_ACCOUNT   # would have been truncated
    assert ledger.create("a" * 255, 100) == OK
    assert ledger.create("a" * 255, 100) == DB_ERROR          # duplicate detected at full length
    assert ledger.create("", 100) == INVALID_ACCOUNT
    ledger.create("bob", 100)
    assert ledger.transfer("a" * 256, "bob", 10, "t1") == INVALID_ACCOUNT


def test_initial_balance_limits(ledger):
    assert ledger.create("neg", -1) == INVALID_AMOUNT
    assert ledger.create("huge", MAX_INITIAL_BALANCE + 1) == INVALID_AMOUNT
    assert ledger.create("max", MAX_INITIAL_BALANCE) == OK
    assert ledger.create("huge2", 2**62) == INVALID_AMOUNT
    assert ledger.total()[0] == 1


def test_largest_allowed_balances_do_not_overflow_sums(ledger):
    for i in range(5):
        assert ledger.create(f"whale{i}", MAX_INITIAL_BALANCE) == OK
    assert ledger.total()[2] == 5 * MAX_INITIAL_BALANCE
    assert not ledger.verify().has_anomalies


# --------------------------------------------------------------------------
# Capacity: behaviour at the table limits (FACTORY.md Known limitation 2)
# --------------------------------------------------------------------------
def test_full_transaction_table_changes_nothing(small):
    small.create("alice", 1_000_000)
    small.create("bob", 1_000_000)
    for i in range(5):                                   # LEDGER_MAX_TRANSACTIONS == 5
        assert small.transfer("alice", "bob", 100, f"t{i}") == OK
    before = (small.balance("alice"), small.balance("bob"), small.total())

    assert small.transfer("alice", "bob", 100, "t_overflow") == DB_ERROR

    assert (small.balance("alice"), small.balance("bob"), small.total()) == before
    assert small.get_txn("t_overflow")[0] == INVALID_ACCOUNT          # no record, no money moved
    assert small.transfer("alice", "bob", 100, "t0") == DUPLICATE     # replays still answered
    state = small.verify()
    assert not state.has_anomalies, state.anomaly_details


def test_full_account_table_rejects_cleanly(small):
    for name in ("a", "b", "c"):                         # LEDGER_MAX_ACCOUNTS == 3
        assert small.create(name, 100) == OK
    assert small.create("d", 100) == DB_ERROR
    assert small.total()[0] == 3
    assert small.total()[2] == 300                       # the rejected account seeded nothing
    assert not small.verify().has_anomalies
