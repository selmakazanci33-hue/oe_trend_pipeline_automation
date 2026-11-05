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

## Snapshot history and report

Each normal run (not `SISENSE_DIAGNOSTICS_ONLY`) does:

1. authenticate, retrieve, validate (unchanged from above);
2. store one snapshot in `data/history/oe_metrics.sqlite3`, only after a validated value;
3. compute derived analytics from stored snapshots;
4. regenerate `reports/latest/index.html` (self-contained; open it in a browser).

One snapshot per metric per UTC hour (`SNAPSHOT_GRANULARITY=hour`, or `day`).
A second run in the same slot keeps the first stored value; the report still
shows the new live value, but it is not stored. A failed retrieval stores no
snapshot, records the run as `FAILED`, and regenerates the report from stored
history marked `STALE` (or `NO HISTORY`).

Comparisons (daily, 7-day, 30-day) show "Insufficient history" until enough
real snapshots exist. They are never filled with zero or a baseline.

`data/` and `reports/` contain internal business metrics and are git-ignored.
The database contains metric values and run status only; no credentials,
cookies, headers or payloads.

## Passive JAQL proof (validation only)

With `SISENSE_PASSIVE_PROOF=true`, the run also finds the dashboard's own JAQL
request for the verified widget, checks dashboard, widget, datasource,
primary measure and all business filters against the verified payload, reads
the response the browser already received, parses it with the same parser,
and prints `Passive Validation: PASS/FAIL` next to the application value.
It sends no extra request. The application value remains the only value
stored or reported; bodies are inspected in memory and never logged or saved.

```powershell
$env:SISENSE_PASSIVE_PROOF="true"; .\.venv\Scripts\python.exe main.py
```

## Wave 1 metric discovery (developer review only)

With `SISENSE_DISCOVERY=true`, the run waits until the dashboard's own JAQL
traffic is quiet, then summarizes each widget it loaded (titles, measure and
dimension titles, filter counts, HTTP status, response shape) and matches the
Wave 1 labels exactly (case and spacing only; no substrings). Results:

- `VERIFIED`: catalog metric with a verified payload (Currently Enrolled People), re-checked strictly;
- `DISCOVERED`: exactly one widget carries the label and returned a parseable HTTP 200 response;
- `PENDING`: no exact match, ambiguous match, or unusable response.

Output: `reports/discovery/wave1_metric_discovery.html` (+ `.json`). No values,
filter values or bodies are written. Discovered metrics are never stored in
history or shown in the executive dashboard. No extra request is sent.

```powershell
$env:SISENSE_DISCOVERY="true"; .\.venv\Scripts\python.exe main.py
```

## Wave 1 runtime validation (developer review only)

With `SISENSE_RUNTIME_VALIDATION=true` (implies discovery), the run reads the
values of the seven DISCOVERED metrics (Applications Started, Submitted
Applications, Total Applicants, % Effectuated, New, Active, Passive) from the
responses the dashboard already received. Each value is bound to its exact
measure definition in the dashboard's own request and checked by type; anything
ambiguous fails closed. Cancelled and Terminated stay PENDING.

Output: `reports/validation/wave1_runtime_validation.html` (+ `.json`) and a
console summary, including the Currently Enrolled passive-vs-application
control. Values are for comparison with the Sisense UI only: never stored in
history, never shown in the executive dashboard, never promoted automatically.

```powershell
$env:SISENSE_RUNTIME_VALIDATION="true"; .\.venv\Scripts\python.exe main.py
```

## Unit tests

Unit tests use fakes only; outbound network connections are blocked during the
test run.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## Runtime files

`runtime/auth/sisense_state.json` (saved session) and `runtime/artifacts/`
(failure screenshots) are created automatically and are git-ignored, as are
`.env`, `data/` (snapshot history) and `reports/` (generated dashboard).
