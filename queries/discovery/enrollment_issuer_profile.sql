SELECT
    [coverage_year] AS coverage_year,
    [hios_issuer_id] AS hios_issuer_id,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
GROUP BY [coverage_year], [hios_issuer_id]
ORDER BY [coverage_year], [hios_issuer_id]
