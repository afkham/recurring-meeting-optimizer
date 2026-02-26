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

import datetime
import logging

from googleapiclient.errors import HttpError

import calendar_service
import docs_service

logger = logging.getLogger(__name__)

CANCELLATION_NOTE = "Meeting canceled since there are no topics to be discussed today"


def should_cancel_event(event: dict, docs_svc, today: datetime.date) -> tuple:
    """
    Determine whether a recurring event occurrence should be cancelled.

    Returns (should_cancel: bool, reason: str) where reason is one of:
      'no_doc'      - no Google Doc attachment found (do NOT cancel)
      'has_topics'  - at least one doc has topics for today (do NOT cancel)
      'no_topics'   - doc(s) found but none have topics for today (DO cancel)
      'doc_error'   - all docs had access errors (do NOT cancel, to be safe)
    """
    summary = event.get('summary', 'Untitled')
    doc_ids = docs_service.extract_doc_ids_from_event(event)

    if not doc_ids:
        logger.warning(
            "No Google Doc attached to '%s' — skipping (will not cancel).", summary
        )
        return False, 'no_doc'

    any_doc_read = False

    for doc_id in doc_ids:
        try:
            content = docs_service.fetch_doc_content(docs_svc, doc_id)
            any_doc_read = True
        except HttpError as exc:
            logger.error(
                "Could not read doc '%s' for event '%s': %s — skipping this doc.",
                doc_id, summary, exc,
            )
            continue

        if docs_service.has_topics_for_today(content, today):
            logger.info("'%s': topics found in doc '%s' — meeting is required.", summary, doc_id)
            return False, 'has_topics'

    if not any_doc_read:
        logger.warning(
            "All docs for '%s' had access errors — skipping (will not cancel).", summary
        )
        return False, 'doc_error'

    logger.info("'%s': no topics found in any attached doc — will cancel.", summary)
    return True, 'no_topics'


def process_event(event: dict, calendar_svc, docs_svc, today: datetime.date, dry_run: bool = False) -> None:
    """Evaluate one recurring event and cancel it if no topics are found."""
    summary = event.get('summary', 'Untitled')
    cancel, reason = should_cancel_event(event, docs_svc, today)

    if cancel:
        if dry_run:
            logger.info("[DRY RUN] Would cancel '%s' (reason: %s).", summary, reason)
        else:
            calendar_service.cancel_event_occurrence(calendar_svc, event, CANCELLATION_NOTE)
    else:
        logger.info("Keeping '%s' (reason: %s).", summary, reason)
