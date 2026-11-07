-- Case Monitor: keep each resolved case's record type, so aggregator reports don't
-- count ordinary support cases.

alter table monitor_resolved add column if not exists record_type text;
