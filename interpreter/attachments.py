"""
Phase 25 — image attachments on a support Case.

Customers attach screenshots to *show* the issue or to *share details*
(an error dialog, an invoice, a log). This module fetches the image files
linked to a Salesforce Case and runs local OCR on them, so the text in a
screenshot becomes plain context for `classify` / `draft` at **zero** LLM
cost. Visual understanding ("my dashboard looks wrong") is a separate,
opt-in vision call in the `ai_prompt` node — this module just also carries
the raw bytes so that node can send them.

    from interpreter.attachments import extract
    out = extract(case, tenant_id=tid)
    # out = {"attachments": [{filename, mime, size, ocr_text, sf_content_id}],
    #        "attachment_text": "<all OCR joined>",
    #        "_blobs": {sf_content_id: bytes}}   # not persisted

Everything is best-effort — a missing file, no OCR engine, or no SF creds
never raises.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("interpreter.attachments")

_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
             "tif": "image/tiff", "tiff": "image/tiff"}

MAX_IMAGES = int(os.environ.get("ATTACH_MAX_IMAGES", "5") or 5)
MAX_BYTES = int(os.environ.get("ATTACH_MAX_BYTES", str(8 * 1024 * 1024)))

_ocr = None
_ocr_tried = False


def _ocr_engine():
    """Lazy RapidOCR singleton (ONNX, CPU, no torch). None if not installed."""
    global _ocr, _ocr_tried
    if _ocr_tried:
        return _ocr
    _ocr_tried = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
        log.info("attachments: RapidOCR ready")
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: OCR unavailable (%s) — text-in-image will be skipped", e)
        _ocr = None
    return _ocr


def ocr_bytes(data: bytes) -> str:
    """Text found in an image, or '' — never raises."""
    eng = _ocr_engine()
    if eng is None or not data:
        return ""
    try:
        import io

        import numpy as np
        from PIL import Image

        img = np.array(Image.open(io.BytesIO(data)).convert("RGB"))
        res, _ = eng(img)
        lines = [str(r[1]).strip() for r in (res or []) if len(r) > 1 and str(r[1]).strip()]
        return "\n".join(lines).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: OCR failed on a %d-byte image: %s", len(data), e)
        return ""


# ── Salesforce fetch ───────────────────────────────────────────────
def _sf_case_images(case_id: str, tenant_id: str | None, limit: int) -> list[dict]:
    from interpreter import salesforce

    if not case_id or not salesforce.available():
        return []
    sf = salesforce.client_for(tenant_id)
    try:
        links = sf.query(
            "SELECT ContentDocumentId FROM ContentDocumentLink "
            f"WHERE LinkedEntityId = '{salesforce._soql_lit(case_id)}'"
        ).get("records", [])
        doc_ids = [r["ContentDocumentId"] for r in links if r.get("ContentDocumentId")]
        if not doc_ids:
            return []
        ids = ", ".join(f"'{salesforce._soql_lit(d)}'" for d in doc_ids[:50])
        cvs = sf.query(
            "SELECT Id, Title, FileExtension, FileType, ContentSize "
            f"FROM ContentVersion WHERE ContentDocumentId IN ({ids}) AND IsLatest = true "
            "ORDER BY CreatedDate DESC"
        ).get("records", [])
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: SF list failed for %s: %s", case_id, e)
        return []

    out: list[dict] = []
    for cv in cvs:
        ext = (cv.get("FileExtension") or "").lower()
        if ext not in _IMAGE_EXT or (cv.get("ContentSize") or 0) > MAX_BYTES:
            continue
        data = _sf_blob(sf, cv["Id"])
        if not data:
            continue
        out.append({
            "filename": f"{cv.get('Title') or cv['Id']}.{ext}",
            "mime": _EXT_MIME.get(ext, "image/png"),
            "size": len(data),
            "sf_content_id": cv["Id"],
            "data": data,
        })
        if len(out) >= limit:
            break
    return out


def _sf_blob(sf, content_version_id: str) -> bytes | None:
    try:
        url = f"{sf.base_url}sobjects/ContentVersion/{content_version_id}/VersionData"
        r = sf.session.get(url, headers={"Authorization": f"Bearer {sf.session_id}"},
                           timeout=30)
        r.raise_for_status()
        return r.content if len(r.content) <= MAX_BYTES else None
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: blob fetch %s failed: %s", content_version_id, e)
        return None


# ── public ─────────────────────────────────────────────────────────
def extract(case: dict, *, tenant_id: str | None = None, limit: int | None = None,
            do_ocr: bool = True, source: str = "salesforce") -> dict[str, Any]:
    """Image attachments for `case` + their OCR text. See module docstring."""
    limit = limit or MAX_IMAGES
    case_id = case.get("sf_id") or case.get("id")
    raw: list[dict] = []
    if source in ("salesforce", "auto"):
        raw = _sf_case_images(case_id, tenant_id, limit)
    # (email-MIME source: a future channel — the poller would pass images in
    #  on `case["_inbound_attachments"]`; wire when that channel exists.)
    for a in (case.get("_inbound_attachments") or []) if source in ("email", "auto") else []:
        if len(raw) >= limit:
            break
        if a.get("data") and (a.get("mime") or "").startswith("image/"):
            raw.append({"filename": a.get("filename") or "image", "mime": a["mime"],
                        "size": len(a["data"]), "sf_content_id": None, "data": a["data"]})

    attachments, blobs, texts = [], {}, []
    for a in raw:
        txt = ocr_bytes(a["data"]) if do_ocr else ""
        rec = {k: a[k] for k in ("filename", "mime", "size", "sf_content_id")}
        rec["ocr_text"] = txt
        attachments.append(rec)
        key = a["sf_content_id"] or f"inline:{len(blobs)}"
        blobs[key] = a["data"]
        rec["blob_key"] = key
        if txt:
            texts.append(f"[{rec['filename']}]\n{txt}")

    return {"attachments": attachments,
            "attachment_text": "\n\n".join(texts).strip(),
            "_blobs": blobs}
