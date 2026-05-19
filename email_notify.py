import os
import requests
import logging

logger = logging.getLogger(__name__)

# Resend configuration (read from environment variables)
RESEND_API_KEY = os.getenv("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL")   # e.g., noreply@projects.csnigerpoly.com

def send_email(to_email: str, subject: str, html_content: str, to_name: str = "") -> bool:
    """Send an email via Resend API. Returns True if successful."""
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set, email not sent")
        return False
    if not RESEND_FROM_EMAIL:
        logger.warning("RESEND_FROM_EMAIL not set, email not sent")
        return False

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html_content
    }
    try:
        response = requests.post(url, json=data, headers=headers, timeout=20)
        if response.status_code in (200, 201):
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        else:
            logger.error(f"Resend error {response.status_code}: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        return False

# ----------------------------------------------------------------------
# Notification functions for student actions (supervisor receives)
# ----------------------------------------------------------------------
def notify_supervisor_topic_submitted(student_name, student_matric, topic_title, supervisor_email, supervisor_name):
    subject = f"Topic Submission: {student_name} ({student_matric})"
    html = f"""
    <h3>Topic Submitted</h3>
    <p>Dear {supervisor_name},</p>
    <p>Your supervisee <strong>{student_name}</strong> ({student_matric}) has submitted the following project topic:</p>
    <blockquote>{topic_title}</blockquote>
    <p>Please log in to the portal to review and clear the student when ready.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(supervisor_email, subject, html, supervisor_name)

def notify_supervisor_topic_dropped(student_name, student_matric, topic_title, supervisor_email, supervisor_name):
    subject = f"Topic Dropped: {student_name} ({student_matric})"
    html = f"""
    <h3>Topic Dropped</h3>
    <p>Dear {supervisor_name},</p>
    <p>Your supervisee <strong>{student_name}</strong> ({student_matric}) has dropped the topic:</p>
    <blockquote>{topic_title}</blockquote>
    <p>They may select a new topic.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(supervisor_email, subject, html, supervisor_name)

# ----------------------------------------------------------------------
# Student notifications
# ----------------------------------------------------------------------
def notify_student_cleared(student_email, student_name, supervisor_name):
    subject = "Your Project Topic Has Been Cleared"
    html = f"""
    <h3>Topic Cleared by Supervisor</h3>
    <p>Dear {student_name},</p>
    <p>Your supervisor <strong>{supervisor_name}</strong> has cleared your project topic.</p>
    <p>You are now eligible to proceed with the presentation after paying the required fee.</p>
    <p>Log in to the portal for further details.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(student_email, subject, html, student_name)

def notify_student_registered(student_email, student_name):
    subject = "Welcome to the Project Portal"
    html = f"""
    <h3>Account Created Successfully</h3>
    <p>Dear {student_name},</p>
    <p>Your account has been created. You can now log in and submit your project topic.</p>
    <p>If you have any issues, contact the Project Coordinator.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(student_email, subject, html, student_name)

# ----------------------------------------------------------------------
# Coordinator notifications for supervisor proposals
# ----------------------------------------------------------------------
def notify_coordinator_proposal_submitted(proposer, proposal_type, new_topic, student_matric=None):
    """Notify the coordinator when a supervisor proposes a new topic or a topic change."""
    coordinator_email = os.getenv("COORDINATOR_EMAIL")
    if not coordinator_email:
        logger.warning("COORDINATOR_EMAIL not set, skipping coordinator notification")
        return False

    subject = f"New Proposal from {proposer}"
    html = f"""
    <h3>New Topic Proposal Submitted</h3>
    <p>Dear Coordinator,</p>
    <p><strong>{proposer}</strong> has submitted a proposal:</p>
    <ul>
        <li><strong>Type:</strong> {proposal_type}</li>
        <li><strong>New Topic:</strong> {new_topic}</li>
        {f'<li><strong>Student:</strong> {student_matric}</li>' if student_matric else ''}
    </ul>
    <p>Please log in to the portal to review and decide.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(coordinator_email, subject, html, "Coordinator")

def notify_proposer_decision(proposer_email, proposer_name, proposal_type, new_topic, decision, note=""):
    """Notify the supervisor about the decision on their proposal."""
    subject = f"Proposal {decision}: {new_topic[:50]}"
    html = f"""
    <h3>Proposal {decision}</h3>
    <p>Dear {proposer_name},</p>
    <p>Your proposal for <strong>"{new_topic}"</strong> has been <strong>{decision}</strong>.</p>
    <p><strong>Type:</strong> {proposal_type}</p>
    {f'<p><strong>Note:</strong> {note}</p>' if note else ''}
    <p>Thank you for your contribution.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(proposer_email, subject, html, proposer_name)

# ----------------------------------------------------------------------
# Coordinator notification for student clearance (already existing)
# ----------------------------------------------------------------------
def notify_coordinator_cleared(student_name, student_matric, supervisor_name):
    coordinator_email = os.getenv("COORDINATOR_EMAIL")
    if not coordinator_email:
        logger.warning("COORDINATOR_EMAIL not set, skipping coordinator notification")
        return False
    subject = f"Student Cleared: {student_name} ({student_matric})"
    html = f"""
    <h3>Student Cleared by Supervisor</h3>
    <p>Dear Coordinator,</p>
    <p>The following student has been cleared by their supervisor:</p>
    <ul>
        <li><strong>Student:</strong> {student_name} ({student_matric})</li>
        <li><strong>Supervisor:</strong> {supervisor_name}</li>
    </ul>
    <p>The student is now eligible to make the presentation fee.</p>
    <p>Please log in to the portal to record payment when received.</p>
    <hr>
    <small>Project Portal – Department of Computer Science, Niger State Polytechnic</small>
    """
    return send_email(coordinator_email, subject, html, "Coordinator")