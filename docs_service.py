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
import re

import httplib2
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_DOC_MIME_TYPE = 'application/vnd.google-apps.document'
_DOC_ID_PATTERN = re.compile(r'/document/d/([a-zA-Z0-9_-]+)')

# Sanity limits on attachment URL and doc ID length to reject obviously
# malformed or adversarially crafted values before passing them to the API.
_MAX_URL_LENGTH    = 2048
_MAX_DOC_ID_LENGTH = 128

# Number of times to retry a failed API call before giving up.
_MAX_API_RETRIES = 5

# Hard cap on the number of content elements processed per document.  A
# normal meeting-notes doc has tens to low hundreds of elements; this prevents
# a pathologically large document from consuming excessive CPU/memory.
_MAX_CONTENT_ELEMENTS = 10_000

# Matches the date prefix in a heading, e.g. "Feb 25, 2026"
_DATE_PREFIX_RE = re.compile(r'^[A-Z][a-z]{2} \d{1,2}, \d{4}')

# Known section names that signal the end of the Topics section.
# Matched case-insensitively against the full paragraph text.
_END_SECTION_NAMES = frozenset({
    'notes', 'action items', 'action item', 'next steps', 'next step',
    'attendees', 'attendees:', 'agenda', 'resources', 'follow-up', 'follow up',
})

# Map namedStyleType to a numeric level for hierarchy comparisons.
# Lower number = higher in the document hierarchy.
_HEADING_LEVELS = {
    'TITLE':       0,
    'HEADING_1':   1,
    'HEADING_2':   2,
    'HEADING_3':   3,
    'HEADING_4':   4,
    'HEADING_5':   5,
    'HEADING_6':   6,
    'SUBTITLE':    7,
    'NORMAL_TEXT': 99,
}


def extract_doc_ids_from_event(event: dict) -> list:
    """Return a list of Google Doc IDs found in the event's Drive attachments."""
    doc_ids = []
    attachments = event.get('attachments', [])
    if not isinstance(attachments, list):
        return doc_ids

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        if attachment.get('mimeType') != _DOC_MIME_TYPE:
            continue

        url = attachment.get('fileUrl', '')
        if not isinstance(url, str) or len(url) > _MAX_URL_LENGTH:
            logger.warning("Attachment has missing or oversized fileUrl — skipping.")
            continue

        match = _DOC_ID_PATTERN.search(url)
        if not match:
            continue

        doc_id = match.group(1)
        if len(doc_id) > _MAX_DOC_ID_LENGTH:
            logger.warning("Extracted doc ID exceeds maximum length — skipping.")
            continue

        doc_ids.append(doc_id)

    return doc_ids


def fetch_doc_content(docs_svc, doc_id: str) -> list:
    """Fetch a Google Doc and return its body content list."""
    doc = docs_svc.documents().get(documentId=doc_id).execute(
        num_retries=_MAX_API_RETRIES
    )

    if not isinstance(doc, dict):
        logger.error("Unexpected response type from Docs API for doc '%s'.", doc_id)
        return []

    body = doc.get('body', {})
    if not isinstance(body, dict):
        logger.error("Unexpected 'body' type in Docs API response for doc '%s'.", doc_id)
        return []

    content = body.get('content', [])
    if not isinstance(content, list):
        logger.error("Unexpected 'content' type in Docs API response for doc '%s'.", doc_id)
        return []

    return content


def build_today_date_prefix(today: datetime.date) -> str:
    """Return today's date formatted as the heading prefix, e.g. 'Feb 25, 2026'."""
    # Use .day integer directly to avoid platform-specific zero-stripping flags.
    return f"{today.strftime('%b')} {today.day}, {today.year}"


def _get_paragraph_text(paragraph: dict) -> str:
    """
    Join all readable content in a paragraph into a single stripped string.

    Handles the element types that carry display text:
      - textRun:     regular and formatted text
      - dateElement: Google Docs date smart chip (displayText)
      - richLink:    calendar/Drive smart chip (title)
      - person:      @mention (name)
    """
    parts = []
    for elem in paragraph.get('elements', []):
        if 'textRun' in elem:
            parts.append(elem['textRun'].get('content', ''))
        elif 'dateElement' in elem:
            display = elem['dateElement'].get('dateElementProperties', {}).get('displayText', '')
            parts.append(display)
        elif 'richLink' in elem:
            title = elem['richLink'].get('richLinkProperties', {}).get('title', '')
            parts.append(title)
        elif 'person' in elem:
            name = elem['person'].get('personProperties', {}).get('name', '')
            parts.append(name)
    return ''.join(parts).strip()


def _heading_level(paragraph: dict) -> int:
    """Return the numeric heading level of a paragraph (99 = normal text)."""
    style = paragraph.get('paragraphStyle', {}).get('namedStyleType', 'NORMAL_TEXT')
    return _HEADING_LEVELS.get(style, 99)


def has_topics_for_today(content: list, today: datetime.date) -> bool:
    """
    Parse the flat body.content list from the Docs API and determine whether
    today's meeting section has non-empty topics.

    Document structure expected:
        [Heading] "Feb 25, 2026 | Meeting title"
            [Sub-heading] "Topics"
                - Topic 1
                - Topic 2
        [Heading] "Feb 18, 2026 | Meeting title"
            ...

    Returns True if today's date heading is found AND a 'Topics' sub-heading
    exists beneath it with at least one non-empty content line.
    Returns False otherwise.
    """
    date_prefix = build_today_date_prefix(today)

    # 3-state machine: SEARCHING_DATE → SEARCHING_TOPICS → CHECKING_CONTENT
    STATE_SEARCHING_DATE    = 0
    STATE_SEARCHING_TOPICS  = 1
    STATE_CHECKING_CONTENT  = 2

    state = STATE_SEARCHING_DATE
    date_heading_level = None

    elements_processed = 0

    for element in content:
        elements_processed += 1
        if elements_processed > _MAX_CONTENT_ELEMENTS:
            logger.warning(
                "Document content element limit (%d) reached — stopping parse.",
                _MAX_CONTENT_ELEMENTS,
            )
            break

        if 'paragraph' not in element:
            # Skip sectionBreak, table, etc.
            continue

        para  = element['paragraph']
        text  = _get_paragraph_text(para)
        level = _heading_level(para)
        is_heading = level < 99

        if state == STATE_SEARCHING_DATE:
            if is_heading and text.startswith(date_prefix):
                date_heading_level = level
                state = STATE_SEARCHING_TOPICS
                logger.debug("Found today's date heading (level %d).", level)

        elif state == STATE_SEARCHING_TOPICS:
            if is_heading:
                # A heading at strictly higher hierarchy means we've left the entire date section.
                if level < date_heading_level:
                    logger.debug("Left today's section (higher hierarchy heading).")
                    return False
                # A heading at the same level as the date heading is only a section boundary
                # if it looks like another date entry — not if it's e.g. "Attendees".
                if level == date_heading_level and _DATE_PREFIX_RE.match(text):
                    logger.debug("Left today's section (next date heading).")
                    return False
            # Match "Topics", "Topic", "Topics:", "Topic:" case-insensitively.
            if text.lower().rstrip(':') in ('topics', 'topic'):
                state = STATE_CHECKING_CONTENT
                logger.debug("Found 'Topics' sub-heading (level %d).", level)

        elif state == STATE_CHECKING_CONTENT:
            if is_heading:
                # Strictly higher hierarchy — left the date section entirely.
                if level < date_heading_level:
                    logger.debug("Left today's section: no topic content found.")
                    return False
                # Same level as date heading and looks like a new date — left the section.
                if level == date_heading_level and _DATE_PREFIX_RE.match(text):
                    logger.debug("Left today's section (next date heading).")
                    return False
            # A known end-section name (Notes, Action items, etc.) ends the Topics section
            # regardless of whether it is a heading or normal/bold text.
            if text.lower() in _END_SECTION_NAMES:
                logger.debug("Left 'Topics' sub-section (end section encountered).")
                return False
            # Any other non-empty text is a topic item.
            if text:
                logger.debug("Found topic content.")
                return True

    # Document exhausted — report why no topics were confirmed.
    if state == STATE_SEARCHING_DATE:
        logger.debug("Today's date heading not found in document.")
    elif state == STATE_SEARCHING_TOPICS:
        # Today's date heading WAS found, but there was no Topics section beneath
        # it before the document ended.  Treat as no topics → cancel.
        logger.info(
            "Today's date heading found but no Topics section present — "
            "treating as no topics."
        )
    elif state == STATE_CHECKING_CONTENT:
        logger.debug("Topics section found but contained no content items.")
    return False
