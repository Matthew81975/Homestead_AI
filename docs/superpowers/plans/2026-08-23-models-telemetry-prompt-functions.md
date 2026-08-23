# Models, Telemetry, and Prompt Functions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add model discovery/management, per-model inference telemetry, and a safe Prompt Functions language/UI to HCS.

**Architecture:** Preserve the existing llama.cpp engine and service endpoints, adding focused modules for model catalog operations, telemetry persistence, prompt-function interpretation, and a GUI extension layer. Instrument `llm.chat()` at the model-response boundary and keep the updater aware of every new module.

**Tech Stack:** Python 3.10+, Tkinter, httpx, urllib, SQLite, psutil, Python `ast`, existing llama.cpp OpenAI-compatible API.

**Spec:** `docs/superpowers/specs/2026-08-23-models-telemetry-prompt-functions-design.md`

## Global Constraints
- Windows 10/11 remains the primary desktop target.
- Existing llama.cpp engine process control remains authoritative.
- Prompt Functions must not use Python `eval` or `exec`.
- Model deletion is confined to the managed HCS models directory.
- Telemetry collection must never make an inference request fail.

---

### Task 1: Inference telemetry
- [x] Add telemetry extraction/persistence module.
- [x] Add tests for server timing preference and wall-clock fallback.
- [x] Instrument `llm.chat()` and support an explicit model override.

### Task 2: Model manager
- [x] Add Hugging Face GGUF discovery, download, metadata, import, local listing, compatibility, and deletion safeguards.
- [x] Add tests for quantization parsing and managed-folder deletion policy.

### Task 3: Prompt Functions runtime
- [x] Add safe AST interpreter, LLM/tool/KB primitives, conditionals, helper functions, traces, persistence, and revision history.
- [x] Add tests for conditionals, function composition, and unsafe top-level rejection.

### Task 4: GUI integration
- [x] Add Models and Prompt Functions tabs through a Home-GUI subclass.
- [x] Add AI telemetry display.
- [x] Add model search/download/local-management controls.
- [x] Add Prompt Function editor, inputs, highlighting, save/load/history, run, and trace controls.

### Task 5: Packaging
- [x] Point the desktop wrapper at the new GUI layer.
- [x] Add new modules to the updater manifest.
- [x] Bump to v0.10.0 and add release notes.
- [x] Run focused tests and syntax compilation.
