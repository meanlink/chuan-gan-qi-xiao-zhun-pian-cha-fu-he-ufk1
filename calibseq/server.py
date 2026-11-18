"""HTTP API 与页面宿主（仅标准库）。

运行：python3 -m calibseq.server --host 127.0.0.1 --port 8000 --db calibseq.db
启动时自动恢复所有被中断（SEALED）的判定运行。
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core, store

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY = 2_000_000


def make_handler(conn, lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "calibseq/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

        # ---------------------------------------------------------- 响应助手
        def _send(self, body: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise core.Validation("请求体过大")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                raise core.Validation("请求体不是合法 JSON")

        # ---------------------------------------------------------- 路由
        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _dispatch(self, method: str):
            try:
                path = self.path.split("?", 1)[0]
                if method == "GET" and path == "/":
                    return self._send((STATIC_DIR / "index.html").read_bytes(),
                                      "text/html; charset=utf-8")
                with lock:
                    if method == "GET" and path == "/api/health":
                        return self._json({"ok": True})
                    if method == "GET" and path == "/api/cases":
                        return self._json({"cases": core.list_cases(conn)})
                    if method == "POST" and path == "/api/cases":
                        body = self._read_json()
                        case = core.create_case(
                            conn,
                            name=body.get("name"),
                            overall_risk=body.get("overall_risk"),
                            zones=body.get("zones"),
                            rounds=body.get("rounds"),
                        )
                        return self._json(case, status=201)
                    m = re.fullmatch(r"/api/cases/(\d+)", path)
                    if method == "GET" and m:
                        return self._json(core.get_case_detail(conn, int(m.group(1))))
                    m = re.fullmatch(r"/api/cases/(\d+)/measurements", path)
                    if method == "POST" and m:
                        body = self._read_json()
                        return self._json(
                            core.report_measurements(conn, int(m.group(1)), body.get("records")))
                    m = re.fullmatch(r"/api/cases/(\d+)/judge", path)
                    if method == "POST" and m:
                        return self._json(core.judge(conn, int(m.group(1))))
                    m = re.fullmatch(r"/api/cases/(\d+)/resume", path)
                    if method == "POST" and m:
                        return self._json(core.resume(conn, int(m.group(1))))
                return self._json({"error": {"code": "NOT_FOUND", "message": "接口不存在"}}, 404)
            except core.DomainError as e:
                return self._json({"error": {"code": e.code, "message": str(e)}}, e.http_status)
            except Exception:
                traceback.print_exc()
                return self._json({"error": {"code": "INTERNAL", "message": "服务内部错误"}}, 500)

    return Handler


def make_server(conn, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    """在已有连接上构建 HTTP 服务（测试可用内存库 + 随机端口）。"""
    lock = threading.RLock()
    return ThreadingHTTPServer((host, port), make_handler(conn, lock))


def serve(host: str = "127.0.0.1", port: int = 8000, db_path: str = "calibseq.db"):
    conn = store.connect(db_path)
    lock = threading.RLock()
    with lock:
        recovered = core.resume(conn)  # 启动恢复：完成上次中断的封存判定
    if recovered["resumed"]:
        print(f"启动恢复：完成 {len(recovered['resumed'])} 个被中断的判定运行 {recovered['resumed']}")
    httpd = make_server(conn, host, port)
    actual = httpd.server_address[1]
    print(f"校准序贯判定服务已启动: http://{host}:{actual}  (数据库: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="calibseq.server", description="校准分组序贯判定服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="calibseq.db")
    args = ap.parse_args(argv)
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
