"""Phase 1 main: load config, enforce paper mode, run migrations, log ready, exit."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from x_alpaca_trading_bot import db
from x_alpaca_trading_bot.config import Config, assert_paper_mode


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    _configure_logging()
    logger = logging.getLogger(__name__)

    cfg = Config.load()
    assert_paper_mode(cfg.alpaca_base_url)

    project_root = Path(__file__).resolve().parent.parent
    deploy_dir = project_root / "deploy"

    with db.connect(cfg.database_url) as conn:
        db.run_migrations(conn, deploy_dir)

    logger.info("ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
