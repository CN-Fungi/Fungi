"""Store: server-enforced path guard + fs ops + consent gate."""

import pytest

from fungi.hub.asks import Asks
from fungi.hub.store import GuardError, Store


@pytest.fixture()
def store(tmp_path):
    return Store(tmp_path, Asks())


def test_layout_created(store):
    assert (store.root / "public").is_dir()
    assert (store.root / "homes").is_dir()
    assert (store.root / "sessions").is_dir()


def test_public_free_for_any_host(store):
    p = store.resolve("alpha", "public/x.txt")
    store.write(p, "hello")
    assert store.read(store.resolve("beta", "public/x.txt")) == "hello"


def test_public_docs_write_needs_consent_read_free(store):
    # Clones self-authoring knowledge bases unannounced was judged an
    # overreach: writes under public/docs/ ride a consent card, reads stay free.
    with pytest.raises(GuardError):
        store.resolve("alpha", "public/docs/manual.md", mutating=True)
    ask = store.asks.open("alpha", {"action": "write", "path": "public/docs/manual.md"})
    store.asks.resolve(ask["ask_id"], value="yes")
    p = store.resolve("alpha", "public/docs/manual.md", consent_id=ask["ask_id"], mutating=True)
    store.write(p, "v1")
    assert store.read(store.resolve("beta", "public/docs/manual.md")) == "v1"
    # plain public/ files stay free for the transfer protocol
    store.write(store.resolve("alpha", "public/blob.bin"), "raw")


def test_own_home_read_free_write_needs_consent(store):
    p = store.resolve("alpha", "homes/alpha/notes/a.md")
    store.write(p, "# notes")  # direct store.write bypasses the guard
    # own-home read: free
    assert "notes" in store.read(store.resolve("alpha", "homes/alpha/notes/a.md"))
    # own-home write via the guarded fs path: needs the own user's consent
    with pytest.raises(GuardError):
        store.resolve("alpha", "homes/alpha/notes/b.md", mutating=True)
    ask = store.asks.open("alpha", {"action": "write", "path": "homes/alpha/notes/b.md"})
    store.asks.resolve(ask["ask_id"], value="yes")
    p2 = store.resolve("alpha", "homes/alpha/notes/b.md", consent_id=ask["ask_id"], mutating=True)
    store.write(p2, "own write")
    assert "own write" in store.read(store.resolve("alpha", "homes/alpha/notes/b.md"))


def test_other_home_requires_consent(store):
    with pytest.raises(GuardError):
        store.resolve("alpha", "homes/beta/secret.txt")
    # own-home WRITES need the owner's consent too (own-home reads stay free):
    # the duplicated assertion here used to leave that branch untested.
    with pytest.raises(GuardError):
        store.resolve("alpha", "homes/alpha/notes/c.md", mutating=True)


def test_other_home_with_answered_consent(store):
    ask = store.asks.open("beta", {"action": "write", "path": "homes/beta/doc.md"})
    store.asks.resolve(ask["ask_id"], value="yes")
    p = store.resolve("alpha", "homes/beta/doc.md", consent_id=ask["ask_id"])
    store.write(p, "shared doc")
    read_back = store.read(store.resolve("alpha", "homes/beta/doc.md", consent_id=ask["ask_id"]))
    assert read_back == "shared doc"


def test_other_home_with_denied_consent(store):
    ask = store.asks.open("beta", {})
    store.asks.resolve(ask["ask_id"], value="no")
    with pytest.raises(GuardError):
        store.resolve("alpha", "homes/beta/doc.md", consent_id=ask["ask_id"])


def test_forged_consent_id_rejected(store):
    with pytest.raises(GuardError):
        store.resolve("alpha", "homes/beta/doc.md", consent_id="fake-id")


@pytest.mark.parametrize(
    "rel",
    [
        "sessions/s1.json",
        "sessions",
        "config.json",
        "../etc/passwd",
        "public/../../escape",
        "/etc/passwd",
        "homes",  # no owner component
        "",
        "public\\win.txt",
    ],
)
def test_guard_rejects(store, rel):
    with pytest.raises(GuardError):
        store.resolve("alpha", rel)


def test_edit_unique_match(store):
    p = store.resolve("alpha", "public/f.txt")
    store.write(p, "one two two three")
    with pytest.raises(GuardError, match="not found"):
        store.edit(p, "missing", "x")
    with pytest.raises(GuardError, match="2 times"):
        store.edit(p, "two", "x")
    store.edit(p, "two three", "TWO THREE")
    assert store.read(p) == "one two TWO THREE"


def test_glob_grep_roots(store):
    pub = store.resolve("alpha", "public/a.py")
    store.write(pub, "alpha_marker = 1\n")
    own = store.resolve("alpha", "homes/alpha/b.py")
    store.write(own, "beta_marker = 2\n")

    root = store.check_search_root("alpha", "public")
    assert store.glob(root, "*.py") == ["public/a.py"]
    assert store.grep(root, "alpha_marker") == ["public/a.py:1:alpha_marker = 1"]

    own_root = store.check_search_root("alpha", "homes/alpha")
    assert store.glob(own_root, "*.py") == ["homes/alpha/b.py"]

    with pytest.raises(GuardError):
        store.check_search_root("alpha", "homes/beta")
