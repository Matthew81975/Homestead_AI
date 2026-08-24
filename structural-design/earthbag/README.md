# Earthbag Wall Modeler

A dedicated HCS structural-design subsystem for converting an earthbag wall assembly into engineering properties and a reduced-order wall model suitable for whole-building analysis.

## Initial scope

Inputs:
- Wall length, height, curvature, and openings
- Bag/tube dimensions and course geometry
- Fill density and material properties
- Compaction assumptions
- Inter-course friction
- Barbed wire/reinforcement configuration
- Buttresses
- Bond beam
- Foundation/restraint assumptions
- Plaster or structural skins

Outputs:
- Wall mass and material quantities
- Effective density
- Axial/compressive capacity
- Shear/sliding capacity
- Effective elastic and shear stiffness
- Bending stiffness
- Slenderness indicators
- Overturning resistance
- Approximate lateral-load capacity
- Governing utilization/failure mode
- Reduced-order properties for the parent structural solver
- Assumptions, provenance, and confidence flags

## Architecture

The modeler is intentionally separate from the whole-building solver:

earthbag assembly -> Earthbag Wall Modeler -> equivalent/reduced-order wall element -> whole-building analysis

The implementation will support increasing fidelity:
1. Preliminary analytical model
2. Course/section model
3. Detailed nonlinear/contact model

Published or experimentally measured material properties must retain source/provenance information. Physical test data from the actual construction system can later be used to calibrate model parameters.

## Safety/validation

Engineering outputs must expose assumptions and uncertainty rather than silently treating poorly characterized earthbag properties as exact. Models should be validated against hand calculations, published tests, and eventually physical wall tests before relying on higher-fidelity predictions.
