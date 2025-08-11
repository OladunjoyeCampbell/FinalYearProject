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

# Load .env for local development
load_dotenv()
app = Flask(__name__)

# — SECRET_KEY setup (Secret Manager in prod, .env locally) —
def get_secret_key():
    if os.getenv('GAE_ENV','').startswith('standard'):
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

# — Google Sheets credentials —
def get_google_credentials():
    if os.getenv('GAE_ENV','').startswith('standard'):
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT')
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/google-credentials-json/versions/latest"
        resp = client.access_secret_version(request={'name': name})
        return resp.payload.data.decode('UTF-8')
    return os.getenv('GOOGLE_CREDENTIALS_JSON')

# — Staff passphrase (Secret Manager in prod, .env locally) —
def get_staff_passphrase():
    # 1️⃣ Try env var first
    env = os.getenv('STAFF_PASSPHRASE')
    if env:
        return env

    # 2️⃣ Fallback to Secret Manager (prod)
    if os.getenv('GAE_ENV', '').startswith('standard'):
        project = os.getenv('GOOGLE_CLOUD_PROJECT')
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/staff-passphrase/versions/latest"
        resp = client.access_secret_version(request={"name": name})
        return resp.payload.data.decode("UTF-8")

    # 3️⃣ If we’re local and no env var, just return empty
    return ''


# — bring in our business logic —
import sheet
from sheet import (
    get_available_topics,
    get_taken_topics,
    register_topic,
    is_student_registered,
    drop_registered_topic,
    find_student_record,
    ensure_students_sheet_exists
)
from model import register_student, verify_student, student_exists

# ensure sheets exist
ensure_students_sheet_exists()

# inject year into templates
@app.context_processor
def inject_current_year():
    return {'current_year': datetime.now().year}

PROGRAMMES = [
    'HND Software & Web Development',
    'HND Networking & Cloud Computing',
    'ND Computer Science'
]

# student login decorator
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            flash('Please log in first.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# staff-only decorator
def staff_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'staff':
            flash('Supervisor access only.', 'warning')
            return redirect(url_for('staff_login'))
        return f(*args, **kwargs)
    return decorated

# — Home —
@app.route('/')
def home():
    return render_template('index.html')

# — Student Login —
@app.route('/login', methods=['GET','POST'])
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
                    'logged_in': True,
                    'matric_number': student['matric_number'],
                    'student_name' : student['name'],
                    'programme'    : student['programme']
                })
                flash(f"Welcome, {student['name']}!", 'success')
                return redirect(url_for('home'))
            flash('Invalid credentials.', 'danger')
    return render_template('login.html')

# — Student Registration —
@app.route('/register', methods=['GET','POST'])
def register():
    if request.method=='POST':
        full = request.form['full_name'].strip()
        m    = request.form['matric_number'].strip()
        prog = request.form['programme'].strip()
        email= request.form['email'].strip()
        pw   = request.form['password']
        cf   = request.form['confirm_password']
        if not all([full,m,prog,email,pw,cf]):
            flash('All fields are required.', 'danger')
        elif pw!=cf:
            flash('Passwords do not match.', 'danger')
        elif student_exists(m):
            flash('Matric number already registered.', 'danger')
        elif register_student(full,m,prog,email,pw):
            flash('Registered—please log in.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Registration failed.', 'danger')
    return render_template('register.html', programmes=PROGRAMMES)

# — Student Logout —
@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

# — Submit Topic —
@app.route('/submit-topic', methods=['GET','POST'])
@login_required
def submit_topic():
    m     = session['matric_number']
    prog  = session['programme']
    topics= get_available_topics(prog)
    taken = is_student_registered(m)
    just  = session.pop('just_registered', False)
    if request.method=='POST':
        name= session['student_name']
        t   = request.form['topic_title'].strip()
        sup = request.form['supervisor'].strip()
        if not all([name,m,prog,t,sup]):
            flash('All fields are required.', 'danger')
        elif taken:
            flash('Drop existing topic first.', 'warning')
        elif register_topic(name,m,prog,t,sup):
            flash('Topic registered!', 'success')
            session['just_registered'] = True
            return redirect(url_for('submit_topic'))
        else:
            flash('Topic unavailable or taken.', 'danger')
    return render_template(
      'submit_topic.html',
      topics=topics,
      already_registered=taken,
      just_registered=just,
      programmes=PROGRAMMES
    )

# — View Available Topics (public) —
@app.route('/view-topics')
def view_topics():
    prog = session.get('programme')
    if prog:
        topics = get_available_topics(prog)
    else:
        topics = {p: get_available_topics(p) for p in PROGRAMMES}
    return render_template('view_topics.html', topics=topics, programme=prog)

# — Supervisor Login —
@app.route('/staff-login', methods=['GET','POST'])
def staff_login():
    if request.method=='POST':
        ph = request.form.get('passphrase','').strip()
        if ph and ph == get_staff_passphrase():
            session.clear()
            session['role'] = 'staff'
            flash('Supervisor logged in.', 'success')
            return redirect(url_for('view_registered'))
        flash('Invalid passphrase.', 'danger')
    return render_template('staff_login.html')

# — Supervisor Logout —
@app.route('/staff-logout')
def staff_logout():
    session.pop('role', None)
    flash('Supervisor logged out.', 'info')
    return redirect(url_for('staff_login'))

# ——————————————————————————————————————————————————————————————————————
# — View All Registered Topics (per programme) —
@app.route('/view-registered')
@staff_required
def view_registered():
    raw = get_taken_topics()
    app.logger.info(f"VIEW-REGISTERED fetched {len(raw)} rows")

    # prepare buckets
    registrations = {prog: [] for prog in PROGRAMMES}

    # case-insensitive match
    for rec in raw:
        prog_cell = rec["Programme"].strip()
        for prog in PROGRAMMES:
            if prog.lower() == prog_cell.lower():
                registrations[prog].append({
                    "student_name":    rec["Student Name"],
                    "matric_number":   rec["Matric Number"],
                    "topic_title":     rec["Topic Title"],
                    "supervisor":      rec["Supervisor"],
                    "submission_date": rec["Submission Date"]
                })
                break
        else:
            app.logger.warning(f"Ignored unknown programme: '{prog_cell}'")

    return render_template('view_registered.html', registrations=registrations)




# — Drop Topic (student only) —
@app.route('/drop-topic', methods=['POST'])
@login_required
def drop_topic():
    m = session['matric_number']
    rec = find_student_record(m)
    if not rec:
        flash('No student record.', 'warning')
        return redirect(url_for('submit_topic'))
    prog = rec.get('Programme')
    if drop_registered_topic(m, prog):
        flash('Topic dropped.', 'success')
    else:
        flash('Drop failed.', 'danger')
    return redirect(url_for('submit_topic'))

# — Forgot Password —
@app.route('/forgot-password', methods=['GET','POST'])
@app.route('/forgot_password', methods=['GET','POST'])
def forgot_password():
    ensure_students_sheet_exists()
    ws = sheet._students_sheet
    if request.method=='POST':
        m   = request.form['matric_number'].strip().upper()
        npw = request.form['new_password']
        cf  = request.form['confirm_password']
        if not all([m,npw,cf]):
            flash('All fields are required.', 'danger')
            return redirect(url_for('forgot_password'))
        if npw!=cf:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('forgot_password'))
        header = ws.row_values(1)
        try:
            cidx = header.index('Password Hash') + 1
        except ValueError:
            flash('Password Hash column missing.', 'danger')
            return redirect(url_for('forgot_password'))
        records = ws.get_all_records()
        row_to_update=None
        for i,rec in enumerate(records, start=2):
            if str(rec.get('Matric Number','')).strip().upper()==m:
                row_to_update=i
                break
        if not row_to_update:
            flash('Matric not found.', 'danger')
            return redirect(url_for('forgot_password'))
        hashed = generate_password_hash(npw, method='scrypt')
        try:
            ws.update_cell(row_to_update, cidx, hashed)
            flash('Password updated; log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Error updating password: {e}','danger')
            return redirect(url_for('forgot_password'))
    return render_template('forgot_password.html')

# — Test endpoint —
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

if __name__=='__main__':
    app.run(host='0.0.0.0',
            port=int(os.environ.get('PORT',5000)),
            debug=False)
