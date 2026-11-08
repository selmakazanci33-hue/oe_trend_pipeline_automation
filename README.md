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

## Verified dashboard metrics (passive, production)

Besides Currently Enrolled People (the application request), every normal run
reads production-VERIFIED metrics from the JAQL responses the dashboard itself
already received — no additional request. Currently: **% Effectuated**, the
second measure of the verified Currently Enrolled widget.

A value is accepted only when the source request matches the verified
definition exactly (dashboard, widget, datasource, every measure and scope
filter; `queryGuid` ignored), the latest such response is HTTP 200, the value
binds to the exact measure title and passes type checks, and the Currently
Enrolled value in the same response equals the application value (canary).
Otherwise that metric alone is `NOT AVAILABLE` (or `STALE` from its last
snapshot) and nothing is stored for it. Passive values are stored only when
the application request also succeeded. % Effectuated is stored as the raw
ratio and shown as a percentage; its changes are shown in percentage points.

Header semantics: **Data Status** summarizes every production metric — `LIVE`
(all live), `PARTIAL` (some live), `STALE` (none live, some history) or
`NOT AVAILABLE`; each card keeps its own status. **Last Successful Refresh**
is the latest time any production metric was validated (this run's live
values or stored snapshots). **Pipeline Status** is this run's retrieval
result.

Applications Started, Submitted Applications, Total Applicants, New, Active
and Passive are **validation only** (PENDING in the dashboard). With
`SISENSE_RUNTIME_VALIDATION=true` the validation report adds a promotion
evidence section per candidate (widget, datasource, exact measure titles,
bound measure, value, HTTP status, definition fingerprint, eligibility) and a
shared-widget check for New/Active/Passive. When the Currently Enrolled
control passes, a value-free observation (identities, exact measure titles,
fingerprints) is appended to `data/history/wave1_fingerprint_observations.json`;
the next run compares against it: `STABLE`, `CHANGED`, `MISSING` or
`NO BASELINE`. Nothing is promoted automatically: promotion means setting
`expected_fingerprint` (and the exact measure title) in `metrics/catalog.py`
and moving the metric into `METRICS` after human review.

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
values of the seven Wave 1 metrics (Applications Started, Submitted
Applications, Total Applicants, % Effectuated, New, Active, Passive) from the
responses the dashboard already received. Each value is bound to its exact
measure definition in the dashboard's own request and checked by type; anything
ambiguous fails closed. Cancelled and Terminated stay PENDING.

Output: `reports/validation/wave1_runtime_validation.html` (+ `.json`) and a
console summary, including the Currently Enrolled passive-vs-application
control and, per widget, a `definition_fingerprint` (SHA-256 of the request's
dashboard, widget, datasource, measure and filter definitions; no filter
values). Values here are for comparison with the Sisense UI only: never stored
in history and never promoted automatically.

```powershell
$env:SISENSE_RUNTIME_VALIDATION="true"; .\.venv\Scripts\python.exe main.py
```

## Enrollment database source

Sisense and the enrollment database are separate sources. `main.py` retrieves
Sisense metrics only and does not open a database connection.

The database package reads FFM/enrollment records from SQL Server using
`Authentication=ActiveDirectoryInteractive` (browser sign-in, no database
password). Settings in `.env` are `DB_SERVER`, `DB_DATABASE`, `DB_USERNAME`,
and optionally `DB_DRIVER` and `DB_CONNECTION_TIMEOUT`. The server value is
used as written. Nothing from the connection is printed. After those values
are filled in, check the connection manually:

```powershell
.\.venv\Scripts\python.exe -m database.check_connection
```

The command prints `Database connection: PASS` or `Database connection: FAILED`.

Source discovery is a separate command. It does not run from `main.py` and it
does not contact Sisense. It writes aggregate counts only:

```powershell
.\.venv\Scripts\python.exe -m database.discovery.enrollment_source
```

Plan year comes from `coverage_year`, not from a table name or a calendar year.

The first Currently Enrolled People comparison is a separate command. It reads the
verified Sisense payload locally, does not call Sisense, and does not open the
database. It writes a comparison artifact and stops when the period is not proven:

```powershell
.\.venv\Scripts\python.exe -m comparison.currently_enrolled
```

The approved way to refresh the live PY2027 metric remains `main.py`. The
comparison command does not run it.

Lifecycle alignment is a separate command. It reads the stored Currently
Enrolled People observation, shifts that UTC date back one calendar year, and
catalogs PY2026 snapshots. It does not send JAQL and it does not run from
`main.py`. A filename date is not treated as the as-of date.

```powershell
.\.venv\Scripts\python.exe -m comparison.lifecycle
```

A diagnostic people-count comparison is a separate command. It uses the stored
PY2027 Currently Enrolled People observation and a filename-dated PY2026
snapshot. It is not an official year-over-year metric. If no observation is
stored, run `main.py` first.

```powershell
.\.venv\Scripts\python.exe -m comparison.diagnostic_numbers
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
