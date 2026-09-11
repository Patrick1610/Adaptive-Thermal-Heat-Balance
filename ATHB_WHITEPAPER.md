# Adaptive Thermal Heat Balance (ATHB)
## Whitepaper and production specification for Home Assistant

**Status:** Design baseline for initial production implementation  
**Product name:** **Adaptive Thermal Heat Balance (ATHB)**  
**Proposed Home Assistant domain:** `athb`  
**Primary use:** Adaptive, comfort-driven control of Home Assistant `climate` entities  
**Scientific model:** Adaptive Thermal Heat Balance (`ATHB_PMV`) only  

---

## 1. Executive summary

Adaptive Thermal Heat Balance (ATHB) is a Home Assistant custom integration that converts environmental measurements into a dynamically calculated thermal-comfort range and then applies that result to one or more Home Assistant `climate` entities.

The integration is intentionally **not** an ASHRAE 55 compliance integration and must not be presented as one. Its scientific basis is the Adaptive Thermal Heat Balance framework described by Schweiker and implemented in the open-source `pythermalcomfort` project as `pmv_athb`.

The core design principle is:

> **ATHB calculates human thermal sensation from the actual thermal environment, solves the inverse problem to determine the air-temperature range that produces the desired sensation, and only then translates that range into safe, capability-aware Home Assistant climate commands.**

The integration must account for, at minimum:

- representative indoor air temperature;
- relative humidity;
- adaptive outdoor exposure through running-mean outdoor temperature;
- mean radiant temperature when available, with a documented fallback when it is not;
- measured/fixed air speed converted to relative air speed as required by ATHB;
- metabolic rate;
- automatic ATHB clothing adaptation or an explicitly configured fixed clothing level;
- optional local cold/critical air-temperature points without falsely treating those sensors as mean radiant temperature;
- occupancy or demand state;
- comfort, eco/setback and boost policy;
- user hard temperature limits;
- Home Assistant climate capabilities, modes, temperature units, limits and setpoint precision;
- stale, unavailable or implausible sensor data;
- restarts, first-start outdoor-history bootstrap and command retry/recovery;
- external/manual changes to controlled climate entities.

The initial production release is **not a read-only MVP**. It must be capable of replacing an existing adaptive-heating automation and taking ownership of heating setpoints immediately after configuration and acceptance testing.

---

## 2. Background and reason for replacement

The legacy implementation is a Home Assistant automation blueprint named `ASHRAE 55 Adaptive Climate Control v3 - Patrick`. It combines a comfort formula, humidity offsets, an operative-temperature approximation, occupancy/setback/boost policy, Home Assistant climate capability detection, fan-mode behavior and direct service calls in one large YAML/Jinja state machine.

Important characteristics of that baseline include:

- direct use of current outdoor temperature rather than a running mean;
- a custom adaptive formula;
- custom relative-humidity temperature offsets;
- a `mean_radiant_temp_sensor` that is currently populated with a critical/cold-point temperature sensor;
- absolute min/max comfort-temperature caps;
- occupancy-based setback;
- boost input;
- climate mode and setpoint manipulation;
- periodic ten-minute triggers;
- fallback values such as 20 °C or 50% RH when data parsing fails;
- hard-coded fan-mode names;
- separate write-outs to helper entities.

The replacement should preserve the useful policy concepts, but not the architecture or unsupported physical assumptions.

### 2.1 Problems to solve

1. **Scientific ambiguity** — the current formula is described as ASHRAE 55 while mixing concepts that do not form a pure ASHRAE Adaptive implementation.
2. **Outdoor instability** — current outdoor temperature can move quickly over the day and is not a good representation of human thermal adaptation.
3. **Humidity handling** — fixed temperature offsets for RH are an approximation rather than a heat-balance calculation.
4. **Radiant handling** — a cold local air sensor is not automatically mean radiant temperature.
5. **Control complexity** — model, policy and Home Assistant actuation are interwoven.
6. **Weak failure semantics** — failed inputs can silently become plausible fictitious values.
7. **Actuator assumptions** — climate modes/features/fan strings are not universal.
8. **Polling-heavy behavior** — repeated time-pattern execution is less precise than event-driven recomputation.
9. **No explicit ownership model** — external/manual setpoint changes can conflict with automation writes.

---

## 3. Product goals

### 3.1 Primary goals

ATHB must:

1. Implement **one thermal comfort model only: ATHB**.
2. Produce a scientifically interpretable thermal-sensation value.
3. Derive a dynamic lower, neutral and upper comfort air-temperature solution via an inverse solver.
4. Use recent outdoor climate history rather than instantaneous outdoor temperature for adaptation.
5. Include relative humidity directly in the heat-balance calculation.
6. Include mean radiant temperature directly where a valid MRT source exists.
7. Support one or more critical local air-temperature points as separate ATHB comfort locations.
8. Apply comfort policy for occupied/active, eco/setback and boost states.
9. Directly control configured Home Assistant `climate` entities.
10. Work correctly with all Home Assistant HVAC modes without assuming that `auto` means `heat_cool`.
11. Discover and respect device capabilities, min/max temperatures, temperature units and target steps.
12. Be stable under noisy sensors, rapid source updates, Home Assistant restarts and temporary source outages.
13. Provide transparent diagnostics explaining exactly why a target was chosen.
14. Use UI config/reconfigure/options flows rather than YAML as the primary configuration mechanism.
15. Be HACS-ready and structured to modern Home Assistant integration standards.

### 3.2 Production-readiness goal

The initial accepted release must be sufficiently complete to replace the legacy heating blueprint in normal daily use. “Version 1” therefore means **first production-capable release**, not a deliberately thin feature slice.

### 3.3 Non-goals

The initial product is not intended to:

- claim ASHRAE 55 compliance;
- implement ASHRAE Adaptive, PMV, SET, EN 16798 or any second runtime comfort model;
- become a PID/TPI/MPC valve-modulation controller;
- directly drive raw TRV valve position unless a future separate actuator backend explicitly adds that capability;
- create its own full scheduling system; Home Assistant schedule/binary entities can supply occupancy/demand state;
- automatically control humidifiers/dehumidifiers merely because RH is an ATHB input;
- invent fan mode names or force swing/preset states;
- conceal model extrapolation or data-quality limitations.

`pythermalcomfort` may be used as a **development and test oracle**. It should not automatically become a runtime dependency without an explicit dependency review.

---

## 4. Scientific basis: ATHB only

### 4.1 ATHB rationale

The Adaptive Thermal Heat Balance framework combines adaptive principles with a heat-balance model. The updated 2022 formulation was developed using 57,084 records from the ASHRAE Global Thermal Comfort Database and reported improved thermal-sensation prediction across different climates, building types and cooling strategies.

This aligns well with the intended residential-control use case because it retains physical indoor-environment inputs such as RH, radiant temperature and air velocity while adding adaptation to outdoor climate.

### 4.2 Canonical ATHB inputs

The canonical model interface shall be equivalent to:

```python
athb_pmv(
    tdb,             # dry-bulb air temperature, °C
    tr,              # mean radiant temperature, °C
    vr,              # relative air speed, m/s
    rh,              # relative humidity, %
    met,             # metabolic rate, met
    t_running_mean,  # running-mean outdoor temperature, °C
    clo=False,       # false => ATHB clothing adaptation
)
```

The production engine must reproduce the selected reference implementation to a documented numerical tolerance.

### 4.3 Model result

The primary model result is `athb_pmv`, a predicted thermal-sensation vote. For control purposes the integration interprets the scale continuously, with 0 representing thermal neutrality.

ATHB does **not** natively produce a thermostat setpoint or lower/upper air-temperature band. Those are derived by the integration's inverse solver.

### 4.4 No parallel runtime models

There shall be no ASHRAE Adaptive or conventional PMV “reference entity” in the runtime integration.

During development, test code may compare ATHB outputs with `pythermalcomfort` to prove correctness and may generate sanity matrices to detect absurd or unstable behavior. That validation infrastructure is not part of the user's entity model.

---

## 5. Input model and semantics

Each ATHB zone is a Home Assistant config entry/device representing one thermally controlled room or zone.

### 5.1 Required inputs

| Input | HA source | Meaning |
|---|---|---|
| Primary indoor air temperature | `sensor`, `input_number` | Representative occupied-zone dry-bulb temperature |
| Indoor relative humidity | `sensor`, `input_number` | Zone RH used directly by ATHB |
| Outdoor air temperature | `sensor`, `input_number`, `weather` | Source from which daily means and running mean are derived |
| Target climate(s) | one or more `climate` entities | Actuators receiving ATHB-derived setpoints |

For `weather` sources, use the documented temperature attribute and validate it explicitly.

### 5.2 Required human parameters

| Parameter | Default behavior | Notes |
|---|---|---|
| Metabolic rate (`met`) | configurable living-room preset around sedentary/light activity | Must also allow numeric custom value |
| Clothing (`clo`) | ATHB automatic clothing adaptation | User may opt into fixed `clo` |

Preset labels should be user-friendly; the stored model value must remain explicit and inspectable.

### 5.3 Air speed

ATHB expects **relative air speed (`vr`)**, not simply raw measured room air speed.

Configuration shall allow:

- fixed measured air speed;
- air-speed sensor;
- optional direct relative-air-speed mode for advanced users.

When the input represents measured ambient air speed, the engine must convert it to relative air speed using an implementation equivalent to `v_relative(v, met)`.

This avoids repeating the legacy mistake of treating a single numeric air-velocity value as an undocumented generic comfort offset.

### 5.4 Optional MRT source

A true mean-radiant-temperature source can be supplied directly.

If no valid MRT source exists, the default fallback is:

```text
tr = representative tdb
```

This is a **uniform radiant environment approximation**, not a measured MRT. Diagnostics must identify it as estimated.

### 5.5 Optional critical/local air-temperature sources

The integration shall allow one or more optional “critical comfort point” temperature entities.

These represent **local air temperature**, not MRT.

Examples:

- a sensor at a seating location near glazing;
- the coldest relevant occupied-zone sensor;
- an existing helper that selects the coldest meaningful room temperature.

A critical sensor placed in an unoccupied corner, on a window frame or directly above a radiator is not automatically suitable. Setup text must explain this.

### 5.6 Optional surface or globe temperature support

Advanced radiant modes may be included in the initial production release if implemented and tested cleanly:

1. **Globe temperature** → derive MRT from globe temperature, primary `tdb`, air speed, globe diameter and emissivity.
2. **Surface temperature + view factor** → estimate composite MRT through fourth-power radiative weighting.

These modes must be clearly labelled as derived/estimated MRT and must not be conflated with a local air sensor.

Direct MRT plus critical local air points remains the preferred architecture.

---

## 6. Outdoor adaptation and history

### 6.1 Running mean is a first-class data product

ATHB shall not use instantaneous outside temperature as `t_running_mean`.

The integration maintains daily outdoor-temperature means and computes a running mean from prior days.

Recommended initial policy:

- 7 previous complete calendar days;
- exponential weighting;
- default `alpha = 0.8`;
- `alpha` configurable within a validated safe range;
- newest completed day carries the greatest weight.

The implementation must be validated numerically against `pythermalcomfort.utilities.running_mean_outdoor_temperature` for the same inputs and alpha.

### 6.2 Daily mean calculation

Daily means must be **time-weighted**, not merely the arithmetic mean of incoming state-change events. Sensors with variable report frequency must not bias the daily mean.

The accumulator should integrate the previous valid value over elapsed time, subject to a maximum gap policy. Missing periods reduce coverage and are reported.

### 6.3 Shared outdoor history

If multiple ATHB zones use the same outdoor entity, daily history must be maintained once and shared rather than duplicated independently per room.

Suggested abstraction:

```text
OutdoorHistoryRegistry
└── OutdoorHistorySource(entity_id)
    ├── current_day accumulator
    ├── persisted daily means
    ├── coverage metadata
    └── running mean
```

### 6.4 Startup/bootstrap

A production integration cannot require seven days of runtime before becoming useful.

Bootstrap order:

1. Load ATHB's own persisted daily history.
2. If insufficient, attempt to backfill from Home Assistant Recorder/history/statistics using supported APIs.
3. If some but not all days are available, compute from available valid days and mark the result `degraded_history`.
4. If no usable history exists, use an explicit startup fallback policy rather than silently pretending the current outdoor temperature is a complete running mean.

The default startup fallback should retain safe heating behavior. A practical approach is a user-configured fixed fallback comfort temperature until sufficient outdoor history becomes available.

### 6.5 Persistence

Persist versioned outdoor daily summaries, not high-frequency raw sensor data.

Storage must survive:

- Home Assistant restart;
- integration reload;
- config entry reconfigure;
- daylight-saving transitions.

Use Home Assistant's local timezone for calendar-day boundaries.

---

## 7. Psychrometrics and humidity behavior

### 7.1 RH is a model input, not a temperature offset

The legacy `humidity_offset` concept is removed.

Measured RH is supplied directly to ATHB.

### 7.2 Inverse-solving temperature must not blindly hold RH constant

When solving for a hypothetical future room air temperature, holding measured RH constant can be physically inconsistent during heating. If no humidification/dehumidification occurs, warming air usually changes RH while approximately preserving moisture content/vapor pressure.

Therefore the inverse solver must support a humidity assumption.

**Default:** `constant_moisture_content` (or equivalent constant vapor-pressure/humidity-ratio approximation).

Process:

1. derive moisture state from current `tdb + RH`;
2. for each candidate `tdb`, derive candidate RH at unchanged moisture state;
3. clamp only to physical 0–100% limits, never to a “comfortable RH range” invented by policy;
4. use candidate RH in ATHB.

Advanced option:

- `constant_relative_humidity` for spaces where a separate humidity-control system genuinely maintains RH.

The psychrometric implementation must be unit-tested over realistic indoor conditions.

---

## 8. Mean radiant temperature and cold-point handling

### 8.1 Principle

A coldest-point temperature sensor is **not** automatically MRT.

ATHB must preserve this distinction in data types and code structure.

### 8.2 Primary ATHB location

Every zone has one primary comfort location:

```text
primary tdb
primary/estimated tr
zone RH
relative air speed
met
running mean
clo behavior
```

This yields the current primary ATHB sensation and inverse solutions.

### 8.3 Critical comfort locations

For each selected critical air-temperature sensor, calculate a separate ATHB evaluation using that local `tdb`.

For heating control, the governing result is the location requiring the **highest safe room target** to achieve the configured target sensation.

This is still one scientific model: ATHB is evaluated at multiple physical comfort locations.

### 8.4 Mapping a critical point to a primary room setpoint

Let:

```text
Tp = current primary air temperature
Tc = current critical air temperature
Δc = Tp - Tc
```

For a candidate primary room temperature `x`, predict the critical location initially as:

```text
Tc_candidate = x - Δc_effective
```

where `Δc_effective` is filtered and bounded according to configuration.

Then:

1. evaluate the primary location at `x`;
2. evaluate each critical location at its candidate local temperature;
3. solve for the primary target required to meet the target sensation at each location;
4. use the most demanding valid location for heating;
5. expose which location governed the target.

This is a pragmatic local-zone approximation. It must be described as such and never mislabelled MRT.

### 8.5 Critical-point protection

To prevent a failed or poorly placed sensor from demanding extreme heat:

- require valid temperature device class/unit where available;
- enforce freshness checks;
- reject implausible values;
- optionally require the cold delta to persist for a short period;
- smooth/filter the primary-to-critical delta rather than the absolute measured temperature where appropriate;
- allow a configurable maximum critical-point influence/delta;
- raise a diagnostic reason when the influence is capped;
- never hide the raw critical value.

### 8.6 Surface-temperature mode

If an entity actually measures surface temperature, a radiative estimate may use a view-factor model in Kelvin:

```text
Tr^4 = Σ(Fi × Ti^4)
```

with remaining view factor assigned to the background radiant environment.

This mode requires explicit user declaration that the sensor is a surface temperature and requires a configured view factor. It must never be inferred from an entity name.

---

## 9. Inverse ATHB solver

### 9.1 Purpose

The model predicts sensation from environmental conditions. Home Assistant needs the inverse:

> Which representative room air temperature produces the desired thermal sensation under the current adaptive, humidity, radiant, air-speed and human conditions?

### 9.2 Core outputs

For each valid zone calculation, solve at least:

- `comfort_lower_temperature` at `comfort_vote_lower`;
- `neutral_temperature` at `target_vote` (normally 0.0);
- `comfort_upper_temperature` at `comfort_vote_upper`.

Recommended default policy values:

```text
comfort_vote_lower = -0.5
comfort_vote_upper = +0.5
target_vote        =  0.0
```

These thresholds are **ATHB control-policy choices**, not ASHRAE categories or a standards-compliance claim. They must be configurable in advanced options and documented accordingly.

### 9.3 Numerical method

Use a deterministic bracketed root finder such as bisection unless there is a compelling tested reason to use another method.

Requirements:

- no SciPy runtime dependency merely for root solving;
- configurable/validated solver search interval;
- guaranteed iteration cap;
- numerical tolerance significantly finer than target climate precision;
- explicit `no_bracket`, `non_finite` and `non_monotonic` error states;
- no silent extrapolated return value when no root exists.

### 9.4 Candidate-state semantics

For each candidate `tdb`:

- update candidate RH according to the configured psychrometric assumption;
- calculate candidate relative air speed consistently;
- if `tr` is approximated as `tdb`, move `tr` with candidate `tdb`;
- if a real MRT sensor is used, hold measured MRT constant for the instantaneous inverse calculation unless a documented derived-radiant model says otherwise;
- if surface-derived MRT is used, recompute according to that model;
- evaluate all critical comfort locations consistently.

### 9.5 Raw versus policy-limited values

Never destroy the scientific result by clipping it before exposure.

Keep separate:

```text
raw ATHB comfort solution
↓
policy target
↓
user hard-bound clamp
↓
device capability/min/max/step normalization
↓
effective command
```

Diagnostics must show each stage and the reason for every clamp.

---

## 10. Thermal policy layer

The ATHB engine itself contains no concepts such as occupancy, eco or boost. Those belong in a separate policy layer.

### 10.1 Profiles

Every zone supports at least:

- `auto`
- `comfort`
- `eco`
- `boost`

Recommended entity:

```text
select.<zone>_athb_profile
```

### 10.2 Auto profile

`auto` resolves the effective profile from an optional Home Assistant binary entity representing occupancy, schedule activity or heating demand.

If the binary entity is configured:

```text
on  => comfort
off => eco
```

If no such source is configured, `auto` resolves to `comfort`.

ATHB should not duplicate Home Assistant's schedule helper.

### 10.3 Comfort profile

Heating-only target:

```text
inverse ATHB target at configured target_vote
```

Heat/cool range:

```text
lower = inverse ATHB at comfort_vote_lower
upper = inverse ATHB at comfort_vote_upper
```

### 10.4 Eco profile

Eco is explicitly a policy decision, not a new comfort model.

For heating:

```text
eco_target = comfort_target - setback_temperature
```

For cooling:

```text
eco_target = comfort_target + setback_temperature
```

For heat/cool range:

```text
eco_low  = comfort_low  - heating_setback
eco_high = comfort_high + cooling_setback
```

All values remain subject to hard bounds and device limits.

### 10.5 Boost profile

Boost must not set an actuator to its hardware maximum by default.

Recommended policy:

```text
heating boost target = comfort_target + configured boost_delta
cooling boost target = comfort_target - configured boost_delta
```

with immediate hard-bound/device clamping.

The UI must make the boost magnitude visible.

### 10.6 User thermal preference

ATHB predicts a population-level thermal sensation, not an individual's exact preference.

Personal preference should be represented transparently through `target_vote`, not hidden inside RH/MRT/outdoor “corrections”.

A user-friendly option can map labels such as `cooler`, `neutral`, `warmer` to explicit numeric target-vote values, with an advanced numeric setting available.

### 10.7 Hard target limits

Support user-configured absolute target limits separately from the ATHB comfort range, for example:

```text
minimum_control_temperature
maximum_control_temperature
```

These are safety/policy constraints, not model inputs.

The raw ATHB result remains available even when a policy limit clamps the actual command.

---

## 11. Climate-control architecture

### 11.1 Separation of concerns

```text
Home Assistant source states
          │
          ▼
┌─────────────────────────────┐
│ Input normalization         │
│ units / validity / freshness│
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Outdoor history             │
│ daily mean / running mean   │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ ATHB engine                 │
│ one scientific model        │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Inverse solver              │
│ lower / neutral / upper     │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Policy engine               │
│ auto/comfort/eco/boost      │
└──────────────┬──────────────┘
               ▼
┌─────────────────────────────┐
│ Climate adapters            │
│ capabilities / units / step │
└──────────────┬──────────────┘
               ▼
      climate.* service calls
```

No Home Assistant service-call logic belongs in the scientific engine.

### 11.2 One or more climate targets

A zone may control one or more `climate` entities.

Each target gets its own `ClimateAdapter` because capabilities, limits and setpoint precision may differ.

A failure or incompatibility of one target should not corrupt the model result for the zone; it should degrade actuator status and be surfaced explicitly.

### 11.3 Climate setpoint offset

Allow an optional per-actuator fixed setpoint calibration offset for devices whose internal temperature reference differs consistently from the configured room reference.

Do **not** automatically infer a dynamic calibration from a radiator-mounted temperature sensor without a separate validated design; radiator self-heating can make such inference unstable.

---

## 12. Home Assistant climate capability matrix

Home Assistant currently defines these HVAC modes:

- `off`
- `heat`
- `cool`
- `heat_cool`
- `auto`
- `dry`
- `fan_only`

It separately advertises supported features such as target temperature, target temperature range, target humidity, fan mode and presets.

ATHB must read both the HVAC mode and `supported_features` rather than infer capabilities from names alone.

### 12.1 Control strategies

Per zone, configuration shall support:

1. **Follow current HVAC mode** — ATHB never changes `hvac_mode`; it only applies a compatible setpoint/range.
2. **Managed heating** — ATHB may place compatible targets into `heat` and is allowed to recover from `off` when control policy requires heating.
3. **Managed cooling** — equivalent for cooling.
4. **Managed heat/cool** — ATHB manages a ranged comfort band when the target exposes compatible support, otherwise uses an explicitly defined fallback strategy.

For the intended direct heating takeover, **Managed heating** is the primary configuration.

### 12.2 Behavior by HVAC mode

| Current/managed mode | Required behavior |
|---|---|
| `heat` | Set normalized single heating target if `TARGET_TEMPERATURE` exists |
| `cool` | Set normalized single cooling target if supported |
| `heat_cool` | Prefer `target_temp_low` + `target_temp_high` when `TARGET_TEMPERATURE_RANGE` exists |
| `auto` | Do not assume it means automatic heating/cooling; if range is genuinely supported, a range may be written; otherwise use only explicitly compatible target-temperature behavior |
| `dry` | Preserve mode; suspend temperature control unless future explicit policy says otherwise |
| `fan_only` | Preserve mode; suspend temperature control |
| `off` | In follow-mode, suspend; in managed mode, ATHB may restore the configured managed HVAC mode |

### 12.3 Preserve unrelated climate settings

ATHB must not change these merely because they exist:

- fan mode;
- preset mode;
- swing mode;
- horizontal swing mode;
- target humidity.

If a future feature deliberately controls one of them, it must be capability-aware and separate from the thermal model.

### 12.4 Unit, range and step normalization

Internal scientific calculations use SI units, specifically °C and m/s.

For each target climate:

- identify `temperature_unit`;
- convert calculated target to the target's expected Home Assistant unit;
- enforce `min_temp` and `max_temp`;
- normalize to `target_temperature_step` when provided;
- otherwise honor entity `precision` appropriately;
- avoid float-noise service calls.

Input temperature sensors must also be normalized safely; do not assume every numeric state is °C.

---

## 13. Controller state machine and ownership

### 13.1 Control states

At minimum:

```text
DISABLED
ACTIVE
MANUAL_OVERRIDE
DEGRADED_HOLD
SUSPENDED_HVAC_MODE
INVALID
```

The current state and reason must be visible diagnostically and preferably through an enum sensor.

### 13.2 Control enable switch

Provide:

```text
switch.<zone>_athb_control
```

Turning the switch off stops ATHB command writes immediately but does **not** turn the underlying climate device off unless the user explicitly requests that behavior through a separate action/policy.

### 13.3 External/manual setpoint detection

When ATHB owns a climate target, a human or another automation may still change it.

The integration must distinguish its own service calls from external state changes, using Home Assistant context where possible plus command correlation as a fallback.

Default behavior for an external target-temperature change:

1. enter `MANUAL_OVERRIDE`;
2. stop replacing that target immediately;
3. retain the user-selected target for a configurable duration or until the configured resume condition;
4. expose remaining override status;
5. provide a `Resume ATHB control` button/action.

Do not create a tug-of-war where ATHB rewrites a user's manual setpoint seconds later.

### 13.4 External HVAC-mode changes

In follow-mode, honor the new mode.

In managed mode, a user-initiated mode change should be treated as a manual override or suspension rather than immediately fought, unless the user has explicitly chosen strict ownership behavior.

### 13.5 Command correlation

Maintain per-target metadata for at least:

- last commanded mode;
- last commanded temperature/range;
- command timestamp;
- Home Assistant context ID where available;
- acknowledgement status;
- retry count.

---

## 14. Stable actuation and anti-chatter behavior

### 14.1 Event-driven recomputation

Recompute on meaningful source changes, not every ten minutes by default.

Listen to:

- primary temperature;
- RH;
- MRT/radiant sources;
- critical comfort point(s);
- air-speed source;
- occupancy/demand source;
- profile/control entities;
- target climate state/capability changes;
- daily outdoor-history rollover;
- Home Assistant start/reload.

Use a short debounce to coalesce bursts from multiple sensors.

### 14.2 Write threshold

Only write a new setpoint when the normalized effective target differs materially from the last/current target.

Minimum threshold should be at least the target climate's supported step, with a configurable policy floor.

### 14.3 Minimum command interval

Prevent repeated actuator service calls during state storms. Policy transitions such as `eco → comfort`, `boost`, manual resume and critical safety changes may bypass normal rate limiting where appropriate.

### 14.4 Environmental target slew limit

A configurable target slew-rate limiter should be available to prevent noisy RH/MRT/critical-point changes from moving a thermostat target too rapidly.

Important distinction:

- **environment-driven** target changes may be slew-limited;
- **explicit policy transitions** should normally apply immediately.

### 14.5 No fixed one-second delay assumptions

Do not copy the legacy “set HVAC mode → sleep one second → set temperature” pattern.

Use a command broker/state-aware sequence:

1. send required HVAC-mode command;
2. observe/await compatible state acknowledgement with timeout;
3. re-read capabilities/state;
4. send normalized temperature command;
5. retry transient failures with bounded backoff;
6. surface persistent failure without log spam.

---

## 15. Data validity, degradation and fail-safe policy

### 15.1 Never fabricate normal measurements silently

Do not implement patterns equivalent to:

```text
invalid temperature -> 20 °C
invalid humidity    -> 50%
```

Fallback behavior must be explicit and diagnosable.

### 15.2 Input validation

For every source validate:

- entity exists;
- state/attribute is present;
- value parses to finite numeric type;
- unit is supported/convertible;
- value is physically plausible;
- state is not stale beyond configured threshold.

### 15.3 Quality state, not fake precision

Do not invent a “96% confidence” number unless it has a defensible statistical meaning.

Use deterministic states and reasons, for example:

```text
good
degraded_history
degraded_radiant_estimate
degraded_critical_point_capped
extrapolated
invalid_input
actuator_degraded
```

Multiple reasons can coexist.

### 15.4 Last-good hold

If a model input fails temporarily:

- preserve the last valid calculated target for a configurable grace period;
- do not recompute with invented inputs;
- do not turn heating off as a side effect;
- expose `DEGRADED_HOLD`.

After the grace period, apply the configured safe fallback policy.

### 15.5 Safe fallback temperature

Every actively controlled heating zone must configure or accept an explicit safe fallback target for situations where no valid ATHB result exists.

This fallback is **policy**, not ATHB output.

It must be clamped to user/device limits and clearly labelled as fallback in diagnostics.

### 15.6 Model-domain/extrapolation handling

ATHB is empirical and its predictive performance is not uniform over all possible environmental conditions. Dutch winter running means can enter ranges with less empirical support.

The implementation must therefore:

1. document the applicable input ranges found in the cited ATHB work/reference implementation;
2. mark out-of-domain calculations as `extrapolated` rather than pretending they are equally validated;
3. retain raw outputs for inspection;
4. subject effective control targets to hard user bounds;
5. exercise cold-climate test grids before production acceptance;
6. optionally fall back to the configured fixed safe target if the user chooses `reject_extrapolation`.

Do **not** silently clamp `t_running_mean` to a research range; that changes the model while hiding the fact that it happened.

---

## 16. Entity model

The integration should remain compact but expose enough state to be understandable and automatable.

### 16.1 Enabled by default

Recommended default entities per zone:

```text
sensor.<zone>_athb_thermal_sensation
sensor.<zone>_athb_comfort_temperature
sensor.<zone>_athb_comfort_lower_temperature
sensor.<zone>_athb_comfort_upper_temperature
sensor.<zone>_athb_effective_target_temperature
sensor.<zone>_athb_running_mean_outdoor_temperature
sensor.<zone>_athb_control_status
binary_sensor.<zone>_athb_comfortable
switch.<zone>_athb_control
select.<zone>_athb_profile
button.<zone>_athb_resume_control
```

If entity-count minimization is preferred, lower/upper or selected diagnostics may be disabled by default, but they must be readily available from the entity registry.

### 16.2 Suggested semantics

#### Thermal sensation

State: current governing ATHB PMV/sensation.

Useful attributes or companion diagnostic data:

- primary sensation;
- governing comfort location;
- critical sensations.

Avoid frequently changing large attributes when separate entities are more appropriate for Recorder efficiency.

#### Comfort temperature

State: neutral/personalized target from inverse ATHB before eco/boost and actuator normalization.

#### Comfort lower/upper

Raw ATHB-derived comfort policy boundaries.

#### Effective target temperature

The actual target after profile, hard bounds, unit conversion and actuator constraints. With multiple target climates whose normalized values differ, expose a canonical zone target plus per-actuator differences in diagnostics.

#### Control status

Enum-like values such as:

```text
active
manual_override
degraded_hold
suspended_hvac_mode
disabled
invalid
```

### 16.3 Diagnostics-only or disabled-by-default entities

Candidates:

- effective MRT;
- relative air speed;
- current-day outdoor mean;
- outdoor-history coverage;
- automatic clothing value if exposed by the implementation;
- adapted metabolic value if useful;
- critical-point delta;
- target clamp reason;
- raw pre-policy target.

---

## 17. Configuration UX

### 17.1 Config flow

Setup must be fully UI-based through `config_flow`.

Suggested steps:

1. **Zone identity**
   - name/area.
2. **Environmental sources**
   - primary temperature;
   - RH;
   - outdoor temperature;
   - optional MRT;
   - optional critical point(s);
   - air speed fixed/sensor.
3. **Human comfort parameters**
   - activity preset/custom `met`;
   - automatic/fixed clothing;
   - thermal preference/target vote;
   - comfort vote band.
4. **Control policy**
   - optional occupancy/demand entity;
   - setback;
   - boost magnitude;
   - min/max control temperatures;
   - safe fallback target.
5. **Climate targets**
   - one or more climate entities;
   - control strategy: follow / managed heating / managed cooling / managed heat-cool;
   - per-target optional setpoint offset.
6. **Advanced stability**
   - write threshold;
   - debounce;
   - minimum command interval;
   - target slew rate;
   - manual override duration;
   - stale-data thresholds;
   - running-mean alpha/history behavior.
7. **Validation summary**
   - show discovered target capabilities;
   - show current normalized source values;
   - calculate a preview ATHB result;
   - block obviously incompatible configurations.

### 17.2 Reconfigure and options flow

All entity selections that can change in a home must be editable without deleting/recreating the config entry.

Changing source entities must trigger:

- listener rebinding;
- validation;
- outdoor-history decision when outdoor source changes;
- immediate recomputation.

### 17.3 No credentials

ATHB is local and requires no account/authentication flow. Reauthentication is therefore not applicable.

---

## 18. Architecture and suggested modules

Suggested repository structure:

```text
custom_components/athb/
├── __init__.py
├── manifest.json
├── const.py
├── config_flow.py
├── models.py
├── athb_engine.py
├── pmv_core.py
├── psychrometrics.py
├── inverse_solver.py
├── outdoor_history.py
├── input_manager.py
├── comfort_locations.py
├── policy.py
├── climate_adapter.py
├── command_broker.py
├── zone_controller.py
├── storage.py
├── diagnostics.py
├── repairs.py
├── sensor.py
├── binary_sensor.py
├── switch.py
├── select.py
├── button.py
├── strings.json
└── translations/
    ├── en.json
    └── nl.json
```

Tests should mirror functional modules rather than only end-to-end behavior.

### 18.1 Pure-Python model boundary

`athb_engine.py`, `pmv_core.py`, `psychrometrics.py` and `inverse_solver.py` should be usable without Home Assistant imports.

This enables deterministic scientific unit testing.

### 18.2 Runtime dependency policy

`pythermalcomfort 4.4.2` currently depends on NumPy, SciPy and Numba. Home Assistant can install custom integration requirements, but adding this complete scientific stack purely for one ATHB function increases installation and cross-platform risk.

Preferred implementation direction:

- implement the required ATHB/PMV mathematical subset in a compact pure-Python internal module;
- base it on published equations/reference behavior;
- use pinned `pythermalcomfort` as a development/test oracle;
- compare across large input grids to tight tolerance;
- preserve required attribution/license notices if source code is adapted from MIT-licensed material.

If the agent discovers that maintaining an exact internal implementation is materially riskier than the runtime dependency, it must document the trade-off before changing this decision.

### 18.3 No unnecessary polling coordinator

Because the primary inputs are Home Assistant state entities, this should be push/event driven. Do not force a polling-oriented `DataUpdateCoordinator` pattern unless it genuinely improves the design.

A zone controller can subscribe to state changes and publish coherent snapshots to entities.

---

## 19. Internal data contracts

Use typed immutable dataclasses where practical.

Example conceptual contracts:

```python
@dataclass(frozen=True)
class EnvironmentalSnapshot:
    tdb_c: float
    rh_pct: float
    tr_c: float
    measured_air_speed_m_s: float
    relative_air_speed_m_s: float
    running_mean_outdoor_c: float
    met: float
    clo: float | None
    critical_locations: tuple[CriticalLocation, ...]
    quality: tuple[str, ...]

@dataclass(frozen=True)
class AthbResult:
    primary_vote: float
    governing_vote: float
    governing_location: str

@dataclass(frozen=True)
class ComfortSolution:
    lower_c: float
    target_c: float
    upper_c: float
    governing_location: str
    quality: tuple[str, ...]

@dataclass(frozen=True)
class PolicyTarget:
    profile: str
    heat_target_c: float | None
    cool_target_c: float | None
    range_low_c: float | None
    range_high_c: float | None

@dataclass(frozen=True)
class ActuatorCommand:
    entity_id: str
    hvac_mode: str | None
    target_temperature: float | None
    target_temp_low: float | None
    target_temp_high: float | None
    reason: str
```

Model output, policy output and actuator command must remain separately inspectable.

---

## 20. Home Assistant integration quality target

Although this is a custom/HACS integration, development should aim at modern Home Assistant **Gold-equivalent engineering discipline** where relevant.

Required from the initial production release:

- UI config flow;
- reconfigure/options flow;
- translations at least English and Dutch;
- correct entity naming and unique IDs;
- config entry migration support from the first released schema onward;
- diagnostics download;
- repairs/issues for persistent actionable configuration problems;
- no log flooding during recurring source failure;
- correct unload/reload behavior;
- async-safe implementation;
- no blocking I/O in the event loop;
- config-flow test coverage of all branches/errors;
- >95% total integration test coverage target, with scientific core effectively 100% branch coverage where practical;
- README installation, setup, entity, troubleshooting and scientific-limitation documentation;
- HACS validation;
- lint/format/test CI.

---

## 21. Test and verification strategy

Testing is a release requirement, not cleanup after implementation.

### 21.1 Scientific golden tests

Use `pythermalcomfort==4.4.2` as a pinned oracle during development/test generation.

Validate ATHB output across:

- hand-selected documented examples;
- dense deterministic grids;
- random seeded input sets;
- automatic clothing;
- fixed clothing;
- varying RH;
- varying MRT independently from air temperature;
- varying met;
- varying relative air speed;
- varying running mean.

Numerical tolerance must be defined and justified.

### 21.2 Inverse-solver tests

For every solved target:

1. feed the solved environmental state back into the ATHB engine;
2. prove the resulting sensation matches the requested vote within tolerance.

Test:

- lower, neutral and upper roots;
- no-bracket conditions;
- numerical limits;
- psychrometric candidate RH;
- fixed vs derived MRT behavior;
- critical-location governing behavior.

### 21.3 Cold-climate sanity matrix

Explicitly test winter scenarios relevant to northern European residential heating, including running-mean temperatures below the strongest empirical range.

The objective is not to invent a second model; it is to detect:

- non-finite results;
- implausible discontinuities;
- target reversals;
- extreme clothing/met adaptation;
- comfort targets outside reasonable residential ranges;
- critical-point runaway behavior.

Out-of-domain model results may remain visible, but control must remain bounded by configured policy limits.

### 21.4 Physical/sanity invariants

Where scientifically justified, test properties such as:

- a warmer radiant environment should not require a substantially warmer air target for the same sensation;
- increasing heating comfort preference should not lower the solved heating target;
- critical points must never lower the heating target relative to the same scenario without them;
- hard min/max bounds are never exceeded;
- device min/max bounds are never exceeded;
- normalized setpoints align to supported step;
- invalid numeric inputs never become plausible defaults;
- solver never returns NaN/inf as a command.

Do not enforce a “physical invariant” if ATHB theory/reference behavior disproves it; document such cases instead.

### 21.5 Home Assistant behavior tests

Cover at least:

- config flow happy path and every error branch;
- reconfigure/options;
- unload/reload;
- entity restoration/state updates;
- `heat` target temperature;
- `cool` target temperature;
- `heat_cool` target range;
- `auto` with and without range capability;
- `off`, `dry`, `fan_only` suspension rules;
- Fahrenheit climate target normalization;
- min/max and target step;
- multiple target climates with mixed capabilities;
- occupancy auto profile;
- eco/boost transitions;
- manual override detection and expiry/resume;
- unavailable/stale sensors;
- last-good hold;
- fixed fallback target;
- target command de-duplication;
- mode acknowledgement timeout and retry;
- integration reload during pending command;
- Recorder history bootstrap with data/no data/partial data;
- corrupted storage recovery;
- DST daily-history rollover.

### 21.6 Legacy comparison

Create a test fixture representing the current living-room configuration and compare the old blueprint's *intent* against ATHB behavior. Exact temperatures do not need to match because the scientific model is deliberately changing.

The comparison should identify and document why materially different results occur.

---

## 22. Observability and diagnostics

A downloaded diagnostic payload should include, with no sensitive location data:

```text
integration version
config schema version
zone control state
source entity IDs
source availability/freshness/unit
normalized environmental snapshot
outdoor daily history summaries + coverage
running mean and alpha
met/clo configuration
MRT mode and effective MRT
critical point values/deltas/caps
overall quality/degradation reasons
current ATHB votes
inverse-solver outputs
profile and policy calculation
hard-bound clamp reasons
per-climate capabilities
per-climate normalized command
last command/ack/retry metadata
manual override state
```

Diagnostics must make it possible to answer:

> “Why did ATHB set this room to 19.7 °C at this moment?”

without needing to read source code.

---

## 23. README requirements

The README title and product references must use exactly:

# Adaptive Thermal Heat Balance (ATHB)

The README must state early that:

- this integration uses the Adaptive Thermal Heat Balance framework;
- it is **not an ASHRAE 55 compliance/certification tool**;
- it can directly control Home Assistant climate entities;
- RH, MRT/estimated radiant environment, air speed, activity, clothing adaptation and running-mean outdoor temperature influence the target;
- critical local air-temperature points are treated as separate comfort locations rather than pretending they are MRT;
- the model has empirical applicability limitations and extrapolation is surfaced;
- hard user bounds and fail-safe behavior remain authoritative for actuation.

Include:

- HACS/manual installation;
- configuration walkthrough;
- source semantics;
- examples for heat-only radiator/TRV, cooling and heat/cool devices;
- entity list;
- manual override behavior;
- diagnostics/troubleshooting;
- scientific basis and references;
- migration guidance from the legacy blueprint.

Do not market the product as “ASHRAE 55 Adaptive Climate Control”.

---

## 24. Migration from the existing blueprint

The initial implementation should include a documented migration mapping.

Legacy living-room example:

```yaml
climate_entity: climate.roommind_living_room_override
indoor_temp_sensor: sensor.living_room_temperature_calculated
outdoor_temp_sensor: sensor.outside_temperature_combined
occupancy_sensor: binary_sensor.heating_living_room_active
boost_sensor: input_boolean.heating_boost_living_room
setback_temperature_offset: 2.5
min_comfort_temp: 18
max_comfort_temp: 21
indoor_humidity_sensor: sensor.living_room_humidity_calculated
mean_radiant_temp_sensor: sensor.living_room_temperature_sensor_critical_point
use_operative_temperature: true
air_velocity: 0.4
```

ATHB mapping:

| Legacy | ATHB |
|---|---|
| `indoor_temp_sensor` | primary indoor air temperature |
| `outdoor_temp_sensor` | outdoor-history source |
| `indoor_humidity_sensor` | direct ATHB RH input |
| `occupancy_sensor` | auto-profile occupancy/demand source |
| `boost_sensor` | replace with ATHB `boost` profile or optional compatibility input |
| `setback_temperature_offset` | eco heating setback |
| `min_comfort_temp` | minimum control temperature policy |
| `max_comfort_temp` | maximum control temperature policy |
| legacy `mean_radiant_temp_sensor` containing critical point | configure as **critical local air temperature**, unless measurement provenance proves it is true MRT |
| `air_velocity: 0.4` | must be reclassified as measured ambient air speed vs direct relative air speed; do not blindly migrate |
| climate output | new selected target climate(s) |

The old ASHRAE category, humidity min/max formulas and custom humidity/airspeed offsets have no direct ATHB equivalent and should not be migrated.

### 24.1 Compatibility input for existing boost automation

To ease immediate replacement, the production release may optionally accept an external binary boost entity in addition to the native `select` profile. If enabled, it temporarily resolves the effective profile to `boost`.

This is preferable to requiring all existing automations to be rewritten on day one.

---

## 25. Initial direct-heating takeover acceptance criteria

Before removing the legacy automation for a zone, all of the following must pass:

1. Seven-day running mean is populated from persisted or Recorder-backed history.
2. Current primary temperature and RH are valid and fresh.
3. Critical point is configured with the correct semantic type.
4. Air-speed configuration is semantically correct (`v` vs `vr`).
5. Current ATHB sensation and solved target are finite and plausible.
6. Comfort target, eco target and boost target stay within configured hard bounds.
7. Target climate capability detection is correct.
8. A real service-call test proves the climate accepts normalized setpoints.
9. Manual external setpoint change is detected and does not trigger a tug-of-war.
10. Sensor-unavailable test results in hold/fallback, not HVAC shutdown or fictitious model input.
11. Home Assistant restart restores control cleanly.
12. No repeated unnecessary setpoint writes occur during a stable 30-minute test.
13. Diagnostics can explain each chosen target and clamp.
14. Legacy automation is disabled only after the ATHB controller has demonstrated stable control on the intended target entity.

---

## 26. Example calculation flow

Conceptual example only:

```text
Primary Tdb                  20.1 °C
RH                           43 %
Measured MRT                 unavailable
MRT fallback                 primary Tdb
Measured air speed           0.10 m/s
Met                          1.1
Clothing                     automatic ATHB
Running mean outdoor          9.8 °C
Critical comfort point       19.2 °C
Primary-critical delta        0.9 K
Profile                      comfort
Target vote                   0.0
Comfort band votes           -0.5 ... +0.5
```

Processing:

```text
normalize measurements
       ↓
calculate relative air speed
       ↓
calculate primary ATHB vote
       ↓
calculate critical-location ATHB vote
       ↓
inverse solve primary temperature for vote -0.5 / 0 / +0.5
       ↓
inverse solve while preserving critical delta
       ↓
select governing location
       ↓
apply profile
       ↓
apply user hard bounds
       ↓
normalize separately for every climate target
       ↓
dedupe / rate-limit / command
```

The exact target is not specified in this whitepaper; it must emerge from the validated ATHB implementation.

---

## 27. Security, privacy and safety

ATHB is a local controller and should require no cloud service.

- Do not transmit environmental data externally.
- Diagnostics should redact precise location coordinates if any Home Assistant metadata is ever included.
- Do not execute dynamically supplied templates or arbitrary Python.
- Validate all entity-derived values before use.
- On internal exceptions, preserve the last safe actuator state/target rather than issuing `off` or extreme setpoints.
- Never use device hardware min/max as a definition of human-safe boost.

---

## 28. Performance expectations

A house with many ATHB zones should have negligible impact on Home Assistant.

Design expectations:

- pure numerical calculation measured in milliseconds, not seconds;
- no high-frequency polling;
- shared outdoor history per source;
- debounced source bursts;
- no Recorder query during normal steady-state operation after bootstrap;
- bounded command and diagnostic history;
- no NumPy/SciPy/Numba runtime cost unless an explicit architecture decision changes the pure-Python approach.

Performance tests should include at least dozens of zones and source-change bursts to prove no event-loop blocking.

---

## 29. Decisions that are deliberately fixed

The following are design decisions, not open questions for the initial implementation:

1. Product name: **Adaptive Thermal Heat Balance (ATHB)**.
2. One runtime comfort model: **ATHB only**.
3. No ASHRAE Adaptive/PMV secondary runtime model or user-facing reference entities.
4. Running-mean outdoor temperature, not instantaneous outdoor temperature, drives adaptation.
5. RH enters ATHB directly; no hand-written humidity temperature offset.
6. A critical local air-temperature sensor is not MRT.
7. Critical points are handled as additional ATHB comfort locations.
8. Raw scientific result and final control target remain separate.
9. No silent `20 °C`/`50%` type fallbacks.
10. Direct climate control is in scope for the initial production release.
11. Home Assistant climate commands are capability-driven.
12. Fan/preset/swing are preserved unless explicitly supported by a future separate feature.
13. Setup is UI/config-entry based.
14. Integration behavior is event driven.
15. Manual changes must not cause controller tug-of-war.
16. Scientific correctness is validated against a pinned independent `pythermalcomfort` oracle during development.

---

## 30. Open engineering choices the implementation may resolve

These details may be chosen by the implementation agent if it documents the decision and preserves all fixed requirements:

- exact pure-Python PMV/ATHB module structure;
- exact Home Assistant event subscription helper APIs;
- exact Recorder bootstrap API appropriate to the current Home Assistant version;
- exact local storage schema;
- exact debounce/min-command default values after testing;
- exact critical-delta filter implementation;
- whether advanced globe/surface-derived MRT ships enabled in the first production release or is present but marked experimental;
- exact internal entity-description pattern;
- whether a dispatcher or lightweight coordinator is used to fan out coherent zone state to platform entities;
- exact CI action versions.

Any choice that changes the thermal model, control semantics or fail-safe behavior requires an explicit design note.

---

## 31. References

### Scientific

1. Schweiker, M. (2022). *Combining adaptive and heat balance models for thermal sensation prediction: A new approach towards a theory and data-driven adaptive thermal heat balance model*. Indoor Air, 32, e13018. DOI: https://doi.org/10.1111/ina.13018
2. Schweiker, M. & Wagner, A. (2015). *A framework for an adaptive thermal heat balance model (ATHB).* Building and Environment, 94, 252–262. DOI: https://doi.org/10.1016/j.buildenv.2015.08.018
3. pythermalcomfort 4.4.2 model documentation: https://pythermalcomfort.readthedocs.io/en/latest/documentation/models.html
4. pythermalcomfort `pmv_athb` source/reference implementation: https://pythermalcomfort.readthedocs.io/en/latest/_modules/pythermalcomfort/models/pmv_athb.html
5. pythermalcomfort utility documentation (`v_relative`, running mean, MRT utilities): https://pythermalcomfort.readthedocs.io/en/latest/documentation/utilities_functions.html
6. pythermalcomfort PyPI package metadata: https://pypi.org/project/pythermalcomfort/4.4.2/

### Home Assistant

7. Climate entity developer documentation: https://developers.home-assistant.io/docs/core/entity/climate/
8. Sensor entity developer documentation: https://developers.home-assistant.io/docs/core/entity/sensor/
9. Config flow documentation: https://developers.home-assistant.io/docs/core/integration/config_flow/
10. Integration manifest and requirements: https://developers.home-assistant.io/docs/creating_integration_manifest/
11. Integration Quality Scale: https://developers.home-assistant.io/docs/core/integration-quality-scale/
12. Diagnostics rule: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/
13. Config-flow test coverage rule: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/config-flow-test-coverage/
14. Reconfiguration flow rule: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/reconfiguration-flow/

### Legacy source

15. `adaptive_climate_control_v3_patrick-3.yaml` — current blueprint supplied as migration/reference baseline. It is not the scientific source for ATHB.

---

## 32. Final product statement

**Adaptive Thermal Heat Balance (ATHB)** is a Home Assistant comfort controller, not merely a dynamic outside-temperature thermostat.

Its target is determined by the combined thermal environment:

```text
adaptive outdoor history
+ indoor air temperature
+ relative humidity
+ radiant environment
+ local cold comfort points
+ air movement
+ activity
+ clothing adaptation
= ATHB thermal sensation
= inverse comfort temperature/range
= policy-aware Home Assistant climate target
```

The integration is successful when a user can replace a complex heating automation with one understandable ATHB zone whose output is scientifically traceable, operationally stable and safe under failure.
