from ..common.errors import ContractError, ScoringError


class MatrixContractError(ContractError):
    """A task's frozen Matrix definition is incomplete or contradictory."""


class MatrixDomainError(MatrixContractError):
    """A raw value is outside the scientific domain of its declared transform."""


class MatrixScoringError(ScoringError):
    """Matrix execution failed independently of the tested Agent's performance."""


class ArenaContractError(ContractError):
    """A frozen cross-task Arena snapshot is incomplete or contradictory."""


class ArenaScoringError(ScoringError):
    """Bradley-Terry fitting failed independently of an Agent's performance."""
