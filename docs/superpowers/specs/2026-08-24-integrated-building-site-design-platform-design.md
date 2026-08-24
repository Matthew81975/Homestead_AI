# Integrated Building & Site Design Platform — Design Specification

**Date:** 2026-08-24

## Purpose

Build an HCS-integrated, open-source-oriented design platform that uses one parametric physical model for site and building geometry, then exposes that model to specialized engineering, systems, visualization, estimating, code-reference, and drawing engines.

The first working engineering discipline is structural analysis, beginning with mass walls (earthbag and rubble). The architecture must not trap later plumbing, electrical, drainage, mechanical, energy, CAD, or blueprint work inside the structural subsystem.

## Core architectural rule

There is one canonical project model. Walls, openings, foundations, roofs, equipment, pipes, circuits, drains, and site objects are represented once and referenced by discipline-specific analyses. Derived results never become competing copies of geometry.

A change to canonical geometry invalidates affected derived calculations and drawings.

## Units

User-facing quantities accept US customary and metric units. The computational core normalizes physical quantities to SI before calculation. Outputs may be rendered in either unit system without changing stored canonical values.

## Major subsystems

- **geometry** — canonical site/building geometry, spatial identifiers, openings, assemblies, relationships.
- **site** — grading, soil, surface/subsurface drainage, water flow, utility routes.
- **structural** — loads, foundations, frames/trusses, tunnels, mass walls, connections, utilization and failure modes.
- **plumbing** — potable/cold/hot water, DWV, rainwater/cistern systems, pumps and fixtures.
- **electrical** — service, panels, circuits, conductors/conduit, lighting, receptacles, grounding, low-voltage, PV/battery/microgrid integration.
- **mechanical** — HVAC/ventilation and later geothermal/thermal systems.
- **visualization** — interactive 3D engineering viewer using Ursina initially, with Panda3D available beneath it.
- **coordination** — cross-discipline collision, clearance, penetration and routing checks.
- **codes** — jurisdiction/version-aware references and traceable requirements.
- **materials/hardware/suppliers** — engineering properties, product identities, catalog links, provenance and price snapshots.
- **costing/BOM** — quantities, supplier selections, material cost estimates and later labor assumptions.
- **CAD/blueprints** — generated plans, elevations, sections, details, schedules and plotter-ready drawing sets.
- **validation** — hand checks, published-test comparison, physical-test calibration and uncertainty tracking.

## Renderer independence

The canonical model must not contain Ursina/Panda3D objects. Visualization consumes renderer-neutral geometry and metadata. This permits another renderer, CAD exporter, or web viewer later without rewriting engineering logic.

Initial viewer capabilities should include orbit/pan/zoom, object selection, discipline visibility toggles, cutaway/transparency support, and metadata inspection. Later overlays may show utilization, reactions, flow, voltage drop, drainage paths and exaggerated deformation.

## Structural analysis

Structural calculations expose assumptions, loads, reactions, capacities, utilization ratios, governing modes and uncertainty. Unknown engineering properties remain unknown unless the user deliberately selects a sourced reference value.

The platform should support increasingly detailed solvers. Transparent analytical checks are retained even when Frame3DD, CalculiX or another numerical solver is added.

### Mass-wall structural module

Mass walls are a structural family with assembly-specific models.

Initial assembly types:
- earthbag
- rubble-filled / rubble-core

Shared wall behavior includes geometry, openings, wall piers, buttresses, bond beams/lintels, foundations, vertical/lateral loads, sliding, overturning, bearing, slenderness and reduced-order export.

Earthbag-specific properties include bag/tube geometry, course geometry, compacted fill, inter-course friction, barbed wire/reinforcement, skins/plaster and confinement.

Rubble-specific properties include stone size distribution, packing/void ratio, fill density, confinement, mortar/binder state, skins/facings, ties/through-stones and drainage-sensitive behavior.

Level 1 uses a hybrid analytical model: hand-checkable wall-strip calculations plus a coarse load-path/frame representation around openings. Doors and windows are included from the first implementation.

## Drainage

Drainage is a top-level site/building subsystem, not a mass-wall feature. It eventually models grading, runoff, groundwater, infiltration, drains, gravel/geotextile, waterproofing interfaces, discharge/overflow destinations and hydrostatic effects. Drainage results can alter structural loads and foundation/soil assumptions.

## Plumbing and electrical

Plumbing and electrical share canonical geometry with structural and site systems. Routes and components must be spatially represented so coordination can identify conflicts such as a drain crossing a footing or a penetration intersecting a structural pier.

## Provenance and codes

Engineering values and rules should carry source metadata where available. The code system is jurisdiction- and edition-aware; it must not silently assume that the newest model code is locally adopted.

Supplier catalog data is distinct from engineering authority. A purchasable part may link to a supplier while its allowable-load or installation data cites manufacturer engineering documentation.

## Results and uncertainty

Every calculation result should be inspectable. Capacity checks use demand/capacity utilization where meaningful:

U = demand / capacity

Values above 1.0 indicate modeled demand exceeds modeled capacity. The system reports governing failure mode rather than only pass/fail.

Empirical, assumed, sourced and physically calibrated values are distinguishable. Physical testing can later calibrate mass-wall effective properties.

## Costing

Cost estimates derive from calculated quantities and supplier price snapshots when possible. Price data records supplier, part number, date observed and unit basis so stale prices can be identified.

## Output

The long-term project output can include:
- interactive 3D model
- engineering calculations and assumptions
- site/grading/drainage plans
- floor/roof/foundation/structural plans
- plumbing and electrical plans
- sections and connection details
- schedules
- BOM and supplier list
- cost estimate
- code-reference/compliance report
- plotter-ready construction drawings

## Initial implementation boundary

The first implementation slice establishes the renderer-independent shared model and units infrastructure, then implements Level 1 straight mass-wall geometry with rectangular openings and an analytical structural result API. Earthbag is the first assembly adapter; rubble uses the same shared wall interface immediately after.

Drainage, plumbing, electrical, mechanical and detailed 3D visualization are represented by stable extension boundaries in the shared model but are not fully engineered in the first slice.

## Testing strategy

- Unit tests for dimensional conversion and geometry.
- Analytical tests against independently calculated simple cases.
- Invariant tests: opening areas cannot exceed host wall bounds; negative dimensions/loads are rejected where physically invalid.
- Regression fixtures for representative earthbag and rubble walls.
- Later solver comparisons against Frame3DD/CalculiX and published/physical tests.
- Visualization tests verify renderer adapters do not mutate canonical model state.

## Safety and scope

The software is an engineering design and analysis aid. It must expose assumptions and uncertainty rather than imply that numerical output is automatically code-compliant or professionally stamped.
