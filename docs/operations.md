# Operations and troubleshooting

ATHB is push-only. Source, target, option and expiry events trigger a debounced calculation; there
is no periodic ten-minute control loop. One calculation runs per zone, at most two run globally,
and only the newest queued snapshot survives an event burst.

## Status and “Why this temperature?”

Entity state stays compact. **Target — current** shows the common normalized setpoint ATHB offers
to the climate and exposes the active scenario, comfort/setback/Boost policy, pre-slew and
requested values, quality/recovery state, and per-climate temperature,
setpoint, bounds, grid, normalized command and outcome as native attributes. Downloaded diagnostics contain the latest coherent decision and at
most 20 material traces: source provenance and validity, history quality, radiant assumptions,
comfort-level votes, attempted roots, occupancy/Boost and critical transforms, normalized target, ownership and
the command or exact suppression reason. Household identifiers are consistently pseudonymized;
user/context identity, coordinates, URLs, arbitrary attributes and raw occupancy history are
removed.

The `heating_demand` decision on **Target — current** explains whether the calculated heating
request is active or replaced by an idle target, the room shortfall, configured start/stop
thresholds, Boost override and both desired and final actuator targets. The demand latch is
persisted before a transition command and restored on restart.

## Common suppression reasons

- `hvac_off`: ATHB does not turn the target on.
- `unsupported_auto_mapping`: the target exposes ambiguous scalar semantics in `auto`; select a
  concrete supported HVAC mode on the climate device. `unsupported_hvac_mode` means the current
  mode/target shape is unsupported.
- `target_unavailable` / `restored_target_state`: wait for an authoritative live target state.
- `manual_override`: an external temperature target owns the actuator until expiry or Resume.
- `running_mean_unavailable`: adaptive data is insufficient; fixed fallback/no-write policy applies.
- `missing_required_root:*`: the root needed for that actuator direction did not solve.
- `no_legal_inward_target` / `control_band_too_narrow`: bounds, grid or required gap are infeasible.
- `storage_verification_failed`: control dispatch is inhibited because the recovery journal could
  not be saved and read back exactly.
- `retry_pending`: automatic delivery has not been confirmed; ATHB will retry a fresh calculation.
- `write_failed`: five attempts at the same normalized target have not been confirmed. Ownership
  is retained, no Resume is required, and a new normalized target can start a new attempt series.

## Climate-write retries

ATHB makes at most five service calls for each normalized actuator target (initial call plus four
retries), normally at 0, 60, 120, 180 and 240 seconds. It waits up to 30 seconds for target feedback
after each dispatch. A service exception is not proof that the target was unchanged: late feedback
can still confirm delivery. Matching live feedback, not successful service return alone, is success.

After the fifth unconfirmed attempt ATHB reports `write_failed` rather than taking ownership away
or waiting indefinitely for Resume. A genuinely new grid-rounded target, including a lowering,
gets a new budget, but cannot bypass the 60-second interval while recovering a write error.
Calculation changes that normalize to the same scalar target or the same two range endpoints do
not reset the budget. Ordinary successfully acknowledged explicit policy changes retain the
existing 10-second hard minimum. Retry timers capture a fresh snapshot and recalculate; they do
not replay an old queued target. Every attempt retains normal input, lease, capability, bounds,
HeatGuard and ownership checks. Before sending, the live setpoint must match a known baseline,
confirmed ATHB target or an uncertain ATHB request; an unexpected external value stops writing.

The `command_delivery` attributes and diagnostics report per-target status, attempts, maximum
attempts, retry interval, last error, last send, last acknowledgement and next retry time. Counters
are runtime-local: a restart starts with live reconciliation, not replay or restored retry timers.

## Recovery and removal

An ATHB comfort, Boost or Preheat change is not a manual intervention. Some controllers delegate
an ATHB temperature command using a fresh Home Assistant context. Within the original 30-second
acknowledgement window, ATHB recognizes an exact dispatched scalar target (or both range endpoints)
using the existing feedback tolerance, provided capability and ownership revisions are unchanged.
New calculation/input generations do not invalidate feedback for an already dispatched command;
they still invalidate unsent intents. A matching service call alone does not acknowledge success:
target-state feedback is required. Successful feedback and bounded duplicate echoes are inferred
correlations, not proof of a particular external caller's identity. Explicit user-context calls
and differing external targets remain interventions. No arbitrary grace period suppresses them.
ATHB additionally retains at most five uncertain dispatched commands until target recovery or
Resume. Exact late echoes use the same tolerance and revision checks, never acknowledge a different
newer command, and never suppress explicit user-context intervention. If an older write arrives
after the latest target was confirmed, ATHB schedules a fresh, rate-limited correction of the
latest goal instead of suppressing it as unchanged. A context-free exact match
is an inferred correlation, not proof of who changed the target. `unavailable`, `unknown`, removed
or restored target states do not create a manual override. They suspend delivery; existing manual
overrides remain intact. The original expiry is retained across restart and unavailability.

Clean and unclean restarts, reloads, upgrades and ordinary configuration or comfort-policy changes
reconcile live target state automatically before any new command; persisted commands are never
replayed. An unresolved journal alone no longer requires Resume. Ordinary legacy command faults
(`command_outcome_unknown` / `coerced_or_rejected`) are migrated to live reconciliation. Resume
remains fail-closed for corrupt control storage or another explicit persisted hard recovery gate;
existing manual overrides are not cleared by the upgrade. `recovery_reason` identifies the primary
cause and `recovery_reasons` lists every detected startup condition. Disabling or unloading ATHB
closes the dispatch gate and releases listeners, timers, tasks, leases and shared-source references;
it does not turn climate equipment off. Removing a zone removes only its zone state and never
unrelated Recorder/helper data.

When Resume remains required for one hour, ATHB creates a Repair with the recovery context and a
recommended check. It is removed automatically after safe reconciliation.

The environmental slew limiter applies only to ordinary measured changes. A start or reload, the
first trustworthy calculation after an invalid mandatory input, an occupancy/setback change, a
comfort-level or Boost change, Enable and Resume are explicit transitions. Their first valid
calculation bypasses environmental slew once so the current target immediately represents the
active policy. An invalid or recovering calculation cannot consume that one-shot transition.
