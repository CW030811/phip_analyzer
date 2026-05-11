"""Email delivery helpers for scheduled PHIP reports."""
from __future__ import annotations

import mimetypes
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Iterable


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    use_ssl: bool
    username: str
    password: str
    sender: str
    sender_name: str
    recipients: list[str]

    @classmethod
    def from_env(cls, *, default_to: str | None = None) -> "EmailConfig":
        username = os.getenv("SMTP_USER", "").strip()
        password = (
            os.getenv("SMTP_PASSWORD", "").strip()
            or os.getenv("QQ_MAIL_AUTH_CODE", "").strip()
        )
        recipient_text = (
            os.getenv("SMTP_TO", "").strip()
            or os.getenv("MAIL_TO", "").strip()
            or (default_to or "")
        )
        recipients = [
            item.strip()
            for item in recipient_text.replace(";", ",").split(",")
            if item.strip()
        ]
        if not username:
            raise RuntimeError("SMTP_USER is not configured in .env")
        if not password:
            raise RuntimeError("SMTP_PASSWORD / QQ_MAIL_AUTH_CODE is not configured in .env")
        if not recipients:
            raise RuntimeError("SMTP_TO / MAIL_TO is not configured in .env")

        return cls(
            host=os.getenv("SMTP_HOST", "smtp.qq.com").strip() or "smtp.qq.com",
            port=int(os.getenv("SMTP_PORT", "465")),
            use_ssl=_truthy(os.getenv("SMTP_SSL"), default=True),
            username=username,
            password=password,
            sender=os.getenv("SMTP_FROM", "").strip() or username,
            sender_name=os.getenv("SMTP_SENDER_NAME", "PHIP Analyzer").strip()
            or "PHIP Analyzer",
            recipients=recipients,
        )


def send_email(
    *,
    subject: str,
    body: str,
    attachments: Iterable[str | Path] = (),
    config: EmailConfig | None = None,
) -> None:
    cfg = config or EmailConfig.from_env()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.sender_name, cfg.sender))
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(body)

    for raw_path in attachments:
        path = Path(raw_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Attachment not found: {path}")
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with path.open("rb") as f:
            msg.add_attachment(
                f.read(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )

    if cfg.use_ssl:
        with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=60) as smtp:
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(cfg.host, cfg.port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
