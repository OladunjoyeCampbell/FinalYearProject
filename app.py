import os
import json
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, get_flashed_messages
)
from functools import wraps
from dotenv import load_dotenv
from google.cloud import secretmanager
from werkzeug.security import generate_password_hash

load_dotenv()

app = Flask(__name__)

# ── SECRET_KEY ────────────────────────────────────────────────────────────────
def get_secret_key():
    if os.getenv('GAE_ENV', '').startswith('standard'):
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/SECRET_KEY/versions/latest"
        resp = client.access_secret_version(request={'name': name})
        return resp.payload.data.decode('UTF-8')
    key = os.getenv('SECRET_KEY')
    if not key:
        raise RuntimeError('SECRET_KEY not set')
    return key

app.secret_key = get_secret_key()

# ── Google Sheets credentials ─────────────────────────────────────────────────
def get_google_credentials():
    if os.getenv('GAE_ENV', '').startswith('standard'):
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/google-credentials-json/versions/latest"
        resp = client.access_secret_version(request={'name': name})
        return resp.payload.data.decode('UTF-8')
    return os.getenv('GOOGLE_CREDENTIALS_JSON')

# ── Passphrases ───────────────────────────────────────────────────────────────
#
# Two separate passphrases control access:
#
#   COORDINATOR_PASSPHRASE  — Project Coordinator (full admin access)
#                             Set this in .env and Render environment.
#                             If not set, falls back to STAFF_PASSPHRASE.
#
#   STAFF_PASSPHRASE        — Supervisors (read-only + propose topics)
#                             This is the existing passphrase supervisors
#                             already know. Keep it unchanged.
#
# The login page accepts EITHER passphrase and assigns the correct role.

def _get_env(var):
    """Read from environment. Works locally (.env) and on Render."""
    return (os.getenv(var) or '').strip()

def _get_gcp_secret(secret_name):
    """Read from GCP Secret Manager when running on App Engine."""
    try:
        project = os.getenv('GOOGLE_CLOUD_PROJECT')
        client  = secretmanager.SecretManagerServiceClient()
        name    = f"projects/{project}/secrets/{secret_name}/versions/latest"
        resp    = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("UTF-8").strip()
    except Exception:
        return ''

def get_coordinator_passphrase():
    """
    Project Coordinator passphrase.
    Reads COORDINATOR_PASSPHRASE; falls back to STAFF_PASSPHRASE if not set.
    """
    if os.getenv('GAE_ENV', '').startswith('standard'):
        return _get_gcp_secret('coordinator-passphrase') or                _get_gcp_secret('staff-passphrase')
    return _get_env('COORDINATOR_PASSPHRASE') or _get_env('STAFF_PASSPHRASE')

def get_supervisor_passphrase():
    """
    Supervisor passphrase — the existing STAFF_PASSPHRASE value.
    Supervisors continue using the same passphrase they already know.
    """
    if os.getenv('GAE_ENV', '').startswith('standard'):
        return _get_gcp_secret('staff-passphrase')
    return _get_env('STAFF_PASSPHRASE')

# Legacy alias
def get_staff_passphrase():
    return get_coordinator_passphrase()


# ── Business logic imports ────────────────────────────────────────────────────
import sheet
from sheet import (
    CURRENT_SESSION,
    PREVIOUS_SESSION,
    backfill_sessions,
    update_student_session,
    edit_student_record,
    backfill_submission_dates,
    submit_topic_proposal,
    get_presentation_records,
    supervisor_clear_student,
    coordinator_unclear_student,
    record_payment,
    reverse_payment,
    sync_presentations_from_registered,
    ensure_presentation_record,
    invalidate_cache,
    verify_supervisor,
    get_assigned_supervisor,
    get_students_for_supervisor,
    assign_supervisor,
    bulk_assign_supervisors,
    get_all_assignments,
    set_supervisor_passphrase,
    is_portal_open,
    get_portal_message,
    get_presentation_fee,
    get_setting,
    set_setting,
    get_supervisors,
    get_supervisor_names,
    add_supervisor,
    update_supervisor_status,
    get_topic_proposals,
    decide_topic_proposal,
    get_available_topics,
    get_taken_topics,
    get_taken_topics_by_session,
    register_topic,
    is_student_registered,
    drop_registered_topic,
    find_student_record,
    ensure_students_sheet_exists,
)
from model import register_student, verify_student, student_exists

ensure_students_sheet_exists()

@app.context_processor
def inject_globals():
    return {
        'current_year':     datetime.now().year,
        'current_session':  CURRENT_SESSION,
        'user_role':        session.get('role', ''),
        'is_coordinator':   session.get('role') == 'coordinator',
        'is_supervisor':    session.get('role') == 'supervisor',
    }

PROGRAMMES = [
    'HND Software & Web Development',
    'HND Networking & Cloud Computing',
    'ND Computer Science',
]

# ── Decorators ────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def staff_required(f):
    """Allows both coordinator and supervisor roles."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') not in ('coordinator', 'supervisor'):
            flash('Staff login required.', 'warning')
            return redirect(url_for('staff_login'))
        return f(*args, **kwargs)
    return decorated

def coordinator_required(f):
    """Restricts route to Project Coordinator (admin) only."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'coordinator':
            flash('Project Coordinator access only.', 'danger')
            return redirect(url_for('coordinator_login'))
        return f(*args, **kwargs)
    return decorated

def is_coordinator():
    """Template-friendly helper — True if current user is coordinator."""
    return session.get('role') == 'coordinator'

def is_supervisor():
    return session.get('role') == 'supervisor' 

# ── Home ──────────────────────────────────────────────────────────────────────
@app.route('/')
def home():
    return render_template('index.html')

# ── Student Login ─────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        get_flashed_messages()
    if request.method == 'POST':
        m = request.form['matric_number'].strip()
        p = request.form['password']
        if not m or not p:
            flash('Enter both matric number and password.', 'danger')
        else:
            student = verify_student(m, p)
            if student:
                session.clear()
                session.update({
                    'logged_in':    True,
                    'matric_number': student['matric_number'],
                    'student_name':  student['name'],
                    'programme':     student['programme'],
                })
                flash(f"Welcome, {student['name']}!", 'success')
                return redirect(url_for('home'))
            flash('Invalid credentials.', 'danger')
    return render_template('login.html')

# ── Student Registration ──────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        full = request.form['full_name'].strip()
        m    = request.form['matric_number'].strip()
        prog = request.form['programme'].strip()
        email= request.form['email'].strip()
        pw   = request.form['password']
        cf   = request.form['confirm_password']
        if not all([full, m, prog, email, pw, cf]):
            flash('All fields are required.', 'danger')
        elif pw != cf:
            flash('Passwords do not match.', 'danger')
        elif student_exists(m):
            flash('Matric number already registered.', 'danger')
        elif register_student(full, m, prog, email, pw):
            flash('Registered — please log in.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration failed.', 'danger')
    return render_template('register.html', programmes=PROGRAMMES)

# ── Student Logout ────────────────────────────────────────────────────────────
@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

# ── Submit Topic ──────────────────────────────────────────────────────────────
@app.route('/submit-topic', methods=['GET', 'POST'])
@login_required
def submit_topic():
    m    = session['matric_number']
    prog = session['programme']
    just = session.pop('just_registered', False)

    # ── Portal open/closed check ──────────────────────────────────────────────
    if not is_portal_open():
        return render_template(
            'portal_closed.html',
            message=get_portal_message()
        )

    topics = get_available_topics(prog)
    taken  = is_student_registered(m)

    if request.method == 'POST':
        name = session['student_name']
        t    = request.form['topic_title'].strip()
        sup  = request.form['supervisor'].strip()
        if not all([name, m, prog, t, sup]):
            flash('All fields are required.', 'danger')
        elif taken:
            flash('Drop your existing topic first.', 'warning')
        elif register_topic(name, m, prog, t, sup):
            flash('Topic registered successfully!', 'success')
            session['just_registered'] = True
            return redirect(url_for('submit_topic'))
        else:
            flash('Topic unavailable or already taken. Please choose another.', 'danger')

    # Get pre-assigned supervisor for this student
    assigned_supervisor = get_assigned_supervisor(m)
    supervisors = get_supervisor_names()
    return render_template(
        'submit_topic.html',
        topics=topics,
        already_registered=taken,
        just_registered=just,
        programmes=PROGRAMMES,
        supervisors=supervisors,
        assigned_supervisor=assigned_supervisor,
    )

# ── View Available Topics (public) ────────────────────────────────────────────
@app.route('/view-topics')
def view_topics():
    prog = session.get('programme')
    if prog:
        topics = get_available_topics(prog)
    else:
        topics = {p: get_available_topics(p) for p in PROGRAMMES}
    return render_template('view_topics.html', topics=topics, programme=prog)

# ── Coordinator Login (dedicated route) ───────────────────────────────────────
@app.route('/coordinator-login', methods=['GET', 'POST'])
def coordinator_login():
    """Dedicated login page for Project Coordinator only."""
    if request.method == 'POST':
        ph       = request.form.get('passphrase', '').strip()
        coord_ph = get_coordinator_passphrase()
        app.logger.info(f"COORDINATOR LOGIN ATTEMPT — entered='{ph}' stored='{coord_ph}' match={ph == coord_ph}")
        if not ph:
            flash('Please enter a passphrase.', 'danger')
        elif coord_ph and ph == coord_ph:
            session.clear()
            session['role'] = 'coordinator'
            app.logger.info("LOGIN: coordinator role assigned via /coordinator-login")
            flash('Welcome, Project Coordinator.', 'success')
            return redirect(url_for('view_registered'))
        elif not coord_ph:
            flash('COORDINATOR_PASSPHRASE is not set on the server. Contact admin.', 'danger')
        else:
            app.logger.warning("COORDINATOR LOGIN FAILED")
            flash('Invalid coordinator passphrase.', 'danger')
    return render_template('coordinator_login.html')


# ── Staff Login (Supervisor) ───────────────────────────────────────────────────
@app.route('/staff-login', methods=['GET', 'POST'])
def staff_login():
    if request.method == 'POST':
        ph       = request.form.get('passphrase', '').strip()
        coord_ph = get_coordinator_passphrase()

        if not ph:
            flash('Please enter a passphrase.', 'danger')
        else:
            # First check if it is the coordinator passphrase
            if coord_ph and ph == coord_ph:
                session.clear()
                session['role'] = 'coordinator'
                flash('Welcome, Project Coordinator.', 'success')
                return redirect(url_for('view_registered'))

            # Otherwise check against individual supervisor passphrases
            sup_name = verify_supervisor(ph)
            if sup_name:
                session.clear()
                session['role']            = 'supervisor'
                session['supervisor_name'] = sup_name
                app.logger.info(f"SUPERVISOR LOGIN: {sup_name}")
                flash(f'Welcome, {sup_name}.', 'success')
                return redirect(url_for('supervisor_clear_student_route'))
            else:
                app.logger.warning(f"LOGIN FAILED — passphrase not matched")
                flash('Invalid passphrase.', 'danger')
    return render_template('staff_login.html')

# ── Staff Logout ───────────────────────────────────────────────────────────────
@app.route('/staff-logout')
def staff_logout():
    role = session.get('role', '')
    session.pop('role', None)
    session.pop('supervisor_name', None)
    label = 'Project Coordinator' if role == 'coordinator' else 'Supervisor'
    flash(f'{label} logged out.', 'info')
    return redirect(url_for('staff_login'))

# ── View Registered Topics ────────────────────────────────────────────────────
@app.route('/view-registered')
@staff_required
def view_registered():
    """
    Staff page showing registered topics.

    Coordinator: sees everything, all controls visible.
    Supervisor:  read-only, filtered to their own students by supervisor name.

    Supports three views via ?view= query parameter:
      current  (default) — this session only, grouped by programme
      all                — every session, grouped by programme
      past               — previous sessions only, grouped by programme

    Also supports ?programme= to filter to a single cohort.
    """
    view_mode   = request.args.get('view', 'current')
    filter_prog = request.args.get('programme', '')
    role        = session.get('role', '')

    # Fetch the right set of records
    if view_mode == 'all':
        raw = get_taken_topics()
    elif view_mode == 'past':
        raw = [r for r in get_taken_topics()
               if r.get('Session', '') != CURRENT_SESSION]
    else:
        raw = get_taken_topics_by_session(CURRENT_SESSION)

    app.logger.info(
        f"VIEW-REGISTERED | mode={view_mode} | session={CURRENT_SESSION} | rows={len(raw)}"
    )

    # ── Group into {programme: [records]} ─────────────────────────────────────
    registrations = {prog: [] for prog in PROGRAMMES}
    unmatched = []

    for rec in raw:
        prog_cell = rec.get('Programme', '').strip()

        # Optional single-programme filter
        if filter_prog and prog_cell.lower() != filter_prog.lower():
            continue

        matched = False
        for prog in PROGRAMMES:
            if prog.lower() == prog_cell.lower():
                registrations[prog].append({
                    'student_name':    rec.get('Student Name', ''),
                    'matric_number':   rec.get('Matric Number', ''),
                    'topic_title':     rec.get('Topic Title', ''),
                    'supervisor':      rec.get('Supervisor', ''),
                    'submission_date': rec.get('Submission Date', ''),
                    'session':         rec.get('Session', CURRENT_SESSION),
                })
                matched = True
                break

        if not matched:
            app.logger.warning(f"Ignored unknown programme in registered view: '{prog_cell}'")
            unmatched.append(rec)

    # Sort each group by student name for readability
    for prog in PROGRAMMES:
        registrations[prog].sort(key=lambda r: r['student_name'].lower())

    # Compute summary counts for the header bar
    total = sum(len(v) for v in registrations.values())

    return render_template(
        'view_registered.html',
        registrations=registrations,
        programmes=PROGRAMMES,
        view_mode=view_mode,
        filter_prog=filter_prog,
        current_session=CURRENT_SESSION,
        total=total,
        unmatched=unmatched,
        role=role,
        is_coordinator=(role == 'coordinator'),
    )

# ── Drop Topic ────────────────────────────────────────────────────────────────
@app.route('/drop-topic', methods=['POST'])
@login_required
def drop_topic():
    m   = session['matric_number']
    rec = find_student_record(m)
    if not rec:
        flash('No student record found.', 'warning')
        return redirect(url_for('submit_topic'))
    prog = rec.get('Programme')
    if drop_registered_topic(m, prog):
        flash('Topic dropped successfully.', 'success')
    else:
        flash('Could not drop topic. Please try again.', 'danger')
    return redirect(url_for('submit_topic'))

# ── Forgot Password ───────────────────────────────────────────────────────────
@app.route('/forgot-password', methods=['GET', 'POST'])
@app.route('/forgot_password',  methods=['GET', 'POST'])
def forgot_password():
    ensure_students_sheet_exists()
    ws = sheet._students_sheet

    if request.method == 'POST':
        m  = request.form['matric_number'].strip().upper()
        npw = request.form['new_password']
        cf  = request.form['confirm_password']

        if not all([m, npw, cf]):
            flash('All fields are required.', 'danger')
            return redirect(url_for('forgot_password'))
        if npw != cf:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('forgot_password'))

        header = ws.row_values(1)
        try:
            cidx = header.index('Password Hash') + 1
        except ValueError:
            flash('Password Hash column missing.', 'danger')
            return redirect(url_for('forgot_password'))

        records = ws.get_all_records()
        row_to_update = None
        for i, rec in enumerate(records, start=2):
            if str(rec.get('Matric Number', '')).strip().upper() == m:
                row_to_update = i
                break

        if not row_to_update:
            flash('Matric number not found.', 'danger')
            return redirect(url_for('forgot_password'))

        hashed = generate_password_hash(npw, method='scrypt')
        try:
            ws.update_cell(row_to_update, cidx, hashed)
            flash('Password updated — please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Error updating password: {e}', 'danger')
            return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html')

# ── Test endpoint ─────────────────────────────────────────────────────────────
@app.route('/test')
def test_credentials():
    js = get_google_credentials()
    if not js:
        return 'Missing GOOGLE_CREDENTIALS_JSON', 500
    try:
        info = json.loads(js)
        return f"Loaded {info.get('client_email')}"
    except json.JSONDecodeError as e:
        return f"JSON error: {e}", 500


# ── Diagnostic route — visit /diag to reveal sheet structure ──────────────────
# Remove or restrict this route before final production deployment
@app.route('/diag')
def diagnostics():
    import html
    sheet._init_sheets()
    out = []

    def section(title):
        out.append(f"<h2 style='background:#1F4E79;color:#fff;padding:6px'>{title}</h2>")

    def table(rows, caption=""):
        if not rows:
            out.append(f"<p><em>(no rows)</em></p>")
            return
        out.append(f"<p><strong>{caption}</strong></p>")
        out.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:12px'>")
        for i, row in enumerate(rows):
            bg = "#DEEAF1" if i == 0 else ("#f9f9f9" if i % 2 == 0 else "#fff")
            out.append(f"<tr style='background:{bg}'>")
            for cell in row:
                tag = "th" if i == 0 else "td"
                out.append(f"<{tag} style='padding:4px'>{html.escape(str(cell))}</{tag}>")
            out.append("</tr>")
        out.append("</table><br>")

    # ── 1. Log sheet ──────────────────────────────────────────────────────────
    section("LOG SHEET — all rows")
    try:
        log_rows = sheet._log_sheet.get_all_values()
        out.append(f"<p>Total rows (incl header): <strong>{len(log_rows)}</strong></p>")
        if log_rows:
            out.append(f"<p>Header columns: <code>{log_rows[0]}</code></p>")
        table(log_rows[:60], f"First 60 rows of Log ({len(log_rows)-1} data rows total)")
    except Exception as e:
        out.append(f"<p style='color:red'>Error reading Log: {e}</p>")

    # ── 2. TakenTopics sheet ──────────────────────────────────────────────────
    section("TAKENTOPICS SHEET — all rows")
    try:
        tt_rows = sheet._taken_sheet.get_all_values()
        out.append(f"<p>Total rows (incl header): <strong>{len(tt_rows)}</strong></p>")
        if tt_rows:
            out.append(f"<p>Header columns: <code>{tt_rows[0]}</code></p>")
        table(tt_rows[:60], f"First 60 rows of TakenTopics ({len(tt_rows)-1} data rows total)")
    except Exception as e:
        out.append(f"<p style='color:red'>Error reading TakenTopics: {e}</p>")

    # ── 3. Log replay trace ───────────────────────────────────────────────────
    section("LOG REPLAY TRACE — what get_taken_topics() sees per student")
    try:
        log_rows = sheet._log_sheet.get_all_values()
        if len(log_rows) >= 2:
            log_header = [h.strip() for h in log_rows[0]]
            out.append(f"<p>Log headers detected: <code>{log_header}</code></p>")

            trace = {}   # mkey → list of (action, topic, raw_matric)
            for row in log_rows[1:]:
                if not any(c.strip() for c in row):
                    continue
                rec = {col: (row[i].strip() if i < len(row) else "")
                       for i, col in enumerate(log_header)}
                raw_matric = rec.get("Matric Number", "")
                mkey = raw_matric.strip().lower()
                action = rec.get("Action", "").strip()
                topic  = rec.get("Topic Title", "").strip()
                if mkey not in trace:
                    trace[mkey] = []
                trace[mkey].append((action, topic, raw_matric,
                                    rec.get("Student Name",""),
                                    rec.get("Session","")))

            out.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:12px'>")
            out.append("<tr style='background:#1F4E79;color:#fff'>"
                       "<th>mkey</th><th>Raw Matric</th><th>Student Name</th>"
                       "<th>Actions (in order)</th><th>Final State</th><th>Session</th></tr>")
            for mkey, events in sorted(trace.items()):
                actions_str = " → ".join(
                    f"<span style='color:{'green' if a=='Submitted' else 'red'}'>{a}</span> ({t[:40]})"
                    for a, t, _, _, _ in events
                )
                last_action = events[-1][0].lower()
                raw_m  = events[-1][2]
                sname  = events[-1][3]
                sess   = events[-1][4]
                final_style = "color:green;font-weight:bold" if last_action=="submitted" else "color:red"
                out.append(
                    f"<tr>"
                    f"<td><code>{html.escape(mkey or '(BLANK)')}</code></td>"
                    f"<td>{html.escape(raw_m)}</td>"
                    f"<td>{html.escape(sname)}</td>"
                    f"<td>{actions_str}</td>"
                    f"<td style='{final_style}'>{last_action.upper()}</td>"
                    f"<td>{html.escape(sess)}</td>"
                    f"</tr>"
                )
            out.append("</table><br>")

            blank_keys = sum(1 for k in trace if not k)
            out.append(f"<p>Unique matric keys found: <strong>{len(trace)}</strong> "
                       f"(blank keys: <strong>{blank_keys}</strong>)</p>")
    except Exception as e:
        out.append(f"<p style='color:red'>Replay trace error: {e}</p>")

    # ── 4. Final get_taken_topics() result ────────────────────────────────────
    section("FINAL get_taken_topics() RESULT")
    try:
        final = get_taken_topics()
        out.append(f"<p>Total active registrations returned: <strong>{len(final)}</strong></p>")
        if final:
            keys = list(final[0].keys())
            out.append("<table border='1' cellpadding='4' cellspacing='0' style='border-collapse:collapse;font-size:12px'>")
            out.append("<tr style='background:#1F4E79;color:#fff'>" +
                       "".join(f"<th>{k}</th>" for k in keys) + "</tr>")
            for rec in final:
                out.append("<tr>" +
                           "".join(f"<td>{html.escape(str(rec.get(k,'')))}</td>" for k in keys) +
                           "</tr>")
            out.append("</table>")
    except Exception as e:
        out.append(f"<p style='color:red'>get_taken_topics() error: {e}</p>")

    return (
        "<html><head><title>Diagnostics</title>"
        "<style>body{font-family:Arial,sans-serif;padding:20px;font-size:13px}</style>"
        "</head><body>" +
        "".join(out) +
        "</body></html>"
    )

# ── Coordinator: Edit a student record ───────────────────────────────────────
@app.route('/admin/edit-student', methods=['GET', 'POST'])
@coordinator_required
def admin_edit_student():
    """
    Allow the Project Coordinator to edit any student's registration details:
      - Student Name
      - Matric Number
      - Programme
      - Topic Title
      - Supervisor
      - Session
    Changes are written directly to TakenTopics in Google Sheets.
    """
    from sheet import edit_student_record
    all_students = sorted(
        get_taken_topics(),
        key=lambda r: r.get('Student Name', '').lower()
    )
    sessions  = [PREVIOUS_SESSION, CURRENT_SESSION]
    programmes = ['HND Software & Web Development',
                  'HND Networking & Cloud Computing',
                  'ND Computer Science']

    if request.method == 'POST':
        orig_matric  = request.form.get('orig_matric', '').strip()
        new_name     = request.form.get('student_name', '').strip()
        new_matric   = request.form.get('matric_number', '').strip()
        new_prog     = request.form.get('programme', '').strip()
        new_topic    = request.form.get('topic_title', '').strip()
        new_super    = request.form.get('supervisor', '').strip()
        new_session  = request.form.get('session', '').strip()

        if not orig_matric:
            flash('No student selected.', 'danger')
        elif not all([new_name, new_matric, new_prog, new_topic, new_super, new_session]):
            flash('All fields are required.', 'danger')
        else:
            result = edit_student_record(
                orig_matric, new_name, new_matric,
                new_prog, new_topic, new_super, new_session
            )
            if result:
                flash(f'Record for {new_matric} updated successfully.', 'success')
            else:
                flash(f'Could not find {orig_matric} in TakenTopics.', 'warning')
        return redirect(url_for('admin_edit_student'))

    return render_template(
        'admin_edit_student.html',
        students=all_students,
        sessions=sessions,
        programmes=programmes,
        current_session=CURRENT_SESSION,
        previous_session=PREVIOUS_SESSION,
    )


# ── Supervisor: Propose a topic or topic change ────────────────────────────────
@app.route('/supervisor/propose-topic', methods=['GET', 'POST'])
@staff_required
def supervisor_propose_topic():
    """
    Supervisors can propose:
      1. A brand-new topic for the available pool
      2. A topic change for an existing student

    Proposals are written to a 'Proposals' sheet in Google Sheets
    and await Project Coordinator approval.
    """
    from sheet import submit_topic_proposal, get_topic_proposals
    all_students = sorted(
        get_taken_topics(),
        key=lambda r: r.get('Student Name', '').lower()
    )
    programmes = ['HND Software & Web Development',
                  'HND Networking & Cloud Computing',
                  'ND Computer Science']

    pending = get_topic_proposals(status='Pending') if session.get('role') == 'coordinator' else []

    if request.method == 'POST':
        proposal_type = request.form.get('proposal_type', '')
        proposer      = request.form.get('proposer_name', '').strip()
        programme     = request.form.get('programme', '').strip()
        new_topic     = request.form.get('new_topic', '').strip()
        student_matric= request.form.get('student_matric', '').strip()
        note          = request.form.get('note', '').strip()

        if not all([proposal_type, proposer, new_topic]):
            flash('Proposer name, type, and topic are required.', 'danger')
        else:
            ok = submit_topic_proposal(
                proposer=proposer,
                proposal_type=proposal_type,
                programme=programme,
                new_topic=new_topic,
                student_matric=student_matric,
                note=note
            )
            if ok:
                flash('Proposal submitted — awaiting Project Coordinator approval.', 'success')
            else:
                flash('Failed to submit proposal. Try again.', 'danger')
        return redirect(url_for('supervisor_propose_topic'))

    return render_template(
        'supervisor_propose_topic.html',
        students=all_students,
        programmes=programmes,
        pending=pending,
        is_coordinator=(session.get('role') == 'coordinator'),
    )


# ── Coordinator: Review and approve/reject proposals ──────────────────────────
@app.route('/admin/review-proposals')
@coordinator_required
def admin_review_proposals():
    from sheet import get_topic_proposals
    pending  = get_topic_proposals(status='Pending')
    approved = get_topic_proposals(status='Approved')
    rejected = get_topic_proposals(status='Rejected')
    return render_template(
        'admin_review_proposals.html',
        pending=pending,
        approved=approved,
        rejected=rejected,
    )


@app.route('/admin/decide-proposal', methods=['POST'])
@coordinator_required
def admin_decide_proposal():
    from sheet import decide_topic_proposal
    proposal_id = request.form.get('proposal_id', '').strip()
    decision    = request.form.get('decision', '').strip()   # Approved / Rejected
    if not proposal_id or decision not in ('Approved', 'Rejected'):
        flash('Invalid request.', 'danger')
        return redirect(url_for('admin_review_proposals'))
    ok, msg = decide_topic_proposal(proposal_id, decision)
    flash(msg, 'success' if ok else 'warning')
    return redirect(url_for('admin_review_proposals'))


# ── Admin: Correct a student's session ───────────────────────────────────────
@app.route('/admin/update-session', methods=['GET', 'POST'])
@coordinator_required
def admin_update_session():
    """
    Allow admin to manually place a student in the correct session.
    This is the fix for carryover students and any other session errors.
    The value set here is ALWAYS honoured — matric-based inference never
    overwrites a manually set session.
    """
    all_students = get_taken_topics()
    sessions = [PREVIOUS_SESSION, CURRENT_SESSION]

    if request.method == 'POST':
        matric  = request.form.get('matric_number', '').strip()
        new_sess = request.form.get('session', '').strip()
        if not matric or not new_sess:
            flash('Matric number and session are required.', 'danger')
        elif new_sess not in sessions:
            flash('Invalid session value.', 'danger')
        else:
            result = update_student_session(matric, new_sess)
            if result:
                flash(
                    f'Session for {matric} updated to {new_sess}.',
                    'success'
                )
            else:
                flash(
                    f'Could not find {matric} in TakenTopics. '
                    f'Run backfill first if student is missing.',
                    'warning'
                )
        return redirect(url_for('admin_update_session'))

    return render_template(
        'admin_update_session.html',
        students=sorted(all_students, key=lambda r: r.get('Student Name','').lower()),
        sessions=sessions,
        current_session=CURRENT_SESSION,
        previous_session=PREVIOUS_SESSION,
    )


# ── Backfill Submission Dates (one-time admin fix) ───────────────────────────
@app.route('/admin/backfill-dates')
@coordinator_required
def admin_backfill_dates():
    """
    Fill blank Submission Date fields in TakenTopics using Log timestamps.
    Safe to run multiple times — only updates blank cells.
    """
    try:
        result = backfill_submission_dates()
        msg = (
            f"Submission dates backfilled. "
            f"Filled: {result['filled']}, "
            f"Already set: {result['skipped']}, "
            f"Errors: {result['errors']}"
        )
        flash(msg, 'success' if result['errors'] == 0 else 'warning')
    except Exception as e:
        flash(f"Backfill dates failed: {e}", 'danger')
    return redirect(url_for('view_registered'))


# ── Backfill Session Values (one-time admin fix) ─────────────────────────────
# Visit /admin/backfill-sessions while logged in as supervisor to correct
# wrongly-assigned session values in TakenTopics and Log sheets.
# Safe to run multiple times — only updates rows where session is wrong.
@app.route('/admin/backfill-sessions')
@coordinator_required
def admin_backfill_sessions():
    try:
        result = backfill_sessions()
        msg = (
            f"TakenTopics rebuilt from Log. "
            f"Active registrations: {result['rebuilt']}, "
            f"Log rows corrected: {result.get('skipped',0)}, "
            f"Errors: {result['errors']}"
        )
        flash(msg, 'success' if result['errors'] == 0 else 'warning')
        app.logger.info(msg)
    except Exception as e:
        flash(f"Backfill failed: {e}", 'danger')
        app.logger.error(f"admin_backfill_sessions error: {e}")
    return redirect(url_for('view_registered'))


# ── Default presentation fee — coordinator can change per session ─────────────
DEFAULT_PRESENTATION_FEE = 2000   # ₦2,000


# ── Supervisor: Clear a student for presentation ──────────────────────────────
@app.route('/supervisor/clear-student', methods=['GET', 'POST'])
@staff_required
def supervisor_clear_student_route():
    """
    Supervisor/Coordinator marks CURRENT SESSION students as cleared.
    Coordinator can also mark payment from this page.
    """
    role = session.get('role', '')

    if request.method == 'POST':
        action     = request.form.get('action', '').strip()
        matric     = request.form.get('matric', '').strip()
        cleared_by = request.form.get('cleared_by', '').strip()

        if not matric:
            flash('No student selected.', 'danger')

        elif action == 'clear':
            if not cleared_by:
                flash('Please enter your name before clearing a student.', 'danger')
            else:
                # Ensure record exists first
                from sheet import ensure_presentation_record as _epr
                _epr(matric)
                ok, msg = supervisor_clear_student(matric, cleared_by)
                flash(msg, 'success' if ok else 'warning')

        elif action == 'unclear':
            ok, msg = coordinator_unclear_student(matric)
            flash(msg, 'success' if ok else 'warning')

        elif action == 'pay' and role == 'coordinator':
            fee = get_presentation_fee()
            ok, msg = record_payment(matric, fee, 'Project Coordinator')
            flash(msg, 'success' if ok else 'warning')

        elif action == 'unpay' and role == 'coordinator':
            ok, msg = reverse_payment(matric)
            flash(msg, 'success' if ok else 'warning')

        return redirect(url_for('supervisor_clear_student_route',
                                name=request.form.get('cleared_by_name', '')))

    # ── GET: build student list ───────────────────────────────────────────────
    all_registered = get_taken_topics_by_session(CURRENT_SESSION)

    # Get coordinator's assignment mappings
    assignments = get_all_assignments()
    # Build matric → assigned_supervisor lookup from AssignedSupervisors sheet
    assignment_map = {
        a.get("Matric Number","").strip().lower(): a.get("Supervisor","").strip()
        for a in assignments
    }

    if role == 'coordinator':
        # Coordinator sees all current session students
        all_students = sorted(all_registered,
                              key=lambda r: r.get('Student Name','').lower())
        my_name = 'Project Coordinator'
    else:
        # Supervisor sees ONLY students assigned to them in AssignedSupervisors
        sup_name = session.get('supervisor_name', '')
        sup_key  = sup_name.lower()
        assigned_matrics = {
            a.get("Matric Number","").strip().lower()
            for a in assignments
            if a.get("Supervisor","").strip().lower() == sup_key
        }
        all_students = sorted(
            [s for s in all_registered
             if s.get('Matric Number','').strip().lower() in assigned_matrics],
            key=lambda r: r.get('Student Name','').lower()
        )
        my_name = sup_name

    # Ensure every visible student has a Presentations row
    from sheet import _get_presentation_sheet, _find_presentation_row
    for s in all_students:
        ws = _get_presentation_sheet()
        row_idx, _ = _find_presentation_row(ws, s.get('Matric Number',''))
        if not row_idx:
            ensure_presentation_record(s.get('Matric Number',''))

    pres_records = {
        r['Matric Number'].strip().lower(): r
        for r in get_presentation_records(CURRENT_SESSION)
    }

    return render_template(
        'supervisor_clear_student.html',
        students=all_students,
        pres_records=pres_records,
        assignment_map=assignment_map,
        current_session=CURRENT_SESSION,
        is_coordinator=(role == 'coordinator'),
        supervisor_name=session.get('supervisor_name', ''),
        default_name=my_name,
        presentation_fee=get_presentation_fee(),
    )


# ── Coordinator: Manage presentations (payment + clearance override) ──────────
@app.route('/admin/manage-presentations', methods=['GET', 'POST'])
@coordinator_required
def admin_manage_presentations():
    """
    Project Coordinator page:
      - See all students with clearance and payment status
      - Mark students as paid
      - Reverse payments
      - Override clearance for absent supervisors
      - Set/change the presentation fee for this session
      - Sync presentation records from registered list
    """
    fee = int(request.args.get('fee', DEFAULT_PRESENTATION_FEE))

    if request.method == 'POST':
        action = request.form.get('action', '')
        matric = request.form.get('matric', '').strip()

        if action == 'sync':
            created = sync_presentations_from_registered()
            flash(
                f'Sync complete — {created} new presentation record(s) created.',
                'success'
            )

        elif action == 'pay':
            amount     = request.form.get('amount', str(DEFAULT_PRESENTATION_FEE))
            marked_by  = request.form.get('marked_by', 'Project Coordinator').strip()
            try:
                amount = int(amount)
            except ValueError:
                amount = DEFAULT_PRESENTATION_FEE
            ok, msg = record_payment(matric, amount, marked_by)
            flash(msg, 'success' if ok else 'warning')

        elif action == 'unpay':
            ok, msg = reverse_payment(matric)
            flash(msg, 'success' if ok else 'warning')

        elif action == 'clear':
            cleared_by = request.form.get('cleared_by',
                                          'Project Coordinator').strip()
            ok, msg = supervisor_clear_student(matric, cleared_by)
            flash(msg, 'success' if ok else 'warning')

        elif action == 'unclear':
            ok, msg = coordinator_unclear_student(matric)
            flash(msg, 'success' if ok else 'warning')

        return redirect(url_for('admin_manage_presentations',
                                fee=request.form.get('fee', fee)))

    # ── Master list: registered students this session ───────────────────────────
    # Use get_taken_topics_by_session as the authoritative source so this page
    # always matches the supervisor clear page exactly (same 84/94 count bug fix).
    registered = get_taken_topics_by_session(CURRENT_SESSION)
    reg_matrics = {s.get('Matric Number','').strip().lower() for s in registered}

    # Presentation records (clearance + payment status) keyed by matric
    all_pres = get_presentation_records(CURRENT_SESSION)
    pres_map  = {r['Matric Number'].strip().lower(): r for r in all_pres}

    # Build unified records list: one entry per registered student,
    # merged with their presentation status if it exists
    records = []
    for s in sorted(registered, key=lambda r: r.get('Student Name','').lower()):
        mkey = s.get('Matric Number','').strip().lower()
        pr   = pres_map.get(mkey, {})
        rec  = {
            'Student Name':      s.get('Student Name', ''),
            'Matric Number':     s.get('Matric Number', ''),
            'Programme':         s.get('Programme', ''),
            'Topic Title':       s.get('Topic Title', ''),
            'Supervisor':        s.get('Supervisor', ''),
            'Session':           s.get('Session', CURRENT_SESSION),
            'Supervisor Cleared':pr.get('Supervisor Cleared', 'No'),
            'Cleared By':        pr.get('Cleared By', ''),
            'Cleared Date':      pr.get('Cleared Date', ''),
            'Payment Amount':    pr.get('Payment Amount', ''),
            'Payment Status':    pr.get('Payment Status', 'Unpaid'),
            'Payment Date':      pr.get('Payment Date', ''),
            'Marked By':         pr.get('Marked By', ''),
            'eligible':          (
                pr.get('Supervisor Cleared','').lower() == 'yes'
                and pr.get('Payment Status','').lower() == 'paid'
            ),
        }
        records.append(rec)

    # Separate into categories for summary cards
    eligible     = [r for r in records if r['eligible']]
    cleared_only = [r for r in records
                    if r.get('Supervisor Cleared','').lower() == 'yes'
                    and r.get('Payment Status','').lower() != 'paid']
    paid_only    = [r for r in records
                    if r.get('Payment Status','').lower() == 'paid'
                    and r.get('Supervisor Cleared','').lower() != 'yes']
    neither      = [r for r in records
                    if r.get('Supervisor Cleared','').lower() != 'yes'
                    and r.get('Payment Status','').lower() != 'paid']

    # Students in Presentations sheet but NOT in current registered list
    # (e.g. session was corrected — they should not appear here)
    not_synced = [
        r for r in all_pres
        if r.get('Matric Number','').strip().lower() not in reg_matrics
        and r.get('Session','') == CURRENT_SESSION
    ]

    return render_template(
        'admin_manage_presentations.html',
        eligible=eligible,
        cleared_only=cleared_only,
        paid_only=paid_only,
        neither=neither,
        not_synced=not_synced,
        records=records,
        current_session=CURRENT_SESSION,
        default_fee=fee,
    )


# ── Coordinator: Printable presentation list ──────────────────────────────────
@app.route('/admin/presentation-list')
@staff_required
def admin_presentation_list():
    """
    Printable / downloadable list of students eligible for presentation.
    Accessible to both coordinator and supervisor (read-only for supervisors).
    Filter: ?status=eligible|cleared|paid|all  ?programme=...
    """
    status_filter  = request.args.get('status', 'eligible')
    programme_filter = request.args.get('programme', '')

    all_records = get_presentation_records(CURRENT_SESSION)

    if status_filter == 'eligible':
        records = [r for r in all_records if r['eligible']]
    elif status_filter == 'cleared':
        records = [r for r in all_records
                   if r.get('Supervisor Cleared','').lower() == 'yes']
    elif status_filter == 'paid':
        records = [r for r in all_records
                   if r.get('Payment Status','').lower() == 'paid']
    elif status_filter == 'uncleared':
        records = [r for r in all_records
                   if r.get('Supervisor Cleared','').lower() != 'yes']
    elif status_filter == 'unpaid':
        records = [r for r in all_records
                   if r.get('Payment Status','').lower() != 'paid']
    else:
        records = all_records

    if programme_filter:
        records = [r for r in records
                   if r.get('Programme','').lower() == programme_filter.lower()]

    # Group by programme
    grouped = {}
    for prog in PROGRAMMES:
        grouped[prog] = [r for r in records
                         if r.get('Programme','') == prog]

    return render_template(
        'admin_presentation_list.html',
        grouped=grouped,
        records=records,
        programmes=PROGRAMMES,
        status_filter=status_filter,
        programme_filter=programme_filter,
        current_session=CURRENT_SESSION,
        total=len(records),
        is_coordinator=(session.get('role') == 'coordinator'),
        generated_at=datetime.now().strftime("%d %B %Y, %I:%M %p"),
    )


# ── Coordinator: Assign Supervisors to Students ──────────────────────────────
@app.route('/admin/assign-supervisors', methods=['GET', 'POST'])
@coordinator_required
def admin_assign_supervisors():
    """
    Project Coordinator assigns supervisors to students.
    Assignments are stored in the AssignedSupervisors sheet and used by:
      - The submit-topic page (student sees pre-assigned supervisor)
      - The supervisor clear page (supervisor sees only their students)
    """
    all_students  = sorted(
        get_taken_topics_by_session(CURRENT_SESSION),
        key=lambda r: r.get('Student Name', '').lower()
    )
    supervisors   = get_supervisor_names()
    assignments   = get_all_assignments()
    assignment_map = {
        a.get("Matric Number","").strip().lower(): a.get("Supervisor","").strip()
        for a in assignments
    }

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'assign_one':
            matric   = request.form.get('matric','').strip()
            sup_name = request.form.get('supervisor','').strip()
            student  = next(
                (s for s in all_students
                 if s.get('Matric Number','').strip() == matric), None
            )
            if not student:
                flash(f'Student {matric} not found.', 'danger')
            elif not sup_name:
                flash('Please select a supervisor.', 'danger')
            else:
                ok, msg = assign_supervisor(
                    matric,
                    student.get('Student Name',''),
                    student.get('Programme',''),
                    sup_name,
                    'Project Coordinator'
                )
                flash(msg, 'success' if ok else 'warning')
                invalidate_cache()

        elif action == 'assign_bulk':
            # Bulk assign from form — one supervisor per student
            assignments_to_make = []
            for s in all_students:
                matric   = s.get('Matric Number','').strip()
                sup_name = request.form.get(f'sup_{matric}','').strip()
                if sup_name:
                    assignments_to_make.append({
                        'matric':       matric,
                        'student_name': s.get('Student Name',''),
                        'programme':    s.get('Programme',''),
                        'supervisor':   sup_name,
                    })
            ok_n, err_n, errors = bulk_assign_supervisors(
                assignments_to_make, 'Project Coordinator'
            )
            flash(
                f'Bulk assignment done: {ok_n} assigned, {err_n} errors.',
                'success' if err_n == 0 else 'warning'
            )

        return redirect(url_for('admin_assign_supervisors'))

    return render_template(
        'admin_assign_supervisors.html',
        students=all_students,
        supervisors=supervisors,
        assignment_map=assignment_map,
        current_session=CURRENT_SESSION,
    )


# ── Coordinator: Portal Settings ─────────────────────────────────────────────
@app.route('/admin/settings', methods=['GET', 'POST'])
@coordinator_required
def admin_settings():
    """
    Project Coordinator settings panel:
      - Open / Close student portal
      - Set closed-portal message
      - Set presentation fee
    """
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'toggle_portal':
            currently_open = is_portal_open()
            new_state = 'false' if currently_open else 'true'
            set_setting('portal_open', new_state)
            state_label = 'closed' if currently_open else 'opened'
            flash(f'Portal {state_label} successfully.', 'success')

        elif action == 'set_message':
            msg = request.form.get('portal_message', '').strip()
            if msg:
                set_setting('portal_message', msg)
                flash('Closed-portal message updated.', 'success')

        elif action == 'set_fee':
            try:
                fee = int(request.form.get('fee', 2000))
                set_setting('presentation_fee', fee)
                flash(f'Presentation fee set to ₦{fee:,}.', 'success')
            except ValueError:
                flash('Invalid fee amount.', 'danger')

        return redirect(url_for('admin_settings'))

    return render_template(
        'admin_settings.html',
        portal_open=is_portal_open(),
        portal_message=get_portal_message(),
        presentation_fee=get_presentation_fee(),
    )


# ── Coordinator: Manage Supervisors ──────────────────────────────────────────
@app.route('/admin/supervisors', methods=['GET', 'POST'])
@coordinator_required
def admin_supervisors():
    """
    Project Coordinator manages the supervisor list.
    Supervisors added here appear in the student topic submission dropdown.
    """
    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'add':
            full_name  = request.form.get('full_name', '').strip()
            short_name = request.form.get('short_name', '').strip()
            department = request.form.get('department', '').strip()
            if not full_name:
                flash('Full name is required.', 'danger')
            else:
                ok, msg = add_supervisor(full_name, short_name, department)
                flash(msg, 'success' if ok else 'warning')

        elif action == 'deactivate':
            name = request.form.get('name', '').strip()
            ok, msg = update_supervisor_status(name, 'No')
            flash(msg, 'success' if ok else 'warning')

        elif action == 'activate':
            name = request.form.get('name', '').strip()
            ok, msg = update_supervisor_status(name, 'Yes')
            flash(msg, 'success' if ok else 'warning')

        elif action == 'set_passphrase':
            name       = request.form.get('name', '').strip()
            passphrase = request.form.get('passphrase', '').strip()
            if not passphrase:
                flash('Passphrase cannot be empty.', 'danger')
            else:
                ok, msg = set_supervisor_passphrase(name, passphrase)
                flash(msg, 'success' if ok else 'warning')

        return redirect(url_for('admin_supervisors'))

    all_supervisors = get_supervisors(active_only=False)
    return render_template(
        'admin_supervisors.html',
        supervisors=all_supervisors,
    )


# ── Download registered topics as Excel ──────────────────────────────────────
@app.route('/download/registered-topics')
@staff_required
def download_registered_topics():
    """
    Generate and download an Excel file of registered topics.
    Filtered by ?view=current|past|all  and ?programme=...
    """
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        flash('openpyxl is required for Excel download. Run: pip install openpyxl', 'danger')
        return redirect(url_for('view_registered'))

    from flask import send_file
    view_mode    = request.args.get('view', 'current')
    filter_prog  = request.args.get('programme', '')

    if view_mode == 'all':
        raw = get_taken_topics()
    elif view_mode == 'past':
        raw = [r for r in get_taken_topics()
               if r.get('Session', '') != CURRENT_SESSION]
    else:
        raw = get_taken_topics_by_session(CURRENT_SESSION)

    if filter_prog:
        raw = [r for r in raw
               if r.get('Programme', '').lower() == filter_prog.lower()]

    # ── Build Excel workbook ──────────────────────────────────────────────────
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Registered Topics"

    # Header styling
    hdr_font = Font(name="Arial", bold=True, color="FFFFFF", size=11)
    hdr_fill = PatternFill(start_color="1F4E79",
                           end_color="1F4E79", fill_type="solid")
    hdr_align = Alignment(horizontal="center",
                          vertical="center", wrap_text=True)

    headers = ["S/N", "Student Name", "Matric Number",
               "Programme", "Topic Title", "Supervisor",
               "Date Submitted", "Session"]
    col_widths = [5, 25, 18, 28, 55, 25, 16, 12]

    for col, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font  = hdr_font
        cell.fill  = hdr_fill
        cell.alignment = hdr_align
        ws.column_dimensions[
            openpyxl.utils.get_column_letter(col)
        ].width = w
    ws.row_dimensions[1].height = 28

    # Group by programme for sorted output
    alt_fill = PatternFill(start_color="DEEAF1",
                           end_color="DEEAF1", fill_type="solid")
    body_font  = Font(name="Arial", size=10)
    body_align = Alignment(vertical="center", wrap_text=True)

    row_num = 2
    sn      = 1
    for prog in PROGRAMMES:
        prog_students = sorted(
            [r for r in raw if r.get('Programme', '') == prog],
            key=lambda r: r.get('Student Name', '').lower()
        )
        for r in prog_students:
            fill = alt_fill if row_num % 2 == 0 else PatternFill()
            row_data = [
                sn,
                r.get('Student Name',    ''),
                r.get('Matric Number',   ''),
                r.get('Programme',       ''),
                r.get('Topic Title',     ''),
                r.get('Supervisor',      ''),
                (r.get('Submission Date', '') or '')[:10],
                r.get('Session',         ''),
            ]
            for col, val in enumerate(row_data, start=1):
                cell = ws.cell(row=row_num, column=col, value=val)
                cell.font      = body_font
                cell.alignment = body_align
                cell.fill      = fill
            ws.row_dimensions[row_num].height = 30
            row_num += 1
            sn      += 1

    # Title row above header
    ws.insert_rows(1)
    title_cell = ws.cell(row=1, column=1,
        value=f"Niger State Polytechnic Zungeru — Dept of Computer Science")
    ws.merge_cells(f'A1:H1')
    title_cell.font = Font(name="Arial", bold=True, size=13)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 24

    # Subtitle
    ws.insert_rows(2)
    sub = ws.cell(row=2, column=1,
        value=(f"Final Year Registered Topics — "
               f"{'Current Session: ' + CURRENT_SESSION if view_mode == 'current' else view_mode.title() + ' Sessions'}"
               f" | Generated: {datetime.now().strftime('%d %b %Y %I:%M %p')}"))
    ws.merge_cells('A2:H2')
    sub.font = Font(name="Arial", italic=True, size=10)
    sub.alignment = Alignment(horizontal="center")
    ws.row_dimensions[2].height = 18

    ws.freeze_panes = "A4"

    # Save to buffer
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = (
        f"Registered_Topics_"
        f"{view_mode.title()}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


if __name__ == '__main__':
    app.run(
        host='0.0.0.0',
        port=int(os.environ.get('PORT', 5000)),
        debug=False,
    )
