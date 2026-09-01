import json

from hcs_ai.gui import markdown_segments, read_update_state
from hcs_ai.gui_recent import (
    active_model_identity,
    format_active_model_status,
)
from hcs_ai.gui_tree import format_kb_search_results


def test_markdown_segments_removes_bold_markers():
    segments = markdown_segments(
        "A node is **stale** after **3.5 x** its interval."
    )

    assert segments == [
        ("A node is ", None),
        ("stale", "bold"),
        (" after ", None),
        ("3.5 x", "bold"),
        (" its interval.", None),
    ]


def test_kb_search_results_are_human_readable():
    text = format_kb_search_results([
        {
            "score": 1.0,
            "source": r"E:\\Home\\Documents\\orchard.md",
            "chunk_index": 2,
            "text": "A node is stale after 3.5 reporting intervals.",
        }
    ])

    assert "Result 1" in text
    assert "Relevance: 1.00" in text
    assert "Source: E:\\Home\\Documents\\orchard.md" in text
    assert "Chunk: 2" in text
    assert "A node is stale after 3.5 reporting intervals." in text
    assert '"source":' not in text


def test_kb_search_results_explain_empty_result():
    assert format_kb_search_results([]) == "No matching knowledge was found."


def test_cloud_model_status_overrides_local_telemetry():
    cloud = {
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "tier": "high",
    }
    local = {
        "model": "models/Qwen3.gguf",
        "generation_tokens_per_second": 8.7,
        "prompt_tokens_per_second": 14.2,
    }

    assert active_model_identity(cloud, "live", local) == (
        "groq/openai/gpt-oss-120b"
    )
    assert format_active_model_status(cloud, "live", local) == (
        "Cloud model: groq / openai/gpt-oss-120b | Tier: high"
    )


def test_local_model_status_remains_available_offline():
    local = {
        "model": "models/Qwen3.gguf",
        "generation_tokens_per_second": 8.7,
        "prompt_tokens_per_second": 14.2,
    }

    assert active_model_identity(None, "offline", local) == "models/Qwen3.gguf"
    assert "Model: Qwen3.gguf" in format_active_model_status(
        None, "offline", local
    )


def test_update_state_reader_accepts_windows_utf8_bom(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps({"installed_sha": "abc123"}).encode("utf-8")
    )

    assert read_update_state(state_path)["installed_sha"] == "abc123"
