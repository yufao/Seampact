"""Deterministic adapter packages. Building never imports or runs original code."""

import json
import re
import zipfile
from pathlib import Path

from .inspection import digest
from .lowering import lower_loop
from .verification import check_candidate, source_unit

ADAPTER = '''"""Generated binding; existing native owners supply the original callables.
No source import, initialization, data rewrite, or new composition is implied.
"""
import json
import uuid
from pathlib import Path
from seampact.handoff import ToolListHandoff

def _manifest():
    return json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))

def attach(runtime, originals, owner, generation=1):
    manifest = _manifest()
    result = {}
    for boundary in manifest["candidate"]["boundaries"]:
        if boundary["mode"] == "R" or isinstance(boundary.get("native_wiring"), dict):
            continue
        identity = boundary["id"]
        if identity not in originals:
            raise ValueError("native owner must supply original callable: " + identity)
        result[identity] = runtime.wrap(originals[identity], identity, owner, generation)
    return result

def install_tool_handoffs(runtime, owner, controller_path, controller_symbol, owner_id=None):
    """Install before native setup/SDK registration. Host supplies the original owner."""
    groups = {}
    for boundary in _manifest()["candidate"]["boundaries"]:
        wiring = boundary.get("native_wiring")
        if boundary["mode"] == "R" or not isinstance(wiring, dict):
            continue
        if wiring["controller_path"] != controller_path or wiring["controller_symbol"] != controller_symbol:
            continue
        key = (wiring["initializer"], wiring["tools_attribute"], wiring.get("schema_policy", "native"))
        groups.setdefault(key, {})[boundary["id"]] = boundary["symbol"]
    hooks = []
    shared_owner = owner_id or "owner-" + uuid.uuid4().hex
    for (initializer, attribute, policy), selectors in groups.items():
        hooks.append(ToolListHandoff(runtime, owner, selectors, initializer, attribute,
                                    owner_id=shared_owner, schema_policy=policy).install())
    return hooks

def install_loop_handoffs(runtime, owner, source_path, source_unit="file", owner_id=None):
    """Owner must already use the supplied patched code unit. No imports here."""
    manifest = _manifest()
    bindings = []
    shared_owner = owner_id or "owner-" + uuid.uuid4().hex
    for transform in manifest["transformations"]:
        if transform["path"] != source_path or transform["unit"] != source_unit:
            continue
        recipe = transform["recipe"]
        helper = recipe["helper"]
        original = getattr(owner, helper)
        expected = recipe["symbol"].split(".")[0] + "." + helper
        if original.__qualname__ != expected:
            raise ValueError("patched native helper anchor mismatch")
        gate = uuid.uuid4().hex + ":" + transform["id"]
        proxy = runtime.wrap(original, gate, shared_owner)
        runtime.call("register", gate, shared_owner)
        setattr(owner, helper, proxy)
        bindings.append(dict(id=transform["id"], gate=gate, owner=shared_owner, helper=helper))
    return bindings
'''

VERIFY = '''"""Offline integrity inspection only; it does not execute the harness."""
import hashlib
import json
from pathlib import Path
root = Path(__file__).resolve().parent
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
for relative, expected in manifest["artifact_hashes"].items():
    actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("digest mismatch: " + relative)
print(json.dumps({"integrity": "PASS", "runtime": "NOT_RUN", "admission": False}))
'''


def build_package(inspector, candidate, destination, identity):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
        raise ValueError("invalid package ID")
    report = check_candidate(inspector, candidate)
    if not report["boundaries"] or not all(b["buildable"] for b in report["boundaries"]):
        raise ValueError("candidate contains unsupported bindings; inspect the independent report")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {"adapter.py": ADAPTER.encode(), "verify.py": VERIFY.encode(),
        "verification.json": json.dumps(report, ensure_ascii=False, indent=2).encode(),
        "request.json": json.dumps(inspector.request, ensure_ascii=False, indent=2).encode(),
        "README.md": ("# Native-owner binding draft\n\n"
            "Build status: BUILT_DRAFT. Native wiring, conformance and resource closure: NOT_RUN/UNKNOWN.\n"
            "Run `python -B verify.py` for integrity only.\n"
            "Native host supplies originals to adapter.attach, or calls install_tool_handoffs before setup.\n"
            "For supported R, patched/ contains a separate artifact; the native host must load it\n"
            "then call install_loop_handoffs. Original source/ remains byte-identical.\n"
            "SDK initialization and dependency installation remain explicit. The original controller\n"
            "keeps iteration, activation and continuation; helper arguments keep mutable aliases.\n"
            "Do not register these boundaries as admitted until the independent runtime evaluator passes.\n"
            "R is limited to sequential-async-loop-body/1 with an inverse AST check.\n"
            "New composition, parallel execution and remote execution are not admitted by this package.\n").encode()}
    transformations=[]
    changed={}
    for boundary in candidate["boundaries"]:
        if boundary["mode"] != "R":
            continue
        path,unit=boundary["path"],boundary.get("unit","file")
        key=(path,unit)
        original=changed.get(key,source_unit(inspector,path,unit))
        lowered=lower_loop(original,boundary["symbol"],boundary.get("loop_index",0))
        changed[key]=lowered["source"]
        transformations.append(dict(id=boundary["id"],path=path,unit=unit,recipe=lowered["recipe"],
            input_unit_sha256=digest(original.encode()),output_unit_sha256=digest(lowered["source"].encode()),
            verification=lowered["verification"]))
    for path in dict.fromkeys(p for p,u in changed):
        if path.endswith(".ipynb"):
            book=json.loads(inspector.data(path).decode("utf-8-sig"))
            for (p,unit),source in changed.items():
                if p==path:
                    book["cells"][int(unit.split(":")[1])]["source"]=source.splitlines(keepends=True)
            payloads["patched/"+path]=json.dumps(book,ensure_ascii=False,indent=2).encode()
        else:
            payloads["patched/"+path]=changed[(path,"file")].encode()
    for path in inspector.files:
        payloads["source/" + path] = inspector.data(path)
    manifest = dict(schema="module-package/2", id=identity, status="BUILT_DRAFT", admitted=False,
                    candidate=candidate, source_version=inspector.request["source_version"],
                    transformations=transformations,
                    environment=dict(python=">=3.11", native_dependencies="UNRESOLVED"),
                    runtime_bindings="native owner must supply original callables and verified registrations",
                    artifact_hashes={p: digest(data) for p, data in payloads.items()})
    payloads["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    target = destination / (identity + ".zip")
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED) as z:
        for path, data in payloads.items():
            z.writestr(path, data)
    return dict(**manifest, archive=target.name, sha256=digest(target.read_bytes()))
