# Changelog

## 0.3.2

- Add an optional per-target temperature-step override to device calibration settings.
  Existing instances keep automatic device precision; compatible virtual targets can use 0.1.
- Apply the selected grid consistently to normal, Boost/preheat, fallback and safety commands,
  and expose configured and effective precision in current-target diagnostics.


## 0.3.1

- Remove the Page action selector and per-section discard machinery from all Configure pages.
  Completing a section retains a draft and returns to the settings menu; only Save and close
  applies the complete draft. Closing the dialog still abandons all unapplied changes.
- Preserve required-field validation, existing instance identities, configuration and runtime
  behavior. No migration or change to heating calculations, Boost or learning is introduced.

## 0.3.0

- Base Boost on thermal neutral. The signed offset defaults to 0 °C: +1 means neutral +1 for
  heating and neutral -1 for cooling; negative offsets stop before neutral. Existing configured
  offsets are retained, but their meaning changes from a comfort-target shift to a neutral offset.
- Add a configurable Rapid approach margin (default 1 °C). Rapid hands off to the executable,
  bounded and rounded Boost goal before reaching it; the existing persisted one-way latch and
  expiry prevent maximum-drive re-entry or timer extension after restart. Mixed/ranged climates
  retain adaptive coordination and minimum separation, never opposing maximum requests.
- Permit scalar Boost from a valid neutral root when an unrelated directional Eco root is
  moisture-limited. Preheat may use its timestamped occupied reference in that heating-only case;
  real source validity, cooling dewpoint rules, bounds, ownership and the broker remain intact.
- Replace the three-route Options wizard with task-based settings sections, per-section discard,
  draft-only navigation and one final validated save/reload. Native HA uses a page-action choice
  for Back without changes; closing the dialog discards the unapplied draft. Existing identities,
  runtime state and third-party integrations are not recreated or changed.
- Keep the default maximum command at 30 °C. A 35 °C maximum is an explicit installation choice,
  bounded by each device, not a global migration or a boiler-water temperature setting.

## 0.2.34

- Allow timed heating-only Boost and Preheat with unavailable/stale/recovering measured RH,
  using the last valid occupied comfort goal rather than a sent idle/setback target. Rapid can
  use its explicit maximum and expiry without a prior comfort reference; Adaptive never invents
  a neutral reference or an RH measurement.
- Keep cache timestamps, normal automatic operation, cooling/dewpoint protection, fresh primary
  requirements, bounds, HeatGuard, ownership and the sole broker write path intact. Expose the
  manual fallback, actual RH failure and reference timestamp in existing diagnostics/attributes.
- Journal downward-only withdrawal on manual stop/expiry, including restart reconciliation and
  storage failure handling. No new entities, setup choices, dependencies or config-entry migration.

## 0.2.33

- Add an opt-in numeric central-demand source and an explicit positive threshold with one decimal.
  The source is compared without rounding, inclusively (`value >= threshold`), and invalid or
  unavailable values never grant an assisted start.
- Let effectively occupied/preheated rooms with a positive, executable heating shortfall join
  below the ordinary start threshold. Reuse the existing restart-safe demand latch and stop
  threshold so a room may finish after central demand falls.
- Keep default operation, setbacks, cooling, Boost/comfort protection, ownership, command guards,
  and existing instances unchanged. Add source-change recalculation and existing diagnostics,
  without new entities, timers, dependencies, or automatic configuration changes.

## 0.2.32

- Preserve command hysteresis when Preheat starts, renews, stops or expires without changing
  the effective comfort/setback profile. A timer-only change no longer releases a held idle
  candidate and causes a temporary setpoint dip.
- Keep immediate comfort/setback transitions through the existing occupancy resolver, together
  with normal recalculation, timer renewal, persistence and all command/ownership/input guards.
- Add transition and end-to-end expiry regressions; no new settings, entities or thresholds.
- Diagnostics now label genuine Preheat-driven profile transitions as `occupancy`; timer-only
  changes no longer add a `preheat` entry to `transition_reasons`.

## 0.2.31

- Start normal heating demand from the unrounded, bounded policy target in room coordinates,
  rather than letting actuator-grid rounding change the configured activation shortfall.
- Keep the existing normalized-target stop threshold, rounded writes, comfort/Boost exceptions
  and ownership/input guards. Do not activate an already satisfied rounded target.
- Expose the unrounded activation target and shortfall in existing demand diagnostics and clarify
  setup descriptions; no new entity or setting is introduced.

## 0.2.30

- Protect effectively occupied rooms below the calculated lower comfort limit from ATHB's
  small-heating-demand suppression. Retain the normal rounded target, reusing the existing demand
  latch and configured stop margin for recovery; no downstream-dependent target increase.
- Capture effective occupancy with the calculation snapshot, retain all input/ownership/write
  safeguards, and explain comfort activation and recovery in existing demand diagnostics.

## 0.2.29

- Keep **Target — current** and its per-climate current target at the established acknowledged
  setpoint when the command broker retains it because of quantization hysteresis or minimum
  change. Apply the same broker decision before publishing, including last-valid display storage.
- Preserve the internal calculation candidate and occupied/unoccupied comfort scenarios. The
  candidate can still be released and written by the existing command path when the guard clears;
  no new sensor, threshold or controller behavior is introduced.

## 0.2.28

- Publish continuously selected scalar heating/neutral/cooling targets even when the outdoor
  running mean is only a diagnostic estimate. Label them `diagnostic_preview` / `preview_only`
  and keep them structurally separate from executable commands; history eligibility is unchanged.
- Keep previews visible with HVAC off or Adaptive control disabled. When the climate is enabled,
  recalculate and reconcile before writing the latest target if all normal control gates pass.
- Explicitly label restored climate-target sensor values as `restored_hold` / `restored_stale`
  instead of presenting an old target as a current calculation when comfort values are available.
- Add regression coverage for estimated heating/cooling/neutral previews, disabled/off climates,
  expired failure holds, history recovery, HVAC activation and restored-target replacement.

## 0.2.25

- Separate climate-write failures from controller ownership: service errors, missing feedback and
  rejected automatic commands no longer latch `command_fault` or require Resume.
- Try each normalized scalar or atomic range target at most five times, with 60 seconds between
  retries. Report `retry_pending` / `write_failed`, attempts, timestamps and cause in diagnostics
  and target attributes. A new normalized target or target recovery can start a fresh series.
- Recalculate current inputs and policy on retry, rate-limit changing goals, and verify live
  setpoints before dispatch so genuine external interventions remain manual overrides.
- Recognize late feedback from the last five uncertain writes without acknowledging a different
  newer target. Keep explicit user actions and ownership/capability/external-revision guards.
- Suspend pending delivery on target unavailability without creating a manual override. Restore
  existing overrides with their original expiry, reconcile unresolved journals without replay,
  and automatically migrate ordinary legacy write-fault latches. Hard storage/recovery gates remain.

## 0.2.24

- Correlate delegated climate service calls and target feedback with the exact, actually
  dispatched ATHB command, even when the controller creates a new Home Assistant context.
- Keep acknowledgement valid across newer measurements and comfort-setting changes without
  weakening dispatch-generation checks, capability/ownership guards or the 30-second deadline.
- Ignore bounded duplicate command echoes while preserving explicit user intervention, differing
  external targets and unresolved command-fault protection.

## 0.2.23

- Add a per-zone Preheat switch that temporarily selects occupied comfort without changing or
  hiding the configured occupancy source.
- Expire Preheat after a configurable 5–180 minutes (60 minutes by default), renew the interval
  when it is selected again, and restore only the remaining time after restart.
- Expose natural and effective occupancy, the Preheat reason and exact expiry in entity attributes
  and diagnostics while retaining all normal input, ownership and heating-demand safeguards.

## 0.2.22

- Add a restart-safe heating-demand buffer before downstream climate controllers. By default ATHB
  exposes the calculated heating target at a 0.5 °C room shortfall and withdraws it at 0.1 °C,
  using a downward-rounded idle target while heating demand is inactive.
- Make both demand thresholds configurable, preserve an atomic range target's cooling endpoint,
  bypass the buffer during Boost, and expose the complete decision in entity attributes and
  diagnostics.
- Label every intermediate setup and options page **Next**, retaining **Submit** only for the final
  step.

## 0.2.21

- Tolerate a temporarily absent source-observation payload while Home Assistant is adding ATHB
  entities during startup, instead of failing the entity platform.
- Serialize manual-override expiry timestamps in decision traces without weakening strict JSON
  validation for other trace values.

## 0.2.20

- Let high-frequency temperature sources leave jump quarantine after one minute of mutually
  consistent reports by retaining the first stable report as the time anchor.
- Let those sources complete strict recovery after 30 seconds without weakening the existing
  jump, consistency or availability checks.

## 0.2.19

- Let Automatic outdoor hold learn from continuously reporting high-frequency sensors by
  coalescing consecutive valid reports into cadence intervals of at least one minute.
- Keep every report available for coverage and liveness while preserving invalid-report and
  six-hour-gap boundaries in the cadence learner.

## 0.2.18

- Collect unchanged outdoor-temperature reports through Home Assistant `state_reported`, so a
  reporting sensor can extend daily running-mean coverage without requiring a value change.
- Add Automatic and Fixed outdoor report holds. Automatic learns a bounded cadence from live
  reports; existing zones migrate conservatively to Fixed 120 minutes.
- Use Recorder `last_updated` and distinct `last_reported` anchors during bootstrap, persist the
  active hold policy, and expose cadence, expiry and coverage evidence in diagnostics.

## 0.2.17

- Apply quantization release hysteresis at the grid boundary belonging to Ceiling, Floor or
  Mathematical rounding. A less-demanding Floor heating target can now be dispatched after the
  established 0.1 °C anti-chatter margin instead of being blocked for an additional grid step.

## 0.2.16

- Recalculate immediately when the configured occupancy source changes state, so occupied and
  unoccupied targets switch without waiting for another measurement event.
- Ignore attribute-only updates whose occupancy state is unchanged, avoiding unnecessary
  recalculations.

## 0.2.15

- Show the normalized, grid-rounded actuator request on **Target — current** while retaining the
  unrounded room-policy values on **Target — occupied** and **Target — unoccupied**.
- Remove the duplicated **Room — current temperature** entity and clean its existing entity-registry
  entry on upgrade; the configured source sensor or climate remains the room-temperature entity.

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
