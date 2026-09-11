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

## Comfort strategy and profile

The comfort band is defined by `lower_comfort_vote` and `upper_comfort_vote`, default -0.5 and
+0.5 ATHB sensation. Strategy targets are solved directly in sensation space:

| Strategy | Inward fraction | Default heating vote | Default cooling vote | Meaning |
|---|---:|---:|---:|---|
| Efficient | 0.30 | -0.35 | +0.35 | More of the comfort band remains available. |
| Balanced | 0.50 | -0.25 | +0.25 | Recommended energy/comfort compromise. |
| Comfort | 0.70 | -0.15 | +0.15 | Targets remain closer to thermal neutral. |

The temperatures are **not** interpolated. Each vote is independently inverse-solved while vapour
pressure remains constant. Thermal neutral (vote 0) is a reference, not the normal actuator
target.

`comfort` uses the solved strategy targets. `eco` subtracts the heating setback and adds the
cooling setback. `boost` temporarily shifts both sides toward comfort by `boost_delta_c`, bounded
by policy, and expires after `boost_duration_minutes`. `auto` resolves to Comfort or Eco from the
optional occupancy source; unknown occupancy is held briefly and then resolves conservatively.

## Radiant and surface models

- **Uniform** (default): mean radiant temperature (MRT) equals local air temperature.
- **Direct MRT**: `mrt_entity` must measure mean radiant temperature, not a surface temperature.
- **Globe**: MRT is derived from globe temperature, air temperature, air speed,
  `globe_diameter_m`, and `globe_emissivity`. Diameter and emissivity control the convective and
  radiative correction.
- **Surface**: a cold surface is either measured or modelled and mixed into MRT using the bounded
  `surface_view_factor`. All remaining radiant surroundings use the uniform assumption.

For a modelled surface:

`T_surface = T_outdoor + f_Rsi × (T_indoor - T_outdoor)`

`surface_f_rsi` is a dimensionless existing calibration and must be greater than zero and at most
one. `surface_rh_threshold_pct` is diagnostic only: surface RH is calculated at constant indoor
vapour pressure and saturation or non-physical states fail explicitly. Surface temperature,
local-air temperature, and MRT remain separate physical quantities.

## Human and air-movement model

`met` is metabolic activity in met, where 1 met = 58.15 W/m². The default 1.1 met represents
light seated activity. The accepted UI range is 0.8–2.0 met.

Automatic clothing derives dynamic insulation from the adaptive outdoor state. Fixed clothing
uses `fixed_clothing_clo` (0.1–2.0 clo), where 1 clo = 0.155 m²K/W. Fixed values are declarations.

Air speed is either `air_speed_m_s` (0–2 m/s, declared) or a measured entity. Air speed changes
convective and evaporative heat loss and, for globe mode, the MRT conversion. Missing or stale
measured air speed blocks the calculation.

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

## Profile tuning

`eco_heating_setback_c` lowers heating and `eco_cooling_setback_c` raises cooling, each 0–5 °C.
`boost_delta_c` (0–3 °C) shifts in the opposite, comfort-seeking direction. Boost lasts 5–180
minutes. These policy operations occur after sensation-space inverse solving and before final
actuator bounds and grid normalization.

## Control limits and Auto mapping

`minimum_control_temperature` and `maximum_control_temperature` are hard user bounds in Celsius
and must be ordered. They are intersected with each climate entity's own limits.
`manual_override_minutes` is 15–1440 minutes.

ATHB never changes HVAC mode. `auto_mapping` only interprets a target already in `auto`:

- `unmapped`: fail-safe; no command.
- `heating`: use the scalar heating root.
- `cooling`: use the scalar cooling root.
- `range`: require ranged capability and use both directional roots.
- `bidirectional_scalar`: use current directional demand with scalar capability.

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
