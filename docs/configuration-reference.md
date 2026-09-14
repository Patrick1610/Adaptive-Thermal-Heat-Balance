# ATHB configuration reference

This is the technical companion to the Home Assistant setup and reconfigure wizards. Every
wizard page links here. Defaults are deliberately conservative; changing a value changes either
the physical comfort calculation, product policy, or command safety. ATHB owns temperature
targets only. It never changes HVAC mode, power, fan, preset, swing, or humidity.

## Environmental inputs

`primary_temperature` is the representative indoor **air** temperature. It may be a temperature
or numeric sensor, whose state is used, or a climate entity, whose public `current_temperature`
attribute is used. Either route must report a finite value in °C, °F or K with a fresh observation.
Unitless numerics, a missing climate attribute, NaN and malformed values are invalid. Exactly one
source is selected; ATHB does not average multiple room temperatures. Indoor
RH is either `measured` from an entity or a fixed `declared` percentage. Declared RH retains that
provenance throughout calculations and diagnostics. ATHB never invents a replacement value.

`outdoor_source` is an outdoor air-temperature entity. It feeds persisted local-calendar daily
summaries and the previous-seven-day exponentially weighted running mean. Outdoor humidity is not
an ATHB 2022 input: thermal adaptation uses outdoor temperature history, while comfort and
surface-risk psychrometrics use indoor vapour pressure.

If calculation cannot proceed, the **Input status** sensor reports the exact hold reason, such as
`primary_temperature_invalid`, `primary_rh_invalid`, `air_speed_invalid`, or
`outdoor_history_insufficient`. The affected numerical sensors remain unavailable rather than
showing plausible but false values.

## Climate targets

Choose one to eight registered climate entities. ATHB inspects each entity's supported features
and exposes only its real scalar `temperature` endpoint or ranged `target_low`/`target_high`
endpoints. Heating is normalized inward upward; cooling inward downward. Device bounds and grid
are applied after ATHB policy. A ranged write is atomic and is suppressed if the configured gap
cannot be preserved.

External target-temperature intervention starts a manual override. An external HVAC-mode change
does not. All production writes pass through the sole `CommandBroker` to
`climate.set_temperature`.

## Comfort level, occupancy and Boost

The comfort band is defined by `lower_comfort_vote` and `upper_comfort_vote`, default -0.5 and
+0.5 ATHB sensation. Strategy targets are solved directly in sensation space:

| Comfort level | Inward fraction | Default heating vote | Default cooling vote | Meaning |
|---|---:|---:|---:|---|
| Eco | 0.10 | -0.45 | +0.45 | Greatest efficiency; roots remain closest to the outer comfort boundaries. |
| Efficient | 0.30 | -0.35 | +0.35 | More of the comfort band remains available. |
| Balanced | 0.50 | -0.25 | +0.25 | Recommended energy/comfort compromise. |
| Comfort | 0.70 | -0.15 | +0.15 | Targets remain closer to thermal neutral. |
| Near neutral | 0.90 | -0.050 | +0.050 | Greatest normal comfort without making neutral the target. |

The temperatures are **not** interpolated. Each vote is independently inverse-solved while vapour
pressure remains constant. Thermal neutral (vote 0) is a reference, not the normal actuator
target.

An optional binary occupancy or schedule source applies Setback at every comfort level: `on`
uses the solved roots unchanged and `off` widens them using the selected Setback. Without a source,
no setback is applied. Unknown input retains the last resolved state for 30 minutes and then
becomes unavailable while calculation explicitly assumes occupied/no setback with
`occupancy_unknown`. A native **Occupancy — resolved** binary sensor is created only when a source
is configured; its attributes show the source state and whether the resolution is held.

Boost is an independent runtime select. **Off** follows comfort level and occupancy. **Adaptive**
bypasses setback and shifts the directional target by `boost_delta_c` toward, but not past,
thermal neutral. **Rapid** requests the maximum command temperature for scalar heating (minimum
for scalar cooling) until the calculated Adaptive Boost target is reached, then holds that target.
Rapid falls back explicitly to Adaptive for an atomic range or a zone with separate heating and
cooling targets. Boost expires to Off. Comfort-level, Setback and Boost changes are lightweight
and do not reload the config entry.

Five diagnostic sensors expose the complete inverse-solved structure before occupancy or Boost
policy. **Comfort range — lower limit**, **Comfort range — neutral reference**, and **Comfort
range — upper limit** form the outer comfort range. **Control point — heating** and
**Control point — cooling** form the selected comfort level's inner control range. Their
temperature states are the solved room temperatures; the corresponding sensation vote and range
role are available as attributes. Setback and Boost do not change these raw ranges.

For ordinary scalar heating-only or cooling-only zones, three room-coordinate sensors make the
policy result explicit:

- **Target — occupied** is the target for the selected comfort level with no occupancy setback and
  with Boost off.
- **Target — unoccupied** is the corresponding target with the configured occupancy setback and
  with Boost off.
- **Target — current** is the target that applies now, including occupancy, Boost and any ordinary
  environmental slew limiting.

These sensor states are the common room target before per-actuator calibration, device limits and
grid rounding. **Target — current** exposes a structured `decision` attribute with scenario,
comfort, occupancy/setback, Boost phase, governing control point, pre-slew/requested values,
transition, quality, suppression and recovery state. Its `per_climate` attribute includes current
temperature, reported setpoint, calibration, bounds, grid, HVAC mode/action, availability,
normalized ATHB requests, ownership/readiness and command outcome. This keeps the device page
compact while preserving actuator-level evidence. Zones that genuinely mix heating and cooling directions or use
an atomic temperature range retain separate climate-specific endpoint sensors: one scalar room
target would be physically ambiguous there.

Targets remain previews while control is disabled. The actual climate target changes only when
adaptive control is enabled and ownership, capability, override, and broker gates allow a write.
Fallback or unavailable states and suppression reasons remain explicit in the status context.
Consequently a fixed effective target with **Status — inputs** equal to
`running_mean_unavailable` is explicit fallback behaviour, not a responsive ATHB result.

All ATHB entities expose relevant state attributes for traceability. Depending on the entity these
include measured or declared sources and provenance, selected control settings, related ATHB model
values, data quality, ownership/readiness and command outcomes. Missing measurements remain `null`;
the attributes never substitute a plausible value.

## Radiant and surface models

- **Standard** (default): mean radiant temperature (MRT) equals the representative room-air
  temperature. This deliberately simple assumption is appropriate when no true occupant-weighted
  radiant measurement is available.
- **Mold Indicator**: `mold_indicator_entity` selects a Home Assistant Mold Indicator. ATHB reads
  its `estimated_critical_temp` attribute as the estimated coldest inner-surface temperature. It
  uses that value only for surface-temperature, surface-RH and saturation diagnostics. Comfort
  solving remains on the Standard room model: the critical point is not room MRT and no arbitrary
  view factor is invented.

Home Assistant's Mold Indicator estimates the critical point from its own configured indoor,
outdoor and calibration inputs. ATHB does not duplicate that calibration and does not require a
template sensor for the attribute. If the entity or attribute is missing, invalid or stale, ATHB
does not fabricate a value.

The underlying diagnostic relationship is equivalent to a calibrated inner-surface model:

`T_surface = T_outdoor + f_Rsi × (T_indoor - T_outdoor)`

`f_Rsi` is the Mold Indicator's dimensionless calibration. `surface_rh_threshold_pct` is
diagnostic only: surface RH is calculated at constant indoor vapour pressure and saturation or
non-physical states fail explicitly. Surface temperature, local-air temperature, and MRT remain
separate physical quantities.

## Human and air-movement model

`met` is metabolic activity in met, where 1 met = 58.15 W/m². The default 1.1 met represents
light seated activity. The accepted UI range is 0.8–2.0 met.

Automatic clothing derives dynamic insulation from the adaptive outdoor state. Fixed clothing
uses `fixed_clothing_clo` (0.1–2.0 clo), where 1 clo = 0.155 m²K/W. Fixed values are declarations.

Air speed is either `air_speed_m_s` (0–2 m/s, declared) or a measured entity. Air speed changes
convective and evaporative heat loss and, for globe mode, the MRT conversion. Missing or stale
measured air speed blocks the calculation.

## Measurement freshness

ATHB uses Home Assistant's latest report timestamp (`last_reported` where available) rather than
assuming that an unchanged value is a new physical observation. The advanced wizard exposes a
freshness window from 5 to 360 minutes for each measured source type that is actually selected:
primary room temperature, measured indoor humidity, critical local-air points, measured radiant
or surface temperature, and measured air speed. The safe default is 30 minutes.

A longer window is appropriate for a trustworthy battery sensor that reports only slowly or when
its value changes. It is an observation-hold assumption, not proof that a new measurement occurred.
Once the configured window expires, the source becomes stale and ATHB stops normal calculation and
normal writes. The last valid calculated values remain visible rather than turning unavailable,
but are explicitly marked `data_quality: stale` with `last_valid_at` and `data_age_minutes`; the
input-status entity retains the exact stale reason. ATHB never substitutes a plausible temperature
or humidity. The bounded last-valid display snapshot is stored with the zone's verified state and
restored after an integration reload or Home Assistant restart when the configuration and comfort
level still match. It remains display-only and stale until a valid calculation is available. Older
installations without such a snapshot can still show `restored_stale` entity values until the first
new valid calculation. The outdoor-history collector retains its separate two-hour maximum hold
and builds adaptation only from completed local calendar days.

Only a calculation for which every mandatory environmental input is valid may replace this stored
snapshot. A fresh room temperature combined with stale humidity, radiant data or another mandatory
source therefore cannot erase previously valid room-target scenarios during startup.

When another input changes, ATHB may re-evaluate using the same already accepted primary report
while it remains inside its freshness window. An identical value and timestamp are reuse of one
observation, not a fabricated new report, and never count as a second recovery sample. An older
timestamp or a different value carrying the same timestamp remains invalid.

Home Assistant's unchanged-state `state_reported` event is tracked for measured inputs. A sensor
may therefore renew its freshness by reporting the same physical value with a genuinely newer
`last_reported` timestamp. Climate target feedback remains independent from source validation.
When one climate is both primary source and target, the same Home Assistant report can update its
public `current_temperature` input and acknowledge its target endpoint without conflating roles.

Age-only staleness clears on the first genuinely newer, valid timestamp. Recovery from unavailable,
missing, malformed, out-of-range or implausibly jumping input remains subject to the stricter
multi-report stability gate.

### Stale-measurement safety

One hour after the newest trustworthy primary room-temperature report, ATHB performs a one-shot
safety check. If that last temperature is below the currently observed heating setpoint, it lowers
the setpoint to `fallback_heating_c`. For cooling, the inverse applies and ATHB raises the setpoint
to `fallback_cooling_c`. An atomic range can only be widened toward both fallback limits. If this
would not strictly reduce an existing demand, nothing is sent.

The timestamped, numerically valid Home Assistant state may be reused as safety evidence after an
integration reload, but remains stale and is never admitted to the ATHB comfort calculation. An
unavailable, malformed or physically invalid state cannot supply this reload evidence.

This is not an adaptive calculation from stale data. It is a bounded withdrawal of an already
present demand so a silent room sensor cannot leave a stale high-heating or low-cooling request in
place indefinitely. It still requires enabled control, owned target, supported and available
climate state, the current ATHB lease, matching generations and verified command persistence. The
exact normalized fallback target goes through `CommandBroker`; no HVAC-mode or other climate
setting is included. Manual override and disabled or unavailable targets remain untouched. A fully
valid recovered calculation clears the one-shot state and resumes ordinary event-driven control.

## Comfort and adaptation bounds

`lower_comfort_vote` (-1.0 to -0.05) and `upper_comfort_vote` (+0.05 to +1.0) define the comfort
band in ATHB sensation space. The control band sits inside it according to the selected strategy.

`running_mean_alpha` (0.60–0.90, default 0.80) is the exponential memory weight. Conceptually the
newest eligible day receives weight `(1-α)`, the preceding day `(1-α)α`, and so on, with bounded
normalization over the persisted previous seven local calendar days. Higher alpha retains older
weather longer.

When `reject_extrapolation` is off, a numerically valid result outside the ATHB applicability
range is explicitly flagged. When on, those roots are rejected and control is suppressed if the
required directional roots are no longer available.

## Setback tuning

Setback controls what absence does after sensation-space inverse solving. The wizard and
entity present the choices from maximum saving to greatest comfort, with Custom last:

| Setback | Heating policy | Cooling policy | Intended use |
|---|---|---|---|
| Boundary limit | configured minimum command temperature | configured maximum command temperature | Maximum saving or long absence; no adaptive target is implied. |
| Eco — 4 °C | solved root − 4 °C | solved root + 4 °C | Longer daytime absence. |
| Comfort — 2 °C | solved root − 2 °C | solved root + 2 °C | Short absence or modest savings. |
| Custom | root − `eco_heating_setback_c` | root + `eco_cooling_setback_c` | Expert offsets from 0–5 °C. |

Boundary limit uses the configured minimum and maximum command temperatures directly. Any eligible critical
local-air location can still add
its already bounded, directional correction of at most 2 °C. The result is then intersected with
the configured control bounds and device bounds and rounded inward to the device grid. Existing
entries created before Setback retain their configured offsets through the Custom mode; new entries
default to Comfort — 2 °C. Only the two offsets used by Custom appear, and only when an occupancy
source is configured.

## Everyday control settings

`minimum_control_temperature` and `maximum_control_temperature` are hard user bounds in Celsius
and must be ordered. They are intersected with each climate entity's own limits. They also directly
form the heating and cooling requests for Boundary limit setback. `manual_override_minutes` is 15–1440 minutes.

`boost_delta_c` (0–3 °C) defines the calculated Boost target in the comfort-seeking direction,
capped at thermal neutral. Boost lasts 5–180 minutes according to `boost_duration_minutes`, then
returns to Off. Adaptive requests that target. Rapid first uses the heating maximum or cooling
minimum command bound to create a larger control delta, then holds the same calculated Boost
target after the room reaches it. These five everyday values are shown in normal Setup,
Reconfigure and Options, not hidden behind Advanced. Boost operations occur before final actuator
bounds and grid normalization.

ATHB never changes HVAC mode. For a target already in `auto`, it derives the mapping from public
Home Assistant capabilities:

- range target support maps to the atomic heating/cooling range;
- a scalar target with only `heat` advertised maps to heating;
- a scalar target with only `cool` advertised maps to cooling;
- scalar Auto with both or neither direction advertised is ambiguous and remains fail-safe
  suppressed.

No Auto-mapping choice is shown to the user. Capability inference never causes an HVAC-mode call.

## Command normalization and fallback

`minimum_range_gap` is 1–10 °C and preserves separation between ranged endpoints.
`minimum_meaningful_change` suppresses normalized commands smaller than 0–5 °C.
`feedback_resolution` is the 0–5 °C comparison tolerance between observed target feedback and the
last owned command; increase it only for a device that reports a coarser or noisy target grid.

With insufficient outdoor history, `fixed` uses `fallback_heating_c` and
`fallback_cooling_c` within the hard bounds. `no_write` sends nothing until history qualifies.
Neither mode substitutes current outdoor temperature for the missing running mean. Both fallback
temperatures are always configured and validated because they are also the stale-measurement
safety limits described above; `no_write` affects only insufficient-history behaviour.

## Critical local-air locations

Up to eight separately measured air-temperature locations may be configured. Each has a stable
ID, a sensor, and `monitoring`, `heating`, `cooling`, or `both` eligibility. These sensors must
measure local **air**, not a surface or MRT.

Each location uses the same selected strategy vote as the primary location. Heating can only be
constrained by a directionally eligible location that asks for a higher heating target; cooling
only by one asking for a lower cooling target. Influence is capped at 2 °C and guarded inside the
outer comfort roots. Monitoring locations are diagnostic only.

## Target calibration

Every climate target has its own `calibration_<target UUID>` value, shown by entity name in the
wizard. The accepted range is -3 to +3 °C. The mapping is:

`requested actuator temperature = solved room target + calibration offset`

This corrects a known actuator/setpoint relationship. It does not alter the primary room sensor
and does not merge air, surface, or radiant temperatures.
