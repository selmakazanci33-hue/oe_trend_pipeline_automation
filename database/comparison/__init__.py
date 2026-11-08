"""Read-only PY2026 comparison discovery. No passwords. No write SQL.

Run explicitly:

- ``python -m database.comparison.discovery``
- ``python -m database.comparison.field_mapping``
- ``python -m database.comparison.event_date_validation``

``main.py`` does not import this package. Sisense is not contacted.
PY2027 enrollment rows are not queried.
"""
