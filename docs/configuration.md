# Configuration

## Normal setup

Create one ATHB config entry per zone. Every entry creates one Home Assistant device that groups
the zone's ATHB entities. Select one primary air-temperature source: a temperature/numeric sensor
(its state is used) or a climate entity (its `current_temperature` attribute is used), an outdoor temperature source,
one indoor-RH mode and one to eight registered climate targets. Measured RH is recommended; fixed
RH may instead be entered directly as a finite 0–100% declaration and needs no helper entity.
Only the field for the selected RH mode is shown. Balanced is the default comfort level. New entries
save with adaptive control disabled.

The comfort-level select runs from Eco through Near neutral. An optional binary occupancy or
schedule source applies the separately selected Setback whenever it is off; without that source,
Setback is not shown or applied. Boost is a separate runtime select with Off, Adaptive and Rapid.
Adaptive bypasses setback and targets extra comfort. Rapid temporarily drives a scalar heat-only
target at the configured maximum (or cooling at the minimum) until the calculated Boost target is
reached, then holds that target. It never changes HVAC mode.

The normal setup explains each input. Its **Everyday control settings** page always shows command
minimum/maximum, manual-override duration and Boost shift/duration. Numerical comfort assumptions,
fallback behaviour and specialist source settings remain behind **Configure advanced settings**.
A final review page summarizes the zone before creation. The device's **Configure** action opens a
short settings menu for room/measurement sources, climate targets, or comfort/control settings.
It covers the same choices without forcing every technical page into one long form. Run
**Reconfigure** from the integration entry when a complete sequential setup walkthrough is
preferred; both routes retain the same zone identity and device.

Setup and Reconfigure expose the same complete set of sources, targets, model choices and expert
parameters. Conditional pages show only values required by the selected humidity, radiant,
clothing and air-speed modes. Fallback heating and cooling temperatures are always retained: they
also bound the stale-input safety action. See the full [technical configuration
reference](configuration-reference.md) for formulas, supported ranges and policy effects.

The device page groups occupant-facing outputs under semantic names such as **Comfort**, **Target**,
**Surface**, and **Weather**. The diagnostic section separates the outer **Comfort range**
(lower limit, neutral reference and upper limit) from the selected inner heating and
cooling **Control points**. Input readiness, control state, and eligibility are also diagnostic
entities. Non-target unique IDs remain stable. The former per-climate scalar target entities are
deliberately replaced by the three room-target entities below; dashboards or automations that
referenced an old scalar target must select the new Current target once after updating.

For a normal scalar heat-only or cool-only zone, **Target — current**, **Target — occupied** and
**Target — unoccupied** show the common room target without repeating climate names. Open
**Target — current** to inspect the decision chain, actual temperature, reported setpoint, limits,
grid and exact normalized ATHB request for every controlled climate. Other ATHB entities also expose relevant source, provenance,
setting and related-value attributes so a displayed result can be traced without adding more
entities to the device page.

Configure user command bounds in Celsius. They are intersected with device bounds before grid
normalization. Heating rounds inward upward; cooling rounds inward downward. Ranged targets remain
atomic, ordered and separated by at least the configured gap. An infeasible range is suppressed.

## Climate support

- `heat` with scalar target support: heating root.
- `cool` with scalar target support: cooling root.
- `heat_cool` with range support: heating/cooling control band.
- `auto`: mapped automatically only when public climate capabilities make the target semantics
  unambiguous; otherwise fail-safe suppressed.
- `off`, unavailable, restored or unobservable setpoint state: no write.

Changing HVAC mode does not create a manual temperature override, and ATHB never changes the mode.
Changing a target externally does create an override for the configured duration or until Resume.

## Stale room-temperature safety

When a previously valid source stops reporting, ATHB keeps the last valid outputs visible and
marks them stale instead of showing a wall of unavailable entities. The input-status entity still
shows the real stale reason. These retained values are display-only: ATHB stops normal calculation
and normal target writes. ATHB persists this bounded display snapshot, so an integration reload or
Home Assistant restart can restore it when the zone configuration and comfort level are unchanged.

After one hour without a new primary room-temperature report, ATHB checks whether the currently
observed climate setpoint still implies heating or cooling demand against the last accepted room
temperature. If so, it sends the configured fallback heating or cooling temperature once, solely
to reduce that demand. The safeguard never increases demand, never changes HVAC mode, respects
manual override and all normal target/ownership limits, and uses the sole CommandBroker. After
age-only staleness, the first genuinely newer valid sensor report returns the zone to normal
event-driven control; invalid or unavailable-source recovery retains the stricter stability gate.

## Room model and advanced environmental inputs

The default **Standard** room model assumes mean radiant temperature (MRT) equals the representative
room-air temperature. This is the recommended choice for normal Home Assistant installations.
The only alternative in Setup, Reconfigure and Options is **Mold Indicator**. Select an existing
Home Assistant Mold Indicator entity and ATHB reads its `estimated_critical_temp` attribute
directly—no template sensor is required.

That critical point drives only surface-temperature, surface-RH and predicted-saturation
diagnostics. It does not influence the comfort targets because a coldest surface point is not the
room's occupant-weighted MRT. The Mold Indicator therefore needs no view-factor setting.

Critical local air remains an advanced option for a separately measured air temperature with
monitoring, heating, cooling or both-direction eligibility. It is not a surface or MRT input.

## Outdoor history

ATHB shares one collector for zones using the same outdoor source and local timezone. It persists
up to 35 daily summaries and derives adaptation from the previous seven local calendar days. Seven
eligible days are complete; a qualifying three-or-more-day window is partial; insufficient history
uses the explicitly labelled fixed fallback (or no-write policy). Current outdoor temperature is
never substituted for the adaptation running mean.

The running-mean sensor therefore normally changes at local-day rollover, after a completed day is
admitted to the previous-seven-day window; it is not intended to drift with every current outdoor
reading. Advanced options separately allow 5–360 minute freshness windows for selected indoor and
radiant measurement sources. The default remains 30 minutes, and extending it declares how long a
slow-reporting value may be held—it does not fabricate a fresh observation.

Outdoor relative humidity is intentionally not requested. ATHB comfort uses indoor RH; outdoor
adaptation uses only the persisted running mean of outdoor temperature. Surface-risk diagnostics
combine indoor vapor pressure with the selected measured or modelled surface temperature.
