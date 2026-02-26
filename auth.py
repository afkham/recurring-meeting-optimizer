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

import logging
import os
import stat
import tempfile

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

# Suppress google_auth_oauthlib.flow from emitting OAuth state/code values at
# DEBUG level, which would expose them in log files.
logging.getLogger('google_auth_oauthlib').setLevel(logging.WARNING)
logging.getLogger('google.auth.transport').setLevel(logging.WARNING)
logging.getLogger('googleapiclient.discovery').setLevel(logging.WARNING)
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/documents.readonly',
    # drive.readonly is needed to resolve attachment file metadata.
    'https://www.googleapis.com/auth/drive.readonly',
]

CREDENTIALS_PATH = 'credentials.json'
TOKEN_PATH = 'token.json'

# Maximum seconds to wait for any single Google API call.
_API_TIMEOUT_SECONDS = 30


def _restrict_file_permissions(path: str) -> None:
    """Set file permissions to owner-read/write only (0o600)."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        logger.warning("Could not restrict permissions on '%s': %s", path, exc)


def _validate_token_scopes(creds: Credentials) -> bool:
    """Return True if the cached token covers all required scopes."""
    if not creds.scopes:
        return True  # Cannot determine; assume valid (older token format).
    return set(SCOPES).issubset(set(creds.scopes))


def _save_token(creds: Credentials) -> None:
    """Write credentials to TOKEN_PATH atomically and restrict permissions."""
    token_dir = os.path.dirname(os.path.abspath(TOKEN_PATH))
    tmp_fd, tmp_path = tempfile.mkstemp(dir=token_dir, suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            f.write(creds.to_json())
        _restrict_file_permissions(tmp_path)
        os.replace(tmp_path, TOKEN_PATH)  # atomic on POSIX
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    _restrict_file_permissions(TOKEN_PATH)
    logger.info("Credentials saved to '%s'.", TOKEN_PATH)


def get_credentials() -> Credentials:
    """Load OAuth2 credentials, refreshing or re-authorizing as needed."""
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"'{CREDENTIALS_PATH}' not found. "
            "Please download your OAuth2 client credentials from the Google Cloud Console "
            "(APIs & Services > Credentials > Create Credentials > OAuth client ID > Desktop app) "
            f"and save it as '{CREDENTIALS_PATH}' in the project root."
        )

    # Restrict credentials.json to owner-read/write.
    _restrict_file_permissions(CREDENTIALS_PATH)

    creds = None
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
            if not _validate_token_scopes(creds):
                logger.warning(
                    "Cached token is missing required scopes — re-authenticating."
                )
                creds = None
        except (ValueError, KeyError) as exc:
            # token.json is corrupt (e.g. truncated write).  Delete it and
            # trigger a fresh browser flow rather than crashing the run.
            logger.warning(
                "Token file '%s' is unreadable (%s) — re-authenticating.",
                TOKEN_PATH, exc,
            )
            try:
                os.unlink(TOKEN_PATH)
            except OSError:
                pass
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            logger.info("Refreshing expired access token.")
            creds.refresh(Request())
        except RefreshError:
            logger.warning(
                "Refresh token is invalid or revoked. Re-authenticating via browser."
            )
            creds = None
        except TransportError as exc:
            # Network was unreachable during token refresh.  Re-raise so the
            # caller's fatal-error handler can surface a clear message.
            logger.error("Network error during token refresh: %s", exc)
            raise

    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        logger.info("Browser authentication completed.")

    _save_token(creds)
    return creds


def build_services(creds: Credentials):
    """Build and return (calendar_service, docs_service, drive_service).

    Each service uses an AuthorizedHttp transport with a timeout so that
    hung API calls do not block indefinitely.
    """
    def _authorized_http() -> AuthorizedHttp:
        return AuthorizedHttp(creds, http=httplib2.Http(timeout=_API_TIMEOUT_SECONDS))

    calendar_svc = build('calendar', 'v3', http=_authorized_http())
    docs_svc     = build('docs',     'v1', http=_authorized_http())
    drive_svc    = build('drive',    'v3', http=_authorized_http())
    return calendar_svc, docs_svc, drive_svc
