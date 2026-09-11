# ATHB implementation checklist

This compact checklist preserves the normative implementation order and dependency graph from `ATHB_ARCHITECTURE_PLAN.md` section 21. Status reflects repository implementation and verification, not a separate orchestration gate.

| ID | Deliverable | Depends on | Implementation phase | Status |
| --- | --- | --- | --- | --- |
| ATHB-001 | Confirm the canonical repository, working copy, remote, and technical baseline. | — | Phase 1: numerical contract | Complete |
| ATHB-002 | Record immutable scientific references, coefficients, source hashes, discrepancy, and contract version. | 001 | Phase 1: numerical contract | Complete |
| ATHB-003 | Isolate and lock the reference oracle; generate reviewed forward and root fixtures. | 002 | Phase 1: numerical contract | Complete |
| ATHB-004 | Implement immutable contracts, typed roots/failures, tagged sources, validation, and applicability. | 002 | Phase 1: numerical contract | Complete |
| ATHB-005 | Implement the scalar heat-load kernel and unrounded ATHB transfer function with conformance tests. | 003–004 | Phase 1: numerical contract | Complete |
| ATHB-006 | Implement saturation pressure, vapor pressure, dew/frost point, and constant-vapor-pressure candidate RH. | 004 | Phase 2: moisture and radiation | Complete |
| ATHB-007 | Implement uniform, direct, globe, and surface-composite MRT models. | 004, 006 | Phase 2: moisture and radiation | Complete |
| ATHB-008 | Implement strategy-vote derivation and bounded inverse solving with typed failures and budgets. | 005–007 | Phase 3: inverse and locations | Complete |
| ATHB-009 | Implement critical-air typing, moisture, filtering, warm-up, mapped roots, and influence protection. | 008 | Phase 3: inverse and locations | Complete |
| ATHB-010 | Implement calibrated surface estimates and surface-RH diagnostics without helper dependencies. | 006–007 | Phase 3: inverse and locations | Complete |
| ATHB-011 | Implement shared source identity, declarations, conversion, freshness, quarantine, and reference counting. | 004 | Phase 4: environmental history | Complete |
| ATHB-012 | Implement time-weighted daily summaries, calendar weighting, coverage, and DST behavior. | 004, 011 | Phase 4: environmental history | Complete |
| ATHB-013 | Implement Recorder bootstrap, persisted history, lineage changes, and corrupt-history recovery. | 012 | Phase 4: environmental history | Complete |
| ATHB-014 | Implement strategy-based policy, critical caps/conflicts, profiles, fallback, bounds, and coordination. | 009–010, 013 | Phase 5: product policy | Complete |
| ATHB-015 | Implement the climate capability matrix, HA-unit handling, inward grids, and range feasibility. | 014 | Phase 6: climate compatibility | Complete |
| ATHB-016 | Implement per-target ownership transitions and deterministic acknowledgement classification. | 004, 015 | Phase 7: ownership and broker | Complete |
| ATHB-017 | Implement versioned recovery storage, verified writes, and clean/unclean restart reconciliation. | 013, 016 | Phase 7: ownership and broker | Complete |
| ATHB-018 | Implement the sole broker, preflight, coalescing, hysteresis, limits, and acknowledgement handling. | 015–017 | Phase 7: ownership and broker | Complete |
| ATHB-019 | Pass ownership, race, and failure tests before enabling the production dispatch path. | 018 | Phase 7: ownership and broker | Complete |
| ATHB-020 | Implement zone orchestration, executor limits, coherent snapshots, timers, and unload cleanup. | 011–019 | Phase 8: native Home Assistant | Pending |
| ATHB-021 | Implement config, reconfigure, and options flows with progressive disclosure and validation. | 020 | Phase 8: native Home Assistant | Pending |
| ATHB-022 | Implement entity platforms, lightweight strategy persistence, and stable identities. | 020–021 | Phase 8: native Home Assistant | Pending |
| ATHB-023 | Implement decision traces, redacted diagnostics, transition logging, and repairs. | 020–022 | Phase 9: observability and documentation | Pending |
| ATHB-024 | Complete translations and scientific, configuration, operation, installation, and validation documentation. | 002, 021–023 | Phase 9: observability and documentation | Pending |
| ATHB-025 | Implement versioned virtual-installation fixtures, runner, scenarios, and reports. | 003–004, 014, 018, 020–024 | Phase 9: observability and documentation | Pending |
| ATHB-026 | Complete integrated Virtual Installation Validation and all safety/load qualification. | 025 | Phase 10: integrated qualification | Pending |
| ATHB-027 | Run the full numerical, policy, HA-contract, race, privacy, static, and packaging suite. | 026 | Phase 10: integrated qualification | Pending |
| ATHB-028 | Perform independent final architecture, numerical, and safety review of one immutable candidate. | 027 | Phase 10: integrated qualification | Pending |
| ATHB-029 | Produce the validated local distribution and closure evidence; publication remains separate. | 028 | Phase 10: integrated qualification | Pending |
