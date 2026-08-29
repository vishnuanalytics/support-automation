"""
Shared "one document -> chunks + embeddings in the content tables" helper.

Used by both `markdown_source.py` (a directory of .md files, CLI) and the
Phase 14 KB API (`api/kb.py`, one user-authored entry at a time). Rows land
in the shared `zapier_docs` / `doc_chunks` tables tagged with `source_id`,
so retrieval scoping (`resolve_sources` + `p_source_ids`) works unchanged.
"""

from __future__ import annotations

from ingestion.scraper import chunk_markdown, content_hash, get_embedder, normalise


def embed_entry(sb, *, source_id: str, url: str, title: str, body_md: str,
                section: str, crumb: str | None = None) -> int:
    """Upsert one logical document and replace its chunks. Returns the chunk
    count. `sb` must be able to write `zapier_docs` / `doc_chunks` (service
    role — those tables are not tenant-RLS'd for writes)."""
    md = normalise(body_md or "")
    crumb = crumb or f"{section} > {title}"

    sb.table("zapier_docs").upsert({
        "url": url, "title": title or url, "content_hash": content_hash(md),
        "raw_text": md, "status": "active", "missed_runs": 0,
        "source_id": source_id,
    }).execute()
    sb.table("doc_chunks").delete().eq("doc_url", url).execute()

    chunks = chunk_markdown(md, crumb) if md.strip() else []
    if chunks:
        vecs = [v.tolist() for v in get_embedder().embed([c["chunk_text"] for c in chunks])]
        sb.table("doc_chunks").insert([
            {
                "doc_url": url, "chunk_index": i, "chunk_text": c["chunk_text"],
                "embedding": e, "heading_path": c["heading_path"],
                "chunk_type": c["chunk_type"], "token_count": c["token_count"],
                "section": section, "source_id": source_id,
            }
            for i, (c, e) in enumerate(zip(chunks, vecs))
        ]).execute()
    return len(chunks)


def delete_entry(sb, *, url: str) -> None:
    """Soft-delete: drop the chunks, mark the doc row deleted (mirrors the
    `zapier_docs.status` soft-delete rule — never hard-delete ingested rows)."""
    sb.table("doc_chunks").delete().eq("doc_url", url).execute()
    sb.table("zapier_docs").update({"status": "deleted"}).eq("url", url).execute()
