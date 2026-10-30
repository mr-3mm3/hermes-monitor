import json
import unittest

from dashboard.workers import build_workers_response, hash_arguments, is_repeated_tool_loop


NOW = 1_700_000_000


def event(name, arguments, timestamp):
    return {"tool_name": name, "arguments_hash": hash_arguments(arguments), "timestamp": timestamp}


def test_detects_four_identical_consecutive_tool_calls():
    events = [event("read_file", {"path": "a"}, NOW - offset) for offset in (4, 3, 2, 1)]
    assert is_repeated_tool_loop(events)


def test_different_arguments_or_intervening_tool_is_not_loop():
    different_args = [event("read_file", {"path": str(i)}, NOW - i) for i in range(4)]
    interrupted = [
        event("read_file", {"path": "a"}, NOW - 4),
        event("read_file", {"path": "a"}, NOW - 3),
        event("write_file", {"path": "a"}, NOW - 2),
        event("read_file", {"path": "a"}, NOW - 1),
    ]
    assert not is_repeated_tool_loop(different_args)
    assert not is_repeated_tool_loop(interrupted)


def test_completed_historical_run_does_not_keep_worker_in_loop():
    events = [event("read_file", {"path": "a"}, NOW - offset) for offset in (5, 4, 3, 2)]
    events.append(event("terminal", {"command": "recovered"}, NOW - 1))
    assert not is_repeated_tool_loop(events)


def test_worker_shape_loop_priority_and_stall_threshold():
    tasks = [
        {"id": "t_loop", "title": "Loop", "assignee": "a", "started_at": NOW - 60},
        {"id": "t_edge", "title": "Edge", "assignee": "b", "started_at": NOW - 400},
        {"id": "t_stall", "title": "Stall", "assignee": "c", "started_at": NOW - 600},
    ]
    repeated = [event("terminal", {"command": "safe"}, NOW - i) for i in range(4, 0, -1)]
    activities = {
        "t_loop": repeated,
        "t_edge": [event("terminal", {}, NOW - 300)],
        "t_stall": [event("terminal", {}, NOW - 301)],
    }

    result = build_workers_response(tasks, activities, now=NOW)

    assert result == {
        "generated_at": NOW,
        "count": 3,
        "workers": [
            {"card_id": "t_loop", "title": "Loop", "assignee": "a", "running_since": NOW - 60,
             "duration_s": 60, "card_age_s": 60, "last_activity_s": 1, "state": "loop", "kanban_url": None},
            {"card_id": "t_edge", "title": "Edge", "assignee": "b", "running_since": NOW - 400,
             "duration_s": 400, "card_age_s": 400, "last_activity_s": 300, "state": "active", "kanban_url": None},
            {"card_id": "t_stall", "title": "Stall", "assignee": "c", "running_since": NOW - 600,
             "duration_s": 600, "card_age_s": 600, "last_activity_s": 301, "state": "stalled", "kanban_url": None},
        ],
    }
    assert "safe" not in json.dumps(result)


def test_unknown_activity_is_active_with_null_age():
    result = build_workers_response(
        [{"id": "t_x", "title": "Unknown", "assignee": "a", "started_at": NOW}],
        {},
        now=NOW,
    )
    assert result["workers"][0]["last_activity_s"] is None
    assert result["workers"][0]["state"] == "active"


def test_elapsed_uses_current_run_start_not_card_start():
    task = {"id": "t_a", "title": "A", "assignee": "a", "started_at": NOW - 90_000, "run_started_at": NOW - 2_400}
    worker = build_workers_response([task], {}, now=NOW)["workers"][0]
    assert worker["duration_s"] == 2_400
    assert worker["running_since"] == NOW - 2_400
    assert worker["card_age_s"] == 90_000


def test_last_activity_from_heartbeat_when_no_tool_events():
    task = {"id": "t_a", "title": "A", "assignee": "a", "started_at": NOW - 900, "last_heartbeat_at": NOW - 45}
    worker = build_workers_response([task], {}, now=NOW)["workers"][0]
    assert worker["last_activity_s"] == 45
    assert worker["state"] == "active"


def test_last_activity_falls_back_to_task_event_or_comment():
    task = {"id": "t_a", "title": "A", "assignee": "a", "started_at": NOW - 900, "last_event_at": NOW - 120}
    worker = build_workers_response([task], {}, now=NOW)["workers"][0]
    assert worker["last_activity_s"] == 120


def test_last_activity_is_newest_of_all_signals():
    task = {"id": "t_a", "title": "A", "assignee": "a", "started_at": NOW - 900,
            "last_heartbeat_at": NOW - 400, "last_event_at": NOW - 200}
    activity = {"t_a": [event("terminal", {}, NOW - 500)]}
    worker = build_workers_response([task], activity, now=NOW)["workers"][0]
    assert worker["last_activity_s"] == 200


def test_load_running_tasks_uses_current_run_and_events(tmp_path=None):
    import os, sqlite3, tempfile
    from pathlib import Path
    from unittest.mock import patch
    from dashboard import plugin_api
    home = Path(tempfile.mkdtemp())
    db = sqlite3.connect(home / "kanban.db")
    db.executescript("""
      CREATE TABLE tasks(id, title, assignee, status, started_at, current_run_id);
      CREATE TABLE task_runs(id INTEGER, task_id, started_at, last_heartbeat_at);
      CREATE TABLE task_events(task_id, created_at);
      CREATE TABLE task_comments(task_id, created_at);
      INSERT INTO tasks VALUES('t1','T','default','running',100,2);
      INSERT INTO task_runs VALUES(1,'t1',100,150),(2,'t1',9000,9100);
      INSERT INTO task_events VALUES('t1',9050),('t1',9200);
      INSERT INTO task_comments VALUES('t1',9300);
    """)
    db.commit(); db.close()
    with patch.object(plugin_api, "_hermes_home", lambda: home):
        task = plugin_api._load_running_tasks()[0]
    assert task["started_at"] == 100 and task["run_started_at"] == 9000
    assert task["last_heartbeat_at"] == 9100 and task["last_event_at"] == 9300


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in globals().items():
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=name))
    return suite
