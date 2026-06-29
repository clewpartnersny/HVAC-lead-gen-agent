"""Entry point: `python main.py` runs one batch of the lead-gen pipeline."""

from __future__ import annotations

import logging
import sys

from src.pipeline import run


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers.
    for noisy in ("urllib3", "google", "httpx", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> int:
    _setup_logging()
    try:
        run()
    except KeyboardInterrupt:
        logging.warning("Interrupted.")
        return 130
    except Exception:  # noqa: BLE001
        logging.exception("Run failed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
