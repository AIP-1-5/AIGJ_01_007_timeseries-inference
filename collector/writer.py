"""수신과 저장 분리: 콜백 → 큐 → 쓰기 스레드 1개 → SQLite. research R4.

DB 쓰기는 이 스레드 하나만 한다. 연결 이벤트도 큐를 거쳐 여기서 기록한다.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import asdict, dataclass

from .config import Config
from .log import log
from .parser import RejectedMessage, classify_topic, now_iso, parse_join, parse_uplink
from .store import Store

_STOP = object()


@dataclass
class Stats:
    processed: int = 0
    saved: int = 0
    duplicate: int = 0
    rejected: int = 0
    ignored: int = 0
    joins: int = 0
    unmapped: int = 0        # 매핑 안 된 (모델, 키) 종류 수
    skipped_values: int = 0  # 숫자가 아니라 건너뛴 값 수
    errors: int = 0
    queue_drop: int = 0


class Pipeline:
    """메시지 하나를 파싱 → epoch → 저장. Writer와 replay가 같이 쓴다."""

    def __init__(self, store: Store, cfg: Config, stats: Stats | None = None):
        self.store = store
        self.cfg = cfg
        self.stats = stats or Stats()
        self._seen_unmapped: set[tuple[str, str]] = set()
        self._seen_skipped: set[tuple[str, str]] = set()

    def handle(self, topic: str, payload: bytes, received_at: str) -> str:
        st = self.stats
        st.processed += 1
        kind = classify_topic(topic)
        if kind is None or (kind == "join" and not self.cfg.mqtt.join_topic):
            st.ignored += 1
            return "ignored"

        if kind == "join":
            device_id = parse_join(topic, payload)
            if device_id is None:
                return self._reject(topic, payload, received_at, "missing_field:devEui")
            epoch = self.store.save_join(device_id, received_at)
            st.joins += 1
            log("INFO", "join", device_id=device_id, epoch=epoch)
            return "join"

        msg = parse_uplink(topic, payload, received_at, self.cfg)
        if isinstance(msg, RejectedMessage):
            return self._reject(topic, payload, received_at, msg.reason)

        for key in msg.unmapped_keys:
            if (msg.model, key) not in self._seen_unmapped:
                self._seen_unmapped.add((msg.model, key))
                st.unmapped += 1
                detail = {"model": msg.model, "key": key, "policy": self.cfg.unmapped_policy}
                self.store.log_event("unmapped_key", msg.device_id, detail, received_at)
                log("WARN", "unmapped_key", device_id=msg.device_id, **detail)
        for key in msg.skipped_keys:
            st.skipped_values += 1
            if (msg.model, key) not in self._seen_skipped:
                self._seen_skipped.add((msg.model, key))
                log("WARN", "non_numeric_value", device_id=msg.device_id, model=msg.model, key=key)

        result = self.store.save_uplink(msg, self.cfg.fcnt)
        if result.bumped:
            log("INFO", "epoch_bump", device_id=msg.device_id, epoch=result.epoch, f_cnt=msg.f_cnt)
        if result.duplicate:
            st.duplicate += 1
            return "duplicate"
        st.saved += 1
        return "saved"

    def _reject(self, topic: str, payload: bytes, received_at: str, reason: str) -> str:
        self.stats.rejected += 1
        self.store.save_rejected(received_at, topic, reason, payload)
        self.store.log_event("parse_error", None, {"reason": reason, "topic": topic}, received_at)
        log("WARN", "parse_error", reason=reason, topic=topic)
        return "rejected"


class Writer:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg
        self.stats = Stats()
        self.pipeline = Pipeline(store, cfg, self.stats)
        self.queue: queue.Queue = queue.Queue(maxsize=cfg.storage.queue_max)
        self._drop_lock = threading.Lock()
        self._drops_logged = 0
        self._thread = threading.Thread(target=self._run, name="collector-writer", daemon=True)

    # --- 수신 쪽(MQTT 스레드)에서 부른다. 절대 막히지 않는다 ---------------

    def _put(self, item) -> bool:
        try:
            self.queue.put_nowait(item)
            return True
        except queue.Full:
            with self._drop_lock:
                self.stats.queue_drop += 1
            return False

    def submit_message(self, topic: str, payload: bytes, received_at: str) -> bool:
        return self._put(("m", topic, payload, received_at))

    def submit_event(self, kind: str, device_id: str | None = None, detail=None, ts: str | None = None) -> bool:
        return self._put(("e", kind, device_id, detail, ts or now_iso()))

    # --- 쓰기 스레드 -------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float | None = 30) -> None:
        """큐에 남은 것을 모두 쓰고 `stop` 이벤트를 남긴 뒤 끝낸다."""
        if not self._thread.is_alive():
            return
        self.queue.put(_STOP)
        self._thread.join(timeout)

    def snapshot(self) -> dict:
        d = asdict(self.stats)
        d["queue_size"] = self.queue.qsize()
        return d

    def _handle(self, item) -> None:
        try:
            if item[0] == "m":
                self.pipeline.handle(item[1], item[2], item[3])
            else:
                _, kind, device_id, detail, ts = item
                self.store.log_event(kind, device_id, detail, ts)
        except Exception as e:  # 한 메시지의 실패가 다음 메시지를 막지 않는다
            self.stats.errors += 1
            topic = item[1] if item[0] == "m" else f"event:{item[1]}"
            log("ERROR", "write_error", topic=topic, error=repr(e)[:200])
            try:
                self.store.log_event("write_error", None, {"topic": topic, "error": repr(e)[:200]})
            except Exception:
                pass

    def _check_drops(self) -> None:
        with self._drop_lock:
            total = self.stats.queue_drop
        if total > self._drops_logged:
            new = total - self._drops_logged
            self._drops_logged = total
            log("WARN", "queue_drop", dropped=new, total=total)
            self._safe_event("queue_drop", {"dropped": new, "total": total})

    def _safe_event(self, kind: str, detail) -> None:
        try:
            self.store.log_event(kind, None, detail)
        except Exception as e:
            self.stats.errors += 1
            log("ERROR", "event_write_error", kind=kind, error=repr(e)[:200])

    def _run(self) -> None:
        interval = self.cfg.storage.heartbeat_s
        next_hb = time.monotonic() + interval
        while True:
            try:
                item = self.queue.get(timeout=max(0.0, next_hb - time.monotonic()))
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            if item is not None:
                self._handle(item)
            self._check_drops()
            if time.monotonic() >= next_hb:
                self._safe_event("heartbeat", self.snapshot())
                next_hb = time.monotonic() + interval
        # STOP 뒤에 들어온 것까지 비운다
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                self._handle(item)
        self._check_drops()
        self._safe_event("stop", self.snapshot())
        log("INFO", "writer_stopped", **{k: v for k, v in self.snapshot().items()})
