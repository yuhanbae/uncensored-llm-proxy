#!/usr/bin/env python3
"""uncensored-llm proxy: stable public face for the rotating llm-compute tunnel.

- Resolves the current trycloudflare URL from the GitHub Gist on every request
  (60s cache, bypassed on upstream errors).
- Streams SSE chat completions through unchanged.
- Passes through /v1/models, /v1/models/<id>, /props, /health.
Env: GIST_TOKEN (GitHub PAT), GIST_ID, PORT (Railway sets PORT)
"""
import http.client, http.server, json, os, ssl, threading, time, urllib.request

GIST_TOKEN = os.environ.get("GIST_TOKEN", "")
GIST_ID = os.environ.get("GIST_ID", "7ec01cb438fa7f501847b725ed44063a")
PORT = int(os.environ.get("PORT", "8080"))
TTL = 60

_cache = {"url": None, "ts": 0.0}
_lock = threading.Lock()

def resolve_upstream(force=False):
    with _lock:
        now = time.time()
        if not force and _cache["url"] and now - _cache["ts"] < TTL:
            return _cache["url"]
        req = urllib.request.Request(
            f"https://api.github.com/gists/{GIST_ID}",
            headers={"Authorization": f"Bearer {GIST_TOKEN}", "User-Agent": "railway-proxy"})
        with urllib.request.urlopen(req, timeout=15) as r:
            gist = json.loads(r.read().decode())
        url = gist["files"]["url.txt"]["content"].strip().rstrip("/")
        if not url.startswith("https://"):
            raise RuntimeError("bad gist url")
        _cache["url"], _cache["ts"] = url, now
        return url

def fetch(upstream, method, path, body=None, headers=None, timeout=900):
    """Return (status, httpmessage, raw-streaming file object) from upstream."""
    parsed = urllib.parse.urlsplit(upstream)
    ctx = ssl.create_default_context()
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), timeout=timeout, context=ctx if parsed.scheme == "https" else None)
    h = dict(headers or {})
    if body is not None:
        h["Content-Length"] = str(len(body))
    conn.request(method, path, body=body, headers=h)
    resp = conn.getresponse()
    return conn, resp

class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _passthrough(self, include_body=False):
        length = int(self.headers.get("Content-Length") or 0) if include_body else 0
        body = self.rfile.read(length) if include_body else None
        # Path mapping: the public face carries a /v1 prefix (BASE=".../v1"), but
        # llama-server only serves OpenAI endpoints under /v1. Other /v1/* paths
        # (e.g. /v1/props) map to the bare root endpoint (/props).
        path = self.path
        OPENAI_V1 = ("/v1/chat/completions", "/v1/completions", "/v1/models",
                     "/v1/embeddings", "/v1/responses", "/v1/rerank", "/v1/reranking")
        if path.startswith("/v1/") and not path.startswith(OPENAI_V1):
            path = path[3:]  # /v1/props -> /props
        attempts = 0
        while attempts < 2:
            upstream = resolve_upstream(force=(attempts > 0))
            try:
                fwd = {k: v for k, v in self.headers.items()
                       if k.lower() not in ("host", "content-length", "connection", "transfer-encoding")}
                conn, resp = fetch(upstream, self.command, path, body=body, headers=fwd)
            except Exception as e:
                attempts += 1
                if attempts >= 2:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    msg = json.dumps({"error": {"message": f"upstream unreachable: {e}"}}).encode()
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                continue
            status = resp.getcode()
            # retry only on tunnel-rotation/infra errors, never on client errors
            if status in (502, 530, 524) and attempts == 0:
                try:
                    resp.close()
                except Exception:
                    pass
                try:
                    conn.close()
                except Exception:
                    pass
                attempts += 1
                continue
            self.send_response(status)
            is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "content-length", "connection", "keep-alive"):
                    continue
                self.send_header(k, v)
            if is_sse or include_body:
                # close-delimited streaming: no Content-Length
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    pass
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
            try:
                conn.close()
            except Exception:
                pass
            self.close_connection = True
            return

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._passthrough()

    def do_POST(self):
        self._passthrough(include_body=True)

    def do_HEAD(self):
        self._passthrough()

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Proxy)
    server.daemon_threads = True
    print(f"proxy listening on {PORT}", flush=True)
    server.serve_forever()
