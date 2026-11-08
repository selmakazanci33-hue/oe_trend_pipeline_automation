SELECT
    [coverage_year] AS coverage_year,
    [application_type] AS application_type,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
GROUP BY [coverage_year], [application_type]
ORDER BY [coverage_year], [application_type]
