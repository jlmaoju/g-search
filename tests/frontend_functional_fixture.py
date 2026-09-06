"""Deterministic browser QA: slow queries, one failed request, rare hosts.

Run: python tests/frontend_functional_fixture.py --port 8878
No production catalog, API keys, or external search calls are used.
"""
import argparse
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "gcores_crawler" / "frontend"
LOCK = threading.Lock()
ATTEMPTS = {}


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

    def do_GET(self):
        parsed = urlsplit(self.path)
        status = 200
        if parsed.path == "/assets/runtime-config.json":
            payload = {"apiBase": ""}
        elif parsed.path == "/api/meta":
            payload = {"title": "机核电台记忆检索", "mode": "server", "mode_label": "在线版", "library_updated_at": "2026-09-04T18:59:30+08:00", "participants_count": 645}
        elif parsed.path == "/api/participants":
            payload = {"participants": [{"name": f"测试参与者{i}", "count": 1} for i in range(1, 646)], "program_types": [{"name": "Gadio News", "count": 4}, {"name": "Gadio Pro", "count": 2}]}
        elif parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            if query.startswith("慢查询"):
                time.sleep(1.2)
            with LOCK:
                ATTEMPTS[query] = ATTEMPTS.get(query, 0) + 1
                attempt = ATTEMPTS[query]
            if query.startswith("断线恢复") and attempt == 1:
                status = 503
                payload = {"error": "fixture: temporarily unavailable"}
            else:
                scope = params.get("scope", ["content"])[0]
                people = params.get("participants", [])
                payload = {
                    "query": query, "scope": scope,
                    "results": [] if query == "空结果" else [{
                        "doc_id": "fixture-1", "item_id": "197231", "item_title": f"功能测试结果：{query}",
                        "doc_type": "item_title" if scope == "title" else "scene_summary",
                        "category": "Gadio News", "users": people or ["测试参与者645"],
                        "published_at": "2025-04-19", "display_timestamp": "09:22",
                        "display_text": "这是隔离测试片段，用于验证查询、筛选与键盘操作。",
                        "source_url": "https://www.gcores.com/radios/197231",
                        "neighbor_atoms": [{"display_timestamp": "09:22", "text": "这是用于浏览器回归验证的上下文。"}],
                    }],
                }
                if query == "额度测试":
                    payload.update(degraded=True, degraded_reason="embedding_quota_exhausted", degraded_detail="请到机核私信 YQBelmont贲", recall_stats={"lexical_fallback": True})
        else:
            return super().do_GET()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Expected when the frontend cancels the slow request.

    def log_message(self, _format, *_args):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8878)
    args = parser.parse_args()
    print(f"Functional fixture: http://127.0.0.1:{args.port}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), FixtureHandler).serve_forever()
    except KeyboardInterrupt:
        pass
