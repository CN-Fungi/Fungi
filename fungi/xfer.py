"""Send-file progress jobs: the records the WebUI's file modal polls.

The browser mints a job id and hands it to POST /comm-send; the send itself
runs in this process (the file is on this disk, or the phone just uploaded it
here), so the bytes moving to the hub are observable from nowhere else. GET
/transfer-progress reads a record back — room.py serves it, server.py routes it.

A job has two legs, and the page tells them apart by `phase` (§49):

    state   phase     what is true
    running upload    bytes are moving to the hub
    sent    deliver   the hub has every byte; the receiver's own step is left
    done    deliver   the receiver landed the file (the only "已收到")
    error   upload    the send never made it out of this machine
    error   deliver   the receiver's verdict: it did not land (see `error`)

`sent` is why the modal no longer closes when the upload ends: uploading 1 GB to
a local hub takes seconds, while the peer's download can run for minutes after —
and closing the app in between used to leave a half file on their disk.

One registry for the whole room: transfers are rare, and a job is a handful of
ints.
"""

import threading
import time

JOB_TTL_S = 300.0  # settled jobs stay readable this long, for a last poll
AWAIT_TTL_S = 3600.0  # `sent` outlives the receiver's own consent card (30 min)


class TransferJobs:
    """Thread-safe registry of file-send jobs (id -> the record the page polls)."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, job_id: str, name: str, total: int) -> None:
        with self._lock:
            self._sweep()
            self._jobs[str(job_id)] = {
                "id": str(job_id),
                "name": str(name),
                "total": max(0, int(total)),
                "done": 0,
                "state": "running",
                "phase": "upload",
                "error": "",
                "saved": "",
                "ts": time.time(),
            }

    def progress(self, job_id: str, done: int) -> None:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None and job["state"] == "running":
                job["done"] = max(0, int(done))

    def sent(self, job_id: str) -> None:
        """Every byte is in the hub's staging area; the delivery is not ours.

        The name is the honest one: the file has been *sent*, and whether it
        lands is the receiving host's answer — `deliver` carries that.
        """
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None and job["state"] == "running":
                job["state"] = "sent"
                job["phase"] = "deliver"
                job["done"] = job["total"]
                job["ts"] = time.time()

    def deliver(self, job_id: str, ok: bool, error: str = "", saved: str = "") -> None:
        """The receiving host's verdict on a job this room sent."""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return
            job["state"] = "done" if ok else "error"
            job["phase"] = "deliver"
            job["saved"] = str(saved or "")
            if not ok:
                job["error"] = str(error or "the receiver did not take the file")
            job["ts"] = time.time()

    def fail(self, job_id: str, error: str) -> None:
        """The upload itself failed: nothing left this machine."""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is not None:
                job["state"] = "error"
                job["phase"] = "upload"
                job["error"] = str(error)
                job["ts"] = time.time()

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(str(job_id))
        return dict(job) if job is not None else {"error": "unknown transfer job"}

    def _sweep(self) -> None:
        """Drop settled jobs nobody can be polling any more (lock held).

        Called from start(), the only moment the registry grows. A job in flight
        is never swept, however long a slow transfer takes; `sent` gets its own
        longer window because the receiver may be sitting on a consent card.
        """
        now = time.time()
        dead = []
        for key, job in self._jobs.items():
            state = job["state"]
            if state == "running":
                continue
            ttl = AWAIT_TTL_S if state == "sent" else JOB_TTL_S
            if now - float(job.get("ts") or 0) > ttl:
                dead.append(key)
        for key in dead:
            del self._jobs[key]
