# Operations and troubleshooting

ATHB is push-only. Source, target, option and expiry events trigger a debounced calculation; there
is no periodic ten-minute control loop. One calculation runs per zone, at most two run globally,
and only the newest queued snapshot survives an event burst.

## Status and “Why this temperature?”

Entity state stays compact. **Target — current** exposes the active scenario, comfort/setback/Boost
policy, pre-slew and requested values, quality/recovery state, and per-climate temperature,
setpoint, bounds, grid, normalized command and outcome as native attributes. Downloaded diagnostics contain the latest coherent decision and at
most 20 material traces: source provenance and validity, history quality, radiant assumptions,
comfort-level votes, attempted roots, occupancy/Boost and critical transforms, normalized target, ownership and
the command or exact suppression reason. Household identifiers are consistently pseudonymized;
user/context identity, coordinates, URLs, arbitrary attributes and raw occupancy history are
removed.

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

## Recovery and removal

Clean and unclean restarts reconcile live target state before any new command; persisted commands
are never replayed. Ambiguous or corrupt control state requires Resume. Disabling or unloading ATHB
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
