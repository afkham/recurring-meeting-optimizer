#!/usr/bin/env python3
"""
recurring-meeting-optimizer

Checks today's recurring Google Calendar meetings and cancels any occurrence
whose associated Google Doc has no agenda topics for today.

Usage:
    python main.py             # normal run
    python main.py --dry-run   # log what would be cancelled without making changes
"""

import argparse
import datetime
import logging
import sys
from zoneinfo import ZoneInfo

import auth
import calendar_service
import canceller


def configure_logging() -> None:
    fmt = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler('optimizer.log'),
        ],
    )


def main() -> None:
    configure_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description='Cancel recurring meetings with no agenda topics.')
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Log what would be cancelled without actually cancelling anything.',
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("=== DRY RUN MODE — no meetings will be cancelled ===")

    logger.info("recurring-meeting-optimizer starting.")

    try:
        creds = auth.get_credentials()
        calendar_svc, docs_svc, _ = auth.build_services(creds)

        tz_string = calendar_service.get_user_timezone(calendar_svc)
        logger.info("User timezone: %s", tz_string)

        today = datetime.datetime.now(ZoneInfo(tz_string)).date()
        logger.info("Checking meetings for: %s", today)

        events = calendar_service.get_todays_recurring_events(calendar_svc, today, tz_string)

        if not events:
            logger.info("No recurring meetings today — nothing to do.")
        else:
            for event in events:
                try:
                    canceller.process_event(
                        event, calendar_svc, docs_svc, today, dry_run=args.dry_run
                    )
                except Exception:
                    logger.exception(
                        "Error processing event '%s' — skipping and continuing.",
                        event.get('summary', 'Untitled'),
                    )

    except FileNotFoundError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception:
        logger.exception("Fatal error — see traceback above.")
        sys.exit(1)

    logger.info("recurring-meeting-optimizer finished.")


if __name__ == '__main__':
    main()
