# ATHB configuration reference

This is the technical companion to the Home Assistant setup and reconfigure wizards. Every
wizard page links here. Defaults are deliberately conservative; changing a value changes either
the physical comfort calculation, product policy, or command safety. ATHB owns temperature
targets only. It never changes HVAC mode, power, fan, preset, swing, or humidity.

## Environmental inputs

`primary_temperature` is the representative indoor **air** temperature. A selected source must
report a finite temperature with a recognised temperature unit and a fresh observation. Indoor
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
| Near neutral | 0.85 | -0.075 | +0.075 | Greatest normal comfort without making neutral the target. |

The temperatures are **not** interpolated. Each vote is independently inverse-solved while vapour
pressure remains constant. Thermal neutral (vote 0) is a reference, not the normal actuator
target.

An optional binary occupancy or schedule source applies Setback at every comfort level: `on`
uses the solved roots unchanged and `off` widens them using the selected Setback. Without a source,
no setback is applied. Unknown input retains the last state for 30 minutes and then assumes
occupied/no setback with `occupancy_unknown`. The Setback page and entity exist only when a source
is configured.

Boost is an independent runtime select. **Off** follows comfort level and occupancy. **Adaptive**
bypasses setback and shifts the directional target by `boost_delta_c` toward, but not past,
thermal neutral. **Rapid** requests the maximum command temperature for scalar heating (minimum
for scalar cooling) until the calculated Adaptive Boost target is reached, then holds that target.
Rapid falls back explicitly to Adaptive for an atomic range or a zone with separate heating and
cooling targets. Boost expires to Off. Comfort-level, Setback and Boost changes are lightweight
and do not reload the config entry.

The **Heating control target**, **Thermal neutral**, and **Cooling control target** sensors expose
the inverse-solved ATHB roots before occupancy or Boost policy. They therefore change with comfort
level, but not with Setback or Boost. A climate-specific **effective temperature** (or effective low/high)
is the final request after occupancy/Boost, critical-location policy, calibration, bounds, and device-grid
normalization. It is a preview even while control is disabled. The actual climate target changes
only when adaptive control is enabled and ownership, capability, override, and broker gates allow
a write. Its state attributes distinguish `adaptive`, `fallback`, and `unavailable` mode and give
the fallback or suppression reason. Consequently a fixed effective target with Input status
`running_mean_unavailable` is explicit fallback behaviour, not a responsive ATHB result.

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
Once the configured window expires, the source becomes stale and dependent ATHB values become
unavailable; ATHB never substitutes a plausible temperature or humidity. The outdoor-history
collector retains its separate two-hour maximum hold and builds adaptation only from completed
local calendar days.

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
| Max | configured minimum command temperature | configured maximum command temperature | Maximum saving or long absence; no adaptive target is implied. |
| Eco — 4 °C | solved root − 4 °C | solved root + 4 °C | Longer daytime absence. |
| Comfort — 2 °C | solved root − 2 °C | solved root + 2 °C | Short absence or modest savings. |
| Custom | root − `eco_heating_setback_c` | root + `eco_cooling_setback_c` | Expert offsets from 0–5 °C. |

Max uses the configured minimum and maximum command temperatures directly. Any eligible critical
local-air location can still add
its already bounded, directional correction of at most 2 °C. The result is then intersected with
the configured control bounds and device bounds and rounded inward to the device grid. Existing
entries created before Setback retain their configured offsets through the Custom mode; new entries
default to Comfort — 2 °C. Only the two offsets used by Custom appear, and only when an occupancy
source is configured.

## Everyday control settings

`minimum_control_temperature` and `maximum_control_temperature` are hard user bounds in Celsius
and must be ordered. They are intersected with each climate entity's own limits. They also directly
form the heating and cooling requests for Max setback. `manual_override_minutes` is 15–1440 minutes.

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
Neither mode substitutes current outdoor temperature for the missing running mean.

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
