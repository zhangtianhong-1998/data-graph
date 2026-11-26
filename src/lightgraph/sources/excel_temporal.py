"""Bind ISO literals at their comparison site in already-authorized Excel SQL."""
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from lightgraph.errors import LightGraphError
from lightgraph.query.predicate import parse_temporal_value
from lightgraph.source_values import source_scalar

TEMPORAL_TYPES = {'date', 'time', 'timestamp'}
_UNCERTAIN = 'derived_temporal'
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.Like)


def bind_comparisons(tree, parameters, tables):
    """Match pure worksheet temporal columns to driver values. Mixed columns keep their storage types."""
    outputs, column_types = {}, {}
    selectable_types = {}
    changed = False

    def result_type(expr):
        value = expr.unnest()
        if isinstance(value, exp.Column):
            return column_types.get(id(value))
        if isinstance(value, (exp.Select, exp.SetOperation)):
            return selectable_types.get(id(value))
        if isinstance(value, (exp.Alias, exp.Min, exp.Max)):
            return result_type(value.this)
        if isinstance(value, (exp.Count, exp.Predicate)):
            return None
        if any(column_types.get(id(c)) in TEMPORAL_TYPES | {_UNCERTAIN} for c in value.find_all(exp.Column)):
            return _UNCERTAIN
        return None

    def bind(kind, operand):
        nonlocal changed
        if kind not in TEMPORAL_TYPES | {_UNCERTAIN}:
            return
        operand = operand.unnest()
        if isinstance(operand, exp.Null):
            return
        if isinstance(operand, exp.Placeholder):
            value = parameters[operand.name]
            if value is None:
                return
        elif isinstance(operand, exp.Literal) and operand.is_string:
            value = operand.this
        elif isinstance(operand, exp.Column):
            return
        else:
            raise LightGraphError('excel_temporal_comparison_unsupported')
        if kind == _UNCERTAIN or not isinstance(value, str):
            raise LightGraphError('excel_temporal_comparison_unsupported')
        parsed = parse_temporal_value(value, kind)
        if getattr(parsed, 'tzinfo', None) is not None:
            raise LightGraphError('excel_temporal_comparison_unsupported')
        typed = source_scalar(parsed)
        if type(value) is type(typed) and value == typed:
            return
        # One named parameter may also occur in an ordinary text comparison.
        name = f'_excel_temporal_{len(parameters)}'
        while name in parameters:
            name += '_'
        parameters[name] = typed
        operand.replace(exp.Placeholder(this=name))
        changed = True

    for scope in traverse_scope(tree):
        sources = {}
        for alias, (_, source) in scope.selected_sources.items():
            if isinstance(source, exp.Table):
                qualified = f'{source.db}.{source.name}' if source.db else source.name
                sources[alias] = tables.get(qualified, {})
            else:
                sources[alias] = outputs[id(source)]
        for column in scope.find_all(exp.Column):
            types = {columns[column.name] for alias, columns in sources.items()
                     if (not column.table or alias == column.table) and column.name in columns}
            if len(types) == 1:
                column_types[id(column)] = types.pop()
        for comparison in scope.find_all(*_COMPARISONS, exp.Between, exp.In):
            if isinstance(comparison, (exp.Between, exp.In)):
                operands = ([comparison.args['low'], comparison.args['high']] if isinstance(comparison, exp.Between)
                            else comparison.expressions)
                subject = comparison.this
                kind = result_type(subject)
                for operand in operands:
                    bind(kind, operand)
                if isinstance(subject.unnest(), (exp.Placeholder, exp.Literal)):
                    compared = ([comparison.args['query']] if comparison.args.get('query') else operands)
                    kinds = {result_type(operand) for operand in compared}
                    if kinds.intersection(TEMPORAL_TYPES | {_UNCERTAIN}):
                        if len(kinds) != 1:
                            raise LightGraphError('excel_temporal_comparison_unsupported')
                        bind(kinds.pop(), subject)
            else:
                left, right = comparison.this, comparison.expression
                if isinstance(comparison, exp.Like) and result_type(left) in TEMPORAL_TYPES:
                    raise LightGraphError('excel_temporal_comparison_unsupported')
                bind(result_type(left), right)
                bind(result_type(right), left)
        names = scope.outer_columns or scope.expression.named_selects
        if scope.union_scopes:
            branches = [list(outputs[id(child)].values()) for child in scope.union_scopes]
            kinds = []
            for values in zip(*branches):
                distinct = set(values)
                if len(distinct) == 1:
                    kinds.append(values[0])
                else:
                    kinds.append(_UNCERTAIN if distinct.intersection(TEMPORAL_TYPES | {_UNCERTAIN}) else None)
        else:
            kinds = [result_type(item) for item in scope.expression.selects]
        outputs[id(scope)] = dict(zip(names, kinds))
        if len(kinds) == 1:
            selectable_types[id(scope.expression)] = kinds[0]
    return changed
