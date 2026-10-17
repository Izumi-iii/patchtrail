"""Strict JSON task contract for the first independently executable verifier."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


class VerificationError(Exception):
    def __init__(self, status, reason, message):
        super().__init__(message)
        self.status = status
        self.reason = reason


def invalid(message):
    raise VerificationError("CONFIG_ERROR", "invalid_task", message)


def safe_relative(value):
    if not isinstance(value, str) or not value or len(value) > 500:
        invalid("Expected a non-empty repository-relative path.")
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        invalid(f"Unsafe relative path: {value!r}")
    if any(char in value for char in '\\:*?"<>|') or any(ord(c) < 32 for c in value):
        invalid(f"Unsupported path characters: {value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}
    for part in path.parts:
        if part.casefold() in (".", "..", ".git") or part.endswith((".", " ")):
            invalid(f"Unsafe path component: {value!r}")
        if part.split(".")[0].upper() in reserved:
            invalid(f"Reserved filename: {value!r}")
    return value


def absolute_path(value, name, *, directory=True):
    if not isinstance(value, str) or not Path(value).is_absolute():
        invalid(f"{name} must be an absolute local path.")
    path = Path(value)
    if path.is_symlink():
        invalid(f"{name} must not be a symlink.")
    try:
        path = path.resolve(strict=True)
    except (OSError, ValueError):
        invalid(f"{name} does not exist.")
    if directory and not path.is_dir():
        invalid(f"{name} must be a directory.")
    return str(path)


def integer(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        invalid(f"{key} must be an integer between {low} and {high}.")
    return value


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    request: str
    repository_path: str
    base_commit: str
    image: str
    regression_dir: str
    feedback_dir: str
    editable_paths: tuple[str, ...]
    import_modules: tuple[str, ...]
    holdout_dir: str | None = None
    source_roots: tuple[str, ...] = (".",)
    timeout_seconds: int = 60
    max_changed_files: int = 5
    max_changed_lines: int = 250
    schema_version: int = 1

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            invalid("Task must be a JSON object.")
        allowed = set(cls.__dataclass_fields__)
        if set(data) - allowed:
            invalid("Unknown task fields: " + ", ".join(sorted(set(data) - allowed)))
        if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
            invalid("Unsupported task schema_version.")
        task_id = data.get("task_id", "")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", task_id):
            invalid("Invalid task_id.")
        request = data.get("request")
        if not isinstance(request, str) or not request.strip() or len(request) > 12000 or "\x00" in request:
            invalid("request must describe the expected behavior in 1 to 12000 characters.")
        commit = data.get("base_commit", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", commit):
            invalid("base_commit must be a full commit hash, not a branch or short SHA.")
        image = data.get("image", "")
        if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]{0,255}", image):
            invalid("Invalid image reference.")

        def paths(name, default=None):
            values = data.get(name, default)
            if not isinstance(values, list) or not values or len(values) > 30:
                invalid(f"{name} must contain 1 to 30 paths.")
            return tuple(safe_relative(value) for value in values)

        modules = data.get("import_modules")
        if not isinstance(modules, list) or not modules or len(modules) > 30:
            invalid("import_modules must list the source modules to verify inside the container.")
        if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*", item)
               for item in modules):
            invalid("Invalid Python module name.")
        roots = data.get("source_roots", ["."])
        if not isinstance(roots, list) or not roots or len(roots) > 10:
            invalid("source_roots must contain 1 to 10 paths.")
        roots = tuple("." if value == "." else safe_relative(value) for value in roots)
        holdout = data.get("holdout_dir")
        return cls(task_id=task_id, request=request.strip(),
                   repository_path=absolute_path(data.get("repository_path"), "repository_path"),
                   base_commit=commit.lower(), image=image,
                   regression_dir=absolute_path(data.get("regression_dir"), "regression_dir"),
                   feedback_dir=absolute_path(data.get("feedback_dir"), "feedback_dir"),
                   holdout_dir=absolute_path(holdout, "holdout_dir") if holdout is not None else None,
                   editable_paths=paths("editable_paths"), import_modules=tuple(modules),
                   source_roots=roots,
                   timeout_seconds=integer(data, "timeout_seconds", 60, 1, 600),
                   max_changed_files=integer(data, "max_changed_files", 5, 1, 30),
                   max_changed_lines=integer(data, "max_changed_lines", 250, 1, 2000))

    @classmethod
    def load(cls, path):
        try:
            path = Path(path)
            if path.stat().st_size > 65536:
                invalid("Task file exceeds 64 KiB.")
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            invalid(f"Cannot read task JSON: {exc}")

    def to_dict(self):
        return asdict(self)
