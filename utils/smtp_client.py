"""
utils/smtp_client.py

Simple SMTP helper for Vantage.

Settings are read from the Flask app config, which app.py populates from
environment variables (supplied by docker-compose via .env):

    SMTP_HOST       SMTP server hostname                            (required)
    SMTP_PORT       Port                                            (default 587)
    SMTP_USER       Login username                                  (optional)
    SMTP_PASS       Login password                                  (optional)
    SMTP_FROM       From address                                    (required)
    SMTP_FROM_NAME  Friendly name shown beside the From address      (optional)
    SMTP_REPLY_TO   Default Reply-To address                        (optional)
    SMTP_FORCE_CC   Comma-separated addresses copied on every email  (optional)
    SMTP_USE_TLS    Upgrade the connection with STARTTLS             (default true)
    SMTP_TIMEOUT    Socket timeout in seconds                       (default 10)

Usage:

    from utils.smtp_client import send_email

    send_email(
        subject='Team 3 credentials',
        body='Attached is your credential export.',
        to=['coach@example.edu'],
        cc=['white-lead@example.edu'],
        attachments=[('team3_accounts.csv', csv_bytes)],
        sent_by=session['user_info']['username'],
    )
"""

import logging
import mimetypes
import os
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _setting(key, default=None):
    """
    Read a setting from the Flask app config, falling back to the environment.

    The fallback matters for Celery workers and CLI scripts that call this
    module outside of a request/app context.
    """
    try:
        from flask import current_app
        if key in current_app.config:
            return current_app.config[key]
    except RuntimeError:
        pass  # No app context; fall through to the environment.
    return os.environ.get(key, default)


def _as_int(value, default):
    """Coerce a config value to int, falling back on anything unusable."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def get_smtp_config():
    """Build the SMTP settings dictionary. Read per call, so edits take effect."""
    return {
        'host': _setting('SMTP_HOST'),
        'port': _as_int(_setting('SMTP_PORT', 587), 587),
        'user': _setting('SMTP_USER'),
        'password': _setting('SMTP_PASS'),
        'from_email': _setting('SMTP_FROM'),
        'from_name': _setting('SMTP_FROM_NAME', 'Vantage'),
        'reply_to': _setting('SMTP_REPLY_TO'),
        'force_cc': [a.strip() for a in (_setting('SMTP_FORCE_CC') or '').split(',') if a.strip()],
        'use_tls': str(_setting('SMTP_USE_TLS', 'true')).lower() in ('1', 'true', 'yes', 'on'),
        'timeout': _as_int(_setting('SMTP_TIMEOUT', 10), 10),
    }


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _as_list(value):
    """Accept a single address or a list, and always return a clean list."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    return [str(item).strip() for item in value if str(item).strip()]


def _attachment_bytes(source):
    """
    Turn an attachment source into raw bytes.

    Accepts raw bytes (what BytesIO.getvalue() returns), an open file-like
    object, or a path on the local filesystem.
    """
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)

    if hasattr(source, 'read'):
        try:
            source.seek(0)  # Rewind buffers that were already written to.
        except (AttributeError, OSError, ValueError):
            pass
        return source.read()

    with open(source, 'rb') as handle:
        return handle.read()


def _log_email_sent(sent_by, subject, to_list, cc_list, bcc_list, attachments):
    """Write an AuditLog row for a send. Never lets a logging failure break the send."""
    if not sent_by:
        return

    try:
        # Imported here to avoid a circular import at module load time.
        from models import db, AuditLog

        db.session.add(AuditLog(
            username=sent_by,
            action='email_send',
            entity_type='email',
            entity_id=None,
            old_value=None,
            new_value={
                'subject': subject,
                'to': to_list,
                'cc': cc_list,
                'bcc': bcc_list,
                'attachments': [name for name, _ in (attachments or [])],
            },
        ))
        db.session.commit()
    except Exception as exc:
        logger.error('Failed to write audit log for email %r: %s', subject, exc)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def send_email(
    subject,
    body,
    to,
    cc=None,
    bcc=None,
    reply_to=None,
    attachments=None,
    html=False,
    sent_by=None,
):
    """
    Send an email through the configured SMTP server.

    Args:
        subject:     Subject line.
        body:        Message body (plain text, or HTML when html=True).
        to:          One address, or a list of addresses.
        cc:          Optional CC address(es).
        bcc:         Optional BCC address(es) -- hidden from the headers but
                     still delivered.
        reply_to:    Overrides the SMTP_REPLY_TO default for this message.
        attachments: List of (filename, source) pairs, or a list of file paths.
                     A source may be raw bytes (e.g. BytesIO.getvalue()), an
                     open file-like object, or a path on disk.
        html:        Treat body as HTML instead of plain text.
        sent_by:     Username to record in the AuditLog. Omit for automated
                     system mail that has no originating user.

    Returns:
        True if the server accepted the message, False otherwise. Failures are
        logged, not raised, so this is safe to call straight from a route.

    Every address in SMTP_FORCE_CC is added to the CC list of every message,
    on top of whatever the caller passes in. Addresses already present in To
    or Cc are not duplicated.
    """
    config = get_smtp_config()

    if not config['host'] or not config['from_email']:
        logger.error('SMTP is not configured (SMTP_HOST and SMTP_FROM are required).')
        return False

    to_list = _as_list(to)
    cc_list = _as_list(cc)
    bcc_list = _as_list(bcc)

    # Forced CC: always added, never duplicated.
    already = {addr.lower() for addr in to_list + cc_list}
    for addr in config['force_cc']:
        if addr.lower() not in already:
            cc_list.append(addr)
            already.add(addr.lower())

    if not (to_list or cc_list or bcc_list):
        logger.warning('send_email called with no recipients; nothing sent.')
        return False

    # --- Build the message ---
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr((config['from_name'], config['from_email']))
    if to_list:
        msg['To'] = ', '.join(to_list)
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)
    if reply_to or config['reply_to']:
        msg['Reply-To'] = reply_to or config['reply_to']
    msg['Message-ID'] = make_msgid()

    if html:
        msg.set_content(body, subtype='html')
    else:
        msg.set_content(body)

    # --- Attachments ---
    for item in attachments or []:
        # Allow a bare path in place of a (filename, source) pair.
        if isinstance(item, (str, os.PathLike)):
            filename, source = os.path.basename(str(item)), item
        else:
            filename, source = item

        try:
            payload = _attachment_bytes(source)
        except OSError as exc:
            logger.error('Could not read attachment %r: %s', filename, exc)
            return False

        content_type, _ = mimetypes.guess_type(filename)
        maintype, _, subtype = (content_type or 'application/octet-stream').partition('/')
        msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)

    # --- Send ---
    # BCC goes in the envelope only; it must never appear in the headers.
    recipients = to_list + cc_list + bcc_list

    try:
        with smtplib.SMTP(config['host'], config['port'], timeout=config['timeout']) as server:
            if config['use_tls']:
                server.starttls()
            if config['user'] and config['password']:
                server.login(config['user'], config['password'])
            server.send_message(msg, from_addr=config['from_email'], to_addrs=recipients)
    except smtplib.SMTPAuthenticationError:
        logger.error('SMTP authentication failed; check SMTP_USER and SMTP_PASS.')
        return False
    except smtplib.SMTPException as exc:
        logger.error('SMTP error sending %r: %s', subject, exc)
        return False
    except OSError as exc:
        logger.error('Could not reach %s:%s (%s).', config['host'], config['port'], exc)
        return False

    logger.info('Sent %r to %d recipient(s).', subject, len(recipients))
    _log_email_sent(sent_by, subject, to_list, cc_list, bcc_list, attachments)
    return True
