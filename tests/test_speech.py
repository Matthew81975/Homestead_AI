import io
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

    def test_router_falls_back_when_neural_voice_fails(self):
        neural = _RecordingSpeechBackend(error=RuntimeError("neural failure"))
        native = _RecordingSpeechBackend()
        router = speech.SpeechRouter(neural=neural, native=native)

        self.assertEqual(router.speak("Fallback reply."), "native")
        self.assertEqual(native.spoken, ["Fallback reply."])

    def test_engine_rejects_empty_spoken_text(self):
        engine = speech.SpeechEngine(command=["speaker"])
        self.assertFalse(engine.speak("   "))


class _BytesResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


class _FakeKokoro:
    def __init__(self):
        self.calls = []

    def create(self, text, **options):
        self.calls.append((text, options))
        return [0.0, 0.25], 24000


class NaturalVoiceAssetsTests(unittest.TestCase):
    def test_download_natural_voice_assets_installs_complete_pack(self):
        payloads = {
            "kokoro-v1.0.onnx": b"model-bytes",
            "voices-v1.0.bin": b"voice-bytes",
        }

        def opener(url, timeout):
            name = url.rsplit("/", 1)[-1]
            return _BytesResponse(payloads[name])

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installed = speech.download_natural_voice_assets(root, opener=opener)
            contents = [path.read_bytes() for path in installed]
            ready = speech.natural_voice_ready(root)

        self.assertEqual(contents, [b"model-bytes", b"voice-bytes"])
        self.assertTrue(ready)

    def test_kokoro_backend_uses_alexandria_voice_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model_path, voices_path = speech.natural_voice_asset_paths(root)
            model_path.parent.mkdir(parents=True)
            model_path.write_bytes(b"model")
            voices_path.write_bytes(b"voices")
            kokoro = _FakeKokoro()
            played = []
            backend = speech.KokoroSpeechBackend(
                root=root,
                synthesizer=kokoro,
                player=lambda samples, rate: played.append((samples, rate)),
            )

            backend.speak("Good morning, Matthew.")

        self.assertEqual(
            kokoro.calls,
            [(
                "Good morning, Matthew.",
                {"voice": "af_heart", "speed": 0.95, "lang": "en-us"},
            )],
        )
        self.assertEqual(played, [([0.0, 0.25], 24000)])

    def test_natural_voice_ready_requires_both_nonempty_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            model, voices = speech.natural_voice_asset_paths(root)
            model.parent.mkdir(parents=True)
            model.write_bytes(b"model")
            self.assertFalse(speech.natural_voice_ready(root))
            voices.write_bytes(b"voices")
            self.assertTrue(speech.natural_voice_ready(root))


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
