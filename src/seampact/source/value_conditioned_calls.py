"""Same-call source facts, separate from worker objects and execution rights.

The first domain is the pure, explicitly typed whole-W structure checked by
value_conditioned_admission. Source functions are never imported or run here.
An accepted source path still needs the original worker/read-use obligations.
"""
import ast
import copy
import math

from seampact.source.call_value_conditions import ordinary, call_context, contextual, digest, builtin_state
from seampact.source.dependency_closure import _Closure, resolve_resource_calls
from seampact.source.inspection import native_ast
from seampact.source.result_structure import _ResultObserver, _schema
from seampact.source.value_conditioned_admission import verify_structure_plan


SCHEMA = 'source-value-conditioned-call/1'
_NO_PERMISSIONS = dict(semantic_export=False, invoke=False, dispatch=False, process_start=False, import_prefix=False)


class _CallResultObserver(_ResultObserver):
    """Narrow only tests over this frame's exact ordinary parameters.

    This deliberately does not change the global result observer. Calls,
    attributes, subscripts, globals and untracked local aliases remain broad.
    """
    def conditional_truth(self, node, env, path, truth):
        if type(truth) is not bool or not self.frames:
            return super().conditional_truth(node, env, path, truth)
        target = self.frames[-1]['target']
        names = {item.arg for item in (*target.node.args.posonlyargs, *target.node.args.args,
                                       *target.node.args.kwonlyargs)}
        forbidden = (ast.Call, ast.Attribute, ast.Subscript, ast.NamedExpr, ast.Lambda,
                     ast.Await, ast.Yield, ast.YieldFrom, ast.ListComp, ast.DictComp,
                     ast.SetComp, ast.GeneratorExp)
        nodes = list(ast.walk(node))
        if not any(isinstance(part, forbidden) for part in nodes) and all(
                part.id in names for part in nodes if isinstance(part, ast.Name)):
            return truth
        return super().conditional_truth(node, env, path, truth)

    def statement(self, node, path, env):
        super().statement(node, path, env)
        if not self.frames or not isinstance(node, ast.Return) or not isinstance(node.value, ast.Name):
            return
        frame = self.frames[-1]
        names = {item.arg for item in (*frame['target'].node.args.posonlyargs,
            *frame['target'].node.args.args, *frame['target'].node.args.kwonlyargs)}
        if node.value.id not in names or node.value.id not in env:
            return
        value = env[node.value.id]
        kinds = {bool: 'boolean', int: 'integer', float: 'number', str: 'string'}
        kind = kinds.get(type(value))
        from seampact.source.dependency_closure import _LiteralNone
        if type(value) is _LiteralNone:
            kind = 'null'
        if kind and (kind != 'number' or math.isfinite(value)):
            frame.setdefault('exact_parameter_returns', {})[id(node.value)] = dict(status='CHECKED', kind=kind)

    def expression(self, node, frame):
        proof = frame.get('exact_parameter_returns', {}).get(id(node))
        return proof if proof is not None else super().expression(node, frame)


def _result_compatible(proved, declared):
    return proved == declared or proved == {'type': 'integer'} and declared == {'type': 'number'}


def check_call_source(inspector, entry, candidate, structure, arguments):
    """Produce C_x source facts; never turn them into an executable permit."""
    result = dict(schema=SCHEMA, status='UNKNOWN', reason=None,
        source_version=inspector.request.get('source_version'), structure_digest=None,
        payload_digest=None, supplied=[], omitted=[], source_trace=None,
        actual_value_kinds={},
        same_call_result=None, permissions=dict(_NO_PERMISSIONS),
        residual_worker_obligations=[], global_body_status='UNKNOWN')
    try:
        if not verify_structure_plan(inspector, entry, candidate, structure):
            raise ValueError('independent original structure/prefix/W/interface proof changed')
        result['structure_digest'] = structure['plan_digest']
        values = ordinary(arguments)
        if type(values) is not dict:
            raise ValueError('same-call arguments are not exact ordinary values')
        interface = structure['interface']
        from seampact.units import _matches
        if (set(values) - set(interface['properties']) or set(interface['required']) - set(values) or
                any(not _matches(value, interface['properties'][name]) for name, value in values.items())):
            raise ValueError('same-call values do not satisfy the unchanged original interface')
        if not builtin_state():
            raise ValueError('original ordinary operation kernel changed')
        ctx, payload_digest = call_context(values)
        shadow = contextual(inspector, ctx)
        trace = resolve_resource_calls(shadow, entry, candidate)
        result.update(payload_digest=payload_digest, supplied=sorted(values),
            omitted=sorted(set(structure['original_defaults']) - set(values)), source_trace=trace,
            actual_value_kinds={name: type(value).__name__ for name, value in values.items()})
        if (trace.get('status') != 'CHECKED' or trace.get('unresolved_targets') or
                trace.get('negative_scope_status') != 'CHECKED' or
                trace.get('activation_prefix', {}).get('status') != 'CHECKED'):
            raise ValueError('complete same-call source/control/effect path remains UNKNOWN')
        # The first structure domain has no declared external dependencies.
        # A previously unknown branch must not smuggle an opaque SDK/native
        # call into an empty working set, even when its result is unused.
        if trace.get('calls'):
            raise ValueError('same-call external effect/resource domain is outside the first finite rule')
        observer = _CallResultObserver()
        checker = _Closure(shadow, candidate, result_observer=observer)
        output_trace = checker.entry(entry)
        proof = checker.result_output.get('proof', {'status': 'UNKNOWN'})
        if (output_trace.get('status') != 'CHECKED' or output_trace.get('unresolved_targets') or
                checker.negative_scope_gaps or checker.activation_prefix().get('status') != 'CHECKED' or
                proof.get('status') != 'CHECKED' or checker.steps > 16000):
            raise ValueError('same-call normal return coverage remains UNKNOWN')
        schema = _schema(proof)
        if not _result_compatible(schema, structure['declared_result_schema']):
            raise ValueError('same-call return is incompatible with the retained original declaration')
        result.update(status='CONTROLLER_SOURCE_CHECKED', reason=None,
            same_call_result=dict(schema=schema, declared=copy.deepcopy(structure['declared_result_schema']),
                scope='this exact call only; no universal result or success claim'),
            residual_worker_obligations=[
                'CURRENT_NORMAL_GROUP_AND_PROCESS_IDENTITY',
                'ORIGINAL_CHECKED_LOADER_AND_COMPLETE_PREFIX_OCCURRENCE',
                'ORIGINAL_CALLABLE_CODE_DEFAULTS_GLOBALS_AND_BINDING',
                'ORIGINAL_BUILTIN_PROTOCOLS_AND_NO_LOCK_OUTSIDE_WRITER',
                'CURRENT_COMPLETE_RESOURCE_RESERVATION_AND_ATOMIC_PERMIT',
                'RECOMPUTE_THIS_SAME_PAYLOAD_UNDER_ORIGINAL_READ_USE_LOCK',
                'RUNTIME_RESULT_CODEC_AND_ORIGINAL_NORMAL_EXCEPTION_EXITS'])
    except (ValueError, KeyError, TypeError, SyntaxError, OSError, AttributeError) as error:
        result['reason'] = str(error)
    return dict(result, proof_digest=digest(result))


def verify_call_source(inspector, entry, candidate, structure, arguments, proof):
    """A serialized C_x is never accepted without the same source replay."""
    from seampact.source.import_only import _metadata
    try:
        supplied = _metadata(proof)
        return (type(supplied) is dict and supplied.get('status') == 'CONTROLLER_SOURCE_CHECKED'
                and supplied == check_call_source(inspector, entry, candidate, structure, arguments))
    except (ValueError, KeyError, TypeError, SyntaxError, OSError):
        return False
