import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hcs_ai import config, speech


class _RecordingSpeechBackend:
    def __init__(self, available=True, error=None):
        self.available = available
        self.error = error
        self.spoken = []

    def speak(self, text):
        if self.error:
            raise self.error
        self.spoken.append(text)


class SpeechHelpersTests(unittest.TestCase):
    def test_clean_for_speech_removes_chat_markdown(self):
        text = "# **Answer**\n- Visit [the guide](https://example.test).\n- Use \x60voice mode\x60."
        self.assertEqual(
            speech.clean_for_speech(text),
            "Answer Visit the guide. Use voice mode.",
        )

    def test_sentence_chunks_preserve_complete_sentences(self):
        self.assertEqual(
            speech.sentence_chunks(
                "First thought. Second thought! Final question?",
                max_chars=24,
            ),
            ["First thought.", "Second thought!", "Final question?"],
        )

    @patch("hcs_ai.speech.subprocess.run")
    def test_run_speech_command_passes_reply_over_stdin(self, run):
        speech.run_speech_command(["speaker", "--stdin"], "hello; $(safe)")
        self.assertEqual(run.call_args.kwargs["input"], "hello; $(safe)")
        self.assertEqual(run.call_args.args[0], ["speaker", "--stdin"])
        self.assertFalse(run.call_args.kwargs["check"])

    def test_router_prefers_ready_neural_backend(self):
        neural = _RecordingSpeechBackend()
        native = _RecordingSpeechBackend()
        router = speech.SpeechRouter(neural=neural, native=native)

        self.assertEqual(router.speak("A warm reply."), "neural")
        self.assertEqual(neural.spoken, ["A warm reply."])
        self.assertEqual(native.spoken, [])

    def test_engine_rejects_empty_spoken_text(self):
        engine = speech.SpeechEngine(command=["speaker"])
        self.assertFalse(engine.speak("   "))


class VoicePreferenceTests(unittest.TestCase):
    def test_update_local_config_preserves_existing_local_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            local_path = Path(folder) / "config.json"
            local_path.write_text(
                json.dumps({"cloud_ai": {"enabled": True}}),
                encoding="utf-8",
            )
            with patch.object(config, "LOCAL_CONFIG_PATH", local_path):
                updated = config.update_local_config(
                    {"voice": {"speak_replies": True}}
                )
            saved = json.loads(local_path.read_text(encoding="utf-8"))

        self.assertTrue(updated["cloud_ai"]["enabled"])
        self.assertTrue(saved["voice"]["speak_replies"])


if __name__ == "__main__":
    unittest.main()
