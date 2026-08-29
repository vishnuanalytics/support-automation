-- Phase 12: point the Globex support flow at its own SOP source first.
--
-- The `globex-sop` source (tenant 2222…, 4 Markdown docs / 8 chunks) is
-- ingested by:
--   python -m ingestion.sources.markdown_source --name globex-sop \
--     --tenant 22222222-2222-2222-2222-222222222222 \
--     --dir ingestion/sources/globex_sop
-- (content lives in the repo at that path). This migration just adds
-- `kb_sources` to the flow's retrieve node — `["globex-sop", "zapier-public"]`
-- means "our SOP wins, fall back to the public docs". Acme's flows keep no
-- `kb_sources` = search everything (unchanged).

update flow_nodes
set config = config || '{"kb_sources": ["globex-sop", "zapier-public"]}'::jsonb
where flow_id = 'a2a2a2a2-2222-4222-8222-222222222222'
  and type = 'retrieve';
