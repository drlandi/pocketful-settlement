"""Planner seat: read-only. Validates a request against live state."""

from band_seats import service
from band_seats.seat import run_seat

TOOLS = [service.HEALTH, service.ACCOUNT, service.TRANSACTION]


def main() -> None:
    run_seat("planner", TOOLS)


if __name__ == "__main__":
    main()
