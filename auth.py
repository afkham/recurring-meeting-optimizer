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

import os
import logging

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/documents.readonly',
    'https://www.googleapis.com/auth/drive.readonly',
]

CREDENTIALS_PATH = 'credentials.json'
TOKEN_PATH = 'token.json'


def get_credentials() -> Credentials:
    """Load OAuth2 credentials, refreshing or re-authorizing as needed."""
    if not os.path.exists(CREDENTIALS_PATH):
        raise FileNotFoundError(
            f"'{CREDENTIALS_PATH}' not found. "
            "Please download your OAuth2 client credentials from the Google Cloud Console "
            "(APIs & Services > Credentials > Create Credentials > OAuth client ID > Desktop app) "
            f"and save it as '{CREDENTIALS_PATH}' in the project root."
        )

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

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

    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        logger.info("Browser authentication completed.")

    with open(TOKEN_PATH, 'w') as f:
        f.write(creds.to_json())
    logger.info("Credentials saved to '%s'.", TOKEN_PATH)

    return creds


def build_services(creds: Credentials):
    """Build and return (calendar_service, docs_service, drive_service)."""
    calendar_svc = build('calendar', 'v3', credentials=creds)
    docs_svc = build('docs', 'v1', credentials=creds)
    drive_svc = build('drive', 'v3', credentials=creds)
    return calendar_svc, docs_svc, drive_svc
