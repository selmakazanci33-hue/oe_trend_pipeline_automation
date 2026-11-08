SELECT
    [coverage_year] AS coverage_year,
    COUNT_BIG(*) AS raw_rows,
    COUNT_BIG(DISTINCT [enrollment_id]) AS distinct_enrollment_id,
    COUNT_BIG(DISTINCT [enrollee_id]) AS distinct_enrollee_id,
    COUNT_BIG(DISTINCT [hios_issuer_id]) AS distinct_issuer_count,
    MIN([enrollment_confirmation_date]) AS min_enrollment_confirmation_date,
    MAX([enrollment_confirmation_date]) AS max_enrollment_confirmation_date,
    MIN([enrollment_create_date]) AS min_enrollment_create_date,
    MAX([enrollment_create_date]) AS max_enrollment_create_date,
    MIN([benefit_effective_date]) AS min_benefit_effective_date,
    MAX([benefit_effective_date]) AS max_benefit_effective_date,
    SUM(CASE WHEN [enrollment_id] IS NULL THEN 1 ELSE 0 END) AS null_enrollment_id,
    SUM(CASE WHEN [enrollee_id] IS NULL THEN 1 ELSE 0 END) AS null_enrollee_id,
    SUM(CASE WHEN [hios_issuer_id] IS NULL THEN 1 ELSE 0 END) AS null_hios_issuer_id,
    SUM(CASE WHEN [enrollment_confirmation_date] IS NULL THEN 1 ELSE 0 END) AS null_enrollment_confirmation_date,
    SUM(CASE WHEN [enrollment_status_description] IS NULL THEN 1 ELSE 0 END) AS null_enrollment_status_description,
    SUM(CASE WHEN [enrollee_status_description] IS NULL THEN 1 ELSE 0 END) AS null_enrollee_status_description,
    SUM(CASE WHEN [application_type] IS NULL THEN 1 ELSE 0 END) AS null_application_type
FROM {{qualified_object}}
WHERE [coverage_year] IN (?, ?)
GROUP BY [coverage_year]
ORDER BY [coverage_year]
