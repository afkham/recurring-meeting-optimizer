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
  TC-01  Recurring  + doc WITH topics            → KEEP   (topics found)
  TC-02  Recurring  + doc NO topics              → CANCEL (no topics)
  TC-03  Non-recurring + doc, no topics          → KEEP   (not recurring, ignored)
  TC-04  Recurring  + NO doc attached            → KEEP   (no doc, skipped)
  TC-05  Recurring  + doc has today heading but NO Topics section
                                                 → CANCEL (state machine finds no topics)
  TC-06  Recurring  + doc has only past-date entries (no today heading)
                                                 → CANCEL (date heading not found)
  TC-07  Recurring  + two docs: one no-topics, one with-topics
                                                 → KEEP   (any-doc-with-topics logic)
  TC-08  Recurring  + two docs: both no topics   → CANCEL (all docs checked, none qualify)
  TC-09  All-day recurring + doc no topics       → KEEP   (all-day events filtered out)
  TC-10  Idempotency: already-cancelled occurrence is not re-processed on 2nd optimizer pass

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

def _create_doc(
    docs_svc,
    title: str,
    today: datetime.date,
    include_topics: bool = True,
    include_topics_section: bool = True,
    heading_date: datetime.date | None = None,
) -> str:
    """
    Create a Google Doc using the meeting template structure.

    Args:
        include_topics:         Whether to list items under the Topics section.
        include_topics_section: Whether to include a Topics section at all.
                                Set False to omit it entirely (TC-05).
        heading_date:           Date to use in the heading. Defaults to today.
                                Set to a past date to simulate old entries (TC-06).

    Returns the document ID.
    """
    date_for_heading = heading_date if heading_date is not None else today
    date_prefix = f"{date_for_heading.strftime('%b')} {date_for_heading.day}, {date_for_heading.year}"
    heading_text = f"{date_prefix} | {title}"

    if include_topics_section:
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
    else:
        # No Topics section at all (TC-05).
        body_text = (
            f"{heading_text}\n"
            "Attendees: Test User\n"
            "\n"
            "Notes\n"
            "\n"
            "Action items\n"
        )

    doc = docs_svc.documents().create(body={'title': f'[TEST] {title}'}).execute()
    doc_id = doc['documentId']

    heading_end = 1 + len(heading_text) + 1  # +1 offset, +1 for trailing \n

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
    doc_ids: list[str] | None,
    is_recurring: bool,
    today: datetime.date,
    tz: str,
    all_day: bool = False,
) -> str:
    """
    Create a calendar event for today.

    Args:
        doc_ids:    List of Google Doc IDs to attach. None or empty = no attachment.
        all_day:    If True, creates an all-day event (uses 'date' not 'dateTime').

    Returns the base event ID.
    """
    tz_info = ZoneInfo(tz)

    body: dict = {'summary': summary}

    if all_day:
        body['start'] = {'date': today.isoformat()}
        body['end']   = {'date': (today + datetime.timedelta(days=1)).isoformat()}
    else:
        start = datetime.datetime(today.year, today.month, today.day, 22, 0, 0, tzinfo=tz_info)
        end   = datetime.datetime(today.year, today.month, today.day, 22, 30, 0, tzinfo=tz_info)
        body['start'] = {'dateTime': start.isoformat(), 'timeZone': tz}
        body['end']   = {'dateTime': end.isoformat(),   'timeZone': tz}

    if is_recurring:
        body['recurrence'] = ['RRULE:FREQ=WEEKLY;COUNT=4']

    if doc_ids:
        body['attachments'] = [
            {
                'fileUrl': f'https://docs.google.com/document/d/{did}/edit',
                'mimeType': 'application/vnd.google-apps.document',
                'title': f'[TEST] {summary}',
            }
            for did in doc_ids
        ]

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
    """Return the status of today's occurrence of an event."""
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
        if isinstance(did, list):
            for i, d in enumerate(did):
                try:
                    drive_svc.files().delete(fileId=d).execute()
                    print(f'  Deleted doc    {tc}[{i}]: {d}')
                except Exception as exc:
                    print(f'  Could not delete doc {tc}[{i}] ({d}): {exc}')
        else:
            try:
                drive_svc.files().delete(fileId=did).execute()
                print(f'  Deleted doc    {tc}: {did}')
            except Exception as exc:
                print(f'  Could not delete doc {tc} ({did}): {exc}')


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_tests() -> int:
    logging.basicConfig(level=logging.ERROR)

    print('=' * 60)
    print('  recurring-meeting-optimizer — integration tests')
    print('=' * 60)

    print('\nAuthenticating (browser may open on first run)...')
    creds = _get_test_credentials()
    cal_svc, docs_svc_obj, drive_svc = _build_services(creds)

    tz    = cal_module.get_user_timezone(cal_svc)
    today = datetime.datetime.now(ZoneInfo(tz)).date()
    yesterday = today - datetime.timedelta(days=7)  # use last week for "past date" tests
    print(f'Timezone : {tz}')
    print(f'Today    : {today}')

    base_event_ids: dict[str, str] = {}
    doc_ids: dict = {}  # values may be str or list[str]

    # ------------------------------------------------------------------ Setup
    print('\n--- Creating test events and docs ---')
    try:
        # TC-01: Recurring + doc WITH topics → KEEP
        doc_ids['tc01'] = _create_doc(docs_svc_obj, '[TEST] Recurring with topics', today, include_topics=True)
        base_event_ids['tc01'] = _create_event(cal_svc, '[TEST] Recurring with topics',
            [doc_ids['tc01']], is_recurring=True, today=today, tz=tz)
        print(f'  TC-01  recurring, doc+topics             event={base_event_ids["tc01"]}')

        # TC-02: Recurring + doc WITHOUT topics → CANCEL
        doc_ids['tc02'] = _create_doc(docs_svc_obj, '[TEST] Recurring no topics', today, include_topics=False)
        base_event_ids['tc02'] = _create_event(cal_svc, '[TEST] Recurring no topics',
            [doc_ids['tc02']], is_recurring=True, today=today, tz=tz)
        print(f'  TC-02  recurring, doc+no topics          event={base_event_ids["tc02"]}')

        # TC-03: Non-recurring + doc WITHOUT topics → KEEP
        doc_ids['tc03'] = _create_doc(docs_svc_obj, '[TEST] One-off no topics', today, include_topics=False)
        base_event_ids['tc03'] = _create_event(cal_svc, '[TEST] One-off no topics',
            [doc_ids['tc03']], is_recurring=False, today=today, tz=tz)
        print(f'  TC-03  non-recurring, doc+no topics      event={base_event_ids["tc03"]}')

        # TC-04: Recurring + NO doc → KEEP
        base_event_ids['tc04'] = _create_event(cal_svc, '[TEST] Recurring no doc',
            None, is_recurring=True, today=today, tz=tz)
        print(f'  TC-04  recurring, no doc                 event={base_event_ids["tc04"]}')

        # TC-05: Recurring + doc with today's heading but NO Topics section → CANCEL
        doc_ids['tc05'] = _create_doc(docs_svc_obj, '[TEST] No topics section', today,
            include_topics_section=False)
        base_event_ids['tc05'] = _create_event(cal_svc, '[TEST] No topics section',
            [doc_ids['tc05']], is_recurring=True, today=today, tz=tz)
        print(f'  TC-05  recurring, doc+no topics section  event={base_event_ids["tc05"]}')

        # TC-06: Recurring + doc with only past-date entries (no today heading) → CANCEL
        doc_ids['tc06'] = _create_doc(docs_svc_obj, '[TEST] Past date only', today,
            include_topics=True, heading_date=yesterday)
        base_event_ids['tc06'] = _create_event(cal_svc, '[TEST] Past date only',
            [doc_ids['tc06']], is_recurring=True, today=today, tz=tz)
        print(f'  TC-06  recurring, doc+past date only     event={base_event_ids["tc06"]}')

        # TC-07: Recurring + two docs (first no topics, second has topics) → KEEP
        doc_ids['tc07'] = [
            _create_doc(docs_svc_obj, '[TEST] Two docs A no topics', today, include_topics=False),
            _create_doc(docs_svc_obj, '[TEST] Two docs B with topics', today, include_topics=True),
        ]
        base_event_ids['tc07'] = _create_event(cal_svc, '[TEST] Two docs one with topics',
            doc_ids['tc07'], is_recurring=True, today=today, tz=tz)
        print(f'  TC-07  recurring, 2 docs (no+yes topics) event={base_event_ids["tc07"]}')

        # TC-08: Recurring + two docs (both no topics) → CANCEL
        doc_ids['tc08'] = [
            _create_doc(docs_svc_obj, '[TEST] Two docs both no topics A', today, include_topics=False),
            _create_doc(docs_svc_obj, '[TEST] Two docs both no topics B', today, include_topics=False),
        ]
        base_event_ids['tc08'] = _create_event(cal_svc, '[TEST] Two docs both no topics',
            doc_ids['tc08'], is_recurring=True, today=today, tz=tz)
        print(f'  TC-08  recurring, 2 docs (both no topics) event={base_event_ids["tc08"]}')

        # TC-09: All-day recurring + doc no topics → KEEP (all-day filtered out)
        doc_ids['tc09'] = _create_doc(docs_svc_obj, '[TEST] All-day no topics', today, include_topics=False)
        base_event_ids['tc09'] = _create_event(cal_svc, '[TEST] All-day recurring',
            [doc_ids['tc09']], is_recurring=True, today=today, tz=tz, all_day=True)
        print(f'  TC-09  all-day recurring, doc+no topics  event={base_event_ids["tc09"]}')

        # TC-10: Idempotency — recurring no topics; verify not re-processed after cancellation
        doc_ids['tc10'] = _create_doc(docs_svc_obj, '[TEST] Idempotency check', today, include_topics=False)
        base_event_ids['tc10'] = _create_event(cal_svc, '[TEST] Idempotency check',
            [doc_ids['tc10']], is_recurring=True, today=today, tz=tz)
        print(f'  TC-10  recurring, no topics (idempotency) event={base_event_ids["tc10"]}')

    except Exception as exc:
        print(f'\nSetup failed: {exc}')
        _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)
        return 1

    print('\nWaiting 5 s for Calendar API propagation...')
    time.sleep(5)

    # ------------------------------------------------------------------ Run optimizer (pass 1)
    print('\n--- Running optimizer (pass 1) ---')
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

    time.sleep(3)

    # ------------------------------------------------------------------ Run optimizer (pass 2, idempotency)
    print('\n--- Running optimizer (pass 2 — idempotency check) ---')
    try:
        all_recurring_pass2 = cal_module.get_todays_recurring_events(cal_svc, today, tz)
        test_recurring_pass2 = [e for e in all_recurring_pass2 if e.get('summary', '').startswith('[TEST]')]
        print(f'  Test recurring events found in pass 2: {len(test_recurring_pass2)}')

        tc10_found_in_pass2 = any(
            e.get('recurringEventId') == base_event_ids['tc10']
            for e in test_recurring_pass2
        )
        tc09_found_in_pass2 = any(
            e.get('recurringEventId') == base_event_ids['tc09']
            for e in test_recurring_pass2
        )
    except Exception as exc:
        print(f'\nPass 2 run failed: {exc}')
        _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)
        return 1

    # ------------------------------------------------------------------ Verify
    print('\n--- Verification ---')
    print(f'  {"Test case":<50} {"Expected":<12} {"Got":<22} {"Result"}')
    print('  ' + '-' * 90)

    test_cases = [
        # label,                                              tc key, is_recurring, expect_cancelled
        ('TC-01  Recurring  + doc WITH topics           ', 'tc01', True,  False),
        ('TC-02  Recurring  + doc NO topics             ', 'tc02', True,  True),
        ('TC-03  Non-recurring + doc, no topics         ', 'tc03', False, False),
        ('TC-04  Recurring  + NO doc attached           ', 'tc04', True,  False),
        ('TC-05  Recurring  + doc, no Topics section    ', 'tc05', True,  True),
        ('TC-06  Recurring  + doc, past date only       ', 'tc06', True,  True),
        ('TC-07  Recurring  + 2 docs (no+yes topics)    ', 'tc07', True,  False),
        ('TC-08  Recurring  + 2 docs (both no topics)   ', 'tc08', True,  True),
        ('TC-09  All-day recurring + doc, no topics     ', 'tc09', True,  False),
    ]

    results = []
    for label, tc, is_recurring, expect_cancelled in test_cases:
        status             = _get_instance_status(cal_svc, base_event_ids[tc], today, tz, is_recurring)
        actually_cancelled = (status == 'cancelled')
        passed             = (actually_cancelled == expect_cancelled)
        results.append(passed)

        expected_str = 'CANCEL' if expect_cancelled else 'KEEP'
        got_str      = 'CANCELLED' if actually_cancelled else f'KEPT ({status})'
        result_str   = 'PASS' if passed else 'FAIL'
        print(f'  {label:<50} {expected_str:<12} {got_str:<22} {result_str}')

    # TC-10 idempotency: already-cancelled tc10 must NOT appear in pass-2 query
    tc10_passed = not tc10_found_in_pass2
    results.append(tc10_passed)
    tc10_result = 'PASS' if tc10_passed else 'FAIL'
    tc10_got    = 'not in pass-2 query' if tc10_passed else 'STILL in pass-2 query'
    print(f'  {"TC-10  Idempotency (cancelled not re-queried) ":<50} {"KEEP":<12} {tc10_got:<22} {tc10_result}')

    # TC-09 extra check: all-day event must NOT appear in either pass
    tc09_in_pass1 = any(
        e.get('recurringEventId') == base_event_ids['tc09']
        for e in test_recurring
    )
    if tc09_in_pass1:
        print('  TC-09  WARN: all-day event appeared in pass-1 query (unexpected)')

    all_passed = all(results)
    print()
    print(f'  {"ALL TESTS PASSED ✓" if all_passed else "SOME TESTS FAILED ✗"}  '
          f'({sum(results)}/{len(results)})')

    # ------------------------------------------------------------------ Cleanup
    _cleanup(cal_svc, drive_svc, base_event_ids, doc_ids)

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(run_tests())
