"""
model.py — Student account management
--------------------------------------
Revised to delegate entirely to sheet.py v2 functions.

Student IDENTITY (Name, Programme, Supervisor) now comes from the
AssignedSupervisors sheet — not stored in the Students sheet.

Students sheet stores ONLY:
  Matric Number | Password Hash | Email | Registration Date
"""

from sheet import (
    lookup_student_in_master,
    student_exists_v2,
    register_student_v2,
    verify_student_v2,
    update_student_password,
)


def student_exists(matric):
    """
    Check if a student has a portal account.
    Delegates to sheet.student_exists_v2().
    """
    return student_exists_v2(matric)


def register_student(matric, email, password_hash):
    """
    Create a portal account for a student who is already in
    the AssignedSupervisors master sheet.

    Parameters
    ----------
    matric        : str  — student matric number (already validated)
    email         : str  — student email address
    password_hash : str  — werkzeug-hashed password (hash before calling)

    Returns
    -------
    (True, message)  on success
    (False, message) on failure
    """
    return register_student_v2(matric, email, password_hash)


def verify_student(matric, password):
    """
    Verify login credentials.

    Returns a dict with student details if credentials match:
        {
            'Matric Number':      str,
            'Student Name':       str,
            'Programme':          str,
            'Assigned Supervisor': str,
            'email':              str,
        }
    Returns None if credentials do not match or student not found.
    """
    return verify_student_v2(matric, password)


def get_student_identity(matric):
    """
    Fetch student identity from AssignedSupervisors master sheet.
    Useful for refreshing session data without re-authenticating.
    Returns dict or None.
    """
    return lookup_student_in_master(matric)


def reset_password(matric, new_password_hash):
    """
    Update a student's password hash.
    Caller is responsible for hashing before passing.
    Returns True on success.
    """
    return update_student_password(matric, new_password_hash)
