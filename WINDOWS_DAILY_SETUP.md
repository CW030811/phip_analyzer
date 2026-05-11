# Windows Daily Email Automation

This project now supports a weekday 11:00 scheduled run that scans HKEX PHIP,
generates pending reports, and emails a daily summary.

## Commands

Validate SMTP settings without sending:

```bat
python main.py test-email --dry-run
```

Send one test email:

```bat
python main.py test-email
```

Run the daily flow immediately:

```bat
python main.py daily --max-items 5 --since-days 30
```

## Schedule

Register the Windows Task Scheduler job:

```bat
scripts\register_daily_task.bat
```

The task name is `PHIP Analyzer Daily`. It runs Monday to Friday at 11:00.
The task executes an ASCII-path launcher to avoid Windows Task Scheduler
encoding issues with the Chinese project path:

```text
C:\Users\WIN\.codex\memories\phip_daily_launcher.ps1
```

The launcher calls the project script through this junction:

```bat
C:\Users\WIN\.codex\memories\phip_analyzer_link\scripts\daily.ps1
```

Logs are written to:

```text
logs\daily_*.log
```

## Email Settings

The QQ Mail SMTP settings live in `.env`:

```text
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_SSL=true
SMTP_USER=your_qq_number@qq.com
SMTP_PASSWORD=your_qq_mail_authorization_code
SMTP_FROM=your_qq_number@qq.com
SMTP_TO=your_recipient@example.com
SMTP_SENDER_NAME=PHIP Analyzer
```

QQ Mail requires the SMTP authorization code, not the normal QQ password.
