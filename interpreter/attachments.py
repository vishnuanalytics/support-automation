"""
Phase 25 — image **and video** attachments on a support Case.

Customers attach screenshots to *show* the issue or *share details* (an error
dialog, an invoice, a log), and screen recordings to walk through a repro.
This module fetches the media linked to a Salesforce Case and turns it into
plain text for `classify` / `draft` at low cost:

  * images  -> local OCR (RapidOCR, ONNX, no torch)
  * video   -> audio transcript (faster-whisper, CT2, no torch) + OCR of a
               handful of sampled keyframes (opt-in: `do_video`)

Visual understanding ("my dashboard looks wrong") is a separate opt-in vision
call in `ai_prompt` — this module also carries the raw image bytes / a couple
of video keyframes so that node can send them.

    from interpreter.attachments import extract
    out = extract(case, tenant_id=tid, do_video=True)
    # out = {"attachments": [{filename, mime, size, kind, ocr_text|transcript, sf_content_id}],
    #        "attachment_text": "<all extracted text joined>",
    #        "_blobs": {blob_key: bytes}}      # images + video keyframes; not persisted

Everything is best-effort — a missing file, no OCR / no Whisper, no ffmpeg,
or no SF creds never raises.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from typing import Any

log = logging.getLogger("interpreter.attachments")

_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}
_VIDEO_EXT = {"mp4", "mov", "webm", "mkv", "m4v", "avi", "ogv"}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
             "tif": "image/tiff", "tiff": "image/tiff",
             "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
             "mkv": "video/x-matroska", "m4v": "video/mp4", "avi": "video/x-msvideo",
             "ogv": "video/ogg"}

MAX_IMAGES = int(os.environ.get("ATTACH_MAX_IMAGES", "5") or 5)
MAX_BYTES = int(os.environ.get("ATTACH_MAX_BYTES", str(8 * 1024 * 1024)))
VIDEO_MAX_BYTES = int(os.environ.get("ATTACH_VIDEO_MAX_BYTES", str(80 * 1024 * 1024)))
VIDEO_MAX_SECONDS = int(os.environ.get("ATTACH_VIDEO_MAX_SECONDS", "300") or 300)
VIDEO_FRAMES = int(os.environ.get("ATTACH_VIDEO_FRAMES", "4") or 4)
WHISPER_MODEL = os.environ.get("ATTACH_WHISPER_MODEL", "base")

_ocr = None
_ocr_tried = False
_whisper = None
_whisper_tried = False


# ── OCR (images + video frames) ───────────────────────────────────
def _ocr_engine():
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


# ── video: transcript + keyframes ────────────────────────────────
def _whisper_model():
    global _whisper, _whisper_tried
    if _whisper_tried:
        return _whisper
    _whisper_tried = True
    try:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("attachments: faster-whisper (%s) ready", WHISPER_MODEL)
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: transcription unavailable (%s) — video audio skipped", e)
        _whisper = None
    return _whisper


def transcribe(data: bytes, *, max_seconds: int = VIDEO_MAX_SECONDS) -> str:
    """Speech in a video/audio blob -> text, or '' — never raises."""
    m = _whisper_model()
    if m is None or not data:
        return ""
    path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".media", delete=False) as f:
            f.write(data)
            path = f.name
        segs, _info = m.transcribe(path, beam_size=1, vad_filter=True,
                                   clip_timestamps=f"0,{max_seconds}")
        return " ".join(s.text.strip() for s in segs).strip()
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: transcribe failed (%d bytes): %s", len(data), e)
        return ""
    finally:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass


def _ffmpeg() -> str | None:
    for cand in (os.environ.get("FFMPEG_BIN"), "ffmpeg"):
        if not cand:
            continue
        try:
            subprocess.run([cand, "-version"], capture_output=True, check=True, timeout=10)
            return cand
        except Exception:  # noqa: BLE001
            continue
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def video_frames(data: bytes, *, n: int = VIDEO_FRAMES,
                 max_seconds: int = VIDEO_MAX_SECONDS) -> list[bytes]:
    """`n` evenly-spaced PNG frames from the first `max_seconds` — [] on any
    failure (no ffmpeg, corrupt file, …)."""
    ff = _ffmpeg()
    if ff is None or not data or n < 1:
        return []
    d = tempfile.mkdtemp(prefix="vid_")
    src = os.path.join(d, "in.media")
    try:
        with open(src, "wb") as f:
            f.write(data)
        # fps = n frames across the clip window
        fps = max(1, n) / max(1, min(max_seconds, 600))
        subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error", "-t", str(max_seconds),
             "-i", src, "-vf", f"fps={fps:.4f}", "-frames:v", str(n),
             os.path.join(d, "f_%03d.png")],
            capture_output=True, check=True, timeout=120,
        )
        out = []
        for name in sorted(os.listdir(d)):
            if name.startswith("f_") and name.endswith(".png"):
                with open(os.path.join(d, name), "rb") as f:
                    out.append(f.read())
        return out[:n]
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: frame extraction failed (%d bytes): %s", len(data), e)
        return []
    finally:
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def process_video(data: bytes, name: str, *, n_frames: int = VIDEO_FRAMES,
                  max_seconds: int = VIDEO_MAX_SECONDS) -> dict[str, Any]:
    """{transcript, frame_ocr, frames:[bytes]} for one video — never raises."""
    transcript = transcribe(data, max_seconds=max_seconds)
    frames = video_frames(data, n=n_frames, max_seconds=max_seconds)
    frame_ocr = "\n".join(t for t in (ocr_bytes(fr) for fr in frames) if t).strip()
    return {"transcript": transcript, "frame_ocr": frame_ocr, "frames": frames}


# ── Salesforce fetch ─────────────────────────────────────────────
def _sf_case_files(case_id: str, tenant_id: str | None, *, want_video: bool,
                   img_limit: int) -> list[dict]:
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
    n_img = 0
    for cv in cvs:
        ext = (cv.get("FileExtension") or "").lower()
        size = cv.get("ContentSize") or 0
        if ext in _IMAGE_EXT and size <= MAX_BYTES and n_img < img_limit:
            data = _sf_blob(sf, cv["Id"], MAX_BYTES)
            if data:
                out.append(_rec(cv, ext, data, "image"))
                n_img += 1
        elif want_video and ext in _VIDEO_EXT and size <= VIDEO_MAX_BYTES:
            data = _sf_blob(sf, cv["Id"], VIDEO_MAX_BYTES)
            if data:
                out.append(_rec(cv, ext, data, "video"))
    return out


def _rec(cv: dict, ext: str, data: bytes, kind: str) -> dict:
    return {"filename": f"{cv.get('Title') or cv['Id']}.{ext}",
            "mime": _EXT_MIME.get(ext, "application/octet-stream"),
            "size": len(data), "sf_content_id": cv["Id"], "kind": kind, "data": data}


def _sf_blob(sf, content_version_id: str, max_bytes: int) -> bytes | None:
    try:
        url = f"{sf.base_url}sobjects/ContentVersion/{content_version_id}/VersionData"
        r = sf.session.get(url, headers={"Authorization": f"Bearer {sf.session_id}"},
                           timeout=60)
        r.raise_for_status()
        return r.content if len(r.content) <= max_bytes else None
    except Exception as e:  # noqa: BLE001
        log.warning("attachments: blob fetch %s failed: %s", content_version_id, e)
        return None


# ── public ───────────────────────────────────────────────────────
def extract(case: dict, *, tenant_id: str | None = None, limit: int | None = None,
            do_ocr: bool = True, do_video: bool = False,
            video_frames_n: int | None = None, video_max_seconds: int | None = None,
            source: str = "salesforce") -> dict[str, Any]:
    """Media attachments for `case` + extracted text. See module docstring."""
    limit = limit or MAX_IMAGES
    nfr = video_frames_n or VIDEO_FRAMES
    vsec = video_max_seconds or VIDEO_MAX_SECONDS
    case_id = case.get("sf_id") or case.get("id")

    raw: list[dict] = []
    if source in ("salesforce", "auto"):
        raw = _sf_case_files(case_id, tenant_id, want_video=do_video, img_limit=limit)
    for a in (case.get("_inbound_attachments") or []) if source in ("email", "auto") else []:
        mime = a.get("mime") or ""
        if not a.get("data"):
            continue
        if mime.startswith("image/"):
            raw.append({"filename": a.get("filename") or "image", "mime": mime,
                        "size": len(a["data"]), "sf_content_id": None,
                        "kind": "image", "data": a["data"]})
        elif do_video and mime.startswith("video/"):
            raw.append({"filename": a.get("filename") or "video", "mime": mime,
                        "size": len(a["data"]), "sf_content_id": None,
                        "kind": "video", "data": a["data"]})

    attachments, blobs, texts = [], {}, []
    for a in raw:
        rec = {k: a[k] for k in ("filename", "mime", "size", "sf_content_id", "kind")}
        if a["kind"] == "image":
            txt = ocr_bytes(a["data"]) if do_ocr else ""
            rec["ocr_text"] = txt
            key = a["sf_content_id"] or f"inline:{len(blobs)}"
            blobs[key] = a["data"]
            rec["blob_key"] = key
            if txt:
                texts.append(f"[image {rec['filename']}]\n{txt}")
        else:  # video
            v = process_video(a["data"], a["filename"], n_frames=nfr, max_seconds=vsec)
            rec["transcript"] = v["transcript"]
            rec["frame_ocr"] = v["frame_ocr"]
            rec["frame_count"] = len(v["frames"])
            for i, fr in enumerate(v["frames"][:2]):     # a couple of keyframes for vision
                fk = f"{a['sf_content_id'] or 'inline'}#f{i}"
                blobs[fk] = fr
                rec.setdefault("frame_keys", []).append(fk)
            block = [f"[video {rec['filename']}]"]
            if v["transcript"]:
                block.append(f"narration: {v['transcript']}")
            if v["frame_ocr"]:
                block.append(f"on-screen text: {v['frame_ocr']}")
            if len(block) > 1:
                texts.append("\n".join(block))
        attachments.append(rec)

    return {"attachments": attachments,
            "attachment_text": "\n\n".join(texts).strip(),
            "_blobs": blobs}
