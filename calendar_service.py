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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httplib2
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Hard cap on the number of pagination pages fetched per run.  A typical
# calendar has far fewer than 100 events in a single day; this prevents an
# unbounded loop if the API unexpectedly keeps returning page tokens.
_MAX_PAGES = 100

# Number of times to retry a failed API call before giving up.  The
# googleapiclient library handles 429 / 5xx / transport errors automatically
# when num_retries > 0, with exponential back-off.
_MAX_API_RETRIES = 5


def _safe_summary(event: dict) -> str:
    """Return a sanitised event summary safe to write to logs."""
    raw = event.get('summary', 'Untitled')
    return repr(raw[:80])


def get_user_timezone(calendar_svc) -> str:
    """Return the user's calendar timezone string (e.g. 'America/New_York')."""
    setting = calendar_svc.settings().get(setting='timezone').execute(
        num_retries=_MAX_API_RETRIES
    )
    tz = setting.get('value')
    if not tz or not isinstance(tz, str):
        raise ValueError(
            f"Calendar API returned unexpected timezone setting: {setting!r}"
        )
    return tz


def get_todays_recurring_events(calendar_svc, today: datetime.date, tz: str) -> list:
    """Return all recurring event instances scheduled for today in the user's timezone."""
    try:
        tz_info = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        logger.warning(
            "Unknown timezone '%s' from Calendar API; falling back to UTC.", tz
        )
        tz_info = ZoneInfo('UTC')

    time_min = datetime.datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=tz_info).isoformat()
    time_max = datetime.datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=tz_info).isoformat()

    events = []
    page_token = None
    pages_fetched = 0

    while True:
        if pages_fetched >= _MAX_PAGES:
            logger.warning(
                "Pagination limit (%d pages) reached while fetching today's events — "
                "some events may have been skipped.",
                _MAX_PAGES,
            )
            break

        try:
            response = calendar_svc.events().list(
                calendarId='primary',
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,  # Expands recurring series into individual instances
                orderBy='startTime',
                pageToken=page_token,
            ).execute(num_retries=_MAX_API_RETRIES)
        except (HttpError, httplib2.HttpLib2Error, OSError) as exc:
            logger.error(
                "Failed to fetch events page %d: %s — returning %d event(s) collected so far.",
                pages_fetched + 1, exc, len(events),
            )
            break  # Return partial results rather than aborting the entire run.

        pages_fetched += 1

        for event in response.get('items', []):
            # Only recurring instances (they have recurringEventId), that are not cancelled,
            # and have a specific time (skip all-day events which only have 'date', not 'dateTime').
            if (
                'recurringEventId' in event
                and event.get('status') != 'cancelled'
                and 'dateTime' in event.get('start', {})
            ):
                events.append(event)

        page_token = response.get('nextPageToken')
        if not page_token:
            break

    logger.info("Found %d recurring event(s) for %s.", len(events), today)
    return events


def cancel_event_occurrence(calendar_svc, event: dict, note: str) -> None:
    """Prepend cancellation note to event description, then delete the occurrence for all attendees."""
    event_id = event['id']
    summary  = _safe_summary(event)

    existing_desc = event.get('description', '') or ''

    # Idempotency guard: if a previous run patched the description but failed
    # before completing the delete, skip re-patching and go straight to delete.
    if not existing_desc.startswith(note):
        new_desc = f"{note}\n\n{existing_desc}".strip()
        calendar_svc.events().patch(
            calendarId='primary',
            eventId=event_id,
            body={'description': new_desc},
        ).execute(num_retries=_MAX_API_RETRIES)

    try:
        calendar_svc.events().delete(
            calendarId='primary',
            eventId=event_id,
            sendUpdates='all',
        ).execute(num_retries=_MAX_API_RETRIES)
    except Exception:
        # The description has been updated but the occurrence was NOT deleted.
        # Log at CRITICAL so an operator can manually cancel the event.
        logger.critical(
            "Cancellation of %s (id=%s) is INCOMPLETE: description was updated "
            "but the occurrence was NOT deleted. Please cancel it manually in Google Calendar.",
            summary, event_id,
        )
        raise

    logger.info("Cancelled occurrence of %s (id=%s).", summary, event_id)
