import os
import json
import secrets
import gspread
import logging
from datetime import datetime
from google.oauth2 import service_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CREDENTIALS_ENV_VAR = "GOOGLE_CREDENTIALS_JSON"

CURRENT_SESSION = "2025/2026"
PREVIOUS_SESSION = "2024/2025"
SESSION_CUTOFF_DATE = "2026-04-01"

MATRIC_YEAR_SESSION = {
    "022": "2023/2024",
    "023": "2024/2025",
    "024": "2025/2026",
    "025": "2026/2027",
    "026": "2027/2028",
}
_KNOWN_TEST_MATRICS = {'1234', '12234455', '123321', '7788'}

PROGRAM_SHEET_MAP = {
    "HND Software & Web Development":   "AvailableHNDSWDtopics",
    "HND Networking & Cloud Computing": "AvailableHNDNCCtopics",
    "ND Computer Science":              "AvailableNDTopics"
}

PRESENTATION_HEADERS = [
    "Matric Number", "Student Name", "Programme", "Topic Title",
    "Supervisor", "Supervisor Cleared", "Cleared By", "Cleared Date",
    "Payment Amount", "Payment Status", "Panel Accepted", "Payment Date", "Marked By", "Session"
]

STUDENTS_HEADERS_V2 = ["Matric Number", "Password Hash", "Email", "Registration Date"]
SUPERVISORS_HEADERS = ["Full Name", "Short Name", "Department", "Email", "Active", "Passphrase"]
ASSIGNMENTS_HEADERS = ["Matric Number", "Student Name", "Programme", "Supervisor", "Assigned By", "Assigned Date", "Session"]

_LOG_COL = {"Student Name": 0, "Matric Number": 1, "Programme": 2, "Topic Title": 3, "Supervisor": 4, "Action": 5, "Session": 6}

_client = None
_spreadsheet = None
_available_sheets = {}
_students_sheet = None
_settings_sheet = None
_supervisors_sheet = None
_assignments_sheet = None
_log_sheet = None
_session_sheets = {}          # cache for session‑specific tabs

# ------------------------------------------------------------
def _col_letter(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result

def _normalise_topic(t):
    if not t:
        return ""
    import re
    return re.sub(r'\s+', ' ', t.strip())

def _topic_key(t):
    return _normalise_topic(t).lower()

def _valid_session(s):
    import re
    return bool(re.match(r'^[0-9]{4}/[0-9]{4}$', s.strip())) if s else False

def _extract_registration_date(action_str):
    if not action_str:
        return ""
    import re
    m = re.search(r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})', action_str)
    return m.group(1) if m else ""

def _parse_log_row(row):
    def col(idx):
        return row[idx].strip() if idx < len(row) else ""
    raw_session = col(_LOG_COL["Session"])
    import re
    session_ok = bool(re.match(r'^[0-9]{4}/[0-9]{4}$', raw_session))
    return {
        "Student Name": col(_LOG_COL["Student Name"]),
        "Matric Number": col(_LOG_COL["Matric Number"]),
        "Programme": col(_LOG_COL["Programme"]),
        "Topic Title": col(_LOG_COL["Topic Title"]),
        "Supervisor": col(_LOG_COL["Supervisor"]),
        "Action": col(_LOG_COL["Action"]),
        "Session": raw_session if session_ok else "",
    }

# ------------------------------------------------------------
def infer_session_from_matric(matric_str):
    if not matric_str:
        return PREVIOUS_SESSION
    clean = matric_str.strip()
    import re
    m = re.search(r'/([0-9]{2,3})/', clean)
    if m:
        year_code_padded = m.group(1)
        year_code = year_code_padded.lstrip('0') or '0'
        sess = MATRIC_YEAR_SESSION.get(year_code_padded) or MATRIC_YEAR_SESSION.get(year_code)
        if sess:
            return sess
    if clean not in _KNOWN_TEST_MATRICS:
        logger.warning(f"infer_session_from_matric: cannot parse year code from '{clean}'")
    return None

def infer_session_from_date(date_str):
    if not date_str or not str(date_str).strip():
        return PREVIOUS_SESSION
    from datetime import datetime
    date_str = str(date_str).strip()
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
               "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"]
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
    sess = infer_session_from_matric(matric_str)
    if sess:
        return sess
    return infer_session_from_date(date_str)

def _infer_session_for_student(matric_str, reg_date_str="", sub_date_str="", existing_session=""):
    if existing_session and _valid_session(existing_session):
        return existing_session
    sess = infer_session_from_matric(matric_str)
    if sess:
        return sess
    date_to_use = reg_date_str or sub_date_str
    if date_to_use:
        sess = infer_session_from_date(date_to_use)
        if sess:
            return sess
    return PREVIOUS_SESSION

# ------------------------------------------------------------
def get_credentials():
    creds_json = os.getenv(CREDENTIALS_ENV_VAR, "").strip()
    if creds_json:
        try:
            info = json.loads(creds_json)
        except json.JSONDecodeError as e:
            raise EnvironmentError(f"Invalid JSON in {CREDENTIALS_ENV_VAR}: {e}")
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    for secret_path in ["/etc/secrets/credentials.json", "/run/secrets/credentials.json"]:
        if os.path.isfile(secret_path):
            return service_account.Credentials.from_service_account_file(secret_path, scopes=SCOPES)
    local_file = os.path.join(os.getcwd(), "credentials.json")
    if os.path.isfile(local_file):
        return service_account.Credentials.from_service_account_file(local_file, scopes=SCOPES)
    raise EnvironmentError("No Google credentials found.")

def invalidate_cache():
    global _client, _spreadsheet, _available_sheets, _students_sheet, _settings_sheet
    global _supervisors_sheet, _assignments_sheet, _log_sheet, _session_sheets
    _client = _spreadsheet = _students_sheet = _settings_sheet = _supervisors_sheet = _assignments_sheet = _log_sheet = None
    _available_sheets = {}
    _session_sheets = {}
    logger.info("Sheet cache invalidated")

def _repair_log_header(ws=None):
    correct = ["Student Name", "Matric Number", "Programme", "Topic Title", "Supervisor", "Action", "Session"]
    target = ws or _log_sheet
    if not target:
        return
    try:
        current = [c.strip() for c in target.row_values(1)]
        if current != correct:
            target.update('A1:G1', [correct])
            logger.info("Log header repaired")
    except Exception as e:
        logger.error(f"_repair_log_header error: {e}")

def _ensure_presentation_headers(ws):
    try:
        first_row = ws.row_values(1)
        if not first_row or first_row[0] != PRESENTATION_HEADERS[0]:
            all_rows = ws.get_all_values()
            header_row_idx = None
            for idx, row in enumerate(all_rows, start=1):
                if row and row[0] == PRESENTATION_HEADERS[0]:
                    header_row_idx = idx
                    break
            if header_row_idx and header_row_idx != 1:
                header_row = ws.row_values(header_row_idx)
                ws.insert_row(header_row, 1)
                ws.delete_rows(header_row_idx + 1)
                logger.info("Moved existing header to row 1")
            else:
                ws.insert_row(PRESENTATION_HEADERS, 1)
                logger.info("Inserted missing header row")
    except Exception as e:
        logger.error(f"_ensure_presentation_headers error: {e}")

def _init_sheets():
    global _client, _spreadsheet, _students_sheet, _settings_sheet, _supervisors_sheet
    global _assignments_sheet, _log_sheet, _available_sheets
    if _client and _spreadsheet:
        return
    try:
        creds = get_credentials()
        _client = gspread.authorize(creds)
        _spreadsheet = _client.open("FinalYear2025ProjectTopics")
    except Exception as e:
        logger.error(f"Failed to connect: {e}")
        raise
    try:
        all_ws = {ws.title: ws for ws in _spreadsheet.worksheets()}
    except Exception as e:
        logger.error(f"Failed to fetch worksheets: {e}")
        raise
    # Students
    if "Students" in all_ws:
        _students_sheet = all_ws["Students"]
    else:
        _students_sheet = _spreadsheet.add_worksheet("Students", rows=1000, cols=4)
        _students_sheet.append_row(STUDENTS_HEADERS_V2)
    # Settings
    if "Settings" in all_ws:
        _settings_sheet = all_ws["Settings"]
    else:
        _settings_sheet = _spreadsheet.add_worksheet("Settings", rows=20, cols=3)
        _settings_sheet.append_row(["Key", "Value", "Updated"])
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _settings_sheet.append_row(["portal_open", "true", ts])
        _settings_sheet.append_row(["presentation_fee", "2000", ts])
        _settings_sheet.append_row(["portal_message", "The portal is currently closed. Please check back later.", ts])
    # Supervisors
    if "Supervisors" in all_ws:
        _supervisors_sheet = all_ws["Supervisors"]
    else:
        _supervisors_sheet = _spreadsheet.add_worksheet("Supervisors", rows=100, cols=6)  # added Email column
        _supervisors_sheet.append_row(SUPERVISORS_HEADERS)
    # AssignedSupervisors
    if "AssignedSupervisors" in all_ws:
        _assignments_sheet = all_ws["AssignedSupervisors"]
    else:
        _assignments_sheet = _spreadsheet.add_worksheet("AssignedSupervisors", rows=500, cols=7)
        _assignments_sheet.append_row(ASSIGNMENTS_HEADERS)
    # Log
    if "Log" in all_ws:
        _log_sheet = all_ws["Log"]
        _repair_log_header(_log_sheet)
    else:
        _log_sheet = _spreadsheet.add_worksheet("Log", rows=5000, cols=7)
        _log_sheet.append_row(["Student Name", "Matric Number", "Programme", "Topic Title", "Supervisor", "Action", "Session"])
    # Available topics sheets
    for prog, title in PROGRAM_SHEET_MAP.items():
        if title in all_ws:
            _available_sheets[prog] = all_ws[title]
        else:
            ws = _spreadsheet.add_worksheet(title=title, rows=1000, cols=2)
            ws.append_row(["Topic Title", "Session"])
            _available_sheets[prog] = ws
    logger.info("All static sheets initialised.")

def _get_taken_sheet():
    """Return cached TakenTopics sheet for current session."""
    global _session_sheets
    _init_sheets()
    tab_name = f"TakenTopics_{CURRENT_SESSION.replace('/', '_')}"
    if tab_name not in _session_sheets:
        try:
            ws = _spreadsheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = _spreadsheet.add_worksheet(tab_name, rows=1000, cols=7)
            ws.append_row(["Student Name", "Matric Number", "Programme", "Topic Title", "Supervisor", "Submission Date", "Session"])
            logger.info(f"Created {tab_name}")
        _session_sheets[tab_name] = ws
    return _session_sheets[tab_name]

def _get_presentation_sheet():
    """Return cached Presentations sheet for current session."""
    global _session_sheets
    _init_sheets()
    tab_name = f"Presentations_{CURRENT_SESSION.replace('/', '_')}"
    if tab_name not in _session_sheets:
        try:
            ws = _spreadsheet.worksheet(tab_name)
        except gspread.exceptions.WorksheetNotFound:
            ws = _spreadsheet.add_worksheet(tab_name, rows=500, cols=len(PRESENTATION_HEADERS))
            ws.append_row(PRESENTATION_HEADERS)
            logger.info(f"Created {tab_name}")
        _session_sheets[tab_name] = ws
    _ensure_presentation_headers(_session_sheets[tab_name])
    return _session_sheets[tab_name]

# ------------------------------------------------------------
# Topic management
def get_available_topics(programme=None):
    _init_sheets()
    if programme is None:
        return {prog: _read_topics_from_sheet(ws) for prog, ws in _available_sheets.items()}
    ws = _available_sheets.get(programme)
    if not ws:
        return []
    return _read_topics_from_sheet(ws)

def _read_topics_from_sheet(ws):
    try:
        all_values = ws.get_all_values()
    except Exception as e:
        logger.error(f"Could not read sheet: {e}")
        return []
    topics = []
    for row in all_values:
        if not row:
            continue
        candidate = row[0].strip()
        if not candidate or candidate.lower() in ("topic title", "topic", "topics"):
            continue
        topic = _normalise_topic(candidate)
        if topic:
            topics.append(topic)
    return topics

def is_student_registered(matric_number):
    taken = _get_taken_sheet()
    key = matric_number.strip().lower()
    try:
        for rec in taken.get_all_records():
            if str(rec.get("Matric Number", "")).strip().lower() == key:
                return True
    except Exception as e:
        logger.error(f"is_student_registered error: {e}")
    return False

def register_topic(student_name, matric_number, programme, topic_title, supervisor):
    _init_sheets()
    ws_avail = _available_sheets.get(programme)
    if not ws_avail:
        logger.warning(f"Invalid programme: {programme}")
        return False
    available_topics = _read_topics_from_sheet(ws_avail)
    topic_key = _topic_key(topic_title)
    matched_title = None
    for t in available_topics:
        if _topic_key(t) == topic_key:
            matched_title = t
            break
    if not matched_title:
        logger.warning(f"Topic not found: '{topic_title}'")
        return False
    taken_sheet = _get_taken_sheet()
    try:
        taken_keys = [_topic_key(r.get("Topic Title", "")) for r in taken_sheet.get_all_records()]
    except Exception as e:
        logger.error(f"Could not read TakenTopics: {e}")
        return False
    if topic_key in taken_keys:
        logger.warning(f"Topic already taken: '{topic_title}'")
        return False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    student_session = CURRENT_SESSION
    try:
        taken_sheet.append_row([student_name, matric_number, programme, matched_title, supervisor, ts, student_session])
    except Exception as e:
        logger.error(f"Failed to write to TakenTopics: {e}")
        return False
    try:
        all_rows = ws_avail.get_all_values()
        for idx, row in enumerate(all_rows, start=1):
            if row and _topic_key(row[0]) == topic_key:
                ws_avail.delete_rows(idx)
                break
    except Exception as e:
        logger.error(f"Could not delete topic from available: {e}")
    try:
        _log_sheet.append_row([student_name, matric_number, programme, matched_title, supervisor, "Submitted", CURRENT_SESSION])
    except Exception as e:
        logger.error(f"Could not write to Log: {e}")
    logger.info(f"Topic '{matched_title}' registered for {matric_number}")
    return True

def drop_registered_topic(matric_number, programme):
    taken_sheet = _get_taken_sheet()
    key = matric_number.strip().lower()
    try:
        rows = taken_sheet.get_all_values()
    except Exception as e:
        logger.error(f"drop_registered_topic error: {e}")
        return False, None
    for idx, row in enumerate(rows[1:], start=2):
        if str(row[1]).strip().lower() == key:
            title = row[3].strip()
            prog = row[2].strip()
            ws_avail = _available_sheets.get(prog)
            if ws_avail:
                existing_keys = [_topic_key(t) for t in _read_topics_from_sheet(ws_avail)]
                if _topic_key(title) not in existing_keys:
                    ws_avail.append_row([title, CURRENT_SESSION])
            try:
                taken_sheet.delete_rows(idx)
                _log_sheet.append_row([row[0], row[1], prog, row[3], row[4], "Dropped", CURRENT_SESSION])
            except Exception as e:
                logger.error(f"drop error: {e}")
                return False, None
            logger.info(f"Dropped '{title}' for {matric_number}")
            return True, title
    return False, None

def get_taken_topics():
    taken_sheet = _get_taken_sheet()
    try:
        records = taken_sheet.get_all_records()
    except Exception as e:
        logger.error(f"get_taken_topics error: {e}")
        return []
    return records   # already only current session because sheet is session‑specific

def get_taken_topics_by_session(session=None):
    if session is None:
        session = CURRENT_SESSION
    if session == CURRENT_SESSION:
        return get_taken_topics()
    tab_name = f"TakenTopics_{session.replace('/', '_')}"
    try:
        old_sheet = _spreadsheet.worksheet(tab_name)
        return old_sheet.get_all_records()
    except gspread.exceptions.WorksheetNotFound:
        return []

def update_student_session(matric_number, new_session):
    if new_session != CURRENT_SESSION:
        return False
    taken_sheet = _get_taken_sheet()
    key = matric_number.strip().lower()
    try:
        all_rows = taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        m_idx = header.index("Matric Number")
        s_idx = header.index("Session")
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[m_idx].strip().lower() == key:
                taken_sheet.update_cell(row_idx, s_idx+1, new_session)
                return True
    except Exception as e:
        logger.error(f"update_student_session error: {e}")
    return False

def edit_student_record(orig_matric, new_name, new_matric, new_prog, new_topic, new_supervisor, new_session):
    if new_session != CURRENT_SESSION:
        return False
    taken_sheet = _get_taken_sheet()
    key = orig_matric.strip().lower()
    try:
        all_rows = taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        col = {h: i+1 for i, h in enumerate(header)}
        for row_idx, row in enumerate(all_rows[1:], start=2):
            m_idx = col.get('Matric Number', 2)-1
            if row[m_idx].strip().lower() == key:
                updates = {'Student Name': new_name, 'Matric Number': new_matric,
                           'Programme': new_prog, 'Topic Title': new_topic,
                           'Supervisor': new_supervisor, 'Session': new_session}
                for field, value in updates.items():
                    if field in col:
                        taken_sheet.update_cell(row_idx, col[field], value)
                # Update presentations
                pres_sheet = _get_presentation_sheet()
                if pres_sheet:
                    p_rows = pres_sheet.get_all_values()
                    p_hdr = [h.strip() for h in p_rows[0]]
                    if "Matric Number" in p_hdr:
                        pm = p_hdr.index("Matric Number")
                        for p_idx, p_row in enumerate(p_rows[1:], start=2):
                            if p_row[pm].strip().lower() == orig_matric.strip().lower():
                                for field, val in [("Matric Number", new_matric), ("Student Name", new_name),
                                                   ("Programme", new_prog), ("Topic Title", new_topic),
                                                   ("Supervisor", new_supervisor), ("Session", new_session)]:
                                    if field in p_hdr:
                                        pres_sheet.update_cell(p_idx, p_hdr.index(field)+1, val)
                                break
                invalidate_cache()
                return True
    except Exception as e:
        logger.error(f"edit_student_record error: {e}")
    return False

def backfill_submission_dates():
    return {"filled": 0, "skipped": 0, "errors": 0}

def backfill_sessions():
    return {"rebuilt": 0, "skipped": 0, "errors": 0}

# ------------------------------------------------------------
# Student account functions
def ensure_students_sheet_exists():
    _init_sheets()
    return _students_sheet is not None

def student_exists_v2(matric):
    if not _students_sheet:
        return False
    key = matric.strip().lower()
    try:
        all_rows = _students_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), 0)
        for row in all_rows[1:]:
            cell = row[m_idx].strip().lower() if m_idx < len(row) else ""
            if cell == key:
                return True
    except Exception as e:
        logger.error(f"student_exists_v2 error: {e}")
    return False

def register_student_v2(matric, email, password_hash):
    _init_sheets()
    if not _students_sheet:
        return False, "Students sheet not available."
    if student_exists_v2(matric):
        return False, f"Account already exists for {matric}."
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _students_sheet.append_row([matric.strip(), password_hash, email.strip(), ts])
        identity = lookup_student_in_master(matric)
        student_name = identity.get("Student Name", matric) if identity else matric
        _log_sheet.append_row([student_name, matric, identity.get("Programme", "") if identity else "",
                               "N/A", "System", f"Student Registration: {ts}", CURRENT_SESSION])
        return True, "Account created."
    except Exception as e:
        logger.error(f"register_student_v2 error: {e}")
        return False, str(e)

def verify_student_v2(matric, password):
    from werkzeug.security import check_password_hash
    _init_sheets()
    if not _students_sheet:
        return None
    key = matric.strip().lower()
    try:
        all_rows = _students_sheet.get_all_values()
        if len(all_rows) < 2:
            return None
        header = [h.strip() for h in all_rows[0]]
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), None)
        ph_idx = next((i for i, h in enumerate(header) if 'password' in h.lower() or 'hash' in h.lower()), None)
        em_idx = next((i for i, h in enumerate(header) if 'email' in h.lower()), None)
        if m_idx is None or ph_idx is None:
            return None
        for row in all_rows[1:]:
            if row[m_idx].strip().lower() == key:
                stored_hash = row[ph_idx].strip()
                if not stored_hash or not check_password_hash(stored_hash, password):
                    return None
                identity = lookup_student_in_master(matric)
                if not identity:
                    identity = {"Student Name": matric, "Matric Number": matric, "Programme": "", "Assigned Supervisor": ""}
                identity["email"] = row[em_idx].strip() if em_idx and em_idx < len(row) else ""
                return identity
    except Exception as e:
        logger.error(f"verify_student_v2 error: {e}")
    return None

def update_student_password(matric, new_hash):
    if not _students_sheet:
        return False
    key = matric.strip().lower()
    try:
        all_rows = _students_sheet.get_all_values()
        if len(all_rows) < 2:
            return False
        header = [h.strip() for h in all_rows[0]]
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), 0)
        ph_col = next((i for i, h in enumerate(header) if 'password' in h.lower() or 'hash' in h.lower()), 1) + 1
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[m_idx].strip().lower() == key:
                _students_sheet.update_cell(row_idx, ph_col, new_hash)
                return True
    except Exception as e:
        logger.error(f"update_student_password error: {e}")
    return False

def get_student_email(matric):
    """Return student email from Students sheet."""
    _init_sheets()
    if not _students_sheet:
        return None
    key = matric.strip().lower()
    try:
        all_rows = _students_sheet.get_all_values()
        if len(all_rows) < 2:
            return None
        header = [h.strip() for h in all_rows[0]]
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), None)
        e_idx = next((i for i, h in enumerate(header) if 'email' in h.lower()), None)
        if m_idx is None or e_idx is None:
            return None
        for row in all_rows[1:]:
            if len(row) > m_idx and row[m_idx].strip().lower() == key:
                return row[e_idx].strip()
    except Exception as e:
        logger.error(f"get_student_email error: {e}")
    return None

# Password reset token functions

PASSWORD_RESET_SHEET = "PasswordResetTokens"

def create_password_reset_token(matric):
    """Create a one-time token for password reset and store it in the sheet."""
    import secrets
    token = secrets.token_urlsafe(32)
    expiry = datetime.now().timestamp() + 1800  # 30 minutes from now
    
    _init_sheets()   # important!
    try:
        ws = _spreadsheet.worksheet(PASSWORD_RESET_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = _spreadsheet.add_worksheet(PASSWORD_RESET_SHEET, rows=100, cols=3)
        ws.append_row(["token", "matric", "expiry"])
    
    ws.append_row([token, matric, expiry])
    logger.info(f"Password reset token created for {matric}")
    return token

def verify_reset_token(token):
    """Return matric if token is valid and not expired, else None. Does NOT delete."""
    _init_sheets()
    try:
        ws = _spreadsheet.worksheet(PASSWORD_RESET_SHEET)
        records = ws.get_all_records()
        for rec in records:
            if rec.get("token") == token:
                expiry = float(rec.get("expiry", 0))
                if datetime.now().timestamp() < expiry:
                    matric = rec.get("matric")
                    logger.info(f"Valid token for {matric}")
                    return matric
                else:
                    logger.info(f"Expired token for {rec.get('matric')}")
                    # Optionally delete expired tokens here, but not necessary
                    return None
    except Exception as e:
        logger.error(f"verify_reset_token error: {e}")
    return None

def delete_reset_token(token):
    """Remove the token from the sheet (after successful password reset)."""
    _init_sheets()
    try:
        ws = _spreadsheet.worksheet(PASSWORD_RESET_SHEET)
        all_rows = ws.get_all_values()
        for idx, row in enumerate(all_rows, start=1):
            if len(row) > 0 and row[0] == token:
                ws.delete_rows(idx)
                logger.info(f"Deleted token {token}")
                return True
    except Exception as e:
        logger.error(f"delete_reset_token error: {e}")
    return False


def get_student_email_by_matric(matric):
    """Get student email from Students sheet using matric number."""
    _init_sheets()
    if not _students_sheet:
        return None
    key = matric.strip().lower()
    try:
        all_rows = _students_sheet.get_all_values()
        if len(all_rows) < 2:
            return None
        header = [h.strip() for h in all_rows[0]]
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), None)
        e_idx = next((i for i, h in enumerate(header) if 'email' in h.lower()), None)
        if m_idx is None or e_idx is None:
            return None
        for row in all_rows[1:]:
            if len(row) > m_idx and row[m_idx].strip().lower() == key:
                return row[e_idx].strip()
    except Exception as e:
        logger.error(f"get_student_email_by_matric error: {e}")
    return None

def get_student_name(matric):
    """Return student name from AssignedSupervisors sheet."""
    identity = lookup_student_in_master(matric)
    return identity.get("Student Name", "") if identity else ""

def migrate_students_sheet():
    return True, "Already migrated."

def lookup_student_in_master(matric):
    """Fetch student details from AssignedSupervisors sheet with robust column matching."""
    _init_sheets()   # ✅ CRITICAL FIX – ensures the sheet is loaded
    
    if not _assignments_sheet:
        logger.error("lookup_student_in_master: AssignedSupervisors sheet not available after init")
        return None
    
    key = matric.strip().lower()
    try:
        all_rows = _assignments_sheet.get_all_values()
        if len(all_rows) < 2:
            logger.warning("AssignedSupervisors sheet has no data rows")
            return None
        
        header = [h.strip() for h in all_rows[0]]
        logger.info(f"AssignedSupervisors headers: {header}")
        
        # Find column indices (flexible matching)
        m_idx = next((i for i, h in enumerate(header) if 'matric' in h.lower()), None)
        n_idx = next((i for i, h in enumerate(header) if 'name' in h.lower()), None)
        p_idx = next((i for i, h in enumerate(header) if 'programme' in h.lower() or 'program' in h.lower()), None)
        s_idx = next((i for i, h in enumerate(header) if 'supervisor' in h.lower()), None)
        
        if m_idx is None:
            logger.error("lookup_student_in_master: no column with 'matric' found")
            return None
        
        # Log sample matric numbers for debugging
        sample = []
        for row in all_rows[1:11]:
            if len(row) > m_idx and row[m_idx].strip():
                sample.append(row[m_idx].strip())
        logger.info(f"Sample matric numbers from sheet: {sample}")
        
        for row in all_rows[1:]:
            if len(row) <= m_idx:
                continue
            cell = row[m_idx].strip().lower() if row[m_idx] else ""
            if cell == key:
                logger.info(f"Matched student: {row}")
                return {
                    "Student Name": row[n_idx].strip() if n_idx is not None and len(row) > n_idx else "",
                    "Matric Number": row[m_idx].strip(),
                    "Programme": row[p_idx].strip() if p_idx is not None and len(row) > p_idx else "",
                    "Assigned Supervisor": row[s_idx].strip() if s_idx is not None and len(row) > s_idx else "",
                }
        logger.warning(f"Matric {matric} not found in AssignedSupervisors")
    except Exception as e:
        logger.error(f"lookup_student_in_master error: {e}")
    return None

def get_all_registered_students():
    if not _students_sheet:
        return []
    try:
        records = _students_sheet.get_all_records()
        result = []
        for rec in records:
            matric = rec.get("Matric Number") or rec.get("Matric") or ""
            if matric:
                result.append({
                    "Student Name": rec.get("Full Name") or rec.get("Student Name") or "",
                    "Matric Number": matric.strip(),
                    "Programme": rec.get("Programme") or "",
                    "Email": rec.get("Email", "")
                })
        return result
    except Exception as e:
        logger.error(f"get_all_registered_students: {e}")
        return []

def find_student_record(matric_number):
    return None

# ------------------------------------------------------------
# Presentation / Clearance / Payment
def get_presentation_records(session_filter=None):
    if session_filter and session_filter != CURRENT_SESSION:
        tab_name = f"Presentations_{session_filter.replace('/', '_')}"
        try:
            ws = _spreadsheet.worksheet(tab_name)
            records = ws.get_all_records()
            for r in records:
                r["eligible"] = (r.get("Supervisor Cleared", "").lower() == "yes" and r.get("Payment Status", "").lower() == "paid")
            return records
        except gspread.exceptions.WorksheetNotFound:
            return []
    ws = _get_presentation_sheet()
    try:
        records = ws.get_all_records()
        for r in records:
            r["eligible"] = (r.get("Supervisor Cleared", "").lower() == "yes" and r.get("Payment Status", "").lower() == "paid")
        return records
    except Exception as e:
        logger.error(f"get_presentation_records error: {e}")
        return []

def _find_presentation_row(ws, matric):
    key = matric.strip().lower()
    try:
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return None, None
        header = [h.strip() for h in all_rows[0]]
        try:
            m_idx = header.index("Matric Number")
        except ValueError:
            return None, None
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[m_idx].strip().lower() == key:
                rec = {col: (row[i].strip() if i < len(row) else "") for i, col in enumerate(header)}
                return row_idx, rec
    except Exception as e:
        logger.error(f"_find_presentation_row error: {e}")
    return None, None

def ensure_presentation_record(matric):
    ws = _get_presentation_sheet()
    row_idx, rec = _find_presentation_row(ws, matric)
    if row_idx:
        return ws, row_idx, rec
    taken = get_taken_topics()
    student = next((r for r in taken if r.get("Matric Number","").strip().lower() == matric.strip().lower()), None)
    if not student:
        return ws, None, None
    new_row = [
        student.get("Matric Number",""), student.get("Student Name",""), student.get("Programme",""),
        student.get("Topic Title",""), student.get("Supervisor",""), "No", "", "", "", "Unpaid", "", "", CURRENT_SESSION
    ]
    ws.append_row(new_row)
    return _find_presentation_row(ws, matric)

def supervisor_clear_student(matric, cleared_by):
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, f"Student {matric} not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        sc_col = header.index("Supervisor Cleared") + 1
        cb_col = header.index("Cleared By") + 1
        cd_col = header.index("Cleared Date") + 1
    except ValueError as e:
        return False, f"Missing column: {e}"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        ws.update([[ "Yes", cleared_by, ts ]], f"{_col_letter(sc_col)}{row_idx}:{_col_letter(cd_col)}{row_idx}")
        return True, f"{rec.get('Student Name', matric)} cleared."
    except Exception:
        try:
            ws.update_cell(row_idx, sc_col, "Yes")
            ws.update_cell(row_idx, cb_col, cleared_by)
            ws.update_cell(row_idx, cd_col, ts)
            return True, f"{rec.get('Student Name', matric)} cleared."
        except Exception as e2:
            return False, str(e2)

def coordinator_unclear_student(matric):
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, "Student not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        sc_col = header.index("Supervisor Cleared") + 1
        cb_col = header.index("Cleared By") + 1
        cd_col = header.index("Cleared Date") + 1
    except ValueError:
        return False, "Column missing"
    ws.update_cell(row_idx, sc_col, "No")
    ws.update_cell(row_idx, cb_col, "")
    ws.update_cell(row_idx, cd_col, "")
    return True, "Clearance reversed."

def record_payment(matric, amount, marked_by):
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, "Student not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        pa_col = header.index("Payment Amount") + 1
        ps_col = header.index("Payment Status") + 1
        pd_col = header.index("Payment Date") + 1
        mb_col = header.index("Marked By") + 1
    except ValueError:
        return False, "Column missing"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ws.update_cell(row_idx, pa_col, str(amount))
    ws.update_cell(row_idx, ps_col, "Paid")
    ws.update_cell(row_idx, pd_col, ts)
    ws.update_cell(row_idx, mb_col, marked_by)
    return True, f"Payment recorded: ₦{amount}"

def reverse_payment(matric):
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, "Student not found."
    header = [h.strip() for h in ws.row_values(1)]
    try:
        ps_col = header.index("Payment Status") + 1
        pa_col = header.index("Payment Amount") + 1
        pd_col = header.index("Payment Date") + 1
        mb_col = header.index("Marked By") + 1
    except ValueError:
        return False, "Column missing"
    ws.update_cell(row_idx, ps_col, "Unpaid")
    ws.update_cell(row_idx, pa_col, "")
    ws.update_cell(row_idx, pd_col, "")
    ws.update_cell(row_idx, mb_col, "")
    return True, "Payment reversed."

def sync_presentations_from_registered():
    created = 0
    for student in get_taken_topics():
        matric = student.get("Matric Number","")
        if not matric:
            continue
        ws, row_idx, _ = ensure_presentation_record(matric)
        if row_idx:
            continue
        created += 1
    return created

def set_panel_accepted(matric, accepted=True, marked_by="Coordinator"):
    """Mark a student's topic as accepted by the panel – only if eligible (cleared + paid)."""
    ws, row_idx, rec = ensure_presentation_record(matric)
    if not row_idx:
        return False, "Student not found"
    
    # Check eligibility: Supervisor Cleared = Yes AND Payment Status = Paid
    cleared = rec.get("Supervisor Cleared", "").strip().lower() == "yes"
    paid = rec.get("Payment Status", "").strip().lower() == "paid"
    if not (cleared and paid):
        return False, "Student must be cleared by supervisor AND have paid before panel acceptance."
    
    header = [h.strip() for h in ws.row_values(1)]
    try:
        pa_col = header.index("Panel Accepted") + 1
    except ValueError:
        # Add column if missing
        current_cols = len(header)
        new_col = current_cols + 1
        ws.add_cols(1)
        ws.update_cell(1, new_col, "Panel Accepted")
        pa_col = new_col
        header.append("Panel Accepted")
    
    value = "Yes" if accepted else "No"
    ws.update_cell(row_idx, pa_col, value)
    logger.info(f"Panel acceptance set to {value} for {matric} by {marked_by}")
    return True, f"Panel acceptance updated for {rec.get('Student Name', matric)}"

def clean_presentations_sheet():
    ws = _get_presentation_sheet()
    taken = get_taken_topics()
    taken_matrics = {s.get("Matric Number","").strip().lower() for s in taken}
    removed, fixed = 0, 0
    try:
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return 0, 0
        header = [h.strip() for h in all_rows[0]]
        m_idx = header.index("Matric Number")
        s_idx = header.index("Session")
        for row_idx in range(len(all_rows)-1, 0, -1):
            row = all_rows[row_idx]
            if not any(c.strip() for c in row):
                continue
            matric = row[m_idx].strip().lower() if m_idx < len(row) else ""
            if not matric:
                continue
            if matric not in taken_matrics:
                ws.delete_rows(row_idx+1)
                removed += 1
            else:
                cur_sess = row[s_idx].strip() if s_idx < len(row) else ""
                if cur_sess != CURRENT_SESSION:
                    ws.update_cell(row_idx+1, s_idx+1, CURRENT_SESSION)
                    fixed += 1
        return removed, fixed
    except Exception as e:
        logger.error(f"clean_presentations_sheet error: {e}")
        return 0, 0

# ------------------------------------------------------------
# Settings
def _get_settings_sheet():
    _init_sheets()
    return _settings_sheet

def get_setting(key, default=""):
    ws = _get_settings_sheet()
    if not ws:
        return default
    try:
        for rec in ws.get_all_records():
            if rec.get("Key","").strip() == key:
                return str(rec.get("Value", default)).strip()
    except Exception:
        pass
    return default

def set_setting(key, value):
    ws = _get_settings_sheet()
    if not ws:
        return False
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        all_rows = ws.get_all_values()
        if len(all_rows) >= 2:
            header = [h.strip() for h in all_rows[0]]
            k_idx = header.index("Key")
            v_idx = header.index("Value") + 1
            u_idx = header.index("Updated") + 1
            for row_idx, row in enumerate(all_rows[1:], start=2):
                if row[k_idx].strip() == key:
                    ws.update_cell(row_idx, v_idx, str(value))
                    ws.update_cell(row_idx, u_idx, ts)
                    return True
        ws.append_row([key, str(value), ts])
        return True
    except Exception as e:
        logger.error(f"set_setting error: {e}")
        return False

def is_portal_open():
    return get_setting("portal_open", "true").lower() == "true"

def get_portal_message():
    return get_setting("portal_message", "Portal closed.")

def get_presentation_fee():
    try:
        return int(get_setting("presentation_fee", "2000"))
    except ValueError:
        return 2000

# ------------------------------------------------------------
# Supervisors
def _get_supervisors_sheet():
    _init_sheets()
    return _supervisors_sheet

def get_supervisors(active_only=True):
    ws = _get_supervisors_sheet()
    if not ws:
        return []
    try:
        records = ws.get_all_records()
        if active_only:
            records = [r for r in records if str(r.get("Active","Yes")).strip().lower() != "no"]
        return sorted(records, key=lambda r: r.get("Full Name",""))
    except Exception as e:
        logger.error(f"get_supervisors error: {e}")
        return []

def get_supervisor_names():
    return [r.get("Full Name","") for r in get_supervisors(active_only=True)]

def add_supervisor(full_name, short_name="", department="", active="Yes"):
    ws = _get_supervisors_sheet()
    if not ws:
        return False, "Sheet missing"
    try:
        existing = [r.get("Full Name","").strip().lower() for r in ws.get_all_records()]
        if full_name.strip().lower() in existing:
            return False, "Already exists"
        ws.append_row([full_name.strip(), short_name.strip(), department.strip(), active, ""])
        return True, "Added"
    except Exception as e:
        return False, str(e)

def update_supervisor_status(full_name, active):
    ws = _get_supervisors_sheet()
    if not ws:
        return False, "Sheet missing"
    try:
        all_rows = ws.get_all_values()
        header = [h.strip() for h in all_rows[0]]
        n_idx = header.index("Full Name")
        a_col = header.index("Active") + 1
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[n_idx].strip().lower() == full_name.strip().lower():
                ws.update_cell(row_idx, a_col, active)
                return True, "Updated"
        return False, "Not found"
    except Exception as e:
        return False, str(e)

def set_supervisor_passphrase(full_name, passphrase):
    ws = _get_supervisors_sheet()
    if not ws:
        return False, "Sheet missing"
    try:
        all_rows = ws.get_all_values()
        header = [h.strip() for h in all_rows[0]]
        n_idx = header.index("Full Name")
        if "Passphrase" not in header:
            pp_col = len(header) + 1
            ws.update_cell(1, pp_col, "Passphrase")
            header.append("Passphrase")
        else:
            pp_col = header.index("Passphrase") + 1
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[n_idx].strip().lower() == full_name.strip().lower():
                ws.update_cell(row_idx, pp_col, passphrase.strip())
                return True, "Passphrase set"
        return False, "Supervisor not found"
    except Exception as e:
        return False, str(e)

def verify_supervisor(passphrase):
    ws = _get_supervisors_sheet()
    if not ws:
        return None
    try:
        records = ws.get_all_records()
        ph = passphrase.strip()
        for rec in records:
            if str(rec.get("Active","Yes")).strip().lower() == "no":
                continue
            stored = str(rec.get("Passphrase","")).strip()
            if stored and ph == stored:
                return rec.get("Full Name","").strip()
    except Exception as e:
        logger.error(f"verify_supervisor error: {e}")
    return None

def get_supervisor_email(supervisor_name):
    """Return the email address of a supervisor by their full name."""
    ws = _get_supervisors_sheet()
    if not ws:
        return None
    try:
        records = ws.get_all_records()
        for rec in records:
            if rec.get("Full Name", "").strip().lower() == supervisor_name.strip().lower():
                return rec.get("Email", "").strip()
    except Exception as e:
        logger.error(f"get_supervisor_email error: {e}")
    return None

# Alias for consistency
get_supervisor_email_by_name = get_supervisor_email

# ------------------------------------------------------------
# Proposals
def _get_proposals_sheet():
    _init_sheets()
    try:
        return _spreadsheet.worksheet('Proposals')
    except Exception:
        ws = _spreadsheet.add_worksheet('Proposals', rows=500, cols=9)
        ws.append_row(['ID', 'Proposer', 'Type', 'Programme', 'New Topic', 'Student Matric', 'Note', 'Status', 'Submitted At'])
        logger.info("Created Proposals sheet")
        return ws

def submit_topic_proposal(proposer, proposal_type, programme, new_topic, student_matric='', note=''):
    try:
        ws = _get_proposals_sheet()
        all_rows = ws.get_all_values()
        proposal_id = str(len(all_rows))
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([proposal_id, proposer, proposal_type, programme, new_topic, student_matric, note, 'Pending', ts])
        logger.info(f"Proposal #{proposal_id} submitted by {proposer}")
        return True
    except Exception as e:
        logger.error(f"submit_topic_proposal error: {e}")
        return False

def get_topic_proposals(status=None):
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
    try:
        ws = _get_proposals_sheet()
        all_rows = ws.get_all_values()
        if len(all_rows) < 2:
            return False, "Proposals sheet is empty.", None, None, None, None
        header = [h.strip() for h in all_rows[0]]
        try:
            id_idx = header.index('ID')
            status_idx = header.index('Status') + 1
            type_idx = header.index('Type')
            prog_idx = header.index('Programme')
            topic_idx = header.index('New Topic')
            matric_idx = header.index('Student Matric')
            proposer_idx = header.index('Proposer')   # important: get proposer column
        except ValueError as e:
            return False, f"Proposals sheet missing column: {e}", None, None, None, None
        
        for row_idx, row in enumerate(all_rows[1:], start=2):
            if row[id_idx].strip() != str(proposal_id).strip():
                continue
            
            proposer_name = row[proposer_idx].strip() if proposer_idx < len(row) else ""
            proposal_type = row[type_idx].strip()
            new_topic = row[topic_idx].strip()
            
            # Get proposer email from Supervisors sheet
            proposer_email = get_supervisor_email_by_name(proposer_name) if proposer_name else None
            
            # Update status
            ws.update_cell(row_idx, status_idx, decision)
            logger.info(f"Proposal #{proposal_id} → {decision}")
            
            if decision != 'Approved':
                return True, f"Proposal #{proposal_id} rejected.", proposer_name, proposer_email, proposal_type, new_topic
            
            # Process approval
            programme = row[prog_idx].strip()
            student_matric = row[matric_idx].strip()
            
            if proposal_type == 'New Topic':
                ws_avail = _available_sheets.get(programme)
                if not ws_avail:
                    return False, f"No available topics sheet for '{programme}'.", proposer_name, proposer_email, proposal_type, new_topic
                ws_avail.append_row([new_topic, CURRENT_SESSION])
                return True, f"Topic added to {programme} pool.", proposer_name, proposer_email, proposal_type, new_topic
            
            elif proposal_type == 'Topic Change':
                taken_sheet = _get_taken_sheet()
                all_taken = taken_sheet.get_all_values()
                if len(all_taken) < 2:
                    return False, "TakenTopics empty.", proposer_name, proposer_email, proposal_type, new_topic
                taken_header = [h.strip() for h in all_taken[0]]
                m_col = taken_header.index("Matric Number")
                t_col = taken_header.index("Topic Title")
                updated = False
                for taken_idx, taken_row in enumerate(all_taken[1:], start=2):
                    if taken_row[m_col].strip().lower() == student_matric.strip().lower():
                        taken_sheet.update_cell(taken_idx, t_col+1, new_topic)
                        updated = True
                        break
                if not updated:
                    return False, f"Student {student_matric} not found in TakenTopics.", proposer_name, proposer_email, proposal_type, new_topic
                return True, f"Topic updated for {student_matric}.", proposer_name, proposer_email, proposal_type, new_topic
            
            else:
                return False, f"Unknown type: {proposal_type}", proposer_name, proposer_email, proposal_type, new_topic
        
        return False, f"Proposal #{proposal_id} not found.", None, None, None, None
    except Exception as e:
        logger.error(f"decide_topic_proposal error: {e}")
        return False, str(e), None, None, None, None

# ------------------------------------------------------------
# Assignments
def _get_assignments_sheet():
    _init_sheets()
    return _assignments_sheet

def get_assigned_supervisor(matric):
    ws = _get_assignments_sheet()
    if not ws:
        return ""
    key = matric.strip().lower()
    try:
        for rec in ws.get_all_records():
            if rec.get("Matric Number","").strip().lower() == key:
                return rec.get("Supervisor","").strip()
    except Exception:
        pass
    return ""

def get_students_for_supervisor(supervisor_name):
    ws = _get_assignments_sheet()
    if not ws:
        return []
    name_key = supervisor_name.strip().lower()
    try:
        return [rec.get("Matric Number","").strip() for rec in ws.get_all_records()
                if rec.get("Supervisor","").strip().lower() == name_key]
    except Exception:
        return []

def get_all_assignments():
    ws = _get_assignments_sheet()
    if not ws:
        return []
    try:
        return ws.get_all_records()
    except Exception as e:
        logger.error(f"get_all_assignments error: {e}")
        return []

def assign_supervisor(matric, student_name, programme, supervisor_name, assigned_by):
    ws = _get_assignments_sheet()
    if not ws:
        return False, "Sheet missing"
    key = matric.strip().lower()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sess = CURRENT_SESSION
    try:
        all_rows = ws.get_all_values()
        if len(all_rows) >= 2:
            header = [h.strip() for h in all_rows[0]]
            try:
                m_idx = header.index("Matric Number")
                sv_idx = header.index("Supervisor")
                ab_idx = header.index("Assigned By")
                ad_idx = header.index("Assigned Date")
                se_idx = header.index("Session")
            except ValueError:
                m_idx = sv_idx = ab_idx = ad_idx = se_idx = None
            if m_idx is not None:
                for row_idx, row in enumerate(all_rows[1:], start=2):
                    if row[m_idx].strip().lower() == key:
                        if sv_idx is not None:
                            ws.update_cell(row_idx, sv_idx+1, supervisor_name)
                        if ab_idx is not None:
                            ws.update_cell(row_idx, ab_idx+1, assigned_by)
                        if ad_idx is not None:
                            ws.update_cell(row_idx, ad_idx+1, ts)
                        if se_idx is not None:
                            ws.update_cell(row_idx, se_idx+1, sess)
                        return True, f"Updated assignment for {student_name}"
        ws.append_row([matric, student_name, programme, supervisor_name, assigned_by, ts, sess])
        return True, f"Assigned {student_name} to {supervisor_name}"
    except Exception as e:
        logger.error(f"assign_supervisor error: {e}")
        return False, str(e)

def sync_all_assignments():
    taken_sheet = _get_taken_sheet()
    assignments = get_all_assignments()
    assign_map = {a.get("Matric Number","").strip().lower(): a.get("Supervisor","").strip() for a in assignments}
    try:
        all_rows = taken_sheet.get_all_values()
        if len(all_rows) < 2:
            return 0
        header = [h.strip() for h in all_rows[0]]
        m_idx = header.index("Matric Number")
        sv_col = header.index("Supervisor") + 1
        updated = 0
        for row_idx, row in enumerate(all_rows[1:], start=2):
            matric = row[m_idx].strip().lower()
            if matric in assign_map:
                new_sup = assign_map[matric]
                taken_sheet.update_cell(row_idx, sv_col, new_sup)
                updated += 1
        return updated
    except Exception as e:
        logger.error(f"sync_all_assignments error: {e}")
        return 0

def bulk_assign_supervisors(assignments, assigned_by):
    ok = 0
    errors = []
    for a in assignments:
        success, msg = assign_supervisor(a['matric'], a['student_name'], a['programme'], a['supervisor'], assigned_by)
        if success:
            ok += 1
        else:
            errors.append(msg)
    sync_all_assignments()
    return ok, len(errors), errors

def delete_student_record(matric, reason=""):
    taken_sheet = _get_taken_sheet()
    key = matric.strip().lower()
    try:
        rows = taken_sheet.get_all_values()
        if len(rows) < 2:
            return False, "Not found"
        header = [h.strip() for h in rows[0]]
        m_idx = header.index("Matric Number")
        t_idx = header.index("Topic Title")
        p_idx = header.index("Programme")
        n_idx = header.index("Student Name")
        sv_idx = header.index("Supervisor")
        del_row_idx = None
        topic_title = programme = student_name = supervisor = ""
        for idx, row in enumerate(rows[1:], start=2):
            if row[m_idx].strip().lower() == key:
                del_row_idx = idx
                topic_title = row[t_idx].strip() if t_idx < len(row) else ""
                programme = row[p_idx].strip() if p_idx < len(row) else ""
                student_name = row[n_idx].strip() if n_idx < len(row) else ""
                supervisor = row[sv_idx].strip() if sv_idx < len(row) else ""
                break
        if not del_row_idx:
            return False, "Matric not found"
        if topic_title and programme:
            ws_avail = _available_sheets.get(programme)
            if ws_avail:
                existing = [_topic_key(t) for t in _read_topics_from_sheet(ws_avail)]
                if _topic_key(topic_title) not in existing:
                    ws_avail.append_row([topic_title, CURRENT_SESSION])
        taken_sheet.delete_rows(del_row_idx)
        # Remove from Presentations
        pres_sheet = _get_presentation_sheet()
        if pres_sheet:
            p_rows = pres_sheet.get_all_values()
            if len(p_rows) >= 2:
                p_hdr = [h.strip() for h in p_rows[0]]
                pm = p_hdr.index("Matric Number")
                for p_idx, p_row in enumerate(p_rows[1:], start=2):
                    if p_row[pm].strip().lower() == key:
                        pres_sheet.delete_rows(p_idx)
                        break
        # Remove from assignments
        assign_sheet = _get_assignments_sheet()
        if assign_sheet:
            a_rows = assign_sheet.get_all_values()
            if len(a_rows) >= 2:
                a_hdr = [h.strip() for h in a_rows[0]]
                am = a_hdr.index("Matric Number")
                for a_idx, a_row in enumerate(a_rows[1:], start=2):
                    if a_row[am].strip().lower() == key:
                        assign_sheet.delete_rows(a_idx)
                        break
        _log_sheet.append_row([student_name, matric, programme, topic_title, supervisor, f"Deleted: {reason}", CURRENT_SESSION])
        return True, f"Deleted {student_name}"
    except Exception as e:
        logger.error(f"delete_student_record error: {e}")
        return False, str(e)

def append_log_entry(row):
    if _log_sheet:
        if len(row) < 7:
            row = list(row) + [CURRENT_SESSION]
        try:
            _log_sheet.append_row(row)
            return True
        except Exception as e:
            logger.error(f"append_log_entry error: {e}")
    return False