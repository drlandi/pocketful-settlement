"""Fresh shared-object state per test; never connect to a running service."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
import ledger_wrapper as binding
import main as api


@pytest.fixture(scope="session")
def libraries(tmp_path_factory):
    build = tmp_path_factory.mktemp("ledger-build")
    outputs = {}
    for name in ("plain", "overlap"):
        output = build / f"{name}.so"
        command = ["gcc", "-fPIC", "-shared", "-pthread", str(ROOT / "ledger/ledger.c")]
        if name == "overlap":
            command += [str(ROOT / "tests/overlap_probe.c"), "-Wl,--wrap=pthread_mutex_lock"]
        subprocess.run(command + ["-o", str(output)], check=True, capture_output=True, text=True)
        outputs[name] = output
    return outputs


@pytest.fixture
def ledger_factory(libraries, tmp_path):
    wrappers = []

    def create(name="plain"):
        # A distinct file/path yields distinct C globals and a fresh static mutex.
        path = tmp_path / f"ledger-{len(wrappers)}.so"
        shutil.copyfile(libraries[name], path)
        wrapper = binding.LedgerWrapper(str(path))
        assert wrapper.init()[0] == binding.LedgerStatus.OK
        wrappers.append(wrapper)
        for account, dollars in (("source", 100), ("destination", 0), ("alternate", 0)):
            assert wrapper.create_account(account, dollars)[0] == binding.LedgerStatus.OK
        return wrapper

    yield create
    # Worker executors must finish before fixture teardown and ledger_destroy.
    for wrapper in reversed(wrappers):
        wrapper.destroy()


@pytest.fixture
def ledger(ledger_factory):
    return ledger_factory()


@pytest.fixture
def client(ledger):
    previous = api.app.dependency_overrides.copy()
    api.app.dependency_overrides[api.get_wrapper] = lambda: ledger
    # No lifespan context: fixture owns init/destroy, startup must not load the
    # default library or seed accounts. TestClient uses in-process ASGI, no sockets.
    transport = TestClient(api.app)
    try:
        yield transport
    finally:
        transport.close()
        api.app.dependency_overrides.clear()
        api.app.dependency_overrides.update(previous)


def assert_state(ledger, balances, transactions):
    actual = {}
    for account in balances:
        status, message, record = ledger.get_account(account)
        assert status == binding.LedgerStatus.OK, message
        actual[account] = record["balance_cents"]
    assert actual == balances
    assert all(value >= 0 for value in actual.values())
    status, message, stats = ledger.get_stats()
    assert status == binding.LedgerStatus.OK, message
    assert stats["num_accounts"] == len(balances)
    assert stats["num_transactions"] == transactions
    assert stats["total_value_cents"] == sum(actual.values()) == 10000
