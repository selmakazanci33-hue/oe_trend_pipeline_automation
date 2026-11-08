-- Phase 4B metadata only. No enrollment rows are read.
-- Historical table names are not referenced. Plan year is not inferred here.
SELECT
    c.TABLE_SCHEMA AS table_schema,
    c.TABLE_NAME AS table_name,
    t.TABLE_TYPE AS table_type,
    c.COLUMN_NAME AS column_name,
    c.DATA_TYPE AS data_type,
    c.CHARACTER_MAXIMUM_LENGTH AS character_maximum_length,
    c.NUMERIC_PRECISION AS numeric_precision,
    c.NUMERIC_SCALE AS numeric_scale,
    c.DATETIME_PRECISION AS datetime_precision
FROM INFORMATION_SCHEMA.COLUMNS AS c
INNER JOIN INFORMATION_SCHEMA.TABLES AS t
    ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
   AND t.TABLE_NAME = c.TABLE_NAME
WHERE t.TABLE_TYPE IN ('BASE TABLE', 'VIEW')
  AND c.TABLE_SCHEMA NOT IN ('INFORMATION_SCHEMA', 'sys')
  AND c.COLUMN_NAME IN (
        'coverage_year',
        'enrollment_id',
        'enrollee_id',
        'hios_issuer_id',
        'enrollment_status_description',
        'enrollee_status_description',
        'application_type',
        'enrollment_confirmation_date',
        'enrollment_create_date',
        'enrollment_last_update_date',
        'benefit_effective_date',
        'benefit_end_date'
  )
ORDER BY c.TABLE_SCHEMA, c.TABLE_NAME, c.COLUMN_NAME;
