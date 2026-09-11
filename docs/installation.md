# Installation

## Supported software baseline

The initial integration baseline is Home Assistant 2026.9.0. Repository contract tests also run
against the current explicitly pinned supported version in CI. Compatibility with untested future
Home Assistant releases is not implied.

## Manual custom-integration installation

Copy the `custom_components/athb` directory into the Home Assistant configuration's
`custom_components` directory and restart Home Assistant. Add **Adaptive Thermal Heat Balance**
through Settings → Devices & services. Configure a zone and inspect its numerical preview before
deliberately enabling Adaptive control.

The repository can also build a HACS-compatible ZIP whose root contains `custom_components/athb`.
Published releases attach this deterministic archive and its SHA-256 checksum.

## HACS custom-repository testing

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/Patrick1610/Adaptive-Thermal-Heat-Balance` with category
   **Integration**.
3. Install the latest published release and restart Home Assistant.
4. Add **Adaptive Thermal Heat Balance** through Settings → Devices & services.

HACS normally installs the integration from the tagged repository release. The attached ZIP and
checksum are also available for deterministic manual verification. Repository qualification does
not claim that a live Home Assistant installation or physical climate device was tested.

## Safety boundary

ATHB owns temperature targets only. Keep ordinary equipment safeguards and operating modes in the
climate integration/device. No installation step grants ATHB authority to change HVAC mode, power,
fan, preset, swing or humidity functions.
