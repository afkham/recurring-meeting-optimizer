# Software Requirements Specification
## Recurring Meeting Optimizer

| Field | Value |
|---|---|
| Version | 1.0 |
| Date | February 2026 |
| Author | Afkham Azeez |
| License | Apache License 2.0 |
| Repository | https://github.com/afkham/recurring-meeting-optimizer2 |

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Overall Description](#2-overall-description)
3. [Actors](#3-actors)
4. [Functional Requirements](#4-functional-requirements)
5. [Non-Functional Requirements](#5-non-functional-requirements)
6. [External Interface Requirements](#6-external-interface-requirements)
7. [Constraints](#7-constraints)
8. [Out of Scope](#8-out-of-scope)
9. [Configuration Reference](#9-configuration-reference)
10. [Glossary](#10-glossary)

---

## 1. Introduction

### 1.1 Purpose

This document specifies the functional and non-functional requirements for the **Recurring Meeting Optimizer** — a command-line tool that automatically cancels recurring Google Calendar meetings whose attached agenda document has no discussion topics listed for today.

### 1.2 Problem Statement

Recurring meetings are frequently held even when there is nothing to discuss. Cancelling them manually requires the organizer to check the agenda document, decide to cancel, update the calendar event, and notify all attendees — a repetitive task that is easy to forget or skip. This tool automates that decision and action.

### 1.3 Scope

The system:

- Runs once per invocation (typically scheduled via cron).
- Reads from the user's **primary** Google Calendar.
- Reads attached **Google Docs** for agenda content.
- **Cancels** individual occurrences of recurring events that have no agenda topics for today.
- Notifies all attendees of each cancellation via Google Calendar's built-in email mechanism.

The system does **not** reschedule meetings, modify attendees, alter the recurring series, or interact with any system outside of Google Calendar, Google Docs, and Google Drive.

### 1.4 Definitions

| Term | Definition |
|---|---|
| **Occurrence** | A single instance of a recurring event series on a specific date |
| **Recurring event** | A Google Calendar event with a `recurringEventId` field set by the Calendar API |
| **Agenda doc** | A Google Doc attached to a calendar event via a Google Drive attachment |
| **Date heading** | A paragraph with a Heading style whose text starts with today's date in `Mon DD, YYYY` format |
| **Topics section** | A paragraph with text matching `Topic`, `Topics`, `Topic:`, or `Topics:` (case-insensitive) |
| **Dry-run mode** | Execution mode in which decisions are logged but no changes are made to Calendar or Docs |
| **Live mode** | Default execution mode in which qualifying occurrences are cancelled and attendees notified |

---

## 2. Overall Description

### 2.1 System Context

```
┌─────────────────────────────────────────────────────────────┐
│                         User Machine                        │
│                                                             │
│   ┌─────────────────────────────────────┐                   │
│   │   Recurring Meeting Optimizer       │                   │
│   │   (Python CLI, cron-scheduled)      │                   │
│   └──────────┬───────────┬─────────────┘                   │
│              │           │                                  │
└──────────────┼───────────┼──────────────────────────────────┘
               │ HTTPS     │ HTTPS
    ┌──────────▼──┐   ┌────▼──────────┐
    │  Google     │   │  Google Docs  │
    │  Calendar   │   │  + Drive APIs │
    │  API v3     │   │  v1 / v3      │
    └─────────────┘   └───────────────┘
```

The system runs on the user's machine with OAuth2 credentials that grant access to the user's Google account. No server infrastructure is required.

### 2.2 Operating Modes

| Mode | Flag | Behaviour |
|---|---|---|
| **Live** | *(none)* | Cancels qualifying occurrences, emails attendees |
| **Dry-run** | `--dry-run` | Logs what would be cancelled; no changes made |

### 2.3 Decision Logic Summary

```
For each recurring event today:
  ├─ No Google Doc attached?          → KEEP  (reason: no_doc)
  ├─ Doc unreadable (permission/net)? → KEEP  (reason: doc_error)
  ├─ No date heading for today?       → CANCEL (reason: no_topics)
  ├─ Date heading found, no Topics
  │    section present?               → CANCEL (reason: no_topics)
  ├─ Topics section present but
  │    empty?                         → CANCEL (reason: no_topics)
  └─ Topics section has content?      → KEEP  (reason: has_topics)
```

When multiple docs are attached, the event is **kept** if **any** doc has topics.

---

## 3. Actors

| Actor | Description |
|---|---|
| **Organizer** | The Google account user who runs the tool and owns the primary calendar |
| **Attendees** | Other participants of the recurring meetings; receive cancellation emails |
| **Scheduler** | The OS cron daemon or equivalent that triggers the program automatically |

---

## 4. Functional Requirements

### 4.1 Event Retrieval

**FR-01** The system SHALL fetch all events from the user's **primary** Google Calendar that occur on today's date, using the user's calendar timezone to determine day boundaries.

**FR-02** The system SHALL expand recurring series into individual occurrences (using `singleEvents=True`) so that only today's instance is evaluated and (if necessary) cancelled, leaving past and future occurrences untouched.

**FR-03** The system SHALL process **only** events that have the `recurringEventId` field set by the Calendar API. Non-recurring events SHALL never be cancelled.

**FR-04** The system SHALL skip events whose `status` is already `cancelled`.

**FR-05** The system SHALL skip all-day events (events where the `start` object contains a `date` field but no `dateTime` field).

**FR-06** If the Calendar API returns more than 100 pages of results, the system SHALL stop fetching further pages, log a warning, and process the events collected so far.

### 4.2 Timezone Handling

**FR-07** The system SHALL retrieve the user's timezone from the Calendar API settings (`settings().get(setting='timezone')`) and use it to compute today's date and the time-window for event retrieval.

**FR-08** If the timezone string returned by the API is not recognised by the platform's `zoneinfo` database, the system SHALL log a warning and fall back to UTC.

### 4.3 Google Doc Attachment Detection

**FR-09** The system SHALL identify the agenda doc via the event's Google Drive attachments list. Only attachments with MIME type `application/vnd.google-apps.document` are considered.

**FR-10** The system SHALL extract the Google Doc ID from the attachment's `fileUrl` by matching the pattern `/document/d/<ID>`.

**FR-11** The system SHALL validate attachment URLs: URLs longer than **2 048 characters** or with an extracted doc ID longer than **128 characters** SHALL be silently skipped.

**FR-12** Events with no valid Google Doc attachment SHALL be skipped (not cancelled). A warning SHALL be logged.

**FR-13** If an event has multiple Google Doc attachments, all shall be evaluated. The event SHALL be kept if **at least one** doc has topics for today.

### 4.4 Agenda Document Parsing

**FR-14** The system SHALL fetch the full body content of the agenda doc from the Google Docs API.

**FR-15** The system SHALL parse the flat `body.content` list returned by the Docs API using a **three-state machine**:

| State | Transition trigger |
|---|---|
| `SEARCHING_DATE` | A heading whose text starts with today's date prefix (e.g. `Feb 26, 2026`) |
| `SEARCHING_TOPICS` | Normal or heading text matching `Topic`, `Topics`, `Topic:`, or `Topics:` (case-insensitive) |
| `CHECKING_CONTENT` | Any non-empty text not in the end-section list |

**FR-16** The date prefix in the heading SHALL be matched using the format `Mon DD, YYYY` (three-letter month abbreviation, day without leading zero, four-digit year), e.g. `Feb 26, 2026`. The heading may contain additional text after the date (e.g. `Feb 26, 2026 | Team Sync`).

**FR-17** The date heading MUST use a Google Docs Heading style (Heading 1–6, Title, or Subtitle). Plain bold text does not qualify.

**FR-18** The system SHALL extract the display text from all of the following Docs API paragraph element types: `textRun`, `dateElement` (smart chip), `richLink` (calendar/Drive chip), and `person` (@mention).

**FR-19** A date section ends when one of the following is encountered after the date heading:
- A heading at a **strictly higher** level in the document hierarchy (lower level number), or
- A heading at the **same level** whose text matches the date prefix pattern `^[A-Z][a-z]{2} \d{1,2}, \d{4}`.

**FR-20** The Topics section ends when any of the following strings are encountered (case-insensitive, in either heading or normal text): `notes`, `action items`, `action item`, `next steps`, `next step`, `attendees`, `attendees:`, `agenda`, `resources`, `follow-up`, `follow up`.

**FR-21** At least one **non-empty, non-whitespace** line must appear between the Topics section marker and the first end-section name for the event to be kept.

**FR-22** The parser SHALL process at most **10 000 content elements** per document. If this limit is reached, parsing stops and the document is treated as having no topics (meeting is cancelled).

**FR-23** If today's date heading is found but no Topics section exists before the end of the document (or the start of the next date section), the system SHALL treat this as "no topics" and cancel the meeting. An INFO-level log message SHALL be emitted to distinguish this from the case where the date heading was not found at all.

### 4.5 Cancellation

**FR-24** In live mode, the system SHALL cancel a qualifying occurrence by:
1. PATCHing the event's description to prepend the cancellation note (see FR-25).
2. DELETing the specific occurrence with `sendUpdates='all'` so all attendees receive a cancellation email.

**FR-25** The cancellation note prepended to the event description SHALL be:
> `Meeting canceled since there are no topics to be discussed today`

**FR-26** The system SHALL implement an **idempotency guard**: if the event's description already begins with the cancellation note (indicating a previous partial run patched but did not delete), the PATCH step SHALL be skipped and the DELETE SHALL proceed directly.

**FR-27** If the PATCH succeeds but the DELETE fails, the system SHALL log a CRITICAL-level message containing the event ID and a prompt for the operator to cancel the event manually. The exception SHALL be re-raised so the per-event error handler in the main loop catches it.

**FR-28** In dry-run mode, the system SHALL log the events it **would** cancel and the reason, without making any API write calls.

### 4.6 Logging

**FR-29** The system SHALL write logs to both **stdout** and a rotating log file (`optimizer.log` in the working directory) at INFO level by default.

**FR-30** The log file SHALL rotate at **10 MB** and keep at most **5** backup files.

**FR-31** If the log file cannot be opened (permission denied, disk full, missing directory), the system SHALL log a warning to stdout and continue with stdout-only logging. It SHALL NOT exit due to a logging failure.

**FR-32** The system SHALL emit the following log levels:
- `DEBUG` — internal parsing milestones (not shown at default level)
- `INFO` — normal operations: events kept, events cancelled, token refresh, timezone
- `WARNING` — skipped events (no doc, no doc readable), unknown timezone, token scope mismatch
- `ERROR` — recoverable per-event errors (doc unreadable, API error)
- `CRITICAL` — incomplete cancellation requiring manual intervention

**FR-33** All event summaries written to logs SHALL be truncated to **80 characters** and passed through `repr()` to neutralise embedded newlines and control characters.

### 4.7 Authentication

**FR-34** The system SHALL use OAuth 2.0 with an `InstalledAppFlow` (desktop application) to authenticate with Google APIs.

**FR-35** On first run the system SHALL open the user's browser for a one-time consent flow and save the resulting token to `token.json`.

**FR-36** On subsequent runs the system SHALL load `token.json` and refresh it silently. If the token is expired the system SHALL refresh it; if the refresh fails (token revoked) the system SHALL trigger the browser consent flow again.

**FR-37** If `token.json` is corrupt or unparseable, the system SHALL log a warning, delete the corrupt file, and trigger a fresh browser consent flow.

**FR-38** If a network error (`TransportError`) occurs during token refresh, the system SHALL log the error and exit with code 1; it SHALL NOT silently fall through to a browser flow.

**FR-39** The system SHALL validate that the scopes stored in a cached token are a superset of the required scopes. If not, the system SHALL discard the token and trigger re-authentication.

**FR-40** The required OAuth 2.0 scopes are:
- `https://www.googleapis.com/auth/calendar`
- `https://www.googleapis.com/auth/documents.readonly`
- `https://www.googleapis.com/auth/drive.readonly`

### 4.8 Error Isolation

**FR-41** If processing one event raises an unhandled exception, the system SHALL log the error and continue processing the remaining events. A single event failure SHALL NOT abort the run.

**FR-42** If a Google API call to fetch a doc fails with an `HttpError`, `httplib2.HttpLib2Error`, or `OSError`, the system SHALL log the error, skip that doc, and continue evaluating any remaining docs attached to the event.

**FR-43** If all attached docs fail with access errors and none could be read, the system SHALL treat the result as `doc_error` and keep the meeting (safe side).

---

## 5. Non-Functional Requirements

### 5.1 Security

**NFR-01** `credentials.json` and `token.json` SHALL have their file permissions set to owner read/write only (Unix mode `0o600`) immediately after they are written or read.

**NFR-02** `optimizer.log` SHALL have its permissions set to owner read/write only (`0o600`).

**NFR-03** The OAuth state/code values SHALL NOT appear in log output. The `google_auth_oauthlib` and `google.auth.transport` loggers SHALL be suppressed to WARNING level or above.

**NFR-04** `token.json` SHALL be written **atomically** (write to a temporary file, then `os.replace()`) to prevent a partial write leaving a corrupt or empty token file.

**NFR-05** Event summaries SHALL be sanitised before being included in log messages (truncation to 80 chars + `repr()`) to prevent log injection attacks via crafted calendar event titles.

**NFR-06** The `googleapiclient.discovery` logger SHALL be suppressed to WARNING level to prevent API discovery responses from appearing in logs.

### 5.2 Reliability

**NFR-07** All Google API calls SHALL be retried up to **5 times** with exponential back-off on transient errors (HTTP 429, 5xx, transport failures), using the `num_retries` parameter of the `googleapiclient` library.

**NFR-08** API calls SHALL time out after **30 seconds** via `httplib2.Http(timeout=30)` wrapped in `AuthorizedHttp`, so that hung connections do not block the process indefinitely.

**NFR-09** If the Calendar API returns a paginated response and a page fetch fails after all retries are exhausted, the system SHALL return the events accumulated from successfully fetched pages (partial results) rather than aborting the entire run.

**NFR-10** The system SHALL impose a hard limit of **100 pagination pages** per run to prevent an infinite loop if the API unexpectedly returns an unending sequence of page tokens.

**NFR-11** The system SHALL impose a hard limit of **10 000 content elements** per document parse to prevent a pathologically large document from consuming excessive CPU or memory.

### 5.3 Maintainability

**NFR-12** The codebase SHALL be split into single-responsibility modules: `auth`, `calendar_service`, `docs_service`, `canceller`, and `main`.

**NFR-13** All configurable limits (timeouts, retry counts, page caps, element caps, URL length caps) SHALL be defined as named module-level constants, not as magic numbers inline.

### 5.4 Portability

**NFR-14** The system SHALL run on any platform supporting Python 3.9 or later and the `zoneinfo` standard library module.

**NFR-15** The system SHALL run unattended (non-interactively) on every run after the initial browser authentication, making it suitable for use in cron jobs.

### 5.5 Observability

**NFR-16** Every run SHALL log: the detected user timezone, today's date, the number of recurring events found, and the keep/cancel decision with reason for each event processed.

**NFR-17** An incomplete cancellation (PATCH succeeded, DELETE failed) SHALL be surfaced at CRITICAL log level with the event ID so that an operator can identify and manually resolve it.

---

## 6. External Interface Requirements

### 6.1 Google Calendar API (v3)

| Operation | Method | Key Parameters |
|---|---|---|
| Get user timezone | `settings().get(setting='timezone')` | — |
| List today's events | `events().list(...)` | `calendarId='primary'`, `singleEvents=True`, `timeMin`, `timeMax`, `orderBy='startTime'` |
| Update event description | `events().patch(...)` | `calendarId='primary'`, `eventId`, `body={'description': ...}` |
| Delete occurrence | `events().delete(...)` | `calendarId='primary'`, `eventId`, `sendUpdates='all'` |

### 6.2 Google Docs API (v1)

| Operation | Method | Key Parameters |
|---|---|---|
| Fetch document content | `documents().get(documentId=...)` | `documentId` |

The system reads `body.content` from the response, which is a flat list of structural elements. The system is read-only with respect to documents.

### 6.3 Google Drive API (v3)

The Drive API scope (`drive.readonly`) is used implicitly by the Calendar API to resolve attachment metadata. No explicit Drive API calls are made in the production code path.

### 6.4 Command-Line Interface

```
python3 main.py [--dry-run]
```

| Argument | Type | Description |
|---|---|---|
| `--dry-run` | Flag | Log decisions without making changes |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Run completed normally (regardless of how many meetings were cancelled) |
| `1` | Fatal error: missing `credentials.json`, unrecoverable auth failure, or uncaught exception |

### 6.5 File System

| File | Read/Write | Description |
|---|---|---|
| `credentials.json` | Read | OAuth 2.0 client secret downloaded from Google Cloud Console |
| `token.json` | Read/Write | Cached OAuth 2.0 access and refresh tokens |
| `optimizer.log` | Write | Rotating application log |

All three files reside in the working directory from which the program is invoked.

---

## 7. Constraints

| ID | Constraint |
|---|---|
| CON-01 | Python 3.9 or later is required (`zoneinfo` is a 3.9+ stdlib module) |
| CON-02 | Internet access is required for all Google API calls |
| CON-03 | The user must complete a one-time browser-based OAuth 2.0 consent before the first unattended run |
| CON-04 | The agenda doc must be attached to the calendar event as a Google Drive attachment; a URL pasted into the event description is not supported |
| CON-05 | The date heading in the agenda doc must use a Heading style (Heading 1–6, Title, Subtitle); bold normal text is not recognised |
| CON-06 | The date heading must use the format `Mon DD, YYYY` (English three-letter month abbreviation); other date formats are not supported |
| CON-07 | The system only processes the user's **primary** Google Calendar; secondary calendars are not supported |
| CON-08 | The Google Cloud project must have the Calendar API, Docs API, and Drive API enabled, and an OAuth 2.0 Desktop app credential created |

---

## 8. Out of Scope

The following capabilities are **explicitly excluded** from this version:

### 8.1 Calendar Operations
- Cancelling non-recurring (one-off) events
- Cancelling events from secondary or shared calendars
- Modifying the recurring series (only individual occurrences are affected)
- Rescheduling meetings to a different time or date
- Modifying event titles, attendee lists, or any field other than the description

### 8.2 Document Operations
- Writing to or modifying Google Docs
- Processing Google Sheets, Slides, or other file types
- Following document links or references to secondary documents
- Processing documents that are linked in the event description rather than attached via Drive

### 8.3 Authentication
- Service account authentication
- API key authentication
- Credentials passed via environment variables

### 8.4 Notifications
- Customising the cancellation email message
- Sending notifications through channels other than Google Calendar (Slack, Teams, email, etc.)
- Sending notifications for meetings that are kept

### 8.5 Configuration and Extensibility
- Runtime configuration files (all limits and constants are hardcoded)
- Custom topic section names beyond the built-in list
- Support for date formats other than `Mon DD, YYYY`
- Plugin or extension system
- REST API or web interface

### 8.6 Scheduling and Monitoring
- Self-scheduling (relies on external cron or launchd)
- Real-time calendar monitoring (point-in-time execution model only)
- Built-in alerting on failure (operators must monitor log files)
- Health check endpoints

---

## 9. Configuration Reference

### 9.1 Runtime Constants

| Constant | Module | Value | Purpose |
|---|---|---|---|
| `CREDENTIALS_PATH` | `auth` | `credentials.json` | OAuth client secret file path |
| `TOKEN_PATH` | `auth` | `token.json` | Cached OAuth token file path |
| `_API_TIMEOUT_SECONDS` | `auth` | `30` | Per-API-call HTTP timeout (seconds) |
| `LOG_FILE` | `main` | `optimizer.log` | Log file path |
| `_LOG_MAX_BYTES` | `main` | `10 485 760` (10 MB) | Log file rotation threshold |
| `_LOG_BACKUP_COUNT` | `main` | `5` | Number of rotated log backups to keep |
| `_MAX_PAGES` | `calendar_service` | `100` | Maximum pagination pages per run |
| `_MAX_API_RETRIES` | `calendar_service`, `docs_service` | `5` | Maximum API call retries |
| `_MAX_CONTENT_ELEMENTS` | `docs_service` | `10 000` | Maximum doc elements parsed per document |
| `_MAX_URL_LENGTH` | `docs_service` | `2 048` | Maximum attachment fileUrl length (chars) |
| `_MAX_DOC_ID_LENGTH` | `docs_service` | `128` | Maximum extracted doc ID length (chars) |
| `CANCELLATION_NOTE` | `canceller` | See FR-25 | Text prepended to cancelled event descriptions |

### 9.2 End-Section Names

The following strings (matched case-insensitively) terminate the Topics section during document parsing:

`notes` · `action items` · `action item` · `next steps` · `next step` · `attendees` · `attendees:` · `agenda` · `resources` · `follow-up` · `follow up`

### 9.3 Dependencies

| Package | Purpose |
|---|---|
| `google-api-python-client` | Google Calendar, Docs, and Drive API clients |
| `google-auth-oauthlib` | OAuth 2.0 browser consent flow |
| `google-auth-httplib2` | Authorized HTTP transport with credentials |
| `httplib2` | HTTP transport with configurable timeout |

---

## 10. Glossary

| Term | Definition |
|---|---|
| **Calendar API** | Google Calendar API v3 |
| **Docs API** | Google Docs API v1 |
| **Drive API** | Google Drive API v3 |
| **body.content** | The flat list of structural elements returned in a Google Docs API document response |
| **dateElement** | A Docs API paragraph element type representing a date smart chip inserted via Insert → Smart chips → Date |
| **richLink** | A Docs API paragraph element type representing a smart chip linked to a Drive file or Calendar event |
| **InstalledAppFlow** | The `google-auth-oauthlib` OAuth 2.0 flow type used for desktop applications |
| **occurrence** | A single date-specific instance of a recurring event series |
| **recurringEventId** | A field in a Google Calendar event that identifies which recurring series the event belongs to; only present on instances of recurring events |
| **sendUpdates** | A Google Calendar API parameter controlling whether attendees receive email notifications; set to `'all'` on cancellations |
| **singleEvents** | A Google Calendar API list parameter that expands recurring series into individual, cancellable instances |
| **namedStyleType** | A field in a Google Docs paragraph style that identifies the heading level: `HEADING_1` through `HEADING_6`, `TITLE`, `SUBTITLE`, or `NORMAL_TEXT` |
