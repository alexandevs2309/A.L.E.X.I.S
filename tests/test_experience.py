from alexis.experience.presenter import present


def test_idle_when_no_mission():
    p = present({"mission": None, "verification": None, "memory": []})
    assert p["context"] == "idle"
    assert p["decision"] is None
    assert p["result"] is None


def test_waiting_approval_presents_decision():
    snapshot = {
        "mission": {
            "id": "m1",
            "objective": "Revisar docs",
            "state": "waiting_approval",
            "results": [{"step": "understand", "success": True}],
            "pending_approval": {"step": "execute", "action": "execute", "risk": "medium"},
        },
        "verification": None,
        "memory": [],
    }
    p = present(snapshot)
    assert p["context"] == "waiting_approval"
    assert p["decision"]["action"] == "Ejecutar la acción"
    assert p["decision"]["risk"] == "riesgo medio"


def test_completed_shows_real_result():
    snapshot = {
        "mission": {
            "id": "m1",
            "objective": "Revisar docs",
            "state": "completed",
            "results": [
                {"step": "understand", "success": True},
                {"step": "research", "success": True},
            ],
        },
        "verification": {"confidence": 0.7, "evidence": ["evid"]},
        "memory": [],
    }
    p = present(snapshot)
    assert p["context"] == "completed"
    assert p["result"]["confidence"] == 70
    assert p["result"]["evidence"] == ["evid"]
    assert "Entender el objetivo" in p["result"]["steps"]


def test_failed_explains_and_needs_human():
    snapshot = {
        "mission": {"id": "m1", "objective": "x", "state": "failed", "results": []},
        "verification": None,
        "memory": [],
    }
    p = present(snapshot)
    assert p["context"] == "error"
    assert p["error"]["needs"]


def test_running_execute_maps_to_executing():
    snapshot = {
        "mission": {
            "id": "m1",
            "objective": "x",
            "state": "running",
            "results": [
                {"step": "understand", "success": True},
                {"step": "research", "success": True},
                {"step": "execute", "success": True},
            ],
            "pending_approval": None,
        },
        "verification": None,
        "memory": [],
    }
    p = present(snapshot)
    assert p["context"] == "executing"