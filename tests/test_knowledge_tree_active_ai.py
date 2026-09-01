import json

from hcs_ai import knowledge_tree


def _analysis_json():
    return json.dumps({
        "summary": "Test summary",
        "primary_path": [{"name": "Science", "confidence": 0.95, "reason": "test", "aliases": []}],
        "additional_paths": [],
        "relationships": [],
        "overall_confidence": 0.95,
    })


def test_live_mode_uses_cloud_router_for_tree_classification(monkeypatch):
    monkeypatch.setattr(knowledge_tree, "_existing_tree_snapshot", lambda: [])
    monkeypatch.setattr(
        knowledge_tree,
        "load_config",
        lambda: {"ai": {"mode": "live"}},
        raising=False,
    )
    monkeypatch.setattr(
        knowledge_tree,
        "llm_config",
        lambda: (_ for _ in ()).throw(AssertionError("local llm must not be used in Live mode")),
    )
    calls = []

    def fake_cloud_chat(task_id, messages, tools=None):
        calls.append((task_id, messages))
        return {
            "text": _analysis_json(),
            "provider": "Groq",
            "model": "openai/gpt-oss-120b",
            "tier": "high",
            "approval_required": False,
        }

    monkeypatch.setattr(knowledge_tree.cloud_router, "chat", fake_cloud_chat, raising=False)

    result = knowledge_tree.analyze_for_tree(
        title="test.txt",
        artifact_type="txt",
        text="A document about science.",
    )

    assert calls
    assert calls[0][0] == "knowledge-tree-classifier"
    assert result["summary"] == "Test summary"
    assert result["model_name"] == "Groq/openai/gpt-oss-120b"


def test_live_tree_classification_stops_when_router_requires_tier_approval(monkeypatch):
    monkeypatch.setattr(knowledge_tree, "_existing_tree_snapshot", lambda: [])
    monkeypatch.setattr(
        knowledge_tree,
        "load_config",
        lambda: {"ai": {"mode": "live"}},
        raising=False,
    )

    def fake_cloud_chat(task_id, messages, tools=None):
        return {
            "approval_required": True,
            "current_tier": "high",
            "proposed_tier": "medium",
            "model": "replacement-model",
            "message": "Approval required",
        }

    monkeypatch.setattr(knowledge_tree.cloud_router, "chat", fake_cloud_chat, raising=False)

    try:
        knowledge_tree.analyze_for_tree(
            title="test.txt",
            artifact_type="txt",
            text="A document about science.",
        )
    except RuntimeError as exc:
        assert "approval" in str(exc).lower()
    else:
        raise AssertionError("Expected Knowledge Tree classification to stop for cross-tier approval")
