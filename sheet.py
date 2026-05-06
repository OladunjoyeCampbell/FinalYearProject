import os
import json
import gspread
import logging
from datetime import datetime
from google.oauth2 import service_account

# --------------------------------------------------------------------------------
# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CREDENTIALS_ENV_VAR = "GOOGLE_CREDENTIALS_JSON"

# Current academic session — change this each year in one place only
CURRENT_SESSION = "2025/2026"

# ── Session cutoff configuration ─────────────────────────────────────────────
# Niger State Polytechnic Zungeru — Dept of Computer Science
#
# Session registration does NOT follow a fixed calendar month.
# Instead, a CUTOFF DATE separates sessions:
#
#   Submitted BEFORE cutoff  →  PREVIOUS_SESSION  (or no date = previous)
#   Submitted ON/AFTER cutoff →  CURRENT_SESSION
#
# Update SESSION_CUTOFF_DATE at the start of each new registration period.
# Format: "YYYY-MM-DD"
PREVIOUS_SESSION = "2024/2025"

# ── Matric number year-code → session mapping ─────────────────────────────────
# Niger State Polytechnic matric numbers encode the intake year:
#   HNDNCC/023/XXXX  or  HNDSWD/023/XXXX  or  NDCS/023/XXXX  → 2024/2025
#   HNDNCC/024/XXXX  or  HNDSWD/024/XXXX  or  NDCS/024/XXXX  → 2025/2026
# This is the most reliable session signal — no date inference needed.
# Add new entries here each year.
MATRIC_YEAR_SESSION = {
    "023": "2024/2025",
    "024": "2025/2026",
    "025": "2026/2027",   # future
}
# Fallback cutoff date — only used when matric year code cannot be extracted
SESSION_CUTOFF_DATE = "2026-04-01"


# Matric numbers known to be test/dummy entries — suppress warnings for these
_KNOWN_TEST_MATRICS = {'1234', '12234455', '123321', '7788'}


def infer_session_from_matric(matric_str):
    """
    Determine academic session from the year code embedded in the matric number.

    Niger State Polytechnic matric format:
      PROGRAMME/YY/NNNN  e.g. HNDNCC/023/0005, NDCS/024/2759

    The two or three digit year code after the first slash identifies the
    intake cohort and maps directly to an academic session.

    Returns None if the matric number cannot be parsed (signals caller to
    try date-based fallback).
    """
    import re as _re
    if not matric_str:
        return PREVIOUS_SESSION
    clean = matric_str.strip()
    # Match the year code: digits between two slashes
    m = _re.search('/([0-9]{2,3})/', clean)
    if m:
        year_code_padded = m.group(1)
        year_code        = year_code_padded.lstrip('0') or '0'
        sess = MATRIC_YEAR_SESSION.get(year_code_padded) or                MATRIC_YEAR_SESSION.get(year_code)
        if sess:
            return sess
    # Only warn if not a known test matric
    if clean not in _KNOWN_TEST_MATRICS:
        logger.warning(
            f"infer_session_from_matric: cannot parse year code from '{clean}' "
            f"— falling back to date-based inference"
        )
    return None   # signals caller to try date fallback


def infer_session_from_date(date_str):
    """
    Fallback session inference from a date string when matric parsing fails.
    Uses SESSION_CUTOFF_DATE (April 1 2026) as the boundary.
    """
    if not date_str or not str(date_str).strip():
        return PREVIOUS_SESSION
    from datetime import datetime
    date_str = str(date_str).strip()
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
    ]
    dt = None
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str[:len(fmt)], fmt)
            break
        except ValueError:
            continue
    if not dt:
        return PREVIOUS_SESSION
    cutoff = datetime.strptime(SESSION_CUTOFF_DATE, "%Y-%m-%d")
    return CURRENT_SESSION if dt >= cutoff else PREVIOUS_SESSION


def infer_session(matric_str, date_str=""):
    """
    Master session inference — tries matric year code first, then date.
    This is the single function all code should call.

    Priority:
      1. Matric number year code  (most reliable — always present)
      2. Date string fallback     (for unusual/test matric numbers)
      3. PREVIOUS_SESSION         (safe default)
    """
    sess = infer_session_from_matric(matric_str)
    if sess:
        return sess
    return infer_session_from_date(date_str)

PROGRAM_SHEET_MAP = {
    "HND Software & Web Development":   "AvailableHNDSWDtopics",
    "HND Networking & Cloud Computing":  "AvailableHNDNCCtopics",
    "ND Computer Science":               "AvailableNDTopics"
}

# Internal globals — reset to None so _init_sheets() reconnects cleanly
_client           = None
_spreadsheet      = None
_available_sheets = {}
_taken_sheet      = None
_log_sheet        = None
_students_sheet   = None


#def get_credentials():
#    creds_json = os.getenv(CREDENTIALS_ENV_VAR)
#    if creds_json:
#        info = json.loads(creds_json)
#       return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
#  creds_file = os.path.join(os.getcwd(), "credentials.json")
#    if os.path.isfile(creds_file):
#        return service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
#    raise EnvironmentError(
#        f"No Google credentials found. Set {CREDENTIALS_ENV_VAR} or provide credentials.json"
#    )

def get_credentials():
    # 1. Try environment variable (single-line minified JSON)
    creds_json = os.getenv(CREDENTIALS_ENV_VAR, "").strip()
    if creds_json:
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise EnvironmentError(
                f"'{CREDENTIALS_ENV_VAR}' contains invalid JSON: {e}"
            ) from e
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        return service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    # 2. Try Render Secret File (recommended — avoids paste/formatting issues)
    secret_file = "/etc/secrets/credentials.json"
    if os.path.isfile(secret_file):
        return service_account.Credentials.from_service_account_file(
            secret_file, scopes=SCOPES
        )

    # 3. Try local credentials.json (development only)
    local_file = os.path.join(os.getcwd(), "credentials.json")
    if os.path.isfile(local_file):
        return service_account.Credentials.from_service_account_file(
            local_file, scopes=SCOPES
        )

    raise EnvironmentError(
        "No Google credentials found. Options:\n"
        "  1. Render env var: set GOOGLE_CREDENTIALS_JSON to minified JSON\n"
        "  2. Render Secret File: add file at /etc/secrets/credentials.json\n"
        "  3. Local dev: place credentials.json in the project root"
    )



def _reset_globals():
    """Reset all cached sheet objects so _init_sheets() reconnects from scratch."""
    global _client, _spreadsheet, _available_sheets, _taken_sheet, _log_sheet,            _students_sheet, _settings_sheet, _supervisors_sheet, _presentation_sheet
    _client                = None
    _spreadsheet           = None
    _available_sheets      = {}
    _taken_sheet           = None
    _log_sheet             = None
    _students_sheet        = None
    _settings_sheet        = None
    _supervisors_sheet     = None
    _presentation_sheet    = None
    _assignments_sheet     = None


def invalidate_cache():
    """
    Force a full reconnection on the next request by clearing only the
    spreadsheet-level cache without dropping credentials.
    Called after any write operation that changes session/matric data
    so the next read reflects the updated values.
    """
    global _client, _spreadsheet, _available_sheets, _taken_sheet, _log_sheet,            _students_sheet, _settings_sheet, _supervisors_sheet, _presentation_sheet
    _client                = None
    _spreadsheet           = None
    _available_sheets      = {}
    _taken_sheet           = None
    _log_sheet             = None
    _students_sheet        = None
    _settings_sheet        = None
    _supervisors_sheet     = None
    _presentation_sheet    = None
    _assignments_sheet     = None
    logger.info("Sheet cache invalidated — will reconnect on next request")


def _init_sheets():
    """
    Authorize and open ALL worksheets in a SINGLE API call.

    All sheet objects are cached as globals so subsequent calls to
    _get_presentation_sheet(), _get_settings_sheet() etc. return
    instantly without any further API calls — eliminating the 429
    rate-limit errors caused by repeated worksheets() fetches.

    Health check: if already initialised, skips reconnection entirely.
    Only reconnects if the spreadsheet object itself is missing (e.g.
    after Render dyno restart resets the process globals).
    """
    global _client, _spreadsheet, _available_sheets,            _taken_sheet, _log_sheet, _students_sheet,  _assignments_sheet,      _settings_sheet, _supervisors_sheet, _presentation_sheet

    # Already fully initialised — skip all API calls
    # Both _client AND at least _taken_sheet must be set to consider fully initialised
    if _client is not None and _spreadsheet is not None and _taken_sheet is not None:
        return

    # Connect to Google Sheets
    try:
        creds        = get_credentials()
        _client      = gspread.authorize(creds)
        _spreadsheet = _client.open("FinalYear2025ProjectTopics")
    except Exception as e:
        logger.error(f"Failed to connect to Google Sheets: {e}")
        raise

    # ── ONE API CALL: fetch all worksheets at once ────────────────────────────
    try:
        all_ws = {ws.title: ws for ws in _spreadsheet.worksheets()}
        logger.info(
            f"Worksheets loaded: {list(all_ws.keys())}"
        )
    except Exception as e:
        logger.error(f"Failed to fetch worksheets: {e}")
        raise

    # ── 1) Available-topics sheets ────────────────────────────────────────────
    for prog, title in PROGRAM_SHEET_MAP.items():
        if title in all_ws:
            _available_sheets[prog] = all_ws[title]
        else:
            ws = _spreadsheet.add_worksheet(title=title, rows=1000, cols=2)
            ws.append_row(["Topic Title", "Session"])
            _available_sheets[prog] = ws
            logger.info(f"Created sheet '{title}' for programme '{prog}'")

    # ── 2) TakenTopics ────────────────────────────────────────────────────────
    if "TakenTopics" in all_ws:
        _taken_sheet = all_ws["TakenTopics"]
        _ensure_session_column(_taken_sheet,
            ["Student Name", "Matric Number", "Programme",
             "Topic Title", "Supervisor", "Submission Date", "Session"])
    else:
        _taken_sheet = _spreadsheet.add_worksheet(title="TakenTopics", rows=1000, cols=7)
        _taken_sheet.append_row([
            "Student Name", "Matric Number", "Programme",
            "Topic Title", "Supervisor", "Submission Date", "Session"
        ])
        logger.info("Created TakenTopics sheet")

    # ── 3) Log sheet ──────────────────────────────────────────────────────────
    if "Log" in all_ws:
        _log_sheet = all_ws["Log"]
        _repair_log_header(ws=_log_sheet)
    else:
        _log_sheet = _spreadsheet.add_worksheet(title="Log", rows=5000, cols=7)
        _log_sheet.append_row([
            "Student Name", "Matric Number", "Programme",
            "Topic Title", "Supervisor", "Action", "Session"
        ])
        logger.info("Created Log sheet")

    # ── 4) Students sheet ─────────────────────────────────────────────────────
    if "Students" in all_ws:
        _students_sheet = all_ws["Students"]

    # ── 5) Settings sheet ─────────────────────────────────────────────────────
    if "Settings" in all_ws:
        _settings_sheet = all_ws["Settings"]
    else:
        _settings_sheet = _spreadsheet.add_worksheet(title="Settings", rows=20, cols=3)
        _settings_sheet.append_row(["Key", "Value", "Updated"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _settings_sheet.append_row(["portal_open",        "true",  ts])
        _settings_sheet.append_row(["presentation_fee",   "2000",  ts])
        _settings_sheet.append_row(["portal_message",
            "The portal is currently closed. Please check back later.", ts])
        logger.info("Created Settings sheet with defaults")

    # ── 6) Supervisors sheet ──────────────────────────────────────────────────
    if "Supervisors" in all_ws:
        _supervisors_sheet = all_ws["Supervisors"]
    else:
        _supervisors_sheet = _spreadsheet.add_worksheet(
            title="Supervisors", rows=100, cols=4)
        _supervisors_sheet.append_row(["Full Name", "Short Name", "Department", "Active"])
        logger.info("Created Supervisors sheet")

    # ── 7) Presentations sheet ────────────────────────────────────────────────
    if PRESENTATION_SHEET_NAME in all_ws:
        _presentation_sheet = all_ws[PRESENTATION_SHEET_NAME]
    else:
        _presentation_sheet = _spreadsheet.add_worksheet(
            title=PRESENTATION_SHEET_NAME,
            rows=500,
            cols=len(PRESENTATION_HEADERS)
        )
        _presentation_sheet.append_row(PRESENTATION_HEADERS)
        logger.info("Created Presentations sheet")

    # ── 8) AssignedSupervisors sheet ─────────────────────────────────────────
    if "AssignedSupervisors" in all_ws:
        _assignments_sheet = all_ws["AssignedSupervisors"]
    else:
        _assignments_sheet = _spreadsheet.add_worksheet(
            title="AssignedSupervisors", rows=500,
            cols=len(ASSIGNMENTS_HEADERS)
        )
        _assignments_sheet.append_row(ASSIGNMENTS_HEADERS)
        logger.info("Created AssignedSupervisors sheet")

    # ── Ensure Supervisors sheet has Passphrase column ────────────────────────
    if _supervisors_sheet:
        try:
            sv_header = [h.strip() for h in _supervisors_sheet.row_values(1)]
            if "Passphrase" not in sv_header:
                next_col = len(sv_header) + 1
                _supervisors_sheet.update_cell(1, next_col, "Passphrase")
                logger.info("Added Passphrase column to Supervisors sheet")
        except Exception as e:
            logger.warning(f"Could not check Supervisors Passphrase column: {e}")

    logger.info("All sheets initialised successfully in one API call")


def _ensure_session_column(ws, expected_headers):
    """
    If the worksheet is missing a 'Session' column, append it to the header row.
    This makes the migration from old data non-destructive.
    """
    try:
        existing = [h.strip() for h in ws.row_values(1)]
        if "Session" not in existing:
            next_col = len(existing) + 1
            ws.update_cell(1, next_col, "Session")
            logger.info(f"Added 'Session' column to '{ws.title}'")
    except Exception as e:
        logger.warning(f"Could not check/add Session column in '{ws.title}': {e}")


# ─────────────────────────────────────────────────────────────────────────────
# BUG 1 FIX — robust topic reading that tolerates manually-pasted data
# ─────────────────────────────────────────────────────────────────────────────

def _read_topics_from_sheet(ws):
    """
    Read all non-blank topic titles from an available-topics worksheet.

    Handles all the ways manual pasting can go wrong:
      • Topics pasted into column B instead of column A
      • Header row accidentally overwritten or missing
      • Extra blank rows between entries
      • Leading/trailing whitespace
      • em-dashes (—) mixed with hyphens (-) — normalised to a single space
        around the separator so matching is consistent

    Returns a list of clean topic title strings.
    """
    try:
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"Could not read sheet '{ws.title}': {e}")
        return []

    if not all_values:
        return []

    topics = []
    for row_idx, row in enumerate(all_values):
        # Collect all non-blank cells in this row
        cells = [c.strip() for c in row if c.strip()]
        if not cells:
            continue

        # Skip the header row — detect it by common header words
        candidate = cells[0]
        if candidate.lower() in ("topic title", "topic", "topics"):
            continue

        # Accept the first non-blank cell in the row as the topic title.
        # This means topics in column B (common pasting error) are still found
        # as long as column A is blank for that row.
        topic = _normalise_topic(candidate)
        if topic:
            topics.append(topic)

    return topics


def _normalise_topic(t):
    """
    Normalise a topic string for consistent storage and comparison:
      - Strip whitespace
      - Collapse multiple spaces
      - Do NOT change case (we keep display capitalisation)
    """
    if not t:
        return ""
    # collapse runs of whitespace
    import re
    return re.sub(r'\s+', ' ', t.strip())


def _topic_key(t):
    """
    Produce a lowercase, whitespace-collapsed key for comparison only.
    Used internally; never stored.
    """
    return _normalise_topic(t).lower()


# ─────────────────────────────────────────────────────────────────────────────

def get_available_topics(programme=None):
    """
    Returns available topics for a programme (list) or all programmes (dict).
    """
    _init_sheets()

    if programme is None:
        return {
            prog: _read_topics_from_sheet(ws)
            for prog, ws in _available_sheets.items()
        }

    ws = _available_sheets.get(programme)
    if not ws:
        logger.warning(f"No available sheet for programme '{programme}'")
        return []

    return _read_topics_from_sheet(ws)


def is_student_registered(matric_number):
    """Return True if this matric number has a record in TakenTopics."""
    _init_sheets()
    key = str(matric_number).strip().lower()
    try:
        for rec in _taken_sheet.get_all_records():
            if str(rec.get("Matric Number", "")).strip().lower() == key:
                return True
    except Exception as e:
        logger.error(f"is_student_registered error: {e}")
    return False


def register_topic(student_name, matric_number, programme, topic_title, supervisor):
    """
    Move a topic from the Available sheet into TakenTopics and write to Log.

    BUG 1 FIX: uses _read_topics_from_sheet() and _topic_key() for matching,
    which is tolerant of manual pasting, extra whitespace, and column offsets.
    """
    _init_sheets()

    ws_avail = _available_sheets.get(programme)
    if not ws_avail:
        logger.warning(f"Invalid programme: {programme}")
        return False

    # ── Check topic is still available ───────────────────────────────────────
    available_topics = _read_topics_from_sheet(ws_avail)
    topic_key = _topic_key(topic_title)

    matched_title = None
    for t in available_topics:
        if _topic_key(t) == topic_key:
            matched_title = t   # use the exact string from the sheet
            break

    if not matched_title:
        logger.warning(
            f"Topic not found in available sheet for '{programme}': '{topic_title}'"
        )
        return False

    # ── Check not already taken ───────────────────────────────────────────────
    try:
        taken_keys = [
            _topic_key(r.get("Topic Title", ""))
            for r in _taken_sheet.get_all_records()
        ]
    except Exception as e:
        logger.error(f"Could not read TakenTopics: {e}")
        return False

    if topic_key in taken_keys:
        logger.warning(f"Topic already taken: '{topic_title}'")
        return False

    # ── Write to TakenTopics ──────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Determine correct session for this student:
    # honour any existing TakenTopics session (carryover), else use matric default
    existing_session = ""
    try:
        for r in _taken_sheet.get_all_records():
            if str(r.get("Matric Number","")).strip().lower() == matric_number.strip().lower():
                existing_session = r.get("Session","")
                break
    except Exception:
        pass
    student_session = _infer_session_for_student(
        matric_number, sub_date_str=ts, existing_session=existing_session
    ) if existing_session else CURRENT_SESSION

    try:
        _taken_sheet.append_row([
            student_name,
            matric_number,
            programme,
            matched_title,
            supervisor,
            ts,                 # Submission Date — always written now
            student_session
        ])
    except Exception as e:
        logger.error(f"Failed to write to TakenTopics: {e}")
        return False

    # ── Remove from Available sheet ───────────────────────────────────────────
    try:
        all_rows = ws_avail.get_all_values()
        for idx, row in enumerate(all_rows, start=1):
            cells = [c.strip() for c in row if c.strip()]
            if cells and _topic_key(cells[0]) == topic_key:
                ws_avail.delete_rows(idx)
                break
    except Exception as e:
        logger.error(f"Could not delete topic from available sheet: {e}")
        # Not fatal — topic is registered, just may appear available briefly

    # ── Write to Log ──────────────────────────────────────────────────────────
    try:
        _log_sheet.append_row([
            student_name,
            matric_number,
            programme,
            matched_title,
            supervisor,
            "Submitted",
            CURRENT_SESSION
        ])
    except Exception as e:
        logger.error(f"Could not write to Log: {e}")

    logger.info(f"Topic '{matched_title}' registered for {matric_number} [{CURRENT_SESSION}]")
    return True


def drop_registered_topic(matric_number, programme):
    """Remove a student's taken topic and return it to the Available sheet."""
    _init_sheets()
    key = str(matric_number).strip().lower()

    try:
        rows = _taken_sheet.get_all_values()
    except Exception as e:
        logger.error(f"drop_registered_topic — could not read TakenTopics: {e}")
        return False

    for idx, row in enumerate(rows[1:], start=2):
        if str(row[1]).strip().lower() == key:
            title = row[3].strip()
            prog  = row[2].strip()
            ws_avail = _available_sheets.get(prog)
            if ws_avail:
                existing_keys = [_topic_key(t) for t in _read_topics_from_sheet(ws_avail)]
                if _topic_key(title) not in existing_keys:
                    ws_avail.append_row([title, CURRENT_SESSION])
            try:
                _taken_sheet.delete_rows(idx)
                _log_sheet.append_row([
                    row[0], row[1], prog, row[3], row[4], "Dropped", CURRENT_SESSION
                ])
            except Exception as e:
                logger.error(f"drop_registered_topic — delete/log error: {e}")
                return False
            logger.info(f"Dropped '{title}' for {matric_number}")
            return True

    return False


def _repair_log_header(ws=None):
    """
    Fix the Log sheet header row if it is corrupted or missing.

    The correct column order written by append_log_entry() is:
      A: Student Name  B: Matric Number  C: Programme
      D: Topic Title   E: Supervisor     F: Action   G: Session

    Accepts optional ws so it can be called from _init_sheets() before
    the _log_sheet global is assigned. Safe to call repeatedly.
    """
    correct = ["Student Name", "Matric Number", "Programme",
               "Topic Title", "Supervisor", "Action", "Session"]
    target = ws or _log_sheet
    if not target:
        return
    try:
        current = [c.strip() for c in target.row_values(1)]
        if current != correct:
            logger.warning(
                f"Log header is wrong: {current} — repairing to: {correct}"
            )
            target.update('A1:G1', [correct])
            logger.info("Log header repaired successfully.")
    except Exception as e:
        logger.error(f"_repair_log_header error: {e}")


# Column indices for Log sheet rows (0-based) — position-based reading
# so a corrupt header never breaks data extraction.
#
# Column layout written by append_log_entry():
#   A(0) Student Name | B(1) Matric Number | C(2) Programme
#   D(3) Topic Title  | E(4) Supervisor    | F(5) Action
#   G(6) Session
#
# NOTE: The old broken header called col A "Topic" and col B "Session".
#       Real data was always in the positions below — the header was just wrong.
_LOG_COL = {
    "Student Name":    0,
    "Matric Number":   1,
    "Programme":       2,
    "Topic Title":     3,
    "Supervisor":      4,
    "Action":          5,
    "Session":         6,
}

# Known bad session values that were incorrectly backfilled
_BAD_SESSION_VALUES = {"2025/2026", ""}   # will be re-inferred from date


def _parse_log_row(row):
    """
    Extract fields from a Log row by column position.

    Session determination priority:
      1. col G value — if it looks like a valid session (e.g. "2024/2025")
         AND is not a known bad backfill value
      2. Inferred from Submission Date in TakenTopics (looked up separately)
      3. CURRENT_SESSION fallback

    Returns a dict with all expected keys, tolerating short rows.
    """
    def col(idx):
        return row[idx].strip() if idx < len(row) else ""

    raw_session = col(_LOG_COL["Session"])

    # Validate session format: must look like "YYYY/YYYY"
    import re
    session_ok = bool(re.match(r'^[0-9]{4}/[0-9]{4}$', raw_session))

    return {
        "Student Name":    col(_LOG_COL["Student Name"]),
        "Matric Number":   col(_LOG_COL["Matric Number"]),
        "Programme":       col(_LOG_COL["Programme"]),
        "Topic Title":     col(_LOG_COL["Topic Title"]),
        "Supervisor":      col(_LOG_COL["Supervisor"]),
        "Action":          col(_LOG_COL["Action"]),
        "Session":         raw_session if session_ok else "",
    }


def _extract_registration_date(action_str):
    """
    Extract timestamp embedded in 'Student Registration: YYYY-MM-DD HH:MM:SS'.
    Returns date string or empty string.
    Example: "Student Registration: 2026-04-22 11:00:21" -> "2026-04-22 11:00:21"
    """
    if not action_str:
        return ""
    import re as _re
    m = _re.search(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})', action_str)
    return m.group(1) if m else ""


def _infer_session_for_student(matric_str, reg_date_str="", sub_date_str="",
                               existing_session=""):
    """
    Determine session for a student — used ONLY when no session is recorded.

    Priority:
      1. existing_session already stored in TakenTopics → honour it always
         (admin corrections, carryover students must never be overwritten)
      2. Valid session in Log col G  → already handled by caller
      3. Matric year code            → default for new registrations
      4. Date fallback               → for unusual matric formats
      5. PREVIOUS_SESSION            → safe last resort

    The matric year code is a DEFAULT, not a rule — it can always be
    overridden by an admin editing TakenTopics directly.
    """
    # Honour existing stored session — never overwrite admin corrections
    if existing_session and _valid_session(existing_session):
        return existing_session
    # Try matric year code as intelligent default
    sess = infer_session_from_matric(matric_str)
    if sess:
        return sess
    # Date fallback
    return infer_session_from_date(reg_date_str or sub_date_str)


def _valid_session(s):
    """Return True if s looks like a valid YYYY/YYYY session string."""
    import re as _re
    return bool(_re.match('^[0-9]{4}/[0-9]{4}$', s.strip())) if s else False


def get_taken_topics():
    """
    Build the definitive list of active topic registrations by replaying
    the Log sheet — the single source of truth.

    The Log Action column contains "Student Registration: YYYY-MM-DD HH:MM:SS"
    for every student's first entry. This timestamp is extracted and used for
    session inference — it is more reliable than TakenTopics Submission Date
    because TakenTopics is incomplete (127 rows vs 253 unique students in Log).

    Session assignment:
      1. Registration timestamp from Log "Student Registration" row → infer session
      2. Fallback: Submission Date from TakenTopics
      3. Fallback: PREVIOUS_SESSION (safe default — all dateless = old records)

    Cutoff: SESSION_CUTOFF_DATE = April 1 2026
      Before → 2024/2025 (previous)
      On/after → 2025/2026 (current)

    Final state per student:
      Submit only             → shown
      Submit → Drop           → hidden
      Submit → Drop → Submit  → shown
      No Log trace            → shown from TakenTopics baseline
    """
    _init_sheets()
    _repair_log_header()

    # ── Step 1: TakenTopics baseline (legacy / incomplete records) ────────────
    baseline = {}   # matric_lower → rec dict
    tt_dates = {}   # matric_lower → submission_date string
    tt_expected = ["Student Name", "Matric Number", "Programme",
                   "Topic Title", "Supervisor", "Submission Date", "Session"]
    try:
        tt_rows = _taken_sheet.get_all_values()
        if len(tt_rows) >= 2:
            hdr = [h.strip() for h in tt_rows[0]]
            for row in tt_rows[1:]:
                if not any(c.strip() for c in row):
                    continue
                rec = {col: (row[i].strip() if i < len(row) else "")
                       for i, col in enumerate(hdr)}
                for k in tt_expected:
                    rec.setdefault(k, "")
                mkey = rec.get("Matric Number", "").strip().lower()
                if not mkey:
                    continue
                baseline[mkey] = rec
                tt_dates[mkey] = rec.get("Submission Date", "")
        logger.info(f"TakenTopics baseline: {len(baseline)} records")
    except Exception as e:
        logger.error(f"get_taken_topics — TakenTopics read error: {e}")

    # ── Step 2: Replay Log by column position ────────────────────────────────
    reg_date    = {}   # matric_lower → registration date string
    last_action = {}   # matric_lower → "submitted" | "dropped"
    last_submit = {}   # matric_lower → rec dict

    try:
        log_rows = _log_sheet.get_all_values()
        logger.info(f"Log: {len(log_rows)} total rows (incl header)")
        data_rows = log_rows[1:] if len(log_rows) > 1 else []

        for row in data_rows:
            if not any(c.strip() for c in row):
                continue

            rec    = _parse_log_row(row)
            mkey   = rec["Matric Number"].strip().lower()
            action = rec["Action"].strip()
            topic  = rec["Topic Title"].strip()

            if not mkey:
                continue

            # Capture registration timestamp from Action field
            if action.lower().startswith("student registration"):
                date_found = _extract_registration_date(action)
                if date_found and mkey not in reg_date:
                    reg_date[mkey] = date_found
                continue   # no topic on registration rows

            # Skip rows with no meaningful topic
            if not topic or topic.lower() in ("n/a", "-", "none", ""):
                continue

            action_lower = action.lower()
            if action_lower == "submitted":
                last_action[mkey] = "submitted"
                # Submission Date priority:
                #   1. TakenTopics Submission Date (set by register_topic)
                #   2. Registration timestamp from Log Action field
                #   3. Blank — shown as — in the view
                sub_date = (
                    tt_dates.get(mkey, "")
                    or reg_date.get(mkey, "")
                )
                last_submit[mkey] = {
                    "Student Name":    rec["Student Name"],
                    "Matric Number":   rec["Matric Number"],
                    "Programme":       rec["Programme"],
                    "Topic Title":     topic,
                    "Supervisor":      rec["Supervisor"],
                    "Submission Date": sub_date,
                    "Session":         "",   # assigned in Step 3
                }
            elif action_lower == "dropped":
                last_action[mkey] = "dropped"

        logger.info(
            f"Log replay: {len(last_action)} students with topic activity | "
            f"{len(reg_date)} registration timestamps captured | "
            f"{sum(1 for a in last_action.values() if a == 'submitted')} currently submitted"
        )
    except Exception as e:
        logger.error(f"get_taken_topics — Log replay error: {e}")

    # ── Step 3: Assign session to every student ──────────────────────────────
    # existing_session from TakenTopics is always honoured first —
    # this preserves admin corrections and carryover student placements.
    for mkey in last_submit:
        matric = last_submit[mkey]["Matric Number"]
        existing = baseline.get(mkey, {}).get("Session", "")
        last_submit[mkey]["Session"] = _infer_session_for_student(
            matric,
            reg_date.get(mkey, ""),
            tt_dates.get(mkey, ""),
            existing_session=existing
        )
    for mkey, rec in baseline.items():
        if mkey not in last_action:
            matric = rec.get("Matric Number", "")
            existing = rec.get("Session", "")
            rec["Session"] = _infer_session_for_student(
                matric,
                reg_date.get(mkey, ""),
                tt_dates.get(mkey, ""),
                existing_session=existing
            )

    # ── Step 4: Build final result ────────────────────────────────────────────
    final = {}

    # Baseline records with no Log trace at all
    for mkey, rec in baseline.items():
        if mkey not in last_action:
            final[mkey] = rec

    # Log replay overrides baseline
    for mkey, action in last_action.items():
        if action == "submitted":
            final[mkey] = last_submit[mkey]
            if mkey not in baseline:
                logger.info(
                    f"Recovered from Log: {last_submit[mkey]['Matric Number']} "
                    f"[{last_submit[mkey]['Session']}] — "
                    f"{last_submit[mkey]['Topic Title'][:50]}"
                )
        else:
            final.pop(mkey, None)

    logger.info(f"get_taken_topics: {len(final)} active registrations")
    return list(final.values())


def get_taken_topics_by_session(session=None):
    """
    Like get_taken_topics() but filtered to a specific session.
    Defaults to CURRENT_SESSION.
    """
    target = session or CURRENT_SESSION
    return [r for r in get_taken_topics() if r.get("Session", "") == target]


def ensure_students_sheet_exists():
    """Create Students sheet with headers if it doesn't already exist."""
    global _students_sheet
    _init_sheets()
    if _students_sheet:
        return True
    try:
        _students_sheet = _spreadsheet.worksheet("Students")
    except gspread.exceptions.WorksheetNotFound:
        _students_sheet = _spreadsheet.add_worksheet("Students", rows=1000, cols=10)
        _students_sheet.append_row([
            "Full Name", "Matric Number", "Programme",
            "Email", "Password Hash", "Registration Date"
        ])
    return True


def append_student_record(row):
    if not ensure_students_sheet_exists():
        return False
    _students_sheet.append_row(row)
    return True


def find_student_record(matric_number):
    if not ensure_students_sheet_exists():
        return None
    key = str(matric_number).strip().lower()
    try:
        for rec in _students_sheet.get_all_records():
            if str(rec.get("Matric Number", "")).strip().lower() == key:
                return rec
    except Exception as e:
        logger.error(f"find_student_record error: {e}")
    return None


def update_student_session(matric_number, new_session):
    """
    Permanently update a student's session in TakenTopics.

    This is the admin override — once set, get_taken_topics() and
    backfill_sessions() will ALWAYS honour this value and never
    overwrite it with matric-based or date-based inference.

    Used for:
      - Carryover students (matric says 023 but they are in current session)
      - Any session corrections the admin needs to make

    Returns True if updated, False if student not found.
    """
    _init_sheets()
    if not _valid_session(new_session):
        logger.error(f"update_student_session: invalid session '{new_session}'")
        return False

    key = matric_number.strip().lower()
    try:
        all_rows = _taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        try:
            m_idx = header.index("Matric Number")
            s_idx = header.index("Session")
        except ValueError as e:
            logger.error(f"update_student_session: missing column — {e}")
            return False

        for row_idx, row in enumerate(all_rows[1:], start=2):
            cell_matric = row[m_idx].strip().lower() if m_idx < len(row) else ""
            if cell_matric == key:
                _taken_sheet.update_cell(row_idx, s_idx + 1, new_session)
                logger.info(
                    f"update_student_session: {matric_number} → {new_session}"
                )
                return True
    except Exception as e:
        logger.error(f"update_student_session error: {e}")
    return False


def backfill_submission_dates():
    """
    Fill blank Submission Date cells in TakenTopics using the best
    available date from the Log sheet.

    Date source priority per student:
      1. TakenTopics Submission Date — already set, leave unchanged
      2. Registration timestamp from Log Action field
         ("Student Registration: YYYY-MM-DD HH:MM:SS")

    This is a one-time repair. Safe to run multiple times — only updates
    rows where Submission Date is currently blank.

    Returns summary dict: {filled, skipped, errors}
    """
    _init_sheets()
    summary = {"filled": 0, "skipped": 0, "errors": 0}

    # ── Step 1: Build reg_date lookup from Log ────────────────────────────────
    reg_date = {}   # matric_lower → registration timestamp string
    try:
        log_rows = _log_sheet.get_all_values()
        for row in log_rows[1:]:
            if not any(c.strip() for c in row):
                continue
            rec    = _parse_log_row(row)
            mkey   = rec["Matric Number"].strip().lower()
            action = rec["Action"].strip()
            if mkey and action.lower().startswith("student registration"):
                date_found = _extract_registration_date(action)
                if date_found and mkey not in reg_date:
                    reg_date[mkey] = date_found
        logger.info(f"backfill_submission_dates: {len(reg_date)} timestamps from Log")
    except Exception as e:
        logger.error(f"backfill_submission_dates Log read error: {e}")
        return summary

    # ── Step 2: Update blank Submission Date cells in TakenTopics ────────────
    try:
        tt_rows = _taken_sheet.get_all_values()
        if len(tt_rows) < 2:
            return summary

        header = [h.strip() for h in tt_rows[0]]
        try:
            m_idx = header.index("Matric Number")
            d_idx = header.index("Submission Date")
        except ValueError as e:
            logger.error(f"backfill_submission_dates: missing column — {e}")
            return summary

        d_col_1based = d_idx + 1

        for row_idx, row in enumerate(tt_rows[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            matric   = row[m_idx].strip() if m_idx < len(row) else ""
            cur_date = row[d_idx].strip() if d_idx < len(row) else ""

            if not matric:
                continue

            if cur_date:
                # Already has a date — leave it alone
                summary["skipped"] += 1
                continue

            # Look up best available date
            mkey     = matric.lower()
            new_date = reg_date.get(mkey, "")

            if new_date:
                try:
                    _taken_sheet.update_cell(row_idx, d_col_1based, new_date)
                    summary["filled"] += 1
                    logger.info(
                        f"Submission Date filled: {matric} → {new_date}"
                    )
                except Exception as e:
                    logger.error(f"backfill_submission_dates row {row_idx}: {e}")
                    summary["errors"] += 1
            else:
                summary["skipped"] += 1

    except Exception as e:
        logger.error(f"backfill_submission_dates TakenTopics error: {e}")

    logger.info(f"backfill_submission_dates complete: {summary}")
    return summary


def backfill_sessions():
    """
    Rebuild TakenTopics entirely from the Log replay and fix session
    values throughout.

    Strategy:
      1. Replay the full Log to get each student's final active submission
         and their registration timestamp (from Action field).
      2. Use registration timestamp to infer correct session via cutoff date.
      3. Completely rewrite TakenTopics with the correct, complete data.
         This replaces the current incomplete/wrong 127 rows with a full
         accurate set derived from all 253 unique Log students.

    Session cutoff: SESSION_CUTOFF_DATE (April 1 2026)
      Before  → PREVIOUS_SESSION (2024/2025)
      On/after → CURRENT_SESSION  (2025/2026)
      No date  → PREVIOUS_SESSION

    Returns summary dict: {rebuilt, skipped, errors}
    """
    _init_sheets()
    summary = {"rebuilt": 0, "skipped": 0, "errors": 0}

    # ── Phase 1: Full Log replay (same logic as get_taken_topics) ─────────────
    try:
        log_rows = _log_sheet.get_all_values()
        data_rows = log_rows[1:] if len(log_rows) > 1 else []
    except Exception as e:
        logger.error(f"backfill_sessions: cannot read Log — {e}")
        return summary

    # Read existing TakenTopics for submission dates
    tt_dates = {}
    tt_sessions = {}   # matric_lower → existing Session value
    try:
        tt_all = _taken_sheet.get_all_values()
        if len(tt_all) >= 2:
            hdr = [h.strip() for h in tt_all[0]]
            for row in tt_all[1:]:
                rec = {col: (row[i].strip() if i < len(row) else "")
                       for i, col in enumerate(hdr)}
                mkey = rec.get("Matric Number", "").strip().lower()
                if mkey:
                    tt_dates[mkey]   = rec.get("Submission Date", "")
                    tt_sessions[mkey] = rec.get("Session", "")
    except Exception as e:
        logger.warning(f"backfill_sessions: TakenTopics read warning — {e}")

    reg_date    = {}
    last_action = {}
    last_submit = {}

    for row in data_rows:
        if not any(c.strip() for c in row):
            continue
        rec    = _parse_log_row(row)
        mkey   = rec["Matric Number"].strip().lower()
        action = rec["Action"].strip()
        topic  = rec["Topic Title"].strip()
        if not mkey:
            continue

        if action.lower().startswith("student registration"):
            date_found = _extract_registration_date(action)
            if date_found and mkey not in reg_date:
                reg_date[mkey] = date_found
            continue

        if not topic or topic.lower() in ("n/a", "-", "none", ""):
            continue

        if action.lower() == "submitted":
            last_action[mkey] = "submitted"
            sub_date = (
                tt_dates.get(mkey, "")
                or reg_date.get(mkey, "")
            )
            last_submit[mkey] = {
                "Student Name":    rec["Student Name"],
                "Matric Number":   rec["Matric Number"],
                "Programme":       rec["Programme"],
                "Topic Title":     topic,
                "Supervisor":      rec["Supervisor"],
                "Submission Date": sub_date,
            }
        elif action.lower() == "dropped":
            last_action[mkey] = "dropped"

    # Build final active registrations
    active = {}
    for mkey, action in last_action.items():
        if action == "submitted":
            rec = last_submit[mkey]
            existing = tt_sessions.get(mkey, "")
            sess = _infer_session_for_student(
                rec.get("Matric Number", ""),
                reg_date.get(mkey, ""),
                tt_dates.get(mkey, ""),
                existing_session=existing
            )
            rec["Session"] = sess
            active[mkey] = rec

    logger.info(
        f"backfill_sessions: Log replay produced {len(active)} active registrations"
    )

    # ── Phase 2: Rewrite TakenTopics completely ───────────────────────────────
    header = ["Student Name", "Matric Number", "Programme",
              "Topic Title", "Supervisor", "Submission Date", "Session"]

    # Prepare all rows
    new_rows = [header]
    for mkey in sorted(active.keys()):
        rec = active[mkey]
        new_rows.append([
            rec.get("Student Name",    ""),
            rec.get("Matric Number",   ""),
            rec.get("Programme",       ""),
            rec.get("Topic Title",     ""),
            rec.get("Supervisor",      ""),
            rec.get("Submission Date", ""),
            rec.get("Session",         ""),
        ])

    try:
        # Clear the sheet and rewrite from scratch
        _taken_sheet.clear()
        _taken_sheet.update(f'A1:G{len(new_rows)}', new_rows)
        summary["rebuilt"] = len(new_rows) - 1
        logger.info(
            f"backfill_sessions: TakenTopics rebuilt with "
            f"{summary['rebuilt']} rows"
        )
    except Exception as e:
        logger.error(f"backfill_sessions: TakenTopics rewrite failed — {e}")
        summary["errors"] += 1
        return summary

    # ── Phase 3: Fix Session column in Log sheet ──────────────────────────────
    try:
        log_rows_fresh = _log_sheet.get_all_values()
        sess_col = _LOG_COL["Session"] + 1       # 1-based for update_cell
        mat_idx  = _LOG_COL["Matric Number"]
        act_idx  = _LOG_COL["Action"]
        ses_idx  = _LOG_COL["Session"]

        fixes = 0
        for row_idx, row in enumerate(log_rows_fresh[1:], start=2):
            if not any(c.strip() for c in row):
                continue
            mkey     = row[mat_idx].strip().lower() if mat_idx < len(row) else ""
            cur_sess = row[ses_idx].strip()         if ses_idx  < len(row) else ""
            if not mkey:
                continue
            matric_raw = row[mat_idx].strip() if mat_idx < len(row) else ""
            correct = _infer_session_for_student(
                matric_raw,
                reg_date.get(mkey, ""),
                tt_dates.get(mkey, "")
            )
            if correct != cur_sess:
                try:
                    _log_sheet.update_cell(row_idx, sess_col, correct)
                    fixes += 1
                except Exception as e:
                    logger.error(f"backfill Log row {row_idx}: {e}")
                    summary["errors"] += 1
        logger.info(f"backfill_sessions: Log session column — {fixes} rows corrected")
        summary["skipped"] = len(log_rows_fresh) - 1 - fixes
    except Exception as e:
        logger.error(f"backfill_sessions Log phase: {e}")

    logger.info(f"backfill_sessions complete — {summary}")
    return summary




def edit_student_record(orig_matric, new_name, new_matric,
                        new_prog, new_topic, new_supervisor, new_session):
    """
    Project Coordinator edit: update any field of a student's TakenTopics row.
    Finds the row by orig_matric, then overwrites all editable fields.
    Returns True if found and updated, False otherwise.
    """
    _init_sheets()
    key = orig_matric.strip().lower()
    try:
        all_rows = _taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        col = {h: i + 1 for i, h in enumerate(header)}   # 1-based col index

        for row_idx, row in enumerate(all_rows[1:], start=2):
            m_idx = col.get('Matric Number', 2) - 1
            cell_matric = row[m_idx].strip().lower() if m_idx < len(row) else ''
            if cell_matric == key:
                updates = {
                    'Student Name':    new_name,
                    'Matric Number':   new_matric,
                    'Programme':       new_prog,
                    'Topic Title':     new_topic,
                    'Supervisor':      new_supervisor,
                    'Session':         new_session,
                }
                for field, value in updates.items():
                    c = col.get(field)
                    if c:
                        _taken_sheet.update_cell(row_idx, c, value)
                logger.info(
                    f"edit_student_record: {orig_matric} → {new_matric} updated"
                )

                # ── Also update Presentations sheet if it has this student ──
                # This keeps the coordinator's presentations page in sync
                # without needing a manual Sync click after every edit.
                try:
                    pws = _presentation_sheet
                    if pws:
                        p_rows = pws.get_all_values()
                        if len(p_rows) >= 2:
                            p_hdr = [h.strip() for h in p_rows[0]]
                            p_m   = p_hdr.index("Matric Number")
                            p_s   = p_hdr.index("Session")
                            p_n   = p_hdr.index("Student Name")
                            p_pr  = p_hdr.index("Programme")
                            p_t   = p_hdr.index("Topic Title")
                            p_sv  = p_hdr.index("Supervisor")
                            for p_idx, p_row in enumerate(p_rows[1:], start=2):
                                cell_m = p_row[p_m].strip().lower() if p_m < len(p_row) else ''
                                # Match by OLD matric (before rename)
                                if cell_m == orig_matric.strip().lower():
                                    pws.update_cell(p_idx, p_m  + 1, new_matric)
                                    pws.update_cell(p_idx, p_s  + 1, new_session)
                                    pws.update_cell(p_idx, p_n  + 1, new_name)
                                    pws.update_cell(p_idx, p_pr + 1, new_prog)
                                    pws.update_cell(p_idx, p_t  + 1, new_topic)
                                    pws.update_cell(p_idx, p_sv + 1, new_supervisor)
                                    logger.info(
                                        f"Presentations sheet updated for {orig_matric}"
                                    )
                                    break
                except Exception as pe:
                    logger.warning(f"Presentations sync after edit failed: {pe}")

                # Invalidate cache so next page load reads fresh data
                invalidate_cache()
                return True
    except Exception as e:
        logger.error(f"edit_student_record error: {e}")
    return False


def _get_proposals_sheet():
    """Get or create the Proposals worksheet."""
    global _spreadsheet
    _init_sheets()
    try:
        return _spreadsheet.worksheet('Proposals')
    except Exception:
        ws = _spreadsheet.add_worksheet(title='Proposals', rows=500, cols=9)
        ws.append_row([
            'ID', 'Proposer', 'Type', 'Programme',
            'New Topic', 'Student Matric', 'Note',
            'Status', 'Submitted At'
        ])
        logger.info("Created Proposals sheet")
        return ws


def submit_topic_proposal(proposer, proposal_type, programme,
                          new_topic, student_matric='', note=''):
    """
    Write a supervisor's topic proposal to the Proposals sheet.
    proposal_type: 'New Topic' | 'Topic Change'
    Status is set to 'Pending' on creation.
    """
    try:
        ws  = _get_proposals_sheet()
        all_rows = ws.get_all_values()
        # Generate simple numeric ID
        proposal_id = str(len(all_rows))   # row count as ID
        ts  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        ws.append_row([
            proposal_id, proposer, proposal_type, programme,
            new_topic, student_matric, note, 'Pending', ts
        ])
        logger.info(f"Proposal #{proposal_id} submitted by {proposer}")
        return True
    except Exception as e:
        logger.error(f"submit_topic_proposal error: {e}")
        return False


def get_topic_proposals(status=None):
    """
    Return proposals from the Proposals sheet.
    If status is given ('Pending'|'Approved'|'Rejected'), filter by it.
    """
    try:
        ws = _get_proposals_sheet()
        rows = ws.get_all_records()
        if status:
            rows = [r for r in rows if r.get('Status', '') == status]
        return rows
    except Exception as e:
        logger.error(f"get_topic_proposals error: {e}")
        return []


def decide_topic_proposal(proposal_id, decision):
    """
    Update a proposal's Status to 'Approved' or 'Rejected'.

    On Approval:
      'New Topic'    → appends topic to the correct AvailableXXX sheet
      'Topic Change' → finds the student in TakenTopics by matric number
                       and overwrites their Topic Title with the new topic.
                       Also appends a record to the Log for audit trail.

    Returns (True, message) on success, (False, error_message) on failure.
    """
    try:
        ws = _get_proposals_sheet()
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return False, "Proposals sheet is empty."

        header = [h.strip() for h in all_rows[0]]
        try:
            id_idx      = header.index('ID')
            status_idx  = header.index('Status') + 1   # 1-based for update_cell
            type_idx    = header.index('Type')
            prog_idx    = header.index('Programme')
            topic_idx   = header.index('New Topic')
            matric_idx  = header.index('Student Matric')
            proposer_idx= header.index('Proposer')
        except ValueError as e:
            return False, f"Proposals sheet missing column: {e}"

        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[id_idx].strip() != str(proposal_id).strip():
                continue

            # ── Mark proposal as decided ──────────────────────────────────
            ws.update_cell(row_idx, status_idx, decision)
            logger.info(f"Proposal #{proposal_id} → {decision}")

            if decision != 'Approved':
                return True, f"Proposal #{proposal_id} rejected."

            prop_type      = row[type_idx].strip()
            programme      = row[prog_idx].strip()
            new_topic      = row[topic_idx].strip()
            student_matric = row[matric_idx].strip()
            proposer       = row[proposer_idx].strip() if proposer_idx < len(row) else ""

            # ── Approved: New Topic → add to available pool ───────────────
            if prop_type == 'New Topic':
                if not programme or not new_topic:
                    return False, "New Topic proposal is missing programme or topic text."
                ws_avail = _available_sheets.get(programme)
                if not ws_avail:
                    return False, f"No available topics sheet found for '{programme}'."
                ws_avail.append_row([new_topic, CURRENT_SESSION])
                logger.info(f"New topic added to '{programme}': {new_topic[:60]}")
                return True, f"Topic added to {programme} pool."

            # ── Approved: Topic Change → update student's TakenTopics row ──
            elif prop_type == 'Topic Change':
                if not student_matric or not new_topic:
                    return False, "Topic Change proposal is missing student matric or new topic."

                key = student_matric.strip().lower()
                try:
                    tt_rows = _taken_sheet.get_all_values()
                except Exception as e:
                    return False, f"Could not read TakenTopics: {e}"

                if len(tt_rows) < 2:
                    return False, "TakenTopics sheet has no data."

                tt_header = [h.strip() for h in tt_rows[0]]
                try:
                    m_col = tt_header.index('Matric Number')   # 0-based
                    t_col = tt_header.index('Topic Title')     # 0-based
                    n_col = tt_header.index('Student Name')
                    p_col = tt_header.index('Programme')
                    s_col = tt_header.index('Supervisor')
                except ValueError as e:
                    return False, f"TakenTopics missing column: {e}"

                # ── Step A: Update TakenTopics ───────────────────────────
                updated       = False
                student_name  = ''
                prog          = ''
                supervisor    = ''
                old_topic     = ''

                for tt_idx, tt_row in enumerate(tt_rows[1:], start=2):
                    cell_matric = tt_row[m_col].strip().lower() if m_col < len(tt_row) else ''
                    if cell_matric == key:
                        old_topic    = tt_row[t_col].strip() if t_col < len(tt_row) else ''
                        student_name = tt_row[n_col].strip() if n_col < len(tt_row) else ''
                        prog         = tt_row[p_col].strip() if p_col < len(tt_row) else ''
                        supervisor   = tt_row[s_col].strip() if s_col < len(tt_row) else ''
                        _taken_sheet.update_cell(tt_idx, t_col + 1, new_topic)
                        logger.info(
                            f"TakenTopics updated: {student_matric} | "
                            f"'{old_topic[:40]}' → '{new_topic[:40]}'"
                        )
                        updated = True
                        break

                if not updated:
                    return False, (
                        f"Student matric '{student_matric}' not found in TakenTopics. "
                        f"Run 'Rebuild Register' first, then try approving again."
                    )

                # ── Step B: Update the Log's LAST Submitted row ──────────
                # This is critical — the Log replay used by Rebuild reads
                # the last Submitted row's topic. If we don't update it,
                # Rebuild will revert TakenTopics back to the old topic.
                try:
                    log_rows   = _log_sheet.get_all_values()
                    log_header = [h.strip() for h in log_rows[0]]
                    l_mat_idx  = _LOG_COL["Matric Number"]   # 0-based
                    l_act_idx  = _LOG_COL["Action"]
                    l_top_idx  = _LOG_COL["Topic Title"]
                    l_top_col  = l_top_idx + 1               # 1-based for update_cell

                    # Walk Log in reverse to find the last Submitted row
                    # for this student and update its Topic Title.
                    # log_rows[0]=header, log_rows[1]=sheet row 2, etc.
                    # enumerate(log_rows[1:], start=2) gives correct 1-based idx.
                    last_submitted_row = None
                    data_rows_indexed = list(enumerate(log_rows[1:], start=2))
                    for actual_idx, l_row in reversed(data_rows_indexed):
                        l_matric = l_row[l_mat_idx].strip().lower() if l_mat_idx < len(l_row) else ''
                        l_action = l_row[l_act_idx].strip().lower() if l_act_idx < len(l_row) else ''
                        if l_matric == key and l_action == 'submitted':
                            _log_sheet.update_cell(actual_idx, l_top_col, new_topic)
                            logger.info(
                                f"Log row {actual_idx} updated: "
                                f"'{old_topic[:40]}' → '{new_topic[:40]}'"
                            )
                            last_submitted_row = actual_idx
                            break

                    if not last_submitted_row:
                        logger.warning(
                            f"No Submitted row found in Log for {student_matric} — "
                            f"appending new Submitted row so Rebuild stays correct"
                        )
                        sess = _infer_session_for_student(student_matric)
                        _log_sheet.append_row([
                            student_name, student_matric, prog,
                            new_topic, supervisor, 'Submitted', sess
                        ])

                except Exception as log_err:
                    logger.warning(f"Log update for topic change failed: {log_err}")

                # ── Step C: Append audit trail entry ─────────────────────
                try:
                    sess = _infer_session_for_student(student_matric)
                    _log_sheet.append_row([
                        student_name, student_matric, prog, new_topic,
                        supervisor,
                        f"Topic Change approved by Coordinator (proposed by {proposer})",
                        sess,
                    ])
                except Exception as audit_err:
                    logger.warning(f"Audit log entry failed: {audit_err}")

                return True, f"Topic updated for {student_matric}. Rebuild is no longer needed."

            else:
                return False, f"Unknown proposal type: '{prop_type}'"

        return False, f"Proposal #{proposal_id} not found."

    except Exception as e:
        logger.error(f"decide_topic_proposal error: {e}")
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# PRESENTATION / CLEARANCE / PAYMENT FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

PRESENTATION_SHEET_NAME = "Presentations"
PRESENTATION_HEADERS = [
    "Matric Number", "Student Name", "Programme", "Topic Title",
    "Supervisor", "Supervisor Cleared", "Cleared By",
    "Cleared Date", "Payment Amount", "Payment Status",
    "Payment Date", "Marked By", "Session"
]


def _get_presentation_sheet():
    """Return cached Presentations worksheet (initialised by _init_sheets)."""
    _init_sheets()
    return _presentation_sheet


def _find_presentation_row(ws, matric):
    """
    Find a student's row in the Presentations sheet by matric number.
    Returns (row_index_1based, row_data) or (None, None).
    """
    key = matric.strip().lower()
    try:
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return None, None
        header = [h.strip() for h in all_rows[0]]
        m_idx  = header.index("Matric Number")
        for row_idx, row in enumerate(all_rows[1:], start=2):
            cell = row[m_idx].strip().lower() if m_idx < len(row) else ""
            if cell == key:
                rec = {col: (row[i].strip() if i < len(row) else "")
                       for i, col in enumerate(header)}
                return row_idx, rec
    except Exception as e:
        logger.error(f"_find_presentation_row error: {e}")
    return None, None


def ensure_presentation_record(matric):
    """
    Ensure a student has a row in the Presentations sheet.
    Pulls student details from TakenTopics if not already present.
    Returns (ws, row_idx, rec) or (ws, None, None).
    """
    ws = _get_presentation_sheet()
    row_idx, rec = _find_presentation_row(ws, matric)
    if row_idx:
        return ws, row_idx, rec

    # Create a new row from TakenTopics data
    taken = get_taken_topics()
    student = next(
        (r for r in taken
         if r.get("Matric Number", "").strip().lower() == matric.strip().lower()),
        None
    )
    if not student:
        logger.warning(f"ensure_presentation_record: {matric} not in TakenTopics")
        return ws, None, None

    sess = _infer_session_for_student(matric)
    new_row = [
        student.get("Matric Number", ""),
        student.get("Student Name",  ""),
        student.get("Programme",     ""),
        student.get("Topic Title",   ""),
        student.get("Supervisor",    ""),
        "No",    # Supervisor Cleared
        "",      # Cleared By
        "",      # Cleared Date
        "",      # Payment Amount
        "Unpaid",# Payment Status
        "",      # Payment Date
        "",      # Marked By
        sess,    # Session
    ]
    ws.append_row(new_row)
    logger.info(f"Presentation record created for {matric}")
    row_idx, rec = _find_presentation_row(ws, matric)
    return ws, row_idx, rec


def get_presentation_records(session_filter=None):
    """
    Return all presentation records, optionally filtered by session.
    Adds computed field 'eligible': True when cleared AND paid.
    """
    ws = _get_presentation_sheet()
    try:
        records = ws.get_all_records()
        if session_filter:
            records = [r for r in records
                       if r.get("Session", "") == session_filter]
        for r in records:
            r["eligible"] = (
                r.get("Supervisor Cleared", "").lower() == "yes"
                and r.get("Payment Status", "").lower() == "paid"
            )
        return records
    except Exception as e:
        logger.error(f"get_presentation_records error: {e}")
        return []


def supervisor_clear_student(matric, cleared_by):
    """
    Supervisor (or coordinator) marks a student as cleared for presentation.
    Creates a Presentations row if one does not exist.
    Updates all three clearance cells in a single batch call for reliability.
    Returns (True, message) or (False, error).
    """
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, (
            f"Could not create presentation record for {matric}. "
            f"Make sure this student has a registered topic first."
        )

    header = [h.strip() for h in ws.row_values(1)]
    try:
        sc_col = header.index("Supervisor Cleared") + 1
        cb_col = header.index("Cleared By")         + 1
        cd_col = header.index("Cleared Date")       + 1
    except ValueError as e:
        return False, f"Presentations sheet missing column: {e}"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # Batch update all three cells at once — more reliable than
        # three separate update_cell calls which can fail midway
        ws.update([[
            "Yes", cleared_by, ts
        ]], f"{_col_letter(sc_col)}{row_idx}:{_col_letter(cd_col)}{row_idx}")
        logger.info(f"Student {matric} cleared by {cleared_by}")
        return True, f"{rec.get('Student Name', matric)} marked as cleared."
    except Exception as e:
        logger.error(f"supervisor_clear_student update error: {e}")
        # Fallback: try individual cells
        try:
            ws.update_cell(row_idx, sc_col, "Yes")
            ws.update_cell(row_idx, cb_col, cleared_by)
            ws.update_cell(row_idx, cd_col, ts)
            return True, f"{rec.get('Student Name', matric)} marked as cleared."
        except Exception as e2:
            return False, f"Failed to update clearance: {e2}"


def _col_letter(n):
    """Convert 1-based column number to letter (A, B, ... Z, AA, ...)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def coordinator_unclear_student(matric):
    """Coordinator reverses a clearance (e.g. supervisor requests reversal)."""
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, f"Student {matric} not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        sc_col = header.index("Supervisor Cleared") + 1
        cb_col = header.index("Cleared By")         + 1
        cd_col = header.index("Cleared Date")       + 1
    except ValueError as e:
        return False, str(e)
    ws.update_cell(row_idx, sc_col, "No")
    ws.update_cell(row_idx, cb_col, "")
    ws.update_cell(row_idx, cd_col, "")
    logger.info(f"Clearance reversed for {matric}")
    return True, f"Clearance reversed for {rec.get('Student Name', matric)}."


def record_payment(matric, amount, marked_by):
    """
    Coordinator marks a student as having paid the presentation fee.
    Returns (True, message) or (False, error).
    """
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, f"Student {matric} not found."

    header = [h.strip() for h in ws.row_values(1)]
    try:
        pa_col = header.index("Payment Amount")  + 1
        ps_col = header.index("Payment Status")  + 1
        pd_col = header.index("Payment Date")    + 1
        mb_col = header.index("Marked By")       + 1
    except ValueError as e:
        return False, f"Presentations sheet missing column: {e}"

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.update_cell(row_idx, pa_col, str(amount))
    ws.update_cell(row_idx, ps_col, "Paid")
    ws.update_cell(row_idx, pd_col, ts)
    ws.update_cell(row_idx, mb_col, marked_by)
    logger.info(f"Payment recorded for {matric}: ₦{amount} by {marked_by}")
    return True, (
        f"Payment of ₦{amount:,} recorded for "
        f"{rec.get('Student Name', matric)}."
    )


def reverse_payment(matric):
    """Coordinator reverses a payment record (e.g. bounced or error)."""
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, f"Student {matric} not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        pa_col = header.index("Payment Amount")  + 1
        ps_col = header.index("Payment Status")  + 1
        pd_col = header.index("Payment Date")    + 1
        mb_col = header.index("Marked By")       + 1
    except ValueError as e:
        return False, str(e)
    ws.update_cell(row_idx, ps_col, "Unpaid")
    ws.update_cell(row_idx, pa_col, "")
    ws.update_cell(row_idx, pd_col, "")
    ws.update_cell(row_idx, mb_col, "")
    logger.info(f"Payment reversed for {matric}")
    return True, f"Payment reversed for {rec.get('Student Name', matric)}."


def sync_presentations_from_registered():
    """
    Ensure every currently registered student has a row in Presentations.
    Call this after a Rebuild to keep Presentations in sync.
    Returns count of new rows created.
    """
    _init_sheets()
    registered = get_taken_topics()
    created = 0
    for student in registered:
        matric = student.get("Matric Number", "").strip()
        if not matric:
            continue
        ws = _get_presentation_sheet()
        row_idx, _ = _find_presentation_row(ws, matric)
        if not row_idx:
            ensure_presentation_record(matric)
            created += 1
    logger.info(f"sync_presentations_from_registered: {created} new rows created")
    return created


# ─────────────────────────────────────────────────────────────────────────────
# PORTAL SETTINGS (Open/Close + Fee)
# ─────────────────────────────────────────────────────────────────────────────

_settings_sheet     = None
_presentation_sheet = None
_assignments_sheet  = None

def _get_settings_sheet():
    """Return cached Settings worksheet (initialised by _init_sheets)."""
    _init_sheets()
    return _settings_sheet


def get_setting(key, default=""):
    """Read a setting value from the Settings sheet."""
    try:
        ws = _get_settings_sheet()
        for rec in ws.get_all_records():
            if rec.get("Key", "").strip() == key:
                return str(rec.get("Value", default)).strip()
    except Exception as e:
        logger.error(f"get_setting({key}) error: {e}")
    return default


def set_setting(key, value):
    """Write or update a setting in the Settings sheet."""
    try:
        ws  = _get_settings_sheet()
        ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        all_rows = ws.get_all_values()
        if len(all_rows) >= 2:
            header = [h.strip() for h in all_rows[0]]
            k_idx  = header.index("Key")
            v_idx  = header.index("Value") + 1      # 1-based
            u_idx  = header.index("Updated") + 1
            for row_idx, row in enumerate(all_rows[1:], start=2):
                if row[k_idx].strip() == key:
                    ws.update_cell(row_idx, v_idx, str(value))
                    ws.update_cell(row_idx, u_idx, ts)
                    logger.info(f"Setting '{key}' → '{value}'")
                    return True
        # Key not found — append
        ws.append_row([key, str(value), ts])
        logger.info(f"Setting '{key}' created → '{value}'")
        return True
    except Exception as e:
        logger.error(f"set_setting({key}) error: {e}")
        return False


def is_portal_open():
    """Return True if student topic submission is currently open."""
    return get_setting("portal_open", "true").lower() == "true"


def get_portal_message():
    """Return the message shown to students when portal is closed."""
    return get_setting(
        "portal_message",
        "The portal is currently closed. Please check back later."
    )


def get_presentation_fee():
    """Return the current presentation fee as integer."""
    try:
        return int(get_setting("presentation_fee", "2000"))
    except ValueError:
        return 2000


# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISORS LIST
# ─────────────────────────────────────────────────────────────────────────────

_supervisors_sheet = None

def _get_supervisors_sheet():
    """Return cached Supervisors worksheet (initialised by _init_sheets)."""
    _init_sheets()
    return _supervisors_sheet


def get_supervisors(active_only=True):
    """
    Return list of supervisor dicts.
    Each dict has: Full Name, Short Name, Department, Active.
    If active_only=True, returns only rows where Active == 'Yes'.
    """
    try:
        ws = _get_supervisors_sheet()
        records = ws.get_all_records()
        if active_only:
            records = [r for r in records
                       if str(r.get("Active", "Yes")).strip().lower() != "no"]
        return sorted(records, key=lambda r: r.get("Full Name", ""))
    except Exception as e:
        logger.error(f"get_supervisors error: {e}")
        return []


def add_supervisor(full_name, short_name="", department="", active="Yes"):
    """Add a new supervisor to the Supervisors sheet."""
    try:
        ws = _get_supervisors_sheet()
        # Check for duplicate
        existing = [r.get("Full Name","").strip().lower()
                    for r in ws.get_all_records()]
        if full_name.strip().lower() in existing:
            return False, f"'{full_name}' already exists."
        ws.append_row([full_name.strip(), short_name.strip(),
                       department.strip(), active])
        logger.info(f"Supervisor added: {full_name}")
        return True, f"'{full_name}' added successfully."
    except Exception as e:
        logger.error(f"add_supervisor error: {e}")
        return False, str(e)


def update_supervisor_status(full_name, active):
    """Set a supervisor as Active or Inactive."""
    try:
        ws = _get_supervisors_sheet()
        all_rows = ws.get_all_values()
        header   = [h.strip() for h in all_rows[0]]
        n_idx    = header.index("Full Name")
        a_col    = header.index("Active") + 1   # 1-based
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[n_idx].strip().lower() == full_name.strip().lower():
                ws.update_cell(row_idx, a_col, active)
                logger.info(f"Supervisor '{full_name}' set to {active}")
                return True, "Updated."
        return False, "Supervisor not found."
    except Exception as e:
        logger.error(f"update_supervisor_status error: {e}")
        return False, str(e)


def get_supervisor_names():
    """Return a simple list of active supervisor full names for dropdowns."""
    return [r.get("Full Name", "") for r in get_supervisors(active_only=True)]


# ─────────────────────────────────────────────────────────────────────────────
# SUPERVISOR AUTHENTICATION & ASSIGNMENT
# ─────────────────────────────────────────────────────────────────────────────

_assignments_sheet = None

SUPERVISORS_HEADERS  = ["Full Name", "Short Name", "Department",
                         "Active", "Passphrase"]
ASSIGNMENTS_HEADERS  = ["Matric Number", "Student Name", "Programme",
                         "Supervisor", "Assigned By", "Assigned Date", "Session"]


#def _get_assignments_sheet():
#    """Return cached AssignedSupervisors worksheet."""
#   global _assignments_sheet, _spreadsheet
#   _init_sheets()
#   return _assignments_sheet

def _get_assignments_sheet():
    global _assignments_sheet
    _init_sheets()
    return _assignments_sheet

def verify_supervisor(passphrase):
    """
    Check passphrase against the Supervisors sheet.
    Returns the supervisor's Full Name if matched, or None if not found.
    Only Active supervisors can log in.
    """
    try:
        ws = _get_supervisors_sheet()
        records = ws.get_all_records()
        ph = passphrase.strip()
        for rec in records:
            active = str(rec.get("Active", "Yes")).strip().lower()
            if active == "no":
                continue
            stored = str(rec.get("Passphrase", "")).strip()
            if stored and ph == stored:
                return rec.get("Full Name", "").strip()
    except Exception as e:
        logger.error(f"verify_supervisor error: {e}")
    return None


def get_assigned_supervisor(matric):
    """
    Return the supervisor assigned to a student by the coordinator.
    Returns empty string if no assignment exists.
    """
    ws = _get_assignments_sheet()
    if not ws:
        return ""
    key = matric.strip().lower()
    try:
        for rec in ws.get_all_records():
            if rec.get("Matric Number", "").strip().lower() == key:
                return rec.get("Supervisor", "").strip()
    except Exception as e:
        logger.error(f"get_assigned_supervisor error: {e}")
    return ""


def get_students_for_supervisor(supervisor_name):
    """
    Return list of matric numbers assigned to a specific supervisor
    from the AssignedSupervisors sheet (coordinator mappings).
    """
    ws = _get_assignments_sheet()
    if not ws:
        return []
    name_key = supervisor_name.strip().lower()
    try:
        return [
            rec.get("Matric Number", "").strip()
            for rec in ws.get_all_records()
            if rec.get("Supervisor", "").strip().lower() == name_key
        ]
    except Exception as e:
        logger.error(f"get_students_for_supervisor error: {e}")
        return []


def assign_supervisor(matric, student_name, programme,
                      supervisor_name, assigned_by):
    """
    Coordinator assigns a supervisor to a student.
    Creates or updates the row in AssignedSupervisors sheet.
    Also updates the Supervisor field in TakenTopics for consistency.
    Returns (True, message) or (False, error).
    """
    ws = _get_assignments_sheet()
    if not ws:
        return False, "AssignedSupervisors sheet not available."

    key = matric.strip().lower()
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sess = _infer_session_for_student(matric)

    try:
        all_rows = ws.get_all_values()
        header   = [h.strip() for h in all_rows[0]] if all_rows else []

        # Check if assignment already exists — update if so
        if len(all_rows) >= 2 and "Matric Number" in header:
            m_idx  = header.index("Matric Number")
            sv_col = header.index("Supervisor") + 1     if "Supervisor"    in header else None
            ab_col = header.index("Assigned By") + 1    if "Assigned By"   in header else None
            ad_col = header.index("Assigned Date") + 1  if "Assigned Date" in header else None

            for row_idx, row in enumerate(all_rows[1:], start=2):
                cell = row[m_idx].strip().lower() if m_idx < len(row) else ""
                if cell == key:
                    if sv_col: ws.update_cell(row_idx, sv_col, supervisor_name)
                    if ab_col: ws.update_cell(row_idx, ab_col, assigned_by)
                    if ad_col: ws.update_cell(row_idx, ad_col, ts)
                    logger.info(f"Supervisor reassigned: {matric} → {supervisor_name}")
                    # Also update TakenTopics
                    _sync_supervisor_to_takentopics(matric, supervisor_name)
                    return True, (
                        f"{student_name} reassigned to {supervisor_name}."
                    )

        # No existing row — append new
        ws.append_row([
            matric, student_name, programme,
            supervisor_name, assigned_by, ts, sess
        ])
        logger.info(f"Supervisor assigned: {matric} → {supervisor_name}")
        _sync_supervisor_to_takentopics(matric, supervisor_name)
        return True, f"{student_name} assigned to {supervisor_name}."

    except Exception as e:
        logger.error(f"assign_supervisor error: {e}")
        return False, str(e)


def _sync_supervisor_to_takentopics(matric, supervisor_name):
    """Update the Supervisor column in TakenTopics to match the assignment."""
    try:
        all_rows = _taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return
        header = [h.strip() for h in all_rows[0]]
        m_idx  = header.index("Matric Number")
        sv_col = header.index("Supervisor") + 1
        key    = matric.strip().lower()
        for row_idx, row in enumerate(all_rows[1:], start=2):
            cell = row[m_idx].strip().lower() if m_idx < len(row) else ""
            if cell == key:
                _taken_sheet.update_cell(row_idx, sv_col, supervisor_name)
                logger.info(
                    f"TakenTopics supervisor synced: {matric} → {supervisor_name}"
                )
                break
    except Exception as e:
        logger.warning(f"_sync_supervisor_to_takentopics error: {e}")


def bulk_assign_supervisors_old(assignments, assigned_by):
    """
    Coordinator bulk-assigns supervisors.
    assignments: list of dicts with keys: matric, student_name, programme, supervisor
    Returns (success_count, error_count)
    """
    ok_count  = 0
    err_count = 0
    for a in assignments:
        ok, _ = assign_supervisor(
            a['matric'], a['student_name'],
            a['programme'], a['supervisor'], assigned_by
        )
        if ok:
            ok_count += 1
        else:
            err_count += 1
    invalidate_cache()
    return ok_count, err_count
def bulk_assign_supervisors(assignments, assigned_by):
    """
    Coordinator bulk-assigns supervisors.
    assignments: list of dicts with keys:
        matric, student_name, programme, supervisor
    Returns (success_count, error_count, errors)
    """
    ok_count = 0
    err_count = 0
    errors = []

    for a in assignments:
        ok, msg = assign_supervisor(
            a['matric'],
            a['student_name'],
            a['programme'],
            a['supervisor'],
            assigned_by
        )

        if ok:
            ok_count += 1
        else:
            err_count += 1
            errors.append(
                f"{a['matric']} ({a['student_name']}): {msg}"
            )

    invalidate_cache()
    return ok_count, err_count, errors

def get_all_assignments():
    """Return all rows from AssignedSupervisors as list of dicts."""
    ws = _get_assignments_sheet()
    if not ws:
        return []
    try:
        return ws.get_all_records()
    except Exception as e:
        logger.error(f"get_all_assignments error: {e}")
        return []

def set_supervisor_passphrase(full_name, passphrase):
    """Set or update a supervisor's login passphrase in the Supervisors sheet."""
    try:
        ws = _get_supervisors_sheet()
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return False, "Supervisors sheet is empty."
        header = [h.strip() for h in all_rows[0]]
        n_idx  = header.index("Full Name")
        # Ensure Passphrase column exists
        if "Passphrase" not in header:
            pp_col = len(header) + 1
            ws.update_cell(1, pp_col, "Passphrase")
            header.append("Passphrase")
        else:
            pp_col = header.index("Passphrase") + 1
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[n_idx].strip().lower() == full_name.strip().lower():
                ws.update_cell(row_idx, pp_col, passphrase.strip())
                logger.info(f"Passphrase set for supervisor: {full_name}")
                return True, f"Passphrase set for {full_name}."
        return False, f"Supervisor '{full_name}' not found."
    except Exception as e:
        logger.error(f"set_supervisor_passphrase error: {e}")
        return False, str(e)


def append_log_entry(row):
    _init_sheets()
    if _log_sheet:
        # Pad row with current session if not already present
        if len(row) < 7:
            row = list(row) + [CURRENT_SESSION]
        try:
            _log_sheet.append_row(row)
            return True
        except Exception as e:
            logger.error(f"append_log_entry error: {e}")
    return False
