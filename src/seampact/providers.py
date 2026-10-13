"""DeepSeek and GLM chat-compatible transport, without application usage caps."""

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


def protected(data, decrypt=False):
    if os.name != "nt":
        raise ValueError("this host uses environment credentials; Windows UI uses current-user DPAPI")
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


class Profiles:
    def __init__(self, directory, legacy=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / "providers.json"
        self.lock = threading.RLock()
        self.profiles = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {
            name: dict(provider=name, **preset, capabilities=None, credential=None)
            for name, preset in PRESETS.items()}
        # Migrate only an actual DeepSeek profile, and only a current-user encrypted key.
        # Old context, token, reasoning, call-count and spending limits are not copied.
        if not self.file.exists() and legacy and os.name == "nt":
            legacy = Path(legacy)
            if (legacy / "models.json").exists():
                old = json.loads((legacy / "models.json").read_text(encoding="utf-8"))
                matches = [v for v in old.get("profiles", {}).values()
                           if v.get("provider") == "deepseek" and v.get("enabled")
                           and urlsplit(v.get("base_url", "")).hostname == "api.deepseek.com"]
                if matches:
                    p = matches[0]
                    new = self.profiles["deepseek"]
                    new["model"] = p["model"]
                    new["base_url"] = p["base_url"].rstrip("/")
                    vault_file = legacy / "credentials.dpapi.json"
                    if p.get("api_key_ref") and vault_file.exists():
                        vault = json.loads(vault_file.read_text(encoding="utf-8"))
                        new["credential"] = vault.get(p["api_key_ref"])
                    elif p.get("api_key_env"):
                        new["key_env"] = p["api_key_env"]
        self._save()

    def _save(self):
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)

    def key(self, identity):
        with self.lock:
            p = self.profiles[identity]
            if p.get("credential"):
                return protected(base64.b64decode(p["credential"]), True).decode()
            return os.environ.get(p["key_env"], "")

    def public(self):
        with self.lock:
            result = []
            for identity, p in self.profiles.items():
                present = bool(p.get("credential") or os.environ.get(p["key_env"]))
                error = None
                try:
                    usable = bool(self.key(identity))
                except ValueError:
                    usable = False
                    error = "旧凭据在当前 Windows 会话中无法解密，请在此页面重新填写；Linux 使用环境变量。"
                result.append(dict(**{k: v for k, v in p.items() if k != "credential"},
                                   key_present=present, key_usable=usable, credential_error=error,
                                   usage_policy="observe_only", round_limit=None, context_truncation=False))
            return result

    def save(self, identity, body):
        if identity not in PRESETS:
            raise ValueError("supported providers: deepseek, glm")
        base = body.get("base_url", PRESETS[identity]["base_url"]).strip().rstrip("/")
        parsed = urlsplit(base)
        allowed = {"api.deepseek.com"} if identity == "deepseek" else {"open.bigmodel.cn", "api.z.ai"}
        if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.port not in (None, 443) or (
                parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("use an official HTTPS provider endpoint")
        model = body.get("model", "").strip()
        key = body.get("api_key", "").strip()
        if key and (any(c.isspace() for c in key) or len(key) > 8192):
            raise ValueError("invalid API credential")
        ceiling = body.get("max_output_tokens")
        if ceiling is not None and (type(ceiling) is not int or ceiling < 1):
            raise ValueError("provider output capability must be a positive integer or null")
        with self.lock:
            old = self.profiles[identity]
            if base != old["base_url"] and old.get("credential") and not key:
                raise ValueError("replace the key explicitly when changing its endpoint")
            replacement = dict(old, base_url=base, model=model, capabilities=None)
            replacement["declared_max_output_tokens"] = ceiling
            if key:
                replacement["credential"] = base64.b64encode(protected(key.encode())).decode()
            self.profiles[identity] = replacement
            self._save()

    def discover(self, identity, transport=None):
        p = self.profiles[identity]
        data = (transport or Transport()).send(p["base_url"] + "/models", self.key(identity))
        models = data.get("data", [])
        match = next((m for m in models if m.get("id") == p["model"]), None)
        with self.lock:
            p["capabilities"] = dict(checked_at=now(), models=models, selected=match)
            self._save()
        return p["capabilities"]

    def profile(self, identity):
        with self.lock:
            return dict(self.profiles[identity])


class ProviderError(RuntimeError):
    pass


class Transport:
    def send(self, url, key, body=None):
        headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, headers=headers,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None)
        # No automatic retry: a transport failure may have consumed provider usage.
        # Redirects must not forward an Authorization header to another endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, hdrs, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=600) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise ProviderError(f"provider HTTP {error.code}; checkpoint retained; no automatic retry") from None
        except (OSError, ValueError) as error:
            raise ProviderError(f"provider transport/{type(error).__name__}; outcome or usage may be unknown") from None


def completion_body(profile, messages, tools):
    # Keep all messages and provider reasoning/tool fields. Do not truncate context
    # or impose a spend, call-count, analysis-round or application token budget.
    body = dict(model=profile["model"], messages=messages, tools=tools, tool_choice="auto", stream=False)
    selected = (profile.get("capabilities") or {}).get("selected") or {}
    ceiling = selected.get("max_output_tokens") or profile.get("declared_max_output_tokens")
    if ceiling:
        body["max_tokens"] = ceiling
    return body
