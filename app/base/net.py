"""Standalone replacement for the CatVod/TVBox ``base.net`` module.

The bundled live spiders only touch ``net.req``, ``net.json``, ``net.cached``,
``net.session`` and ``net.ws``.  This module implements that surface on top of
requests / websocket-client so the spiders can run outside the player.
"""

import json
import threading
import time
import urllib.parse

import requests

DEFAULT_TIMEOUT = 10.0


def _timeout(options):
    try:
        seconds = float((options or {}).get("timeout", 10000)) / 1000.0
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return seconds if seconds > 0 else DEFAULT_TIMEOUT


def _binary(options):
    # buffer=0 (or unset) -> text, anything else -> raw bytes
    return bool((options or {}).get("buffer"))


def _perform(caller, url, options):
    options = options or {}
    method = str(options.get("method") or ("POST" if "data" in options else "GET")).upper()
    kwargs = {
        "headers": dict(options.get("headers") or {}),
        "timeout": _timeout(options),
    }
    if options.get("params"):
        kwargs["params"] = options["params"]
    data = options.get("data")
    if data is not None:
        if str(options.get("postType", "json")).lower() in ("form", "urlencoded"):
            kwargs["data"] = data
        else:
            kwargs["json"] = data
    try:
        response = caller.request(method, url, **kwargs)
    except Exception as error:
        return {"code": 502, "error": str(error), "content": ""}
    content = response.content if _binary(options) else response.text
    return {
        "code": response.status_code,
        "content": content,
        "headers": dict(response.headers),
    }


class Session:
    """Cookie carrying session that can set a cookie for a whole domain."""

    def __init__(self):
        self._session = requests.Session()

    def req(self, url, options=None):
        return _perform(self._session, url, options)

    def json(self, url, options=None):
        return Net._as_json(self.req(url, options))

    def setCookie(self, url, cookie):
        parts = [part.strip() for part in str(cookie).split(";")]
        name, _, value = parts[0].partition("=")
        if not name.strip():
            return False
        domain = ""
        path = "/"
        for part in parts[1:]:
            key, _, raw = part.partition("=")
            key = key.strip().lower()
            if key == "domain":
                domain = raw.strip()
            elif key == "path":
                path = raw.strip() or "/"
        if not domain:
            domain = urllib.parse.urlsplit(url).hostname or ""
        if not domain:
            return False
        try:
            self._session.cookies.set(name.strip(), value.strip(), domain=domain, path=path)
            return True
        except Exception:
            return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def close(self):
        self._session.close()


class Net:

    def __init__(self):
        self._cache = {}
        self._locks = {}
        self._guard = threading.Lock()

    def req(self, url, options=None):
        return _perform(requests, url, options)

    def json(self, url, options=None):
        return self._as_json(self.req(url, options))

    def session(self):
        return Session()

    def ws(self, url, options=None):
        options = options or {}
        try:
            import websocket
        except ImportError as error:
            return {"code": 0, "error": "websocket-client missing: %s" % error, "content": ""}
        headers = ["%s: %s" % (key, value) for key, value in (options.get("headers") or {}).items()]
        try:
            client = websocket.create_connection(url, header=headers, timeout=_timeout(options))
            try:
                payload = options.get("data")
                if payload is not None:
                    client.send(json.dumps(payload))
                content = client.recv()
            finally:
                client.close()
        except Exception as error:
            return {"code": 0, "error": str(error), "content": ""}
        return {"code": 101, "content": content}

    def cached(self, key, options=None, loader=None):
        options = options or {}
        try:
            ttl = float(options.get("ttl") or 0) / 1000.0
        except (TypeError, ValueError):
            ttl = 0.0
        allow_stale = bool(options.get("stale"))
        with self._guard:
            entry = self._cache.get(key)
            lock = self._locks.setdefault(key, threading.Lock())
        if entry is not None and time.time() - entry[0] < ttl:
            return entry[1]
        with lock:
            with self._guard:
                entry = self._cache.get(key)
            if entry is not None and time.time() - entry[0] < ttl:
                return entry[1]
            try:
                value = loader()
            except Exception:
                if entry is not None and allow_stale:
                    return entry[1]
                raise
            with self._guard:
                self._cache[key] = (time.time(), value)
            return value

    @staticmethod
    def _as_json(result):
        if result.get("error"):
            raise ValueError(str(result["error"]))
        if result["code"] >= 400:
            raise ValueError("HTTP " + str(result["code"]))
        try:
            return json.loads(result["content"])
        except (TypeError, ValueError) as error:
            raise ValueError("invalid json: %s" % error) from None
