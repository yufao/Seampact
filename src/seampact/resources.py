"""Named local resources, connection checks and explicit capability calls."""

import base64
import binascii
import copy
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

from .intake import inside_platform_checkout
from .store import now
from .providers import ProviderError, Transport, completion_body, completion_parameters, embedding_body, image_evidence


class ResourceRegistry:
    def __init__(self, home, store, profiles):
        self.home = Path(home).resolve()
        self.store = store
        self.profiles = profiles
        self.lock = threading.RLock()

    @staticmethod
    def _digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def _local_path(self, value):
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("provide a path in the platform execution environment")
        value = value.strip()
        if "://" in value or (re.match(r"^[A-Za-z0-9_.-]+:", value) and
                not (os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", value))):
            raise ValueError("weight paths belong to this execution environment; remote machine paths are unsupported")
        path = Path(value).expanduser().resolve(strict=False)
        if inside_platform_checkout(self.home / "sources", str(path)):
            raise ValueError("权重不要登记在平台克隆的来源目录里。请把文件放到独立目录（例如平台 models 目录）后再填写路径。")
        private = getattr(self.profiles, "private_directory", None)
        if private and (path.is_relative_to(Path(private).resolve()) or
                        Path(private).resolve().is_relative_to(path)):
            raise ValueError("private credentials cannot be registered as weights")
        return path

    @staticmethod
    def _relative(value):
        if not isinstance(value, str) or not value or "\x00" in value or ":" in value:
            raise ValueError("required files must be relative to the weight directory")
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("required files must stay inside the weight directory")
        return path.as_posix()

    @staticmethod
    def _sha(value):
        if value is None or value == "":
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", value):
            raise ValueError("expected SHA-256 must contain 64 hexadecimal characters")
        return value.lower()

    def save_weight(self, body):
        if not isinstance(body, dict):
            raise ValueError("weight resource must be an object")
        with self.lock:
            identity = body.get("id") or body.get("resource_id")
            previous = self.store.get("weight_resource", identity) if identity else None
            if identity and previous is None:
                raise ValueError("unknown weight resource")
            previous = previous or {}
            identity = identity or secrets.token_hex(16)
            name = body.get("name", previous.get("name", ""))
            if not isinstance(name, str) or not name.strip():
                raise ValueError("provide a weight resource name")
            kind = body.get("kind", previous.get("kind", "directory"))
            if kind not in {"directory", "file"}:
                raise ValueError("weight kind must be directory or file")
            environment = body.get("execution_environment", previous.get("execution_environment", "local"))
            if environment != "local":
                raise ValueError("weights must be visible to the local platform execution environment")
            path = self._local_path(body.get("path", previous.get("path")))
            revision = body.get("model_revision", previous.get("model_revision"))
            if revision is not None and not isinstance(revision, str):
                raise ValueError("model revision must be a string or null")
            required = body.get("required_files", previous.get("required_files", []))
            if not isinstance(required, list):
                raise ValueError("required files must be a list")
            normalized = []
            for item in required:
                if isinstance(item, str):
                    item = dict(path=item)
                if not isinstance(item, dict) or set(item) - {"path", "sha256", "bytes"}:
                    raise ValueError("required files use path and optional sha256 or bytes")
                record = dict(path=self._relative(item.get("path")))
                if item.get("sha256"):
                    record["sha256"] = self._sha(item["sha256"])
                if "bytes" in item:
                    if type(item["bytes"]) is not int or item["bytes"] < 0:
                        raise ValueError("required file byte size must be a non-negative integer")
                    record["bytes"] = item["bytes"]
                normalized.append(record)
            if kind == "file" and normalized:
                raise ValueError("required files apply to a directory resource")
            normalized.sort(key=lambda item: item["path"])
            if len({item["path"] for item in normalized}) != len(normalized):
                raise ValueError("required file paths must be unique")
            expected_sha = self._sha(body.get("sha256", previous.get("sha256")))
            if kind == "directory" and expected_sha:
                raise ValueError("directory checksums belong to individual required files")
            config = dict(path=str(path), kind=kind, model_revision=revision,
                          required_files=normalized, sha256=expected_sha, execution_environment="local")
            digest = self._digest(config)
            changed = digest != previous.get("config_digest")
            record = dict(previous, **config, id=identity, resource_id=identity, name=name.strip(),
                          config_digest=digest, revision=previous.get("revision", 0) + int(changed),
                          created_at=previous.get("created_at", now()), updated_at=now())
            if changed:
                record["check"] = self._unchecked(record)
            self.store.save("weight_resource", identity, record)
            return copy.deepcopy(record)

    @staticmethod
    def _unchecked(record):
        return dict(status="not_checked", checked_at=None, missing_files=[], resolved_path=record["path"],
                    config_digest=record["config_digest"], revision=record["revision"],
                    target_execution="NOT_RUN", identity=None)

    def public_weights(self):
        # Observation never performs file reads, hashes, downloads or model loading.
        return self.store.list("weight_resource")

    def public_connections(self):
        connections = self.profiles.public_connections()
        records = self.store.list("connection_operation_check")
        checks = {item["id"]: item for item in self.store.list("connection_check")}
        for connection in connections:
            identity = connection["id"]
            # Public max_output_tokens may be a discovered display value, not
            # an independently configured limit. Compare the actual profile.
            profile = self.profiles.connection_profile(identity)
            parameters = {}
            for operation in ("text", "image", "embedding"):
                try:
                    parameters[operation] = self._operation_parameters(profile, operation)
                except (ValueError, TypeError, KeyError):
                    # Observation must still load when an old connection has
                    # options that the current request path cannot accept.
                    parameters[operation] = None
            current = lambda item: (item.get("connection_id", item.get("id")) == identity and
                item.get("revision") == connection.get("revision") and
                item.get("config_digest") == connection.get("config_digest") and
                item.get("auth_binding") == connection.get("auth"))
            previous = checks.get(identity)
            if previous and current(previous):
                connection["check"] = previous
            connection["operation_checks"] = {}
            for item in records:
                operation = item.get("operation")
                if (current(item) and item.get("credential_version") == connection.get("credential_version") and
                        operation not in connection["operation_checks"] and
                        self._operation_parameters_match(item, profile, parameters.get(operation))):
                    connection["operation_checks"][operation] = item
        return connections

    @staticmethod
    def _operation_parameters(profile, operation):
        if operation == "embedding":
            return {name: value for name, value in embedding_body(profile, "").items()
                    if name not in {"model", "input"}}
        return completion_parameters(profile)

    @staticmethod
    def _operation_parameters_match(record, profile, parameters):
        if parameters is None:
            return False
        if "request_parameters" in record:
            return record["request_parameters"] == parameters
        # Before request options were forwarded, only empty-JSON operations can
        # retain their meaning. Keep their history even when no longer current.
        if profile.get("parameters"):
            return False
        legacy = {}
        if record.get("operation") in {"text", "image"}:
            selected = (profile.get("capabilities") or {}).get("selected") or {}
            ceiling = (selected.get("max_output_tokens") or profile.get("declared_max_output_tokens")
                       or profile.get("max_output_tokens"))
            if ceiling:
                legacy["max_tokens"] = ceiling
        return legacy == parameters

    @staticmethod
    def _usage(value):
        if not isinstance(value, dict):
            return None
        result = {name: value[name] for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                  "input_tokens", "output_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
                  if type(value.get(name)) is int and value[name] >= 0}
        for name in ("prompt_tokens_details", "completion_tokens_details"):
            details = value.get(name)
            if isinstance(details, dict):
                result[name] = {field: details[field] for field in ("cached_tokens", "audio_tokens",
                    "reasoning_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens")
                    if type(details.get(field)) is int and details[field] >= 0}
        return result or None

    def invoke_connection(self, body):
        """Run one user-requested operation; this is never a Harness target result."""
        if not isinstance(body, dict):
            raise ValueError("connection capability call must be an object")
        if set(body) - {"connection_id", "operation", "input", "image_path"}:
            raise ValueError("connection capability calls accept only the declared input fields")
        operation, text = body.get("operation"), body.get("input")
        if operation not in {"text", "image", "embedding"}:
            raise ValueError("connection operation must be text, image or embedding")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("provide explicit nonempty text for this capability call")
        if operation != "image" and "image_path" in body:
            raise ValueError("image_path is only supported for an image capability call")
        with self.profiles.lock:
            profile = self.profiles.connection_profile(body.get("connection_id"))
            if profile.get("protocol") != "openai-compatible":
                raise ValueError("capability calls require an OpenAI-compatible connection")
            if operation not in profile.get("service_capabilities", []):
                raise ValueError("selected connection has not declared this capability")
            if not profile.get("model"):
                raise ValueError("capability calls require the selected connection's model ID")
            credential_error = None
            try:
                key = self.profiles.connection_key(profile["id"])
                if profile.get("auth", {}).get("mode") != "none" and not key:
                    credential_error = "selected connection credentials are missing"
            except (ValueError, OSError):
                key, credential_error = "", "selected connection credentials cannot be read by this runtime"
        # Reject configuration conflicts before any paid request, with the field
        # explanation intact instead of the generic provider-response diagnostic.
        request = embedding_body(profile, text) if operation == "embedding" else None
        parameters = ({name: value for name, value in request.items() if name not in {"model", "input"}}
                      if request is not None else completion_parameters(profile))
        result = dict(id=secrets.token_hex(16), connection_id=profile["id"], revision=profile["revision"],
            config_digest=profile["config_digest"], credential_ref=profile.get("credential_ref"),
            credential_version=profile.get("credential_version"), auth_binding=copy.deepcopy(profile["auth"]),
            operation=operation, checked_at=now(),
            status="FAILED", latency_ms=None, usage=None, target_execution="NOT_RUN", response=None,
            reason=credential_error, requests=0, money_status="UNKNOWN", server_inference_ms=None,
            request_parameters=parameters,
            input_identity=dict(sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), characters=len(text)))
        started = time.perf_counter()
        try:
            if credential_error:
                raise ValueError("credential_unavailable")
            if operation == "embedding":
                endpoint = "/embeddings"
            else:
                content = text
                if operation == "image":
                    path = self._local_path(body.get("image_path"))
                    image, identity = image_evidence(str(path))
                    result["image_identity"] = identity
                    content = [dict(type="text", text=text), image]
                endpoint = "/chat/completions"
                request = completion_body(profile, [dict(role="user", content=content)])
            result["requests"] = 1
            data = Transport().send(profile["base_url"].rstrip("/") + endpoint, key, request)
            if not isinstance(data, dict):
                raise ValueError("invalid_provider_response")
            result["usage"] = self._usage(data.get("usage"))
            if operation == "embedding":
                values = data.get("data")
                if not isinstance(values, list) or not values:
                    raise ValueError("missing_embeddings")
                dimensions = []
                for item in values:
                    vector = item.get("embedding") if isinstance(item, dict) else None
                    if isinstance(vector, str) and request.get("encoding_format") == "base64":
                        try:
                            encoded = base64.b64decode(vector, validate=True)
                        except (ValueError, binascii.Error):
                            raise ValueError("invalid_embedding_vector") from None
                        if (not encoded or len(encoded) % 4 or
                                any(not math.isfinite(number[0]) for number in struct.iter_unpack("<f", encoded))):
                            raise ValueError("invalid_embedding_vector")
                        dimensions.append(len(encoded) // 4)
                    elif isinstance(vector, list) and vector and all(type(number) in (int, float)
                            and math.isfinite(number) for number in vector):
                        dimensions.append(len(vector))
                    else:
                        raise ValueError("invalid_embedding_vector")
                result["response"] = dict(embeddings_count=len(values), dimensions=dimensions)
            else:
                choices = data.get("choices")
                message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list) and all(isinstance(item, dict) and item.get("type") == "text"
                                                    and isinstance(item.get("text"), str) for item in content):
                    content = "\n".join(item["text"] for item in content)
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("missing_text_response")
                if key:
                    content = content.replace(key, "[private credential]")
                result["response"] = dict(text=content[:4096], characters=len(content), truncated=len(content) > 4096)
            result.update(status="SUCCEEDED", reason=None)
        except ProviderError as error:
            # Transport exposes only a status/category, never an HTTP response body.
            reason = str(error)
            result["reason"] = reason.replace(key, "[private credential]") if key else reason
        except (ValueError, OSError, TypeError, KeyError, OverflowError):
            result["reason"] = credential_error or "input or provider response does not satisfy the selected operation"
        finally:
            result.update(latency_ms=round((time.perf_counter() - started) * 1000, 3), finished_at=now())
        self.store.create("connection_operation_check", result["id"], result)
        return copy.deepcopy(result)

    @staticmethod
    def _file_identity(path, expected):
        info = path.stat()
        result = dict(path=str(path), bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                      identity_basis="path_size_mtime", content_identity="UNKNOWN")
        if "bytes" in expected:
            result["expected_bytes"] = expected["bytes"]
            result["size_matches"] = info.st_size == expected["bytes"]
        with path.open("rb") as stream:
            if expected.get("sha256"):
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
                result.update(sha256=digest.hexdigest(), identity_basis="sha256",
                              expected_sha256=expected["sha256"],
                              checksum_matches=digest.hexdigest() == expected["sha256"],
                              content_identity="checked_sha256")
            else:
                stream.read(1)
        result["readable"] = True
        return result

    def check_weight(self, identity):
        with self.lock:
            record = self.store.get("weight_resource", identity)
            if record is None:
                raise ValueError("unknown weight resource")
            result = self._unchecked(record)
            result.update(checked_at=now(), status="present", files=[], missing_files=[],
                          mismatches=[], unreadable_files=[], index_files=[], errors=[],
                          exists=None, readable=None,
                          identity=dict(basis="path_size_mtime", content_identity="UNKNOWN"),
                          identity_note="Path, size and mtime do not prove unchanged file content.")
            try:
                path = self._local_path(record["path"])
                result["resolved_path"] = str(path)
                if not path.exists():
                    result.update(status="missing", exists=False, readable=False,
                                  missing_files=([item["path"] for item in record["required_files"]]
                                                 or [str(path)]))
                elif (record["kind"] == "file" and not path.is_file()) or (
                        record["kind"] == "directory" and not path.is_dir()):
                    result.update(status="wrong_type", exists=True, readable=False)
                elif record["kind"] == "file":
                    result["exists"] = True
                    result["files"].append(self._file_identity(path, record))
                else:
                    result["exists"] = True
                    # Listing the root also checks directory readability without importing it.
                    with os.scandir(path):
                        pass
                    required = {item["path"]: dict(item) for item in record["required_files"]}
                    for filename in ("model.safetensors.index.json", "pytorch_model.bin.index.json"):
                        index = path / filename
                        if not index.exists():
                            continue
                        result["index_files"].append(filename)
                        required.setdefault(filename, dict(path=filename))
                        try:
                            if index.stat().st_size > 32 * 1024 * 1024:
                                raise ValueError("index_too_large")
                            data = json.loads(index.read_text(encoding="utf-8"))
                            mapping = data.get("weight_map") if isinstance(data, dict) else None
                            if not isinstance(mapping, dict) or not mapping:
                                raise ValueError("invalid_weight_map")
                            for shard in mapping.values():
                                relative = self._relative(shard)
                                required.setdefault(relative, dict(path=relative))
                        except (ValueError, UnicodeError, OSError):
                            result["errors"].append(dict(file=filename, category="invalid_or_unreadable_index"))
                    for relative, expected in sorted(required.items()):
                        try:
                            selected = self._local_path(str(path / relative))
                            if not selected.is_relative_to(path):
                                raise ValueError("required_file_escapes_directory")
                            if not selected.is_file():
                                result["missing_files"].append(relative)
                                continue
                            result["files"].append(self._file_identity(selected, expected))
                        except ValueError:
                            result["errors"].append(dict(file=relative, category="unsupported_required_file_path"))
                        except OSError:
                            result["unreadable_files"].append(relative)
                for item in result["files"]:
                    if item.get("size_matches") is False or item.get("checksum_matches") is False:
                        result["mismatches"].append(item["path"])
                if result["status"] == "present":
                    result["status"] = ("invalid_manifest" if result["errors"] else "unreadable" if
                        result["unreadable_files"] else "missing" if result["missing_files"] else
                        "checksum_or_size_mismatch" if result["mismatches"] else "present")
                if result["files"] and all(item.get("sha256") for item in result["files"]):
                    result["identity"] = dict(basis="checked_file_sha256", content_identity="listed_files_only")
                if result["exists"] and result["status"] not in {"wrong_type", "unreadable"}:
                    result["readable"] = True
                result["necessary_files"] = ("present" if result["status"] == "present" and
                    (record["kind"] == "file" or record["required_files"] or result["index_files"])
                    else "not_declared" if result["status"] == "present" else "unsatisfied")
            except ValueError:
                result.update(status="unsupported_path", error_category="invalid_local_path")
            except OSError:
                result.update(status="unreadable", readable=False, error_category="filesystem_access")
            result["finished_at"] = now()
            record["check"] = result
            self.store.save("weight_resource", identity, record)
            return copy.deepcopy(record)

    def check_connection(self, identity):
        # Resolve authentication beside the immutable profile copy, never into Store or a URL.
        with self.profiles.lock:
            profile = self.profiles.connection_profile(identity)
            try:
                key = self.profiles.connection_key(identity)
                credential_status = ("not_required" if profile.get("auth", {}).get("mode") == "none"
                                     else "resolvable" if key else "missing")
            except (ValueError, OSError):
                key = ""
                credential_status = "unreadable"
        result = dict(id=profile["connection_id"], connection_id=profile["connection_id"], revision=profile["revision"],
                      config_digest=profile["config_digest"], checked_at=now(),
                      auth_binding=copy.deepcopy(profile["auth"]),
                      protocol=profile["protocol"], status="not_checked", accessible=False,
                      credential_status=credential_status, model_in_list=None,
                      capability_status="NOT_RUN", target_execution="NOT_RUN")
        if credential_status in {"missing", "unreadable"}:
            result["status"] = "credential_" + credential_status
        else:
            base = profile.get("base_url", "").rstrip("/")
            url = base + "/models" if profile["protocol"] == "openai-compatible" else base
            headers = {"Accept": "application/json"}
            if key:
                headers["Authorization"] = "Bearer " + key
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                    return None
            try:
                request = urllib.request.Request(url, headers=headers, method="GET")
                with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                    result.update(http_status=response.status, accessible=True, status="accessible")
                    if profile["protocol"] == "openai-compatible":
                        raw = response.read(2 * 1024 * 1024 + 1)
                        if len(raw) > 2 * 1024 * 1024:
                            raise ValueError("models_response_too_large")
                        data = json.loads(raw)
                        models = data.get("data") if isinstance(data, dict) else None
                        if not isinstance(models, list):
                            raise ValueError("invalid_models_response")
                        result.update(models_count=len(models), model_in_list=(any(
                            isinstance(item, dict) and item.get("id") == profile.get("model")
                            for item in models) if profile.get("model") else None),
                            status="models_list_accessible")
            except urllib.error.HTTPError as error:
                result.update(http_status=error.code, accessible=True,
                              status="authentication_failed" if error.code in {401, 403} else
                              "accessible_http_error", error_category="http_status")
            except urllib.error.URLError as error:
                reason = error.reason
                category = ("dns" if isinstance(reason, socket.gaierror) else
                            "tls" if isinstance(reason, ssl.SSLError) else
                            "timeout" if isinstance(reason, TimeoutError) else "connection_failed")
                result.update(status="connection_failed", error_category=category)
            except (OSError, http.client.HTTPException):
                result.update(status="connection_failed", error_category="transport")
            except (ValueError, UnicodeError):
                result.update(status="invalid_response", error_category="invalid_models_response")
        result["finished_at"] = now()
        self.store.save("connection_check", profile["connection_id"], result)
        return copy.deepcopy(result)
