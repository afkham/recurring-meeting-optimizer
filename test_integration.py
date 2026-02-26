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
Integration tests for recurring-meeting-optimizer.

Test cases:
  TC-01  Recurring  + doc WITH topics     → KEEP   (topics found, meeting required)
  TC-02  Recurring  + doc NO topics       → CANCEL (no topics, meeting not required)
  TC-03  Non-recurring + doc, no topics   → KEEP   (not a recurring event, ignored)
  TC-04  Recurring  + NO doc attached     → KEEP   (no doc, skipped by design)

Usage:
  python test_integration.py

On first run the browser will open once to grant expanded scopes (docs + drive
write access) into test_token.json. This does not affect the main token.json.
"""

import datetime
import sys
import time
import logging
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import calendar_service as cal_module
import canceller
import docs_service

# ---------------------------------------------------------------------------
# Auth — expanded scopes so we can create/delete Docs and Drive files.
# Uses test_token.json, leaving the main app's token.json untouched.
# ---------------------------------------------------------------------------
TEST_SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/documents',
    'https://www.googleapis.com/auth/drive',
]
TEST_TOKEN_PATH = 'test_token.json'
CREDENTIALS_PATH = 'credentials.json'


def _get_test_credentials() -> Credentials:
    import os
    creds = None
    if os.path.exists(TEST_TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TEST_TOKEN_PATH, TEST_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, TEST_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TEST_TOKEN_PATH, 'w') as f:
            f.write(creds.to_json())
    return creds


def _build_services(creds):
    cal  = build('calendar', 'v3',  credentials=creds)
    docs = build('docs',     'v1',  credentials=creds)
    drv  = build('drive',    'v3',  credentials=creds)
    return cal, docs, drv


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------

def _create_doc(docs_svc, title: str, today: datetime.date, include_topics: bool) -> str:
    """
    Create a Google Doc using the meeting template structure.
    Returns the document ID.
    """
    date_prefix = f"{today.strftime('%b')} {today.day}, {today.year}"
    heading_text = f"{date_prefix} | {title}"

    if include_topics:
        body_text = (
            f"{heading_text}\n"
            "Attendees: Test User\n"
            "\n"
            "Topic:\n"
            "- Integration test topic 1\n"
            "- Integration test topic 2\n"
            "\n"
            "Notes\n"
            "\n"
            "Action items\n"
        )
    else:
        body_text = (
            f"{heading_text}\n"
            "Attendees: Test User\n"
            "\n"
            "Topic:\n"
            "\n"
            "Notes\n"
            "\n"
            "Action items\n"
        )

    # Create blank document
    doc = docs_svc.documents().create(body={'title': f'[TEST] {title}'}).execute()
    doc_id = doc['documentId']

    # Insert text then apply HEADING_2 style to the first line.
    # In the Docs API all indices are offset by 1 (sectionBreak at 0).
    heading_end = 1 + len(heading_text) + 1  # +1 offset, +1 for the trailing \n

    docs_svc.documents().batchUpdate(
        documentId=doc_id,
        body={
            'requests': [
                {
                    'insertText': {
                        'location': {'index': 1},
                        'text': body_text,
                    }
                },
                {
                    'updateParagraphStyle': {
                        'range': {'startIndex': 1, 'endIndex': heading_end},
                        'paragraphStyle': {'namedStyleType': 'HEADING_2'},
                        'fields': 'namedStyleType',
                    }
                },
            ]
        },
    ).execute()

    return doc_id


def _create_event(
    cal_svc,
    summary: str,
    doc_id: str | None,
    is_recurring: bool,
    today: datetime.date,
    tz: str,
) -> str:
    """
    Create a calendar event for today at 22:00-22:30 local time.
    Returns the base event ID.
    """
    tz_info = ZoneInfo(tz)
    start = datetime.datetime(today.year, today.month, today.day, 22, 0, 0, tzinfo=tz_info)
    end   = datetime.datetime(today.year, today.month, today.day, 22, 30, 0, tzinfo=tz_info)

    body = {
        'summary': summary,
        'start': {'dateTime': start.isoformat(), 'timeZone': tz},
        'end':   {'dateTime': end.isoformat(),   'timeZone': tz},
    }

    if is_recurring:
        body['recurrence'] = ['RRULE:FREQ=WEEKLY;COUNT=4']

    if doc_id:
        body['attachments'] = [{
            'fileUrl': f'https://docs.google.com/document/d/{doc_id}/edit',
            'mimeType': 'application/vnd.google-apps.document',
            'title': f'[TEST] {summary}',
        }]

    event = cal_svc.events().insert(
        calendarId='primary',
        body=body,
        supportsAttachments=True,
    ).execute()

    return event['id']


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def _get_instance_status(cal_svc, base_id: str, today: datetime.date, tz: str, is_recurring: bool) -> str:
    """
    Return the status of today's occurrence of an event.
    For non-recurring events returns the event's own status.
    """
    if not is_recurring:
        event = cal_svc.events().get(calendarId='primary', eventId=base_id).execute()
        return event.get('status', 'confirmed')

    tz_info = ZoneInfo(tz)
    time_min = datetime.datetime(today.year, today.month, today.day, 0,  0,  0,  tzinfo=tz_info).isoformat()
    time_max = datetime.datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=tz_info).isoformat()

    response = cal_svc.events().list(
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        showDeleted=True,
    ).execute()

    for event in response.get('items', []):
        if event.get('recurringEventId') == base_id:
            return event.get('status', 'confirmed')

    return 'not_found'


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def _cleanup(cal_svc, drive_svc, base_event_ids: dict, doc_ids: dict) -> None:
    print('\n--- Cleanup ---')
    for tc, eid in base_event_ids.items():
        try:
            cal_svc.events().delete(
                calendarId='primary',
                eventId=eid,
                sendUpdates='none',
            ).execute()
            print(f'  Deleted event  {tc}: {eid}')
        except Exception as exc:
            print(f'  Could not delete event {tc} ({eid}): {exc}')

    for tc, did in doc_ids.items():
        try:
            drive_svc.files().delete(fileId=did).execute()
            print(f'  Deleted doc    {tc}: {did}')
        except Exception as exc:
            print(f'  Could not delete doc {tc} ({did}): {exc}')


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_tests() -> int:
    # Suppress noisy library logs during tests
    logging.basicConfig(level=logging.ERROR)

    print('=' * 60)
    print('  recurring-meeting-optimizer — integration tests')
    print('=' * 60)

    print('\nAuthenticating (browser may open on first run)...')
    creds = _get_test_credentials()
    cal_svc, docs_svc_obj, drive_svc = _build_services(creds)

    tz    = cal_module.get_user_timezone(cal_svc)
    today = datetime.datetime.now(ZoneInfo(tz)).date()
    print(f'Timezone : {tz}')
    print(f'Today    : {today}')

    base_event_ids: dict[str, str] = {}
    doc_ids:        dict[str, str] = {}

    # ------------------------------------------------------------------ Setup
    print('\n--- Creating test events and docs ---')
    try:
        # TC-01: Recurring + doc WITH topics → expect KEEP
        doc_ids['tc01'] = _create_doc(
            docs_svc_obj, '[TEST] Recurring with topics', today, include_topics=True)
        base_event_ids['tc01'] = _create_event(
            cal_svc, '[TEST] Recurring with topics',
            doc_ids['tc01'], is_recurring=True, today=today, tz=tz)
        print(f'  TC-01  recurring, doc+topics   event={base_event_ids["tc01"]}')

        # TC-02: Recurring + doc WITHOUT topics → expect CANCEL
        doc_ids['tc02'] = _create_doc(
            docs_svc_obj, '[TEST] Recurring no topics', today, include_topics=False)
        base_event_ids['tc02'] = _create_event(
            cal_svc, '[TEST] Recurring no topics',
            doc_ids['tc02'], is_recurring=True, today=today, tz=tz)
        print(f'  TC-02  recurring, doc+no topics  event={base_event_ids["tc02"]}')

        # TC-03: Non-recurring + doc WITHOUT topics → expect KEEP
        doc_ids['tc03'] = _create_doc(
            docs_svc_obj, '[TEST] One-off no topics', today, include_topics=False)
        base_event_ids['tc03'] = _create_event(
            cal_svc, '[TEST] One-off no topics',
            doc_ids['tc03'], is_recurring=False, today=today, tz=tz)
        print(f'  TC-03  non-recurring, doc+no topics  event={base_event_ids["tc03"]}')

        # TC-04: Recurring + NO doc → expect KEEP
        base_event_ids['tc04'] = _create_event(
            cal_svc, '[TEST] Recurring no doc',
            None, is_recurring=True, today=today, tz=tz)
        print(f'  TC-04  recurring, no doc   event={base_event_ids["tc04"]}')

    except Exception as exc:
        print(f'\nSetup failed: {exc}')
        _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)
        return 1

    # Allow the Calendar API a moment to propagate the new events
    print('\nWaiting 5 s for Calendar API propagation...')
    time.sleep(5)

    # ------------------------------------------------------------------ Run optimizer
    print('\n--- Running optimizer on today\'s recurring events ---')
    try:
        all_recurring = cal_module.get_todays_recurring_events(cal_svc, today, tz)
        test_recurring = [e for e in all_recurring if e.get('summary', '').startswith('[TEST]')]
        print(f'  Test recurring events found: {len(test_recurring)}')

        for event in test_recurring:
            try:
                canceller.process_event(event, cal_svc, docs_svc_obj, today, dry_run=False)
            except Exception as exc:
                print(f'  Error processing {event.get("summary")}: {exc}')

    except Exception as exc:
        print(f'\nOptimizer run failed: {exc}')
        _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)
        return 1

    # Brief pause for cancellations to propagate
    time.sleep(3)

    # ------------------------------------------------------------------ Verify
    print('\n--- Verification ---')
    print(f'  {"Test case":<45} {"Expected":<12} {"Got":<12} {"Result"}')
    print('  ' + '-' * 75)

    test_cases = [
        ('TC-01  Recurring  + doc WITH topics   ', 'tc01', True,  False),
        ('TC-02  Recurring  + doc NO topics     ', 'tc02', True,  True),
        ('TC-03  Non-recurring + doc, no topics ', 'tc03', False, False),
        ('TC-04  Recurring  + NO doc attached   ', 'tc04', True,  False),
    ]

    results = []
    for label, tc, is_recurring, expect_cancelled in test_cases:
        status           = _get_instance_status(cal_svc, base_event_ids[tc], today, tz, is_recurring)
        actually_cancelled = (status == 'cancelled')
        passed           = (actually_cancelled == expect_cancelled)
        results.append(passed)

        expected_str = 'CANCEL' if expect_cancelled else 'KEEP'
        got_str      = 'CANCELLED' if actually_cancelled else f'KEPT ({status})'
        result_str   = 'PASS' if passed else 'FAIL'
        print(f'  {label:<45} {expected_str:<12} {got_str:<20} {result_str}')

    all_passed = all(results)
    print()
    print(f'  {"ALL TESTS PASSED ✓" if all_passed else "SOME TESTS FAILED ✗"}  '
          f'({sum(results)}/{len(results)})')

    # ------------------------------------------------------------------ Cleanup
    _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(run_tests())
