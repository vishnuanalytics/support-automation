"""
Phase 12 — ingest a directory of Markdown files as a named KB source.

Reuses the scraper's structure-aware chunker + local embedder. Rows land in
`zapier_docs` / `doc_chunks` (the shared content tables) tagged with the
source's `source_id`, so the `retrieve` node can scope to it via
`config.kb_sources`.

    python -m ingestion.sources.markdown_source \
        --name globex-sop --tenant 22222222-2222-2222-2222-222222222222 \
        --dir ingestion/sources/globex_sop

Re-running replaces that source's chunks (idempotent).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
load_dotenv()

from ingestion.scraper import get_supabase, normalise  # noqa: E402
from ingestion.sources.kb_common import embed_entry  # noqa: E402


def _source_id(sb, name: str, tenant: str | None, kind: str) -> str:
    rows = sb.table("sources").select("source_id").eq("name", name).execute().data
    if rows:
        return rows[0]["source_id"]
    return sb.table("sources").insert(
        {"name": name, "tenant_id": tenant, "kind": kind}
    ).execute().data[0]["source_id"]


def ingest(name: str, tenant: str | None, directory: str, kind: str = "markdown") -> None:
    sb = get_supabase()
    sid = _source_id(sb, name, tenant, kind)
    files = sorted(pathlib.Path(directory).glob("*.md"))
    if not files:
        sys.exit(f"no .md files in {directory}")

    total_chunks = 0
    for f in files:
        url = f"sop://{name}/{f.stem}"
        md = normalise(f.read_text())
        title = md.splitlines()[0].lstrip("# ").strip() if md else f.stem
        n = embed_entry(sb, source_id=sid, url=url, title=title, body_md=md,
                        section=name, crumb=f"{name} > {f.stem}")
        total_chunks += n
        print(f"  {url}: {n} chunks")

    print(f"\nsource '{name}' ({sid}): {len(files)} docs, {total_chunks} chunks")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--tenant", default=None)
    ap.add_argument("--dir", required=True)
    ap.add_argument("--kind", default="markdown")
    args = ap.parse_args()
    ingest(args.name, args.tenant, args.dir, args.kind)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
