import unittest
from unittest import mock

from hcs_ai import llm


class _Response:
    is_success = True
    status_code = 200
    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"completion_tokens": 2}}


class _Client:
    def __init__(self, *args, **kwargs): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


class LlmTelemetryTests(unittest.TestCase):
    def test_chat_records_telemetry_and_accepts_model_override(self):
        seen = []
        with mock.patch.object(llm, "load_config", return_value={"app": {"system_prompt": "sys"}}), \
             mock.patch.object(llm, "llm_config", return_value={
                 "base_url": "http://local/v1", "timeout_seconds": 10,
                 "temperature": 0.1, "model": "auto",
             }), \
             mock.patch.object(llm, "_tool_definitions", return_value=[]), \
             mock.patch.object(llm, "_resolve_model", return_value="chosen-model") as resolver, \
             mock.patch.object(llm, "_post_completion", return_value=(_Response(), False)), \
             mock.patch.object(llm, "record_response", side_effect=lambda model, body, elapsed: seen.append(model)), \
             mock.patch.object(llm.httpx, "Client", _Client):
            out = llm.chat("hello", use_kb=False, model="chosen-model")
        self.assertEqual(out["text"], "ok")
        resolver.assert_called_once()
        self.assertEqual(seen[-1], "chosen-model")
        self.assertIn("backend_timings", out)
        self.assertIn("prompt_meta", out)
        self.assertIn("total_seconds", out["backend_timings"])
        self.assertIn("model_resolve_seconds", out["backend_timings"])
        self.assertEqual(out["prompt_meta"]["kb_hits"], 0)
        self.assertEqual(len(out["backend_timings"]["rounds"]), 1)
        self.assertIn("completion_seconds", out["backend_timings"]["rounds"][0])

    def test_invalid_tool_call_with_text_does_not_trigger_second_round(self):
        class _ToolResponse:
            is_success = True
            status_code = 200
            def json(self):
                return {
                    "choices": [{
                        "message": {
                            "content": "HCS_OK",
                            "tool_calls": [{
                                "id": "bad1",
                                "type": "function",
                                "function": {"name": "HCS_OK", "arguments": "{}"},
                            }],
                        }
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                }

        calls = []
        with mock.patch.object(llm, "load_config", return_value={"app": {"system_prompt": "sys"}}), \
             mock.patch.object(llm, "llm_config", return_value={
                 "base_url": "http://local/v1", "timeout_seconds": 10,
                 "temperature": 0.1, "model": "auto",
             }), \
             mock.patch.object(llm, "_tool_definitions", return_value=[]), \
             mock.patch.object(llm, "_resolve_model", return_value="chosen-model"), \
             mock.patch.object(llm, "_post_completion", side_effect=lambda *a, **k: (calls.append(1) or (_ToolResponse(), True))), \
             mock.patch.object(llm, "record_response"), \
             mock.patch.object(llm, "public_tool_specs", return_value=[{"name": "system_info", "description": "", "schema": {}}]), \
             mock.patch.object(llm, "call_tool") as call_tool_mock, \
             mock.patch.object(llm.httpx, "Client", _Client):
            out = llm.chat("Reply with exactly: HCS_OK", use_kb=False)

        self.assertEqual(out["text"], "HCS_OK")
        self.assertEqual(len(calls), 1)
        self.assertEqual(out["backend_timings"]["rounds"][0]["valid_tool_calls"], 0)
        self.assertEqual(out["backend_timings"]["rounds"][0]["invalid_tool_calls"], ["HCS_OK"])
        call_tool_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
