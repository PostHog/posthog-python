"""Typed offline evaluation uploads, experiment lifecycle, and scorer management.

from posthog.ai.evaluations import OfflineEvaluations, EvaluationItem

with OfflineEvaluations(project_id=123, secret_key="phs_example") as evaluations:
    experiment = evaluations.create_experiment(name="baseline")
    item = EvaluationItem(input="Question", output="Answer")
    # Use an existing immutable scorer-version UUID for each result.
    # experiment.upload_result(item=item, scorer_version_id=version_id, value=True)
    # experiment.complete()
"""

from ._client import (
    AsyncExperiment,
    AsyncOfflineEvaluations,
    Experiment,
    OfflineEvaluations,
)
from ._errors import EvaluationAPIError
from ._scorer_types import (
    BooleanScorerConfig,
    CategoricalPassingRule,
    CategoricalScorerConfig,
    CategoricalScorerOption,
    NumericPassingRule,
    NumericScorerConfig,
    Scorer,
    ScorerConfig,
    ScorerKind,
    ScorerOrder,
    ScorerPage,
    ScorerVersion,
    ScorerVersionPage,
)
from ._scorers import AsyncScorers, Scorers
from ._serialization import JSONValue
from ._types import (
    BulkUploadError,
    BulkUploadReceipt,
    EvaluationItem,
    EvaluationResult,
    ExperimentOptions,
    ExperimentReceipt,
    ItemReceipt,
    ResultOptions,
    ResultReceipt,
    ResultStatus,
    RunSource,
    ScoreValue,
    UploadReceipt,
)

__all__ = [
    "AsyncExperiment",
    "AsyncOfflineEvaluations",
    "AsyncScorers",
    "BooleanScorerConfig",
    "BulkUploadError",
    "BulkUploadReceipt",
    "CategoricalPassingRule",
    "CategoricalScorerConfig",
    "CategoricalScorerOption",
    "EvaluationAPIError",
    "EvaluationItem",
    "EvaluationResult",
    "Experiment",
    "ExperimentOptions",
    "ExperimentReceipt",
    "ItemReceipt",
    "JSONValue",
    "NumericPassingRule",
    "NumericScorerConfig",
    "OfflineEvaluations",
    "ResultOptions",
    "ResultReceipt",
    "ResultStatus",
    "RunSource",
    "ScoreValue",
    "Scorer",
    "ScorerConfig",
    "ScorerKind",
    "ScorerOrder",
    "ScorerPage",
    "Scorers",
    "ScorerVersion",
    "ScorerVersionPage",
    "UploadReceipt",
]
