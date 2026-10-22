"""Finite source checkers. LLM assertions never grant an operation."""

import ast
import re

from .inspection import native_ast, units
from .lowering import lower_loop
from .callsite import RULE as CALLSITE_RULE, lower_callsite


def source_unit(inspector,path,unit="file"):
    available=dict(units(path,inspector.data(path)))
    if unit not in available:
        raise ValueError("source unit is not frozen: " + str(unit))
    return available[unit]


def check_tool_list(inspector,boundary):
    """Check registration anchors only; path order is not dynamic conformance."""
    wiring=boundary["native_wiring"]
    if wiring.get("rule")!="tool-list-before-sdk/1":
        raise ValueError("unsupported native wiring rule")
    for key in ("controller_symbol","initializer","tools_attribute","sdk_intake","setup"):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*",str(wiring.get(key,""))):
            raise ValueError("simple controller and member names required")
    if wiring.get("schema_policy","native") not in {"native","resolved-types"}:
        raise ValueError("unknown schema policy")
    tree=native_ast(source_unit(inspector,wiring["controller_path"],wiring.get("controller_unit","file")))
    owners=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==wiring["controller_symbol"]]
    if len(owners)!=1:
        raise ValueError("native owner class anchor absent or duplicated")
    methods={n.name:n for n in owners[0].body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
    init,intake,setup=(methods.get(wiring[k]) for k in ("initializer","sdk_intake","setup"))
    if not all(isinstance(n,ast.AsyncFunctionDef) for n in (init,intake,setup)):
        raise ValueError("async initializer, SDK intake and setup anchors required")
    def own_attr(node,name):
        return isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id=="self" and node.attr==name
    attribute=wiring["tools_attribute"]
    writes=[n for n in ast.walk(init) if isinstance(n,ast.Assign) and isinstance(n.value,ast.List)
            and any(own_attr(t,attribute) for t in n.targets)]
    leaf=boundary["symbol"].split(".")[-1]
    appends=[n for n in ast.walk(init) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
             and n.func.attr=="append" and own_attr(n.func.value,attribute) and len(n.args)==1
             and isinstance(n.args[0],ast.Attribute) and n.args[0].attr==leaf]
    consumes=[n for n in ast.walk(intake) if isinstance(n,ast.Call)
              and any(k.arg=="tools" and own_attr(k.value,attribute) for k in n.keywords)]
    awaits=[n for n in ast.walk(setup) if isinstance(n,ast.Await) and isinstance(n.value,ast.Call)]
    init_lines=[n.lineno for n in awaits if own_attr(n.value.func,wiring["initializer"])]
    intake_lines=[n.lineno for n in awaits if own_attr(n.value.func,wiring["sdk_intake"])]
    if not writes or not appends or not consumes or len(init_lines)!=1 or len(intake_lines)!=1 or init_lines[0]>=intake_lines[0]:
        raise ValueError("raw-list write, selected append, SDK tools intake or setup ordering anchor missing")
    return dict(rule=wiring["rule"],status="PASS",scope="syntactic registration anchors only",
        initializer_line=init.lineno,intake_line=intake.lineno,setup_order=[init_lines[0],intake_lines[0]],
        controller_sha256=inspector.files[wiring["controller_path"]]["sha256"],dynamic_registration="NOT_RUN")


def check_candidate(inspector,candidate):
    if getattr(inspector,"target_runtime",None):
        return inspector.target_runtime.call("check_candidate",candidate=candidate)
    if not isinstance(candidate,dict) or not isinstance(candidate.get("boundaries"),list):
        raise ValueError("candidate.boundaries must be an array")
    rows=[]
    ids=set()
    for b in candidate["boundaries"]:
        if not isinstance(b,dict):
            raise ValueError("each boundary must be an object")
        identity=b.get("id","")
        if not isinstance(identity,str) or not re.fullmatch(r"[A-Za-z0-9_-]+",identity) or identity in ids:
            raise ValueError("boundary IDs must be unique simple identifiers")
        ids.add(identity)
        path,symbol,mode=b.get("path"),b.get("symbol"),b.get("mode")
        found=[]
        if isinstance(path,str) and path in inspector.files:
            found=[s for s in inspector.inspect_symbols(path)["symbols"]
                   if s["symbol"]==symbol and s["unit"]==b.get("unit","file")]
        anchor=len(found)==1
        supported=mode in {"W","H"} and anchor and not found[0]["generator"]
        binding=dict(status="PASS" if supported else "UNSUPPORTED",rule="native-callable-local/1")
        lowering=None
        try:
            if anchor:
                native_ast(source_unit(inspector,path,b.get("unit","file")))
            if mode=="R" and anchor:
                source=source_unit(inspector,path,b.get("unit","file"))
                rule=b.get("rule","sequential-async-loop-body/1")
                if rule==CALLSITE_RULE:
                    lowered=lower_callsite(source,symbol,b.get("callsite"))
                elif rule=="sequential-async-loop-body/1":
                    lowered=lower_loop(source,symbol,b.get("loop_index",0))
                else:
                    raise ValueError("unsupported finite R rule: "+str(rule))
                lowering=lowered["recipe"]
                supported=True
                binding=dict(status="PASS",rule=lowering["rule"],recipe=lowering,inverse=lowered["verification"])
            elif supported and isinstance(b.get("native_wiring"),dict):
                binding=check_tool_list(inspector,b)
        except (ValueError,KeyError,SyntaxError) as error:
            supported=False
            binding=dict(status="UNSUPPORTED",reason=str(error))
        obligations={
            "source_anchor":dict(status="PASS" if anchor else "FAIL",evidence=found,
                source_sha256=inspector.files.get(path,{}).get("sha256") if isinstance(path,str) else None),
            "binding_rule":binding,
            "owner_and_registration":dict(status="UNKNOWN",required="actual native registration and owner-context probe"),
            "effects_and_retirement":dict(status="UNKNOWN",required="lifecycle/effect summary; callable exit alone may leave remote work"),
            "sequential_conformance":dict(status="NOT_RUN",required="paired native/adapted observations under frozen contract"),
            "dependency_closure":dict(status="UNKNOWN",required="imports, resources, callbacks and configuration")}
        if mode=="R" and lowering and lowering["rule"]==CALLSITE_RULE:
            obligations["state_stability"]=dict(status="UNKNOWN",required="actual argument references, relevant writers and lock ownership while waiting")
            obligations["timeout_preservation"]=dict(status="UNKNOWN",required="original timeout scopes retained; actual queue delay may create a new timeout result",
                queue_counts_inside_original_timeout=True,deadline_reset=False)
        rows.append(dict(id=identity,mode=mode,obligations=obligations,lowering=lowering,
            operations={op:dict(status="UNKNOWN" if supported else "UNSUPPORTED",admitted=False)
                        for op in ("controller_schedule","composition_export","parallel","remote")},
            buildable=supported,runtime_admitted=False))
    # Several candidates may describe alternatives, but this package installs all
    # of them. Ambiguous overlapping cutpoints need an explicit alternative plan.
    method_bindings={}
    for boundary,row in zip(candidate["boundaries"],rows):
        if row["buildable"]:
            key=(boundary["path"],boundary.get("unit","file"),boundary["symbol"])
            method_bindings.setdefault(key,[]).append(row)
    occupied={}
    initializers={}
    for boundary,row in zip(candidate["boundaries"],rows):
        if not row["buildable"]:
            continue
        lowering=row.get("lowering") or {}
        cutpoint=(lowering.get("rule"),tuple(sorted(lowering["callsite"].items()))) if lowering.get("rule")==CALLSITE_RULE else (
             lowering.get("rule"),boundary.get("loop_index",0)) if boundary["mode"]=="R" else None
        key=(boundary["path"],boundary.get("unit","file"),boundary["symbol"],cutpoint)
        conflict=occupied.get(key)
        occupied[key]=row
        wiring=boundary.get("native_wiring")
        if isinstance(wiring,dict) and boundary["mode"]!="R":
            init_key=(wiring["controller_path"],wiring.get("controller_unit","file"),
                      wiring["controller_symbol"],wiring["initializer"])
            shape=(wiring["tools_attribute"],wiring.get("schema_policy","native"),wiring["sdk_intake"],wiring["setup"])
            previous=initializers.get(init_key)
            if previous and previous[0]!=shape:
                conflict=previous[1]
            initializers[init_key]=(shape,row)
        if conflict:
            for item in (row,conflict):
                item["buildable"]=False
                item["obligations"]["binding_rule"]=dict(status="UNSUPPORTED",reason="overlapping bindings require an explicit alternative plan")
                item["operations"]={op:dict(status="UNSUPPORTED",admitted=False) for op in item["operations"]}
    for group in method_bindings.values():
        modes={item["mode"] for item in group}
        if "R" in modes and modes.intersection({"W","H"}):
            # Wrapping the controller and its extracted body would nest gates.
            # The local runtime deliberately lacks a parent/child resource rule.
            for item in group:
                item["buildable"]=False
                item["obligations"]["binding_rule"]=dict(status="UNSUPPORTED",
                    reason="whole-callable and internal R bindings require separate alternative plans")
                item["operations"]={op:dict(status="UNSUPPORTED",admitted=False) for op in item["operations"]}
    return dict(schema="verification-report/1",source_version=inspector.request["source_version"],
                boundaries=rows,status="DRAFT_CHECKED",admitted=False,
                scope="source anchors, registration syntax, finite extraction and original-position call lowering only")
