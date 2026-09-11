# ATHB Virtual Installation Validation

All 30 mandatory scenarios passed.

## VI-001 — Dutch living room / Balanced

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-002 — Same room / Efficient

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_E`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 24.297205249711876}, "heating_control": {"status": "success", "value_c": 18.532199748411774}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `efficient` / `comfort`; fallback: `False`
- Requested heating/cooling: `18.532199748411774` / `None` °C
- Normalized target: `{"bounded_c": 18.532199748411774, "calibrated_c": 18.532199748411774, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.0, "normalized_ha": 19.0, "normalized_room_c": 19.0, "requested_room_c": 18.532199748411774}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-003 — Same room / Comfort

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_C`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 22.661849102303385}, "heating_control": {"status": "success", "value_c": 20.190060796931384}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `comfort` / `comfort`; fallback: `False`
- Requested heating/cooling: `20.190060796931384` / `None` °C
- Normalized target: `{"bounded_c": 20.190060796931384, "calibrated_c": 20.190060796931384, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 20.5, "normalized_ha": 20.5, "normalized_room_c": 20.5, "requested_room_c": 20.190060796931384}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 20.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-004 — Room around Balanced target

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_NEAR_B`; current sensation: `-0.23344521023117604`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479335407027047}, "heating_control": {"status": "success", "value_c": 19.360890697506253}, "lower_comfort": {"status": "success", "value_c": 17.28291404078994}, "thermal_neutral": {"status": "success", "value_c": 21.427614773049022}, "upper_comfort": {"status": "success", "value_c": 25.519803459831518}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.360890697506253` / `None` °C
- Normalized target: `{"bounded_c": 19.360890697506253, "calibrated_c": 19.360890697506253, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.360890697506253}`
- Command(s): `[]`
- Outcome: `target_unchanged` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-005 — Room below comfort band

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BELOW_B`; current sensation: `-0.65306988911957`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.47968305239827}, "heating_control": {"status": "success", "value_c": 19.361325007565316}, "lower_comfort": {"status": "success", "value_c": 17.283392077840865}, "thermal_neutral": {"status": "success", "value_c": 21.428005592904988}, "upper_comfort": {"status": "success", "value_c": 25.52010816750676}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361325007565316` / `None` °C
- Normalized target: `{"bounded_c": 19.361325007565316, "calibrated_c": 19.361325007565316, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361325007565316}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-006 — Cold critical-air location

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `20.861120950075797` / `None` °C
- Normalized target: `{"bounded_c": 20.861120950075797, "calibrated_c": 20.861120950075797, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 21.0, "normalized_ha": 21.0, "normalized_room_c": 21.0, "requested_room_c": 20.861120950075797}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 21.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-007 — Pathological location and influence cap

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `21.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 21.361130272671577, "calibrated_c": 21.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 21.5, "normalized_ha": 21.5, "normalized_room_c": 21.5, "requested_room_c": 21.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 21.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-008 — Cold measured surface

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_SURFACE16_B`; current sensation: `-0.22847971463520508`
- Roots: `{"cooling_control": {"status": "success", "value_c": 24.454739557489756}, "heating_control": {"status": "success", "value_c": 19.799975844338537}, "lower_comfort": {"status": "success", "value_c": 17.448213678225873}, "thermal_neutral": {"status": "success", "value_c": 22.132983926191923}, "upper_comfort": {"status": "success", "value_c": 26.75774110452831}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.799975844338537` / `None` °C
- Normalized target: `{"bounded_c": 19.799975844338537, "calibrated_c": 19.799975844338537, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 20.0, "normalized_ha": 20.0, "normalized_room_c": 20.0, "requested_room_c": 19.799975844338537}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 20.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-009 — Internally modelled surface

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_SURFACE14_B`; current sensation: `-0.25533981643687614`
- Roots: `{"cooling_control": {"status": "success", "value_c": 24.698542652860286}, "heating_control": {"status": "success", "value_c": 20.05128057341277}, "lower_comfort": {"status": "success", "value_c": 17.699518407300115}, "thermal_neutral": {"status": "success", "value_c": 22.384288655266168}, "upper_comfort": {"status": "success", "value_c": 27.00154419989884}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `20.05128057341277` / `None` °C
- Normalized target: `{"bounded_c": 20.05128057341277, "calibrated_c": 20.05128057341277, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 20.5, "normalized_ha": 20.5, "normalized_room_c": 20.5, "requested_room_c": 20.05128057341277}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 20.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-010 — Direct fixed RH declaration

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `declared`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-011 — Missing optional MRT

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-012 — Invalid mandatory air

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `None`; current sensation: `None`
- Roots: `{"cooling_control": {"status": "unavailable", "value_c": null}, "heating_control": {"status": "unavailable", "value_c": null}, "lower_comfort": {"status": "unavailable", "value_c": null}, "thermal_neutral": {"status": "unavailable", "value_c": null}, "upper_comfort": {"status": "unavailable", "value_c": null}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `None` / `None` °C
- Normalized target: `null`
- Command(s): `[]`
- Outcome: `primary_temperature_invalid` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `["missing", "unavailable", "nonfinite", "boolean", "malformed", "stale"]`
- Result: **PASS**

## VI-013 — Invalid or missing RH

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `None`; current sensation: `None`
- Roots: `{"cooling_control": {"status": "unavailable", "value_c": null}, "heating_control": {"status": "unavailable", "value_c": null}, "lower_comfort": {"status": "unavailable", "value_c": null}, "thermal_neutral": {"status": "unavailable", "value_c": null}, "upper_comfort": {"status": "unavailable", "value_c": null}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `None` / `None` °C
- Normalized target: `null`
- Command(s): `[]`
- Outcome: `primary_rh_invalid` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `["missing", "unavailable", "nonfinite", "boolean", "malformed", "stale"]`
- Result: **PASS**

## VI-014 — Partial and insufficient history

- History: `partial_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["three_day_adaptive", "two_day_fixed_fallback"]`
- Result: **PASS**

## VI-015 — Cold-winter extrapolation

- History: `complete_history`; running mean: `-10.0` °C
- RH provenance: `measured`
- Numerical golden: `G_WINTER_B`; current sensation: `0.20671480116953517`
- Roots: `{"cooling_control": {"status": "success", "value_c": 20.381352456375957}, "heating_control": {"status": "success", "value_c": 15.94413612063229}, "lower_comfort": {"status": "success", "value_c": 13.701147643223404}, "thermal_neutral": {"status": "success", "value_c": 18.17212133063376}, "upper_comfort": {"status": "success", "value_c": 22.579331131562594}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `15.94413612063229` / `None` °C
- Normalized target: `{"bounded_c": 18.0, "calibrated_c": 15.94413612063229, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["user_bound_applied"], "normalized_actuator_c": 18.0, "normalized_ha": 18.0, "normalized_room_c": 18.0, "requested_room_c": 15.94413612063229}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 18.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["adaptive_extrapolation_allowed", "rejected_immediate_hold_no_write", "rejected_after_15m_fixed_fallback_heat_18"]`
- Result: **PASS**

## VI-016 — Humid warm room and saturation

- History: `complete_history`; running mean: `24.999999999999996` °C
- RH provenance: `measured`
- Numerical golden: `G_HUMID_B`; current sensation: `0.5639479489801329`
- Roots: `{"cooling_control": {"status": "success", "value_c": 25.6606139216423}, "heating_control": {"status": "moisture_limited_no_solution", "value_c": null}, "lower_comfort": {"status": "moisture_limited_no_solution", "value_c": null}, "thermal_neutral": {"status": "moisture_limited_no_solution", "value_c": null}, "upper_comfort": {"status": "success", "value_c": 27.52452230405808}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `None` / `25.6606139216423` °C
- Normalized target: `{"bounded_c": 25.6606139216423, "calibrated_c": 25.6606139216423, "direction": "cooling_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 30.0, "upper_index": 28}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 25.5, "normalized_ha": 25.5, "normalized_room_c": 25.5, "requested_room_c": 25.6606139216423}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 25.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["cooling_directional_root", "ranged_missing_heating_endpoint"]`
- Result: **PASS**

## VI-017 — Cooling-only strategy targets

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `None` / `23.479527176007633` °C
- Normalized target: `{"bounded_c": 23.479527176007633, "calibrated_c": 23.479527176007633, "direction": "cooling_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 23.0, "normalized_ha": 23.0, "normalized_room_c": 23.0, "requested_room_c": 23.479527176007633}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 23.0}, {"entity_id": "climate.living_room", "temperature": 24.0}, {"entity_id": "climate.living_room", "temperature": 22.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-018 — Ranged heat cool

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `23.479527176007633` °C
- Normalized target: `{"cooling": {"bounded_c": 23.479527176007633, "calibrated_c": 23.479527176007633, "direction": "cooling_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 23.0, "normalized_ha": 23.0, "normalized_room_c": 23.0, "requested_room_c": 23.479527176007633}, "heating": {"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}, "limitations": ["grid_inward_adjustment"], "requested_cooling_room_c": 23.479527176007633, "requested_heating_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "target_temp_high": 23.0, "target_temp_low": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-019 — Explicit mapped auto range

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `23.479527176007633` °C
- Normalized target: `{"cooling": {"bounded_c": 23.479527176007633, "calibrated_c": 23.479527176007633, "direction": "cooling_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 23.0, "normalized_ha": 23.0, "normalized_room_c": 23.0, "requested_room_c": 23.479527176007633}, "heating": {"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}, "limitations": ["grid_inward_adjustment"], "requested_cooling_room_c": 23.479527176007633, "requested_heating_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "target_temp_high": 23.0, "target_temp_low": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-020 — Unsupported auto semantics

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `unsupported_auto_mapping` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-021 — Climate off

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `hvac_off` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-022 — Fahrenheit and one conversion

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 64.4, "lower_index": 5, "origin_ha": 60.0, "step_ha": 1.0, "upper_bound_ha": 78.8, "upper_index": 18}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.444444444444443, "normalized_ha": 67.0, "normalized_room_c": 19.444444444444443, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 67.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-023 — Coarse-grid inward normalization

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 1, "origin_ha": 16.0, "step_ha": 2.0, "upper_bound_ha": 26.0, "upper_index": 5}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 20.0, "normalized_ha": 20.0, "normalized_room_c": 20.0, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 20.0}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["heat_step_2", "cool_step_2", "range_step_2", "range_step_4_infeasible"]`
- Result: **PASS**

## VI-024 — Manual external setpoint change

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `manual_override` / `not_applicable`
- Ending ownership: `manual_override`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-025 — ATHB-originated acknowledgement

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["own_context_before_return", "contextless_exact_pending"]`
- Result: **PASS**

## VI-026 — Obsolete delayed acknowledgement

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `manual_override` / `not_applicable`
- Ending ownership: `manual_override`
- Mandatory variants: `[]`
- Result: **PASS**

## VI-027 — Restart recovery simulation

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `reconciliation_required` / `not_applicable`
- Ending ownership: `reconciling`
- Mandatory variants: `["clean_recompute", "unclean_reconcile", "explicit_resume_new_identity"]`
- Result: **PASS**

## VI-028 — Multiple actuators and conflicts

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[]`
- Outcome: `cross_actuator_conflict` / `not_applicable`
- Ending ownership: `owned`
- Mandatory variants: `["normal_two_actuator", "manual_opposing_conflict"]`
- Result: **PASS**

## VI-029 — Zones sharing outdoor source

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_B`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 23.479527176007633}, "heating_control": {"status": "success", "value_c": 19.361130272671577}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `balanced` / `comfort`; fallback: `False`
- Requested heating/cooling: `19.361130272671577` / `None` °C
- Normalized target: `{"bounded_c": 19.361130272671577, "calibrated_c": 19.361130272671577, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 19.5, "normalized_ha": 19.5, "normalized_room_c": 19.5, "requested_room_c": 19.361130272671577}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 19.5}, {"entity_id": "climate.second_zone", "temperature": 19.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["two_zone_commands", "forty_zone_eight_location_shared_source"]`
- Result: **PASS**

## VI-030 — Event storm and stale work

- History: `complete_history`; running mean: `5.0` °C
- RH provenance: `measured`
- Numerical golden: `G_BASE_C`; current sensation: `-0.17308845704988404`
- Roots: `{"cooling_control": {"status": "success", "value_c": 22.661849102303385}, "heating_control": {"status": "success", "value_c": 20.190060796931384}, "lower_comfort": {"status": "success", "value_c": 17.283177736744285}, "thermal_neutral": {"status": "success", "value_c": 21.427830358043316}, "upper_comfort": {"status": "success", "value_c": 25.519971543416386}}`
- Strategy/profile: `comfort` / `comfort`; fallback: `False`
- Requested heating/cooling: `20.190060796931384` / `None` °C
- Normalized target: `{"bounded_c": 20.190060796931384, "calibrated_c": 20.190060796931384, "direction": "heating_only", "grid": {"assumed_step": false, "lower_bound_ha": 18.0, "lower_index": 4, "origin_ha": 16.0, "step_ha": 0.5, "upper_bound_ha": 26.0, "upper_index": 20}, "limitations": ["grid_inward_adjustment"], "normalized_actuator_c": 20.5, "normalized_ha": 20.5, "normalized_room_c": 20.5, "requested_room_c": 20.190060796931384}`
- Command(s): `[{"entity_id": "climate.living_room", "temperature": 20.5}]`
- Outcome: `own_context_match` / `acknowledged`
- Ending ownership: `owned`
- Mandatory variants: `["ten_thousand_events", "latest_generation_only"]`
- Result: **PASS**
