# Configuration

## Normal setup

Create one ATHB config entry per zone. Every entry creates one Home Assistant device that groups
the zone's ATHB entities. Select a primary air-temperature entity, an outdoor temperature source,
one indoor-RH mode and one to eight registered climate targets. Measured RH is recommended; fixed
RH may instead be entered directly as a finite 0–100% declaration and needs no helper entity.
Only the field for the selected RH mode is shown. Balanced is the default strategy. New entries
save with adaptive control disabled.

The normal setup explains each input and keeps numerical bounds, fallback behaviour and model
parameters behind **Configure advanced settings**. A final review page summarizes the zone before
creation. Run **Reconfigure** from the integration entry to repeat the measurement and target
selection later while retaining the same zone identity and device.

Configure user command bounds in Celsius. They are intersected with device bounds before grid
normalization. Heating rounds inward upward; cooling rounds inward downward. Ranged targets remain
atomic, ordered and separated by at least the configured gap. An infeasible range is suppressed.

## Climate support

- `heat` with scalar target support: heating root.
- `cool` with scalar target support: cooling root.
- `heat_cool` with range support: heating/cooling control band.
- `auto`: disabled unless an explicit supported directional or ranged mapping is configured.
- `off`, unavailable, restored or unobservable setpoint state: no write.

Changing HVAC mode does not create a manual temperature override, and ATHB never changes the mode.
Changing a target externally does create an override for the configured duration or until Resume.

## Advanced environmental inputs

The default radiant model is uniform and requires no MRT expertise. Selecting an advanced mode
reveals only its relevant fields:

- Direct MRT: a true mean-radiant-temperature measurement.
- Globe: globe temperature, diameter and emissivity.
- Surface: measured or internally modelled surface temperature plus an effective view factor.
- Critical local air: a separately measured air temperature with monitoring/heating/cooling/both
  eligibility.

View factors must be effective occupant factors and sum to at most one. Existing Mold
Indicator-style `f_Rsi` calibration may be entered directly; ATHB does not require or create a
helper solely for that value.

## Outdoor history

ATHB shares one collector for zones using the same outdoor source and local timezone. It persists
up to 35 daily summaries and derives adaptation from the previous seven local calendar days. Seven
eligible days are complete; a qualifying three-or-more-day window is partial; insufficient history
uses the explicitly labelled fixed fallback (or no-write policy). Current outdoor temperature is
never substituted for the adaptation running mean.

Outdoor relative humidity is intentionally not requested. ATHB comfort uses indoor RH; outdoor
adaptation uses only the persisted running mean of outdoor temperature. Surface-risk diagnostics
combine indoor vapor pressure with the selected measured or modelled surface temperature.
