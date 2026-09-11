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
Building or testing that archive is not a publication or a live installation.

## Safety boundary

ATHB owns temperature targets only. Keep ordinary equipment safeguards and operating modes in the
climate integration/device. No installation step grants ATHB authority to change HVAC mode, power,
fan, preset, swing or humidity functions.
