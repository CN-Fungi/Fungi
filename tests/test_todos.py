"""todos: shared calendar store + agent todo tool + courier prompt injection."""

import datetime as dt

from fungi import todos


def _write(path, data: dict) -> None:
    import json

    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_normalizes(tmp_path):
    p = tmp_path / "todos.json"
    _write(p, {"2026-09-10": ["去上课", "", "  "], "bad": "nope", "2026-09-11": []})
    assert todos.load(p) == {"2026-09-10": ["去上课"]}
    assert todos.load(tmp_path / "missing.json") == {}


def test_set_day_roundtrip(tmp_path):
    p = tmp_path / "todos.json"
    todos.set_day("2026-09-10", ["开会"], path=p)
    todos.set_day("2026-09-10", ["开会", "买菜"], path=p)  # replace, not append
    assert todos.load(p)["2026-09-10"] == ["开会", "买菜"]
    todos.set_day("2026-09-10", [], path=p)  # empty clears the day
    assert todos.load(p) == {}


def test_upcoming_orders_overdue_first(tmp_path, monkeypatch):
    p = tmp_path / "todos.json"
    today = dt.date(2026, 9, 10)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    _write(
        p,
        {
            "2026-09-09": ["yesterday"],
            "2026-09-10": ["today"],
            "2026-09-24": ["later"],
            "2026-08-20": ["recently overdue"],
        },
    )
    got = todos.upcoming(today=today)
    assert [d for d, _ in got] == ["2026-08-20", "2026-09-09", "2026-09-10", "2026-09-24"]


def test_todo_tool_add_list_remove(tmp_path, monkeypatch):
    p = tmp_path / "todos.json"
    monkeypatch.setattr(todos, "TODOS_PATH", p)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    assert "added" in todos.todo_tool({"action": "add", "date": "2026-09-12", "text": "去看牙"})
    assert "error" in todos.todo_tool({"action": "add", "date": "not-a-date", "text": "x"})
    assert "2026-09-12" in todos.todo_tool({"action": "list"})
    assert "removed" in todos.todo_tool(
        {"action": "remove", "date": "2026-09-12", "text": "去看牙"}
    )
    assert "no items" in todos.todo_tool(
        {"action": "remove", "date": "2026-09-12", "text": "去看牙"}
    )
    assert todos.load(p) == {}


def test_add_keeps_existing_items_and_never_duplicates(tmp_path, monkeypatch):
    """New detail lands beside the old entry. 2026-09-10 real-machine finding:
    the courier removed the host's 09-11 entry and re-added a reworded copy --
    a silent cancellation, where the wish was one more item that day."""
    p = tmp_path / "todos.json"
    monkeypatch.setattr(todos, "TODOS_PATH", p)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    todos.todo_tool({"action": "add", "date": "2026-09-11", "text": "傍晚18:00 出去玩（地点待定）"})
    todos.todo_tool({"action": "add", "date": "2026-09-11", "text": "去咖啡店当集合点"})
    assert todos.load(p)["2026-09-11"] == ["傍晚18:00 出去玩（地点待定）", "去咖啡店当集合点"]
    assert "already" in todos.todo_tool(
        {"action": "add", "date": "2026-09-11", "text": "去咖啡店当集合点"}
    )
    assert todos.load(p)["2026-09-11"] == ["傍晚18:00 出去玩（地点待定）", "去咖啡店当集合点"]


def test_update_fixes_an_entry_in_place(tmp_path, monkeypatch):
    """2026-09-11 user instruction: the courier may not delete the user's
    entries for no reason, but correcting a mistake of its OWN is allowed --
    `update` replaces one item in place, keeping the day's order, instead of a
    remove+add that reads as a cancellation."""
    p = tmp_path / "todos.json"
    monkeypatch.setattr(todos, "TODOS_PATH", p)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    todos.todo_tool({"action": "add", "date": "2026-09-12", "text": "16:00 校门口碰面"})
    todos.todo_tool({"action": "add", "date": "2026-09-12", "text": "17:30 和 Aria 吃饭"})
    out = todos.todo_tool(
        {
            "action": "update",
            "date": "2026-09-12",
            "old": "17:30 和 Aria 吃饭",
            "text": "17:15 和 Aria 吃饭",
        }
    )
    assert "updated" in out
    assert todos.load(p)["2026-09-12"] == ["16:00 校门口碰面", "17:15 和 Aria 吃饭"]
    # an entry that is not there is never invented, and nothing is deleted
    assert "no such item" in todos.todo_tool(
        {"action": "update", "date": "2026-09-12", "old": "买菜", "text": "买水果"}
    )
    assert todos.load(p)["2026-09-12"] == ["16:00 校门口碰面", "17:15 和 Aria 吃饭"]
    assert "error" in todos.todo_tool({"action": "update", "date": "2026-09-12", "old": "x"})
    # an update onto an existing line leaves one line, not two
    todos.todo_tool(
        {
            "action": "update",
            "date": "2026-09-12",
            "old": "17:15 和 Aria 吃饭",
            "text": "16:00 校门口碰面",
        }
    )
    assert todos.load(p)["2026-09-12"] == ["16:00 校门口碰面"]


def test_remove_needs_the_exact_item(tmp_path, monkeypatch):
    """A bare date must never wipe a day: the GUI calendar is the only
    whole-day editor."""
    p = tmp_path / "todos.json"
    monkeypatch.setattr(todos, "TODOS_PATH", p)
    orig = todos.load
    monkeypatch.setattr(todos, "load", lambda path=None: orig(p))
    todos.todo_tool({"action": "add", "date": "2026-09-11", "text": "出去玩"})
    todos.todo_tool({"action": "add", "date": "2026-09-11", "text": "去咖啡店"})
    assert "error" in todos.todo_tool({"action": "remove", "date": "2026-09-11"})
    assert todos.load(p)["2026-09-11"] == ["出去玩", "去咖啡店"]
    assert "no such item" in todos.todo_tool(
        {"action": "remove", "date": "2026-09-11", "text": "买菜"}
    )
    assert todos.load(p)["2026-09-11"] == ["出去玩", "去咖啡店"]
