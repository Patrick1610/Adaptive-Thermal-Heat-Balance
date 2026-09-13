# ATHB Build Clarifications

These clarifications are normative and supersede conflicting wording in `ATHB_ARCHITECTURE_PLAN.md`.

They do not otherwise reopen the architecture.

## Source-of-truth order

For implementation, use this order:

1. `ATHB_BUILD_CLARIFICATIONS.md`
2. `ATHB_ARCHITECTURE_PLAN.md`
3. `ATHB_WHITEPAPER.md`
4. `docs/reference/adaptive_climate_control_v3_patrick-3.yaml` as reference material only

Current authoritative Home Assistant source/documentation and the pinned scientific/numerical references remain verification sources. If an implementation detail demonstrably conflicts with a current API, the pinned ATHB numerical contract, a primary scientific source, or a safety invariant, document the conflict and implement the smallest correct change with regression tests.

---

## 1. HVAC mode changes are not automatically manual overrides

ATHB owns temperature targets only.

An externally initiated HVAC-mode change must therefore **not automatically create `MANUAL_OVERRIDE`**.

Required behavior:

- `heat -> off`: retain ATHB ownership intent, change target readiness to `SUSPENDED_MODE`, invalidate queued commands and stop writes.
- `off -> heat`: reassess target capabilities and reconcile before issuing a fresh ATHB target.
- `heat -> cool` or `cool -> heat`: reassess direction/capabilities, invalidate stale calculations/intents and reconcile.
- unsupported mode: suspend writes with the documented reason.

An external **temperature-target** change remains a manual ownership intervention and enters `MANUAL_OVERRIDE`.

Preset changes must be classified by their actual effect. A preset that externally changes or assumes ownership of the temperature target must inhibit ATHB appropriately. A harmless state-only preset change must not create an unnecessary long-lived manual override.

ATHB must never fight an external controller.

Update the ownership transition table, event handling, tests, diagnostics, Virtual Installation expectations and safety invariants wherever the architecture plan still treats every external HVAC-mode change as a manual override.

---

## 2. Root eligibility is directional

ATHB should attempt all five semantic roots whenever possible for observability:

1. lower comfort boundary;
2. heating control target;
3. thermal neutral;
4. cooling control target;
5. upper comfort boundary.

However, failure of an **irrelevant** root must not invalidate an otherwise valid directional control decision.

### Heating-only

Adaptive heating requires:

- a valid current ATHB evaluation;
- a valid heating-control root;
- all other mandatory environmental, policy, ownership and target inputs.

Failure of the lower comfort, thermal-neutral, cooling-control or upper-comfort root may make those diagnostic outputs unavailable, but must not by itself block heating.

### Cooling-only

Adaptive cooling requires:

- a valid current ATHB evaluation;
- a valid cooling-control root;
- all other mandatory environmental, policy, ownership and target inputs.

Failure of unrelated heating-side, neutral or outer roots must not by itself block cooling.

### Ranged heat/cool

Adaptive ranged control requires:

- a valid heating-control root;
- a valid cooling-control root;
- correct ordering;
- the required minimum range separation after all relevant transformations/normalization.

Outer comfort and neutral roots remain diagnostic where available.

### Diagnostics

Every attempted root retains its own success or typed failure. Never fabricate a missing root.

This specifically changes hot/humid scenarios where constant-vapor-pressure solving can make colder heating/neutral roots moisture-limited while a valid cooling-control root still exists. A cooling-only actuator must be allowed to use that valid cooling root.

Revise `VI-016` and any related tests/Definition-of-Done wording accordingly. Do not keep the old invariant that every actuator requires one complete five-root result.

---

## 3. Comfort-strategy changes are lightweight runtime changes

`Efficient`, `Balanced` and `Comfort` are normal operating strategies.

Changing the strategy through the ATHB strategy select must **not** require a full config-entry unload/reload.

A runtime strategy change must:

1. persist the authoritative strategy value;
2. increment/invalidate the relevant configuration/calculation generation;
3. cancel or invalidate obsolete calculations and queued intents;
4. calculate the new strategy control roots;
5. publish the new policy result;
6. pass any changed actuator target through the normal command broker.

The following remain active:

- environmental listeners;
- shared outdoor-history collectors;
- target leases;
- unrelated runtime state.

Options-flow changes that genuinely require reload may still use normal Home Assistant reload semantics, but the standard strategy `select` is deliberately lightweight.

The implementation must avoid maintaining two competing authoritative strategy stores. The strategy select and options/configuration representation must resolve to one authoritative persisted value.

---

## 4. Radiant and cold-surface configuration uses simple routes

MRT, cold-surface modelling and critical local-air locations are **optional quality improvements**, not prerequisites for ordinary ATHB use.

### Default user path

The normal default is:

`Uniform radiant environment`

Under this mode, MRT is explicitly estimated from room air temperature according to the architecture plan.

A normal user should therefore be able to configure ATHB without understanding:

- MRT;
- view factors;
- critical points;
- globe thermometers;
- surface modelling.

### Optional Mold Indicator path

The ordinary user-facing room-model choice contains only:

- **Standard**: the uniform radiant environment; and
- **Mold Indicator**: select an existing Home Assistant Mold Indicator and read its
  `estimated_critical_temp` attribute directly.

The Mold Indicator critical point is used only for surface-temperature, surface-RH and saturation
diagnostics. It is not treated as room MRT and does not change comfort roots without an actual
occupant-weighted radiant measurement. The wizard asks for no view factor, direct MRT, globe or
manually modelled surface values. Those numerical primitives may remain internally tested but are
not normal configuration choices.

A calibrated/modelled surface is never treated as local air or direct MRT.

### Home Assistant Mold Indicator-style calibration

A user with an existing Home Assistant Mold Indicator selects that entity directly. ATHB consumes
the integration's calculated critical-point attribute, so no template sensor, duplicated
calibration input or helper solely for ATHB is required.

### Everyday controls and HVAC Auto

Boost shift, Boost duration, minimum and maximum command temperature, and manual-override duration
belong to the ordinary Setup, Reconfigure and Options path rather than Advanced.

The user does not declare an Auto mapping. ATHB infers only semantics that public climate
capabilities make unambiguous: range support, heating-only scalar, or cooling-only scalar. An
ambiguous scalar `auto` target remains suppressed with `unsupported_auto_mapping`. ATHB does not
infer direction from room temperature or `hvac_action` and never changes HVAC mode.

---

## 5. Scope remains repository-validated software

These clarifications do not introduce any live Home Assistant or physical-device acceptance phase.

Software completion remains proven through the repository-defined numerical, policy/state-machine, Virtual Installation Validation, Home Assistant API-contract, storage/race, packaging and quality suites.

No build task depends on access to the repository owner's Home Assistant installation or heating equipment.

---

## 6. Comfort level, occupancy setback and Boost are separate controls

This section replaces the three-strategy and `auto`/`comfort`/`eco`/`boost` profile wording in
the architecture plan. There is no public Profile control.

The one public comfort-level select is ordered from lowest expected conditioning demand to
greatest comfort. Its fixed inward fractions and default-boundary votes are:

| Comfort level | Stable key | Inward fraction | Heating vote | Cooling vote |
|---|---|---:|---:|---:|
| Eco | `eco` | 0.10 | -0.45 | +0.45 |
| Efficient | `efficient` | 0.30 | -0.35 | +0.35 |
| Balanced | `balanced` | 0.50 | -0.25 | +0.25 |
| Comfort | `comfort` | 0.70 | -0.15 | +0.15 |
| Near neutral | `near_neutral` | 0.85 | -0.075 | +0.075 |

Balanced remains the default. Every vote is calculated from the configured outer comfort
boundary and independently inverse-solved in ATHB sensation space. No temperature interpolation
or user-editable fraction is introduced.

An optional Home Assistant binary occupancy or schedule entity controls setback at every comfort
level. `on` means no setback; `off` applies the selected Setback; no configured source means no
setback. For `unknown` or unavailable input, retain the last resolved state for 30 minutes and
then assume occupied/no setback with `occupancy_unknown`. The Setback configuration and entity are
shown only when an occupancy source is configured. The choices remain ordered Max, Eco - 4 °C,
Comfort - 2 °C, and Custom.

Boost is a separate temporary select with `off`, `adaptive`, and `rapid`:

- `off`: comfort level and occupancy setback are authoritative;
- `adaptive`: bypass occupancy setback and request the solved directional target shifted by
  `boost_delta_c` toward, but not past, thermal neutral;
- `rapid`: for a scalar heating target, request the configured maximum command temperature until
  the adaptive Boost target is reached, then hold that Boost target; scalar cooling uses the
  configured minimum analogously. For an atomic ranged target or a zone with separate heating
  and cooling scalar targets, Rapid explicitly falls back to Adaptive because opposing
  maximum-drive requests cannot be expressed safely.

Boost expiry is persisted and returns the select to `off`; reselecting an active non-off mode
restarts the configured duration. Restart never extends an existing expiry. Boost does not change
the selected comfort level, never changes HVAC mode, and remains subject to ownership,
normalization, user/device bounds and the sole `CommandBroker` write path.

Config-entry version 1 migrates to version 2. Legacy `eco` Profile maps to the Eco comfort level;
legacy `boost` maps to Adaptive Boost; other legacy Profiles preserve the selected comfort level
and Boost Off. The obsolete Profile entity is removed.

---

## 7. Stale measurements remain observable and can only trigger de-escalation

This section narrows the architecture's blanket wording that stale primary input always permits
zero writes. It does not permit adaptive calculation from stale input.

When a previously valid mandatory measurement becomes stale, ATHB retains the last valid
calculated values for display only. Every retained value is explicitly labelled stale, exposes the
last-valid timestamp and age, and the input-status entity reports the actual failure. Retention is
not a fabricated observation, a fresh calculation, or evidence that the physical state is
unchanged. Normal adaptive calculation and normal climate writes remain inhibited.

Recalculation caused by another source may reuse an already accepted, still-fresh observation with
the same value and timestamp. This does not refresh its age or advance recovery counters. Older
timestamps and conflicting values at the same timestamp remain invalid.

If the primary room-temperature report has been stale or otherwise invalid for at least one hour
and the newest trustworthy timestamped temperature evidence still indicates demand against the
currently observed climate target, ATHB may issue one safety de-escalation per target. During a
reload, a still-present numerically valid Home Assistant sensor state and its original report
timestamp may supply this evidence; it is never promoted to a fresh adaptive input.

- heating-only: lower the target to the configured fallback heating temperature;
- cooling-only: raise the target to the configured fallback cooling temperature;
- atomic range: widen the active demand side(s) toward the two configured fallback temperatures.

The safety action is permitted only when it strictly reduces existing demand. It may never create
or increase heating or cooling demand. It remains subject to current ownership, target support and
availability, lease identity, entry/input/capability/ownership generations, persistence-before-
dispatch, command acknowledgement, device/user bounds and grid normalization. It goes through the
sole `CommandBroker` and contains temperature fields only; ATHB still never changes HVAC mode.
Manual override, disabled control, unavailable targets and lost leases remain fail-closed.

The action is one-shot for the stale episode. A fully valid recovered calculation clears the
safety state and resumes the ordinary event-driven path. The configured fallback temperatures are
therefore always retained and validated, even when insufficient outdoor history is configured as
`no_write`; that choice controls history fallback only.
