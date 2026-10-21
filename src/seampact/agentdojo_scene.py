"""Optional native AgentDojo tool-return scene for the common training interface.

This hand-written environment bridge is separate from automatic Unit discovery.
Install AgentDojo and bind the unchanged detector's ``detect(str)`` method as a
READY Unit before selecting this factory. Runtime/environment objects stay in
the original AgentDojo pipeline; only text and the native detector result cross
the Unit boundary. No detector weights or task outcomes are supplied here.

Observation schema, in order: query_chars, message_count, latest_tool_count,
latest_tool_chars, decision_count. Candidate feature vectors are explicit scene
configuration. Each configured guard is one alternative to skip; mandatory
guards must be declared in required_units and cannot be skipped.
"""

import copy
import queue
import threading


_STOP = object()
_FIXED_ATTACKS = {"direct", "ignore_previous", "system_message", "injecagent"}


def _plain(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("AgentDojo evidence contains an unsupported native value: " + type(value).__name__)


class AgentDojoScene:
    def __init__(self, config, context):
        # These imports are optional: ordinary platform use needs no AgentDojo.
        from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, load_system_message
        from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
        from agentdojo.agent_pipeline.basic_elements import InitQuery, SystemMessage
        from agentdojo.agent_pipeline.llms.openai_llm import (
            _function_to_openai, _message_to_openai, _openai_to_assistant_message,
        )
        from agentdojo.agent_pipeline.pi_detector import PromptInjectionDetector
        from agentdojo.agent_pipeline.tool_execution import ToolsExecutionLoop, ToolsExecutor
        from agentdojo.task_suite.load_suites import get_suite
        from openai.types.chat import ChatCompletionMessage

        self.config, self.context = copy.deepcopy(config), context
        self.suite = get_suite(config["benchmark_version"], config["suite"])
        self.guards = copy.deepcopy(config.get("guards", []))
        if not self.guards:
            raise ValueError("AgentDojo scene requires a real bound detector Unit")
        ids = [guard.get("unit_id") for guard in self.guards]
        if any(not isinstance(identity, str) or not identity for identity in ids) or len(set(ids)) != len(ids):
            raise ValueError("AgentDojo guard Unit IDs must be present and unique")
        for guard in self.guards:
            if not isinstance(guard.get("features"), list):
                raise ValueError("Each AgentDojo guard needs explicit candidate features")
            if guard.get("mode", "message") not in {"message", "full_conversation"}:
                raise ValueError("Unsupported original detector mode")
        self.required = list(config.get("required_units", []))
        if len(self.required) > 1 or set(self.required) - set(ids):
            raise ValueError("This scene supports one guard alternative; required_units must identify one configured guard")
        if not isinstance(config.get("skip_features"), list):
            raise ValueError("AgentDojo scene requires explicit skip_features")
        all_guard = config.get("all_applicable_guard_id") or (ids[0] if len(ids) == 1 else None)
        if all_guard not in ids:
            raise ValueError("Mutually exclusive guard alternatives need an explicit all_applicable_guard_id")
        self.references = dict(required_only="guard:" + self.required[0] if self.required else "skip",
                               all_applicable="guard:" + (self.required[0] if self.required else all_guard))
        max_iters = config.get("max_iters", 15)
        if type(max_iters) is not int or max_iters < 1:
            raise ValueError("max_iters must be a positive integer")
        self.events, self.actions = queue.Queue(), queue.Queue()
        self.worker, self.tail, self.pre_state = None, None, None
        self.decisions = 0
        scene = self
        profile = context.profiles.role_profile("chat")
        model_name = profile["model"]

        class ModelBridge(BasePipelineElement):
            def query(self, query, runtime, env, messages, extra_args):
                scene.tail = (query, runtime, env, messages, extra_args)
                request = [_message_to_openai(message, model_name) for message in messages]
                tools = [_function_to_openai(tool) for tool in runtime.functions.values()]
                response = context.complete(request, tools=tools)
                if response["choices"][0].get("finish_reason") not in {"stop", "tool_calls", None}:
                    raise ValueError("Model response unfinished: " + str(response["choices"][0].get("finish_reason")))
                native = ChatCompletionMessage.model_validate(response["choices"][0]["message"])
                scene.tail = (query, runtime, env, [*messages, _openai_to_assistant_message(native)], extra_args)
                return scene.tail

        class GuardBridge(PromptInjectionDetector):
            def __init__(self, guard):
                super().__init__(mode=guard.get("mode", "message"),
                                 raise_on_injection=guard.get("raise_on_injection", False))
                self.guard = guard

            def detect(self, tool_output):
                argument = self.guard.get("argument", "tool_output")
                # Original TransformersBasedPIDetector.detect returns (injection, safety_score).
                return context.invoke(self.guard["unit_id"], {argument: tool_output})

        class DecisionBoundary(BasePipelineElement):
            def query(self, query, runtime, env, messages, extra_args):
                scene.tail = (query, runtime, env, messages, extra_args)
                if not messages or messages[-1]["role"] != "tool":
                    return query, runtime, env, messages, extra_args
                latest = []
                for message in reversed(messages):
                    if message["role"] != "tool":
                        break
                    latest.append(message)
                scene.decisions += 1
                text_chars = sum(len(block.get("content") or "") for message in latest
                                 for block in message.get("content") or [])
                candidates = [dict(id="skip", features=config["skip_features"], units=[], legal=True)]
                for guard in scene.guards:
                    candidates.append(dict(id="guard:" + guard["unit_id"], features=guard["features"],
                                           units=[guard["unit_id"]], legal=True))
                scene.events.put(dict(kind="decision", event="tool_result",
                    scene_observation=dict(modalities=["text", "tool_text"], scopes=["tool_use"],
                        available_inputs=["question", "tool_result"], question_chars=len(query),
                        answer_chars=None, evidence_count=len(latest), answer_version=None,
                        prior_calls=len(context.calls), remaining_budget=max_iters - scene.decisions, risk=None),
                    observation=[len(query), len(messages), len(latest), text_chars, scene.decisions],
                    candidates=candidates, required_units=scene.required, references=scene.references))
                action = scene.actions.get()
                if action is _STOP:
                    raise InterruptedError("AgentDojo scene stopped at a real decision boundary")
                if action["id"] == "skip":
                    return query, runtime, env, messages, extra_args
                guard = next((item for item in scene.guards
                              if action["id"] == "guard:" + item["unit_id"]), None)
                if guard is None:
                    raise ValueError("AgentDojo received an unknown guard action")
                # Original query retains message transformation and AbortAgentError behavior.
                return GuardBridge(guard).query(query, runtime, env, messages, extra_args)

        model = ModelBridge()
        loop = ToolsExecutionLoop([ToolsExecutor(), DecisionBoundary(), model], max_iters=max_iters)
        base = AgentPipeline([SystemMessage(load_system_message(config.get("system_message_name"))),
                              InitQuery(), model, loop])
        base.name = model_name

        class CapturePipeline(BasePipelineElement):
            name = model_name

            def query(self, query, runtime, env, messages=(), extra_args=None):
                if scene.pre_state is None:
                    scene.pre_state = _plain(env)
                result = base.query(query, runtime, env, list(messages), extra_args or {})
                scene.tail = result
                return result

        self.pipeline = CapturePipeline()

    def _run(self, task):
        question = None
        try:
            user_task = self.suite.get_user_task_by_id(task["user_task_id"])
            question = user_task.PROMPT
            injection_id = task.get("injection_task_id")
            injection_task = self.suite.get_injection_task_by_id(injection_id) if injection_id else None
            injections = task.get("injections", {})
            if task.get("attack"):
                if not injection_task or task["attack"] not in _FIXED_ATTACKS or "injections" in task:
                    raise ValueError("Use one original fixed-template attack with an injection task, or saved injections")
                from agentdojo.attacks import load_attack
                injections = load_attack(task["attack"], self.suite, self.pipeline).attack(user_task, injection_task)
            if not isinstance(injections, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                     for k, v in injections.items()):
                raise ValueError("AgentDojo injections must be an actual string mapping")
            if injections and injection_task is None:
                raise ValueError("Injected tasks need the corresponding original injection criterion")
            utility, attack_success = self.suite.run_task_with_pipeline(
                self.pipeline, user_task, injection_task, injections)
            messages = self.tail[3] if self.tail else []
            last = messages[-1] if messages else {}
            unfinished = last.get("role") != "assistant" or bool(last.get("tool_calls"))
            evidence = self._evidence(user_task.PROMPT, unfinished)
            self.events.put(dict(kind="terminal", terminated=not unfinished, truncated=unfinished,
                evidence=evidence,
                program_metrics={"official_utility": int(utility),
                    "official_attack_success": (int(attack_success) if injection_task else
                        dict(status="NOT_APPLICABLE", value=None, reason="No injection task; native True is a sentinel"))},
                evaluation_metrics={"utility": int(utility),
                    "attack_success": int(attack_success) if injection_task else None},
                response="\n".join(block.get("content") or "" for block in last.get("content") or []),
                reason="Original tool loop ended with unfinished tool calls" if unfinished else "Original task completed"))
        except Exception as error:
            self.events.put(dict(kind="terminal", terminated=False, truncated=True,
                evidence=self._evidence(question, True), program_metrics={},
                evaluation_metrics={}, response=None, reason=type(error).__name__ + ": " + str(error)))

    def _evidence(self, question, truncated):
        messages = self.tail[3] if self.tail else []
        final = messages[-1].get("content") if messages and messages[-1].get("role") == "assistant" else None
        return dict(question=question, trajectory=_plain(messages), final_answer=_plain(final),
                    pre_state=self.pre_state, post_state=_plain(self.tail[2]) if self.tail else None,
                    truncated=truncated)

    def reset(self, task, seed):
        # The native fixed-template tasks contain no policy-side randomization.
        # The learner seed is retained by the run manifest; no service determinism is claimed.
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=1)
            if self.worker.is_alive():
                raise ValueError("Previous AgentDojo task is still running")
        self.events, self.actions = queue.Queue(), queue.Queue()
        self.tail, self.pre_state, self.decisions = None, None, 0
        self.worker = threading.Thread(target=self._run, args=(copy.deepcopy(task),),
                                       daemon=True, name="seampact-agentdojo-task")
        self.worker.start()
        return self.events.get()

    def step(self, candidate):
        self.actions.put(candidate)
        return self.events.get()

    def close(self):
        if self.worker is not None and self.worker.is_alive():
            self.actions.put(_STOP)
            self.worker.join(timeout=1)


def make_scene(config, context):
    return AgentDojoScene(config, context)
