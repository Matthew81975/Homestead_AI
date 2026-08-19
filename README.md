# HCS-AI v0.6 — HKR Algorithm Library

## v0.7 — Self-contained inference

HCS-AI now manages its own local `llama.cpp` inference server. The Windows setup
can download the current CPU runtime and the recommended Qwen3-4B-Instruct-2507
Q4_K_M model. The model starts with HCS-AI and stops when HCS-AI closes, so LM
Studio is no longer required. The System tab provides model selection, engine
status, start/stop controls, and access to the internal-AI setup utility.

HKR now treats reusable algorithms as first-class **capable knowledge**. It can mine algorithms from newly collected or existing HKR documents, normalize them into a searchable SQLite database, preserve source provenance, compare alternatives and complexity, and generate deployment-specific code through the local HCS model. The HCS coding LLM can call the algorithm database directly through `search_hkr_algorithms`, `get_hkr_algorithm`, and `code_hkr_algorithm`.

The HKR GUI is organized into **Research**, **Algorithms**, and **Software** workspaces. The Algorithms view provides natural-language/problem search, domain filtering, a sortable record table, detailed assumptions/constraints/failure modes/provenance, multi-selection comparison, and Python/C++ generation controls.

# HCS-AI v0.5 — HKR Software Resources

HKR now stores software resources in addition to documents. The first software ecosystem is PyPI: HCS can inspect package metadata, dependency declarations, Python-version requirements, identify a compatible wheel for the current runtime, download the selected distribution into a versioned HKR software cache, verify its SHA-256 against PyPI metadata, and search cached software later. Cached software is never installed or executed automatically.

The HKR Librarian tab now includes a **Software Library — Python packages (PyPI)** panel with Find, Cache in HKR, and Show Cached controls. `tensorflow` is pre-filled as an example package name.

# HCS-AI v0.4 — HKR Research Tool Service

HCS can now call HKR directly as a tool when it needs more information. The LLM searches the existing HKR catalog first, reads relevant passages from selected sources, and can ask HKR to acquire additional authoritative material when the local repository is insufficient. The External Library tab remains available for manual activation/deactivation of persistent HCS knowledge.

# HCS-AI v0.3

Homestead Computer Systems — local AI layer for Windows with integrated HKR knowledge management.

## Included

- One shared local AI service, starting at `127.0.0.1:8765` and automatically
  trying the next ports when the preferred port is already occupied
- Managed llama.cpp local-model engine with optional external OpenAI-compatible connection
- Desktop GUI with AI, Knowledge Base, HKR Librarian, External Library, Memory, MCP, and System tabs
- HKR master repository for acquiring, cataloging, summarizing, and packaging technical knowledge
- External Library tab for choosing which HKR files are active in HCS inference
- Add/remove selected HKR files without deleting the HKR originals
- PDF and text knowledge-base indexing
- Compact HKR summaries, chapter/section navigation, and index pointers
- Persistent SQLite memory and audit logging
- Desktop and Start Menu shortcuts created by the Windows installer

## HKR / HCS model

HKR is the master external library. Collecting a document does **not** automatically load it into the active HCS knowledge base. Open **External Library**, select one or more files, and choose **Add Selected to HCS**. Choose **Remove Selected from HCS** to unload them from active inference while keeping the source files safely stored in HKR.

This lets a large offline archive coexist with a smaller task-specific active knowledge set.

## Install

1. Extract the ZIP.
2. Double-click `install.bat`.
3. The installer creates the virtual environment, installs dependencies, creates Desktop and Start Menu shortcuts, and launches HCS-AI.
4. Accept the Internal AI download when prompted, or run it later from the System tab.

## Requirements

- Windows 10 or 11
- Python 3.10+
- About 3 GB free for the recommended internal model and runtime
