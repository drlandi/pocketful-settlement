"""API-level safety tests: the FastAPI service over the real C ledger.

Run from the repo root:  python -m pytest tests/ -v

Each test gets a freshly compiled ledger and a fresh service (accounts alice,
bob, carol seeded at $5000.00 each), so tests cannot affect one another and a
stale ledger.so can never be what is tested.

This file checks what an agent can observe over HTTP: status codes, response
codes, and balances. The thread-level guarantees (atomicity under contention,
conservation, table limits) are tested directly against the C code in
tests/test_ledger_core.py.
"""
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

ROOT = Path(__file__).resolve().parent.parent
API_DIR = ROOT / "api"
LEDGER_DIR = ROOT / "ledger"
sys.path.insert(0, str(API_DIR))

import ledger_wrapper as lw  # noqa: E402
import main as service  # noqa: E402

INT64_MAX = 2**63 - 1


@pytest.fixture(scope="module")
def fresh_so(tmp_path_factory):
    gcc = shutil.which("gcc") or shutil.which("cc")
    if not gcc:
        pytest.skip("no C compiler available")
    so = tmp_path_factory.mktemp("api_ledger") / "ledger.so"
    res = subprocess.run(
        [gcc, "-O2", "-fPIC", "-pthread", "-shared", "-Wall", "-Wextra",
         str(LEDGER_DIR / "ledger.c"), "-o", str(so)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    return so


@pytest.fixture
def client(fresh_so, monkeypatch):
    monkeypatch.setattr(lw, "_wrapper_instance", lw.LedgerWrapper(str(fresh_so)))
    service._known_accounts.clear()
    with TestClient(service.app) as c:     # runs startup (seeding) and shutdown
        yield c
    lw._wrapper_instance = None


def transfer(client, sender, receiver, amount, txn_id, route="/transfer"):
    return client.post(route, json={"sender_id": sender, "receiver_id": receiver,
                                    "amount_dollars": amount, "txn_id": txn_id})


def balance(client, account_id):
    return client.get(f"/account/{account_id}").json()["balance_cents"]


def txn_count(client):
    return client.get("/verify").json()["num_transactions"]


# --------------------------------------------------------------------------
# Happy path and idempotent replay
# --------------------------------------------------------------------------
def test_health_reads_the_ledger(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"


def test_transfer_success_moves_exactly_the_requested_amount(client):
    r = transfer(client, "alice", "bob", 50.0, "txn_001")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success" and body["transferred_amount"] == 50.0
    assert balance(client, "alice") == 495_000
    assert balance(client, "bob") == 505_000
    assert txn_count(client) == 1


@pytest.mark.parametrize("route", ["/transfer", "/execute"])
def test_replay_is_200_duplicate_and_moves_nothing(client, route):
    assert transfer(client, "alice", "bob", 50.0, "txn_001", route).status_code == 200
    for _ in range(3):
        r = transfer(client, "alice", "bob", 50.0, "txn_001", route)
        assert r.status_code == 200
        assert r.json()["status"] == "duplicate"
        assert r.json()["code"] == "LEDGER_DUPLICATE_TXN"
    assert balance(client, "alice") == 495_000
    assert txn_count(client) == 1


@pytest.mark.parametrize("sender,receiver,amount", [
    ("alice", "bob", 75.0),       # same ids, different amount
    ("alice", "carol", 50.0),     # different receiver
    ("carol", "bob", 50.0),       # different sender
])
def test_reusing_a_txn_id_for_a_different_transfer_is_a_conflict(client, sender, receiver, amount):
    assert transfer(client, "alice", "bob", 50.0, "txn_001").status_code == 200
    snapshot = [balance(client, a) for a in ("alice", "bob", "carol")]

    r = transfer(client, sender, receiver, amount, "txn_001")

    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "TXN_ID_CONFLICT" and body["retryable"] is False
    assert body["recorded"] == {"sender_id": "alice", "receiver_id": "bob", "amount_dollars": 50.0}
    assert [balance(client, a) for a in ("alice", "bob", "carol")] == snapshot
    assert txn_count(client) == 1


# --------------------------------------------------------------------------
# Rejections: permanent, machine-readable, and they change nothing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sender,receiver,amount,http,code", [
    ("alice", "bob", 999999.0, 409, "LEDGER_INSUFFICIENT_BALANCE"),
    ("nobody", "bob", 10.0, 404, "LEDGER_INVALID_ACCOUNT"),
    ("alice", "nobody", 10.0, 404, "LEDGER_INVALID_ACCOUNT"),
    ("alice", "alice", 10.0, 404, "LEDGER_INVALID_ACCOUNT"),   # self-transfer
    ("alice", "bob", 0, 400, "LEDGER_INVALID_AMOUNT"),
    ("alice", "bob", -5, 400, "LEDGER_INVALID_AMOUNT"),
])
def test_rejections_use_the_documented_status_and_code(client, sender, receiver, amount, http, code):
    r = transfer(client, sender, receiver, amount, "txn_bad")
    assert r.status_code == http, r.text
    assert r.json()["code"] == code
    assert balance(client, "alice") == 500_000 and balance(client, "bob") == 500_000
    assert txn_count(client) == 0
    # a rejected attempt must not use up the id
    assert client.get("/transaction/txn_bad").status_code == 404
    assert transfer(client, "alice", "bob", 1.0, "txn_bad").status_code == 200


# --------------------------------------------------------------------------
# Amount integrity: never round, never wrap
# --------------------------------------------------------------------------
@pytest.mark.parametrize("amount", [0.285, 1.005, 0.001, 0.125, 10.999, "0.285", 1e-7])
def test_sub_cent_amounts_are_rejected_not_rounded(client, amount):
    r = transfer(client, "alice", "bob", amount, "txn_cents")
    assert r.status_code == 400
    assert r.json()["code"] == "LEDGER_INVALID_AMOUNT"
    assert balance(client, "alice") == 500_000 and txn_count(client) == 0


@pytest.mark.parametrize("amount", [
    184467440737095552.0,        # wraps to 4096 cents if passed to C unchecked
    92233720368547758.08,        # one cent above int64 max
    1e20, 1e30, "1e400",
])
def test_out_of_range_amounts_are_rejected_not_wrapped(client, amount):
    r = transfer(client, "alice", "bob", amount, "txn_big")
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "LEDGER_INVALID_AMOUNT"
    assert balance(client, "alice") == 500_000        # no money moved, certainly not $40.96
    assert balance(client, "bob") == 500_000
    assert txn_count(client) == 0


def test_decimal_strings_and_exact_cents_are_accepted(client):
    assert transfer(client, "alice", "bob", "12.34", "t1").status_code == 200
    assert transfer(client, "alice", "bob", 0.29, "t2").status_code == 200   # 0.29 -> 29 cents exactly
    assert transfer(client, "alice", "bob", 1, "t3").status_code == 200
    assert balance(client, "alice") == 500_000 - 1234 - 29 - 100
    assert balance(client, "bob") == 500_000 + 1234 + 29 + 100


def test_create_account_validates_the_initial_balance(client):
    ok = client.post("/account", json={"account_id": "dave", "initial_balance_dollars": "10.50"})
    assert ok.status_code == 201 and ok.json()["balance_cents"] == 1050
    for bad in (0.285, -1, 1e30):
        r = client.post("/account", json={"account_id": "erin", "initial_balance_dollars": bad})
        assert r.status_code == 400 and r.json()["code"] == "LEDGER_INVALID_AMOUNT", bad
    assert client.get("/account/erin").status_code == 404


# --------------------------------------------------------------------------
# Audit endpoints the Reconciler relies on
# --------------------------------------------------------------------------
def test_verify_and_ledger_reflect_real_conservation(client):
    transfer(client, "alice", "bob", 50.0, "txn_001")
    v = client.get("/verify").json()
    assert v["status"] == "verified" and v["is_balanced"] is True and v["has_anomalies"] is False
    assert v["num_transactions"] == 1 and v["num_accounts"] == 3
    overview = client.get("/ledger").json()
    assert overview["stats"]["total_value_dollars"] == 15000.0     # conserved
    assert len(overview["accounts"]) == 3


def test_transaction_lookup(client):
    transfer(client, "alice", "bob", 50.0, "txn_001")
    r = client.get("/transaction/txn_001").json()
    assert r["sender_id"] == "alice" and r["receiver_id"] == "bob"
    assert r["amount_cents"] == 5000 and r["status"] == "executed"
    assert client.get("/transaction/nope").status_code == 404


# --------------------------------------------------------------------------
# dollars_to_cents unit tests
# --------------------------------------------------------------------------
@pytest.mark.parametrize("value,cents", [
    (0, 0), (1, 100), ("50.00", 5000), (Decimal("12.34"), 1234), (5000.0, 500_000),
    (0.29, 29), (0.1, 10), ("1e2", 10_000), ("92233720368547758.07", INT64_MAX),
    (-5, -500), ("0.00", 0),
])
def test_dollars_to_cents_exact(value, cents):
    assert lw.dollars_to_cents(value) == cents


@pytest.mark.parametrize("value", [
    0.285, 1.005, 0.1 + 0.2, "0.001", "92233720368547758.08", "1e18", 1e30,
    float("inf"), float("nan"), "abc", "", None, True, [], "1e999999999",
])
def test_dollars_to_cents_rejects(value):
    with pytest.raises(lw.AmountError):
        lw.dollars_to_cents(value)
