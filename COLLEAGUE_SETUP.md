# Setup for a GUC Student

This repository is meant to be copied once per student. Do not share one running repo across multiple students because each monitor stores one state hash and one set of credentials.

## What You Need

- a GitHub account
- your GUC username and password
- a Gmail account with 2-Step Verification enabled
- a Gmail app password for SMTP

## GitHub Variables

Open your repository:

`Settings` -> `Secrets and variables` -> `Actions` -> `Variables` -> `New repository variable`

Add:

| Variable | Example | Required |
| --- | --- | --- |
| `EMAIL_TO` | `student@gmail.com` | No, defaults to `SMTP_USERNAME` |
| `TARGET_YEAR` | `2025-2026` | No, defaults to `2025-2026` |
| `TRANSCRIPT_URL` | `https://apps.guc.edu.eg/student_ext/Grade/Transcript_001.aspx` | No |
| `CHECK_START` | `09:00` | No |
| `CHECK_END` | `17:30` | No |
| `SKIP_DAYS` | `friday` | No |

For most students, set only:

```text
EMAIL_TO=your.email@gmail.com
TARGET_YEAR=2025-2026
```

If your browser shows a URL like `Transcript_001.aspx?v=SMP359651`, ignore the generated `v` part. The monitor strips it automatically and uses the stable transcript endpoint.

## GitHub Secrets

Open:

`Settings` -> `Secrets and variables` -> `Actions` -> `Secrets` -> `New repository secret`

Add:

| Secret | Example |
| --- | --- |
| `GUC_USERNAME` | `GUC\your.username` |
| `GUC_PASSWORD` | your GUC password |
| `SMTP_USERNAME` | your Gmail address |
| `SMTP_PASSWORD` | your Gmail app password, not your Gmail password |

If `GUC\your.username` fails, try just `your.username`.

## Required Proof Before Relying On It

Open `Actions` -> `Check GUC grades` -> `Run workflow`.

1. Run with `self_test_email=true`.
   You must receive `GUC grade monitor self-test`.

2. Run again with:

```text
self_test_email=false
force=true
send_current=true
```

You must receive today's transcript snapshot.

Only rely on scheduled monitoring after both emails arrive.

## How It Runs

- every 10 minutes during the Cairo working-day window
- skips Friday by default
- selects the configured `TARGET_YEAR`
- emails only when the watched transcript/evaluation text changes
- sends a failure email if the monitor or workflow breaks
