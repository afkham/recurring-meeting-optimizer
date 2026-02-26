#!/usr/bin/env python3
# Copyright 2026 Afkham Azeez
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
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
import logging.handlers
import os
import stat
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import auth
import calendar_service
import canceller

LOG_FILE = 'optimizer.log'
# Rotate at 10 MB, keep 5 backups.
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


def configure_logging() -> None:
    fmt = '%(asctime)s %(levelname)-8s %(name)s: %(message)s'
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    file_log_error: OSError | None = None

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding='utf-8',
        )
        handlers.append(file_handler)
    except OSError as exc:
        # Log file is inaccessible (disk full, permissions, missing dir, etc.).
        # Configure stdout-only so the program can still run and the warning
        # is visible rather than producing a bare Python traceback.
        file_log_error = exc

    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)

    if file_log_error:
        logging.getLogger(__name__).warning(
            "Could not open log file '%s': %s — logging to stdout only.",
            LOG_FILE, file_log_error,
        )
        return

    # Restrict log file to owner-read/write.
    try:
        os.chmod(LOG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _safe_summary(event: dict) -> str:
    """Return a sanitised event summary safe to write to logs."""
    raw = event.get('summary', 'Untitled')
    # Truncate to 80 chars and use repr() to neutralise any embedded newlines
    # or control characters that could enable log injection.
    return repr(raw[:80])


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

        try:
            today = datetime.datetime.now(ZoneInfo(tz_string)).date()
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown timezone '%s' from Calendar API; falling back to UTC.", tz_string
            )
            today = datetime.datetime.now(ZoneInfo('UTC')).date()
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
                        "Error processing event %s — skipping and continuing.",
                        _safe_summary(event),
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
