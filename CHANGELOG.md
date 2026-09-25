# Changelog

## 0.2.14

- Show target-grid rounding directly on the **Controlled climate entities** page in setup,
  reconfiguration and options, next to the targets whose commands it affects.
- Remove the duplicate rounding choice from the advanced command-behaviour page while preserving
  the existing setting and runtime behaviour.

## 0.2.13

- Add an advanced target-grid rounding choice: Ceiling, Floor or Mathematical nearest-value
  rounding. The selected policy applies consistently to scalar and heat/cool range targets.
- Keep the established heating-up/cooling-down policy for existing entries until the new setting
  is saved; new and reconfigured entries default to Mathematical rounding.

## 0.2.12

- Defer entity-registry removal repairs for 30 seconds and revalidate configured sources and
  targets against both the registry and loaded states, preventing transient startup removals from
  producing persistent false notifications.
- Reconcile an already persisted removal repair after restart or reload, while still reporting an
  entity that remains genuinely absent after the settling period. Runtime input and target safety
  checks remain immediate and unchanged.

## 0.2.11

- Determine temperature and measured-humidity freshness from the newest valid `last_reported`
  timestamp when both sources share one Home Assistant device; keep their values and validity
  independent.
- Offer optional same-device activity sources such as enabled ZHA LQI or RSSI, with exact
  device/integration allowlists that exclude helpers and derived sensors.
- Keep HeatGuard temperature feedback unchanged, avoid recalculation storms for ordinary liveness
  reports, and expose own versus effective freshness evidence in diagnostics.

## 0.2.10

- Continue calculating and updating setpoints from a last valid but quiet primary temperature
  source, while marking those outputs as stale projections.
- Guard possible heating demand with a 30-minute rolling feedback deadline or one 60-minute
  stale-start trial. A genuine same-value report renews feedback; an ATHB setpoint acknowledgement
  from a climate used as the primary source does not.
- Withdraw heating demand gradually toward the configured fallback temperature after feedback
  expires, with a 30-minute maximum ramp and durable deadlines across restart and reload.
- Expose advanced heat-feedback settings and per-target guard diagnostics. Continue to use the
  CommandBroker, ownership and lease checks for every safety command; leave cooling control
  and HVAC mode unchanged.

## 0.2.9

- Re-adopt persistent Home Assistant Repair issues after restart or reload, so recovered
  conditions remove their existing notifications while unresolved conditions remain visible.
- Reconcile the explicit-control-recovery warning on every calculation and remove all
  zone-specific Repair issues when its config entry is deleted.
- Add regression coverage for restart/reload adoption, automatic recovery and config-entry
  removal.

## 0.2.8

- Group user-facing outputs under **Comfort range**, **Control point**, **Room**, **Surface** and
  **Target**, while keeping only technical status entities diagnostic and preserving existing
  entity unique IDs.
- Define room deviations consistently as current room temperature minus the named target or
  neutral reference, including one-time conversion of restored pre-0.2.8 target deviations.
- Add a fixed 80% **Surface — high humidity** warning and clarify that the existing 100% signal is
  **Surface — condensation risk**; legacy configurable warning thresholds no longer affect the
  runtime calculation.

## 0.2.7

- Use the concise **Target — deviation from current** name when a zone has only one control
  direction; retain explicit heating and cooling names when both directions are present.
- Keep the direction-specific entity unique ID and signed calculation unchanged.

## 0.2.6

- Rename the signed neutral comparison to **Comfort range — deviation from neutral**.
- Add direction-specific **Target — heating/cooling deviation from current** sensors selected from
  climate capabilities: heat-only exposes heating, cool-only exposes cooling and `heat_cool`
  exposes both distances to its active lower and upper targets.

## 0.2.5

- Keep the descriptive outer comfort range available beyond the constant-moisture saturation
  boundary without weakening moisture constraints on control points or climate commands.
- Add diagnostic **Comfort range — current** and **Comfort range — neutral delta** sensors; the
  signed delta is the validated primary indoor temperature minus the neutral reference.

## 0.2.4

- Exercise all seven anonymized diagnostic configurations through one parametrized Home Assistant
  lifecycle test covering startup, invalid or stale mandatory inputs, and automatic recovery.
- Verify fixed-fallback and no-write behavior, recovery without reload or Resume, and the exact
  target-only climate service payload for every applicable scenario.

## 0.2.3

- Coalesce repeated reports and sub-deadband sensor noise before scheduling a new thermal
  calculation: 0.05 °C for indoor/radiant inputs, 0.1 °C outdoors, 0.5 percentage point RH and
  0.02 m/s air speed.
- Compare small changes with the last materially processed value, so cumulative movement always
  crosses the threshold and triggers a calculation.
- Never suppress first observations, availability/validity-shape transitions, target capability
  changes, freshness-expiry checks or command acknowledgements.
- Expose the bounded count of coalesced source reports in runtime diagnostics for field analysis.

## 0.2.2

- Clear only the obsolete v0.2.0 `unclean_restart` Resume gate during startup migration; unresolved
  commands, command faults and other explicit recovery gates remain fail-closed.
- Publish ownership, readiness, eligibility and Resume changes immediately instead of waiting for
  a later environmental calculation.
- Expand the final setup and reconfigure review with freshness-aware primary, humidity and outdoor
  source checks plus a concise list of sources needing attention.
- Add privacy-preserving per-source freshness/availability evidence and active Repair conditions to
  downloaded diagnostics while retaining the Home Assistant entity domain.
- Use 15/30 °C command limits, a 2 °C Boost shift and 18/27 °C fallback temperatures as defaults for
  newly configured values; existing explicitly stored settings remain unchanged.
- Add a seven-zone, anonymized and deliberately varied lifecycle regression matrix based on field
  diagnostics, while preserving the supplied living-room setup as the baseline scenario.

## 0.2.1

- Reconcile automatically after clean or unclean restart, reload, upgrade and ordinary
  configuration or comfort-policy changes when no unresolved command or persisted recovery gate
  exists.
- Keep explicit Resume fail-closed for unresolved commands, corrupt storage and persisted command
  recovery states.
- Persist the current configuration fingerprint and comfort strategy with runtime control changes,
  including occupancy setback changes.
- Report all startup recovery causes separately instead of masking configuration drift as an
  unclean restart.

## 0.2.0

- Accept a temperature/numeric sensor state or a climate `current_temperature` attribute as the
  single validated primary room-temperature source, including °C, °F and K conversion.
- Keep source recalculation and target acknowledgement independent when one climate serves both
  roles.
- Add a conditional resolved-occupancy binary sensor with bounded unknown-state hold evidence.
- Expand Target — current with a structured decision chain and per-climate observation,
  normalization, ownership and command context.
- Add live source/capability information to the final setup/reconfigure review and a persistent
  Resume-required Repair that clears after recovery.

## 0.1.18

- Apply the first valid target after start, reload or mandatory-input recovery without carrying
  forward an obsolete environmental-slew baseline.
- Treat occupancy, setback, comfort level, Boost, Enable and Resume as explicit one-shot policy
  transitions while retaining slew for ordinary sensor changes.
- Expose startup recovery reason, Resume requirement and transition reasons in runtime state.
- Qualify against Home Assistant 2026.9.0 and 2026.9.2.
