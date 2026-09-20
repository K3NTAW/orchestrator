from orchestrator.interference import classify, graph_signal, select_wave


def task(task_id, *scope, depends_on=()):
    return {"id": task_id, "scope": list(scope), "depends_on": list(depends_on)}


def test_same_file_is_hard():
    result = classify(task("a", "orchestrator/daemon.py"), task("b", "orchestrator/daemon.py"))
    assert result == {"level": "hard", "reasons": ["same_file:orchestrator/daemon.py"], "score": 1.0}


def test_glob_covering_literal_is_hard():
    result = classify(task("a", "orchestrator/*.py"), task("b", "orchestrator/daemon.py"))
    assert result["level"] == "hard"
    assert "glob_covers:orchestrator/*.py->orchestrator/daemon.py" in result["reasons"]


def test_dependency_transitive_is_hard():
    tasks = {item["id"]: item for item in [
        task("a", "a.py", depends_on=("b",)), task("b", "b.py", depends_on=("c",)), task("c", "c.py")
    ]}
    assert classify(tasks["a"], tasks["c"], tasks)["level"] == "hard"
    assert classify(tasks["c"], tasks["a"], tasks) == classify(tasks["a"], tasks["c"], tasks)


def test_directory_prefix_is_soft():
    result = classify(task("a", "tests/"), task("b", "tests/test_bus.py"))
    assert result["level"] == "soft"
    assert result["reasons"] == ["dir_contains:tests/->tests/test_bus.py"]


def test_test_adjacency_is_soft():
    result = classify(task("a", "tests/test_bus.py"), task("b", "orchestrator/bus.py"))
    assert result["level"] == "soft"
    assert "test_adjacent:bus" in result["reasons"]


def test_disjoint_scopes_are_none():
    assert classify(task("a", "orchestrator/bus.py"), task("b", "docs/readme.md"))["level"] == "none"


def test_select_wave_defers_hard_and_orders_soft_last():
    tasks = {item["id"]: item for item in [
        task("running", "orchestrator/bus.py"),
        task("hard", "orchestrator/bus.py"),
        task("soft", "orchestrator/daemon.py"),
        task("clean", "docs/readme.md"),
    ]}
    result = select_wave(["hard", "soft", "clean"], ["running"], tasks, 2)
    assert result["wave"] == ["clean", "soft"]
    assert result["deferred"] == [{"task": "hard", "reason": "hard:running"}]


def test_select_wave_respects_capacity_and_is_deterministic():
    tasks = {item["id"]: item for item in [task("a", "a/x.py"), task("b", "b/y.py"), task("c", "c/z.py")]}
    first = select_wave(["a", "b", "c"], [], tasks, 2, order=["c", "a", "b"])
    assert first == select_wave(["a", "b", "c"], [], tasks, 2, order=["c", "a", "b"])
    assert first == {"wave": ["c", "a"], "deferred": [{"task": "b", "reason": "capacity"}]}
    assert graph_signal(tasks["a"], tasks["b"], {}) == []
