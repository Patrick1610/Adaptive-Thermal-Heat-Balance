# ATHB source provenance

## Verification record

- Verification window: `2026-09-11T06:31:36Z` through `2026-09-11T06:36:05Z`.
- Method: read-only resolution of official release metadata, Git refs, immutable raw source URLs, and the primary publication page.
- Result: `PASS`; every pinned identity was available and the two charter-bound implementation-source hashes matched exactly.
- Scope: source and API availability only. This record does not claim general scientific validity, Home Assistant runtime execution, a physical-device test, or publication of ATHB.

## Local normative baseline

Authority remains in this exact order. SHA-256 values were recomputed from the repository bytes before implementation:

| Order | Source | SHA-256 | Result |
| ---: | --- | --- | --- |
| 1 | `ATHB_BUILD_CLARIFICATIONS.md` | `c918cf4f885d8ae6cd383540fc6b99d8caa1b8aeb06b626837b30867338c5f72` | Match |
| 2 | `ATHB_ARCHITECTURE_PLAN.md` | `8874a89030e2750bab0df11c8372a9e6a167d27b9d5d0befa27a19cfdc163313` | Match |
| 3 | `ATHB_WHITEPAPER.md` | `f6578592a7739d9927340e08d18a9abecb333a3e22b7cc53cb6a6520ae18c684` | Match |
| Build instructions | Internal build prompt (local-only, not distributed) | n/a | Process input only |

The legacy YAML remains reference material only and has no authority to change the numerical identity.

## Pinned oracle and implementation sources

| Identity | Official primary source | Immutable identity or SHA-256 | Verification result |
| --- | --- | --- | --- |
| Package release | [PyPI release metadata](https://pypi.org/pypi/pythermalcomfort/4.4.2/json) | `pythermalcomfort==4.4.2`; uploaded 2026-08-18 | Available |
| Wheel | [PyPI project files](https://pypi.org/project/pythermalcomfort/4.4.2/) | `7d9149fa9e39bca2f9b074812569ec8d30e0f2254b945c402bf1248e923ea1e3` | Metadata match |
| Source distribution | [PyPI project files](https://pypi.org/project/pythermalcomfort/4.4.2/) | `d5ed244d3f134d977a8e567586ccd4f7cbe20a09e9d126e3ff6a8fc3e848fe01` | Metadata match |
| Release tag | [Official repository tag commit](https://github.com/pythermalcomfort/pythermalcomfort/commit/2597e88fed10fec2f40d49759ed9b74e10ce9f89) | annotated tag object `d56875459af1215e3e575afe8a3a5392091fe2a7`; peeled commit `2597e88fed10fec2f40d49759ed9b74e10ce9f89` | Exact ref match |
| Heat-balance source | [Immutable raw `_pmv_ppd_optimized.py`](https://raw.githubusercontent.com/pythermalcomfort/pythermalcomfort/2597e88fed10fec2f40d49759ed9b74e10ce9f89/pythermalcomfort/models/_pmv_ppd_optimized.py) | `dd3c1f3d7ffacea65978cb080a1b676dbadc143eeb8121196dfa6b9b8a9d2518` | Charter hash match |
| ATHB source | [Immutable raw `pmv_athb.py`](https://raw.githubusercontent.com/pythermalcomfort/pythermalcomfort/2597e88fed10fec2f40d49759ed9b74e10ce9f89/pythermalcomfort/models/pmv_athb.py) | `6ea3044b9ab0a229e3f47603d64c3070b707fb31271a9a92ff4fff0c92ec5c9d` | Charter hash match |
| Upstream license | [Immutable MIT license](https://raw.githubusercontent.com/pythermalcomfort/pythermalcomfort/2597e88fed10fec2f40d49759ed9b74e10ce9f89/LICENSE) | `6e5fda37ef6f9b91b5227e5df241a1ddedd141d4daaa4c3e396703b4267237ad` | Available; notice retained |

PyPI metadata declares Python `>=3.10.0` and runtime dependencies on SciPy, Numba, NumPy `>=1.21,<2.3`, and setuptools. Those dependencies are confined to the isolated fixture oracle and are absent from production requirements.

## Research and discrepancy sources

| Identity | Primary source | Immutable identity or SHA-256 | Verification result |
| --- | --- | --- | --- |
| 2022 publication | [Indoor Air article, DOI 10.1111/ina.13018](https://onlinelibrary.wiley.com/doi/10.1111/ina.13018) | Volume 32, issue 3, article e13018 | Available |
| Author analysis | [ATHBv2 commit](https://github.com/marcelschweiker/ATHBv2/commit/8cb4e7eabe25bc6dbbafa554a045ceacd56a865b) | `8cb4e7eabe25bc6dbbafa554a045ceacd56a865b` | Commit and current HEAD match |
| Analysis source | [Immutable `analysis_ATHB_v2.r`](https://raw.githubusercontent.com/marcelschweiker/ATHBv2/8cb4e7eabe25bc6dbbafa554a045ceacd56a865b/analysis_ATHB_v2.r) | `1e21ddbbf8424d425ff4aec3a361c4e3a5aba6fc0d80fe1ad89579440eba2ed3` | Available |
| `comf` comparison | [Immutable `calcATHBstandard.R`](https://raw.githubusercontent.com/marcelschweiker/comf/ea3a609e79186087480cca437fac254af32dcbe3/R/calcATHBstandard.R) | commit `ea3a609e79186087480cca437fac254af32dcbe3`; file `b2da3282cea6592e432bfde2987a7814d9cece0ee344f514932af03e6a0b89a3` | Available; not selected as oracle |

The author analysis fits the standard transfer structure without an independent two-way load-by-adapted-metabolism term. The inspected `comf` function adds `0.002971073 * LAdpt * metAdpt` and repeats `0.0002264348 * LAdpt * trm`. No author-confirmed erratum was found. The discrepancy is preserved as a regression target; its terms are not merged into formulation version 1.

## Home Assistant 2026.9 API baseline

The minimum source tag and the current 2026.9 patch tag were resolved from the official repository. The four relevant files were byte-identical across both tags during this verification.

| Source/API | Official source | 2026.9.0 / 2026.9.1 SHA-256 | Result |
| --- | --- | --- | --- |
| Release identity | [Home Assistant Core 2026.9.0](https://github.com/home-assistant/core/releases/tag/2026.9.0) and [2026.9.1](https://github.com/home-assistant/core/releases/tag/2026.9.1) | tag commits `dfb5a9e690daaf204b542896e4b595e61a11a401` / `fc034572d0216a04ed40a07154394908a594dfed` | Available |
| Climate service behavior | [2026.9.0 climate source](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/climate/__init__.py) | `90da35b1b3742566fae627a2d9bb979158717d1bc63282ebcf29bcdd8905c1b5` | Available |
| HVAC/feature constants | [2026.9.0 climate constants](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/climate/const.py) | `1afb9253fe40150307614957b0ca7233a742f346874e87bff07916df702f2da8` | Available |
| Recorder history export | [2026.9.0 Recorder history source](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/components/recorder/history/__init__.py) | `dedf55c9ae51f703fda113961a5e611cd30cef28c286f92c7dfd0b7da1d53cc1` | Available |
| Target-selection helpers | [2026.9.0 target helper source](https://github.com/home-assistant/core/blob/2026.9.0/homeassistant/helpers/target.py) | `8c107296a5492a92af1b2e7ae996caaf40a6d37c0d7c64b29ed936b942767aff` | Available |
| Manifest contract | [Official integration-manifest documentation](https://developers.home-assistant.io/docs/creating_integration_manifest/) | custom version required; `helper` and `calculated` are supported; requirements are explicit | Available |
| Runtime data contract | [Official `ConfigEntry.runtime_data` guidance](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/runtime-data/) | current official guidance | Available |

Phase 1 does not implement Home Assistant runtime behavior or call a service. Its manifest declares no third-party runtime requirement, and its numerical modules do not import Home Assistant. Later phases remain bound to the pinned 2026.9 source/API baseline and must re-verify drift when they implement those paths.

## Sequence evidence

The source checks above completed read-only before any product mutation. The first product mutation was `docs/implementation/ATHB_TASK_CHECKLIST.md`; this provenance record was the next scientific-baseline mutation. No commit, push, live Home Assistant instance, external server process, or physical device was used.
