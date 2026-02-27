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
Unit tests for recurring-meeting-optimizer.

No Google credentials required — all Google API calls are mocked.

Test groups:
  UT-01..04  has_topics_for_today() — pure doc-parsing logic
  UT-05..07  canceller.should_cancel_event() — error handling paths
  UT-08..09  calendar_service.cancel_event_occurrence() — partial-failure & idempotency
  UT-10..11  docs_service.extract_doc_ids_from_event() — URL validation
  UT-12      auth.get_credentials() — corrupt token recovery

Usage:
  python test_unit.py
  python -m unittest test_unit -v
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call, mock_open

import httplib2
from googleapiclient.errors import HttpError

import auth
import calendar_service
import canceller
import docs_service
import main
from canceller import CANCELLATION_NOTE


# ---------------------------------------------------------------------------
# Helpers for building fake Docs API body.content lists
# ---------------------------------------------------------------------------

def _para(text: str, style: str = 'NORMAL_TEXT') -> dict:
    """Return a paragraph content element with the given text and style."""
    return {
        'paragraph': {
            'paragraphStyle': {'namedStyleType': style},
            'elements': [{'textRun': {'content': text + '\n'}}],
        }
    }


def _heading(text: str, level: int = 2) -> dict:
    return _para(text, f'HEADING_{level}')


SECTION_BREAK = {'sectionBreak': {}}

TODAY = datetime.date(2026, 2, 26)
DATE_HEADING = 'Feb 26, 2026 | Team Sync'


# ---------------------------------------------------------------------------
# UT-01 .. UT-04  has_topics_for_today
# ---------------------------------------------------------------------------

class TestHasTopicsForToday(unittest.TestCase):

    def _call(self, content):
        return docs_service.has_topics_for_today(content, TODAY)

    def test_ut01_whitespace_only_lines_not_counted_as_topics(self):
        """UT-01: Topics section with only blank lines → False."""
        content = [
            SECTION_BREAK,
            _heading(DATE_HEADING),
            _para('Attendees: Alice'),
            _para('Topic:'),
            _para(''),       # blank line — must NOT count as a topic
            _para('   '),    # whitespace only — must NOT count
            _para('Notes'),
            _para('Action items'),
        ]
        self.assertFalse(self._call(content))

    def test_ut02_topic_content_before_next_date_heading_is_counted(self):
        """UT-02: Topic items appearing before the next date heading are found → True."""
        content = [
            SECTION_BREAK,
            _heading(DATE_HEADING),
            _para('Topic:'),
            _para('- Discuss Q2 plan'),           # real topic item
            _heading('Feb 19, 2026 | Team Sync'),  # previous week's entry
            _para('Topic:'),
            _para('- Old topic'),
        ]
        self.assertTrue(self._call(content))

    def test_ut03_two_consecutive_date_headings_no_content_between(self):
        """UT-03: Today's heading immediately followed by a new date heading → False."""
        content = [
            SECTION_BREAK,
            _heading(DATE_HEADING),
            _heading('Feb 19, 2026 | Team Sync'),  # next entry with no content for today
            _para('Topic:'),
            _para('- Old topic'),
        ]
        self.assertFalse(self._call(content))

    def test_ut13_date_heading_found_no_topics_section_cancels(self):
        """UT-13: Today's date heading present but NO Topics section at all → False (cancel)."""
        content = [
            SECTION_BREAK,
            _heading(DATE_HEADING),        # today's date heading IS present
            _para('Attendees: Alice, Bob'),
            _para(''),
            _para('Notes'),                # no Topics section anywhere
            _para(''),
            _para('Action items'),
        ]
        with self.assertLogs('docs_service', level='INFO') as log_ctx:
            result = self._call(content)

        self.assertFalse(result, "Meeting should be cancelled when Topics section is absent")
        self.assertTrue(
            any('no Topics section' in msg for msg in log_ctx.output),
            "Expected an INFO log mentioning 'no Topics section'",
        )

    def test_ut04_topic_variants_all_recognised(self):
        """UT-04: 'Topic', 'Topic:', 'Topics', 'Topics:', 'TOPICS:' all match."""
        variants = ['Topic', 'Topic:', 'Topics', 'Topics:', 'TOPICS:']
        for variant in variants:
            with self.subTest(variant=variant):
                content = [
                    SECTION_BREAK,
                    _heading(DATE_HEADING),
                    _para(variant),
                    _para('- An agenda item'),
                    _para('Notes'),
                ]
                self.assertTrue(self._call(content), f"'{variant}' should be recognised as Topics header")


# ---------------------------------------------------------------------------
# UT-05 .. UT-07  canceller.should_cancel_event
# ---------------------------------------------------------------------------

def _make_event(doc_ids: list[str]) -> dict:
    """Build a minimal fake calendar event with the given doc attachments."""
    return {
        'id': 'evt_test_001',
        'summary': 'Unit Test Meeting',
        'attachments': [
            {
                'mimeType': 'application/vnd.google-apps.document',
                'fileUrl': f'https://docs.google.com/document/d/{did}/edit',
                'title': 'Meeting Notes',
            }
            for did in doc_ids
        ],
    }


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    return HttpError(resp=resp, content=b'error')


class TestShouldCancelEvent(unittest.TestCase):

    def test_ut05_doc_permission_denied_returns_doc_error(self):
        """UT-05: 403 HttpError on doc fetch → (False, 'doc_error') — safe side."""
        mock_docs = MagicMock()
        mock_docs.documents.return_value.get.return_value.execute.side_effect = _http_error(403)

        event = _make_event(['doc_abc123'])
        should_cancel, reason = canceller.should_cancel_event(event, mock_docs, TODAY)

        self.assertFalse(should_cancel)
        self.assertEqual(reason, 'doc_error')

    def test_ut06_network_error_on_doc_fetch_returns_doc_error(self):
        """UT-06: httplib2.HttpLib2Error (network drop) → (False, 'doc_error')."""
        mock_docs = MagicMock()
        mock_docs.documents.return_value.get.return_value.execute.side_effect = (
            httplib2.HttpLib2Error("connection reset")
        )

        event = _make_event(['doc_abc123'])
        should_cancel, reason = canceller.should_cancel_event(event, mock_docs, TODAY)

        self.assertFalse(should_cancel)
        self.assertEqual(reason, 'doc_error')

    def test_ut07_one_bad_doc_one_good_doc_returns_has_topics(self):
        """UT-07: First doc → 403; second doc → has topics → (False, 'has_topics')."""
        good_content = [
            SECTION_BREAK,
            _heading(DATE_HEADING),
            _para('Topic:'),
            _para('- Real topic'),
            _para('Notes'),
        ]

        call_count = 0

        def execute_side_effect(num_retries=0):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _http_error(403)
            # Second call: return a valid doc structure.
            return {
                'body': {'content': good_content}
            }

        mock_docs = MagicMock()
        mock_docs.documents.return_value.get.return_value.execute.side_effect = execute_side_effect

        event = _make_event(['doc_bad', 'doc_good'])
        should_cancel, reason = canceller.should_cancel_event(event, mock_docs, TODAY)

        self.assertFalse(should_cancel)
        self.assertEqual(reason, 'has_topics')


# ---------------------------------------------------------------------------
# UT-08 .. UT-09  calendar_service.cancel_event_occurrence
# ---------------------------------------------------------------------------

class TestCancelEventOccurrence(unittest.TestCase):

    def _make_cal_svc(self):
        """Return a MagicMock that behaves like a Calendar service."""
        svc = MagicMock()
        # Default: both patch and delete succeed.
        svc.events.return_value.patch.return_value.execute.return_value = {}
        svc.events.return_value.delete.return_value.execute.return_value = {}
        return svc

    def test_ut08_delete_fails_after_patch_logs_critical_and_reraises(self):
        """UT-08: DELETE raises after PATCH succeeds → CRITICAL logged, exception propagated."""
        svc = self._make_cal_svc()
        svc.events.return_value.delete.return_value.execute.side_effect = _http_error(500)

        event = {'id': 'evt001', 'summary': 'Meeting', 'description': ''}

        with self.assertLogs('calendar_service', level='CRITICAL') as log_ctx:
            with self.assertRaises(HttpError):
                calendar_service.cancel_event_occurrence(svc, event, CANCELLATION_NOTE)

        self.assertTrue(
            any('INCOMPLETE' in msg for msg in log_ctx.output),
            "Expected a CRITICAL log containing 'INCOMPLETE'",
        )

    def test_ut09_idempotency_guard_skips_patch_when_note_already_present(self):
        """UT-09: Note already in description → patch skipped, delete still called."""
        svc = self._make_cal_svc()

        event = {
            'id': 'evt002',
            'summary': 'Meeting',
            'description': CANCELLATION_NOTE + '\n\nOriginal description',
        }

        calendar_service.cancel_event_occurrence(svc, event, CANCELLATION_NOTE)

        # patch must NOT have been called.
        svc.events.return_value.patch.assert_not_called()
        # delete must have been called exactly once.
        svc.events.return_value.delete.assert_called_once()


# ---------------------------------------------------------------------------
# UT-10 .. UT-11  docs_service.extract_doc_ids_from_event
# ---------------------------------------------------------------------------

class TestExtractDocIds(unittest.TestCase):

    def _event_with_url(self, url: str) -> dict:
        return {
            'attachments': [{
                'mimeType': 'application/vnd.google-apps.document',
                'fileUrl': url,
            }]
        }

    def test_ut10_malformed_url_without_document_path_returns_empty(self):
        """UT-10: fileUrl with no /document/d/ pattern → empty list, no crash."""
        event = self._event_with_url('https://drive.google.com/file/d/abc123/view')
        result = docs_service.extract_doc_ids_from_event(event)
        self.assertEqual(result, [])

    def test_ut11_url_exceeding_max_length_is_rejected(self):
        """UT-11: fileUrl longer than 2048 chars → empty list."""
        long_url = 'https://docs.google.com/document/d/' + 'a' * 2048 + '/edit'
        event = self._event_with_url(long_url)
        result = docs_service.extract_doc_ids_from_event(event)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# UT-12  auth.get_credentials — corrupt token recovery
# ---------------------------------------------------------------------------

class TestGetCredentials(unittest.TestCase):

    def test_ut12_corrupt_token_triggers_reauth_not_crash(self):
        """UT-12: A corrupt token.json causes re-auth, not an unhandled exception."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Write a corrupt (truncated) token file.
            corrupt_token = os.path.join(tmp_dir, 'token.json')
            with open(corrupt_token, 'w') as f:
                f.write('{invalid json')

            fake_creds_json = os.path.join(tmp_dir, 'credentials.json')
            # Minimal valid-looking client secret structure.
            with open(fake_creds_json, 'w') as f:
                json.dump({
                    'installed': {
                        'client_id': 'test.apps.googleusercontent.com',
                        'client_secret': 'test_secret',
                        'redirect_uris': ['urn:ietf:wg:oauth:2.0:oob'],
                        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                        'token_uri': 'https://oauth2.googleapis.com/token',
                    }
                }, f)

            # Patch the module-level path constants and the browser flow.
            mock_new_creds = MagicMock()
            mock_new_creds.to_json.return_value = json.dumps({
                'token': 'new_token',
                'refresh_token': 'new_refresh',
                'token_uri': 'https://oauth2.googleapis.com/token',
                'client_id': 'test.apps.googleusercontent.com',
                'client_secret': 'test_secret',
                'scopes': auth.SCOPES,
            })
            mock_new_creds.scopes = auth.SCOPES

            with (
                patch.object(auth, 'TOKEN_PATH', corrupt_token),
                patch.object(auth, 'CREDENTIALS_PATH', fake_creds_json),
                patch('auth.InstalledAppFlow') as mock_flow_cls,
                patch('auth._save_token'),  # avoid actual file write after re-auth
            ):
                mock_flow_cls.from_client_secrets_file.return_value.run_local_server.return_value = (
                    mock_new_creds
                )

                # Should not raise — corrupt token is silently discarded and
                # the browser auth flow is invoked instead.
                result = auth.get_credentials()

            # Browser flow must have been triggered.
            mock_flow_cls.from_client_secrets_file.assert_called_once()
            self.assertEqual(result, mock_new_creds)


# ---------------------------------------------------------------------------
# UT-14 .. UT-16  run-once-per-day guard (main._read_last_success /
#                 _write_last_success / early-exit in main())
# ---------------------------------------------------------------------------

class TestRunOncePerDay(unittest.TestCase):

    def test_ut14_already_ran_today_exits_early(self):
        """UT-14: last_success.txt contains today → main() exits before fetching events."""
        today = datetime.date(2026, 2, 27)

        # Fake creds / services so auth doesn't touch the filesystem.
        mock_creds = MagicMock()
        mock_cal_svc = MagicMock()
        mock_docs_svc = MagicMock()
        mock_cal_svc.settings.return_value.get.return_value.execute.return_value = {
            'value': 'UTC'
        }

        with (
            patch('main._read_last_success', return_value=today),
            patch('main._write_last_success') as mock_write,
            patch('auth.get_credentials', return_value=mock_creds),
            patch('auth.build_services', return_value=(mock_cal_svc, mock_docs_svc, MagicMock())),
            patch('calendar_service.get_todays_recurring_events') as mock_fetch,
            patch('sys.argv', ['main.py']),
        ):
            with self.assertLogs('main', level='INFO') as log_ctx:
                main.main()

        # Events must NOT have been fetched.
        mock_fetch.assert_not_called()
        # Success file must NOT be overwritten.
        mock_write.assert_not_called()
        self.assertTrue(
            any('already ran successfully today' in msg for msg in log_ctx.output),
            "Expected early-exit log message",
        )

    def test_ut15_successful_run_writes_last_success(self):
        """UT-15: After a successful run, last_success.txt is written with today's date."""
        mock_creds = MagicMock()
        mock_cal_svc = MagicMock()
        mock_docs_svc = MagicMock()
        mock_cal_svc.settings.return_value.get.return_value.execute.return_value = {
            'value': 'UTC'
        }
        # No recurring events today → nothing to cancel.
        mock_cal_svc.events.return_value.list.return_value.execute.return_value = {
            'items': []
        }

        with (
            patch('main._read_last_success', return_value=None),
            patch('main._write_last_success') as mock_write,
            patch('auth.get_credentials', return_value=mock_creds),
            patch('auth.build_services', return_value=(mock_cal_svc, mock_docs_svc, MagicMock())),
            patch('sys.argv', ['main.py']),
        ):
            main.main()

        # _write_last_success must have been called with today's date.
        mock_write.assert_called_once()
        written_date = mock_write.call_args[0][0]
        self.assertIsInstance(written_date, datetime.date)

    def test_ut16_past_date_in_file_does_not_exit_early(self):
        """UT-16: last_success.txt contains yesterday → program proceeds normally."""
        yesterday = datetime.date(2026, 2, 26)

        mock_creds = MagicMock()
        mock_cal_svc = MagicMock()
        mock_docs_svc = MagicMock()
        mock_cal_svc.settings.return_value.get.return_value.execute.return_value = {
            'value': 'UTC'
        }
        mock_cal_svc.events.return_value.list.return_value.execute.return_value = {
            'items': []
        }

        with (
            patch('main._read_last_success', return_value=yesterday),
            patch('main._write_last_success') as mock_write,
            patch('auth.get_credentials', return_value=mock_creds),
            patch('auth.build_services', return_value=(mock_cal_svc, mock_docs_svc, MagicMock())),
            patch('calendar_service.get_todays_recurring_events') as mock_fetch,
            patch('sys.argv', ['main.py']),
        ):
            mock_fetch.return_value = []
            main.main()

        # Events MUST have been fetched (program did not exit early).
        mock_fetch.assert_called_once()
        # Success file MUST be written with today's (not yesterday's) date.
        mock_write.assert_called_once()
        written_date = mock_write.call_args[0][0]
        self.assertNotEqual(written_date, yesterday)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    unittest.main(verbosity=2)
