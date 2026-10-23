"""Team invites by email over SMTP (any provider: SES, SendGrid, Resend, Postmark, Mailgun, Gmail all speak it).
Without SMTP_HOST nothing is sent and the inviter copies the link from Settings instead."""
import os
import smtplib
from email.message import EmailMessage

HOST = os.environ.get("SMTP_HOST", "")
PORT = int(os.environ.get("SMTP_PORT") or 587)  # 587: STARTTLS; 465: TLS from the start
USER = os.environ.get("SMTP_USER", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER = os.environ.get("SMTP_FROM") or USER


def send_invite(to, workspace, inviter, link, days):
    """True when the email went out; False when SMTP isn't configured. SMTP errors propagate."""
    if not HOST:
        return False
    msg = EmailMessage()
    msg["Subject"] = f"{inviter} invited you to {workspace}"
    msg["From"], msg["To"] = SENDER, to
    msg.set_content(f"{inviter} invited you to join the {workspace} workspace on the engineering portal.\n\n"
                    f"Accept the invite (sign in with GitHub when asked):\n{link}\n\n"
                    f"The link works once and expires in {days} days. If you weren't expecting this, ignore it.\n")
    with (smtplib.SMTP_SSL(HOST, PORT, timeout=30) if PORT == 465 else smtplib.SMTP(HOST, PORT, timeout=30)) as s:
        if PORT != 465:
            s.ehlo()
            if s.has_extn("starttls"):
                s.starttls()
            elif USER:  # a local catcher like Mailpit has neither TLS nor a login; a real relay must have TLS
                raise smtplib.SMTPException("the SMTP server offers no STARTTLS; refusing to send the password in clear")
        if USER:
            s.login(USER, PASSWORD)
        s.send_message(msg)
    return True
