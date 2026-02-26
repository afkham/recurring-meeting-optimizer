# Recurring Meeting Optimizer — User Guide

Recurring Meeting Optimizer automatically cancels recurring Google Calendar meetings that have no agenda topics. Before each meeting, the organizer attaches a Google Doc to the calendar event. If the doc contains today's date heading with topics listed under it, the meeting proceeds. If the topics section is empty (or the date heading is absent), the meeting is cancelled and all attendees are notified.

---

## Table of Contents

1. [How It Works](#1-how-it-works)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Configuring Google Cloud](#4-configuring-google-cloud)
5. [Setting Up Your Meeting Docs](#5-setting-up-your-meeting-docs)
6. [Attaching the Doc to a Calendar Event](#6-attaching-the-doc-to-a-calendar-event)
7. [First Run and Authentication](#7-first-run-and-authentication)
8. [Running the Program](#8-running-the-program)
9. [Running the Tests](#9-running-the-tests)
10. [Scheduling with Cron](#10-scheduling-with-cron)
11. [Viewing Logs](#11-viewing-logs)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. How It Works

Every time the program runs it:

1. Fetches all **recurring** Google Calendar events scheduled for **today** from your primary calendar.
2. For each recurring event, looks for an attached Google Doc (added via Google Drive attachment on the event).
3. Reads that doc and searches for a heading that starts with **today's date** (e.g. `Feb 26, 2026 | Team Sync`).
4. Under that date heading, looks for a **Topic** or **Topics** section.
5. **If topics are present** → the meeting is required → it is left untouched.
6. **If no date heading, or topics are empty** → the meeting is not required → the occurrence is cancelled and all attendees receive a cancellation email with the note: *"Meeting canceled since there are no topics to be discussed today"*.

Events that are **not recurring**, or recurring events that have **no Google Doc attached**, are always skipped (never cancelled).

---

## 2. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.9 or later | `python3 --version` to check |
| A Google account | The calendar to be managed |
| Internet access | Required to call Google APIs |

---

## 3. Installation

```bash
# Clone the repository
git clone https://github.com/afkham/recurring-meeting-optimizer2.git
cd recurring-meeting-optimizer2

# Install Python dependencies
pip install -r requirements.txt
```

---

## 4. Configuring Google Cloud

This is a one-time setup to grant the program access to your Google Calendar and Docs.

### Step 1 — Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and sign in.
2. Click the **project selector** dropdown at the top of the page (next to the Google Cloud logo).
3. Click **NEW PROJECT** in the popup.
4. Enter a project name (e.g. `recurring-meeting-optimizer`) and click **CREATE**.
5. Select the new project from the dropdown to make it active.

### Step 2 — Enable the three required APIs

Repeat the following steps for each API:

1. In the left sidebar go to **APIs & Services → Library**.
2. Search for the API name, click the result, then click **ENABLE**.

Enable these three APIs:

- **Google Calendar API**
- **Google Docs API**
- **Google Drive API**

### Step 3 — Configure the OAuth consent screen

1. Go to **APIs & Services → OAuth consent screen**.
2. Select **External** as the user type and click **CREATE**.
   *(If you are on a Google Workspace account and only need this for yourself, select **Internal**.)*
3. Fill in the required fields:
   - **App name**: `recurring-meeting-optimizer`
   - **User support email**: your Gmail address
   - **Developer contact information**: your Gmail address
4. Click **SAVE AND CONTINUE**.
5. On the **Scopes** page, click **SAVE AND CONTINUE** (no changes needed).
6. On the **Test users** page, click **+ ADD USERS**, enter your own Gmail address, then click **ADD**.
7. Click **SAVE AND CONTINUE**, then **BACK TO DASHBOARD**.

### Step 4 — Create an OAuth 2.0 credential

1. Go to **APIs & Services → Credentials**.
2. Click **+ CREATE CREDENTIALS → OAuth client ID**.
3. Under **Application type**, select **Desktop app**.
4. Under **Name**, enter `recurring-meeting-optimizer`.
5. Click **CREATE**.
6. In the dialog that appears, click **DOWNLOAD JSON**.
7. Rename the downloaded file to `credentials.json`.
8. Move it into the project root directory:

```bash
mv ~/Downloads/client_secret_*.json /path/to/recurring-meeting-optimizer2/credentials.json
```

> **Security note:** `credentials.json` and `token.json` are listed in `.gitignore` and will never be committed to the repository. Keep these files private.

---

## 5. Setting Up Your Meeting Docs

Each recurring meeting must have a single Google Doc used as its running agenda. The program looks for a specific structure in this doc.

### Document structure

Each meeting session is a separate entry at the **top** of the document, using the format below. Older entries remain further down the document.

```
<Date> | <Meeting title>        ← Heading style (e.g. Heading 2)

Attendees: Name 1, Name 2, ...

Topic:
- Discussion item 1
- Discussion item 2
- Discussion item 3

Notes
- ...

Action items
- ...
```

### Rules

| Element | Requirement |
|---|---|
| Date heading | Must use a Google Docs **Heading** style (Heading 1–6). The date format must be `Mon DD, YYYY` — e.g. `Feb 26, 2026`. Can be inserted as a **date smart chip** (Insert → Smart chips → Date) or typed as plain text. The meeting title after `\|` is optional but recommended. |
| `Topic:` or `Topics:` or `Topics` | Must appear **after** the date heading as normal or bold text. Singular or plural, with or without a colon — all accepted. |
| Topic items | List the agenda items as bullet points or plain lines under the `Topic:` section. At least one non-empty line is required for the meeting to be kept. |
| `Notes`, `Action items` | Standard section names that signal the end of the topics section. |

### Example — meeting will be KEPT (topics present)

```
Feb 26, 2026 | SRE Leadership Sync

Attendees: Alice, Bob, Carol

Topic:
- Review on-call rotation for Q2
- Discuss alerting threshold changes
- Plan for upcoming infrastructure migration

Notes

Action items
```

### Example — meeting will be CANCELLED (topics empty)

```
Feb 26, 2026 | SRE Leadership Sync

Attendees: Alice, Bob, Carol

Topic:

Notes

Action items
```

---

## 6. Attaching the Doc to a Calendar Event

The program finds the meeting notes doc via the **Google Drive attachment** on the calendar event. You only need to do this once per recurring meeting — the attachment carries forward to all future occurrences.

1. Open the recurring event in **Google Calendar**.
2. Click **Edit event** (pencil icon).
3. Click the **paperclip / Add attachment** button (Google Drive icon in the event editor).
4. Browse or search for your meeting notes Google Doc, select it, and click **Add**.
5. Click **Save** → choose **All events** to apply the attachment to the entire recurring series.

> The program only processes recurring events that have a Google Doc attached this way. Events without an attachment are always left untouched.

---

## 7. First Run and Authentication

The first time you run the program it will open your browser for a one-time Google sign-in:

```bash
cd /path/to/recurring-meeting-optimizer2
python3 main.py --dry-run
```

1. A browser window opens to Google's sign-in page.
2. Sign in with the Google account whose calendar you want to manage.
3. You may see a warning saying the app is unverified — click **Advanced → Go to recurring-meeting-optimizer (unsafe)** to proceed. This is expected for personal OAuth apps that have not gone through Google's verification process.
4. Grant the requested permissions (Calendar, Docs read-only, Drive read-only).
5. The browser shows a success message and the program continues.

A `token.json` file is saved in the project directory. All future runs use this token silently — the browser will not open again unless the token is revoked.

---

## 8. Running the Program

### Dry run (safe — no meetings are cancelled)

Use this to preview what the program would do without making any changes:

```bash
python3 main.py --dry-run
```

Sample output:
```
2026-02-26 08:00:01 INFO  recurring-meeting-optimizer starting.
2026-02-26 08:00:02 INFO  User timezone: Asia/Colombo
2026-02-26 08:00:02 INFO  Checking meetings for: 2026-02-26
2026-02-26 08:00:03 INFO  Found 3 recurring event(s) for 2026-02-26.
2026-02-26 08:00:04 INFO  [DRY RUN] Would cancel 'SRE Leadership Sync' (reason: no_topics).
2026-02-26 08:00:04 INFO  Keeping 'Weekly 1:1' (reason: has_topics).
2026-02-26 08:00:04 INFO  Keeping 'Monthly All Hands' (reason: no_doc).
2026-02-26 08:00:04 INFO  recurring-meeting-optimizer finished.
```

### Live run (cancellations are sent)

```bash
python3 main.py
```

Meetings with no topics are cancelled and all attendees receive a cancellation email.

---

## 9. Running the Tests

The integration test suite creates real temporary calendar events and Google Docs, runs the optimizer against them, verifies the results, then cleans everything up.

> The tests require expanded permissions (write access to Docs and Drive). On first run, a browser window will open for a separate one-time consent stored in `test_token.json`. This does not affect your main `token.json`.

```bash
python3 test_integration.py
```

Expected output:
```
============================================================
  recurring-meeting-optimizer — integration tests
============================================================

Authenticating (browser may open on first run)...
Timezone : Asia/Colombo
Today    : 2026-02-26

--- Creating test events and docs ---
  TC-01  recurring, doc+topics      event=abc123...
  TC-02  recurring, doc+no topics   event=def456...
  TC-03  non-recurring, doc+no topics  event=ghi789...
  TC-04  recurring, no doc          event=jkl012...

Waiting 5 s for Calendar API propagation...

--- Running optimizer on today's recurring events ---
  Test recurring events found: 3

--- Verification ---
  Test case                                     Expected     Got                  Result
  ---------------------------------------------------------------------------
  TC-01  Recurring  + doc WITH topics           KEEP         KEPT (confirmed)     PASS
  TC-02  Recurring  + doc NO topics             CANCEL       CANCELLED            PASS
  TC-03  Non-recurring + doc, no topics         KEEP         KEPT (confirmed)     PASS
  TC-04  Recurring  + NO doc attached           KEEP         KEPT (confirmed)     PASS

  ALL TESTS PASSED ✓  (4/4)

--- Cleanup ---
  Deleted event  tc01: abc123...
  ...
```

### What the tests verify

| Test | Setup | Expected outcome |
|---|---|---|
| TC-01 | Weekly recurring event with a doc that has topics | Meeting **kept** |
| TC-02 | Weekly recurring event with a doc that has no topics | Meeting **cancelled** |
| TC-03 | One-off (non-recurring) event with a doc, no topics | Meeting **kept** — non-recurring events are never cancelled |
| TC-04 | Weekly recurring event with no doc attached | Meeting **kept** — no doc means skip |

---

## 10. Scheduling with Cron

To run the program automatically every day (or at any interval), add a cron job.

### Find your Python path

```bash
which python3
# e.g. /opt/homebrew/bin/python3
```

### Open your crontab

```bash
crontab -e
```

### Add the cron entry

**Run once daily at 1 AM:**
```
0 1 * * * cd /path/to/recurring-meeting-optimizer2 && /opt/homebrew/bin/python3 main.py >> /path/to/recurring-meeting-optimizer2/optimizer.log 2>&1
```

**Run every hour:**
```
0 * * * * cd /path/to/recurring-meeting-optimizer2 && /opt/homebrew/bin/python3 main.py >> /path/to/recurring-meeting-optimizer2/optimizer.log 2>&1
```

Replace `/path/to/recurring-meeting-optimizer2` with the actual path on your machine.

### Important notes for macOS

- On macOS, cron jobs will **not run if the machine is asleep** at the scheduled time. If your Mac is frequently asleep at 1 AM, consider using **launchd** (macOS LaunchAgent) instead, which can wake the machine.
- On macOS Ventura and later, you may need to grant cron **Full Disk Access** in **System Settings → Privacy & Security → Full Disk Access**.

### Verify the cron job is set

```bash
crontab -l
```

---

## 11. Viewing Logs

The program writes logs to both the terminal and `optimizer.log` in the project directory.

```bash
# View the last 50 lines
tail -50 optimizer.log

# Follow live output
tail -f optimizer.log
```

Log levels:
- `INFO` — normal operation (meetings kept, meetings cancelled, token refresh)
- `WARNING` — skipped events (e.g. no doc attached)
- `ERROR` — recoverable errors (e.g. doc permission denied)
- `CRITICAL` — fatal errors that stopped the run

---

## 12. Troubleshooting

### `credentials.json not found`
Download the OAuth 2.0 client secret from Google Cloud Console (**APIs & Services → Credentials**) and save it as `credentials.json` in the project root. See [Section 4](#4-configuring-google-cloud).

### Browser does not open on first run
Ensure you are running the program in an environment with a graphical browser. If running over SSH, copy the URL printed in the terminal and open it in a local browser. Complete the auth flow and paste the resulting code back into the terminal.

### `RefreshError` — token revoked
Delete `token.json` and run the program again to re-authenticate:
```bash
rm token.json
python3 main.py --dry-run
```

### Meeting is cancelled even though topics are present
Check the Google Doc structure:
- The date heading (e.g. `Feb 26, 2026 | Meeting title`) must use a **Heading** style in Google Docs — not bold or large normal text.
- The `Topic:` / `Topics` section must appear **after** the date heading.
- At least one **non-empty line** must appear between `Topic:` and the next section (`Notes`, `Action items`, etc.).

### Meeting is not cancelled even though topics are empty
Verify the Google Doc is properly **attached** to the calendar event (not just linked in the description). See [Section 6](#6-attaching-the-doc-to-a-calendar-event).

### Recurring meeting not detected
The program only processes events that appear in your **primary** Google Calendar and have the `recurringEventId` field set by the Calendar API. Ensure the event is a proper recurring series (not just a manually repeated one-off event).

### `file_cache` warning in logs
This warning (`file_cache is only supported with oauth2client<4.0.0`) is cosmetic and comes from the Google API client library. It has no effect on functionality and can be safely ignored.
