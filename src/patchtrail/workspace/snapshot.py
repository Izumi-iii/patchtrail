import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath

from patchtrail.domain.task import VerificationError, safe_relative


def digest(data):
    return hashlib.sha256(data).hexdigest()


def register_path(name, seen):
    """Detect colliding directory prefixes as well as colliding filenames."""
    parts = PurePosixPath(name).parts
    for index in range(1, len(parts) + 1):
        prefix = "/".join(parts[:index])
        folded = prefix.casefold()
        if folded in seen and seen[folded] != prefix:
            raise VerificationError("POLICY_REJECTED", "case_collision", name)
        seen[folded] = prefix


def git(directory, *args, check=True, isolated=False):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    options = []
    if isolated:
        # Isolated replay must preserve bytes, regardless of the host's Windows Git settings.
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        options = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                   "-c", "core.attributesFile=" + os.devnull]
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", "core.hooksPath=", *options, "-C", str(directory), *args],
            capture_output=True, timeout=30, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError("ENVIRONMENT_ERROR", "git_unavailable", str(exc)) from exc
    if check and result.returncode:
        raise VerificationError("CONFIG_ERROR", "git_failed",
                                result.stderr.decode("utf-8", errors="replace")[-2000:])
    return result


def manifest(directory):
    result = {}
    folded = {}
    total = 0
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory).as_posix()
        if relative == ".git" or relative.startswith(".git/"):
            continue
        safe_relative(relative)
        if path.is_symlink():
            raise VerificationError("POLICY_REJECTED", "symlink", relative)
        if not path.is_file():
            continue
        register_path(relative, folded)
        size = path.stat().st_size
        total += size
        if size > 1_048_576 or total > 20_971_520 or len(result) >= 2000:
            raise VerificationError("POLICY_REJECTED", "snapshot_limit", "Snapshot exceeds MVP limits.")
        result[relative] = {"sha256": digest(path.read_bytes()), "size": size}
    return result


def tree_hash(files):
    return digest(json.dumps(files, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode("utf-8"))


def export_commit(task, destination):
    source = Path(task.repository_path)
    root = git(source, "rev-parse", "--show-toplevel").stdout.decode().strip()
    if Path(root).resolve() != source:
        raise VerificationError("CONFIG_ERROR", "not_repository_root", "Select the repository root.")
    if git(source, "status", "--porcelain", "--untracked-files=normal").stdout.strip():
        raise VerificationError("CONFIG_ERROR", "dirty_repository", "Input worktree must be clean.")
    actual = git(source, "rev-parse", "--verify", task.base_commit + "^{commit}").stdout.decode().strip()
    if actual != task.base_commit:
        raise VerificationError("CONFIG_ERROR", "invalid_revision", "Commit did not resolve exactly.")
    entries = git(source, "ls-tree", "-r", "-l", "-z", "--full-tree", task.base_commit).stdout.split(b"\0")
    if len(entries) > 2001:
        raise VerificationError("POLICY_REJECTED", "snapshot_limit", "Too many files.")
    destination.mkdir()
    total, folded = 0, {}
    for entry in filter(None, entries):
        header, raw_name = entry.split(b"\t", 1)
        mode, kind, blob, raw_size = header.split()
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise VerificationError("POLICY_REJECTED", "unsupported_git_entry", repr(raw_name))
        try:
            name = safe_relative(raw_name.decode("utf-8"))
        except (UnicodeDecodeError, VerificationError) as exc:
            raise VerificationError("POLICY_REJECTED", "unsafe_git_path", repr(raw_name)) from exc
        register_path(name, folded)
        size = int(raw_size)
        total += size
        if size > 1_048_576 or total > 20_971_520:
            raise VerificationError("POLICY_REJECTED", "snapshot_limit", name)
        content = git(source, "cat-file", "blob", blob.decode("ascii")).stdout
        if content.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise VerificationError("POLICY_REJECTED", "lfs_unsupported", name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return manifest(destination)


def allowed_source(name, editable_paths):
    path = PurePosixPath(name)
    filename = path.name.casefold()
    if path.suffix != ".py" or filename in ("conftest.py", "setup.py", "sitecustomize.py", "usercustomize.py"):
        return False
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return False
    if any(part.casefold() in ("tests", "test", ".github", ".git", ".patchtrail") for part in path.parts):
        return False
    return any(name == prefix or name.startswith(prefix + "/") for prefix in editable_paths)


def validate_patch(data, task):
    if not data or len(data) > 262144:
        raise VerificationError("POLICY_REJECTED", "patch_size", "Patch must be 1 to 256 KiB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError("POLICY_REJECTED", "binary_patch", "Patch must be UTF-8 text.") from exc
    if "\x00" in text:
        raise VerificationError("POLICY_REJECTED", "binary_patch", "NUL is not supported.")
    names, current, saw_hunk = set(), None, False
    additions, deletions = 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/([^\s]+) b/([^\s]+)", line)
            if not match or match[1] != match[2]:
                raise VerificationError("POLICY_REJECTED", "unsupported_patch_path", line[:200])
            try:
                current = safe_relative(match[1])
            except VerificationError as exc:
                raise VerificationError("POLICY_REJECTED", "unsafe_patch_path", str(exc)) from exc
            if not allowed_source(current, task.editable_paths):
                raise VerificationError("POLICY_REJECTED", "protected_path", current)
            names.add(current)
        elif line.startswith(("--- ", "+++ ")):
            path = line[4:]
            if path != "/dev/null":
                raw_name = path[2:] if path.startswith(("a/", "b/")) else path
                try:
                    header_name = safe_relative(raw_name)
                except VerificationError as exc:
                    raise VerificationError("POLICY_REJECTED", "unsafe_patch_path", "Unsafe unified diff header.") from exc
                if not allowed_source(header_name, task.editable_paths):
                    raise VerificationError("POLICY_REJECTED", "protected_path", header_name)
            if current is None:
                raise VerificationError("REPAIR_FAILED", "missing_git_header",
                                        "Include 'diff --git a/path b/path' before each ---/+++ file section. "
                                        "Keep the complete cumulative patch against the original base.")
            if path not in ("/dev/null", "a/" + current, "b/" + current):
                raise VerificationError("REPAIR_FAILED", "inconsistent_patch_header", "Safe file headers disagree with diff --git paths.")
        elif line.startswith(("old mode ", "new mode ", "rename ", "copy ", "GIT binary patch", "Binary files")):
            raise VerificationError("POLICY_REJECTED", "unsupported_patch_operation", line[:200])
        elif line.startswith(("new file mode ", "deleted file mode ")):
            if not line.endswith("100644"):
                raise VerificationError("POLICY_REJECTED", "unsupported_file_mode", line)
        elif line.startswith("@@ "):
            saw_hunk = True
        elif line.startswith("+"):
            additions += 1
        elif line.startswith("-"):
            deletions += 1
    if not names or not saw_hunk:
        raise VerificationError("REPAIR_FAILED", "invalid_patch", "No source hunks found.")
    if len(names) > task.max_changed_files or additions + deletions > task.max_changed_lines:
        raise VerificationError("POLICY_REJECTED", "change_limit", "Patch exceeds task limits.")
    return {"paths": sorted(names), "additions": additions, "deletions": deletions}


def apply_candidate(base, candidate, patch_path, task):
    shutil.copytree(base, candidate)
    before = manifest(candidate)
    patch_bytes = patch_path.read_bytes()
    policy = validate_patch(patch_bytes, task)
    for name in policy["paths"]:
        source = base / name
        if source.is_file() and b"\r\n" in source.read_bytes():
            raise VerificationError("CONFIG_ERROR", "unsupported_source_newlines",
                                    "Editable committed source must use LF newlines in this release.")
    git(candidate, "init", "--quiet", "--template=", isolated=True)
    normalized = candidate / ".git" / "patchtrail-input.patch"
    normalized.write_bytes(patch_bytes.replace(b"\r\n", b"\n"))
    checked = git(candidate, "apply", "--check", "--whitespace=error", str(normalized), check=False, isolated=True)
    if checked.returncode:
        raise VerificationError("REPAIR_FAILED", "patch_does_not_apply",
                                checked.stderr.decode("utf-8", errors="replace")[-2000:])
    applied = git(candidate, "apply", "--whitespace=error", str(normalized), check=False, isolated=True)
    if applied.returncode:
        raise VerificationError("REPAIR_FAILED", "patch_apply_failed", "Git could not apply candidate.")
    after = manifest(candidate)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if not changed or changed != set(policy["paths"]):
        raise VerificationError("POLICY_REJECTED", "patch_manifest_mismatch", "Actual changes differ from headers.")
    if any(not allowed_source(name, task.editable_paths) for name in changed):
        raise VerificationError("POLICY_REJECTED", "protected_path", "Unexpected changed path.")
    return after, policy
