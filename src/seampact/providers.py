"""Independent chat, analysis and teacher roles with compatible transport."""

import base64
import copy
import ctypes
import hashlib
import http.client
import itertools
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from seampact.store import now

PRESETS = {
    "deepseek": dict(base_url="https://api.deepseek.com", model="deepseek-flash",
                     key_env="DEEPSEEK_API_KEY", docs="https://api-docs.deepseek.com/api/list-models/"),
    "glm": dict(base_url="https://open.bigmodel.cn/api/paas/v4", model="",
                key_env="GLM_API_KEY", docs="https://docs.bigmodel.cn/")}
ROLES = ("chat", "analysis", "teacher")


def image_evidence(value, detail="auto"):
    """Read actual local image bytes; return wire content and a compact identity."""
    if isinstance(value, dict):
        if value.get("source") not in {"path", "file"}:
            raise ValueError("teacher image evidence requires a local path")
        value = value.get("value")
    if not isinstance(value, str) or detail not in {"auto", "low", "high"}:
        raise ValueError("invalid teacher image evidence")
    path = Path(value).resolve()
    data = path.read_bytes()
    mime = ("image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else
            "image/jpeg" if data.startswith(b"\xff\xd8\xff") else
            "image/gif" if data.startswith((b"GIF87a", b"GIF89a")) else
            "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
    if mime is None:
        raise ValueError("unsupported teacher image format")
    identity = dict(path=str(path), sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), mime_type=mime, detail=detail)
    return dict(type="image_url", image_url=dict(url="data:" + mime + ";base64," +
                base64.b64encode(data).decode("ascii"), detail=detail)), identity


def protected(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("Windows credentials require the original user; configure a key or environment variable")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    operation = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ValueError("current-user credential protection failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def provider_name(base_url):
    host = urlsplit(base_url).hostname
    if host == "api.deepseek.com":
        return "deepseek"
    if host in {"open.bigmodel.cn", "api.z.ai"}:
        return "glm"
    return "openai-compatible"


def output_capability(profile):
    selected = (profile.get("capabilities") or {}).get("selected") or {}
    return (profile.get("declared_max_output_tokens") or profile.get("max_output_tokens")
            or selected.get("max_output_tokens"))


def capability_metadata(value):
    """Retain model-list facts, rather than arbitrary server response fields."""
    if not isinstance(value, dict):
        return None
    def model(item):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        result = dict(id=item["id"])
        for name in ("max_output_tokens", "context_length", "context_window"):
            if type(item.get(name)) is int and item[name] > 0:
                result[name] = item[name]
        # vLLM reports the running engine's total sequence capacity in /models.
        served_window = item.get("max_model_len")
        if type(served_window) is int and served_window > 0:
            result["context_window"] = served_window
        return result
    raw_models = value.get("models")
    models = [item for item in (model(item) for item in (raw_models if isinstance(raw_models, list) else []))
              if item is not None]
    return dict(checked_at=value.get("checked_at") if isinstance(value.get("checked_at"), str) else None,
                models=models, selected=model(value.get("selected")))


class Profiles:
    def __init__(self, directory, legacy=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / "providers.json"
        self.private_directory = self.directory / "private"
        self.credentials_file = self.private_directory / "credentials.json"
        self.lock = threading.RLock()
        self._private_directory()
        if self.credentials_file.exists():
            self._private_file(self.credentials_file)
            self.credentials = json.loads(self.credentials_file.read_text(encoding="utf-8")).get("credentials", {})
        else:
            self.credentials = {}
        value = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else None
        if value and value.get("schema") == "named-connections/1":
            self.connections = value["connections"]
            self.roles = value["roles"]
            self.presets = value.get("presets", {})
        else:
            self.connections, self.roles, self.presets = {}, {}, {}
            self._migrate(value, Path(legacy) if legacy else None)
        self._save()
        old_private = self.directory / "credentials.json"
        if getattr(self, "migrated_legacy_credentials", False) and old_private.is_file():
            # Remove only this configuration home's obsolete credential file after
            # both the private atomic write and public metadata migration succeeded.
            old_private.unlink()

    def _private_directory(self):
        if not self.private_directory.exists():
            self.private_directory.mkdir(mode=0o700)
        info = self.private_directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or self.private_directory.is_symlink():
            raise ValueError("credential directory must be a private directory")
        if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("credential directory must be owned by the runtime user with mode 0700")

    @staticmethod
    def _private_file(path):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ValueError("credential storage must be a private regular file")
        if os.name != "nt" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("credential file must be owned by the runtime user with mode 0600")

    def _save_private(self):
        self._private_directory()
        if self.credentials_file.exists():
            self._private_file(self.credentials_file)
        temporary = self.private_directory / ("credentials-" + secrets.token_hex(16) + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(dict(schema="private-credentials/1", credentials=self.credentials), stream,
                          ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            self._private_file(temporary)
            temporary.replace(self.credentials_file)
            self._private_file(self.credentials_file)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _migrate(self, value, legacy):
        old_credentials = {}
        old_private = self.directory / "credentials.json"
        if old_private.is_file():
            self._private_file(old_private)
            old_credentials = json.loads(old_private.read_text(encoding="utf-8"))
            self.migrated_legacy_credentials = True
        if value is None and legacy:
            candidates = [legacy] if legacy.is_file() else [legacy / "providers.json", legacy / "config" / "providers.json"]
            source = next((path for path in candidates if path.name == "providers.json" and path.is_file()), None)
            if source:
                value = json.loads(source.read_text(encoding="utf-8"))
                private = source.parent / "credentials.json"
                if private.is_file():
                    self._private_file(private)
                    old_credentials = json.loads(private.read_text(encoding="utf-8"))
            else:
                directory = legacy.parent if legacy.is_file() else legacy
                models_file = directory / "models.json"
                if models_file.is_file():
                    models = json.loads(models_file.read_text(encoding="utf-8"))
                    previous = next((item for item in models.get("profiles", {}).values()
                        if item.get("provider") == "deepseek" and item.get("enabled") and
                        urlsplit(item.get("base_url", "")).hostname == "api.deepseek.com"), None)
                    if previous:
                        profile = dict(provider="deepseek", **PRESETS["deepseek"], capabilities=None)
                        profile.update(base_url=previous["base_url"].rstrip("/"), model=previous["model"],
                                       key_env=previous.get("api_key_env", PRESETS["deepseek"]["key_env"]))
                        vault_file = directory / "credentials.dpapi.json"
                        if previous.get("api_key_ref") and vault_file.is_file():
                            self._private_file(vault_file)
                            vault = json.loads(vault_file.read_text(encoding="utf-8"))
                            profile["credential"] = vault.get(previous["api_key_ref"])
                        value = dict(deepseek=profile)
        old = (value or {}).get("profiles", value or {})
        for name, preset in PRESETS.items():
            old.setdefault(name, dict(provider=name, **preset, capabilities=None))
        original = old["deepseek"]
        configured = bool(original.get("base_url") and original.get("model") and (
            original.get("api_key") or original.get("credential") or
            old_credentials.get(original.get("credential_ref")) or os.environ.get(original.get("key_env", ""))))
        for role in ROLES:
            if role not in old:
                old[role] = dict(original) if configured and role != "teacher" else dict(
                    provider="openai-compatible", base_url="", model="", capabilities=None,
                    key_env="SEAMPACT_" + role.upper() + "_API_KEY")
        # Each previous role/provider gets its own identity, including identical keys.
        # DPAPI blobs move into private storage without requiring migration-time decrypt.
        for identity, profile in old.items():
            connection_id = secrets.token_hex(16)
            base = self._url(profile["base_url"]) if profile.get("base_url") else ""
            connection = dict(id=connection_id, name=profile.get("name") or identity,
                protocol="openai-compatible", base_url=base, model=profile.get("model", ""),
                provider=profile.get("provider") or provider_name(base),
                service_capabilities=["text"], parameters={}, service_version=profile.get("service_version"),
                capabilities=capability_metadata(profile.get("capabilities")), declared_max_output_tokens=profile.get(
                    "declared_max_output_tokens", profile.get("max_output_tokens")),
                docs=profile.get("docs"), revision=1, created_at=now(), updated_at=now())
            private = old_credentials.get(profile.get("credential_ref"))
            if profile.get("credential"):
                ref = self._new_credential(profile["credential"], encoded=True)
                connection["auth"] = dict(mode="saved", credential_ref=ref,
                    credential_version=self.credentials[ref]["version"],
                    fallback_env=profile.get("key_env", ""))
            elif profile.get("api_key") or private:
                ref = self._new_credential(profile.get("api_key") or private)
                connection["auth"] = dict(mode="saved", credential_ref=ref,
                    credential_version=self.credentials[ref]["version"],
                    fallback_env=profile.get("key_env", ""))
            elif profile.get("key_env"):
                connection["auth"] = dict(mode="environment", env=profile["key_env"])
            else:
                connection["auth"] = dict(mode="none")
            connection["config_digest"] = self._digest(connection)
            self.connections[connection_id] = connection
            if identity in ROLES:
                self.roles[identity] = connection_id
            else:
                self.presets[identity] = connection_id
        self._save_private()

    def _save(self):
        temporary = self.file.with_suffix(".tmp")
        value = dict(schema="named-connections/1", connections=self.connections, roles=self.roles, presets=self.presets)
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)

    def _new_credential(self, value, encoded=False):
        ref = secrets.token_hex(24)
        if encoded or os.name == "nt":
            secret = value if encoded else base64.b64encode(protected(value.encode("utf-8"))).decode("ascii")
            mode = "windows_dpapi"
        else:
            secret, mode = value, "private_file"
        self.credentials[ref] = dict(mode=mode, value=secret, version=secrets.token_hex(16))
        return ref

    @staticmethod
    def _digest(connection):
        # Credentials have a separate identity/version; neither keys nor their hashes
        # are part of the execution configuration's model/resource identity.
        fields = {name: connection.get(name) for name in ("protocol", "base_url", "model", "parameters",
                  "service_capabilities", "service_version", "declared_max_output_tokens",
                  "declared_context_window", "context_management", "resource_kind",
                  "managed_service_id", "managed_service_digest")}
        return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

    def _connection_id(self, identity):
        selected = self.roles.get(identity, self.presets.get(identity, identity))
        if selected not in self.connections:
            raise ValueError("unknown service connection")
        return selected

    @staticmethod
    def _url(base, cloud=False):
        if not isinstance(base, str):
            raise ValueError("provide an HTTP or HTTPS service base URL")
        base = base.strip().rstrip("/")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in ({"https"} if cloud else {"http", "https"}) and parsed.hostname and
                     not (parsed.username or parsed.password or parsed.query or parsed.fragment))
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("use an HTTPS service base URL without credentials or query" if cloud else
                             "use an HTTP or HTTPS service base URL without credentials or query")
        return base

    @staticmethod
    def _parameters(value):
        if not isinstance(value, dict):
            raise ValueError("service parameters must be a non-sensitive object")
        def check(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str) or re.search(
                            r"api.?key|credential|secret|password|authorization|^(access_|refresh_|bearer_)?token$", key, re.I):
                        raise ValueError("credentials must use the private credential channel")
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)
            elif not isinstance(item, (str, int, float, bool, type(None))):
                raise ValueError("service parameters must contain JSON values")
        check(value)
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError("service parameters must contain finite JSON values") from None
        return copy.deepcopy(value)

    def _auth(self, body):
        if not isinstance(body, dict) or body.get("mode") not in {"none", "saved", "environment"}:
            raise ValueError("authentication mode must be none, saved or environment")
        if set(body) - {"mode", "env", "credential_ref", "credential_version"}:
            raise ValueError("credentials must use the private credential channel")
        mode = body["mode"]
        if mode == "environment":
            env = body.get("env", "")
            if not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
                raise ValueError("provide an environment variable name")
            return dict(mode=mode, env=env)
        if mode == "saved":
            ref = body.get("credential_ref")
            if ref is None:
                return dict(mode=mode)
            if not isinstance(ref, str) or ref not in self.credentials:
                raise ValueError("saved credential reference is unavailable")
            return dict(mode=mode, credential_ref=ref, credential_version=self.credentials[ref]["version"])
        return dict(mode=mode)

    def connection_key(self, connection_id):
        with self.lock:
            connection = self.connections[self._connection_id(connection_id)]
            auth = connection.get("auth", {"mode": "none"})
            if auth["mode"] == "none":
                return ""
            if auth["mode"] == "environment":
                return os.environ.get(auth["env"], "")
            record = self.credentials.get(auth.get("credential_ref"))
            if record:
                try:
                    self._private_directory()
                    self._private_file(self.credentials_file)
                    if record["mode"] == "windows_dpapi":
                        return protected(base64.b64decode(record["value"]), True).decode("utf-8")
                    if record["mode"] == "private_file" and os.name != "nt":
                        return record["value"]
                    raise ValueError("credential storage platform differs from this runtime")
                except (ValueError, OSError, UnicodeError):
                    if auth.get("fallback_env") and os.environ.get(auth["fallback_env"]):
                        return os.environ[auth["fallback_env"]]
                    raise ValueError("saved credential cannot be read by this runtime user; save it again or select an environment reference") from None
            if auth.get("fallback_env"):
                return os.environ.get(auth["fallback_env"], "")
            return ""

    def key(self, identity):
        return self.connection_key(identity)

    def _public(self, identity):
        profile = self.profile(identity)
        auth = profile["auth"]
        error = None
        try:
            usable = bool(self.key(identity))
        except (ValueError, OSError):
            usable = False
            error = "已有密钥在当前环境无法读取，请重新填写或配置对应环境变量。"
        configured = bool(profile.get("base_url") and (identity not in ROLES or profile.get("model")))
        key_present = bool(auth.get("credential_ref") or (auth["mode"] == "environment" and
                           os.environ.get(auth.get("env", ""))))
        stored_mode = self.credentials.get(auth.get("credential_ref"), {}).get("mode", "saved")
        ready = configured and not error and (usable or auth["mode"] == "none")
        return dict(profile, identity=identity, max_output_tokens=output_capability(profile),
                    configured=configured, ready=ready, key_present=key_present,
                    key_usable=usable, credential_error=error,
                    credential_status=("not_required" if auth["mode"] == "none" else "unreadable" if error else
                                       "resolvable" if usable else "missing"),
                    credential_mode=(auth["mode"] if auth["mode"] != "saved" else stored_mode),
                    usage_policy="observe_only", round_limit=None, context_truncation=False)

    def public(self):
        with self.lock:
            return [self._public(identity) for identity in list(self.presets) + list(ROLES)]

    def public_connections(self):
        with self.lock:
            return [self._public(connection_id) for connection_id in self.connections]

    def save_connection(self, body):
        with self.lock:
            requested_id = body.get("id") or body.get("connection_id")
            if requested_id is not None and requested_id not in self.connections:
                raise ValueError("unknown service connection")
            connection_id = requested_id or secrets.token_hex(16)
            previous = self.connections.get(connection_id, {})
            if set(body) & {"api_key", "credential", "key", "secret", "password"}:
                raise ValueError("credentials must use the private credential channel")
            name = body.get("name", previous.get("name", "")).strip()
            if not name:
                raise ValueError("provide a service connection name")
            protocol = body.get("protocol", previous.get("protocol", "openai-compatible"))
            if protocol not in {"openai-compatible", "http"}:
                raise ValueError("supported service protocols: openai-compatible, http")
            base = self._url(body.get("base_url", previous.get("base_url", "")))
            model = body.get("model", previous.get("model", ""))
            if not isinstance(model, str):
                raise ValueError("service model ID must be a string")
            declared = body.get("capabilities", previous.get("service_capabilities", []))
            if not isinstance(declared, list) or any(item not in {"text", "image", "embedding", "retrieval"} for item in declared):
                raise ValueError("service capabilities must be text, image, embedding or retrieval")
            ceiling = body.get("max_output_tokens", previous.get("declared_max_output_tokens"))
            if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
                raise ValueError("provider output capability must be a positive integer or null")
            window = body.get('context_window', previous.get('declared_context_window'))
            if window is not None and (type(window) is not int or window < 1):
                raise ValueError('context window must be a positive integer or null')
            manage = body.get('context_management', previous.get('context_management', True))
            if type(manage) is not bool:
                raise ValueError('context management must be true or false')
            version = body.get("service_version", previous.get("service_version"))
            if version is not None and not isinstance(version, str):
                raise ValueError("service version must be a string or null")
            replacement = dict(previous, id=connection_id, name=name, protocol=protocol, base_url=base,
                model=model.strip(), provider=provider_name(base), service_capabilities=sorted(set(declared)),
                parameters=self._parameters(body.get("parameters", previous.get("parameters", {}))),
                service_version=version, declared_max_output_tokens=ceiling,
                declared_context_window=window, context_management=manage,
                created_at=previous.get("created_at", now()), updated_at=now())
            kind = body.get("resource_kind", previous.get("resource_kind", "borrowed"))
            if kind not in {"cloud", "borrowed", "managed"}:
                raise ValueError("连接来源请选择云端、借用服务或平台管理服务")
            replacement["resource_kind"] = kind
            replacement.pop("managed_service_id", None)
            replacement.pop("managed_service_digest", None)
            if kind == "managed":
                service_id = body.get("managed_service_id", previous.get("managed_service_id"))
                manager = getattr(self, "managed_services", None)
                if manager is None:
                    raise ValueError("当前平台没有受管服务执行器")
                service = manager.get(service_id)
                replacement.update(managed_service_id=service["id"], managed_service_digest=service["spec_digest"])
                manager.validate_connection(replacement)
            if protocol == "openai-compatible" and connection_id in self.roles.values():
                completion_parameters(replacement)
            if "auth" in body:
                replacement["auth"] = self._auth(body["auth"])
            elif base != previous.get("base_url"):
                # A changed target must not inherit authentication or silently
                # convert missing authentication into an explicit no-auth choice.
                replacement["auth"] = dict(mode="saved")
            else:
                replacement["auth"] = copy.deepcopy(previous.get("auth", dict(mode="none")))
            fingerprint = self._digest(replacement)
            changed = fingerprint != previous.get("config_digest")
            replacement.update(config_digest=fingerprint, revision=previous.get("revision", 0) + int(changed))
            if changed:
                replacement["capabilities"] = None
            self.connections[connection_id] = replacement
            self._save()
            return self._public(connection_id)

    def save_credential(self, body):
        with self.lock:
            connection_id = self._connection_id(body["connection_id"])
            connection = self.connections[connection_id]
            previous_ref = connection.get("auth", {}).get("credential_ref")
            action = body.get("action")
            if action == "clear":
                connection["auth"] = dict(mode="none")
            elif action == "save":
                value = body.get("value", "")
                if not isinstance(value, str):
                    raise ValueError("invalid API credential")
                value = value.strip()
                # Blank means retain; only the explicit clear action removes it.
                if value:
                    if any(character.isspace() for character in value) or len(value) > 8192:
                        raise ValueError("invalid API credential")
                    ref = self._new_credential(value)
                    self._save_private()
                    connection["auth"] = dict(mode="saved", credential_ref=ref,
                        credential_version=self.credentials[ref]["version"])
            else:
                raise ValueError("credential action must be save or clear")
            if (previous_ref and previous_ref != connection.get("auth", {}).get("credential_ref") and
                    not any(item.get("auth", {}).get("credential_ref") == previous_ref for item in self.connections.values())):
                self.credentials.pop(previous_ref, None)
                self._save_private()
            connection["updated_at"] = now()
            self._save()
            return self._public(connection_id)

    def select_role(self, role, connection_id):
        self.role_identity(role)
        with self.lock:
            selected = self._connection_id(connection_id)
            connection = self.connections[selected]
            if connection["protocol"] != "openai-compatible" or not connection.get("model"):
                raise ValueError("platform model roles require an OpenAI-compatible connection with a model ID")
            self._url(connection["base_url"])
            completion_parameters(connection)
            self.roles[role] = selected
            self._save()
            return self._public(role)

    def role_identity(self, role):
        if role not in ROLES:
            raise ValueError("model role must be chat, analysis or teacher")
        return role

    def role_profile(self, role):
        return self.profile(self.role_identity(role))

    def role_key(self, role):
        return self.key(self.role_identity(role))

    def public_roles(self):
        with self.lock:
            return {role: self._public(self.role_identity(role)) for role in ROLES}

    def save_role(self, role, body):
        identity = self.role_identity(role)
        with self.lock:
            if body.get("connection_id"):
                return self.select_role(role, body["connection_id"])
            self._update(identity, body, cloud=False)
            return self._public(identity)

    def save(self, identity, body):
        if identity in ROLES:
            return self.save_role(identity, body)
        if identity not in PRESETS:
            raise ValueError("supported providers: deepseek, glm")
        with self.lock:
            self._update(identity, body, cloud=True)

    def _update(self, identity, body, cloud):
        connection_id = self._connection_id(identity)
        previous = self.connections[connection_id]
        base = self._url(body.get("base_url", previous.get("base_url", "")), cloud=cloud)
        model = body.get("model", previous.get("model", "")).strip()
        if not model:
            raise ValueError("provide a model ID")
        update = dict(id=connection_id, base_url=base, model=model)
        for name in ("name", "parameters", "auth", "service_version", "max_output_tokens", "context_window", "context_management"):
            if name in body:
                update[name] = body[name]
        if "service_version" not in body and (base != previous.get("base_url") or model != previous.get("model")):
            update["service_version"] = None
        # The legacy role endpoint remains accepted; new UI uses save_credential.
        key = body.get("api_key", "")
        if key is not None and (not isinstance(key, str) or any(character.isspace() for character in key.strip()) or len(key) > 8192):
            raise ValueError("invalid API credential")
        self.save_connection(update)
        if key:
            self.save_credential(dict(connection_id=connection_id, action="save", value=key))

    def discover(self, identity, transport=None):
        with self.lock:
            profile = self.profile(identity)
            key = self.key(profile["connection_id"])
        data = (transport or Transport()).send(profile["base_url"] + "/models", key)
        models = data.get("data", [])
        if not isinstance(models, list):
            raise ValueError("provider models response must contain an array")
        return self.record_model_list(profile, models)

    def record_model_list(self, profile, models):
        match = next((model for model in models if isinstance(model, dict) and model.get("id") == profile["model"]), None)
        capabilities = capability_metadata(dict(checked_at=now(), models=models, selected=match))
        with self.lock:
            current = self.connections.get(profile["connection_id"])
            if current and (current["revision"], current["config_digest"]) == (profile["revision"], profile["config_digest"]):
                current["capabilities"] = capabilities
                self._save()
        return capabilities

    def profile(self, identity):
        with self.lock:
            connection_id = self._connection_id(identity)
            stored = self.connections[connection_id]
            # Profile snapshots may enter analysis and run artifacts. Only the
            # declared nonsecret configuration contract is copied into them.
            fields = ("id", "name", "protocol", "base_url", "model", "provider", "service_capabilities",
                      "parameters", "service_version", "declared_max_output_tokens", "declared_context_window",
                      "context_management", "docs", "revision",
                      "resource_kind", "managed_service_id", "managed_service_digest",
                      "created_at", "updated_at", "config_digest")
            connection = {name: copy.deepcopy(stored[name]) for name in fields if name in stored}
            connection["parameters"] = self._parameters(connection.get("parameters", {}))
            connection["capabilities"] = capability_metadata(stored.get("capabilities"))
            connection["auth"] = {name: value for name, value in stored["auth"].items()
                                  if name in {"mode", "env", "credential_ref", "credential_version", "fallback_env"}}
            auth = connection["auth"]
            connection.update(connection_id=connection_id,
                credential_ref=auth.get("credential_ref"), credential_version=auth.get("credential_version"),
                key_env=auth.get("env", auth.get("fallback_env", "")))
            if identity in ROLES:
                connection["role"] = identity
            return connection

    def connection_profile(self, connection_id):
        return self.profile(connection_id)


class FrozenProfiles:
    """Freeze ordinary Agent model bindings without importing an experiment driver."""
    def __init__(self, profiles):
        self.managed_services = getattr(profiles, "managed_services", None)
        self.keys = {}
        with profiles.lock:
            self.profiles = {role: copy.deepcopy(profiles.role_profile(role)) for role in ROLES}
            for role, profile in self.profiles.items():
                if profile.get("base_url") and profile.get("model"):
                    try:
                        self.keys[role] = profiles.role_key(role)
                    except Exception:
                        self.keys[role] = None

    def role_profile(self, role):
        return copy.deepcopy(self.profiles[role])

    def role_key(self, role):
        if self.keys.get(role) is None:
            raise ValueError("运行创建时未取得该模型凭据：" + role)
        return self.keys[role]

    def public(self):
        result = {}
        for role, profile in self.profiles.items():
            parameters = completion_parameters(profile)
            result[role] = dict(provider=profile.get("provider"), base_url=profile.get("base_url"),
                model=profile.get("model"), service_version=profile.get("service_version"),
                max_output_tokens=parameters.get("max_tokens", parameters.get("max_completion_tokens")),
                **{name: copy.deepcopy(profile.get(name)) for name in (
                    "connection_id", "config_digest", "revision", "protocol", "parameters", "resource_kind",
                    "managed_service_id", "managed_service_digest")})
            if profile.get("parameters"):
                result[role]["request_parameters"] = parameters
        return result

    def resources(self):
        return {role: {name: copy.deepcopy(profile.get(name)) for name in (
            "connection_id", "config_digest", "revision", "credential_ref", "credential_version", "auth",
            "resource_kind", "managed_service_id", "managed_service_digest")} for role, profile in self.profiles.items()}


class ProviderError(RuntimeError):
    def __init__(self, message, *, kind=None, diagnostic=None):
        super().__init__(message)
        self.kind = kind
        self.diagnostic = diagnostic


class RequestCancelled(ProviderError):
    """Client delivery stopped; the provider has not confirmed remote stop."""
    def __init__(self):
        super().__init__("已停止交付；提供方是否停止计算尚未确认", kind="response_incomplete")


class RequestControl:
    """Per-request transport observation and best-effort connection cancellation."""
    def __init__(self, *, deadline_at=None, on_deadline=None):
        self.cancelled = threading.Event()
        self.received = False
        self.response = None
        self.lock = threading.Lock()
        self.deadline_at = deadline_at
        self.on_deadline = on_deadline

    def check(self):
        if self.deadline_at is not None and time.monotonic() >= self.deadline_at:
            if self.on_deadline is not None:
                self.on_deadline()
            raise RequestCancelled()
        if self.cancelled.is_set():
            raise RequestCancelled()

    def timeout(self):
        self.check()
        if self.deadline_at is None:
            return 600
        remaining = self.deadline_at - time.monotonic()
        if remaining <= 0:
            self.check()
            raise RequestCancelled()
        return remaining

    def progress(self, data):
        if data:
            self.received = True
        self.check()

    def attach(self, response):
        with self.lock:
            self.response = response
        self.check()

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            response = self.response
        if response is not None:
            # Closing a client connection is not an engine cancellation receipt.
            def close():
                try:
                    response.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=close, daemon=True, name="seampact-model-disconnect").start()


def _http_diagnostic(error):
    """Classify a bounded error body without retaining provider-supplied text."""
    status = error.code
    category = ("authentication" if status in (401, 403) else
                "rate_limit" if status == 429 else
                "request_too_large" if status == 413 else
                "server" if 500 <= status <= 599 else
                "invalid_request" if status == 400 else "unknown")
    fields = {}
    try:
        raw = error.read(16385)
        if len(raw) <= 16384:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                fields = payload.get("error", payload)
                if not isinstance(fields, dict):
                    fields = {}
    except (OSError, ValueError, TypeError, RecursionError, http.client.IncompleteRead):
        pass
    # Only inspect these three fields; neither the original text nor arbitrary
    # provider data is carried into the exception or its diagnostic.
    text = " ".join(value.lower() for name in ("code", "type", "message")
                    if isinstance((value := fields.get(name)), str))
    if status in (400, 422, 501) and re.search(r"\bstream(?:ing|_options)?\b", text) and re.search(
            r"not supported|unsupported|not implemented|must be false|only.{0,15}false", text):
        category = "stream_options_unsupported" if re.search(r"\bstream_options\b", text) else "stream_unsupported"
    elif status not in (401, 403, 413, 429) and not 500 <= status <= 599:
        if (re.search(r"context[_ -]?(length|window)|maximum context|context.{0,30}(too long|exceed|limit)", text)):
            category = "context_length"
        elif re.search(r"(request|payload|body|content)[_ -]?(too[_ -]?(large|big)|size[_ -]?limit)|"
                       r"(request|payload|body).{0,30}(too large|too big|size limit|size.{0,20}exceed)", text):
            category = "request_too_large"
        elif re.search(r"(max_tokens|max_completion_tokens|max_output_tokens|output[_ -]?(tokens|limit)).{0,80}"
                       r"(exceed|too (large|high|many)|maximum|limit|invalid|range|less than|greater than)|"
                       r"(output[_ -]?limit|invalid[_ -]?max[_ -]?tokens)", text):
            category = "output_limit"
    diagnostic = {"http_status": status, "category": category}
    # Recognized numeric fields only, and only plausible positive token counts.
    for name in ("max_context_tokens", "max_output_tokens", "requested_tokens"):
        value = fields.get(name)
        if type(value) is int and 0 < value <= 100000000:
            diagnostic[name] = value
    reasons = {
        "context_length": "输入超出上下文长度限制",
        "request_too_large": "请求内容超过大小限制",
        "output_limit": "输出 token 参数超出限制",
        "stream_unsupported": "服务明确不支持流式请求",
        "stream_options_unsupported": "服务明确不支持流式用量选项",
        "invalid_request": "请求参数或内容无效",
        "authentication": "认证或访问权限失败",
        "rate_limit": "请求频率或额度受限",
        "server": "供应商服务发生错误",
        "unknown": "供应商拒绝请求，原因未知",
    }
    return diagnostic, reasons[category]


class Transport:
    def send(self, url, key="", body=None, *, control=None):
        return self._send(url, key, body, control=control)

    def stream(self, url, key, body, on_delta, *, include_usage=True, control=None):
        """Read one completion incrementally; never retry a received completion."""
        request = dict(body, stream=True)
        if include_usage:
            request["stream_options"] = dict(include_usage=True)
        else:
            request.pop("stream_options", None)
        return self._send(url, key, request, on_delta, control=control)

    def _send(self, url, key, body, on_delta=None, control=None):
        headers = {"Accept": "text/event-stream, application/json" if on_delta else "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        try:
            if control is not None:
                control.check()
            timeout = control.timeout() if control is not None else 600
            with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
                if control is not None:
                    control.attach(response)
                if on_delta:
                    return _stream_completion(response, on_delta, control=control)
                result = json.load(response)
                if control is not None:
                    control.progress(True)
                return result
        except urllib.error.HTTPError as error:
            if control is not None:
                control.check()
            diagnostic, reason = _http_diagnostic(error)
        except (http.client.IncompleteRead, http.client.RemoteDisconnected) as error:
            if control is not None:
                control.check()
            raise ProviderError(
                f"provider response/{type(error).__name__}：模型响应接收中断，本次响应未完整取得",
                kind="response_incomplete") from None
        except urllib.error.URLError as error:
            if control is not None:
                control.check()
            reason = error.reason
            if isinstance(reason, socket.gaierror):
                category = "DNS"
            elif isinstance(reason, ssl.SSLCertVerificationError):
                category = "TLS-certificate"
            elif isinstance(reason, ssl.SSLError):
                category = "TLS"
            elif isinstance(reason, TimeoutError):
                category = "timeout"
            else:
                category = type(reason).__name__
            code = getattr(reason, "winerror", None)
            detail = f" (winerror={code})" if code is not None else ""
            if code is None and getattr(reason, "errno", None) is not None:
                detail = f" (errno={reason.errno})"
            raise ProviderError(f"provider transport/{category}{detail}", kind="transport") from None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            if control is not None:
                control.check()
            raise ProviderError(f"provider transport/{type(error).__name__}",
                                kind="transport" if isinstance(error, OSError) else "invalid_response") from None
        # Raise outside the handler so the original HTTPError and body are not
        # retained in the new exception's implicit context.
        raise ProviderError(f"provider HTTP {diagnostic['http_status']}：{reason}",
                            kind=diagnostic["category"], diagnostic=diagnostic)


def _stream_completion(response, on_delta, control=None):
    """Assemble the same message contract as send(), exposing only content deltas."""
    first = response.readline()
    if control is not None:
        control.progress(first)
    # Some compatible endpoints ignore stream=True and return a single JSON body.
    if first.lstrip().startswith(b"{"):
        result = json.loads(first + response.read())
        content = result["choices"][0]["message"].get("content")
        if isinstance(content, str) and content:
            on_delta(content)
        return result
    content, reasoning, calls, metadata, data = [], [], {}, {}, []
    observed_fields = {}
    finish, usage = None, None

    def incomplete():
        return ProviderError("provider response：模型流式响应未完整取得", kind="response_incomplete")

    def consume(raw):
        nonlocal finish, usage
        if raw == "[DONE]":
            return True
        chunk = json.loads(raw)
        if not isinstance(chunk, dict) or chunk.get("error"):
            raise incomplete()
        for name in ("id", "model", "created", "system_fingerprint"):
            if name in chunk:
                metadata[name] = chunk[name]
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                raise ValueError("multiple streamed choices")
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}
            # Measure compatible response fields without retaining an unknown
            # reasoning dialect or changing what becomes visible answer text.
            for name in ("content", "reasoning_content", "reasoning"):
                if name in delta:
                    value = delta[name]
                    field = observed_fields.setdefault(name, dict(chars=0, types=[]))
                    kind = type(value).__name__
                    if kind not in field["types"]:
                        field["types"].append(kind)
                    if isinstance(value, str):
                        field["chars"] += len(value)
            for name, parts in (("content", content), ("reasoning_content", reasoning)):
                value = delta.get(name)
                if value is not None:
                    if not isinstance(value, str):
                        raise ValueError("non-text completion delta")
                    parts.append(value)
                    if name == "content" and value:
                        on_delta(value)
            for item in delta.get("tool_calls") or []:
                index = item.get("index")
                if type(index) is not int or index < 0:
                    raise ValueError("missing tool call index")
                call = calls.setdefault(index, dict(id=[], name=[], arguments=[], type="function"))
                if item.get("type") is not None:
                    if item["type"] != "function":
                        raise ValueError("unsupported tool call type")
                function = item.get("function") or {}
                for value, parts in ((item.get("id"), call["id"]),
                                     (function.get("name"), call["name"]),
                                     (function.get("arguments"), call["arguments"])):
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("invalid tool call fragment")
                        parts.append(value)
        return False

    done = False
    for line in itertools.chain([first], response):
        if control is not None:
            control.progress(line)
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                done = consume("\n".join(data))
                data = []
                if done:
                    break
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data and not done:
        consume("\n".join(data))
    if finish is None:
        raise incomplete()
    message = dict(role="assistant", content="".join(content) if content else None)
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = [dict(id="".join(call["id"]), type=call["type"],
            function=dict(name="".join(call["name"]), arguments="".join(call["arguments"])))
            for _, call in sorted(calls.items())]
        ids = [call["id"] for call in message["tool_calls"]]
        if len(set(ids)) != len(ids) or any(not call["id"] or not call["function"]["name"]
                                          for call in message["tool_calls"]):
            raise incomplete()
    return dict(metadata, choices=[dict(index=0, finish_reason=finish, message=message)], usage=usage,
                _seampact_response_fields=dict(transport="sse", fields=observed_fields))


def model_request_scope(profiles, role):
    """Managed role requests use the same frozen task leases as Harness calls."""
    from contextlib import nullcontext
    if profiles.role_profile(role).get("resource_kind") != "managed":
        return nullcontext()
    if not hasattr(profiles, "request_scope"):
        raise ValueError("受管模型角色仅在完整联合实验任务中调用")
    return profiles.request_scope(role)


def request_parameters(profile, reserved):
    """Validate wire-body options without imposing a vendor parameter whitelist.

    The saved object also serves checked Harness mappings. Interpret it only at
    the actual platform endpoint, rather than changing those original mappings.
    """
    parameters = Profiles._parameters(profile.get("parameters", {}))
    if set(parameters) & set(reserved):
        raise ValueError("参数 JSON 不能覆盖平台管理的请求字段：" +
                         ", ".join(sorted(set(parameters) & set(reserved))))
    wrappers = set(parameters) & {"extra_body", "extra_headers", "headers", "base_url"}
    if wrappers:
        raise ValueError("参数 JSON 直接填写服务请求体字段，不使用 SDK／HTTP 包装字段：" +
                         ", ".join(sorted(wrappers)))
    return parameters


def completion_parameters(profile):
    """Return the exact nonsecret generation options sent to Chat Completions."""
    parameters = request_parameters(profile, {
        "model", "messages", "stream", "stream_options", "tools", "tool_choice",
        "functions", "function_call"})
    if "n" in parameters and (type(parameters["n"]) is not int or parameters["n"] != 1):
        raise ValueError("平台每次只处理一条模型回答，参数 n 只能为 1")
    limits = set(parameters) & {"max_tokens", "max_completion_tokens"}
    if len(limits) > 1:
        raise ValueError("max_tokens 与 max_completion_tokens 只能填写其中一个")
    if limits:
        field = next(iter(limits))
        value = parameters[field]
        if type(value) is not int or value < 1:
            raise ValueError(field + " 必须为正整数")
        declared = profile.get("declared_max_output_tokens") or profile.get("max_output_tokens")
        if declared is not None and value != declared:
            raise ValueError("参数 JSON 的最大输出与独立的最大输出 token 字段冲突，请保留一处或填写相同值")
    elif output_capability(profile):
        parameters["max_tokens"] = output_capability(profile)
    return parameters


def embedding_body(profile, text):
    parameters = request_parameters(profile, {"model", "input"})
    if "dimensions" in parameters and (type(parameters["dimensions"]) is not int or parameters["dimensions"] < 1):
        raise ValueError("embedding dimensions 必须为正整数")
    if "encoding_format" in parameters and (not isinstance(parameters["encoding_format"], str)
            or parameters["encoding_format"] not in {"float", "base64"}):
        raise ValueError("明确能力调用支持 float 或 base64 embedding 编码")
    return dict(parameters, model=profile["model"], input=text)


def completion_body(profile, messages, tools=None):
    # Preserve every message and explicit option; only unset fields use provider defaults.
    if profile.get("provider") == "deepseek":
        # AgentDojo's OpenAI wire codec uses developer/content-block messages.
        # Keep native task messages intact and adapt only their provider format.
        converted = []
        for original in messages:
            message = dict(original)
            if message.get("role") == "developer":
                message["role"] = "system"
            content = message.get("content")
            if isinstance(content, list) and all(isinstance(block, dict) and block.get("type") == "text" for block in content):
                message["content"] = "\n".join(block.get("text", "") for block in content)
            converted.append(message)
        messages = converted
    body = dict(completion_parameters(profile), model=profile["model"], messages=messages, stream=False)
    if tools:
        body.update(tools=tools, tool_choice="auto")
    return body
