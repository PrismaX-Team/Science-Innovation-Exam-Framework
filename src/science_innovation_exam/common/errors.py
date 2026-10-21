class ScienceInnovationExamError(Exception):
    """Base class for expected framework failures."""


class ContractError(ScienceInnovationExamError):
    """A versioned input or output contract is invalid."""


class SecurityBoundaryError(ScienceInnovationExamError):
    """An operation would cross a declared trust boundary."""


class StateTransitionError(ScienceInnovationExamError):
    """A persisted lifecycle transition is not permitted."""


class InfrastructureError(ScienceInnovationExamError):
    """The execution infrastructure failed independently of the tested agent."""


class AgentExecutionError(ScienceInnovationExamError):
    """The tested agent could not be launched or observed."""


class ScoringError(ScienceInnovationExamError):
    """The trusted scoring phase failed and must not become an agent penalty."""

