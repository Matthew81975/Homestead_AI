# Lightweight Kokoro Voice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace HCS's GPL-linked Kokoro wrapper with a lightweight, offline, commercially distributable ONNX voice path that retains Alexandria's approved `af_heart` profile.

**Architecture:** HCS will use a small internal ONNX adapter, a pinned 8-bit Kokoro model, and an Apache-2.0 American-English dictionary/tokenization subset derived from Misaki 0.9.4. The existing speech router and GUI remain stable; verified assets, chunk-level fallback, and queue isolation make the optional neural path safe.

**Tech Stack:** Python 3.11, ONNX Runtime, NumPy, spaCy small English model, SoundFile, Tkinter, Windows SAPI fallback, unittest

**Spec:** `docs/superpowers/specs/2026-09-01-lightweight-kokoro-voice-design.md`

## Global Constraints

- Preserve `af_heart`, speed `0.95`, and American English.
- Voice setup remains explicit and free; no API calls are required after setup.
- Natural-voice assets are exactly `onnx/model_quantized.onnx` (92,361,116 bytes, SHA-256 `fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478`) and `voices/af_heart.bin` (522,240 bytes, SHA-256 `d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b`) from ONNX Community revision `4685882`.
- Do not install or import `kokoro-onnx`, `phonemizer`, `phonemizer-fork`, eSpeak, Torch, or Transformers.
- Vendored Misaki material is limited to its American-English dictionary/tokenization path and retains Apache-2.0 attribution.
- All text chunks are at most 240 characters.
- Previously played neural chunks are never repeated by SAPI.
- A backend failure cannot terminate the speech worker.
- Preserve existing HCS voice settings and SAPI-only behavior when neural assets are absent.

---

## File Structure

- Create `hcs_ai/vendor/misaki_en/__init__.py`: stable `EnglishG2P` and `UnsupportedPronunciation` interface.
- Create `hcs_ai/vendor/misaki_en/engine.py`: reviewed American-English logic adapted from Misaki 0.9.4.
- Create `hcs_ai/vendor/misaki_en/token.py`: Misaki token representation.
- Create `hcs_ai/vendor/misaki_en/number_words.py`: small HCS-owned cardinal, ordinal, year, and decimal normalization replacing LGPL `num2words`.
- Create `hcs_ai/vendor/misaki_en/data/__init__.py`, `us_gold.json`, and `us_silver.json`: American-English dictionaries copied from Misaki 0.9.4.
- Create `hcs_ai/vendor/misaki_en/LICENSE` and `UPSTREAM.md`: Apache license, source hash, copied-file inventory, and modifications.
- Create `hcs_ai/kokoro_voice.py`: asset manifest, verified downloader, phoneme vocabulary, lazy ONNX synthesis, and WAV playback.
- Modify `hcs_ai/speech.py`: hard chunking, chunk-aware fallback, unavailable-native guard, and queue exception isolation.
- Modify `hcs_ai/gui.py`: retain setup flow while reporting the exact approximately 93 MB asset download.
- Modify `requirements.txt`: remove the wrapper and add only the approved runtime packages.
- Create `scripts/check_voice_dependencies.py`: reject forbidden voice-path packages and imports.
- Create `THIRD_PARTY_NOTICES.md`: Kokoro, ONNX Community, Misaki, spaCy, ONNX Runtime, NumPy, and SoundFile notices.
- Create `tests/test_misaki_en.py`: pronunciation and unsupported-word behavior.
- Create `tests/test_kokoro_voice.py`: assets, encoding, ONNX inputs, lazy loading, and playback.
- Modify `tests/test_speech.py`: hard chunk limits, partial fallback, unavailable native backend, and queue survival.

---

### Task 1: Vendored American-English Pronunciation

**Files:**
- Create: `hcs_ai/vendor/__init__.py`
- Create: `hcs_ai/vendor/misaki_en/__init__.py`
- Create: `hcs_ai/vendor/misaki_en/engine.py`
- Create: `hcs_ai/vendor/misaki_en/token.py`
- Create: `hcs_ai/vendor/misaki_en/number_words.py`
- Create: `hcs_ai/vendor/misaki_en/data/__init__.py`
- Create: `hcs_ai/vendor/misaki_en/data/us_gold.json`
- Create: `hcs_ai/vendor/misaki_en/data/us_silver.json`
- Create: `hcs_ai/vendor/misaki_en/LICENSE`
- Create: `hcs_ai/vendor/misaki_en/UPSTREAM.md`
- Test: `tests/test_misaki_en.py`

**Interfaces:**
- Produces: `UnsupportedPronunciation(ValueError)`.
- Produces: `EnglishG2P(loader: Callable[[], Callable] | None = None)`.
- Produces: `EnglishG2P.phonemize(text: str) -> str`.
- Produces: `number_to_words(value: str | int | float, mode: str = "cardinal") -> str`.

- [ ] **Step 1: Write failing wrapper and number-normalization tests**

Create `tests/test_misaki_en.py`:

```python
import unittest
from unittest.mock import patch

from hcs_ai.vendor.misaki_en import EnglishG2P, UnsupportedPronunciation
from hcs_ai.vendor.misaki_en.number_words import number_to_words


class MisakiEnglishTests(unittest.TestCase):
    def test_number_words_cover_cardinal_ordinal_year_and_decimal(self):
        self.assertEqual(number_to_words(42), "forty two")
        self.assertEqual(number_to_words(21, mode="ordinal"), "twenty first")
        self.assertEqual(number_to_words(2026, mode="year"), "twenty twenty six")
        self.assertEqual(number_to_words("3.14"), "three point one four")

    def test_wrapper_returns_phonemes_from_injected_engine(self):
        g2p = EnglishG2P(loader=lambda: lambda text: ("həlˈO", []))
        self.assertEqual(g2p.phonemize("hello"), "həlˈO")

    def test_wrapper_rejects_unknown_pronunciation(self):
        g2p = EnglishG2P(loader=lambda: lambda text: ("hˈI ❓", []))
        with self.assertRaises(UnsupportedPronunciation):
            g2p.phonemize("hi qzxx")

    def test_import_does_not_load_forbidden_neural_packages(self):
        with patch.dict("sys.modules", {"torch": None, "transformers": None}):
            from hcs_ai.vendor.misaki_en import engine
            self.assertFalse(hasattr(engine, "FallbackNetwork"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify the missing package fails**

Run:

```bash
python -m unittest tests.test_misaki_en -v
```

Expected: import failure for `hcs_ai.vendor.misaki_en`.

- [ ] **Step 3: Implement the HCS-owned number normalizer**

Create `number_words.py` with lookup tables for zero through nineteen, tens, and scales through billion. Implement `number_to_words` by composing hundreds and scales, converting the final word for ordinals, using the conventional split for years from 2000 through 2099, and spelling decimal digits after “point.” Reject non-finite values and unsupported modes with `ValueError`.

Use this public skeleton:

```python
def number_to_words(value: str | int | float, mode: str = "cardinal") -> str:
    if mode not in {"cardinal", "ordinal", "year"}:
        raise ValueError(f"Unsupported number mode: {mode}")
    # Parse sign and decimal digits without float-rounding.
    # Compose integer groups with _integer_words.
    # Apply _ordinalize to the final lexical word when requested.
    # For year mode 2000..2099, return "twenty" plus the final two digits.
```

- [ ] **Step 4: Extract and adapt the exact Misaki 0.9.4 source**

Download and verify the wheel:

```bash
python -m pip download --no-deps misaki==0.9.4 -d build/vendor
python -c "import hashlib,pathlib; p=pathlib.Path('build/vendor/misaki-0.9.4-py3-none-any.whl'); assert hashlib.sha256(p.read_bytes()).hexdigest() == '90e2eeb169786c014c429e5058d2ea6bcd02d651f2a24450ba6c9ffc0f8da15a'"
```

Extract only:

```bash
python -m zipfile -e build/vendor/misaki-0.9.4-py3-none-any.whl build/vendor/misaki
cp build/vendor/misaki/misaki/en.py hcs_ai/vendor/misaki_en/engine.py
cp build/vendor/misaki/misaki/token.py hcs_ai/vendor/misaki_en/token.py
cp build/vendor/misaki/misaki/data/us_gold.json hcs_ai/vendor/misaki_en/data/us_gold.json
cp build/vendor/misaki/misaki/data/us_silver.json hcs_ai/vendor/misaki_en/data/us_silver.json
cp build/vendor/misaki/misaki-0.9.4.dist-info/licenses/LICENSE hcs_ai/vendor/misaki_en/LICENSE
```

Apply these deliberate edits to `engine.py`:

```python
from . import data
from .token import MToken
from .number_words import number_to_words as num2words
```

Remove the external `num2words` import. Replace calls using `to='ordinal'`, `to='year'`, and `to='cardinal'` with `mode='ordinal'`, `mode='year'`, and `mode='cardinal'`. Rename upstream `G2P` to `MisakiEnglishEngine`. Preserve `fallback=None`; do not add a fallback class, network download, Torch, Transformers, phonemizer, or eSpeak.

Create `UPSTREAM.md` recording:

```markdown
# Vendored Misaki English subset

Source: misaki 0.9.4 wheel
Wheel SHA-256: 90e2eeb169786c014c429e5058d2ea6bcd02d651f2a24450ba6c9ffc0f8da15a
Upstream license: Apache-2.0

Copied: en.py, token.py, data/us_gold.json, data/us_silver.json
Excluded: all multilingual modules, espeak.py, packaging metadata, and neural fallback code.
Modified: local imports, class name, number normalization, and automatic spaCy-download removal.
```

- [ ] **Step 5: Add the stable wrapper and remove runtime downloading**

In `hcs_ai/vendor/misaki_en/__init__.py`:

```python
class UnsupportedPronunciation(ValueError):
    pass


class EnglishG2P:
    def __init__(self, loader=None):
        self._loader = loader or self._load_default
        self._engine = None

    @staticmethod
    def _load_default():
        from .engine import MisakiEnglishEngine
        return MisakiEnglishEngine(
            trf=False,
            british=False,
            fallback=None,
        )

    def phonemize(self, text: str) -> str:
        if self._engine is None:
            self._engine = self._loader()
        phonemes, _tokens = self._engine(str(text or ""))
        if not phonemes or "❓" in phonemes:
            raise UnsupportedPronunciation(text)
        return phonemes
```

In `MisakiEnglishEngine.__init__`, replace `spacy.cli.download(name)` with a clear `RuntimeError` instructing the user to rerun HCS setup if `en_core_web_sm` is unavailable. Do not perform network access during speech.

- [ ] **Step 6: Run pronunciation tests**

Run:

```bash
python -m unittest tests.test_misaki_en -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add hcs_ai/vendor tests/test_misaki_en.py
git commit -m "feat: add lightweight English pronunciation"
```

---

### Task 2: Verified Assets and Direct ONNX Synthesis

**Files:**
- Create: `hcs_ai/kokoro_voice.py`
- Test: `tests/test_kokoro_voice.py`
- Modify: `hcs_ai/speech.py`

**Interfaces:**
- Produces: `VoiceAsset(filename: str, url: str, size: int, sha256: str)`.
- Produces: `VOICE_ASSETS: tuple[VoiceAsset, ...]`.
- Produces: `natural_voice_asset_paths(root: Path) -> tuple[Path, Path]`.
- Produces: `natural_voice_ready(root: Path) -> bool`.
- Produces: `download_natural_voice_assets(root: Path, opener=...) -> tuple[Path, Path]`.
- Produces: `encode_phonemes(phonemes: str) -> list[int]`.
- Produces: `KokoroOnnxBackend(root=ROOT, session_factory=None, g2p=None, player=None)`.

- [ ] **Step 1: Write failing asset-integrity and encoder tests**

Create tests using small injected `VoiceAsset` fixtures whose payload hashes are computed in the test. Cover:

```python
def test_ready_rejects_wrong_hash_even_at_expected_size(self): ...
def test_download_rejects_truncated_asset_and_removes_part(self): ...
def test_download_rejects_wrong_hash_and_preserves_previous_valid_file(self): ...
def test_encode_phonemes_adds_padding_and_rejects_unknown_symbols(self): ...
```

Assert `encode_phonemes("həlˈO")` returns a list beginning and ending with `0`, never exceeds 512 entries, and raises `UnsupportedPronunciation` for a symbol outside the pinned vocabulary.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python -m unittest tests.test_kokoro_voice.NaturalVoiceAssetTests tests.test_kokoro_voice.PhonemeEncodingTests -v
```

Expected: import failure for `hcs_ai.kokoro_voice`.

- [ ] **Step 3: Implement the immutable asset manifest and verifier**

In `kokoro_voice.py`, define:

```python
@dataclass(frozen=True)
class VoiceAsset:
    filename: str
    url: str
    size: int
    sha256: str


VOICE_ASSETS = (
    VoiceAsset(
        "model_quantized.onnx",
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/4685882/onnx/model_quantized.onnx?download=true",
        92_361_116,
        "fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478",
    ),
    VoiceAsset(
        "af_heart.bin",
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/4685882/voices/af_heart.bin?download=true",
        522_240,
        "d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b",
    ),
)
```

Implement streaming SHA-256 calculation, exact-size checking, `.part` cleanup, and atomic replacement only after verification. `natural_voice_ready` must revalidate both installed files.

- [ ] **Step 4: Add the pinned Kokoro vocabulary**

Copy the `vocab` mapping from Kokoro-82M configuration revision `785407d1adfa7ae8fbef8ffd85f34ca127da3039` into a literal `PHONEME_IDS: dict[str, int]`. Implement:

```python
def encode_phonemes(phonemes: str) -> list[int]:
    try:
        values = [PHONEME_IDS[ch] for ch in phonemes]
    except KeyError as exc:
        raise UnsupportedPronunciation(str(exc)) from exc
    if len(values) > 510:
        raise UnsupportedPronunciation("Kokoro phoneme context exceeds 510 tokens")
    return [0, *values, 0]
```

- [ ] **Step 5: Run integrity and encoding tests**

Run the focused command from Step 2.

Expected: all focused tests pass.

- [ ] **Step 6: Write failing ONNX input, profile, and lazy-load tests**

Use a recording session whose `run` method captures `input_ids`, `style`, and `speed`. Use an injected G2P returning `həlˈO`, an injected session factory, and a player list. Assert:

- session creation occurs only on first `speak`;
- `input_ids` has shape `(1, n)` and is padded;
- style has shape `(1, 256)` and selects row `len(tokens_without_padding)`;
- speed is a float32 array containing `0.95`;
- the player receives model output at 24,000 Hz.

- [ ] **Step 7: Run ONNX tests and verify failure**

Run:

```bash
python -m unittest tests.test_kokoro_voice.KokoroOnnxBackendTests -v
```

Expected: missing `KokoroOnnxBackend` behavior.

- [ ] **Step 8: Implement lazy ONNX inference**

Implement `KokoroOnnxBackend.available`, `_load`, and `speak`. Load `af_heart.bin` with `numpy.fromfile(dtype=numpy.float32).reshape(-1, 1, 256)`. Build `input_ids` as `numpy.int64`, select style row by unpadded token count, pass speed as `numpy.array([0.95], dtype=numpy.float32)`, call `InferenceSession.run(None, inputs)`, and pass the first audio row to the existing WAV player at 24,000 Hz.

- [ ] **Step 9: Move compatibility exports into speech.py**

Import and re-export `natural_voice_asset_paths`, `natural_voice_ready`, and `download_natural_voice_assets` from `hcs_ai.kokoro_voice` so existing GUI imports remain valid. Replace `KokoroSpeechBackend` construction with `KokoroOnnxBackend`.

- [ ] **Step 10: Run all natural-voice tests and commit**

```bash
python -m unittest tests.test_misaki_en tests.test_kokoro_voice tests.test_speech -v
git add hcs_ai/kokoro_voice.py hcs_ai/speech.py tests/test_kokoro_voice.py
git commit -m "feat: synthesize Alexandria voice with direct ONNX"
```

---

### Task 3: Hard Chunking and Failure-Isolated Routing

**Files:**
- Modify: `hcs_ai/speech.py`
- Modify: `tests/test_speech.py`

**Interfaces:**
- Consumes: `KokoroOnnxBackend.speak(text: str) -> None`.
- Produces: `sentence_chunks(text: str, max_chars: int = 240) -> list[str]`, with every chunk length at most `max_chars`.
- Produces: `SpeechRouter.speak(text: str) -> str`, returning `"neural"`, `"native"`, or `"unavailable"`.

- [ ] **Step 1: Add failing hard-limit and second-chunk fallback tests**

Add a 700-character punctuation-free sentence and assert every chunk is at most 80 characters and reconstructs the original words. Add a neural backend that succeeds once and raises on its second call. Assert native speech receives only the second and remaining chunks joined in order, never the first chunk.

- [ ] **Step 2: Add failing native-unavailable and queue-survival tests**

Create an unavailable native backend whose `speak` raises if called; assert the router returns `"unavailable"` without invoking it. Create a router that raises for its first queued reply and records the second; assert `SpeechEngine._queue.join()` completes and the second reply is processed.

- [ ] **Step 3: Run tests to verify failures**

```bash
python -m unittest tests.test_speech -v
```

Expected: failures for hard chunk length, repeated fallback prefix, unavailable native invocation, and worker termination.

- [ ] **Step 4: Implement hard sentence splitting**

Preserve sentence boundaries first. For any sentence over `max_chars`, repeatedly split at the final clause punctuation or whitespace at or before the limit; if none exists, split exactly at the limit. Strip boundary whitespace and never emit an empty chunk.

- [ ] **Step 5: Implement remaining-chunk fallback**

Calculate chunks once. Iterate with an index. On neural failure, join `chunks[index:]` with spaces and send only that remainder to native speech when `native.available` is true. If neither backend is available, return `"unavailable"`.

- [ ] **Step 6: Contain worker exceptions per queue item**

Wrap `self.router.speak(text)` with `except Exception: pass` inside the existing `finally: self._queue.task_done()`. Keep the worker loop alive for subsequent queued replies.

- [ ] **Step 7: Run tests and commit**

```bash
python -m unittest tests.test_speech -v
git add hcs_ai/speech.py tests/test_speech.py
git commit -m "fix: isolate speech fallback and queue failures"
```

---

### Task 4: Dependencies, Setup Copy, and License Guard

**Files:**
- Modify: `requirements.txt`
- Modify: `hcs_ai/gui.py`
- Create: `scripts/check_voice_dependencies.py`
- Create: `THIRD_PARTY_NOTICES.md`
- Test: `tests/test_voice_dependencies.py`

**Interfaces:**
- Consumes: exact asset total `92_883_356` bytes.
- Produces: `check_voice_dependencies(requirements_text: str, installed_names: set[str]) -> list[str]`.

- [ ] **Step 1: Write failing dependency guard tests**

Create tests asserting the current `kokoro-onnx` requirement is rejected; names normalized across hyphens/underscores; each of `phonemizer`, `phonemizer-fork`, `espeakng-loader`, `torch`, and `transformers` is rejected; and the approved requirements return no violations.

- [ ] **Step 2: Run the guard tests and verify failure**

```bash
python -m unittest tests.test_voice_dependencies -v
```

Expected: import failure for `scripts.check_voice_dependencies`.

- [ ] **Step 3: Implement the guard**

Parse non-comment requirement names and installed distribution names using `packaging.utils.canonicalize_name`. Return sorted messages for every forbidden name. The CLI reads `requirements.txt`, reads installed distributions through `importlib.metadata.distributions()`, prints violations, and exits 1 when any exist.

- [ ] **Step 4: Replace the runtime dependencies**

Remove `kokoro-onnx>=0.6.0`. Add pinned compatible floors:

```text
numpy>=2.0
onnxruntime>=1.20
soundfile>=0.13
spacy>=3.8,<3.9
https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl
```

Do not add Misaki itself because the reviewed subset is vendored.

- [ ] **Step 5: Update setup copy and notices**

Change the confirmation from approximately 354 MB to approximately 93 MB. Keep the explicit confirmation and background download. Add notices with upstream URLs, versions or revisions, license names, copied material, modifications, and license-file locations.

- [ ] **Step 6: Run the guard and tests**

```bash
python scripts/check_voice_dependencies.py
python -m unittest tests.test_voice_dependencies tests.test_misaki_en tests.test_kokoro_voice tests.test_speech -v
```

Expected: dependency guard exits 0 and all tests pass.

- [ ] **Step 7: Commit**

```bash
git add requirements.txt hcs_ai/gui.py scripts/check_voice_dependencies.py THIRD_PARTY_NOTICES.md tests/test_voice_dependencies.py
git commit -m "build: pin lightweight voice dependencies"
```

---

### Task 5: Full Windows Verification and Draft PR Update

**Files:**
- Modify: `.github/workflows/diagnostics.yml` only if the current workflow does not run the dependency guard.
- Modify: pull request description for PR #28.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: green Windows/Python 3.11 Diagnostics CI and a manual-test handoff.

- [ ] **Step 1: Add the dependency guard to Windows CI**

Ensure the workflow runs:

```yaml
- name: Reject forbidden voice dependencies
  run: python scripts/check_voice_dependencies.py
```

Place it after dependency installation and before compilation/tests.

- [ ] **Step 2: Run local static and full tests**

```bash
python scripts/check_voice_dependencies.py
python -m compileall -q hcs_ai tests scripts
python -m unittest discover -s tests -v
```

Expected: guard exit 0, compile exit 0, and zero test failures/errors.

- [ ] **Step 3: Verify the dependency graph explicitly**

```bash
python -m pip freeze | python -c "import sys; banned=('kokoro-onnx','phonemizer','phonemizer-fork','espeakng-loader','torch==','transformers=='); text=sys.stdin.read().lower(); found=[x for x in banned if x in text]; assert not found, found"
```

Expected: exit 0.

- [ ] **Step 4: Update the draft pull request**

Document the 93 MB pinned assets, direct ONNX adapter, vendored Misaki subset, forbidden dependency guard, fallback isolation, and exact verification commands. Keep PR #28 draft until CI passes.

- [ ] **Step 5: Trigger and inspect Windows CI**

Push the final commit, wait for the Diagnostics workflow tied to the new head SHA, and inspect every job. A prior green run is not sufficient.

- [ ] **Step 6: Manual Windows acceptance**

On Matthew's laptop:

1. Start HCS with natural voice assets absent and confirm SAPI fallback still speaks.
2. Click Voice Setup, confirm the dialog says approximately 93 MB, and install.
3. Speak: “Good morning, Matthew. Alexandria is ready.”
4. Speak a multi-sentence reply longer than 240 characters.
5. Confirm the warm `af_heart` voice, no repeated sentence, responsive GUI, and continued chat operation.
6. Restart HCS and confirm assets remain ready without another download.

- [ ] **Step 7: Commit any workflow change**

```bash
git add .github/workflows/diagnostics.yml
git commit -m "ci: audit lightweight voice dependencies"
```

Skip this commit only when the workflow already executes the guard.
