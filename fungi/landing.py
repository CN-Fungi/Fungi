"""Where a received file lands, and how it gets there (§49).

The receiver's half of a transfer owns two things the wire cannot promise it:

- **Atomic landing.** Bytes stream into `<name>.part`; only a complete delivery
  is renamed onto the real name. Until §49 an interrupted delivery (app closed,
  peer gone, disk full) left a half file under the real name — right size look,
  right name — and the only thing that noticed was WinRAR, hours later. The
  length the sender announced is checked before the rename, and every failure
  path deletes the part file. Nothing else on the wire carries a checksum, so a
  delivery that ends early is caught here or not at all.
- **A writable inbox.** The default landing dir is the program's own folder,
  which a Program Files install cannot write: every transfer died on WinError 5
  naming `Program Files (x86)\\Fungi\\Fungi\\inbox`. It now falls back to
  %LOCALAPPDATA%\\Fungi\\inbox exactly the way the log directory does (§45).
"""

import contextlib
from collections.abc import Iterator
from pathlib import Path

from . import runlog
from .config import PROJECT_ROOT, writable_dir

PART_SUFFIX = ".part"


class TransferTruncatedError(OSError):
    """A delivery that ended early: fewer bytes than the sender announced."""

    def __init__(self, written: int, expected: int):
        super().__init__(f"delivery ended early: {written} of {expected} bytes")
        self.written = written
        self.expected = expected


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

    `expect` is the byte count the sender announced (None: unknown, e.g. a hub
    that answered without Content-Length). A short delivery raises
    TransferTruncatedError instead of landing, and any failure — that one, a
    dropped connection, a full disk — deletes the part file, so the real name
    never names a file that is not exactly what the sender had.

    `tag` (the staged transfer's id) goes into the part's name. Two deliveries
    of the same file — the user sent it twice, or a retry overlapped the first —
    then write their own part files instead of interleaving into one, and a
    crashed part never shadows the next attempt.
    """
    dest = Path(dest)
    part = dest.with_name(f"{dest.name}.{tag}{PART_SUFFIX}" if tag else dest.name + PART_SUFFIX)
    with contextlib.suppress(OSError):  # an earlier crash must not block this one
        part.unlink()
    try:
        with part.open("wb") as fh:
            yield fh
            written = fh.tell()
        if expect is not None and written != expect:
            raise TransferTruncatedError(written, expect)
        part.replace(dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
