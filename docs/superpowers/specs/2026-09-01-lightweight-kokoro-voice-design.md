# Lightweight Commercially Clean Kokoro Voice Design

Date: 2026-09-01  
Status: Approved for implementation

## Context

HCS needs spoken replies that sound warmer and more conversational than the Windows native SAPI voice. The first implementation used the `kokoro-onnx` Python package with Kokoro's `af_heart` voice, but the package currently pulls a GPL phonemizer dependency. HCS may be sold commercially, so the production voice path must avoid that dependency while remaining practical on Matthew's 8 GB Windows laptop.

## Goals

- Preserve the approved warm `af_heart` voice at speed `0.95`.
- Run fully offline after an explicit one-time setup.
- Keep the runtime lightweight enough for an 8 GB laptop.
- Use a commercially distributable dependency path with documented licenses.
- Preserve Windows native speech as an automatic fallback.
- Prevent speech errors from affecting chat or killing the speech queue.
- Keep setup and ongoing use free of API charges.

## Non-goals

- Speech recognition or wake-word detection.
- Multilingual neural speech.
- Voice cloning or voice training.
- Replacing the existing speech settings or GUI flow.
- Bundling voice assets without a separate distribution-license review.

## Architecture

The public behavior of `SpeechEngine` and `SpeechRouter` remains stable. The neural backend is replaced by a small internal Kokoro ONNX adapter.

The adapter:

1. Normalizes and chunks text.
2. Converts American English text to Kokoro-compatible phonemes with Misaki's dictionary-based path and `fallback=None`.
3. Maps phonemes to the pinned Kokoro vocabulary.
4. Runs a quantized Kokoro model through ONNX Runtime.
5. Selects the `af_heart` voice vector and synthesizes at speed `0.95`.
6. Plays the resulting waveform through the existing WAV playback layer.

The install must not include `phonemizer`, `phonemizer-fork`, eSpeak, Torch, or Transformers. Any third-party inference code adapted into HCS must retain its required MIT or Apache attribution.

## Components

### Asset manifest

A manifest defines each required file with:

- immutable or version-pinned source URL;
- destination filename;
- exact byte size;
- SHA-256 digest;
- upstream project and license.

The pinned asset revision is ONNX Community's Apache-2.0 Kokoro-82M v1.0 ONNX commit `4685882`. HCS downloads only these two files:

| Asset | Exact bytes | SHA-256 |
| --- | ---: | --- |
| `onnx/model_quantized.onnx` | 92,361,116 | `fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478` |
| `voices/af_heart.bin` | 522,240 | `d583ccff3cdca2f7fae535cb998ac07e9fcb90f09737b9a41fa2734ec44a8f0b` |

Download URLs use `https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX/resolve/4685882/<path>?download=true`. The voice asset download is 92,883,356 bytes (about 93 MB). The Kokoro vocabulary is represented as reviewed source data in HCS rather than downloaded at runtime.

### Downloader

Voice Setup remains an explicit user action and discloses the approximate download size. Downloads run off the Tk thread, stream into `.part` files, and verify both size and SHA-256 before atomic installation. A mismatch deletes the partial file and reports failure. Readiness checks validate the installed files against the same manifest.

### Pronunciation layer

Misaki is installed without its `en` dependency extra. HCS declares only the permissive runtime packages actually required by Misaki's non-transformer English dictionary path. The G2P object is configured with `trf=False`, American English, and `fallback=None`.

Unknown or unpronounceable text must not invoke eSpeak. Instead, the neural backend reports a chunk-level failure so the router can use native Windows speech for that chunk.

Before implementation is accepted, CI produces and reviews the resolved dependency license list. LGPL dependencies, if any, must remain dynamically installed and have their license/notice obligations documented; GPL dependencies are not accepted in the natural-voice path.

### ONNX synthesis adapter

The internal adapter owns model-session initialization, vocabulary encoding, voice-vector selection, inference inputs, output conversion, and audio playback. Initialization stays lazy so HCS startup is not delayed when spoken replies are disabled.

### Router and queue

Speech is processed in chunks with a hard maximum length. Natural speech is preferred when ready. If chunk N fails after earlier chunks played successfully, native speech receives chunk N and only the remaining chunks. Previously spoken content is never repeated.

If native speech is unavailable, the router returns an unavailable result without invoking an empty command. Backend exceptions are contained per queue item, and the worker continues with later replies.

## Data flow

1. Alexandria produces a text reply.
2. `clean_for_speech` removes presentation-only Markdown.
3. `sentence_chunks` preserves sentence boundaries where possible and hard-splits oversized text at clauses or whitespace.
4. Each chunk is passed to the natural backend.
5. Misaki produces phonemes without an external fallback.
6. The ONNX adapter generates waveform samples for `af_heart`.
7. The WAV playback layer plays the chunk.
8. Any chunk failure routes that chunk and the remainder to SAPI.
9. The queue marks the reply complete and continues.

## Error handling

- Missing assets: natural backend is unavailable; SAPI remains usable.
- Interrupted download: partial file is removed.
- Size or hash mismatch: asset is rejected and setup reports a verification error.
- Unsupported pronunciation: failed chunk and remaining chunks route to SAPI.
- Neural model or playback exception: failed chunk and remaining chunks route to SAPI.
- Missing SAPI command: return unavailable without launching a process.
- SAPI exception: contain it within the current reply and continue the queue.
- GUI shutdown during setup: no partial file is promoted to ready state.

## Testing

Tests must cover:

- sentence-boundary preservation and hard maximum chunk length;
- Markdown cleaning;
- exact `af_heart`, `0.95`, and American-English profile;
- deterministic phoneme-to-ID encoding;
- expected ONNX input construction and output playback;
- lazy initialization;
- complete verified asset installation;
- interrupted, truncated, corrupt, wrong-size, and wrong-hash assets;
- neural preference;
- neural failure on the first chunk;
- neural failure after at least one successful chunk without repeated speech;
- unknown pronunciation fallback;
- unavailable native backend;
- native backend exception followed by successful processing of another queued reply;
- existing voice-setting persistence;
- resolved dependency audit rejecting GPL voice-path packages.

Windows CI installs the exact dependency set, compiles the project, runs the full test suite, and records the resolved dependencies. The draft pull request remains unmerged until CI passes.

## Acceptance criteria

- A short and a multi-sentence reply speak successfully on Matthew's Windows laptop.
- The voice uses `af_heart` at speed `0.95`.
- The installed voice path contains no `phonemizer`, `phonemizer-fork`, eSpeak, Torch, or Transformers.
- A corrupt asset cannot be marked ready.
- Mid-reply neural failure does not repeat already spoken content.
- A failed reply cannot terminate the speech worker or strand later replies.
- HCS chat remains functional with no natural voice assets installed.
- Setup and ongoing speech incur no API cost.
