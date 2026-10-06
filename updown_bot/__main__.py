"""Command line entry point: ``python -m updown_bot``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import Optional

from dotenv import load_dotenv

from .app import Bot
from .config import ConfigError, load_config
from .tui import LOG_DATEFMT, LOG_FORMAT, Dashboard, display


def _ask_overrides() -> dict[str, str]:
    """Let the user confirm the settings that matter most before starting."""
    import questionary

    def positive(text: str) -> bool | str:
        try:
            return float(text) > 0 or "must be positive"
        except ValueError:
            return "must be a number"

    answers = questionary.form(
        live=questionary.confirm("Place real orders?", default=False),
        stake=questionary.text(
            "Stake per entry (USDC):", default=os.getenv("STAKE_USDC", "5"), validate=positive
        ),
        limit=questionary.text(
            "Daily loss limit (USDC):",
            default=os.getenv("MAX_DAILY_LOSS_USDC", "10"),
            validate=positive,
        ),
    ).ask()
    if not answers:
        raise SystemExit("aborted")
    return {
        "DRY_RUN": "false" if answers["live"] else "true",
        "STAKE_USDC": answers["stake"],
        "MAX_DAILY_LOSS_USDC": answers["limit"],
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="updown_bot", description="Trade Polymarket's 5-minute Up/Down markets."
    )
    parser.add_argument("--env-file", default=".env", help="file to read settings from")
    parser.add_argument("--no-tui", action="store_true", help="plain log output")
    parser.add_argument(
        "--interactive", action="store_true", help="confirm mode and stake before starting"
    )
    args = parser.parse_args(argv)

    # Variables already set in the environment win over the file.
    load_dotenv(args.env_file)
    if args.interactive:
        os.environ.update(_ask_overrides())
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logger = logging.getLogger("updown_bot")
    logger.setLevel(logging.INFO)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(cfg.data_dir / "bot.log")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATEFMT))
    logger.addHandler(file_handler)

    dash = Dashboard()
    bot = Bot(cfg, dash)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: bot.stop())

    with display(dash, logger, enabled=not args.no_tui):
        bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
