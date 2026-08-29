from fastapi.testclient import TestClient
import hcs_ai.server as server


client = TestClient(server.app)


def test_offline_chat_keeps_local_path(monkeypatch):
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "offline"})
    monkeypatch.setattr(server, "llm_chat", lambda message, history, use_kb: {"text": "local"})
    response = client.post("/chat", json={"message": "hi", "task_id": "t1"})
    assert response.status_code == 200
    assert response.json()["text"] == "local"


def test_live_chat_uses_cloud_router(monkeypatch):
    monkeypatch.setattr(server, "audit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_ai_mode_status", lambda: {"effective_mode": "live"})
    monkeypatch.setattr(
        server.cloud_router,
        "chat",
        lambda task_id, messages, tools=None: {
            "text": "cloud",
            "provider": "p1",
            "model": "m1",
            "tier": "high",
            "approval_required": False,
        },
    )
    response = client.post("/chat", json={"message": "hi", "history": [], "task_id": "t2"})
    body = response.json()
    assert body["text"] == "cloud"
    assert body["provider"] == "p1"
    assert body["tier"] == "high"


def test_tier_approval_endpoint(monkeypatch):
    monkeypatch.setattr(
        server.cloud_router,
        "approve_tier_change",
        lambda task_id, tier: {"ok": True, "task_id": task_id, "tier": tier},
    )
    response = client.post("/ai/approve-tier", json={"task_id": "t3", "tier": "medium"})
    assert response.status_code == 200
    assert response.json()["tier"] == "medium"
