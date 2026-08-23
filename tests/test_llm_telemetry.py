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


if __name__ == "__main__":
    unittest.main()
