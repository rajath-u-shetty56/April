from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from django.db.models import (
    Avg,
    Case,
    Count,
    F,
    FloatField,
    JSONField,
    Max,
    Min,
    Q,
    Sum,
    TextField,
    Value,
    When,
)
from django.db.models.expressions import BaseExpression
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.db.models.functions import Cast, Coalesce, JSONObject, TruncDay, TruncMonth, TruncWeek

from analytics_platform.analytics.comparisons import QuerySnapshot, compare_snapshots
from analytics_platform.analytics.contracts import (
    AnalyticsInputError,
    BoundedList,
    Scope,
    TimeRange,
    validate_limit,
)
from analytics_platform.catalog.models import Project
from analytics_platform.common.property_observations import observed_non_null_type
from analytics_platform.event_catalog.models import (
    EventDefinition,
    EventDefinitionStatus,
    EventPropertyDefinition,
    PropertyDefinitionStatus,
)
from analytics_platform.events.models import Event

MAX_FILTERS = 20
MAX_DIMENSIONS = 4
MAX_AGGREGATIONS = 8
MAX_COMPARISON_GROUPS = 20_000
MAX_MATCHING_EVENT_ROWS = 100_000
MAX_FILTER_VALUES = 100
RESERVED_PROFILE_EVENT = "$groupidentify"
FILTER_OPERATORS = frozenset({"eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "exists"})
PROPERTY_RANGE_OPERATORS = frozenset({"gt", "gte", "lt", "lte"})
VISIBLE_DEFINITION_STATUSES = (EventDefinitionStatus.VISIBLE, EventDefinitionStatus.VERIFIED)
VISIBLE_PROPERTY_STATUSES = (PropertyDefinitionStatus.VISIBLE, PropertyDefinitionStatus.VERIFIED)


@dataclass(frozen=True)
class Dimension:
    label: str
    alias: str
    expression: BaseExpression
    prerequisites: tuple[tuple[str, BaseExpression], ...] = ()


@dataclass(frozen=True)
class Aggregation:
    label: str
    alias: str
    expression: BaseExpression
    empty_value: object


@dataclass(frozen=True)
class PropertyPopulation:
    event: str
    product: str
    observed_non_null_types: tuple[str, ...]
    has_type_conflict: bool
    nullable: bool


@dataclass(frozen=True)
class PropertyReference:
    property_name: str
    requested_event: str | None
    requested_product: str | None
    populations: tuple[PropertyPopulation, ...]


@dataclass(frozen=True)
class CompiledQuery:
    filters: tuple[dict[str, object], ...]
    dimensions: tuple[Dimension, ...]
    aggregations: tuple[Aggregation, ...]
    property_references: tuple[PropertyReference, ...]
    definition_scopes: tuple[tuple[str, str], ...] | None
    limit: int


@dataclass(frozen=True)
class EventQueryResult:
    scope: Scope
    period: TimeRange
    rows: BoundedList[dict[str, object]]
    dimension_labels: tuple[str, ...]
    aggregation_labels: tuple[str, ...]
    comparison: dict[str, object] | None
    property_coverage: dict[str, BoundedList[dict[str, object]] | None] | None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "scope": self.scope.to_dict(),
            "period": self.period.as_dict(),
            "dimensions": list(self.dimension_labels),
            "aggregations": list(self.aggregation_labels),
            "rows": self.rows.to_dict(),
            "identifier_semantics": "exact_distinct_id_equality",
        }
        if self.comparison is not None:
            result["comparison"] = self.comparison
        if self.property_coverage is not None:
            result["property_coverage"] = {
                name: coverage.to_dict() if coverage is not None else None
                for name, coverage in self.property_coverage.items()
            }
        return result


def query_events(
    project: Project,
    *,
    period: TimeRange,
    filters: Sequence[Mapping[str, object]] = (),
    group_by: Sequence[Mapping[str, object]] = (),
    aggregations: Sequence[Mapping[str, object]],
    limit: int = 50,
    comparison_period: TimeRange | None = None,
) -> EventQueryResult:
    """Run a bounded, catalog-validated event query using structured inputs only."""
    if comparison_period is not None and period.end - period.start != (
        comparison_period.end - comparison_period.start
    ):
        raise AnalyticsInputError("current and comparison periods must have equal duration")
    compiled = _compile_query(
        project,
        filters=filters,
        group_by=group_by,
        aggregations=aggregations,
        limit=limit,
    )
    current_snapshot = _run_snapshot(
        project, period, compiled, include_all=comparison_period is not None
    )
    comparison = None
    if comparison_period is not None:
        baseline_snapshot = _run_snapshot(project, comparison_period, compiled, include_all=True)
        comparison = compare_snapshots(
            current_snapshot,
            baseline_snapshot,
            current_period=period,
            baseline_period=comparison_period,
            dimension_labels=tuple(dimension.label for dimension in compiled.dimensions),
            aggregation_labels=tuple(aggregation.label for aggregation in compiled.aggregations),
            aggregation_empty_values={
                aggregation.label: aggregation.empty_value for aggregation in compiled.aggregations
            },
            limit=compiled.limit,
        )
    property_coverage = None
    if compiled.property_references:
        property_coverage = {
            "current": _property_coverage(project, period, compiled),
            "baseline": (
                _property_coverage(project, comparison_period, compiled)
                if comparison_period is not None
                else None
            ),
        }
    return EventQueryResult(
        scope=Scope(project.pk),
        period=period,
        rows=current_snapshot.rows,
        dimension_labels=tuple(dimension.label for dimension in compiled.dimensions),
        aggregation_labels=tuple(aggregation.label for aggregation in compiled.aggregations),
        comparison=comparison,
        property_coverage=property_coverage,
    )


def _compile_query(
    project: Project,
    *,
    filters: Sequence[Mapping[str, object]],
    group_by: Sequence[Mapping[str, object]],
    aggregations: Sequence[Mapping[str, object]],
    limit: int,
) -> CompiledQuery:
    filters = _bounded_sequence(filters, "filters", MAX_FILTERS)
    group_by = _bounded_sequence(group_by, "group_by", MAX_DIMENSIONS)
    aggregations = _bounded_sequence(aggregations, "aggregations", MAX_AGGREGATIONS)
    if not aggregations:
        raise AnalyticsInputError("at least one aggregation is required")
    limit = validate_limit(limit)

    references: dict[tuple[str, str | None, str | None], PropertyReference] = {}
    compiled_filters = []
    for index, raw_filter in enumerate(filters):
        item = _mapping(raw_filter, f"filters[{index}]")
        field = _choice(
            item.get("field"),
            {"event", "product", "distinct_id", "group_key", "property"},
            "filter field",
        )
        operator = _choice(item.get("operator"), FILTER_OPERATORS, "filter operator")
        allowed = {"field", "operator", "value"}
        if field == "group_key":
            allowed.add("group_type")
        if field == "property":
            allowed.update(("event", "product", "property_name"))
        _reject_extra_keys(item, allowed, f"filters[{index}]")
        if operator != "exists" and "value" not in item:
            raise AnalyticsInputError(f"filters[{index}].value is required")
        if operator == "exists" and "value" in item:
            raise AnalyticsInputError(f"filters[{index}] must omit value for exists")
        if field == "property":
            reference = _resolve_property(
                project,
                event=item.get("event"),
                product=item.get("product"),
                property_name=item.get("property_name"),
            )
            references[
                (
                    reference.property_name,
                    reference.requested_event,
                    reference.requested_product,
                )
            ] = reference
            _validate_property_operator(reference, operator, item.get("value"))
            compiled_filters.append(
                {
                    "field": field,
                    "operator": operator,
                    "value": item.get("value"),
                    "property_reference": reference,
                    "property_name": reference.property_name,
                }
            )
        elif field == "group_key":
            group_type = _nonblank(item.get("group_type"), f"filters[{index}].group_type")
            _validate_simple_operator(operator, item.get("value"), index)
            compiled_filters.append(
                {
                    "field": field,
                    "operator": operator,
                    "value": item.get("value"),
                    "group_type": group_type,
                }
            )
        else:
            if operator not in {"eq", "ne", "in", "not_in", "exists"}:
                raise AnalyticsInputError(f"{operator} is not supported for {field} filters")
            _validate_simple_operator(operator, item.get("value"), index)
            if field in {"event", "product"} and operator != "exists":
                _validate_catalog_selector(project, field, operator, item.get("value"), index)
            compiled_filters.append(
                {"field": field, "operator": operator, "value": item.get("value")}
            )

    dimensions = []
    labels = set()
    for index, raw_dimension in enumerate(group_by):
        item = _mapping(raw_dimension, f"group_by[{index}]")
        kind = _choice(
            item.get("kind"),
            {"event", "product", "group_key", "property", "day", "week", "month"},
            "dimension kind",
        )
        label = _label(item.get("label"), f"group_by[{index}].label")
        if label in labels:
            raise AnalyticsInputError("dimension and aggregation labels must be unique")
        labels.add(label)
        alias = f"_aq_dimension_{index}"
        if kind == "event":
            _reject_extra_keys(item, {"kind", "label"}, f"group_by[{index}]")
            expression = F("event")
        elif kind == "product":
            _reject_extra_keys(item, {"kind", "label"}, f"group_by[{index}]")
            expression = KeyTextTransform("product", "properties")
        elif kind == "group_key":
            _reject_extra_keys(item, {"kind", "label", "group_type"}, f"group_by[{index}]")
            group_type = _nonblank(item.get("group_type"), f"group_by[{index}].group_type")
            expression = KeyTextTransform(group_type, "groups")
        elif kind == "property":
            _reject_extra_keys(
                item,
                {"kind", "label", "event", "product", "property_name"},
                f"group_by[{index}]",
            )
            reference = _resolve_property(
                project,
                event=item.get("event"),
                product=item.get("product"),
                property_name=item.get("property_name"),
            )
            references[
                (
                    reference.property_name,
                    reference.requested_event,
                    reference.requested_product,
                )
            ] = reference
            _require_scalar_property(reference, "property dimensions")
            index_alias = f"_aq_property_text_{index}"
            prerequisites = (
                (index_alias, KeyTextTransform(reference.property_name, "properties")),
            )
            present = Q(properties__has_key=reference.property_name)
            is_null = Q(**{f"{index_alias}__isnull": True})
            expression = Case(
                When(condition=present & is_null, then=JSONObject(kind=Value("null"))),
                When(
                    condition=present,
                    then=JSONObject(
                        kind=Value("value"),
                        value=KeyTransform(reference.property_name, "properties"),
                    ),
                ),
                default=JSONObject(kind=Value("missing")),
                output_field=JSONField(),
            )
        else:
            _reject_extra_keys(item, {"kind", "label"}, f"group_by[{index}]")
            expression = {
                "day": TruncDay("timestamp", tzinfo=UTC),
                "week": TruncWeek("timestamp", tzinfo=UTC),
                "month": TruncMonth("timestamp", tzinfo=UTC),
            }[kind]
        dimensions.append(
            Dimension(
                label=label,
                alias=alias,
                expression=expression,
                prerequisites=prerequisites if kind == "property" else (),
            )
        )

    aggregations_compiled = []
    for index, raw_aggregation in enumerate(aggregations):
        item = _mapping(raw_aggregation, f"aggregations[{index}]")
        kind = _choice(
            item.get("kind"),
            {
                "event_count",
                "distinct_id_count",
                "distinct_group_count",
                "count_property",
                "sum_property",
                "avg_property",
                "min_property",
                "max_property",
            },
            "aggregation kind",
        )
        label = _label(item.get("label"), f"aggregations[{index}].label")
        if label in labels:
            raise AnalyticsInputError("dimension and aggregation labels must be unique")
        labels.add(label)
        alias = f"_aq_aggregation_{index}"
        if kind == "event_count":
            _reject_extra_keys(item, {"kind", "label"}, f"aggregations[{index}]")
            expression = Count("id")
            empty_value = 0
        elif kind == "distinct_id_count":
            _reject_extra_keys(item, {"kind", "label"}, f"aggregations[{index}]")
            expression = Count("distinct_id", distinct=True)
            empty_value = 0
        elif kind == "distinct_group_count":
            _reject_extra_keys(item, {"kind", "label", "group_type"}, f"aggregations[{index}]")
            group_type = _nonblank(item.get("group_type"), f"aggregations[{index}].group_type")
            expression = Count(KeyTextTransform(group_type, "groups"), distinct=True)
            empty_value = 0
        else:
            _reject_extra_keys(
                item,
                {"kind", "label", "event", "product", "property_name"},
                f"aggregations[{index}]",
            )
            reference = _resolve_property(
                project,
                event=item.get("event"),
                product=item.get("product"),
                property_name=item.get("property_name"),
            )
            references[
                (
                    reference.property_name,
                    reference.requested_event,
                    reference.requested_product,
                )
            ] = reference
            property_expression = KeyTextTransform(reference.property_name, "properties")
            property_scope = _property_scope_query(reference)
            if kind == "count_property":
                expression = Count(property_expression, filter=property_scope)
                empty_value = 0
            else:
                _require_numeric_property(reference, kind)
                numeric_value = Cast(property_expression, FloatField())
                expression = {
                    "sum_property": Sum,
                    "avg_property": Avg,
                    "min_property": Min,
                    "max_property": Max,
                }[kind](numeric_value, filter=property_scope)
                empty_value = 0 if kind == "sum_property" else None
        aggregations_compiled.append(
            Aggregation(
                label=label,
                alias=alias,
                expression=expression,
                empty_value=empty_value,
            )
        )

    property_scopes = {
        (population.event, population.product)
        for reference in references.values()
        for population in reference.populations
    }
    selector_conditions = [
        condition for condition in compiled_filters if condition["field"] in {"event", "product"}
    ]
    definition_scopes = property_scopes or None
    if selector_conditions:
        selector_scopes = _visible_selector_scopes(project, selector_conditions)
        definition_scopes = (
            selector_scopes if definition_scopes is None else definition_scopes & selector_scopes
        )
    return CompiledQuery(
        filters=tuple(compiled_filters),
        dimensions=tuple(dimensions),
        aggregations=tuple(aggregations_compiled),
        property_references=tuple(references.values()),
        definition_scopes=(
            tuple(sorted(definition_scopes)) if definition_scopes is not None else None
        ),
        limit=limit,
    )


def _run_snapshot(
    project: Project,
    period: TimeRange,
    compiled: CompiledQuery,
    *,
    include_all: bool,
) -> QuerySnapshot:
    query = _filtered_event_queryset(project, period, compiled)

    dimension_aliases = [dimension.alias for dimension in compiled.dimensions]
    for dimension in compiled.dimensions:
        for alias, expression in dimension.prerequisites:
            query = query.alias(**{alias: expression})
        query = query.annotate(**{dimension.alias: dimension.expression})
    aggregation_map = {
        aggregation.alias: aggregation.expression for aggregation in compiled.aggregations
    }
    if not compiled.dimensions:
        values = query.aggregate(**aggregation_map)
        row = _format_row({}, values, compiled)
        return QuerySnapshot(
            rows=BoundedList.from_items([row], limit=compiled.limit, total_count=1),
            all_rows=(row,),
        )

    grouped = (
        query.values(*dimension_aliases).annotate(**aggregation_map).order_by(*dimension_aliases)
    )
    total_count = grouped.count()
    if include_all and total_count > MAX_COMPARISON_GROUPS:
        raise AnalyticsInputError(
            f"query is limited to {MAX_COMPARISON_GROUPS} grouped rows; add filters"
        )
    raw_rows = (
        list(grouped[:MAX_COMPARISON_GROUPS]) if include_all else list(grouped[: compiled.limit])
    )
    all_rows = tuple(_format_row(row, row, compiled) for row in raw_rows)
    return QuerySnapshot(
        rows=BoundedList.from_items(all_rows, limit=compiled.limit, total_count=total_count),
        all_rows=all_rows,
    )


def _filtered_event_queryset(project: Project, period: TimeRange, compiled: CompiledQuery):
    query = Event.objects.filter(
        project=project,
        timestamp__gte=period.start,
        timestamp__lt=period.end,
    ).exclude(event=RESERVED_PROFILE_EVENT)
    if compiled.definition_scopes is not None:
        query = query.alias(
            _aq_scope_product=Coalesce(
                KeyTextTransform("product", "properties"),
                Value("", output_field=TextField()),
                output_field=TextField(),
            )
        )
        scope_query = Q()
        for event_name, product in compiled.definition_scopes:
            scope_query |= Q(event=event_name, _aq_scope_product=product)
        query = query.filter(scope_query)

    for index, condition in enumerate(compiled.filters):
        field = condition["field"]
        operator = condition["operator"]
        value = condition.get("value")
        if field == "event":
            expression = F("event")
        elif field == "product":
            expression = KeyTextTransform("product", "properties")
        elif field == "distinct_id":
            expression = F("distinct_id")
        elif field == "group_key":
            expression = KeyTextTransform(str(condition["group_type"]), "groups")
        else:
            expression = KeyTransform(str(condition["property_name"]), "properties")
        alias = f"_aq_filter_{index}"
        query = query.alias(**{alias: expression})
        if field == "property":
            reference = condition["property_reference"]
            scoped_match = Q()
            for population in reference.populations:
                scope_condition = Q(event=population.event, _aq_scope_product=population.product)
                present_condition = Q(properties__has_key=reference.property_name)
                if operator == "exists":
                    value_condition = Q()
                elif operator in {"ne", "not_in"}:
                    value_condition = Q(**{f"{alias}__isnull": False})
                    lookup = "in" if operator == "not_in" else "exact"
                    excluded = Q(**{f"{alias}__{lookup}" if lookup == "in" else alias: value})
                    value_condition &= ~excluded
                else:
                    lookup = {
                        "eq": "exact",
                        "in": "in",
                        "gt": "gt",
                        "gte": "gte",
                        "lt": "lt",
                        "lte": "lte",
                    }[operator]
                    value_condition = Q(**{f"{alias}__{lookup}": value})
                scoped_match |= scope_condition & present_condition & value_condition
            query = query.filter(scoped_match)
            continue
        if operator == "exists":
            if field == "property":
                query = query.filter(properties__has_key=condition["property_name"])
            else:
                query = query.filter(**{f"{alias}__isnull": False})
        elif operator in {"ne", "not_in"}:
            query = query.filter(**{f"{alias}__isnull": False})
            lookup = "in" if operator == "not_in" else "exact"
            query = query.exclude(**{f"{alias}__{lookup}" if lookup == "in" else alias: value})
        else:
            lookup = {
                "eq": "exact",
                "in": "in",
                "gt": "gt",
                "gte": "gte",
                "lt": "lt",
                "lte": "lte",
            }[operator]
            query = query.filter(**{f"{alias}__{lookup}": value})
    return query


def query_group_buckets(
    project: Project,
    *,
    period: TimeRange,
    group_type: str,
    filters: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Return bounded, structured per-group counts for a validated activity rule."""
    compiled = _compile_query(
        project,
        filters=(*filters, {"field": "group_key", "group_type": group_type, "operator": "exists"}),
        group_by=({"kind": "group_key", "label": "group_key", "group_type": group_type},),
        aggregations=(
            {"kind": "event_count", "label": "events"},
            {"kind": "distinct_id_count", "label": "distinct_ids"},
        ),
        limit=200,
    )
    return _run_snapshot(project, period, compiled, include_all=True).all_rows


def query_matching_event_rows(
    project: Project,
    *,
    period: TimeRange,
    filters: Sequence[Mapping[str, object]],
    group_type: str | None,
    correlation_property_name: str | None = None,
    maximum_rows: int = MAX_MATCHING_EVENT_ROWS,
) -> tuple[dict[str, object], ...]:
    """Return scoped event evidence for ordered or event-time group analyses."""
    if correlation_property_name is not None:
        event_name = next(
            (item.get("value") for item in filters if item.get("field") == "event"), None
        )
        product = next(
            (item.get("value") for item in filters if item.get("field") == "product"), None
        )
        if not isinstance(event_name, str) or not isinstance(product, str):
            raise AnalyticsInputError(
                "event-property funnel correlation requires event and product selectors per step"
            )
        _resolve_property(
            project,
            event=event_name,
            product=product,
            property_name=correlation_property_name,
        )
    compiled_filters = list(filters)
    if group_type is not None:
        compiled_filters.append(
            {"field": "group_key", "group_type": group_type, "operator": "exists"}
        )
    compiled = _compile_query(
        project,
        filters=compiled_filters,
        group_by=(),
        aggregations=({"kind": "event_count", "label": "events"},),
        limit=1,
    )
    query = _filtered_event_queryset(project, period, compiled)
    total = query.count()
    if total > maximum_rows:
        raise AnalyticsInputError(
            f"event-time analysis is limited to {maximum_rows} matching events; add filters"
        )
    return tuple(
        query.order_by("timestamp", "uuid").values(
            "uuid", "timestamp", "distinct_id", "groups", "event", "properties"
        )
    )


def _format_row(dimensions, aggregate_values, compiled):
    result = {
        dimension.label: _json_value(dimensions.get(dimension.alias))
        for dimension in compiled.dimensions
    }
    result.update(
        {
            aggregation.label: _json_value(aggregate_values.get(aggregation.alias))
            for aggregation in compiled.aggregations
        }
    )
    return result


def _resolve_property(project, *, event, product, property_name):
    event = _nonblank(event, "property event")
    if product is not None and (not isinstance(product, str) or (product and not product.strip())):
        raise AnalyticsInputError("property product must be a string")
    property_name = _nonblank(property_name, "property name")
    definitions = EventDefinition.objects.filter(
        project=project,
        status__in=VISIBLE_DEFINITION_STATUSES,
    )
    definitions = definitions.filter(name=event)
    if product is not None:
        definitions = definitions.filter(product_key=product)
    visible_properties = EventPropertyDefinition.objects.filter(
        event_definition__in=definitions,
        property_name_hash=EventPropertyDefinition.hash_property_name(property_name),
        status__in=VISIBLE_PROPERTY_STATUSES,
    ).select_related("event_definition")
    matches = [prop for prop in visible_properties if prop.property_name == property_name]
    if not matches:
        hidden = EventPropertyDefinition.objects.filter(
            event_definition__in=definitions,
            property_name_hash=EventPropertyDefinition.hash_property_name(property_name),
            status=PropertyDefinitionStatus.HIDDEN,
        ).exists()
        if hidden:
            raise AnalyticsInputError(f"event property {property_name!r} is hidden")
        raise AnalyticsInputError("event property was not discovered for that definition")
    populations = tuple(
        PropertyPopulation(
            event=prop.event_definition.name,
            product=prop.event_definition.product_key,
            observed_non_null_types=tuple(sorted(set(prop.observed_non_null_types))),
            has_type_conflict=prop.has_type_conflict,
            nullable=prop.nullable,
        )
        for prop in matches
    )
    if any(prop.status == PropertyDefinitionStatus.HIDDEN for prop in matches):
        raise AnalyticsInputError(f"event property {property_name!r} is hidden")
    return PropertyReference(
        property_name=property_name,
        requested_event=event,
        requested_product=product,
        populations=populations,
    )


def _validate_property_operator(reference, operator, value):
    if operator in {"in", "not_in"}:
        if not isinstance(value, (list, tuple)) or not value:
            raise AnalyticsInputError(f"{operator} property filter value must be a non-empty list")
        if len(value) > MAX_FILTER_VALUES:
            raise AnalyticsInputError(
                f"{operator} filters are limited to {MAX_FILTER_VALUES} values"
            )
    if operator in {"eq", "ne", "in", "not_in"}:
        candidates = value if operator in {"in", "not_in"} else (value,)
        observed_types = {
            property_type
            for population in reference.populations
            for property_type in population.observed_non_null_types
        }
        nullable = any(population.nullable for population in reference.populations)
        for candidate in candidates:
            candidate_type = observed_non_null_type(candidate)
            if (candidate_type is None and not nullable) or (
                candidate_type is not None and candidate_type not in observed_types
            ):
                raise AnalyticsInputError(
                    "property filter value type is incompatible with observed property types"
                )
    if operator in PROPERTY_RANGE_OPERATORS:
        observed = [set(pop.observed_non_null_types) for pop in reference.populations]
        types = set.union(*observed) if observed else set()
        conflict = any(pop.has_type_conflict for pop in reference.populations)
        if conflict or types not in ({"number"}, {"string"}):
            raise AnalyticsInputError(
                "range operators require one discovered non-null string or number type"
            )
        expected = float if types == {"number"} else str
        if expected is float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AnalyticsInputError("numeric property filters require a number value")
        elif not isinstance(value, str):
            raise AnalyticsInputError("string property filters require a string value")


def _require_numeric_property(reference, kind):
    if any(pop.has_type_conflict for pop in reference.populations) or any(
        set(pop.observed_non_null_types) != {"number"} for pop in reference.populations
    ):
        raise AnalyticsInputError(f"{kind} requires a property observed only as a number")


def _require_scalar_property(reference, usage):
    observed_types = {
        property_type
        for population in reference.populations
        for property_type in population.observed_non_null_types
    }
    if observed_types - {"boolean", "number", "string"}:
        raise AnalyticsInputError(f"{usage} require scalar property values")


def _property_scope_query(reference):
    scope = Q()
    for population in reference.populations:
        scope |= Q(event=population.event, _aq_scope_product=population.product)
    return scope


def _validate_simple_operator(operator, value, index):
    if operator in {"in", "not_in"}:
        if not isinstance(value, (list, tuple)) or not value:
            raise AnalyticsInputError(f"filters[{index}].value must be a non-empty list")
        if len(value) > MAX_FILTER_VALUES:
            raise AnalyticsInputError(
                f"filters[{index}].value is limited to {MAX_FILTER_VALUES} values"
            )
    elif operator == "exists":
        return
    elif isinstance(value, (dict, list)):
        raise AnalyticsInputError(f"filters[{index}].value must be a scalar")


def _validate_catalog_selector(project, field, operator, value, index):
    values = value if operator in {"in", "not_in"} else [value]
    if not isinstance(values, (list, tuple)) or not values:
        raise AnalyticsInputError(f"filters[{index}].value must contain catalog values")
    if any(not isinstance(item, str) for item in values):
        raise AnalyticsInputError(f"filters[{index}].value must contain strings")
    definitions = EventDefinition.objects.filter(
        project=project,
        status__in=VISIBLE_DEFINITION_STATUSES,
    )
    if field == "event":
        known = set(definitions.values_list("name", flat=True))
    else:
        known = set(definitions.values_list("product_key", flat=True))
    if not set(values) <= known:
        raise AnalyticsInputError(
            f"{field} selector must match a visible or verified event catalog definition"
        )


def _visible_selector_scopes(project, conditions):
    definitions = EventDefinition.objects.filter(
        project=project,
        status__in=VISIBLE_DEFINITION_STATUSES,
    ).values_list("name", "product_key")

    def matches(value, condition):
        operator = condition["operator"]
        selected = condition.get("value")
        if operator == "exists":
            return True
        if operator == "eq":
            return value == selected
        if operator == "ne":
            return value != selected
        if operator == "in":
            return value in selected
        if operator == "not_in":
            return value not in selected
        return False

    return {
        (event, product)
        for event, product in definitions
        if all(
            matches(event if condition["field"] == "event" else product, condition)
            for condition in conditions
        )
    }


def _property_coverage(project, period, compiled):
    coverage_rows = []
    for reference in compiled.property_references:
        for population in reference.populations:
            base_query = (
                Event.objects.filter(
                    project=project,
                    event=population.event,
                    timestamp__gte=period.start,
                    timestamp__lt=period.end,
                )
                .alias(
                    _aq_coverage_product=Coalesce(
                        KeyTextTransform("product", "properties"),
                        Value("", output_field=TextField()),
                        output_field=TextField(),
                    ),
                    _aq_null_value=KeyTextTransform(reference.property_name, "properties"),
                )
                .filter(_aq_coverage_product=population.product)
            )
            property_key = reference.property_name
            for condition in compiled.filters:
                if condition["field"] == "property":
                    continue
                base_query = _apply_non_property_filter(base_query, condition, 0)
            coverage = base_query.aggregate(
                base_event_count=Count("id"),
                property_present_count=Count("id", filter=Q(properties__has_key=property_key)),
                explicit_null_count=Count(
                    "id",
                    filter=Q(properties__has_key=property_key) & Q(_aq_null_value__isnull=True),
                ),
            )
            # Keep JSON null distinct from a missing key: the key-existence predicate
            # distinguishes them while text extraction maps JSON null to SQL NULL.
            matched_query = _filtered_event_queryset(project, period, compiled).filter(
                event=population.event,
                _aq_scope_product=population.product,
            )
            base_count = coverage["base_event_count"] or 0
            present_count = coverage["property_present_count"] or 0
            null_count = coverage["explicit_null_count"] or 0
            coverage_rows.append(
                {
                    "event": population.event,
                    "product": population.product,
                    "property_name": property_key,
                    "base_event_count": base_count,
                    "property_present_count": present_count,
                    "explicit_null_count": null_count,
                    "missing_property_count": base_count - present_count,
                    "matched_count": matched_query.count(),
                }
            )
    unique_rows = {
        (row["event"], row["product"], row["property_name"]): row for row in coverage_rows
    }
    sorted_rows = [unique_rows[key] for key in sorted(unique_rows)]
    return BoundedList.from_items(sorted_rows, limit=compiled.limit, total_count=len(sorted_rows))


def _apply_non_property_filter(query, condition, index):
    field = condition["field"]
    operator = condition["operator"]
    value = condition.get("value")
    if field == "event":
        expression = F("event")
    elif field == "product":
        expression = KeyTextTransform("product", "properties")
    elif field == "distinct_id":
        expression = F("distinct_id")
    else:
        expression = KeyTextTransform(str(condition["group_type"]), "groups")
    alias = f"_aq_coverage_filter_{index}"
    query = query.alias(**{alias: expression})
    if operator == "exists":
        return query.filter(**{f"{alias}__isnull": False})
    if operator in {"ne", "not_in"}:
        query = query.filter(**{f"{alias}__isnull": False})
        lookup = "in" if operator == "not_in" else "exact"
        return query.exclude(**{f"{alias}__{lookup}" if lookup == "in" else alias: value})
    lookup = {
        "eq": "exact",
        "in": "in",
        "gt": "gt",
        "gte": "gte",
        "lt": "lt",
        "lte": "lte",
    }[operator]
    return query.filter(**{f"{alias}__{lookup}": value})


def _bounded_sequence(value, name, maximum):
    if not isinstance(value, (list, tuple)):
        raise AnalyticsInputError(f"{name} must be a list")
    if len(value) > maximum:
        raise AnalyticsInputError(f"{name} is limited to {maximum} items")
    return value


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise AnalyticsInputError(f"{name} must be an object")
    return dict(value)


def _reject_extra_keys(value, allowed, name):
    extra = set(value) - allowed
    if extra:
        raise AnalyticsInputError(f"{name} has unsupported fields")


def _choice(value, allowed, name):
    if not isinstance(value, str) or value not in allowed:
        raise AnalyticsInputError(f"unsupported {name}")
    return value


def _nonblank(value, name):
    if not isinstance(value, str) or not value.strip():
        raise AnalyticsInputError(f"{name} must be a nonblank string")
    return value


def _label(value, name):
    value = _nonblank(value, name)
    if len(value) > 64:
        raise AnalyticsInputError(f"{name} must be at most 64 characters")
    return value


def _json_value(value: Any):
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    return value
