import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hcs_ai import model_manager, prompt_functions, telemetry


class PromptFunctionTests(unittest.TestCase):
    def test_conditional_prompt_function_calls_ask_and_returns_value(self):
        calls = []
        def ask(prompt, context=None, model=None, use_kb=True):
            calls.append((prompt, context, model, use_kb))
            return "long" if "detail" in prompt else "short"
        source = '''
def main(detail=False):
    if detail:
        result = ask("Give detail", {"x": 2})
    else:
        result = ask("Be brief", {"x": 1})
    return result
'''
        out = prompt_functions.run_script(source, {"detail": True}, ask_fn=ask)
        self.assertEqual(out["value"], "long")
        self.assertEqual(calls[0][0], "Give detail")
        self.assertTrue(any(step["event"] == "if" for step in out["trace"]))

    def test_user_defined_function_composes_prompt_calls(self):
        source = '''
def summarize(x):
    return ask("Summarize", x)

def main(text):
    first = summarize(text)
    return ask("Improve", first)
'''
        seen = []
        def ask(prompt, context=None, **kwargs):
            seen.append((prompt, context))
            return f"{prompt}:{context}"
        out = prompt_functions.run_script(source, {"text": "abc"}, ask_fn=ask)
        self.assertEqual(len(seen), 2)
        self.assertIn("Improve", out["value"])

    def test_rejects_unsafe_top_level_code(self):
        with self.assertRaises(prompt_functions.PromptScriptError):
            prompt_functions.run_script('import os\ndef main():\n    return 1')


class TelemetryTests(unittest.TestCase):
    def test_prefers_llama_cpp_timings(self):
        body = {
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "timings": {"prompt_per_second": 80.0, "predicted_per_second": 12.5},
        }
        out = telemetry.extract_telemetry("model.gguf", body, 3.0)
        self.assertEqual(out.prompt_tokens_per_second, 80.0)
        self.assertEqual(out.generation_tokens_per_second, 12.5)

    def test_output_tps_falls_back_to_wall_clock(self):
        body = {"usage": {"completion_tokens": 20}}
        out = telemetry.extract_telemetry("m", body, 2.0)
        self.assertEqual(out.generation_tokens_per_second, 10.0)
        self.assertIsNone(out.prompt_tokens_per_second)


class ModelManagerTests(unittest.TestCase):
    def test_quantization_is_detected_from_gguf_filename(self):
        self.assertEqual(model_manager.quantization_from_name("Qwen3-4B-Q4_K_M.gguf"), "Q4_K_M")

    def test_delete_rejects_files_outside_managed_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "models"
            managed.mkdir()
            outside = root / "outside.gguf"
            outside.write_bytes(b"x")
            with mock.patch.object(model_manager, "MODELS_DIR", managed), \
                 mock.patch.object(model_manager, "load_config", return_value={"inference": {"model_path": ""}}):
                with self.assertRaises(ValueError):
                    model_manager.delete_model(str(outside))

    def test_listing_models_survives_inaccessible_active_model_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            managed = Path(tmp) / "models"
            managed.mkdir()
            (managed / "available-Q4_K_M.gguf").write_bytes(b"gguf")
            original_resolve = Path.resolve

            def inaccessible_active(path, *args, **kwargs):
                if str(path) == "/inaccessible-model.gguf":
                    raise OSError(1005, "The volume does not contain a recognized file system")
                return original_resolve(path, *args, **kwargs)

            with mock.patch.object(model_manager, "MODELS_DIR", managed), \
                 mock.patch.object(model_manager, "load_config", return_value={
                     "inference": {"model_path": "/inaccessible-model.gguf"}
                 }), \
                 mock.patch.object(Path, "resolve", inaccessible_active):
                rows = model_manager.list_local_models()

            self.assertEqual([row["name"] for row in rows], ["available-Q4_K_M.gguf"])
            self.assertFalse(rows[0]["active"])


if __name__ == "__main__":
    unittest.main()
