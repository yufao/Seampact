"""Independent chat and analysis model roles with OpenAI-compatible transport."""

import base64
import ctypes
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .store import now

PRESETS = {
    "deepseek": dict(base_url="https://api.deepseek.com", model="deepseek-flash",
                     key_env="DEEPSEEK_API_KEY", docs="https://api-docs.deepseek.com/api/list-models/"),
    "glm": dict(base_url="https://open.bigmodel.cn/api/paas/v4", model="",
                key_env="GLM_API_KEY", docs="https://docs.bigmodel.cn/")}
ROLES = ("chat", "analysis")


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
    return selected.get("max_output_tokens") or profile.get("declared_max_output_tokens") or profile.get("max_output_tokens")


class Profiles:
    def __init__(self, directory, legacy=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / "providers.json"
        self.credentials_file = self.directory / "credentials.json"
        self.lock = threading.RLock()
        self.credentials = json.loads(self.credentials_file.read_text(encoding="utf-8")) if self.credentials_file.exists() else {}
        self.profiles = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
        if not self.file.exists() and legacy:
            self._migrate(Path(legacy))
        for name, preset in PRESETS.items():
            if name not in self.profiles:
                self.profiles[name] = dict(provider=name, **preset, capabilities=None, credential=None)
            else:
                self.profiles[name].setdefault("provider", name)
                self.profiles[name].setdefault("key_env", preset["key_env"])
        # Move any older inline key into the private credential representation.
        for identity, profile in self.profiles.items():
            inline = profile.pop("api_key", None)
            if inline:
                self._set_key(identity, profile, inline)
        original = self.profiles["deepseek"]
        configured = bool(original.get("base_url") and original.get("model") and self._key_present(original))
        for role in ROLES:
            if role not in self.profiles:
                self.profiles[role] = dict(original) if configured else dict(
                    provider="openai-compatible", base_url="", model="",
                    key_env="SEAMPACT_" + role.upper() + "_API_KEY", credential=None, capabilities=None)
            self.profiles[role]["role"] = role
        self._save()

    def _migrate(self, legacy):
        candidates = [legacy] if legacy.is_file() else [legacy / "providers.json", legacy / "config" / "providers.json"]
        source = next((path for path in candidates if path.name == "providers.json" and path.is_file()), None)
        if source:
            value = json.loads(source.read_text(encoding="utf-8"))
            self.profiles = value.get("profiles", value)
            private = source.parent / "credentials.json"
            if private.is_file():
                self.credentials.update(json.loads(private.read_text(encoding="utf-8")))
            return
        directory = legacy.parent if legacy.is_file() else legacy
        models_file = directory / "models.json"
        if not models_file.is_file():
            return
        old = json.loads(models_file.read_text(encoding="utf-8"))
        matches = [value for value in old.get("profiles", {}).values()
                   if value.get("provider") == "deepseek" and value.get("enabled")
                   and urlsplit(value.get("base_url", "")).hostname == "api.deepseek.com"]
        if not matches:
            return
        previous = matches[0]
        profile = dict(provider="deepseek", **PRESETS["deepseek"], capabilities=None, credential=None)
        profile.update(base_url=previous["base_url"].rstrip("/"), model=previous["model"])
        if previous.get("api_key_env"):
            profile["key_env"] = previous["api_key_env"]
        vault_file = directory / "credentials.dpapi.json"
        if os.name == "nt" and previous.get("api_key_ref") and vault_file.is_file():
            vault = json.loads(vault_file.read_text(encoding="utf-8"))
            profile["credential"] = vault.get(previous["api_key_ref"])
        self.profiles["deepseek"] = profile

    def _save(self):
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)
        if self.credentials:
            descriptor = os.open(self.credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.credentials, stream, ensure_ascii=False, indent=2)
            if os.name != "nt":
                os.chmod(self.credentials_file, 0o600)

    def _set_key(self, identity, profile, value):
        if os.name == "nt":
            profile["credential"] = base64.b64encode(protected(value.encode())).decode()
            profile.pop("credential_ref", None)
        else:
            self.credentials[identity] = value
            profile["credential_ref"] = identity
            profile["credential"] = None

    def _key_present(self, profile):
        return bool(profile.get("credential") or self.credentials.get(profile.get("credential_ref"))
                    or os.environ.get(profile.get("key_env", "")))

    def key(self, identity):
        with self.lock:
            profile = self.profiles[identity]
            if os.name == "nt" and profile.get("credential"):
                return protected(base64.b64decode(profile["credential"]), True).decode()
            private = self.credentials.get(profile.get("credential_ref"))
            if private:
                return private
            environment = os.environ.get(profile.get("key_env", ""), "")
            if environment:
                return environment
            if profile.get("credential"):
                raise ValueError("Windows credential unavailable; configure this role's key or environment variable")
            return ""

    def _public(self, identity):
        profile = self.profiles[identity]
        error = None
        try:
            usable = bool(self.key(identity))
        except (ValueError, OSError):
            usable = False
            error = "已有密钥在当前环境无法读取，请重新填写或配置对应环境变量。"
        visible = {name: value for name, value in profile.items()
                   if name not in {"credential", "credential_ref", "api_key"}}
        configured = bool(profile.get("base_url") and profile.get("model"))
        ready = configured and not error and (identity == "chat" or usable)
        return dict(visible, identity=identity, max_output_tokens=output_capability(profile),
                    configured=configured, ready=ready, key_present=self._key_present(profile),
                    key_usable=usable, credential_error=error,
                    credential_mode="windows_dpapi" if os.name == "nt" else "private_config_environment",
                    usage_policy="observe_only", round_limit=None, context_truncation=False)

    def public(self):
        with self.lock:
            return [self._public(identity) for identity in self.profiles]

    def role_identity(self, role):
        if role not in ROLES:
            raise ValueError("model role must be chat or analysis")
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
            self._update(identity, body, cloud=role == "analysis")
            return self._public(identity)

    def save(self, identity, body):
        if identity in ROLES:
            return self.save_role(identity, body)
        if identity not in PRESETS:
            raise ValueError("supported providers: deepseek, glm")
        with self.lock:
            self._update(identity, body, cloud=True)

    def _update(self, identity, body, cloud):
        previous = self.profiles[identity]
        base = body.get("base_url", previous.get("base_url", "")).strip().rstrip("/")
        parsed = urlsplit(base)
        allowed_schemes = {"https"} if cloud else {"http", "https"}
        if parsed.scheme not in allowed_schemes or not parsed.hostname or (
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("analysis requires a cloud HTTPS base URL" if cloud else "use an HTTP or HTTPS model base URL")
        # Accessing port also rejects malformed port syntax.
        parsed.port
        model = body.get("model", previous.get("model", "")).strip()
        if not model:
            raise ValueError("provide a model ID")
        key = body.get("api_key", "").strip()
        if key and (any(character.isspace() for character in key) or len(key) > 8192):
            raise ValueError("invalid API credential")
        ceiling = body.get("max_output_tokens", previous.get("declared_max_output_tokens"))
        if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
            raise ValueError("provider output capability must be a positive integer or null")
        provider = provider_name(base)
        replacement = dict(previous, base_url=base, model=model, provider=provider, capabilities=None,
                           declared_max_output_tokens=ceiling)
        if "max_output_tokens" in body:
            replacement.pop("max_output_tokens", None)
        if base != previous.get("base_url"):
            replacement["credential"] = None
            replacement.pop("credential_ref", None)
            replacement["key_env"] = "" if previous.get("base_url") else (
                PRESETS[provider]["key_env"] if provider in PRESETS else
                "SEAMPACT_" + identity.upper() + "_API_KEY")
        if key:
            self._set_key(identity, replacement, key)
        self.profiles[identity] = replacement
        self._save()

    def discover(self, identity, transport=None):
        profile = self.profile(identity)
        data = (transport or Transport()).send(profile["base_url"] + "/models", self.key(identity))
        models = data.get("data", [])
        match = next((model for model in models if model.get("id") == profile["model"]), None)
        capabilities = dict(checked_at=now(), models=models, selected=match)
        with self.lock:
            self.profiles[identity]["capabilities"] = capabilities
            self._save()
        return capabilities

    def profile(self, identity):
        with self.lock:
            return dict(self.profiles[identity])


class ProviderError(RuntimeError):
    pass


class Transport:
    def send(self, url, key="", body=None):
        headers = {"Accept": "application/json"}
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
            with urllib.request.build_opener(NoRedirect).open(request, timeout=600) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ProviderError(f"provider HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise ProviderError(f"provider transport/{type(error).__name__}") from None


def completion_body(profile, messages, tools=None):
    # Preserve every message; provider defaults apply unless its output capability is known.
    body = dict(model=profile["model"], messages=messages, stream=False)
    if tools:
        body.update(tools=tools, tool_choice="auto")
    ceiling = output_capability(profile)
    if ceiling:
        body["max_tokens"] = ceiling
    return body
