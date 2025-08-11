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
# Google scopes and credential env var
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CREDENTIALS_ENV_VAR = "GOOGLE_CREDENTIALS_JSON"

# Map programme name → its “Available…” sheet title
PROGRAM_SHEET_MAP = {
    "HND Software & Web Development": "AvailableHNDSWDtopics",
    "HND Networking & Cloud Computing": "AvailableHNDNCCtopics",
    "ND Computer Science": "AvailableNDTopics"
}

# Internal globals
_client = None
_spreadsheet = None
_available_sheets = {}
_taken_sheet = None
_log_sheet = None
_students_sheet = None

def get_credentials():
    """Load service account credentials from env var or fallback file."""
    creds_json = os.getenv(CREDENTIALS_ENV_VAR)
    if creds_json:
        info = json.loads(creds_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    creds_file = os.path.join(os.getcwd(), "credentials.json")
    if os.path.isfile(creds_file):
        return service_account.Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    raise EnvironmentError(f"No Google credentials found. Set {CREDENTIALS_ENV_VAR} or provide credentials.json")

def _init_sheets():
    """Authorize and open all worksheets (Available, Taken, Log, Students)."""
    global _client, _spreadsheet, _available_sheets, _taken_sheet, _log_sheet, _students_sheet
    if _client:
        return

    creds = get_credentials()
    _client = gspread.authorize(creds)
    _spreadsheet = _client.open("FinalYear2025ProjectTopics")
    all_ws = {ws.title: ws for ws in _spreadsheet.worksheets()}

    # 1) Available-topics sheets
    for prog, title in PROGRAM_SHEET_MAP.items():
        if title in all_ws:
            ws = all_ws[title]
        else:
            ws = _spreadsheet.add_worksheet(title=title, rows=1000, cols=1)
            ws.append_row(["Topic Title"])
            logger.info(f"Created sheet '{title}' for programme '{prog}'")
        _available_sheets[prog] = ws

    # 2) TakenTopics sheet with Submission Date
    if "TakenTopics" in all_ws:
        _taken_sheet = all_ws["TakenTopics"]
    else:
        _taken_sheet = _spreadsheet.add_worksheet(title="TakenTopics", rows=1000, cols=6)
        _taken_sheet.append_row([
            "Student Name",
            "Matric Number",
            "Programme",
            "Topic Title",
            "Supervisor",
            "Submission Date"
        ])
        logger.info("Created TakenTopics sheet")

    # 3) Log sheet
    if "Log" in all_ws:
        _log_sheet = all_ws["Log"]
    else:
        _log_sheet = _spreadsheet.add_worksheet(title="Log", rows=1000, cols=6)
        _log_sheet.append_row([
            "Student Name",
            "Matric Number",
            "Programme",
            "Topic Title",
            "Supervisor",
            "Action"
        ])
        logger.info("Created Log sheet")

    # 4) Students sheet (headers will be added by ensure_students_sheet_exists)
    if "Students" in all_ws:
        _students_sheet = all_ws["Students"]

def get_available_topics(programme=None):
    """
    If `programme` is provided, returns a list of that programme’s topics.
    Otherwise returns a dict of {programme: [topics]}.
    """
    _init_sheets()
    if programme is None:
        return {
            prog: [cell.strip() for cell in ws.col_values(1)[1:] if cell.strip()]
            for prog, ws in _available_sheets.items()
        }
    ws = _available_sheets.get(programme)
    if not ws:
        logger.warning(f"No available sheet for programme '{programme}'")
        return []
    return [cell.strip() for cell in ws.col_values(1)[1:] if cell.strip()]

def is_student_registered(matric_number):
    """Return True if this matric has a record in TakenTopics."""
    _init_sheets()
    key = str(matric_number).strip().lower()
    for rec in _taken_sheet.get_all_records():
        if str(rec.get("Matric Number", "")).strip().lower() == key:
            return True
    return False

def register_topic(student_name, matric_number, programme, topic_title, supervisor):
    """
    Move a topic out of the programme’s Available sheet into TakenTopics,
    stamping it with a local timestamp.
    """
    _init_sheets()
    ws_avail = _available_sheets.get(programme)
    if not ws_avail:
        logger.warning(f"Invalid programme: {programme}")
        return False

    # still-available check
    avail = [t.lower() for t in ws_avail.col_values(1)[1:]]
    if topic_title.strip().lower() not in avail:
        return False

    # not already taken check
    taken = [r["Topic Title"].strip().lower() for r in _taken_sheet.get_all_records()]
    if topic_title.strip().lower() in taken:
        return False

    # append to TakenTopics
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _taken_sheet.append_row([
        student_name,
        matric_number,
        programme,
        topic_title,
        supervisor,
        ts
    ])

    # delete from Available sheet
    col = ws_avail.col_values(1)
    for idx, val in enumerate(col[1:], start=2):
        if val.strip().lower() == topic_title.strip().lower():
            ws_avail.delete_rows(idx)
            break

    # log it
    _log_sheet.append_row([
        student_name,
        matric_number,
        programme,
        topic_title,
        supervisor,
        "Submitted"
    ])
    logger.info(f"Topic '{topic_title}' registered for {matric_number} at {ts}")
    return True

def drop_registered_topic(matric_number, programme):
    """Remove a student’s taken topic and return it to Available sheet."""
    _init_sheets()
    key = str(matric_number).strip().lower()
    rows = _taken_sheet.get_all_values()
    for idx, row in enumerate(rows[1:], start=2):
        if str(row[1]).strip().lower() == key:
            title = row[3].strip()
            prog = row[2].strip()
            ws_avail = _available_sheets.get(prog)
            if ws_avail:
                exists = [t.lower() for t in ws_avail.col_values(1)]
                if title.lower() not in exists:
                    ws_avail.append_row([title])
            _taken_sheet.delete_rows(idx)
            _log_sheet.append_row([
                row[0], row[1], prog, row[3], row[4], "Dropped"
            ])
            logger.info(f"Dropped '{title}' for {matric_number}")
            return True
    return False

def get_taken_topics():
    """
    Return every non-blank row from TakenTopics as a list of dicts with keys:
      "Student Name", "Matric Number", "Programme",
      "Topic Title", "Supervisor", "Submission Date"
    Works even if old rows only have 5 columns (no date).
    """
    _init_sheets()

    # pull absolutely everything
    all_rows = _taken_sheet.get_all_values()
    if len(all_rows) < 2:
        return []

    # first row is header (whatever columns exist)
    header = [h.strip() for h in all_rows[0]]
    # ensure we know our six logical fields
    expected = ["Student Name", "Matric Number", "Programme",
                "Topic Title", "Supervisor", "Submission Date"]

    out = []
    for row in all_rows[1:]:
        # skip rows where *all* cells are blank
        if not any(cell.strip() for cell in row):
            continue

        rec = {}
        # map whatever columns we have
        for idx, col_name in enumerate(header):
            rec[col_name] = row[idx].strip() if idx < len(row) else ""

        # fill in any missing expected keys
        for key in expected:
            rec.setdefault(key, "")

        out.append(rec)

    return out


def ensure_students_sheet_exists():
    """Create Students sheet with headers if it doesn’t already exist."""
    global _students_sheet
    _init_sheets()
    if _students_sheet:
        return True
    try:
        _students_sheet = _spreadsheet.worksheet("Students")
    except gspread.exceptions.WorksheetNotFound:
        _students_sheet = _spreadsheet.add_worksheet("Students", rows=1000, cols=10)
        _students_sheet.append_row([
            "Full Name",
            "Matric Number",
            "Programme",
            "Email",
            "Password Hash",
            "Registration Date"
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
    for rec in _students_sheet.get_all_records():
        if str(rec.get("Matric Number","")).strip().lower() == str(matric_number).strip().lower():
            return rec
    return None

def append_log_entry(row):
    _init_sheets()
    if _log_sheet:
        _log_sheet.append_row(row)
        return True
    return False
