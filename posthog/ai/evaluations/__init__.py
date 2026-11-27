"""Typed offline evaluation uploads, experiments, scorers, and dataset management.

from posthog.ai.evaluations import OfflineEvaluations, EvaluationItem

with OfflineEvaluations(project_id=123, secret_key="phs_example") as evaluations:
    experiment = evaluations.create_experiment(name="baseline")
    item = EvaluationItem(input="Question", output="Answer")
    # Use an existing immutable scorer-version UUID for each result.
    # experiment.upload_result(item=item, scorer_version_id=version_id, value=True)
    # experiment.complete()
"""

from ._async_datasets import AsyncDatasets, AsyncDatasetSnapshot
from ._dataset_types import (
    Dataset,
    DatasetItem,
    DatasetItemPage,
    DatasetOrder,
    DatasetPage,
    DatasetRevision,
    DatasetRevisionPage,
)
from ._datasets import Datasets, DatasetSnapshot
from ._client import (
    AsyncExperiment,
    AsyncOfflineEvaluations,
    Experiment,
    OfflineEvaluations,
)
from ._errors import EvaluationAPIError
from ._mutations import ItemUpdate, ResultUpdate
from ._scorer_types import (
    BooleanScorerConfig,
    CategoricalPassingRule,
    CategoricalScorerConfig,
    CategoricalScorerOption,
    ExperimentScoreStatistic,
    ExperimentScoreVersionPolicy,
    ExperimentScorerComposition,
    ExperimentScorerCompositionInput,
    NumericPassingRule,
    NumericScorerConfig,
    Scorer,
    ScorerCalculation,
    ScorerComposition,
    ScorerCompositionInput,
    ScorerConfig,
    ScorerInput,
    ScorerKind,
    ScorerOrder,
    ScorerPage,
    ScorerScope,
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
    "AsyncDatasets",
    "AsyncDatasetSnapshot",
    "AsyncExperiment",
    "AsyncOfflineEvaluations",
    "AsyncScorers",
    "BooleanScorerConfig",
    "BulkUploadError",
    "BulkUploadReceipt",
    "CategoricalPassingRule",
    "CategoricalScorerConfig",
    "CategoricalScorerOption",
    "Dataset",
    "DatasetItem",
    "DatasetItemPage",
    "DatasetOrder",
    "DatasetPage",
    "DatasetRevision",
    "DatasetRevisionPage",
    "Datasets",
    "DatasetSnapshot",
    "EvaluationAPIError",
    "EvaluationItem",
    "EvaluationResult",
    "Experiment",
    "ExperimentOptions",
    "ExperimentReceipt",
    "ExperimentScoreStatistic",
    "ExperimentScoreVersionPolicy",
    "ExperimentScorerComposition",
    "ExperimentScorerCompositionInput",
    "ItemReceipt",
    "ItemUpdate",
    "JSONValue",
    "NumericPassingRule",
    "NumericScorerConfig",
    "OfflineEvaluations",
    "ResultOptions",
    "ResultReceipt",
    "ResultStatus",
    "ResultUpdate",
    "RunSource",
    "ScoreValue",
    "Scorer",
    "ScorerCalculation",
    "ScorerComposition",
    "ScorerCompositionInput",
    "ScorerConfig",
    "ScorerInput",
    "ScorerKind",
    "ScorerOrder",
    "ScorerPage",
    "ScorerScope",
    "Scorers",
    "ScorerVersion",
    "ScorerVersionPage",
    "UploadReceipt",
]
