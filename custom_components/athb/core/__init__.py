"""Pure, standard-library-only ATHB numerical core."""

from .athb_engine import (
    FORMULATION_ID,
    NUMERICAL_CONTRACT_VERSION,
    evaluate_athb,
    relative_air_speed,
)
from .contracts import (
    AUTOMATIC_CLOTHING,
    ApplicabilityReason,
    AthbInputs,
    AthbResult,
    AthbSuccess,
    AutomaticClothing,
    FixedClothing,
    NumericalFailure,
    NumericalFailureCode,
)

__all__ = [
    "AUTOMATIC_CLOTHING",
    "FORMULATION_ID",
    "NUMERICAL_CONTRACT_VERSION",
    "ApplicabilityReason",
    "AthbInputs",
    "AthbResult",
    "AthbSuccess",
    "AutomaticClothing",
    "FixedClothing",
    "NumericalFailure",
    "NumericalFailureCode",
    "evaluate_athb",
    "relative_air_speed",
]
