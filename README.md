# oe_trend_pipeline

Phase 1: retrieve the live PY2027 **Currently Enrolled People** metric from the
Georgia Access Sisense environment through an authenticated browser session and
the verified JAQL endpoint `POST /api/datasources/MAIN/jaql`.

## How it runs

```
Python environment with the Playwright package
        +
existing Microsoft Edge (or Google Chrome)
        ↓
Playwright launches the installed browser (headed)
        ↓
normal Sisense login (password prompted, never stored)
        ↓
authenticated browser context
        ↓
verified JAQL request
```

No browser is downloaded. Playwright drives the browser already installed on
the machine, selected by `PLAYWRIGHT_BROWSER_CHANNEL` (`msedge` by default, or
`chrome`). If that browser cannot be launched the run stops with
`CONFIGURED_BROWSER_NOT_AVAILABLE`; it never falls back to another browser.

## Windows setup (PowerShell)

```powershell
cd C:\path\to\oe_trend_pipeline_automation

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env
```

In `.env`, set `SISENSE_EMAIL` to your work email. Leave
`PLAYWRIGHT_BROWSER_CHANNEL=msedge` unless you want Chrome. Never put the
password in `.env`.

## Run

```powershell
.\.venv\Scripts\python.exe main.py
echo $LASTEXITCODE
```

The browser opens visibly. When the login form appears, the terminal asks for
`Sisense password:` (hidden input). Complete any SSO/MFA in the browser window.

Use PowerShell, Command Prompt, or Windows Terminal; Git Bash can break the
hidden password prompt.

Exit codes: `0` success, `1` failure (including
`CONFIGURED_BROWSER_NOT_AVAILABLE`), `2` verified information missing.

## Unit tests

Unit tests use fakes only; outbound network connections are blocked during the
test run.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## Runtime files

`runtime/auth/sisense_state.json` (saved session) and `runtime/artifacts/`
(failure screenshots) are created automatically and are git-ignored, as is
`.env`.
