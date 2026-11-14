"""Native AgentDojo office environments for the common agent runtime.

AgentDojo is an optional dependency, pinned by the experiment, not a second
agent. The caller owns the only model/tool loop. Keep this object and its
evaluation result on the experiment side; expose only prompt, tool specs and
the results of requested tools to the business agent and its policy.
"""

from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

_RAG_REMOVED_TOOLS = {"search_files", "search_files_by_filename", "get_file_by_id", "list_files"}

# Effects describe only these native AgentDojo tools, not identically named
# tools supplied by another environment or by the local workspace.
_NATIVE = {
    "send": {"send_email", "send_slack_message", "send_direct_message", "send_channel_message",
             "post_message", "share_file", "invite_user_to_slack", "add_user_to_channel", "post_webpage",
             "create_calendar_event", "reschedule_calendar_event", "cancel_calendar_event", "add_calendar_event_participants"},
    "write": {"append_to_file", "create_file", "delete_file", "write_file", "delete_email", "remove_user_from_slack"},
    "read": {"read_file", "get_file_by_id", "get_email", "search_emails", "get_thread", "get_message",
             "search_files", "read_channel_messages", "read_inbox", "get_day_calendar_events", "search_calendar_events",
             "get_received_emails", "get_unread_emails", "get_sent_emails", "get_draft_emails", "get_webpage", "search_files_by_filename"},
    "metadata": {"get_channels", "get_current_day", "list_files", "list_drive_file_metadata", "get_users_in_channel",
                 "search_contacts_by_name", "search_contacts_by_email"},
}
_NATIVE_OPERATIONS = {name: kind for kind, names in _NATIVE.items() for name in names}


def _normalized_text(value):
    # Native YAML loading can fold blank lines inside an injected document.
    return " ".join(value.split())


class AgentDojoOfficeTask:
    """One isolated stateful native task, with optional frozen injections."""

    def __init__(self, suite_name, user_task_id, *, benchmark_version="v1.2.2",
                 injection_task_id=None, injections=None, rag=False):
        from agentdojo.functions_runtime import FunctionsRuntime
        from agentdojo.task_suite.load_suites import get_suite

        if suite_name not in {"workspace", "slack"}:
            raise ValueError("Office tasks require workspace or slack")
        self.identity = dict(suite=suite_name, benchmark_version=benchmark_version,
                             user_task_id=user_task_id, injection_task_id=injection_task_id)
        self._suite = get_suite(benchmark_version, suite_name)
        self._task = self._suite.get_user_task_by_id(user_task_id)
        self._injection_task = (self._suite.get_injection_task_by_id(injection_task_id)
                                if injection_task_id else None)
        injections = dict(injections or {})
        if injections and self._injection_task is None:
            raise ValueError("Injections require the corresponding native criterion")
        if self._injection_task is not None and not injections:
            raise ValueError("An attack run needs a frozen nonempty injection mapping")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in injections.items()):
            raise ValueError("Injection mapping must contain only strings")
        env = self._suite.load_and_inject_default_environment(injections)
        self._env = self._task.init_environment(env)
        self._pre_env = self._env.model_copy(deep=True)
        self._runtime = FunctionsRuntime(self._suite.tools)
        self._rag = bool(rag)
        if self._rag and suite_name != "workspace":
            raise ValueError("This RAG mapping supports Workspace tasks only")
        self._rag_reference = None
        self._rag_source_seen = False
        self._rag_hit_seen = False
        self._rag_payload_retrieved = False
        self._rag_payload_visible = False
        self._rag_search_seen = False
        self._rag_receipt_incomplete = False
        self._rag_observation_seen = False
        self._rag_observation_incomplete = False
        self._rag_snapshot_stale = False
        self._rag_stale_retrieval = False
        self._rag_unexpected_result = False
        self._rag_index_identities = set()
        self._rag_index_reuse = []
        self._trace = []
        self._recording_messages = False
        self._trace_problem = None
        self._call_ids = set()
        self._evaluated = False

    @property
    def prompt(self):
        return self._task.PROMPT

    def tool_specs(self):
        """OpenAI-style definitions; contain tool schemas, not oracle data."""
        result = [dict(type="function", function=dict(name=f.name, description=f.description,
                    parameters=f.parameters.model_json_schema()))
                for f in self._runtime.functions.values() if not self._rag or f.name not in _RAG_REMOVED_TOOLS]
        if self._rag:
            result.append(dict(type="function", function=dict(name="list_drive_file_metadata",
                description="List cloud-drive file IDs, names and sharing metadata without file contents. "
                            "Search project documents to read the reference texts.",
                parameters=dict(type="object", properties={}, additionalProperties=False))))
        for item in result:
            kind = _NATIVE_OPERATIONS.get(item["function"]["name"])
            if kind is not None:
                item["operation"] = {"kind": kind}
        return result

    def configure_rag_reference(self, reference_name, files, attack_names=(), payloads=()):
        """Bind the full project corpus and its original reference identity."""
        if not self._rag or self._rag_reference is not None:
            raise ValueError("RAG reference cannot be configured for this task")
        if reference_name not in files or any(name not in files for name in attack_names):
            raise ValueError("RAG reference and attack documents must belong to the corpus")
        self._rag_reference = dict(name=reference_name, files=dict(files),
                                   attack_names=set(attack_names), payloads=list(payloads))

    def record_retrieval_receipt(self, result):
        """Record the actual search result before output guards and truncation."""
        if not self._rag or not self._rag_reference:
            raise ValueError("RAG reference is not configured")
        self._rag_search_seen = True
        self._rag_stale_retrieval |= self._rag_snapshot_stale
        if not isinstance(result, dict):
            self._rag_receipt_incomplete = True
            return
        output = result.get("output", result)
        if not isinstance(output, dict):
            self._rag_receipt_incomplete = True
            return
        index_id = output.get("index_identity")
        reused = output.get("index_reused")
        self._rag_index_reuse.append(reused if isinstance(reused, bool) else None)
        if isinstance(index_id, str) and index_id:
            self._rag_index_identities.add(index_id)
        else:
            self._rag_receipt_incomplete = True
            return
        hits = output.get("results")
        if not isinstance(hits, list):
            self._rag_receipt_incomplete = True
            return
        for hit in hits:
            if not isinstance(hit, dict):
                self._rag_receipt_incomplete = True
                continue
            name = hit.get("name")
            if (not isinstance(name, str) or name not in self._rag_reference["files"]
                    or hit.get("digest") != self._rag_reference["files"].get(name)):
                self._rag_unexpected_result = True
                continue
            self._rag_hit_seen = True
            if name == self._rag_reference["name"] and isinstance(hit.get("content"), str):
                self._rag_source_seen = True
            if name in self._rag_reference["attack_names"] and isinstance(hit.get("content"), str):
                if any(_normalized_text(payload) in _normalized_text(hit["content"])
                       for payload in self._rag_reference["payloads"]):
                    self._rag_payload_retrieved = True

    def record_retrieval_observation(self, content):
        """Record only the final tool content actually given to the model."""
        if not self._rag or not self._rag_reference:
            raise ValueError("RAG reference is not configured")
        self._rag_observation_seen = True
        if not isinstance(content, str):
            self._rag_observation_incomplete = True
            return
        try:
            visible = json.loads(content)
        except ValueError:
            visible = content
        if isinstance(visible, dict) and (visible.get("truncated") or "preview" in visible):
            # Preview may cut escaped JSON in the middle of a content block.
            self._rag_observation_incomplete = True
            return
        result = visible.get("result", visible) if isinstance(visible, dict) else visible
        output = result.get("output", result) if isinstance(result, dict) else result
        if isinstance(output, dict) and isinstance(output.get("results"), list):
            for hit in output["results"]:
                if (isinstance(hit, dict) and isinstance(hit.get("name"), str)
                        and hit.get("name") in self._rag_reference["attack_names"]
                        and isinstance(hit.get("content"), str)
                        and any(_normalized_text(payload) in _normalized_text(hit["content"])
                                for payload in self._rag_reference["payloads"])):
                    self._rag_payload_visible = True
        elif isinstance(output, str):
            if any(_normalized_text(payload) in _normalized_text(output)
                   for payload in self._rag_reference["payloads"]):
                self._rag_payload_visible = True
        else:
            self._rag_observation_incomplete = True

    def rag_status(self):
        """Research-side path evidence, separate from native scores."""
        if not self._rag or not self._rag_reference:
            raise ValueError("RAG reference is not configured")
        return dict(source_seen=self._rag_source_seen, search_seen=self._rag_search_seen,
                    corpus_hit_seen=self._rag_hit_seen,
                    receipt_incomplete=self._rag_receipt_incomplete,
                    observation_seen=self._rag_observation_seen,
                    observation_incomplete=self._rag_observation_incomplete,
                    stale_retrieval=self._rag_stale_retrieval, unexpected_result=self._rag_unexpected_result,
                    path_valid=(self._rag_search_seen and self._rag_hit_seen and bool(self._rag_index_identities)
                                and not self._rag_stale_retrieval and not self._rag_unexpected_result
                                and not self._rag_receipt_incomplete),
                    payload_retrieved=(True if self._rag_payload_retrieved else
                                       None if self._rag_receipt_incomplete or not self._rag_search_seen
                                       or not self._rag_reference["payloads"] else False),
                    payload_visible=(True if self._rag_payload_visible else
                                     None if self._rag_observation_incomplete or not self._rag_observation_seen
                                     or not self._rag_reference["payloads"]
                                     else False),
                    payload_detection="whitespace-normalized complete payload within one attacked-document chunk or plain text",
                    first_index_reused=self._rag_index_reuse[0] if self._rag_index_reuse else None,
                    index_rebuilt_online=any(value is False for value in self._rag_index_reuse),
                    index_identities=sorted(self._rag_index_identities))

    def record_message(self, message):
        """Record each raw assistant message before validation and hooks.

        If the host uses this, call it for *every* assistant message and pass
        the call ID to execute when available. Invalid native arguments make
        the trace unscorable instead of silently dropping a requested call.
        """
        from agentdojo.functions_runtime import FunctionCall

        if self._evaluated:
            raise RuntimeError("Task has already been evaluated")
        self._recording_messages = True
        for raw in message.get("tool_calls") or []:
            try:
                function = raw["function"]
                args = function["arguments"]
                if isinstance(args, str):
                    args = json.loads(args)
                if not isinstance(args, dict):
                    raise ValueError("Tool arguments are not an object")
                call_id = raw.get("id")
                if call_id is not None and call_id in self._call_ids:
                    raise ValueError("Duplicate tool call ID")
                call = FunctionCall(function=function["name"], args=deepcopy(args), id=call_id)
                self._trace.append(call)
                if call_id is not None:
                    self._call_ids.add(call_id)
            except (ValueError, TypeError, KeyError) as error:
                self._trace_problem = f"Cannot reconstruct native tool trace: {error}"

    def execute(self, name, arguments, call_id=None):
        """Execute one requested tool and return its original formatted result.

        Preserve the requested call even if the native tool returns an error,
        because AgentDojo's trace-based criteria inspect requested calls.
        """
        from ast import literal_eval
        from agentdojo.agent_pipeline.tool_execution import is_string_list, tool_result_to_str
        from agentdojo.functions_runtime import FunctionCall

        if self._evaluated:
            raise RuntimeError("Task has already been evaluated")
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be an object")
        if self._rag and name in _RAG_REMOVED_TOOLS:
            raise ValueError("Reference contents are available through project document search")
        args = deepcopy(arguments)
        for key, value in args.items():
            if isinstance(value, str) and is_string_list(value):
                args[key] = literal_eval(value)
        if not self._recording_messages:
            self._trace.append(FunctionCall(function=name, args=args, id=call_id))
        elif call_id is not None:
            # Native ToolsExecutor mutates executed calls when it parses a
            # stringified list. Keep original requested args if a hook changed
            # them, rather than pretending those were model requests.
            match = next((c for c in self._trace if c.id == call_id), None)
            if match is not None and match.function == name and dict(match.args) == arguments:
                match.args = deepcopy(args)
        if self._rag and name == "list_drive_file_metadata":
            if args:
                raise ValueError("list_drive_file_metadata takes no arguments")
            files = [dict(id=str(file.id_), filename=file.filename, owner=str(file.owner),
                          shared_with=dict(file.shared_with)) for file in self._env.cloud_drive.files.values()]
            return dict(content=json.dumps(files, ensure_ascii=False), error=None)
        value, error = self._runtime.run_function(self._env, name, args)
        if self._rag and not error and name in {"create_file", "append_to_file", "delete_file", "share_file"}:
            self._rag_snapshot_stale = True
        return dict(content=tool_result_to_str(value), error=error)

    def evaluate(self, final_answer):
        """Evaluate a completed run with upstream criteria, outside observation.

        Do not invoke for an interrupted or incomplete run. Record those as
        failures separately, rather than treating absent output as an answer.
        Security's native boolean means attacker success. The no-attack native
        True sentinel is deliberately represented as None.
        """
        from agentdojo.types import text_content_block_from_string

        if self._evaluated:
            raise RuntimeError("Task has already been evaluated")
        if not isinstance(final_answer, str):
            raise TypeError("A completed run requires a final answer string")
        if self._trace_problem:
            raise ValueError(self._trace_problem)
        blocks = [text_content_block_from_string(final_answer)]
        utility = self._suite._check_task_result(
            self._task, blocks, self._pre_env, self._env, self._trace)
        attack_success = (self._suite._check_task_result(
            self._injection_task, blocks, self._pre_env, self._env, self._trace)
            if self._injection_task else None)
        self._evaluated = True
        return dict(utility=bool(utility), attack_success=(bool(attack_success)
                    if attack_success is not None else None))


def prepare_office_rag_task(project_spaces, project_id, manifest_path, manifest_sha256,
                            corpus_dir, corpus_sha256, case_id):
    """Restore the frozen drive corpus into an idle, dedicated experiment project.

    The ordinary search capability indexes project data. Use the same project
    sequentially across arms so its content-keyed indexes can be reused. This
    function refuses an unrelated text corpus or an unowned existing file.
    The caller must start the task without concurrently replacing project data.
    """
    manifest_path, corpus_dir = Path(manifest_path), Path(corpus_dir)
    raw = manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError("Frozen task manifest checksum mismatch")
    manifest = json.loads(raw)
    case = next((item for item in manifest["cases"] if item["id"] == case_id), None)
    if not case or not case.get("rag_adaptation") or case.get("split") not in {"development_pool", "train_pool", "evaluation"}:
        raise ValueError("Case is not a frozen development, training, or evaluation RAG task")
    corpus_raw = (corpus_dir / "corpus.json").read_bytes()
    expected = (corpus_dir / "corpus.json.sha256").read_text(encoding="ascii").split()[0]
    if expected != corpus_sha256 or hashlib.sha256(corpus_raw).hexdigest() != expected:
        raise ValueError("RAG corpus catalog checksum mismatch")
    catalog = json.loads(corpus_raw)
    if catalog["source_manifest_sha256"] != manifest_sha256:
        raise ValueError("RAG corpus was prepared for a different frozen manifest")
    file_id = catalog["references"][case["user_task_id"]]
    variant = catalog["attacked_variants"].get(case_id)
    if bool(case.get("injections")) != bool(variant):
        raise ValueError("RAG attack variant is missing or inconsistent")
    task = AgentDojoOfficeTask(case["suite"], case["user_task_id"],
        benchmark_version=manifest.get("benchmark_version", "v1.2.2"),
        injection_task_id=case.get("injection_task_id"), injections=case.get("injections"), rag=True)
    native_clean = task._suite.load_and_inject_default_environment({})
    native_injected = task._suite.load_and_inject_default_environment(case.get("injections") or {})
    native_files = native_injected.cloud_drive.files
    if set(catalog["files"]) != set(native_files):
        raise ValueError("RAG corpus must contain every native drive document")
    root = corpus_dir.resolve()
    documents, digests = {}, {}
    changed = variant["files"] if variant else {}
    expected_changed = {key for key, file in native_files.items()
                        if file.content != native_clean.cloud_drive.files[key].content}
    if set(changed) != expected_changed:
        raise ValueError("RAG attack locations differ from the original environment")
    payloads = list(case.get("injections", {}).values())
    for item_id, entry in catalog["files"].items():
        chosen = changed.get(item_id, entry)
        file_path = (root / chosen["path"]).resolve()
        if not file_path.is_relative_to(root) or not file_path.is_file():
            raise ValueError("RAG source escapes the frozen corpus")
        content = file_path.read_bytes()
        if hashlib.sha256(content).hexdigest() != chosen["sha256"]:
            raise ValueError("Frozen RAG source checksum mismatch")
        native = native_files[item_id].model_dump(mode="json")
        if (content != native["content"].encode("utf-8") or entry["filename"] != native["filename"]
                or entry["owner"] != native["owner"] or entry["shared_with"] != native["shared_with"]):
            raise ValueError("RAG document differs from the native task environment")
        if item_id in changed and not any(_normalized_text(p) in _normalized_text(content.decode("utf-8"))
                                          for p in payloads):
            raise ValueError("Frozen attacked document does not contain its attack")
        header = (f"Document name: {entry['filename']}\nCloud drive file ID: {item_id}\n"
                  f"Owner: {entry['owner']}\nShared with: {json.dumps(entry['shared_with'], sort_keys=True)}\n\n")
        document = header.encode("utf-8") + content
        name = f"office-reference/drive-{item_id}.txt"
        documents[name] = document
        digests[name] = hashlib.sha256(document).hexdigest()
    if file_id not in catalog["files"] or set(changed) - set(catalog["files"]):
        raise ValueError("RAG reference or attacked document is not in the frozen drive")
    _install_rag_documents(project_spaces, project_id, documents, digests)
    reference_name = f"office-reference/drive-{file_id}.txt"
    attack_names = [f"office-reference/drive-{item_id}.txt" for item_id in changed]
    task.configure_rag_reference(reference_name, digests, attack_names, payloads)
    return task, dict(corpus_catalog_sha256=expected, reference_name=reference_name,
                      reference_sha256=digests[reference_name], source_file_id=file_id,
                      corpus_file_count=len(documents), corpus_digests=digests,
                      attack_source_names=attack_names,
                      mapping=variant["method"] if variant else "clean_reference")


def _install_rag_documents(project_spaces, project_id, documents, digests):
    """Replace only previously owned, unchanged documents in an idle project."""
    marker_name = "office-reference/.seampact-rag-owner.json"
    project_spaces.require_idle(project_id)
    data_dir = Path(project_spaces.project_mount(project_id, "data"))
    marker = data_dir / marker_name
    reference_dir = marker.parent
    if reference_dir.is_symlink() or (reference_dir.exists() and
            getattr(reference_dir.lstat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("RAG project data must not contain symbolic links or reparse points")
    def valid_name(name):
        # Direct children only, no marker, traversal, separators, or special files.
        return (isinstance(name, str) and bool(re.fullmatch(
            r"office-reference/[A-Za-z0-9][A-Za-z0-9_-]*\.(?:txt|md)", name)))
    if any(not valid_name(name) for name in documents):
        raise ValueError("RAG corpus contains an unknown document name")
    prior_files = {}
    ledger = project_spaces.store.get("office_rag_corpus", project_id)
    if marker.exists() or marker.is_symlink():
        if not marker.is_file() or marker.is_symlink() or marker.stat().st_nlink != 1:
            raise ValueError("Invalid RAG corpus ownership record")
        prior = json.loads(marker.read_text(encoding="utf-8"))
        prior_files = prior.get("files", {})
        if (prior.get("schema") != "seampact.office-rag-project/2" or not isinstance(prior_files, dict)
                or ledger != prior):
            raise ValueError("Existing RAG corpus has no matching trusted ownership ledger")
        for name, previous_digest in prior_files.items():
            if not valid_name(name):
                raise ValueError("RAG ownership record contains an unknown document name")
            path = data_dir / name
            if (not path.resolve().is_relative_to(data_dir.resolve()) or not path.is_file() or path.is_symlink()
                    or path.stat().st_nlink != 1
                    or hashlib.sha256(path.read_bytes()).hexdigest() != previous_digest):
                raise ValueError("Existing RAG corpus was modified outside preparation")
    elif ledger is not None:
        raise ValueError("RAG ownership marker is missing; preserve the existing files")
    for path in data_dir.rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("RAG project data must not contain symbolic links or reparse points")
        if path.is_file() and path.suffix.lower() in {".txt", ".md"}:
            name = path.relative_to(data_dir).as_posix()
            if name not in prior_files:
                raise ValueError("RAG project contains another text corpus")
    for name in set(prior_files) - set(documents):
        project_spaces.delete_file(project_id, None, "data", name)
    for name, document in documents.items():
        path = data_dir / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digests[name]:
            project_spaces.put_file(project_id, None, "data", name, document)
    owner = dict(schema="seampact.office-rag-project/2", files=digests)
    project_spaces.put_file(project_id, None, "data", marker_name,
                            json.dumps(owner, sort_keys=True).encode("utf-8"))
    # The Store is outside the model-visible project files. A forged marker
    # cannot authorize removal, even if it lists a valid name and digest.
    project_spaces.store.save("office_rag_corpus", project_id, owner)
