"""Browser-test server only. Never imported by the production launcher."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--worker", nargs=2)
    parser.add_argument("--attempt")
    args = parser.parse_args()
    if args.worker:
        import time
        from test_checkpoint import StatelessModel
        from test_verification import FakeBackend
        from web_worker import execute

        class SlowModel(StatelessModel):
            def invoke(self, messages, tools):
                time.sleep(0.35)
                return super().invoke(messages, tools)

        execute(args.data / "web.sqlite3", args.worker[0], args.attempt,
                model=SlowModel(args.data / "scripted-calls.jsonl"), backend=FakeBackend())
        return
    from patchtrail.web.jobs import Jobs
    from patchtrail.web.server import LocalServer
    from patchtrail.web.store import Store
    store = Store(args.data / "web.sqlite3")
    jobs = Jobs(store, require_model=False)
    def launch(identity, attempt, *, retry):
        with (args.data / "worker-test.log").open("ab") as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--data", str(args.data),
                                        "--worker", identity, "unused", "--attempt", attempt],
                                       cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        jobs.processes[identity] = process
    jobs.launcher = launch
    jobs.capabilities = lambda: {"configured": True, "model": "scripted-test", "max_model_calls": 8, "max_candidates": 2}
    server = LocalServer(("127.0.0.1", args.port), store, jobs)
    print(f"http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
