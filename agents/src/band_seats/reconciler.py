"""Reconciler seat: read-only. Audits verification output and account state."""

from band_seats import service
from band_seats.seat import run_seat

TOOLS = [service.VERIFY, service.LEDGER, service.ACCOUNT, service.TRANSACTION]


def main() -> None:
    run_seat("reconciler", TOOLS)


if __name__ == "__main__":
    main()
