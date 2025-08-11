from functools import wraps
from flask import session, redirect, url_for, flash, request, g
from sheet import get_student_info  # Prefer top-level import to avoid circular issues

# Constants for session keys
SESSION_KEY = 'matric_number'

def login_required(f):
    """
    Decorator to ensure the user is logged in before accessing protected routes.
    Redirects to login page if not authenticated.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if SESSION_KEY not in session:
            flash("Please log in to access this page", "danger")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def init_app(app):
    """
    Initialise the authentication context with the Flask app.
    Attaches the current user to Flask's global `g` before each request.
    """
    @app.before_request
    def before_request():
        g.user = None
        if SESSION_KEY in session:
            g.user = get_student_info(session[SESSION_KEY])

def logout_user():
    """
    Clears the user session and global context.
    Call this during logout.
    """
    session.pop(SESSION_KEY, None)
    g.user = None
