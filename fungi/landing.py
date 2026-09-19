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
- **A writable inbox.** The default landing dir is the program's own folder,
  which a Program Files install cannot write: every transfer died on WinError 5
  naming `Program Files (x86)\\Fungi\\Fungi\\inbox`. It now falls back to
  %LOCALAPPDATA%\\Fungi\\inbox exactly the way the log directory does (§45).
"""

import contextlib
import threading
from collections.abc import Iterator
from pathlib import Path

from . import runlog
from .config import PROJECT_ROOT, writable_dir

PART_SUFFIX = ".part"


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


def _part_path(dest: Path, tag: str) -> Path:
    """The part file a delivery writes before it earns the real name.

    `tag` (the staged transfer's id) goes into the name. Two deliveries of the
    same file — the user sent it twice, or a retry overlapped the first — then
    write their own part files instead of interleaving into one, and a crashed
    part never shadows the next attempt.
    """
    return dest.with_name(f"{dest.name}.{tag}{PART_SUFFIX}" if tag else dest.name + PART_SUFFIX)


class Landing:
    """One delivery in flight: a part file that one or more writers fill in place.

    The part starts empty and every writer seeks to its own offset, so a
    delivery fetched as N windows costs one write of the file — no second copy
    to concatenate windows into — and an interrupted delivery leaves exactly one
    `<name>.<tag>.part` behind, whatever the stream count (§50).

    `commit()` is the only place the real name appears, and it demands proof
    that the file is the sender's: the announced length *and* coverage of every
    byte. Anything else — a window that never finished, a dropped connection, a
    full disk, Ctrl-C — deletes the part and leaves nothing (§49).
    """

    def __init__(self, dest: Path | str, expect: int | None = None, tag: str = ""):
        self.dest = Path(dest)
        self.expect = expect
        self.part = _part_path(self.dest, tag)
        self.spans = Spans()
        self._done = False

    def __enter__(self) -> "Landing":
        with contextlib.suppress(OSError):  # an earlier crash must not block this one
            self.part.unlink()
        self.part.touch()
        return self

    @contextlib.contextmanager
    def writer(self, offset: int) -> Iterator:
        """A handle positioned at `offset`: one of these per window."""
        with self.part.open("r+b") as fh:
            fh.seek(offset)
            yield fh

    def written(self, start: int, end: int) -> None:
        """Bytes `[start, end)` are on disk now (a retry reports again)."""
        self.spans.add(start, end)

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

    def __exit__(self, *_exc) -> bool:
        if not self._done:
            with contextlib.suppress(OSError):
                self.part.unlink()
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
def atomic_landing(dest: Path, expect: int | None = None, tag: str = "") -> Iterator:
    """Stream into a part file beside `dest`, then put it in place — or leave nothing.

    The one-writer case of `Landing`: a single stream writes from the start to
    the end, and `expect` is the byte count the sender announced (None: unknown,
    e.g. a hub that answered without Content-Length). A short delivery raises
    TransferTruncatedError instead of landing, and any failure — that one, a
    dropped connection, a full disk — deletes the part file, so the real name
    never names a file that is not exactly what the sender had.
    """
    with Landing(Path(dest), expect, tag) as land:
        with land.writer(0) as fh:
            yield fh
        # After the handle is closed: the part is renamed in `commit`, and
        # Windows refuses to rename a file that is still open (nor does a
        # buffered handle tell the truth about how much has reached the disk).
        land.written(0, land.part.stat().st_size)
        land.commit()
