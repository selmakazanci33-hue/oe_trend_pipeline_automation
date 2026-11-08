SELECT
    [coverage_year] AS coverage_year,
    YEAR([enrollment_confirmation_date]) AS confirmation_calendar_year,
    MONTH([enrollment_confirmation_date]) AS confirmation_calendar_month,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
  AND [enrollment_confirmation_date] IS NOT NULL
GROUP BY [coverage_year], YEAR([enrollment_confirmation_date]), MONTH([enrollment_confirmation_date])
ORDER BY [coverage_year], confirmation_calendar_year, confirmation_calendar_month

WITH first_confirmation AS (
    SELECT
        [coverage_year] AS coverage_year,
        [enrollee_id] AS identity_key,
        MIN([enrollment_confirmation_date]) AS first_confirmation_date
    FROM {{qualified_object}}
    WHERE [coverage_year] IN (?, ?)
      AND [enrollment_confirmation_date] IS NOT NULL
      AND [enrollee_id] IS NOT NULL
    GROUP BY [coverage_year], [enrollee_id]
)
SELECT
    coverage_year,
    YEAR(first_confirmation_date) AS confirmation_calendar_year,
    MONTH(first_confirmation_date) AS confirmation_calendar_month,
    COUNT_BIG(*) AS person_count
FROM first_confirmation
GROUP BY coverage_year, YEAR(first_confirmation_date), MONTH(first_confirmation_date)
ORDER BY coverage_year, confirmation_calendar_year, confirmation_calendar_month

WITH first_confirmation AS (
    SELECT
        [coverage_year] AS coverage_year,
        [enrollment_id] AS identity_key,
        MIN([enrollment_confirmation_date]) AS first_confirmation_date
    FROM {{qualified_object}}
    WHERE [coverage_year] IN (?, ?)
      AND [enrollment_confirmation_date] IS NOT NULL
      AND [enrollment_id] IS NOT NULL
    GROUP BY [coverage_year], [enrollment_id]
)
SELECT
    coverage_year,
    YEAR(first_confirmation_date) AS confirmation_calendar_year,
    MONTH(first_confirmation_date) AS confirmation_calendar_month,
    COUNT_BIG(*) AS enrollment_count
FROM first_confirmation
GROUP BY coverage_year, YEAR(first_confirmation_date), MONTH(first_confirmation_date)
ORDER BY coverage_year, confirmation_calendar_year, confirmation_calendar_month
