import logging
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple, Union

from posthog.types import FlagValue

log = logging.getLogger("posthog")


class DependencyGraphError(Exception):
    """Base exception for dependency graph errors"""

    pass


class CyclicDependencyError(DependencyGraphError):
    """Raised when a cycle is detected in the dependency graph"""

    def __init__(self, flag_key: str):
        self.flag_key = flag_key
        super().__init__(f"Cyclic dependency detected for flag: {flag_key}")


class MissingDependencyError(DependencyGraphError):
    """Raised when a required dependency is missing"""

    def __init__(self, flag_key: str, dependency_key: str):
        self.flag_key = flag_key
        self.dependency_key = dependency_key
        super().__init__(f"Missing dependency: {flag_key} depends on {dependency_key}")


class DependencyGraph:
    """
    Manages feature flag dependencies and provides topological sorting for evaluation order.

    This class builds a directed acyclic graph (DAG) from feature flag dependencies and
    provides methods to evaluate flags in the correct order, handling cycles and missing
    dependencies gracefully.
    """

    def __init__(self):
        # Adjacency list representation: flag_key -> set of flags it depends on
        self.dependencies: Dict[str, Set[str]] = defaultdict(set)
        # Reverse adjacency list: flag_key -> set of flags that depend on it
        self.dependents: Dict[str, Set[str]] = defaultdict(set)
        # All flags in the graph
        self.flags: Set[str] = set()
        # Cached evaluation results during flag evaluation
        self.evaluation_cache: Dict[str, FlagValue] = {}

    def add_flag(self, flag_key: str) -> None:
        """Add a flag to the graph"""
        self.flags.add(flag_key)

    def add_dependency(self, flag_key: str, dependency_key: str) -> None:
        """Add a dependency relationship: flag_key depends on dependency_key"""
        self.flags.add(flag_key)
        self.flags.add(dependency_key)
        self.dependencies[flag_key].add(dependency_key)
        self.dependents[dependency_key].add(flag_key)

    def get_dependencies(self, flag_key: str) -> Set[str]:
        """Get all flags that the given flag depends on"""
        return self.dependencies.get(flag_key, set())

    def get_dependents(self, flag_key: str) -> Set[str]:
        """Get all flags that depend on the given flag"""
        return self.dependents.get(flag_key, set())

    def has_cycles(self) -> bool:
        """Check if the graph contains any cycles"""
        try:
            self.topological_sort()
            return False
        except CyclicDependencyError:
            return True

    def detect_cycles(self) -> List[str]:
        """Detect and return list of flags involved in cycles"""
        visited = set()
        rec_stack = set()
        cycle_flags = []

        def dfs(flag_key: str) -> bool:
            if flag_key in rec_stack:
                cycle_flags.append(flag_key)
                return True
            if flag_key in visited:
                return False

            visited.add(flag_key)
            rec_stack.add(flag_key)

            for dependency in self.dependencies.get(flag_key, set()):
                if dfs(dependency):
                    cycle_flags.append(flag_key)
                    return True

            rec_stack.remove(flag_key)
            return False

        for flag_key in self.flags:
            if flag_key not in visited:
                dfs(flag_key)

        return list(set(cycle_flags))

    def remove_cycles(self) -> List[str]:
        """Remove flags involved in cycles and return the list of removed flags"""
        cycle_flags = self.detect_cycles()
        for flag_key in cycle_flags:
            self.remove_flag(flag_key)
            log.warning(f"Removed flag '{flag_key}' due to cyclic dependency")
        return cycle_flags

    def remove_flag(self, flag_key: str) -> None:
        """Remove a flag and all its relationships from the graph"""
        if flag_key not in self.flags:
            return

        # Remove all dependencies of this flag
        for dependency in self.dependencies.get(flag_key, set()):
            self.dependents[dependency].discard(flag_key)

        # Remove all dependents of this flag
        for dependent in self.dependents.get(flag_key, set()):
            self.dependencies[dependent].discard(flag_key)

        # Clean up the data structures
        self.dependencies.pop(flag_key, None)
        self.dependents.pop(flag_key, None)
        self.flags.discard(flag_key)

    def topological_sort(self) -> List[str]:
        """
        Return flags in topological order (dependencies first).

        Raises CyclicDependencyError if a cycle is detected.
        """
        # Kahn's algorithm for topological sorting
        in_degree = {flag: 0 for flag in self.flags}

        # Calculate in-degrees
        for flag_key in self.flags:
            in_degree[flag_key] = len(self.dependencies.get(flag_key, set()))

        # Find all flags with no dependencies
        queue = deque([flag for flag in self.flags if in_degree[flag] == 0])
        result = []

        while queue:
            flag_key = queue.popleft()
            result.append(flag_key)

            # Update in-degrees of dependents
            for dependent in self.dependents.get(flag_key, set()):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        # Check for cycles
        if len(result) != len(self.flags):
            remaining_flags = [flag for flag in self.flags if flag not in result]
            raise CyclicDependencyError(remaining_flags[0])

        return result

    def filter_by_keys(self, requested_keys: Set[str]) -> "DependencyGraph":
        """
        Create a new graph containing only the requested flags and their dependencies.

        This is useful for evaluating only a subset of flags while preserving
        dependency relationships.
        """
        filtered_graph = DependencyGraph()

        # BFS to find all flags that need to be included
        queue = deque(requested_keys)
        required_flags = set(requested_keys)

        while queue:
            flag_key = queue.popleft()
            if flag_key in self.flags:
                for dependency in self.dependencies.get(flag_key, set()):
                    if dependency not in required_flags:
                        required_flags.add(dependency)
                        queue.append(dependency)

        # Build filtered graph
        for flag_key in required_flags:
            if flag_key in self.flags:
                filtered_graph.add_flag(flag_key)
                for dependency in self.dependencies.get(flag_key, set()):
                    if dependency in required_flags:
                        filtered_graph.add_dependency(flag_key, dependency)

        return filtered_graph

    def clear_cache(self) -> None:
        """Clear the evaluation cache"""
        self.evaluation_cache.clear()

    def cache_result(self, flag_key: str, result: FlagValue) -> None:
        """Cache the evaluation result for a flag"""
        self.evaluation_cache[flag_key] = result

    def get_cached_result(self, flag_key: str) -> Optional[FlagValue]:
        """Get cached evaluation result for a flag"""
        return self.evaluation_cache.get(flag_key)


def match_flag_dependency(
    filter_value: Union[bool, str], flag_result: FlagValue
) -> bool:
    """
    Compare a flag evaluation result with a dependency filter value.

    Matching rules based on PostHog flag dependency specification:
    - filter_value == True: Matches any enabled state (flag_result != False)
    - filter_value == False: Matches only disabled state (flag_result == False)
    - filter_value == string: Matches exact variant name (flag_result == filter_value)

    Args:
        filter_value: The expected value from the dependency filter
        flag_result: The actual evaluation result of the dependent flag

    Returns:
        bool: True if the flag result matches the filter value

    Examples:
        >>> # Boolean True filter matches any enabled state
        >>> match_flag_dependency(True, True)
        True
        >>> match_flag_dependency(True, "variant-a")
        True
        >>> match_flag_dependency(True, False)
        False

        >>> # Boolean False filter matches only disabled state
        >>> match_flag_dependency(False, False)
        True
        >>> match_flag_dependency(False, True)
        False
        >>> match_flag_dependency(False, "variant-a")
        False

        >>> # String filter matches exact variant name
        >>> match_flag_dependency("variant-a", "variant-a")
        True
        >>> match_flag_dependency("variant-a", "variant-b")
        False
        >>> match_flag_dependency("variant-a", True)
        False
    """
    if filter_value is True:
        # True matches any enabled state (not false)
        return flag_result is not False
    elif filter_value is False:
        # False matches only disabled state (exactly false)
        return flag_result is False
    else:
        # String value matches exact variant name
        return flag_result == filter_value


def extract_flag_dependencies(feature_flag: Dict) -> Set[str]:
    """
    Extract flag dependencies from a feature flag definition.

    Scans all property filters in the flag's conditions to find dependencies
    on other flags (property_type == "flag"). The dependency key is the flag ID,
    not the flag key.

    Args:
        feature_flag: The feature flag definition dict

    Returns:
        Set[str]: Set of flag IDs that this flag depends on
    """
    dependencies = set()

    # Get flag conditions
    flag_conditions = (feature_flag.get("filters") or {}).get("groups") or []

    for condition in flag_conditions:
        properties = condition.get("properties") or []
        for prop in properties:
            if prop.get("type") == "flag":
                dependency_id = prop.get("key")  # This is the flag ID, not key
                if dependency_id:
                    dependencies.add(str(dependency_id))  # Ensure string format

    return dependencies


def build_dependency_graph(
    feature_flags: List[Dict],
) -> Tuple[DependencyGraph, Dict[str, str]]:
    """
    Build a dependency graph from a list of feature flags.

    This function creates a dependency graph using flag keys for nodes but
    tracking dependencies by flag IDs. It handles missing dependencies
    and cycles gracefully by logging warnings and removing problematic flags.

    Args:
        feature_flags: List of feature flag definitions

    Returns:
        Tuple[DependencyGraph, Dict[str, str]]: A tuple containing:
            - DependencyGraph: A graph with all valid dependencies (using flag keys)
            - Dict[str, str]: Mapping from flag ID to flag key
    """
    graph = DependencyGraph()
    flag_keys = set()
    id_to_key = {}  # Map flag ID to flag key
    key_to_id = {}  # Map flag key to flag ID

    # First pass: collect all flag keys and IDs, add them to the graph
    for flag in feature_flags:
        flag_key = flag.get("key")
        flag_id = flag.get("id")
        if flag_key:
            flag_keys.add(flag_key)
            graph.add_flag(flag_key)
            if flag_id:
                id_to_key[str(flag_id)] = flag_key
                key_to_id[flag_key] = str(flag_id)

    # Second pass: add dependencies (using flag IDs from filters but storing by flag keys)
    missing_dependencies = []
    for flag in feature_flags:
        flag_key = flag.get("key")
        if not flag_key:
            continue

        dependency_ids = extract_flag_dependencies(flag)
        for dependency_id in dependency_ids:
            # Try to convert dependency ID to flag key, or use directly if it's already a flag key
            dependency_key = id_to_key.get(dependency_id)
            if dependency_key:
                graph.add_dependency(flag_key, dependency_key)
            elif dependency_id in flag_keys:
                # dependency_id is actually a flag key, use it directly
                graph.add_dependency(flag_key, dependency_id)
            else:
                missing_dependencies.append((flag_key, dependency_id))
                log.warning(
                    f"Flag '{flag_key}' depends on missing flag ID '{dependency_id}'. "
                    f"This dependency will be ignored."
                )

    # Handle cycles by removing problematic flags
    if graph.has_cycles():
        removed_flags = graph.remove_cycles()
        log.warning(f"Removed flags due to cycles: {removed_flags}")

    return graph, id_to_key


def evaluate_flags_with_dependencies(
    feature_flags: List[Dict],
    distinct_id: str,
    properties: Dict,
    cohort_properties: Optional[Dict] = None,
    requested_flag_keys: Optional[Set[str]] = None,
    groups: Optional[Dict] = None,
    group_properties: Optional[Dict] = None,
    group_type_mapping: Optional[Dict] = None,
) -> Dict[str, FlagValue]:
    """
    Evaluate feature flags with dependency support.

    This function builds a dependency graph, evaluates flags in topological order,
    and caches results for dependent flags to access.

    Args:
        feature_flags: List of feature flag definitions
        distinct_id: The distinct ID for evaluation
        properties: Properties for evaluation
        cohort_properties: Cohort properties for evaluation
        requested_flag_keys: Set of flag keys to evaluate (None for all)
        groups: Group information for group-level flag evaluation
        group_properties: Group properties for group-level flag evaluation
        group_type_mapping: Mapping from group type index to group name

    Returns:
        Dict[str, FlagValue]: Map of flag keys to their evaluation results
    """
    # Import here to avoid circular imports
    from posthog.feature_flags import (
        InconclusiveMatchError,
        match_feature_flag_properties,
    )

    # Build dependency graph
    dependency_graph, id_to_key = build_dependency_graph(feature_flags)

    # Filter graph to only requested flags if specified
    if requested_flag_keys:
        dependency_graph = dependency_graph.filter_by_keys(requested_flag_keys)

    # Get evaluation order
    try:
        evaluation_order = dependency_graph.topological_sort()
    except CyclicDependencyError:
        # This shouldn't happen as we remove cycles in build_dependency_graph
        log.error("Unexpected cyclic dependency after cycle removal")
        evaluation_order = list(dependency_graph.flags)

    # Create flag lookup for efficient access
    flag_lookup = {flag.get("key"): flag for flag in feature_flags if flag.get("key")}

    # Evaluate flags in dependency order
    results = {}
    for flag_key in evaluation_order:
        flag_def = flag_lookup.get(flag_key)
        if not flag_def:
            continue

        try:
            # Check if this is a group-level flag
            flag_filters = flag_def.get("filters", {})
            aggregation_group_type_index = flag_filters.get(
                "aggregation_group_type_index"
            )

            if aggregation_group_type_index is not None:
                # This is a group-level flag
                _groups = groups or {}
                _group_properties = group_properties or {}
                _group_type_mapping = group_type_mapping or {}

                group_name = _group_type_mapping.get(str(aggregation_group_type_index))
                if not group_name or group_name not in _groups:
                    # Can't evaluate group flag without proper group info
                    results[flag_key] = False
                    dependency_graph.cache_result(flag_key, False)
                    continue

                focused_group_properties = _group_properties.get(group_name, {})
                result = match_feature_flag_properties(
                    flag_def,
                    _groups[group_name],
                    focused_group_properties,
                    cohort_properties,
                    dependency_graph,
                    id_to_key,
                )
            else:
                # Person-level flag
                result = match_feature_flag_properties(
                    flag_def,
                    distinct_id,
                    properties,
                    cohort_properties,
                    dependency_graph,
                    id_to_key,
                )

            results[flag_key] = result
            dependency_graph.cache_result(flag_key, result)
        except InconclusiveMatchError:
            # Flag evaluation inconclusive, skip
            continue

    return results


def match_flag_property(
    property_filter: Dict,
    dependency_graph: Optional[DependencyGraph],
    id_to_key: Optional[Dict[str, str]] = None,
) -> Optional[bool]:
    """
    Match a flag property filter against cached dependency results.

    Args:
        property_filter: The property filter with type "flag"
        dependency_graph: The dependency graph with cached results
        id_to_key: Mapping from flag ID to flag key

    Returns:
        Optional[bool]: True if matches, False if doesn't match, None if dependency unavailable
    """
    if not dependency_graph:
        return None

    dependency_id = property_filter.get("key")  # This is the flag ID
    expected_value = property_filter.get("value")
    operator = property_filter.get("operator", "flag_evaluates_to")

    if not dependency_id:
        return None

    # Validate expected_value type
    if not isinstance(expected_value, (bool, str)):
        log.warning(
            f"Invalid value type for flag dependency: {type(expected_value)}. Expected bool or str."
        )
        return None

    # Only flag_evaluates_to operator is supported for flag dependencies
    if operator != "flag_evaluates_to":
        log.warning(
            f"Unsupported operator '{operator}' for flag dependency. Only 'flag_evaluates_to' is supported."
        )
        return None

    # Convert flag ID to flag key for lookup
    if id_to_key:
        dependency_key = id_to_key.get(str(dependency_id))
    else:
        # Fallback: assume dependency_id is actually a key (backward compatibility)
        dependency_key = dependency_id

    if not dependency_key:
        return None

    # Get the cached result for the dependency
    cached_result = dependency_graph.get_cached_result(dependency_key)
    if cached_result is None:
        return None

    # Apply negation if specified
    negation = property_filter.get("negation", False)
    matches = match_flag_dependency(expected_value, cached_result)

    return not matches if negation else matches
