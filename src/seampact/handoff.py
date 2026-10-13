"""Finite native registration rule: replace selected callables before SDK intake.

The original initializer, native tool instances, controller and continuation stay
in place. This hook does not admit initialization, remote effects or new exports.
"""

import functools
import inspect
import types
import typing
import uuid

from .runtime import ProtocolError


def install_method_proxy(owner, name, proxy):
    """Preserve a native bound-method descriptor when an SDK discovers methods.

    Runtime.wrap returns a function that already calls a bound original. Binding
    that function directly would pass self twice. This bridge consumes only the
    descriptor receiver, preserves the original bound signature and step metadata,
    and keeps the SDK's inspect.ismethod registration path intact.
    """
    original = getattr(owner, name)
    wrapped = getattr(proxy, "__wrapped__", None)
    if not inspect.ismethod(original) or original.__self__ is not owner:
        raise ProtocolError("native bound instance method required")
    if getattr(original, "__seampact_gate__", None) is not None:
        raise ProtocolError("native method already has a gate")
    if not inspect.ismethod(wrapped) or wrapped.__self__ is not owner or wrapped.__func__ is not original.__func__:
        raise ProtocolError("proxy does not wrap this exact native method")
    if inspect.iscoroutinefunction(proxy):
        @functools.wraps(proxy)
        async def attached(receiver, *args, **kwargs):
            if receiver is not owner:
                raise ProtocolError("method proxy receiver changed")
            return await proxy(*args, **kwargs)
    else:
        @functools.wraps(proxy)
        def attached(receiver, *args, **kwargs):
            if receiver is not owner:
                raise ProtocolError("method proxy receiver changed")
            return proxy(*args, **kwargs)
    # Preserve the native unbound descriptor chain. An explicit unbound
    # __signature__ would be copied by functools.wraps(bound_method) inside
    # real SDK dispatchers, exposing self as a spurious required argument.
    attached.__dict__.pop("__signature__", None)
    attached.__wrapped__ = original.__func__
    method = types.MethodType(attached, owner)
    if inspect.signature(method) != inspect.signature(original):
        raise ProtocolError("bound method signature mismatch")
    setattr(owner, name, method)
    return method


class ToolListHandoff:
    def __init__(self, runtime, owner, selectors, initializer, tools_attribute,
                 owner_id=None, schema_policy="native"):
        if not selectors or len(set(selectors.values())) != len(selectors):
            raise ValueError("unique candidate IDs and original callable names required")
        if schema_policy not in {"native", "resolved-types"}:
            raise ValueError("unknown SDK schema policy")
        self.runtime, self.owner, self.selectors = runtime, owner, dict(selectors)
        self.initializer, self.tools_attribute = initializer, tools_attribute
        self.owner_id = owner_id or "owner-" + uuid.uuid4().hex
        self.schema_policy = schema_policy
        self.prefix = uuid.uuid4().hex
        self.bindings, self.round = [], 0
        self.installed = False
        self.initializing = False

    def install(self):
        if self.installed:
            raise ProtocolError("handoff already installed")
        original = getattr(self.owner, self.initializer)
        if getattr(original,"__seampact_handoff__",None) is not None:
            raise ProtocolError("native initializer already has a tool-list handoff")
        if not inspect.iscoroutinefunction(original):
            raise ProtocolError("tool-list initializer rule requires an async initializer")

        @functools.wraps(original)
        async def initialize(*args, **kwargs):
            if self.initializing:
                raise ProtocolError("concurrent native initialization is unsupported")
            self.initializing = True
            try:
                result = await original(*args, **kwargs)
                await self.bind_current_tools()
                return result
            finally:
                self.initializing = False

        initialize.__seampact_handoff__ = self.prefix
        setattr(self.owner, self.initializer, initialize)
        self.installed = True
        return self

    async def bind_current_tools(self):
        tools = getattr(self.owner, self.tools_attribute)
        if not isinstance(tools, list) or not all(callable(t) for t in tools):
            raise ProtocolError("expected the original list of raw callables")
        matches = {}
        for identity, name in self.selectors.items():
            candidates = [(i,t) for i,t in enumerate(tools) if getattr(t,"__qualname__",None) == name]
            if len(candidates) != 1:
                raise ProtocolError("selected callable must occur exactly once: " + name)
            if getattr(candidates[0][1],"__seampact_gate__",None) is not None:
                raise ProtocolError("native tool list already gated; require fresh native tools")
            matches[identity] = candidates[0]
        self.round += 1
        replacements = list(tools)
        # Prepare every schema before changing the native registration list.
        prepared = []
        for identity, (index, original) in matches.items():
            gate = self.prefix + ":" + str(self.round) + ":" + identity
            proxy = self.runtime.wrap(original, gate, self.owner_id)
            if self.schema_policy == "resolved-types":
                # Fixed SDKs evaluate annotations to build tool schemas. Resolve in
                # the original namespace, never in the admission wrapper's globals.
                # This opt-in assumes native annotation evaluation has no effects.
                annotations = typing.get_type_hints(original, include_extras=True)
                proxy.__annotations__ = annotations
                signature = inspect.signature(original)
                proxy.__signature__ = signature.replace(parameters=[
                    p.replace(annotation=annotations.get(p.name,p.annotation))
                    for p in signature.parameters.values()],
                    return_annotation=annotations.get("return",signature.return_annotation))
            prepared.append((identity,index,original,proxy,gate))
        for identity,index,original,proxy,gate in prepared:
            await self.runtime.acall("register",gate,self.owner_id)
            replacements[index] = proxy
            self.bindings.append(dict(id=identity,gate=gate,initialization_round=self.round,
                callable=original.__qualname__,owner=self.owner_id,
                schema_policy=self.schema_policy,rule="tool-list-before-sdk/1"))
        setattr(self.owner,self.tools_attribute,replacements)
