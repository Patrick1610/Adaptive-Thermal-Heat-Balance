# Adaptive Thermal Heat Balance

Adaptive Thermal Heat Balance (ATHB) is a local, event-driven Home Assistant custom integration
that calculates adaptive thermal-comfort targets and, when deliberately enabled, owns only the
temperature target of selected climate entities.

ATHB implements the 2022 ATHB formulation in frozen, standard-library-only Python. The runtime
does not depend on NumPy, SciPy, Numba or `pythermalcomfort`; the pinned `pythermalcomfort==4.4.2`
environment is used only to generate and audit independent development goldens.

## Product boundaries

- Strategies are **Efficient**, **Balanced** (default) and **Comfort**. Each solves its own
  sensation-space roots; targets are not interpolated between temperatures.
- Eco strength is independently selectable as **Mild**, **Workday**, **Deep**, or **Custom**;
  profile policy is applied only after the adaptive roots have been solved.
- Thermal neutral is a reference. Heating, cooling and ranged control use the selected strategy's
  heating/cooling control roots.
- ATHB calls only `climate.set_temperature`. It never turns equipment on or off and never changes
  HVAC mode, preset, fan, swing or humidity settings.
- An external temperature-target change creates a manual override. An external HVAC-mode change
  only causes capability/reconciliation handling.
- The ordinary setup uses the uniform-radiant approximation. Direct MRT, globe temperature,
  measured/modelled surfaces and critical local-air locations are optional advanced inputs and
  remain physically distinct.
- Missing mandatory measurements are never replaced with plausible values. Direct fixed RH and
  air-speed inputs remain explicitly `declared`.

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
