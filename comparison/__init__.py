"""PY2026 database vs PY2027 Sisense comparison gate.

Run explicitly:

- ``python -m comparison.currently_enrolled``
- ``python -m comparison.lifecycle``
- ``python -m comparison.diagnostic_numbers``
- ``python -m comparison.monthly_population``
- ``python -m comparison.latest_state``
- ``python -m comparison.comparability``

``main.py`` does not import or run this package. None of the commands send JAQL.
The diagnostic command is not an official year-over-year metric.
``comparison.comparability`` is the PY2026 vs PY2027 foundation report. It does
not invent PY2026 equivalents and does not assign COMPARABLE without semantic checks.
"""
