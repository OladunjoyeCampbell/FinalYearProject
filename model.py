import os
from werkzeug.security import generate_password_hash, check_password_hash
from sheet import (
    ensure_students_sheet_exists,
    append_student_record,
    find_student_record,
    append_log_entry
)


def register_student(full_name, matric_number, programme, email, password):
    """Register a new student."""
    try:
        # Ensure the Students sheet exists
        if not ensure_students_sheet_exists():
            return False

        # Check for duplicate matric number
        if find_student_record(matric_number):
            return False

        password_hash = generate_password_hash(password)
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Append to Students sheet
        append_student_record([
            full_name,
            matric_number,
            programme,
            email,
            password_hash,
            now
        ])

        # Log registration
        append_log_entry([
            full_name,
            matric_number,
            programme,
            "N/A",
            "N/A",
            f"Student Registration: {now}"
        ])

        return True

    except Exception as e:
        print(f"Error registering student: {e}")
        return False

def reset_password(matric_number, new_password):
    sheet = get_students_sheet()
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # row 2 = first data row
        if row.get("Matric Number") == matric_number:
            sheet.update_cell(i, 5, new_password)  # Assuming column 5 is 'Password'
            return True
    return False

def student_exists(matric_number):
    """Check if a student with the given matric number exists."""
    try:
        return bool(find_student_record(matric_number))
    except Exception as e:
        print(f"Error checking if student exists: {e}")
        return False


def verify_student(matric_number, password):
    """Verify student credentials; returns student dict or None."""
    try:
        rec = find_student_record(matric_number)
        if rec and check_password_hash(rec.get("Password Hash", ""), password):
            return {
                "name": rec.get("Full Name", ""),
                "matric_number": rec.get("Matric Number", ""),
                "programme": rec.get("Programme", ""),
                "email": rec.get("Email", "")
            }
        return None

    except Exception as e:
        print(f"Error verifying student: {e}")
        return None


def get_student_name(matric_number):
    """Get a student's name by matric number."""
    try:
        rec = find_student_record(matric_number)
        return rec.get("Full Name", "Student") if rec else "Student"
    except Exception as e:
        print(f"Error getting student name: {e}")
        return "Student"


authenticate_student = verify_student
get_student_info = verify_student
