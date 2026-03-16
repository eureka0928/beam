"""Client modules for validator external services."""

from .subnet_core_client import (
    SubnetCoreClient,
    UIDRanges,
    ScoreSubmission,
    SpotCheckSubmission,
    FraudPenaltySubmission,
    ChallengeResultSubmission,
    get_subnet_core_client,
    init_subnet_core_client,
    close_subnet_core_client,
)

__all__ = [
    "SubnetCoreClient",
    "UIDRanges",
    "ScoreSubmission",
    "SpotCheckSubmission",
    "FraudPenaltySubmission",
    "ChallengeResultSubmission",
    "get_subnet_core_client",
    "init_subnet_core_client",
    "close_subnet_core_client",
]
