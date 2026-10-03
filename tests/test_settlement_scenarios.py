"""Safety assertions plus explicitly named current-behavior characterizations."""
from concurrent.futures import ThreadPoolExecutor
import ctypes
from decimal import Decimal

import pytest

from conftest import assert_state
from ledger_wrapper import LedgerStatus as Status

INITIAL = {"source": 10000, "destination": 0, "alternate": 0}


def submit(client, identifier="operation", amount=10, sender="source", receiver="destination"):
    return client.post("/execute", json={
        "sender_id": sender, "receiver_id": receiver,
        "amount_dollars": amount, "txn_id": identifier,
    })


def overlap(ledger, operations):
    lib = ledger.ledger_lib
    lib.probe_arm.argtypes = [ctypes.c_uint]
    lib.probe_arm.restype = None
    lib.probe_arrived.restype = ctypes.c_uint
    lib.probe_timed_out.restype = ctypes.c_int
    lib.probe_arm(len(operations))

    def call(operation):
        receiver, cents, identifier = operation
        error = ctypes.create_string_buffer(512)
        # CDLL releases the GIL; no Python lock serializes these calls.
        status = lib.ledger_transfer(b"source", receiver.encode(), cents,
                                     identifier.encode(), error)
        return status, error.value.decode()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        results = list(pool.map(call, operations))
    assert lib.probe_timed_out() == 0, "C calls did not rendezvous before mutex acquisition"
    assert lib.probe_arrived() == len(operations), "Overlap inside ledger not established"
    return [status for status, _ in results]


@pytest.mark.parametrize("round_number", range(5))
def test_simultaneous_drain_is_atomic(ledger_factory, round_number):
    ledger = ledger_factory("overlap")
    operations = [("destination", 8000, f"drain-a-{round_number}"),
                  ("alternate", 8000, f"drain-b-{round_number}")]
    statuses = overlap(ledger, operations)
    assert sorted(statuses) == [Status.OK, Status.INSUFFICIENT_BALANCE]
    winner = statuses.index(Status.OK)
    balances = {"source": 2000, "destination": 0, "alternate": 0}
    balances[operations[winner][0]] = 8000
    assert_state(ledger, balances, 1)
    for index, (receiver, _, identifier) in enumerate(operations):
        status, _, record = ledger.get_transaction(identifier)
        if index == winner:
            assert status == Status.OK
            assert (record["sender_id"], record["receiver_id"], record["amount_cents"],
                    record["status"]) == ("source", receiver, 8000, "executed")
        else:
            assert status == Status.INVALID_ACCOUNT and record is None


@pytest.mark.parametrize("round_number", range(5))
def test_concurrent_same_identifier_has_one_effect(ledger_factory, round_number):
    ledger = ledger_factory("overlap")
    identifier = f"storm-{round_number}"
    statuses = overlap(ledger, [("destination", 1000, identifier)] * 8)
    assert statuses.count(Status.OK) == 1
    assert statuses.count(Status.DUPLICATE_TXN) == 7
    assert_state(ledger, {"source": 9000, "destination": 1000, "alternate": 0}, 1)
    status, _, record = ledger.get_transaction(identifier)
    assert status == Status.OK
    assert (record["sender_id"], record["receiver_id"], record["amount_cents"],
            record["status"]) == ("source", "destination", 1000, "executed")


def test_matching_replay_moves_nothing(client, ledger):
    first = submit(client)
    assert first.status_code == 200 and first.json()["status"] == "success"
    record = client.get("/transaction/operation").json()
    replay = submit(client)
    assert replay.status_code == 200 and replay.json()["status"] == "duplicate"
    assert replay.json()["transferred_amount"] == 10
    assert client.get("/transaction/operation").json() == record
    assert_state(ledger, {"source": 9000, "destination": 1000, "alternate": 0}, 1)


@pytest.mark.parametrize("changes", [{"amount": 20}, {"receiver": "alternate"},
                                      {"sender": "destination", "receiver": "source"}])
def test_characterize_identifier_conflict_as_duplicate_without_new_effect(client, ledger, changes):
    assert submit(client).status_code == 200
    original = client.get("/transaction/operation").json()
    conflict = submit(client, **changes)
    assert conflict.status_code == 200
    assert conflict.json()["status"] == "duplicate"
    assert conflict.json()["transferred_amount"] == 10
    assert client.get("/transaction/operation").json() == original
    assert_state(ledger, {"source": 9000, "destination": 1000, "alternate": 0}, 1)
    # This characterizes identifier-only deduplication, not content validation.


@pytest.mark.parametrize("sender,receiver,amount,http_status,code", [
    ("source", "destination", 101, 409, "LEDGER_INSUFFICIENT_BALANCE"),
    ("missing", "destination", 1, 404, "LEDGER_INVALID_ACCOUNT"),
    ("source", "missing", 1, 404, "LEDGER_INVALID_ACCOUNT"),
    ("source", "source", 1, 404, "LEDGER_INVALID_ACCOUNT"),
])
def test_rejection_preserves_balances_and_count(client, ledger, sender, receiver, amount, http_status, code):
    # Nonempty history ensures rejection cannot erase existing records either.
    assert submit(client, identifier="prior", amount=1).status_code == 200
    prior = client.get("/transaction/prior").json()
    response = submit(client, identifier="rejected", sender=sender, receiver=receiver, amount=amount)
    assert response.status_code == http_status
    assert response.json()["code"] == code
    assert client.get("/transaction/rejected").status_code == 404
    assert client.get("/transaction/prior").json() == prior
    assert_state(ledger, {"source": 9900, "destination": 100, "alternate": 0}, 1)


@pytest.mark.parametrize("amount,cents", [(0.01, 1), (0.004, 0), (0.005, 0),
                                          (0.006, 1), (0.125, 12), (0.1 + 0.2, 30)])
def test_characterize_precision_rounding_and_exact_effects(client, ledger, amount, cents):
    response = submit(client, amount=amount)
    if cents == 0:
        assert response.status_code == 400
        assert response.json()["code"] == "LEDGER_INVALID_AMOUNT"
        assert client.get("/transaction/operation").status_code == 404
        assert_state(ledger, INITIAL, 0)
    else:
        assert response.status_code == 200
        record = client.get("/transaction/operation").json()
        assert record["amount_cents"] == cents
        assert record["amount_dollars"] == cents / 100
        assert record["status"] == "executed"
        assert response.json()["sender_balance"] == (10000 - cents) / 100
        assert response.json()["receiver_balance"] == cents / 100
        assert_state(ledger, {"source": 10000 - cents, "destination": cents, "alternate": 0}, 1)


@pytest.mark.parametrize("amount,cents", [(0.01, 1), (0.006, 1), (0.125, 12)])
def test_success_response_reports_actual_applied_quantity(client, ledger, amount, cents):
    """Safety: response must describe applied money regardless of rounding policy."""
    response = submit(client, amount=amount)
    assert response.status_code == 200
    record = client.get("/transaction/operation").json()
    assert record["amount_cents"] == cents
    assert_state(ledger, {"source": 10000 - cents, "destination": cents, "alternate": 0}, 1)
    assert Decimal(str(response.json()["transferred_amount"])) == Decimal(cents) / 100, (
        "Successful response quantity differs from recorded cents and actual balance deltas"
    )
