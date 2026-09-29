"""Executor seat: the only seat that can change state."""

from band_seats import service
from band_seats.seat import run_seat

# TRANSACTION is for recovering from a lost response: a record exists iff the transfer took effect.
TOOLS = [service.HEALTH, service.EXECUTE, service.TRANSACTION]


def main() -> None:
    run_seat("executor", TOOLS)


if __name__ == "__main__":
    main()
