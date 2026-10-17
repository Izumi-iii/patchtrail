"""Docker CLI adapter with pinned image IDs, read-only mounts and owned cleanup."""

import base64
import json
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath

from patchtrail.domain.task import VerificationError


class DockerBackend:
    def __init__(self):
        self.image_id = None

    def command(self, args, *, timeout=20, check=True):
        try:
            result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerificationError("ENVIRONMENT_ERROR", "docker_unavailable", str(exc)) from exc
        if check and result.returncode:
            raise VerificationError("ENVIRONMENT_ERROR", "docker_command_failed",
                                    result.stderr.decode("utf-8", errors="replace")[-2000:])
        return result

    def resolve_image(self, reference):
        result = self.command(["image", "inspect", reference])
        try:
            info = json.loads(result.stdout)[0]
            self.image_id = info["Id"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image_id):
                raise ValueError("Invalid image ID")
            if info.get("Os") != "linux":
                raise ValueError("Only Linux containers are supported")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_image", str(exc)) from exc
        return {"reference": reference, "image_id": self.image_id,
                "repo_digests": info.get("RepoDigests", []),
                "os": info["Os"], "architecture": info.get("Architecture")}

    def run(self, source, suite, task, run_id, phase, artifact_dir):
        if self.image_id is None:
            raise VerificationError("SYSTEM_ERROR", "unresolved_image", "Resolve the image first.")
        nonce = uuid.uuid4().hex
        control = artifact_dir / "control"
        control.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("pytest_runner.py"), control / "runner.py")
        (control / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
        (control / "config.json").write_text(json.dumps({
            "nonce": nonce, "phase": phase, "source_roots": task.source_roots,
            "import_modules": task.import_modules,
        }), encoding="utf-8")
        arguments = [
            "create", "--pull=never", "--name", f"patchtrail-{nonce}",
            "--label", f"io.patchtrail.run={run_id}", "--label", f"io.patchtrail.operation={nonce}",
            "--network=none", "--read-only", "--user=65534:65534", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--no-healthcheck", "--pids-limit=64",
            "--memory=256m", "--memory-swap=256m", "--cpus=1",
            "--ulimit", "fsize=1048576:1048576", "--log-driver=local",
            "--log-opt", "max-size=1m", "--log-opt", "max-file=2",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=134217728,mode=1777",
            "--workdir=/workspace", "--entrypoint=python",
            "--env=PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--env=PYTHONDONTWRITEBYTECODE=1",
            "--env=PYTEST_ADDOPTS=", "--env=PYTEST_PLUGINS=",
            "--env=HOME=/tmp",
        ]
        for directory, target in ((source, "/workspace"), (suite, "/checks"), (control, "/control")):
            resolved = str(Path(directory).resolve())
            if "," in resolved:
                raise VerificationError("CONFIG_ERROR", "unsupported_mount_path", resolved)
            arguments += ["--mount", f"type=bind,source={resolved},target={target},readonly"]
        arguments += [self.image_id, "-I", "-B", "/control/runner.py"]
        cid = self.command(arguments).stdout.decode().strip()
        if not re.fullmatch(r"[a-f0-9]{64}", cid):
            raise VerificationError("ENVIRONMENT_ERROR", "invalid_container_id", "Docker did not return a container ID.")
        process = None
        started = time.monotonic()
        reason = None
        try:
            inspected = json.loads(self.command(["inspect", cid]).stdout)[0]
            host = inspected["HostConfig"]
            mounts = {item["Destination"]: item for item in inspected.get("Mounts", [])}
            policy = {"user": inspected["Config"]["User"], "network_mode": host["NetworkMode"],
                      "readonly_rootfs": host["ReadonlyRootfs"], "privileged": host["Privileged"],
                      "cap_drop": host["CapDrop"], "security_opt": host["SecurityOpt"],
                      "memory_bytes": host["Memory"], "pids_limit": host["PidsLimit"],
                      "mounts": [{"target": target, "read_only": not mounts[target]["RW"]}
                                 for target in ("/workspace", "/checks", "/control")]}
            (artifact_dir / "runtime-policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")
            if (policy["user"] != "65534:65534" or policy["network_mode"] != "none"
                    or not policy["readonly_rootfs"] or policy["privileged"]
                    or "ALL" not in policy["cap_drop"]
                    or not any(value.startswith("no-new-privileges") for value in policy["security_opt"])
                    or not all(item["read_only"] for item in policy["mounts"])):
                raise VerificationError("ENVIRONMENT_ERROR", "unsafe_container_configuration",
                                        "Container runtime does not match the required isolation policy.")
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                process = subprocess.Popen(["docker", "start", "--attach", cid],
                                           stdout=stdout, stderr=stderr,
                                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                while process.poll() is None:
                    if time.monotonic() - started > task.timeout_seconds:
                        reason = "timeout"
                        break
                    if stdout.tell() + stderr.tell() > 2_097_152:
                        reason = "output_limit"
                        break
                    time.sleep(0.05)
                if reason is None and stdout.tell() + stderr.tell() > 2_097_152:
                    reason = "output_limit"
                if reason:
                    self.command(["kill", cid], check=False)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                stdout.seek(0)
                stderr.seek(0)
                raw_stdout = stdout.read(2_097_153)
                raw_stderr = stderr.read(2_097_153)
                (artifact_dir / "stdout.txt").write_bytes(raw_stdout[:1_048_576])
                (artifact_dir / "stderr.txt").write_bytes(raw_stderr[:1_048_576])
                container_state = json.loads(self.command([
                    "inspect", "--format", "{{json .State}}", cid]).stdout)
                (artifact_dir / "container.json").write_text(json.dumps(container_state), encoding="utf-8")
                if reason:
                    raise VerificationError("ENVIRONMENT_ERROR", reason, f"{phase}: {reason}")
                if container_state.get("OOMKilled"):
                    raise VerificationError("ENVIRONMENT_ERROR", "oom", f"{phase}: out of memory")
                if container_state.get("Running") or container_state.get("ExitCode") != 0 or process.returncode != 0:
                    raise VerificationError("ENVIRONMENT_ERROR", "runner_exit",
                                            f"{phase}: runner did not finish cleanly; {container_state.get('Error', '')}")
                marker = b"\nPATCHTRAIL_RESULT=" + nonce.encode() + b":"
                if marker not in raw_stdout:
                    raise VerificationError("ENVIRONMENT_ERROR", "report_missing", f"{phase}: report is missing.")
                try:
                    if raw_stdout.count(marker) != 1:
                        raise ValueError("Duplicate report frames")
                    frame = raw_stdout.split(marker, 1)[1].split(b"\n", 1)[0].rstrip(b"\r")
                    data = base64.b64decode(frame, validate=True)
                    if len(data) > 524288:
                        raise ValueError("Report exceeds 512 KiB")
                    report = json.loads(data)
                    if not isinstance(report, dict) or report.get("nonce") != nonce or report.get("phase") != phase:
                        raise ValueError("Report identity mismatch")
                    if not report.get("error"):
                        origins = report.get("import_origins")
                        if not isinstance(origins, dict) or set(origins) != set(task.import_modules):
                            raise ValueError("Import evidence missing")
                        for origin in origins.values():
                            if (not isinstance(origin, str) or ".." in PurePosixPath(origin).parts
                                    or not PurePosixPath(origin).is_relative_to("/workspace")):
                                raise ValueError("Source module origin is outside workspace")
                except (ValueError, AttributeError) as exc:
                    raise VerificationError("ENVIRONMENT_ERROR", "invalid_report", str(exc)) from exc
                report.update(container_id=cid, image_id=self.image_id,
                              stdout_truncated=len(raw_stdout) > 1_048_576,
                              stderr_truncated=len(raw_stderr) > 1_048_576,
                              elapsed_seconds=round(time.monotonic() - started, 3))
                (artifact_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                return report
        finally:
            try:
                removed = self.command(["rm", "--force", cid], check=False)
                if removed.returncode:
                    raise VerificationError("ENVIRONMENT_ERROR", "cleanup_failed", f"Could not remove owned container {cid}.")
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
