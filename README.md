# Adaptive Thermal Heat Balance

Adaptive Thermal Heat Balance (ATHB) is a local, event-driven Home Assistant custom integration
that calculates adaptive thermal-comfort targets and, when deliberately enabled, owns only the
temperature target of selected climate entities.

ATHB implements the 2022 ATHB formulation in frozen, standard-library-only Python. The runtime
does not depend on NumPy, SciPy, Numba or `pythermalcomfort`; the pinned `pythermalcomfort==4.4.2`
environment is used only to generate and audit independent development goldens.

## Product boundaries

- The comfort levels are **Eco**, **Efficient**, **Balanced** (default), **Comfort** and
  **Near neutral**, ordered from greatest efficiency to greatest comfort. Each solves its own
  sensation-space roots; targets are not interpolated between temperatures.
- An optional occupancy or schedule entity applies **Setback** whenever it is off, at every
  comfort level. Setback is **Max**, **Eco — 4 °C**, **Comfort — 2 °C**, or **Custom**.
- **Boost** is separate: Adaptive moves to a bounded calculated Boost target; Rapid uses the
  command limit until that target is reached and then holds it. Expiry returns Boost to Off.
- Thermal neutral is a reference. Heating, cooling and ranged control use the selected strategy's
  heating/cooling control roots.
- ATHB calls only `climate.set_temperature`. It never turns equipment on or off and never changes
  HVAC mode, preset, fan, swing or humidity settings.
- An external temperature-target change creates a manual override. An external HVAC-mode change
  only causes capability/reconciliation handling.
- The ordinary setup offers a standard uniform-radiant room model or an existing Home Assistant
  Mold Indicator. Its calculated critical point is used only for cold-surface diagnostics and is
  never mistaken for room mean radiant temperature (MRT). Critical local-air locations remain
  optional advanced inputs and physically distinct.
- Missing mandatory measurements are never replaced with plausible values. Direct fixed RH and
  air-speed inputs remain explicitly `declared`.
- A stale measurement stops normal adaptive writes while the last valid outputs remain visibly
  marked stale. After one hour, a one-shot safeguard may only reduce existing demand to the
  configured fallback temperature; it still uses the sole CommandBroker and never changes mode.

## Installation and configuration

See [installation](docs/installation.md), [configuration](docs/configuration.md), and the
[technical configuration reference](docs/configuration-reference.md). Software
completion is repository-validated; no live Home Assistant installation or physical-device test
is claimed.

For HACS testing, add this public repository as a custom **Integration** repository and install
the latest published release. Restart Home Assistant, then add **Adaptive Thermal Heat Balance**
through Settings → Devices & services. No live installation is part of the repository test suite.
Each configured thermal zone appears as one Home Assistant device. The normal flow shows only the
inputs required by the selected source mode; advanced numerical and radiant settings remain behind
explicit progressive-disclosure choices.

## Documentation

- [Scientific model](docs/scientific-model.md)
- [Configuration](docs/configuration.md)
- [Technical configuration reference](docs/configuration-reference.md)
- [Operations and troubleshooting](docs/operations.md)
- [Validation](docs/validation.md)
- [Normative architecture](ATHB_ARCHITECTURE_PLAN.md)
- [Normative build clarifications](ATHB_BUILD_CLARIFICATIONS.md)

## Development validation

```bash
python -m pytest
python -m pytest tests/virtual_installations -v \
  --athb-report=artifacts/virtual-installations.md
ruff format --check custom_components tests tools
ruff check .
mypy
```

The virtual-installation command produces Markdown, JSON and assertion-summary evidence without
contacting a household Home Assistant instance or external golden service.

## License

ATHB is available under the [MIT License](LICENSE). The separately retained
`LICENSES/pythermalcomfort-4.4.2.txt` notice applies to the adapted upstream numerical kernel.
