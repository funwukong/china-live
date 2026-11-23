"""Standalone replacement for the CatVod/TVBox ``base.spider`` module.

Only the pieces the bundled live spiders rely on are implemented: a ``net``
helper, a ``getProxyUrl`` builder and the no-op lifecycle hooks.
"""

import urllib.parse

from base.net import Net


class Spider:

    key = ""
    proxy_base = ""

    def __init__(self):
        self.net = Net()
        self.local = _LocalStore()

    def init(self, extend=""):
        pass

    def getName(self):
        return ""

    def liveContent(self, url):
        return []

    def localProxy(self, param):
        return [500, "text/plain", "localProxy not implemented"]

    def destroy(self):
        pass

    def getProxyUrl(self, param=None):
        params = dict(param or {})
        if self.key:
            params.setdefault("sp", self.key)
        return (self.proxy_base or "") + "/proxy?" + urllib.parse.urlencode(params)


class _LocalStore:

    def __init__(self):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
