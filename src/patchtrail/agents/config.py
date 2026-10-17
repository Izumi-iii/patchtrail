import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from patchtrail.domain.task import VerificationError


def read_model_env(path):
    """Small, non-executing parser for an explicitly selected control-side file."""
    if path is None or not Path(path).exists():
        return {}
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise VerificationError("CONFIG_ERROR", "invalid_model_env", "Model env file must be at most 16 KiB.")
    values = {}
    allowed = {"PATCHTRAIL_MODEL", "PATCHTRAIL_BASE_URL", "PATCHTRAIL_API_KEY", "DEEPSEEK_API_KEY"}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        raise VerificationError("CONFIG_ERROR", "invalid_model_env", "Cannot read model env file as UTF-8.") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if separator != "=" or name not in allowed or name in values:
            raise VerificationError("CONFIG_ERROR", "invalid_model_env", f"Invalid or duplicate field at line {number}.")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise VerificationError("CONFIG_ERROR", "invalid_model_env", f"Unclosed quote at line {number}.")
            value = value[1:-1]
        values[name] = value
    return values


@dataclass(frozen=True)
class ModelSettings:
    model: str = "deepseek-flash"
    base_url: str = "https://api.deepseek.com"
    api_key: str = field(default="", repr=False)
    timeout_seconds: int = 60
    max_output_tokens: int = 4096

    @classmethod
    def from_env(cls, env_file=None):
        local = read_model_env(env_file)

        def setting(name, default=""):
            return os.environ.get(name, "").strip() or local.get(name, default).strip()

        settings = cls(model=setting("PATCHTRAIL_MODEL", "deepseek-flash"),
                       base_url=setting("PATCHTRAIL_BASE_URL", "https://api.deepseek.com").rstrip("/"),
                       api_key=setting("PATCHTRAIL_API_KEY") or setting("DEEPSEEK_API_KEY"))
        settings.validate()
        return settings

    def validate(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise VerificationError("CONFIG_ERROR", "missing_model_key",
                                    "Set PATCHTRAIL_API_KEY or DEEPSEEK_API_KEY locally; never send keys in chat.")
        if any(ord(char) < 32 for char in self.api_key):
            raise VerificationError("CONFIG_ERROR", "invalid_model_key", "Model key contains invalid characters.")
        if not self.model or len(self.model) > 120 or any(char.isspace() for char in self.model):
            raise VerificationError("CONFIG_ERROR", "invalid_model", "Provide a valid model ID.")
        if (type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 120
                or type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 16384):
            raise VerificationError("CONFIG_ERROR", "invalid_model_limits", "Invalid model timeout/output limits.")
        try:
            parsed = urlsplit(self.base_url)
            parsed.port
        except (ValueError, TypeError):
            raise VerificationError("CONFIG_ERROR", "invalid_model_url", "Invalid endpoint URL.") from None
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (any(ord(char) < 32 for char in self.base_url)
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not (parsed.scheme == "https" or parsed.scheme == "http" and local)):
            raise VerificationError("CONFIG_ERROR", "invalid_model_url", "Use HTTPS, or loopback HTTP for tests.")

    def public(self):
        return {"provider": "openai-compatible", "model": self.model, "base_url": self.base_url,
                "thinking": "disabled", "max_output_tokens": self.max_output_tokens,
                "timeout_seconds": self.timeout_seconds, "transport_retries": 0}


@dataclass(frozen=True)
class AgentBudget:
    max_model_calls: int = 12
    max_tool_calls: int = 40
    max_candidates: int = 3
    max_active_seconds: int = 600
    max_context_characters: int = 60000

    def validate(self):
        for name, low, high in (("max_model_calls", 1, 100), ("max_tool_calls", 1, 300),
                                ("max_candidates", 1, 10), ("max_active_seconds", 10, 7200),
                                ("max_context_characters", 2000, 200000)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise VerificationError("CONFIG_ERROR", "invalid_agent_budget", name)
