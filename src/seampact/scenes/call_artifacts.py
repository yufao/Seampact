"""Bind explicitly selected original CLI outputs to their returned receipts.

This reads source-produced files; it neither creates a source entry nor grants
process/lifecycle permissions. Only a fresh, source-checked completion can bind
file bytes to the current call. NPZ loading never enables pickle.
"""

import hashlib
import io
import re
import uuid
from pathlib import Path, PurePosixPath

from seampact.scenes.evaluation_evidence import passive_value


class CallArtifacts:
    def __init__(self, declarations):
        if type(declarations) is not dict:
            raise ValueError("output_artifacts must map original argument names to declarations")
        self.declarations = declarations
        self.paths = {}
        for name, spec in declarations.items():
            if (type(name) is not str or not name or type(spec) is not dict
                    or set(spec) != {"format", "worker_root", "platform_root", "filename", "fields"}
                    or spec["format"] != "npz"
                    or type(spec["filename"]) is not str
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+\.npz", spec["filename"])
                    or type(spec["fields"]) is not list or not spec["fields"]
                    or any(type(key) is not str or not key for key in spec["fields"])
                    or len(set(spec["fields"])) != len(spec["fields"])):
                raise ValueError("an NPZ output binding needs explicit roots, filename and distinct field names")
            root = Path(spec["platform_root"])
            worker = PurePosixPath(spec["worker_root"])
            if not root.is_absolute() or not worker.is_absolute() or ".." in root.parts or ".." in worker.parts:
                raise ValueError("artifact roots must be absolute without parent traversal")

    def prepare(self, arguments, contract):
        if self.declarations and (len(self.declarations) != 1 or type(contract) is not dict
                or contract.get("source_status") != "SOURCE_CHECKED_SINGLE_FILE"
                or contract.get("format") != "npz" or contract.get("output_scope") != "fresh-parent/1"
                or set(self.declarations) != {contract.get("output_parameter")}):
            raise ValueError("output binding requires the frozen Unit's exact fresh single-file completion contract")
        for name, spec in self.declarations.items():
            if arguments.get(name) != {"$output": name}:
                raise ValueError("output artifact must bind its exact original call argument through $output")
            root = Path(spec["platform_root"]).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("platform artifact root must be an existing shared directory")
            member = PurePosixPath(uuid.uuid4().hex) / spec["filename"]
            path = root / str(member)
            if path.parent.exists() or path.exists() or path.is_symlink():
                raise ValueError("output namespace is not fresh")
            self.paths[name] = dict(path=path, root=root,
                worker=str(PurePosixPath(spec["worker_root"]) / member))

    def output(self, name):
        if type(name) is not str or name not in self.paths:
            raise ValueError("$output requires a prepared artifact argument in this call")
        return self.paths[name]["worker"]

    def capture(self, result):
        if not self.declarations:
            return {}
        completion = result.get("completion", {}) if type(result) is dict else {}
        if (type(result) is not dict or result.get("result_type") != "subprocess.CompletedProcess" or result.get("returncode") != 0
                or completion.get("contract_declared") is not True or completion.get("task_status") != "COMPLETE"
                or completion.get("input_unchanged") is not True or completion.get("newly_completed") is not True):
            raise ValueError("artifact binding requires this call's complete original CLI receipt")
        if len(self.paths) != 1:
            raise ValueError("the existing completion contract binds exactly one output file")
        import numpy as np
        captured = {}
        for name, binding in self.paths.items():
            path, root = binding["path"], binding["root"]
            if path.is_symlink() or path.parent.is_symlink() or path.resolve(strict=True).parent.parent != root:
                raise ValueError("artifact escaped its fresh shared output namespace")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            after = completion.get("output", {}).get("after") or {}
            if after.get("sha256") != digest or after.get("bytes") != len(data):
                raise ValueError("artifact bytes differ from the original call's completion receipt")
            arrays, values = {}, {}
            with np.load(io.BytesIO(data), allow_pickle=False) as archive:
                if not set(self.declarations[name]["fields"]) <= set(archive.files):
                    raise ValueError("declared artifact fields are missing")
                for key in self.declarations[name]["fields"]:
                    array = archive[key]
                    arrays[key] = dict(dtype=array.dtype.str, shape=list(array.shape))
                    values[key] = passive_value(array)
            captured[name] = dict(path=str(path), worker_path=binding["worker"], sha256=digest,
                bytes=len(data), format="npz", arrays=arrays, values=values)
        return captured
