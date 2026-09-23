"""Where a received file lands, and how it gets there (§49, §50).

The receiver's half of a transfer owns three things the wire cannot promise it:

- **Atomic landing.** Bytes stream into `<name>.part`; only a complete delivery
  is renamed onto the real name. Until §49 an interrupted delivery (app closed,
  peer gone, disk full) left a half file under the real name — right size look,
  right name — and the only thing noticed was WinRAR, hours later. Both the
  length the sender announced and the coverage of every byte are checked before
  the rename, and every failure path deletes the part file. Nothing else on the
  wire carries a checksum, so a delivery that ends early is caught here or not
  at all.
- **Several windows at once.** A delivery may be fetched as N byte ranges in
  parallel, each writing its own stretch of that one part file (§50). The rule
  is unchanged: the real name appears only when every window is accounted for,
  and an interrupted delivery still leaves exactly one part file behind.
- **Unfinished deliveries are continued, not restarted (§62).** The part file
  grows a note beside it (`<part>.json`: which staged transfer it is, how big it
  is, which byte ranges are really on disk), and a delivery that dies now leaves
  both behind instead of deleting them — a retry of the same staged transfer
  fetches only what is missing. The note is written after bytes have left the
  writer's handle and every span is clamped to the part's real length on the way
  back in, so it can never claim a byte that is not there. The part the rule
  above is about is untouched: the *real name* still only ever appears for a
  whole file (user, 2026-09-23: "flower我只想加一个断点续传——对于fungi的文件传输").
- **A writable inbox.** The default landing dir is the program's own folder,
  which a Program Files install cannot write: every transfer died on WinError 5
  naming `Program Files (x86)\\Fungi\\Fungi\\inbox`. It now falls back to
  %LOCALAPPDATA%\\Fungi\\inbox exactly the way the log directory does (§45).
"""

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import runlog
from .config import PROJECT_ROOT, writable_dir

PART_SUFFIX = ".part"
NOTE_SUFFIX = ".part.json"  # the note beside a part: what a retry needs to continue it (§62)
NOTE_VERSION = 1
PERSIST_INTERVAL_S = 2.0  # how often the note is rewritten while bytes keep arriving
PART_TTL_S = 7 * 24 * 3600.0  # an unfinished delivery nobody came back for
HEAD_BYTES = 64 * 1024  # how much of a file proves it is the same file (§62)


def head_digest(path: Path) -> str:
    """sha256 of a file's first `HEAD_BYTES` — what makes "the same file" checkable.

    There is no checksum on the wire (§48/§49.4), so a receiver only ever knows a
    delivery's length. The *sender's* leg can do better for nothing: it holds both
    copies of these bytes — its own file and the hub's staging — and 64 KiB is
    enough that a different file of the same name and length is not a story worth
    telling. The whole file is digested when it is shorter than that.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        digest.update(fh.read(HEAD_BYTES))
    return digest.hexdigest()


class TransferTruncatedError(OSError):
    """A delivery that is not all there: fewer bytes than the sender announced,
    or a stretch of the file that no window ever wrote."""

    def __init__(self, written: int, expected: int):
        super().__init__(f"delivery ended early: {written} of {expected} bytes")
        self.written = written
        self.expected = expected


class Spans:
    """Which bytes of a delivery are really here, as a union of ranges.

    A running total cannot answer that once a delivery is split into windows:
    they arrive out of order, a window that dies is retried from where it
    stopped (so its range is reported again), and a whole-file delivery and a
    ranged one can both touch the same transfer. Union length only ever grows,
    which is what a progress count and a "is this file complete?" check need
    (§50). Reports come from one thread per window, so the lock is the point.
    """

    def __init__(self) -> None:
        self._parts: list[tuple[int, int]] = []  # sorted, disjoint, [start, end)
        self._lock = threading.Lock()

    def add(self, start: int, end: int) -> None:
        if end <= start:
            return
        lo, hi = int(start), int(end)
        with self._lock:
            kept: list[tuple[int, int]] = []
            for first, last in self._parts:
                if last < lo or first > hi:  # a gap: touching spans merge
                    kept.append((first, last))
                    continue
                lo, hi = min(lo, first), max(hi, last)
            kept.append((lo, hi))
            kept.sort()
            self._parts = kept

    @property
    def bytes(self) -> int:
        with self._lock:
            return sum(last - first for first, last in self._parts)

    def covers(self, start: int, end: int) -> bool:
        """True when every byte of `[start, end)` has been reported."""
        at = int(start)
        with self._lock:
            for first, last in self._parts:  # sorted
                if last <= at:
                    continue
                if first > at:
                    return False
                at = last
                if at >= end:
                    return True
        return at >= end

    def end_of_run(self, start: int) -> int:
        """How far the covered stretch beginning at `start` reaches (>= start).

        This is where a delivery resumes from: not where the request started,
        but where the bytes actually are (§50).
        """
        at = int(start)
        with self._lock:
            for first, last in self._parts:  # sorted
                if last <= at:
                    continue
                if first > at:
                    break
                at = last
        return at

    def gaps(self, start: int, end: int) -> list[tuple[int, int]]:
        """The stretches of `[start, end)` that nothing has reported yet.

        What a sender needs to know to finish a job it cannot see the far side
        of: the receiver of an upload (the host, fungi/server.py) answers with
        these, so the page re-sends exactly what is missing instead of guessing
        from what the socket said it had handed over (§51).
        """
        out: list[tuple[int, int]] = []
        at = int(start)
        end = int(end)
        with self._lock:
            for first, last in self._parts:  # sorted
                if last <= at:
                    continue
                if first >= end:
                    break
                if first > at:
                    out.append((at, min(first, end)))
                at = max(at, last)
                if at >= end:
                    break
        if at < end:
            out.append((at, end))
        return out


    def ranges(self) -> list[tuple[int, int]]:
        """A snapshot of the union, to write down where a later delivery reads it (§62)."""
        with self._lock:
            return list(self._parts)

    def adopt(self, ranges: list[tuple[int, int]]) -> None:
        """Take in the ranges an earlier attempt left on disk (already clamped to it)."""
        for first, last in ranges:
            self.add(first, last)


def _part_path(dest: Path, tag: str) -> Path:
    """The part file a delivery writes before it earns the real name.

    `tag` (the staged transfer's id) goes into the name. Two deliveries of the
    same file — the user sent it twice, or a retry overlapped the first — then
    write their own part files instead of interleaving into one, and a crashed
    part never shadows the next attempt.
    """
    return dest.with_name(f"{dest.name}.{tag}{PART_SUFFIX}" if tag else dest.name + PART_SUFFIX)


def _note_path(part: Path) -> Path:
    """Where the note about a part lives: beside it, and named after it.

    It has to be findable by a delivery that has only the destination and the
    staged transfer's id to go on, which is exactly what the part file's own name
    already carries — so the note is the part's name plus `.json`, and the two are
    never looked for apart.
    """
    return part.with_name(part.name + ".json")


@dataclass
class PartRecord:
    """What an unfinished delivery left on disk, in the shape the next one acts on.

    Only ranges whose writer had closed its handle are in here, and every one of
    them is clamped to the part's real length on the way back in — a note may
    under-report (a later delivery re-fetches a stretch it already had, which
    costs a little) but must never over-report, because a note that claims a byte
    that is not on disk is how a truncated file gets to wear the real name.

    `transfer` is the identity the resume is decided on: the hub's staged
    transfer id. It is the only thing here that proves the bytes this note
    describes are the bytes the new delivery is about — the same trick as a
    downloader comparing the URL it was asked for, and the reason a re-send that
    mints a new id starts from zero instead of guessing.
    """

    transfer: str
    size: int | None
    part: str
    spans: list[tuple[int, int]]

    def matches(self, transfer: str, expect: int | None) -> bool:
        """Same staged transfer, and — when both sides know a length — the same length."""
        if not self.part or self.transfer != str(transfer):
            return False
        if expect is None or self.size is None:
            return True
        return int(self.size) == int(expect)

    @classmethod
    def read(cls, part: Path) -> "PartRecord | None":
        """The note for this part, or None when there is nothing trustworthy there."""
        try:
            raw = json.loads(_note_path(part).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("version") != NOTE_VERSION:
            return None
        spans: list[tuple[int, int]] = []
        for item in raw.get("spans") or []:
            try:
                first, last = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                return None
            if last > first >= 0:
                spans.append((first, last))
        size = raw.get("size")
        return cls(
            str(raw.get("transfer") or ""),
            int(size) if size is not None else None,
            str(raw.get("part") or ""),
            spans,
        )

    def write(self, part: Path) -> None:
        """Put the note down, in one step: a half-written note is a part nothing can use.

        Written to a scratch name and renamed over the old one, so a note a reader
        finds is a whole note — the alternative (a torn write, a JSON error, and a
        perfectly good part file discarded because of it) throws away exactly the
        bytes this feature exists to keep.
        """
        note = _note_path(part)
        note.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "version": NOTE_VERSION,
                "transfer": self.transfer,
                "size": self.size,
                "part": self.part,
                "spans": [list(span) for span in self.spans],
                "saved": round(time.time(), 3),
            }
        )
        scratch = note.with_name(note.name + ".tmp")
        scratch.write_text(body, encoding="utf-8")
        scratch.replace(note)

    @staticmethod
    def clear(part: Path) -> None:
        with contextlib.suppress(OSError):
            _note_path(part).unlink()


def sweep_parts(folder: Path, ttl: float = PART_TTL_S) -> None:
    """Drop parts nobody came back for, and the notes that name them.

    Keeping an unfinished delivery is the whole point of §62, but a delivery that
    is never retried (the peer reinstalled, the file was sent once and forgotten)
    would otherwise hold disk for ever. `ttl` is deliberately long: the delivery it
    belongs to is only useful while the hub still has the staged copy, and being
    wrong here costs a re-download, not data.
    """
    cutoff = time.time() - ttl
    try:
        notes = list(folder.glob(f"*{NOTE_SUFFIX}"))
    except OSError:  # an inbox that is not there yet
        return
    for note in notes:
        with contextlib.suppress(OSError):
            if note.stat().st_mtime >= cutoff:
                continue
            part = note.with_name(note.name[: -len(".json")])
            part.unlink(missing_ok=True)
            note.unlink(missing_ok=True)


class Landing:
    """One delivery in flight: a part file that one or more writers fill in place.

    The part starts empty and every writer seeks to its own offset, so a
    delivery fetched as N windows costs one write of the file — no second copy
    to concatenate windows into — and an interrupted delivery leaves exactly one
    `<name>.<tag>.part` behind, whatever the stream count (§50).

    `commit()` is the only place the real name appears, and it demands proof
    that the file is the sender's: the announced length *and* coverage of every
    byte. Anything else — a window that never finished, a dropped connection, a
    full disk, Ctrl-C — keeps the part and its note where they are, so the next
    attempt at this same staged transfer continues instead of starting over
    (§62). `transfer` is what makes that safe to do: only the same staged
    transfer, at the same length, may adopt what is on disk.
    """

    def __init__(
        self, dest: Path | str, expect: int | None = None, tag: str = "", transfer: str = ""
    ):
        self.dest = Path(dest)
        self.expect = expect
        self.transfer = str(transfer or "")
        self.part = _part_path(self.dest, tag)
        self.spans = Spans()
        self._saved = 0.0
        self._done = False

    def __enter__(self) -> "Landing":
        self.part.parent.mkdir(parents=True, exist_ok=True)
        sweep_parts(self.part.parent)  # a delivery nobody retried does not hold disk for ever
        if not self.adopt():
            self.restart()
        return self

    def adopt(self) -> bool:
        """Continue the part an earlier attempt left, when it is provably the same delivery.

        False means there is nothing to continue — no note, a note about some
        other staged transfer (or another length), or a part with no usable
        bytes in it — and then the part and its note go, because nothing will
        ever look for that name again. A note that *is* ours hands back every
        range it can prove: clamped to the part's real length, so bytes that
        only ever existed in a buffer are re-fetched rather than believed.
        """
        record = PartRecord.read(self.part)
        if record is None:
            return False
        size = self.part.stat().st_size if self.part.exists() else 0
        spans = [(first, min(last, size)) for first, last in record.spans if first < size]
        if record.part != self.part.name or not record.matches(self.transfer, self.expect):
            self.forget()
            return False
        if not any(last > first for first, last in spans):
            self.forget()
            return False
        self.spans.adopt(spans)
        runlog.note(
            "continuing %s from %d of %s bytes",
            self.part.name,
            self.spans.bytes,
            self.expect,
        )
        return True

    def restart(self) -> None:
        """Start this delivery from an empty part — the fresh-attempt shape (§49)."""
        with contextlib.suppress(OSError):  # an earlier crash must not block this one
            self.part.unlink()
        self.part.touch()
        self.spans = Spans()
        PartRecord.clear(self.part)

    def forget(self) -> None:
        """Drop the part and its note: nothing about this delivery is worth keeping."""
        with contextlib.suppress(OSError):
            self.part.unlink()
        PartRecord.clear(self.part)

    @contextlib.contextmanager
    def writer(self, offset: int) -> Iterator:
        """A handle positioned at `offset`: one of these per window."""
        with self.part.open("r+b") as fh:
            fh.seek(offset)
            yield fh

    def written(self, start: int, end: int) -> None:
        """Bytes `[start, end)` are on disk now (a retry reports again)."""
        self.spans.add(start, end)
        self.persist()

    def persist(self, force: bool = False) -> None:
        """Write the note down, so a delivery that dies here can be continued (§62).

        Rewritten at most every `PERSIST_INTERVAL_S` (a note per chunk would cost
        more than the bytes), and never at the cost of the delivery: a note that
        cannot be written is a delivery that restarts, not a failure. What it
        claims is what has left a writer's handle; `adopt` clamps it again anyway.
        """
        if not self.transfer or self._done or not self.spans.bytes:
            return
        now = time.monotonic()
        if not force and now - self._saved < PERSIST_INTERVAL_S:
            return
        self._saved = now
        with contextlib.suppress(OSError):
            PartRecord(
                transfer=self.transfer,
                size=self.expect,
                part=self.part.name,
                spans=self.spans.ranges(),
            ).write(self.part)

    def commit(self) -> None:
        """Put the part in place — the only way the real name appears (§49)."""
        size = self.part.stat().st_size
        if self.expect is not None:
            if size != self.expect:
                raise TransferTruncatedError(size, self.expect)
            # Length alone is not proof: a window that never ran leaves a hole,
            # and a later window writing past it makes the file the right size.
            if not self.spans.covers(0, self.expect):
                raise TransferTruncatedError(self.spans.bytes, self.expect)
        self.part.replace(self.dest)
        self._done = True
        PartRecord.clear(self.part)

    def __exit__(self, *_exc) -> bool:
        if not self._done:
            if self.transfer and self.spans.bytes:
                # Not landed: keep what is on disk and say how far it got, so the
                # next attempt at this transfer fetches the rest (§62).
                self.persist(force=True)
            else:
                # Either no staged transfer to name it after, or not one byte
                # arrived: nothing could ever continue this part, so keeping it
                # would only be litter (§49 as it was).
                self.forget()
        return False


def inbox_root(configured: str = "") -> Path:
    """The directory received files land in.

    An explicit `inbox_dir` is used as given — that is the user's own choice,
    even when it cannot be written (the error then names their path). Otherwise:
    `inbox/` beside the exe when this user can write there, else the per-user
    folder, so a read-only install can still receive (§49).
    """
    if configured:
        return Path(configured)
    beside = PROJECT_ROOT / "inbox"
    root = writable_dir(beside, "inbox")
    if root != beside:
        runlog.warn_once(
            "inbox-fallback",
            "the program's own folder is read-only; received files land in %s",
            root,
        )
    return root


@contextlib.contextmanager
def atomic_landing(
    dest: Path, expect: int | None = None, tag: str = "", transfer: str = ""
) -> Iterator:
    """Stream into a part file beside `dest`, then put it in place — or leave nothing.

    The one-writer case of `Landing`: a single stream writes from the start to
    the end, and `expect` is the byte count the sender announced (None: unknown,
    e.g. a hub that answered without Content-Length). A short delivery raises
    TransferTruncatedError instead of landing, so the real name never names a
    file that is not exactly what the sender had; the part and its note stay
    behind for the next attempt (§62).
    """
    with Landing(Path(dest), expect, tag, transfer) as land:
        with land.writer(0) as fh:
            yield fh
        # After the handle is closed: the part is renamed in `commit`, and
        # Windows refuses to rename a file that is still open (nor does a
        # buffered handle tell the truth about how much has reached the disk).
        land.written(0, land.part.stat().st_size)
        land.commit()
