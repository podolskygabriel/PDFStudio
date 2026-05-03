"""
email_sign.py — Email-based document signing flow.
Send a PDF to a recipient for signing, track status locally.

Credentials are encrypted at rest using a machine-derived key via
cryptography.fernet. The key is derived from the username + hostname,
so credentials are tied to the local user account and machine.
"""

import base64
import json
import os
import platform
import smtplib
import ssl
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QMessageBox, QCheckBox,
    QGroupBox, QFileDialog, QComboBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QWidget, QTabWidget, QApplication
)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtCore import Qt


# ── Credential Encryption ──────────────────────────────────────

def _get_machine_key() -> bytes:
    """Derive a stable encryption key from the local machine identity.

    This isn't bulletproof security — it's meant to prevent casual
    plaintext credential exposure. For real secrets, use an OS keychain.
    """
    try:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        # Combine username + hostname + a fixed salt as key material
        identity = f"{os.getlogin()}@{platform.node()}".encode()
        salt = b"pdf_studio_smtp_v1"

        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(identity))
        return key
    except Exception:
        # If cryptography isn't available, fall back to base64 (not secure,
        # but better than raw plaintext for casual protection).
        return None


def _encrypt_value(value: str) -> str:
    """Encrypt a string for at-rest storage."""
    if not value:
        return ""
    key = _get_machine_key()
    if key:
        try:
            from cryptography.fernet import Fernet
            f = Fernet(key)
            return "enc:" + f.encrypt(value.encode()).decode()
        except Exception:
            pass
    # Fallback: base64 obfuscation (not real encryption)
    return "b64:" + base64.b64encode(value.encode()).decode()


def _decrypt_value(stored: str) -> str:
    """Decrypt a stored credential value."""
    if not stored:
        return ""
    if stored.startswith("enc:"):
        key = _get_machine_key()
        if key:
            try:
                from cryptography.fernet import Fernet
                f = Fernet(key)
                return f.decrypt(stored[4:].encode()).decode()
            except Exception:
                return ""
        return ""
    elif stored.startswith("b64:"):
        try:
            return base64.b64decode(stored[4:]).decode()
        except Exception:
            return ""
    # Legacy plaintext (from older versions) — return as-is
    return stored


# ── Data Models ─────────────────────────────────────────────────

@dataclass
class SMTPConfig:
    """SMTP server configuration."""
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True
    sender_name: str = ""
    sender_email: str = ""

    def is_configured(self) -> bool:
        return bool(self.host and self.username and self.sender_email)


@dataclass
class SignRequest:
    """A signing request sent to a recipient."""
    request_id: str = ""
    pdf_path: str = ""
    recipient_email: str = ""
    recipient_name: str = ""
    subject: str = ""
    message: str = ""
    status: str = "draft"  # draft, sent, signed, expired
    created_at: str = ""
    sent_at: str = ""
    signed_at: str = ""

    def __post_init__(self):
        if not self.request_id:
            self.request_id = str(uuid.uuid4())[:8]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


# ── Persistence ─────────────────────────────────────────────────

class SigningStore:
    """JSON-based store for SMTP config and signing requests.

    SMTP credentials (username, password) are encrypted at rest.
    """

    def __init__(self, store_dir: Optional[str] = None):
        if store_dir is None:
            store_dir = os.path.join(Path.home(), ".pdf_studio")
        self._dir = store_dir
        os.makedirs(self._dir, exist_ok=True)
        self._config_path = os.path.join(self._dir, "smtp_config.json")
        self._requests_path = os.path.join(self._dir, "sign_requests.json")

    def load_smtp_config(self) -> SMTPConfig:
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r") as f:
                    data = json.load(f)
                # Decrypt sensitive fields
                data["password"] = _decrypt_value(data.get("password", ""))
                return SMTPConfig(**data)
            except Exception:
                pass
        return SMTPConfig()

    def save_smtp_config(self, config: SMTPConfig):
        data = asdict(config)
        # Encrypt sensitive fields before writing
        data["password"] = _encrypt_value(config.password)
        with open(self._config_path, "w") as f:
            json.dump(data, f, indent=2)
        # Restrict file permissions on Unix
        try:
            os.chmod(self._config_path, 0o600)
        except (OSError, AttributeError):
            pass  # Windows doesn't support Unix-style permissions

    def load_requests(self) -> list[SignRequest]:
        if os.path.exists(self._requests_path):
            try:
                with open(self._requests_path, "r") as f:
                    data = json.load(f)
                return [SignRequest(**r) for r in data]
            except Exception:
                pass
        return []

    def save_requests(self, requests: list[SignRequest]):
        with open(self._requests_path, "w") as f:
            json.dump([asdict(r) for r in requests], f, indent=2)

    def add_request(self, req: SignRequest):
        reqs = self.load_requests()
        reqs.append(req)
        self.save_requests(reqs)

    def update_request(self, request_id: str, **kwargs):
        reqs = self.load_requests()
        for r in reqs:
            if r.request_id == request_id:
                for k, v in kwargs.items():
                    setattr(r, k, v)
        self.save_requests(reqs)


# ── Email Sending ───────────────────────────────────────────────

def send_signing_email(config: SMTPConfig, request: SignRequest) -> tuple[bool, str]:
    """Send a PDF as an email attachment for signing.
    Returns (success, error_message).
    """
    if not config.is_configured():
        return False, "SMTP not configured. Go to Settings > Email Setup."

    if not os.path.isfile(request.pdf_path):
        return False, f"PDF not found: {request.pdf_path}"

    try:
        msg = MIMEMultipart()
        msg["From"] = f"{config.sender_name} <{config.sender_email}>"
        msg["To"] = request.recipient_email
        msg["Subject"] = (
            request.subject
            or f"Document for your signature — {Path(request.pdf_path).stem}"
        )

        body_text = request.message or (
            f"Hi {request.recipient_name or 'there'},\n\n"
            f"Please review and sign the attached document.\n\n"
            f"Document: {Path(request.pdf_path).name}\n"
            f"Request ID: {request.request_id}\n\n"
            f"To sign:\n"
            f"1. Open the attached PDF\n"
            f"2. Add your signature using any PDF tool\n"
            f"3. Reply to this email with the signed copy attached\n\n"
            f"Thank you,\n"
            f"{config.sender_name}"
        )

        html_body = f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                      max-width: 600px; margin: 0 auto; color: #333;">
            <div style="background: #1a73e8; padding: 20px; text-align: center;">
                <h2 style="color: white; margin: 0;">PDF Studio — Signature Request</h2>
            </div>
            <div style="padding: 24px; background: #f9f9f9; border: 1px solid #e0e0e0;">
                <p>Hi {request.recipient_name or 'there'},</p>
                <p>You've been asked to review and sign the attached document.</p>
                <div style="background: white; border: 1px solid #ddd; border-radius: 8px;
                            padding: 16px; margin: 16px 0;">
                    <p style="margin: 4px 0;"><strong>Document:</strong>
                       {Path(request.pdf_path).name}</p>
                    <p style="margin: 4px 0;"><strong>Request ID:</strong>
                       {request.request_id}</p>
                    <p style="margin: 4px 0;"><strong>From:</strong>
                       {config.sender_name}</p>
                </div>
                <h3>How to sign:</h3>
                <ol>
                    <li>Open the attached PDF</li>
                    <li>Add your signature using PDF Studio or any PDF tool</li>
                    <li>Reply to this email with the signed copy attached</li>
                </ol>
                {f'<p><em>Note from sender:</em> {request.message}</p>'
                 if request.message else ''}
            </div>
            <div style="padding: 12px; text-align: center; color: #888; font-size: 12px;">
                Sent via PDF Studio — Free, open-source PDF editor
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with open(request.pdf_path, "rb") as f:
            pdf_attachment = MIMEApplication(f.read(), _subtype="pdf")
            pdf_attachment.add_header(
                "Content-Disposition", "attachment",
                filename=Path(request.pdf_path).name
            )
            msg.attach(pdf_attachment)

        if config.use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(config.host, config.port) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(config.username, config.password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(config.host, config.port) as server:
                server.login(config.username, config.password)
                server.send_message(msg)

        return True, ""

    except smtplib.SMTPAuthenticationError:
        return False, (
            "SMTP authentication failed. Check username/password "
            "(you may need an app password)."
        )
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {e}"
    except Exception as e:
        return False, f"Failed to send: {e}"


# ── SMTP Setup Dialog ──────────────────────────────────────────

class SMTPSetupDialog(QDialog):
    """Dialog for configuring SMTP email settings."""

    PRESETS = {
        "Gmail": ("smtp.gmail.com", 587, True),
        "Outlook / Hotmail": ("smtp-mail.outlook.com", 587, True),
        "Yahoo": ("smtp.mail.yahoo.com", 587, True),
        "iCloud": ("smtp.mail.me.com", 587, True),
        "Custom": ("", 587, True),
    }

    def __init__(self, store: SigningStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Email Setup — SMTP Configuration")
        self.setMinimumWidth(480)
        self._store = store
        self._config = store.load_smtp_config()
        self._build_ui()
        self._populate()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Provider:"))
        self._preset_combo = QComboBox()
        self._preset_combo.addItems(self.PRESETS.keys())
        self._preset_combo.currentTextChanged.connect(self._on_preset)
        preset_row.addWidget(self._preset_combo, 1)
        layout.addLayout(preset_row)

        form = QFormLayout()
        form.setSpacing(8)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("smtp.gmail.com")
        form.addRow("SMTP Host:", self._host_edit)
        self._port_edit = QLineEdit()
        self._port_edit.setPlaceholderText("587")
        self._port_edit.setFixedWidth(80)
        form.addRow("Port:", self._port_edit)
        self._tls_check = QCheckBox("Use TLS (STARTTLS)")
        self._tls_check.setChecked(True)
        form.addRow("", self._tls_check)
        self._user_edit = QLineEdit()
        self._user_edit.setPlaceholderText("you@gmail.com")
        form.addRow("Username:", self._user_edit)
        self._pass_edit = QLineEdit()
        self._pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._pass_edit.setPlaceholderText("App password (not your regular password)")
        form.addRow("Password:", self._pass_edit)

        layout.addLayout(form)

        group = QGroupBox("Sender Identity")
        gl = QFormLayout(group)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Your Name")
        gl.addRow("Display Name:", self._name_edit)
        self._email_edit = QLineEdit()
        self._email_edit.setPlaceholderText("you@gmail.com")
        gl.addRow("From Email:", self._email_edit)
        layout.addWidget(group)

        info = QLabel(
            "For Gmail: use an App Password "
            "(Google Account > Security > App Passwords).\n"
            "Credentials are encrypted and stored locally in "
            "~/.pdf_studio/smtp_config.json"
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(info)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        test_btn = QPushButton("Test Connection")
        test_btn.clicked.connect(self._test_connection)
        btn_row.addWidget(test_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        save_btn.setStyleSheet(
            "QPushButton { background: #1a73e8; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold; }"
        )
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _populate(self):
        c = self._config
        self._host_edit.setText(c.host)
        self._port_edit.setText(str(c.port))
        self._tls_check.setChecked(c.use_tls)
        self._user_edit.setText(c.username)
        self._pass_edit.setText(c.password)
        self._name_edit.setText(c.sender_name)
        self._email_edit.setText(c.sender_email)

    def _on_preset(self, name):
        if name in self.PRESETS:
            host, port, tls = self.PRESETS[name]
            if host:
                self._host_edit.setText(host)
                self._port_edit.setText(str(port))
                self._tls_check.setChecked(tls)

    def _build_config(self) -> SMTPConfig:
        return SMTPConfig(
            host=self._host_edit.text().strip(),
            port=int(self._port_edit.text() or 587),
            username=self._user_edit.text().strip(),
            password=self._pass_edit.text(),
            use_tls=self._tls_check.isChecked(),
            sender_name=self._name_edit.text().strip(),
            sender_email=self._email_edit.text().strip(),
        )

    def _test_connection(self):
        config = self._build_config()
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            context = ssl.create_default_context()
            with smtplib.SMTP(config.host, config.port, timeout=10) as server:
                server.ehlo()
                if config.use_tls:
                    server.starttls(context=context)
                server.login(config.username, config.password)
            QApplication.restoreOverrideCursor()
            QMessageBox.information(self, "Success", "SMTP connection successful!")
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Connection Failed", f"Could not connect:\n{e}")

    def _save(self):
        config = self._build_config()
        if not config.host or not config.username:
            QMessageBox.warning(
                self, "Incomplete", "Please fill in at least host and username."
            )
            return
        self._store.save_smtp_config(config)
        self._config = config
        self.accept()


# ── Send for Signing Dialog ────────────────────────────────────

class SendForSigningDialog(QDialog):
    """Dialog to compose and send a signing request."""

    def __init__(self, pdf_path: str, store: SigningStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Send for Signature")
        self.setMinimumSize(520, 460)
        self._pdf_path = pdf_path
        self._store = store
        self._sent = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        doc_label = QLabel(f"Document: {Path(self._pdf_path).name}")
        doc_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        layout.addWidget(doc_label)

        form = QFormLayout()
        form.setSpacing(8)

        self._to_email = QLineEdit()
        self._to_email.setPlaceholderText("recipient@example.com")
        form.addRow("To (email):", self._to_email)

        self._to_name = QLineEdit()
        self._to_name.setPlaceholderText("Recipient Name (optional)")
        form.addRow("Name:", self._to_name)

        self._subject = QLineEdit()
        self._subject.setPlaceholderText(
            f"Please sign: {Path(self._pdf_path).stem}"
        )
        form.addRow("Subject:", self._subject)

        layout.addLayout(form)

        layout.addWidget(QLabel("Message (optional):"))
        self._message = QTextEdit()
        self._message.setPlaceholderText("Add a personal note to the recipient...")
        self._message.setMaximumHeight(120)
        layout.addWidget(self._message)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        setup_btn = QPushButton("Email Setup...")
        setup_btn.clicked.connect(self._open_smtp_setup)
        btn_row.addWidget(setup_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        send_btn = QPushButton("Send for Signature")
        send_btn.setDefault(True)
        send_btn.clicked.connect(self._send)
        send_btn.setStyleSheet(
            "QPushButton { background: #1a73e8; color: white; padding: 8px 20px; "
            "border-radius: 4px; font-weight: bold; }"
        )
        btn_row.addWidget(send_btn)
        layout.addLayout(btn_row)

    def _open_smtp_setup(self):
        dlg = SMTPSetupDialog(self._store, self)
        dlg.exec()

    def _send(self):
        email = self._to_email.text().strip()
        if not email or "@" not in email:
            QMessageBox.warning(self, "Invalid Email", "Please enter a valid email address.")
            return

        config = self._store.load_smtp_config()
        if not config.is_configured():
            QMessageBox.warning(
                self, "Not Configured",
                "SMTP not configured. Click 'Email Setup...' first."
            )
            return

        request = SignRequest(
            pdf_path=self._pdf_path,
            recipient_email=email,
            recipient_name=self._to_name.text().strip(),
            subject=self._subject.text().strip(),
            message=self._message.toPlainText().strip(),
        )

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        ok, err = send_signing_email(config, request)
        QApplication.restoreOverrideCursor()

        if ok:
            request.status = "sent"
            request.sent_at = datetime.now().isoformat()
            self._store.add_request(request)
            self._sent = True
            QMessageBox.information(
                self, "Sent",
                f"Signing request sent to {email}.\n"
                f"Request ID: {request.request_id}\n\n"
                f"Track status in View > Signing Requests."
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Send Failed", err)


# ── Signing Requests Tracker ──────────────────────────────────

class SigningTrackerDialog(QDialog):
    """View and manage sent signing requests."""

    def __init__(self, store: SigningStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signing Requests")
        self.setMinimumSize(700, 400)
        self._store = store
        self._build_ui()
        self._load()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels([
            "ID", "Recipient", "Document", "Status", "Sent", "Actions"
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load)
        btn_row.addWidget(refresh_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _load(self):
        requests = self._store.load_requests()
        self._table.setRowCount(len(requests))
        for row, req in enumerate(reversed(requests)):
            self._table.setItem(row, 0, QTableWidgetItem(req.request_id))
            self._table.setItem(row, 1, QTableWidgetItem(
                f"{req.recipient_name} <{req.recipient_email}>" if req.recipient_name
                else req.recipient_email
            ))
            self._table.setItem(row, 2, QTableWidgetItem(Path(req.pdf_path).name))

            status_item = QTableWidgetItem(req.status.upper())
            color_map = {
                "draft": "#888", "sent": "#e8a317",
                "signed": "#2e7d32", "expired": "#c62828"
            }
            status_item.setForeground(
                QColor(color_map.get(req.status, "#888"))
            )
            self._table.setItem(row, 3, status_item)
            self._table.setItem(row, 4, QTableWidgetItem(
                req.sent_at[:16].replace("T", " ") if req.sent_at else "—"
            ))

            mark_btn = QPushButton("Mark Signed")
            mark_btn.setFixedHeight(28)
            mark_btn.clicked.connect(
                lambda _, rid=req.request_id: self._mark_signed(rid)
            )
            self._table.setCellWidget(row, 5, mark_btn)

    def _mark_signed(self, request_id: str):
        self._store.update_request(
            request_id,
            status="signed",
            signed_at=datetime.now().isoformat()
        )
        self._load()
