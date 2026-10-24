"""Team invites by email over SMTP (any provider: SES, SendGrid, Resend, Postmark, Mailgun, Gmail all speak it).
Without SMTP_HOST nothing is sent and the inviter copies the link from Settings instead."""
import os
import smtplib
from email.message import EmailMessage
from html import escape

HOST = os.environ.get("SMTP_HOST", "")
PORT = int(os.environ.get("SMTP_PORT") or 587)  # 587: STARTTLS; 465: TLS from the start
USER = os.environ.get("SMTP_USER", "")
PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SENDER = os.environ.get("SMTP_FROM") or USER
BRAND, INK, MUTED = "#178771", "#1b1f24", "#5f6b76"  # gg-pulse teal (global.css --brand), text, secondary text
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def invite_message(to, workspace, inviter, link, days):
    """The invite: plain text, and the same as HTML for mail apps that show it. Names are escaped in the HTML, since
    anyone who can invite chooses the workspace name."""
    msg = EmailMessage()
    msg["Subject"] = f"{inviter} invited you to {workspace} on gg-pulse"
    msg["From"], msg["To"] = SENDER, to
    msg.set_content(
        f"{inviter} invited you to join the {workspace} workspace on gg-pulse.\n\n"
        "gg-pulse keeps your team's GitHub repositories, CI, code reviews and GG-Ops tasks in one place.\n\n"
        f"Accept the invitation (sign in with GitHub when asked):\n{link}\n\n"
        f"The link works once and expires in {days} days. If you weren't expecting this invitation, you can ignore "
        "this email.\n\n- gg-pulse\n")
    w, who, url = escape(workspace), escape(inviter), escape(link, quote=True)
    p = f"margin:0 0 16px;font:15px/1.6 {FONT};color:{INK}"
    msg.add_alternative(f"""<!doctype html>
<html><body style="margin:0;padding:32px 16px;background:#f4f6f8">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px">
<tr><td style="padding:0 4px 16px;font:600 17px {FONT};color:{INK}">
<span style="display:inline-block;width:28px;height:28px;border-radius:14px;background:{BRAND};color:#fff;font:700 11px/28px monospace;text-align:center;vertical-align:middle">gg</span>
<span style="vertical-align:middle;margin-left:8px">gg-pulse</span></td></tr>
<tr><td style="background:#fff;border-radius:16px;padding:32px">
<p style="margin:0 0 12px;font:600 20px/1.4 {FONT};color:{INK}">{who} invited you to {w}</p>
<p style="{p}">Join the <b>{w}</b> workspace on gg-pulse, where your team's GitHub repositories, CI, code reviews and GG-Ops tasks come together.</p>
<p style="margin:24px 0"><a href="{url}" style="display:inline-block;padding:12px 24px;border-radius:999px;background:{BRAND};color:#fff;font:600 15px {FONT};text-decoration:none">Accept invitation</a></p>
<p style="margin:0 0 8px;font:13px/1.6 {FONT};color:{MUTED}">You'll sign in with GitHub. The link works once and expires in {days} days.</p>
<p style="margin:0;font:13px/1.6 {FONT};color:{MUTED};word-break:break-all">Button not working? Open {url}</p>
</td></tr>
<tr><td style="padding:16px 4px 0;font:12px/1.6 {FONT};color:{MUTED}">If you weren't expecting this invitation, you can ignore this email.</td></tr>
</table></td></tr></table>
</body></html>
""", subtype="html")
    return msg


def send_invite(to, workspace, inviter, link, days):
    """True when the email went out; False when SMTP isn't configured. SMTP errors propagate."""
    if not HOST:
        return False
    msg = invite_message(to, workspace, inviter, link, days)
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
