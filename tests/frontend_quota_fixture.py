from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


FRONTEND_ROOT = Path(__file__).resolve().parents[1] / "gcores_crawler" / "frontend"

RESPONSES = {
    "/api/meta": {
        "title": "机核电台记忆检索",
        "mode": "server",
        "mode_label": "在线版",
        "library_updated_at": "2026-08-15T00:00:00+08:00",
    },
    "/api/participants": {"program_types": [], "participants": []},
    "/api/search": {
        "degraded": True,
        "degraded_reason": "embedding_quota_exhausted",
        "degraded_message": "语义检索额度不足，当前显示的是关键词降级结果，相关性和排序质量会明显下降。",
        "degraded_detail": "如果你看到这条提示，请到机核私信 YQBelmont贲 告知一声，感谢。",
        "semantic_error": 'HTTP 429: {"code":"1113","msg":"余额不足"}',
        "recall_stats": {"lexical_fallback": True},
        "results": [],
    },
}


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(FRONTEND_ROOT), **kwargs)

    def do_GET(self):
        payload = RESPONSES.get(urlsplit(self.path).path)
        if payload is None:
            return super().do_GET()

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    try:
        ThreadingHTTPServer(("127.0.0.1", args.port), FixtureHandler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
