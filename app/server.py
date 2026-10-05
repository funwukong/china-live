"""Merged China live TV gateway.

Serves the three bundled providers (央视频 / 央视官网 / 广东广电) as one
M3U catalogue plus a proxy endpoint that resolves the live HLS manifest on
demand.  Port defaults to 8577 and is overridable with the PORT env var.
"""

import datetime
import html
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import providers

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8577"))
BASE_URL = os.environ.get("BASE_URL", "")
CATALOG_TTL = 600

_lock = threading.Lock()


def _now():
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(message):
    print("[%s] %s" % (_now(), message), flush=True)


def _base_url(handler):
    if BASE_URL:
        return BASE_URL.rstrip("/")
    host = handler.headers.get("Host") or "%s:%d" % (HOST, PORT)
    return "http://%s" % host


def _origin(handler):
    return _base_url(handler)


def _rewrite(url, origin):
    """Fill in the proxy URL host so imported playlists always hit this box."""
    if not isinstance(url, str) or not url:
        return ""
    if url.startswith("/"):
        return origin + url
    return url


def _catalog(key=None):
    """Returns (groups, errors); groups are [(source, group_name, channel)]."""
    targets = [providers.PROVIDER_MAP[key]] if key else providers.PROVIDERS
    groups = []
    errors = []
    for provider in targets:
        try:
            for group in provider.catalog():
                name = str(group.get("name") or provider.key)
                channels = group.get("channel") or []
                if not isinstance(channels, list):
                    continue
                groups.append((provider.key, name, channels))
        except Exception as error:
            errors.append("%s: %s" % (provider.key, error))
    return groups, errors


def _extinf(channel, group_name):
    parts = ["-1"]
    for attr, key in (("tvg-id", "tvgId"), ("tvg-name", "tvgName"), ("tvg-logo", "logo")):
        value = channel.get(key)
        if isinstance(value, str) and value:
            parts.append('%s="%s"' % (attr, value.replace('"', "'")))
    parts.append('group-title="%s"' % group_name.replace('"', "'"))
    name = str(channel.get("name") or channel.get("tvgName") or "")
    return "#EXTINF:" + " ".join(parts) + "," + name


def _m3u(handler, key=None):
    origin = _origin(handler)
    groups, errors = _catalog(key)
    lines = ["#EXTM3U"]
    total = 0
    for source_key, group_name, channels in groups:
        label = "%s / %s" % (providers.PROVIDER_MAP[source_key].title(), group_name)
        for channel in channels:
            if not isinstance(channel, dict):
                continue
            urls = channel.get("urls") or []
            url = _rewrite(urls[0] if urls else "", origin)
            if not url:
                continue
            lines.append(_extinf(channel, label))
            lines.append(url)
            total += 1
    body = "\n".join(lines) + "\n"
    if not total:
        return 503, body, errors
    return 200, body, errors


def _index(handler):
    cards = []
    for provider in providers.PROVIDERS:
        rows = []
        try:
            groups = provider.catalog()
            status = "ok"
        except Exception as error:
            groups = []
            status = str(error)
        count = 0
        for group in groups:
            for channel in group.get("channel") or []:
                if isinstance(channel, dict) and (channel.get("urls") or []):
                    count += 1
        rows.append(
            '<li><a href="/%s.m3u">%s</a> <span>%d 路</span> <em>%s</em></li>'
            % (provider.key, html.escape(provider.title()), count, html.escape(status))
        )
        cards.append("".join(rows))
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>China Live</title><style>"
        "body{font-family:-apple-system,Helvetica,Arial,\"PingFang SC\",\"Microsoft YaHei\",sans-serif;"
        "max-width:760px;margin:0 auto;padding:24px;color:#1c1c1e;line-height:1.6}"
        "li{margin:8px 0}span{color:#666;font-size:12px;margin-left:8px}"
        "em{color:#999;font-style:normal;font-size:12px}"
        "code{background:#f2f2f7;padding:2px 6px;border-radius:4px}"
        "</style></head><body><h2>China Live</h2>"
        "<p>聚合订阅：<a href=\"/all.m3u\"><code>/all.m3u</code></a> —— 一次性导入播放器。</p>"
        "<p>健康探针 <code>/health</code> · 诊断 <code>/diag</code></p>"
        "<ul>%s</ul></body></html>" % "".join(cards)
    )


def _diag():
    lines = []
    for provider in providers.PROVIDERS:
        try:
            groups = provider.catalog()
            count = sum(
                len(group.get("channel") or [])
                for group in groups
                if isinstance(group.get("channel"), list)
            )
            state = "ok channels=%d updated=%ds ago" % (
                count,
                int(time.time() - provider.updated),
            )
        except Exception as error:
            state = "error: %s" % error
        lines.append("%-6s %-12s %s" % (provider.key, provider.title(), state))
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    server_version = "china-live/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._send(200, _index(self), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(200, "ok\n")
            return
        if path == "/diag":
            self._send(200, _diag())
            return
        if path == "/sources":
            payload = [
                {"key": p.key, "name": p.title(), "url": p.url}
                for p in providers.PROVIDERS
            ]
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path == "/all.m3u":
            code, body, errors = _m3u(self)
            if errors:
                log("all.m3u partial: %s" % "; ".join(errors))
            self._send(code, body, "application/vnd.apple.mpegurl; charset=utf-8")
            return
        if path == "/proxy":
            self._proxy(urllib.parse.parse_qs(parsed.query))
            return

        if path.endswith(".m3u") and len(path) > 4:
            key = path[1:-4]
            if key not in providers.PROVIDER_MAP:
                self._send(404, "unknown source: %s\n" % key)
                return
            code, body, errors = _m3u(self, key)
            if errors:
                log("%s.m3u failed: %s" % (key, "; ".join(errors)))
            self._send(code, body, "application/vnd.apple.mpegurl; charset=utf-8")
            return

        self._send(404, "not found\n")

    def _proxy(self, query):
        params = {key: values[-1] for key, values in query.items()}
        key = params.pop("sp", "")
        provider = providers.PROVIDER_MAP.get(key)
        if provider is None:
            self._send(400, "unknown source: %s\n" % key)
            return
        started = time.time()
        result = provider.proxy(params)
        code = int(result[0])
        ctype = str(result[1] or "text/plain; charset=utf-8")
        body = result[2] if len(result) > 2 else ""
        extra = result[3] if len(result) > 3 and isinstance(result[3], dict) else {}
        headers = dict(extra)
        if code in (301, 302, 303, 307, 308) and not headers.get("Location"):
            self._send(502, "missing redirect target\n")
            return
        log("proxy %s %s -> %d (%.2fs)" % (key, params, code, time.time() - started))
        self._send(code, body, ctype, headers)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log("china-live 启动: %d 个源, 监听 %s:%d" % (len(providers.PROVIDERS), HOST, PORT))
    log("首页: http://127.0.0.1:%d/" % PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for provider in providers.PROVIDERS:
            provider.destroy()


if __name__ == "__main__":
    main()
