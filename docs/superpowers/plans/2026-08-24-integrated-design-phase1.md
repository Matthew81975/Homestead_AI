# Integrated Design Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first working, testable kernel of the integrated building/site design platform: unit normalization, canonical wall/opening geometry, mass-wall material assemblies, and transparent Level-1 structural calculations.

**Architecture:** Keep the engineering kernel independent of HCS UI and independent of Ursina/Panda3D. Canonical dataclasses describe geometry/material/load state; pure calculation functions consume those objects and return inspectable result objects. Earthbag and rubble-specific behavior plug into a shared mass-wall interface.

**Tech Stack:** Python 3.11+, standard library first, pytest for tests; later adapters may use Pint, Ursina/Panda3D, Frame3DD, FreeCAD and CalculiX.

**Spec:** `docs/superpowers/specs/2026-08-24-integrated-building-site-design-platform-design.md`

## Global Constraints

- User-facing quantities accept US customary and metric units; internal calculations use SI.
- Canonical model contains no renderer-specific objects.
- Unknown engineering properties remain unknown rather than silently defaulting.
- Rectangular door/window openings are supported in the first mass-wall slice.
- Calculation results expose assumptions, demand, capacity, utilization and governing mode.
- Initial code remains isolated from the existing HCS runtime until the API is validated.

---

### Task 1: Unit normalization kernel

**Files:**
- Create: `structural-design/design_platform/__init__.py`
- Create: `structural-design/design_platform/units.py`
- Test: `structural-design/tests/test_units.py`

**Interfaces:**
- Produces: `Quantity(value: float, unit: str)`, `to_si(quantity, dimension) -> float`, `from_si(value, unit, dimension) -> float`.

- [ ] Write failing tests for feet/inches/meters, pounds-force/newtons, pcf/kg-m3 and psf/pascals.
- [ ] Run tests and confirm missing-module/function failures.
- [ ] Implement explicit conversion tables and dimensional validation using the standard library.
- [ ] Run unit tests and full structural-design tests.
- [ ] Commit.

### Task 2: Canonical wall geometry and openings

**Files:**
- Create: `structural-design/design_platform/geometry.py`
- Test: `structural-design/tests/test_geometry.py`

**Interfaces:**
- Consumes: SI floats from `units.py`.
- Produces: `RectOpening`, `StraightWallGeometry`; properties `gross_area`, `opening_area`, `net_area`, `pier_segments()`.

- [ ] Write failing tests for wall area, opening subtraction, opening bounds and pier segmentation.
- [ ] Verify failures.
- [ ] Implement immutable dataclasses with validation; openings may not overlap or extend outside the wall.
- [ ] Run tests.
- [ ] Commit.

### Task 3: Mass-wall assembly models

**Files:**
- Create: `structural-design/design_platform/mass_walls.py`
- Test: `structural-design/tests/test_mass_walls.py`

**Interfaces:**
- Produces: `EngineeringValue`, `EarthbagAssembly`, `RubbleAssembly`, each exposing optional density, compressive strength, shear strength, friction coefficient and provenance.

- [ ] Write failing tests showing sourced values are preserved and missing strengths remain `None`.
- [ ] Verify failures.
- [ ] Implement shared validation and earthbag/rubble assembly dataclasses.
- [ ] Run tests.
- [ ] Commit.

### Task 4: Loads and Level-1 checks

**Files:**
- Create: `structural-design/design_platform/loads.py`
- Create: `structural-design/design_platform/analysis.py`
- Create: `structural-design/design_platform/results.py`
- Test: `structural-design/tests/test_analysis.py`

**Interfaces:**
- Produces: `WallLoads`, `CheckResult`, `WallAnalysisResult`, `analyze_straight_mass_wall(...)`.
- Checks: self-weight, axial compression when strength exists, base sliding when friction exists, overturning/eccentricity, and per-pier axial demand around openings.

- [ ] Write failing tests for a hand-calculated solid wall and a wall with one opening.
- [ ] Verify failures.
- [ ] Implement minimal transparent formulas with assumptions recorded in results.
- [ ] Verify utilization is `demand/capacity` and unavailable checks are explicitly `not_evaluated`.
- [ ] Run all tests.
- [ ] Commit.

### Task 5: Public API and example

**Files:**
- Modify: `structural-design/design_platform/__init__.py`
- Create: `structural-design/examples/basic_mass_wall.py`
- Modify: `structural-design/README.md`
- Test: `structural-design/tests/test_public_api.py`

**Interfaces:**
- Produces a small stable public API suitable for future HCS, visualization and solver adapters.

- [ ] Write a failing import/API test.
- [ ] Verify failure.
- [ ] Export stable public symbols and add a runnable metric/US-unit example.
- [ ] Document current limitations and future adapter boundaries.
- [ ] Run all tests.
- [ ] Commit.

### Task 6: Verification gate

**Files:** No production changes unless verification finds a defect.

- [ ] Run `pytest structural-design/tests -v`.
- [ ] Run Python compile/import checks over `structural-design/design_platform`.
- [ ] Review equations and test fixtures against independent hand calculations.
- [ ] Confirm no renderer/HCS imports leaked into the kernel.
- [ ] Record verification results in the final implementation summary.
