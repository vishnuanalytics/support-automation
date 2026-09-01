"""
Salesforce Pub/Sub API subscriber — a long-lived gRPC client that streams
Change Data Capture events for `Case` and `EmailMessage` and turns them
into `run_flow` jobs.

Why this exists (vs the Phase 20i Apex HTTP callout): the callout is
fire-and-forget — if our API is down when it fires, that event is lost, and
it only covers *new* Cases. CDC + Pub/Sub covers new Cases, inbound emails
on an existing Case, and queue (owner) changes in one subscription, and
every event carries a `replay_id` we persist (`sf_cdc_state`, migration
043) so a subscriber restart resumes exactly where it left off — Salesforce
retains events for 72h.

    from ingestion.sf_pubsub.subscriber import PubSubSubscriber
    PubSubSubscriber(get_supabase()).run()            # loop forever
    PubSubSubscriber(get_supabase()).run(max_events=5)  # drain a few, exit (tests)

Auth reuses `interpreter.salesforce` (JWT bearer). The access token is
passed as gRPC metadata; on an UNAUTHENTICATED mid-stream (token expiry)
the client mints a fresh one and resubscribes from the stored replay id.
"""

from __future__ import annotations

import io
import json
import logging
import queue
import threading
import time

import fastavro
import grpc

from interpreter import salesforce
from interpreter.sf_ingest import EntryFlowError, enqueue_case_run

from . import pubsub_api_pb2 as pb
from . import pubsub_api_pb2_grpc as pb_grpc
from .plan import plan_events

log = logging.getLogger("ingestion.sf_pubsub")

PUBSUB_ENDPOINT = "api.pubsub.salesforce.com:7443"
DEFAULT_TOPICS = ("/data/CaseChangeEvent", "/data/EmailMessageChangeEvent")
_WINDOW = 20          # flow-control: events requested per FetchRequest
_MAX_BACKOFF = 60.0


class _RequestStream:
    """Queue-backed iterator of FetchRequests for one Subscribe stream."""

    def __init__(self, first: pb.FetchRequest) -> None:
        self._q: queue.Queue = queue.Queue()
        self._q.put(first)

    def __iter__(self):
        return self

    def __next__(self) -> pb.FetchRequest:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item

    def replenish(self, n: int = _WINDOW) -> None:
        self._q.put(pb.FetchRequest(num_requested=n))

    def close(self) -> None:
        self._q.put(None)


class PubSubSubscriber:
    def __init__(self, sb, topics: tuple[str, ...] = DEFAULT_TOPICS) -> None:
        self.sb = sb
        self.topics = tuple(topics)
        self._schema_cache: dict[str, dict] = {}
        self._stop = threading.Event()
        self._processed = 0
        self._lock = threading.Lock()
        self._max_events: int | None = None
        # the integration user's Id — so we ignore the bot's own Case writes
        try:
            self._bot_user_id = salesforce._current_user_id(salesforce.client_for(None))
        except Exception:  # noqa: BLE001
            self._bot_user_id = None

    # ── auth / channel ────────────────────────────────────────────────
    def _metadata(self, *, refresh: bool = False) -> list[tuple[str, str]]:
        token, instance_url, org_id = salesforce.pubsub_auth(refresh=refresh)
        return [("accesstoken", token), ("instanceurl", instance_url), ("tenantid", org_id)]

    def _stub(self) -> pb_grpc.PubSubStub:
        chan = grpc.secure_channel(PUBSUB_ENDPOINT, grpc.ssl_channel_credentials())
        return pb_grpc.PubSubStub(chan)

    # ── schema / decode ──────────────────────────────────────────────
    def _schema(self, stub: pb_grpc.PubSubStub, schema_id: str, meta) -> dict:
        if schema_id not in self._schema_cache:
            info = stub.GetSchema(pb.SchemaRequest(schema_id=schema_id), metadata=meta)
            self._schema_cache[schema_id] = fastavro.parse_schema(json.loads(info.schema_json))
        return self._schema_cache[schema_id]

    def _decode(self, stub, producer_event, meta) -> dict:
        schema = self._schema(stub, producer_event.schema_id, meta)
        return fastavro.schemaless_reader(io.BytesIO(producer_event.payload), schema)

    # ── replay cursor (stored as hex text) ───────────────────────────
    def _load_replay(self, topic: str) -> bytes | None:
        try:
            rows = self.sb.table("sf_cdc_state").select("replay_id").eq("topic", topic).execute().data
        except Exception as e:  # noqa: BLE001
            log.warning("sf_cdc_state read failed for %s (%s); starting from LATEST", topic, e)
            return None
        if not rows or not rows[0].get("replay_id"):
            return None
        try:
            return bytes.fromhex(rows[0]["replay_id"])
        except ValueError:
            log.warning("sf_cdc_state has a non-hex replay_id for %s; starting from LATEST", topic)
            return None

    def _save_replay(self, topic: str, replay_id: bytes) -> None:
        if not replay_id:
            return
        try:
            self.sb.table("sf_cdc_state").upsert(
                {"topic": topic, "replay_id": replay_id.hex(), "updated_at": "now()"},
                on_conflict="topic",
            ).execute()
        except Exception as e:  # noqa: BLE001 -- a lost cursor write just replays a little
            log.warning("sf_cdc_state write failed for %s: %s", topic, e)

    # ── one topic stream ─────────────────────────────────────────────
    def _run_topic(self, topic: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                meta = self._metadata()
                stub = self._stub()
                replay = self._load_replay(topic)
                first = pb.FetchRequest(
                    topic_name=topic,
                    replay_preset=pb.ReplayPreset.CUSTOM if replay else pb.ReplayPreset.LATEST,
                    replay_id=replay or b"",
                    num_requested=_WINDOW,
                )
                reqs = _RequestStream(first)
                log.info("subscribed %s (%s)", topic, "resume" if replay else "latest")
                backoff = 1.0

                for resp in stub.Subscribe(reqs, metadata=meta):
                    if self._stop.is_set():
                        reqs.close()
                        break

                    for ce in resp.events:
                        payload = self._decode(stub, ce.event, meta)
                        specs = plan_events(payload, ce.replay_id.hex(),
                                            bot_user_id=self._bot_user_id)
                        for s in specs:
                            try:
                                jid = enqueue_case_run(
                                    self.sb, s.case_id, dedupe_key=s.dedupe_key,
                                    idempotency_key=s.idempotency_key, trigger=s.trigger,
                                )
                                log.info("%s %s -> job %s (%s)", topic, s.case_id,
                                         jid or "deduped", s.trigger)
                            except EntryFlowError as e:
                                log.error("no Salesforce-entry flow set — dropping %s: %s", s.case_id, e)
                        self._save_replay(topic, ce.replay_id)
                        if not specs:
                            log.debug("%s ignored a %s/%s change", topic,
                                      (payload.get("ChangeEventHeader") or {}).get("entityName"),
                                      (payload.get("ChangeEventHeader") or {}).get("changeType"))

                        with self._lock:
                            self._processed += 1
                            if self._max_events and self._processed >= self._max_events:
                                self._stop.set()
                        if self._stop.is_set():
                            reqs.close()
                            break

                    if self._stop.is_set():
                        reqs.close()
                        break

                    if not resp.events and resp.latest_replay_id:
                        self._save_replay(topic, resp.latest_replay_id)   # keepalive cursor

                    try:
                        from interpreter.health import beat
                        beat("cdc", {"topic": topic.rsplit("/", 1)[-1]}, sb=self.sb)
                    except Exception:  # noqa: BLE001
                        pass

                    reqs.replenish()

            except grpc.RpcError as e:  # noqa: PERF203
                code = e.code() if hasattr(e, "code") else None
                if code is grpc.StatusCode.UNAUTHENTICATED:
                    log.warning("%s: token rejected, refreshing", topic)
                    try:
                        salesforce.pubsub_auth(refresh=True)
                    except Exception:  # noqa: BLE001
                        pass
                elif code is grpc.StatusCode.CANCELLED and self._stop.is_set():
                    break
                else:
                    log.warning("%s stream error %s: %s", topic, code, e.details() if hasattr(e, "details") else e)
                if self._stop.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            except Exception as e:  # noqa: BLE001
                log.exception("%s subscriber crashed: %s", topic, e)
                if self._stop.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    # ── public ───────────────────────────────────────────────────────
    def run(self, *, max_events: int | None = None) -> int:
        """Block, streaming every topic in a thread. Returns the number of
        events processed (only meaningful with `max_events`)."""
        self._max_events = max_events
        self._processed = 0
        self._stop.clear()
        threads = [threading.Thread(target=self._run_topic, args=(t,), name=f"pubsub:{t}", daemon=True)
                   for t in self.topics]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads) and not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        self._stop.set()
        for t in threads:
            t.join(timeout=5)
        return self._processed
