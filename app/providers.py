"""Loads and caches the bundled CatVod live providers."""

import importlib
import os
import threading
import time

from base.spider import Spider

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(APP_DIR, "live")
CATALOG_CACHE_SECONDS = 600

SOURCES = [
    {
        "key": "ysp",
        "module": "live.yangshipin",
        "url": "https://capi.yangshipin.cn/api/oms/pc/navigation/home_top_nav",
        "ext": os.path.join(LIVE_DIR, "yangshipin.json"),
        "timeout": 30,
    },
    {
        "key": "cctv",
        "module": "live.cctv",
        "url": "https://tv.cctv.com/live/",
        "ext": os.path.join(LIVE_DIR, "cctv.json"),
        "timeout": 20,
    },
    {
        "key": "gdtv",
        "module": "live.gdtv",
        "url": "https://gdtv-api.gdtv.cn/api/tv/v2/tvChannel?category=0",
        "ext": "",
        "timeout": 20,
    },
]


def log(message):
    print("[providers] %s" % message, flush=True)


class Provider:

    def __init__(self, config):
        self.key = config["key"]
        self.url = config["url"]
        self.ext = config["ext"]
        self.module_name = config["module"]
        self.timeout = int(config.get("timeout") or 20)
        self.lock = threading.RLock()
        self.spider = None
        self.error = None
        self.groups = []
        self.updated = 0.0

    def import_error(self):
        return self.error

    def ensure(self):
        with self.lock:
            if self.spider is not None or self.error is not None:
                return self.spider
            try:
                spider = importlib.import_module(self.module_name).Spider()
            except Exception as error:
                self.error = "import failed: %s" % error
                log("%s %s" % (self.key, self.error))
                return None
            spider.key = self.key
            spider.proxy_base = ""
            try:
                spider.init(self.ext)
            except Exception as error:
                self.error = "init failed: %s" % error
                log("%s %s" % (self.key, self.error))
                return None
            self.spider = spider
            return spider

    def title(self):
        spider = self.ensure()
        if spider is None:
            return "%s (unavailable)" % self.key
        try:
            name = spider.getName()
        except Exception:
            name = ""
        return name or self.key

    def catalog(self, refresh=False):
        """Returns [[group_name, [channel...]]]; raises RuntimeError on failure."""
        with self.lock:
            if self.groups and not refresh and time.time() - self.updated < CATALOG_CACHE_SECONDS:
                return self.groups
            spider = self.ensure()
            if spider is None:
                raise RuntimeError(self.error or "provider unavailable")
            try:
                groups = spider.liveContent(self.url)
            except Exception as error:
                raise RuntimeError("catalog failed: %s" % error) from None
            if not isinstance(groups, list) or not groups:
                raise RuntimeError("empty catalog")
            self.groups = groups
            self.updated = time.time()
            return groups

    def proxy(self, params):
        spider = self.ensure()
        if spider is None:
            return [502, "text/plain; charset=utf-8", self.error or "provider unavailable"]
        try:
            result = spider.localProxy(params)
        except Exception as error:
            return [502, "text/plain; charset=utf-8", "proxy failed: %s" % error]
        if not isinstance(result, (list, tuple)) or not result:
            return [502, "text/plain; charset=utf-8", "empty proxy result"]
        return list(result)

    def destroy(self):
        with self.lock:
            spider, self.spider = self.spider, None
            self.groups = []
        if spider is not None:
            try:
                spider.destroy()
            except Exception:
                pass


PROVIDERS = [Provider(config) for config in SOURCES]
PROVIDER_MAP = {provider.key: provider for provider in PROVIDERS}
