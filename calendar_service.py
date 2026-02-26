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
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def get_user_timezone(calendar_svc) -> str:
    """Return the user's calendar timezone string (e.g. 'America/New_York')."""
    setting = calendar_svc.settings().get(setting='timezone').execute()
    return setting['value']


def get_todays_recurring_events(calendar_svc, today: datetime.date, tz: str) -> list:
    """Return all recurring event instances scheduled for today in the user's timezone."""
    tz_info = ZoneInfo(tz)
    time_min = datetime.datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=tz_info).isoformat()
    time_max = datetime.datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=tz_info).isoformat()

    events = []
    page_token = None

    while True:
        response = calendar_svc.events().list(
            calendarId='primary',
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,  # Expands recurring series into individual instances
            orderBy='startTime',
            pageToken=page_token,
        ).execute()

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
    summary = event.get('summary', 'Untitled')

    # Prepend the note to the existing description so attendees see the reason.
    existing_desc = event.get('description', '') or ''
    new_desc = f"{note}\n\n{existing_desc}".strip()
    calendar_svc.events().patch(
        calendarId='primary',
        eventId=event_id,
        body={'description': new_desc},
    ).execute()

    # Delete this specific occurrence and notify all attendees.
    calendar_svc.events().delete(
        calendarId='primary',
        eventId=event_id,
        sendUpdates='all',
    ).execute()

    logger.info("Cancelled occurrence of '%s' (id=%s).", summary, event_id)
