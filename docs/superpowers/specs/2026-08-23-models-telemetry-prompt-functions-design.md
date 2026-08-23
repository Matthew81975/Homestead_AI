# HCS Models, Telemetry, and Prompt Functions Design

## Goal
Add the HCS features approved after the Home-tab integration: model discovery/management, measured model performance, and a prompt-as-function scripting layer.

## Architecture
Keep the existing managed llama.cpp engine authoritative for loading and unloading models. Add `model_manager.py` for internet/local model catalog operations, `telemetry.py` for durable inference measurements, `prompt_functions.py` for a safe high-level DSL, and `gui_recent.py` as an extension over the existing Home-enabled GUI. The existing large GUI and FastAPI service remain largely unchanged.

## Models
Search Hugging Face for GGUF files, download into HCS's managed `models/` directory, retain source/checksum metadata, estimate RAM fit, list local models, and allow import/load/unload/delete/open-folder operations. Active-model changes use the existing `/inference/config` endpoint so engine process ownership remains unchanged.

## Telemetry
Capture llama.cpp/OpenAI-compatible timing and usage data at the LLM request boundary. Prefer server-reported prompt and generation token rates; if generation timing is absent, derive output throughput from completion tokens / wall-clock request time. Never invent a prompt rate. Persist samples with model and hardware identifiers.

## Prompt Functions
Use a restricted Python-like syntax parsed with `ast` but never executed with `eval` or `exec`. Scripts define `main(...)` and optional helper functions. Supported flow includes assignments, `if/else`, returns, comparisons, safe data structures, and whitelisted calls. Core primitives are `ask`, `ask_json`, `kb`, and `tool`. Each run produces an execution trace. Saved scripts keep versioned revisions.

## GUI
Add top-level Models and Prompt Functions tabs through a subclass layer. Add the latest measured model throughput to the AI tab. Model discovery/download runs off the Tk main thread. Prompt Functions provide source editing, basic highlighting, JSON inputs, save/load/history, run, and trace output.

## Safety and compatibility
Only GGUF downloads from validated Hugging Face repository/file identifiers are accepted. Deletion is limited to HCS's managed model directory. Telemetry failures cannot break inference. Prompt Functions cannot import Python modules or access arbitrary attributes/functions.
