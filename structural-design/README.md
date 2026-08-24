# HCS Integrated Building/Site Design — Engineering Kernel

This directory contains the renderer-independent calculation core for HCS's integrated building and site design subsystem.

Phase 1 supports dual-unit input conversion, straight mass-wall geometry, rectangular openings, earthbag/rubble assembly metadata with provenance, and transparent Level-1 structural checks.

The kernel deliberately does **not** import the HCS UI, LLM backend, or Ursina/Panda3D. HCS calls the public API, while a shared visualization service renders engineering and Knowledge Tree data through separate adapters.

## Current Level-1 assumptions

- Internal calculations are SI.
- Lateral pressure is uniform over gross projected wall area.
- Openings reduce load-carrying pier width by their horizontal projection, conservative for windows.
- Vertical load distributes to piers by pier width.
- Sliding uses friction only when a friction coefficient is explicitly provided.
- Unknown material strengths remain unevaluated rather than receiving silent defaults.

## Planned HCS integration

HCS is the host/orchestrator and supplies the LLM, knowledge lookup, code/supplier research, UI, persistence, and workflow coordination. The engineering kernel remains testable independently so structural calculations do not depend on LLM behavior.

The Ursina/Panda3D renderer will be shared HCS infrastructure. Engineering models and the Knowledge Tree each convert their own data into a renderer-neutral scene contract, preventing either subsystem from depending directly on renderer objects.

## Run tests

```bash
PYTHONPATH=structural-design pytest structural-design/tests -v
```
