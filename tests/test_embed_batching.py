"""Indexing embeds in small batches (EMBED_BATCH_SIZE, default 4) to cap
ONNX Runtime's memory. That's only safe if batch size never changes the
vectors -- the integration test below proves it on the real model with
texts of very different lengths (the case where in-batch padding differs).
"""

from __future__ import annotations

import numpy as np
import pytest

from ingestion import scraper


def test_embed_texts_passes_the_configured_batch_size(monkeypatch):
    seen = {}

    class _Fake:
        def embed(self, texts, batch_size=None):
            seen["batch_size"] = batch_size
            return [np.array([float(len(t))]) for t in texts]

    monkeypatch.setattr(scraper, "get_embedder", lambda: _Fake())
    monkeypatch.setattr(scraper, "EMBED_BATCH_SIZE", 4)
    assert scraper.embed_texts(["ab", "abcd"]) == [[2.0], [4.0]]
    assert seen["batch_size"] == 4


def test_kb_entries_are_embedded_through_the_batched_helper(monkeypatch):
    from ingestion.sources import kb_common

    calls = []
    monkeypatch.setattr(kb_common, "embed_texts",
                        lambda texts: calls.append(texts) or [[0.0] * 384 for _ in texts])

    class _T:
        def __getattr__(self, _n):
            return lambda *a, **k: self

        def execute(self):
            return type("R", (), {"data": []})()

    class _SB:
        def table(self, _n):
            return _T()

    n = kb_common.embed_entry(_SB(), source_id="s", url="kb://x/1", title="T",
                              body_md="# Heading\n\n" + "Some useful body text. " * 40,
                              section="kb")
    assert n >= 1 and calls and len(calls[0]) == n


@pytest.mark.integration
def test_batch_size_does_not_change_the_vectors():
    texts = [
        "webhook",
        "How do I get the webhook URL for a Catch Hook trigger?",
        ("Zapier webhooks let your app send data to a Zap with a POST request. "
         "Copy the custom URL from the trigger step, send a test request, then "
         "pull in the sample so later steps can map its fields. ") * 8,
        "Billing: plans, invoices and refunds.",
        "x " * 300,
    ] * 3
    emb = scraper.get_embedder()
    big = np.array(list(emb.embed(texts, batch_size=256)))
    for bs in (1, 4, 16):
        small = np.array(list(emb.embed(texts, batch_size=bs)))
        cos = (big * small).sum(1) / (np.linalg.norm(big, axis=1) * np.linalg.norm(small, axis=1))
        assert cos.min() > 0.99999, f"batch_size={bs}: min cosine {cos.min():.7f}"
