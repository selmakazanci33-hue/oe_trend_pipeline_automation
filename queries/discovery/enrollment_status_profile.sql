SELECT
    [coverage_year] AS coverage_year,
    [enrollment_status_description] AS enrollment_status_description,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
GROUP BY [coverage_year], [enrollment_status_description]
ORDER BY [coverage_year], [enrollment_status_description]

SELECT
    [coverage_year] AS coverage_year,
    [enrollee_status_description] AS enrollee_status_description,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
GROUP BY [coverage_year], [enrollee_status_description]
ORDER BY [coverage_year], [enrollee_status_description]
