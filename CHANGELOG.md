# Changelog

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
