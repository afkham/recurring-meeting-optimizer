import datetime
import logging
import re

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_DOC_MIME_TYPE = 'application/vnd.google-apps.document'
_DOC_ID_PATTERN = re.compile(r'/document/d/([a-zA-Z0-9_-]+)')

# Map namedStyleType to a numeric level for hierarchy comparisons.
# Lower number = higher in the document hierarchy.
_HEADING_LEVELS = {
    'TITLE': 0,
    'HEADING_1': 1,
    'HEADING_2': 2,
    'HEADING_3': 3,
    'HEADING_4': 4,
    'HEADING_5': 5,
    'HEADING_6': 6,
    'SUBTITLE': 7,
    'NORMAL_TEXT': 99,
}


def extract_doc_ids_from_event(event: dict) -> list:
    """Return a list of Google Doc IDs found in the event's Drive attachments."""
    doc_ids = []
    for attachment in event.get('attachments', []):
        if attachment.get('mimeType') != _DOC_MIME_TYPE:
            continue
        url = attachment.get('fileUrl', '')
        match = _DOC_ID_PATTERN.search(url)
        if match:
            doc_ids.append(match.group(1))
    return doc_ids


def fetch_doc_content(docs_svc, doc_id: str) -> list:
    """Fetch a Google Doc and return its body content list."""
    doc = docs_svc.documents().get(documentId=doc_id).execute()
    return doc.get('body', {}).get('content', [])


def build_today_date_prefix(today: datetime.date) -> str:
    """Return today's date formatted as the heading prefix, e.g. 'Feb 25, 2026'."""
    # Use .day integer directly to avoid platform-specific zero-stripping flags.
    return f"{today.strftime('%b')} {today.day}, {today.year}"


def _get_paragraph_text(paragraph: dict) -> str:
    """Join all textRun content in a paragraph into a single stripped string."""
    return ''.join(
        elem.get('textRun', {}).get('content', '')
        for elem in paragraph.get('elements', [])
    ).strip()


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
    STATE_SEARCHING_DATE = 0
    STATE_SEARCHING_TOPICS = 1
    STATE_CHECKING_CONTENT = 2

    state = STATE_SEARCHING_DATE
    date_heading_level = None
    topics_heading_level = None

    for element in content:
        if 'paragraph' not in element:
            # Skip sectionBreak, table, etc.
            continue

        para = element['paragraph']
        text = _get_paragraph_text(para)
        level = _heading_level(para)
        is_heading = level < 99

        if state == STATE_SEARCHING_DATE:
            if is_heading and text.startswith(date_prefix):
                date_heading_level = level
                state = STATE_SEARCHING_TOPICS
                logger.debug("Found today's date heading: '%s' (level %d)", text, level)

        elif state == STATE_SEARCHING_TOPICS:
            # If we hit another heading at the same or higher level, we've left today's section.
            if is_heading and level <= date_heading_level:
                logger.debug("Left today's section without finding 'Topics'.")
                return False
            if text.lower() == 'topics':
                topics_heading_level = level
                state = STATE_CHECKING_CONTENT
                logger.debug("Found 'Topics' sub-heading (level %d)", level)

        elif state == STATE_CHECKING_CONTENT:
            # A heading at same or higher level than the date heading ends today's section.
            if is_heading and level <= date_heading_level:
                logger.debug("Left today's section: no topic content found.")
                return False
            # A heading at same or higher level as the topics heading means a new sub-section.
            if is_heading and topics_heading_level is not None and level <= topics_heading_level:
                logger.debug("Left 'Topics' sub-section without content.")
                return False
            # Non-empty, non-heading text counts as a topic.
            if not is_heading and text:
                logger.debug("Found topic content: '%s'", text[:60])
                return True

    # Exhausted document without confirming topics.
    return False
