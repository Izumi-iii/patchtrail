"""Loopback development server. Not a production or remote hosting server."""

import argparse
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .service import RequestError, Workbench
from .store import Store

STATIC_ROOT = Path(__file__).with_name("static")
ROUTES = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/styles.css": ("styles.css", "text/css; charset=utf-8"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.workbench = Workbench(store)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "PatchTrail/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def respond(self, status, payload, content_type="application/json; charset=utf-8"):
        body = (json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if not isinstance(payload, bytes) else payload)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; img-src 'self'; connect-src 'self'; "
                         "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        port = self.server.server_port
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed_hosts:
            raise RequestError("只允许通过本机地址访问。", 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed_hosts}:
            raise RequestError("不允许跨站访问本地工作台。", 403)
        if self.command != "GET":
            if self.headers.get("X-PatchTrail") != "1":
                raise RequestError("缺少本地工作台请求标识。", 403)

    def _body(self):
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            raise RequestError("请求必须使用 JSON。", 415)
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("不支持此传输编码。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise RequestError("无效的请求长度。") from None
        if length > 65536:
            # Drain small rejected bodies before closing: Windows can reset a
            # socket with unread request data before the client receives 413.
            if length <= 1_048_576:
                self.rfile.read(length)
            raise RequestError("请求大小必须为 1～65536 字节。", 413)
        if length <= 0:
            raise RequestError("请求大小必须为 1～65536 字节。", 413)
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise RequestError("JSON 内容无效。") from None
        if not isinstance(data, dict):
            raise RequestError("JSON 必须是对象。")
        return data

    def _dispatch(self):
        self._guard()
        path = urlsplit(self.path).path
        service = self.server.workbench
        store = service.store
        if self.command == "GET" and path in ROUTES:
            name, kind = ROUTES[path]
            return self.respond(200, (STATIC_ROOT / name).read_bytes(), kind)
        if self.command == "GET" and path == "/api/healthz":
            return self.respond(200, {"status": "ok", "mode": "workbench_preview",
                                      "agent_connected": False, "tests_available": False,
                                      "repository_inspection": True})
        if path == "/api/conversations":
            if self.command == "GET":
                return self.respond(200, {"conversations": store.list()})
            if self.command == "POST":
                self._body()
                return self.respond(201, store.create())
        match = re.fullmatch(r"/api/conversations/([a-f0-9]{32})(/messages)?", path)
        if match:
            conversation_id, messages = match.groups()
            if messages and self.command == "POST":
                return self.respond(200, service.send(conversation_id, self._body()))
            if not messages:
                if self.command == "GET":
                    return self.respond(200, store.get(conversation_id))
                if self.command == "PATCH":
                    return self.respond(200, service.update(conversation_id, self._body()))
                if self.command == "DELETE":
                    service.delete(conversation_id)
                    return self.respond(200, {"deleted": True})
        raise RequestError("接口或资源不存在。", 404)

    def handle_request(self):
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except KeyError:
            self.respond(404, {"error": "会话不存在，可能已在其他页面删除。"})
        except RequestError as exc:
            self.respond(exc.status, {"error": str(exc)})
        except Exception:
            logging.exception("Workbench request failed")
            self.respond(500, {"error": "服务暂时出错，请保留输入并重试。"})

    do_GET = handle_request
    do_POST = handle_request
    do_PATCH = handle_request
    do_DELETE = handle_request


def main():
    parser = argparse.ArgumentParser(description="PatchTrail local web workbench")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, default=Path(".patchtrail"))
    args = parser.parse_args()
    store = Store(args.data_dir / "workbench.sqlite3")
    try:
        server = LocalServer(("127.0.0.1", args.port), store)
    except OSError as exc:
        parser.exit(1, f"Cannot start server: {exc}\nUse --port to select another port.\n")
    print(f"PatchTrail: http://127.0.0.1:{server.server_port}", flush=True)
    print("Local workbench preview. Model repair and test execution are not connected.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
