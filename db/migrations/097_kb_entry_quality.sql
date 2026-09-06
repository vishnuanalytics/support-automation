-- KB source connectors (docs/KB_SOURCE_CONNECTORS.md, cross-cutting #4):
-- a trust signal per document. `KBDocument.quality` — set by every connector
-- ("official" for a help article / Linear Document, "community_resolved" for
-- a shipped Linear issue or a done Nolt post, "unverified" for a manual
-- entry / a raw crawl) — was carried on the dataclass but never persisted.
--
-- Now stored on `kb_entries`, and `interpreter/retrieval.hybrid_retrieve`
-- nudges fused scores by it (official ×1.15, community_resolved ×1.0,
-- unverified ×0.9) so an official article outranks a random forum reply that
-- scored similarly. No CHECK — it's a soft signal, values are the
-- connector's business.

alter table kb_entries
  add column if not exists quality text not null default 'unverified';

comment on column kb_entries.quality is
  'Source-trust signal from KBDocument.quality: official | community_resolved '
  '| unverified. Weights retrieval scoring (interpreter/retrieval.py).';
