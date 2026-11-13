"""HTTP 服务（仅依赖标准库 http.server）。

路由见模块底部 ROUTES；所有 /api 响应统一为 JSON：
    成功 {"ok": true, "data": ...}
    失败 {"ok": false, "error": {...}}，并带合适的 HTTP 状态码
错误文本统一经过 errors.sanitize_message，不包含服务端私有路径。
"""
from __future__ import annotations

import csv
import io
import json
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, history, samples
from .errors import SQLError, ValidationError, sanitize_message
from .importer import drop_table, import_csv, list_tables
from .queryrunner import service as query_service
from .spaces import SpaceRegistry

STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
_MULTIPART_RE = re.compile(rb'boundary=(?:"([^"]+)"|([^;]+))')


# ---------------------------------------------------------------------------
# multipart/form-data 最小解析器（仅本项目的单文件上传场景需要）
# ---------------------------------------------------------------------------
def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    match = _MULTIPART_RE.search(content_type.encode("utf-8"))
    if not match:
        raise ValidationError("无法识别 multipart 边界（boundary）")
    boundary = match.group(1) or match.group(2)
    delimiter = b"--" + boundary
    parts: dict[str, dict] = {}
    for raw_part in body.split(delimiter):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in header_blob.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]+)"', disp)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disp)
        parts[name] = {
            "filename": filename_match.group(1) if filename_match else None,
            "content": content,
        }
    return parts


class AppHandler(BaseHTTPRequestHandler):
    server_version = "SQLLab/1.0"
    registry: SpaceRegistry = None  # 由 main 注入（类属性，所有请求共享）

    # -- 基础工具 -----------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        return

    def _send_json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, data, status: int = 200) -> None:
        self._send_json({"ok": True, "data": data}, status)

    def _fail(self, message: str, status: int = 400, extra: dict | None = None) -> None:
        err = {"message": sanitize_message(message)}
        if extra:
            err.update(extra)
        self._send_json({"ok": False, "error": err}, status)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        if length > config.IMPORT_MAX_UPLOAD_BYTES + 64 * 1024:
            raise ValidationError("请求体超过大小上限")
        return self.rfile.read(length)

    def _json_body(self) -> dict:
        body = self._read_body()
        if not body:
            return {}
        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return data

    def _space(self, params: dict):
        return self.registry.get(params.get("space_id", ""))

    # -- 路由分发 -----------------------------------------------------------
    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._dispatch_get(path, query)
            else:
                self._serve_static(path)
        except (ValidationError, SQLError) as exc:
            self._fail(exc.message if isinstance(exc, SQLError) else str(exc),
                       404 if isinstance(exc, ValidationError) and "不存在" in str(exc) else 400,
                       exc.to_dict() if isinstance(exc, SQLError) else None)
        except Exception as exc:  # 兜底：脱敏后返回，绝不把 traceback 给前端
            self._fail(f"服务器内部错误：{type(exc).__name__}", 500)

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if not path.startswith("/api/"):
                self._fail("not found", 404)
                return
            self._dispatch_post(path)
        except SQLError as exc:
            d = exc.to_dict()
            self._fail(d.pop("message"), 400, d)
        except ValidationError as exc:
            self._fail(str(exc), 400)
        except Exception as exc:
            self._fail(f"服务器内部错误：{type(exc).__name__}", 500)

    def do_DELETE(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/spaces" and self._json_body_safe().get("space_id"):
                params = self._json_body_safe()
                self.registry.delete(params["space_id"])
                self._ok({"deleted": params["space_id"]})
                return
            m = re.fullmatch(r"/api/spaces/([^/]+)/tables/([^/]+)", path)
            if m:
                space = self.registry.get(m.group(1))
                drop_table(space, urllib.parse.unquote(m.group(2)))
                self._ok({"dropped": urllib.parse.unquote(m.group(2))})
                return
            self._fail("not found", 404)
        except SQLError as exc:
            self._fail(exc.message, 400, exc.to_dict())
        except ValidationError as exc:
            self._fail(str(exc), 404 if "不存在" in str(exc) else 400)
        except Exception as exc:
            self._fail(f"服务器内部错误：{type(exc).__name__}", 500)

    def _json_body_safe(self) -> dict:
        try:
            return self._json_body()
        except ValidationError:
            return {}

    # -- GET 路由 -----------------------------------------------------------
    def _dispatch_get(self, path: str, query: dict) -> None:
        if path == "/api/config":
            self._ok({
                "query_timeout_ms": config.QUERY_TIMEOUT_MS,
                "query_max_rows": config.QUERY_MAX_ROWS,
                "page_size_options": list(config.PAGE_SIZE_OPTIONS),
                "import_max_rows": config.IMPORT_MAX_ROWS,
                "import_max_bytes": config.IMPORT_MAX_UPLOAD_BYTES,
            })
            return
        if path == "/api/spaces":
            self._ok([s.to_dict() for s in self.registry.list()])
            return
        if path == "/api/sample-queries":
            self._ok({
                "join": samples.JOIN_QUERIES,
                "attack": samples.ATTACK_QUERIES,
                "stress": samples.STRESS_QUERIES,
            })
            return

        m = re.fullmatch(r"/api/spaces/([^/]+)/tables", path)
        if m:
            self._ok(list_tables(self.registry.get(m.group(1))))
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/history", path)
        if m:
            self._ok(history.list_history(self.registry.get(m.group(1))))
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/results/([^/]+)", path)
        if m:
            space_id, result_id = m.group(1), m.group(2)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", [str(config.QUERY_PAGE_SIZE_DEFAULT)])[0])
            page = query_service.page(space_id, result_id, offset, limit)
            self._ok(page)
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/queries/([^/]+)", path)
        if m:
            self._ok(query_service.get_status(m.group(1), m.group(2)))
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/results/([^/]+)/download", path)
        if m:
            self._download_csv(m.group(1), m.group(2))
            return
        self._fail("not found", 404)

    # -- POST 路由 ----------------------------------------------------------
    def _dispatch_post(self, path: str) -> None:
        if path == "/api/spaces":
            params = self._json_body()
            self._ok(self.registry.create(params.get("name", "")).to_dict(), 201)
            return

        m = re.fullmatch(r"/api/spaces/([^/]+)/import", path)
        if m:
            self._handle_import(self.registry.get(m.group(1)))
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/sample", path)
        if m:
            space = self.registry.get(m.group(1))
            self._ok(samples.load_sample(space))
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/query", path)
        if m:
            space = self.registry.get(m.group(1))
            params = self._json_body()
            # 立即返回 query_id；实际执行在后台线程，前端轮询 /queries/<id>
            self._ok(query_service.submit(space, params.get("sql", "")), 202)
            return
        m = re.fullmatch(r"/api/spaces/([^/]+)/cancel", path)
        if m:
            space = self.registry.get(m.group(1))
            params = self._json_body()
            cancelled = query_service.cancel(space.id, params.get("query_id", ""))
            if not cancelled:
                raise ValidationError("查询不存在或已经结束，无法取消")
            self._ok({"cancelling": params.get("query_id")})
            return
        self._fail("not found", 404)

    def _handle_import(self, space) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" in content_type:
            parts = parse_multipart(self._read_body(), content_type)
            if "file" not in parts:
                raise ValidationError("缺少 file 字段")
            raw = parts["file"]["content"]
            table_field = parts.get("table_name")
            table_name = table_field["content"].decode("utf-8").strip() if table_field else ""
            if not table_name:
                filename = parts["file"]["filename"] or "upload.csv"
                table_name = Path(filename).stem
        else:
            params = self._json_body()
            raw = params.get("content", "").encode("utf-8")
            table_name = params.get("table_name", "")
        self._ok(import_csv(space, raw, table_name), 201)

    # -- CSV 下载 -----------------------------------------------------------
    def _download_csv(self, space_id: str, result_id: str) -> None:
        snap = query_service.get_snapshot(space_id, result_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        if snap.truncated:
            # 截断必须在文件内显式标明（下载只包含允许返回的数据）
            writer.writerow([
                f"# 注意：结果已被截断，仅包含前 {config.QUERY_MAX_ROWS} 行"
                f"（系统单次返回上限）"
            ])
        writer.writerow(snap.columns)
        for row in snap.rows:
            writer.writerow(["" if v is None else v for v in row])
        data = buf.getvalue().encode("utf-8-sig")  # BOM 方便 Excel 识别
        filename = f"query_result_{result_id[:8]}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Result-Truncated", "1" if snap.truncated else "0")
        self.send_header("X-Result-Row-Count", str(len(snap.rows)))
        self.send_header("X-Result-Max-Rows", str(config.QUERY_MAX_ROWS))
        self.end_headers()
        self.wfile.write(data)

    # -- 静态文件 -----------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        # 防目录穿越
        target = (STATIC_ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(STATIC_ROOT.resolve())) or not target.is_file():
            self._fail("not found", 404)
            return
        suffix = target.suffix.lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    AppHandler.registry = SpaceRegistry()
    server = ThreadingHTTPServer((host, port), AppHandler)
    return server


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="SQL 数据空间分析服务（零依赖）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    server = build_server(args.host, args.port)
    print(f"✅ 服务已启动: http://{args.host}:{args.port}")
    print(f"   数据目录: {config.DATA_ROOT}")
    print(f"   查询上限: {config.QUERY_TIMEOUT_MS} ms / {config.QUERY_MAX_ROWS} 行")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…")
        server.shutdown()


if __name__ == "__main__":
    main()
