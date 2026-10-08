"""Loads and caches the bundled live providers.

Two shapes are supported:

* ``live``  - a CatVod live spider exposing ``liveContent`` / ``localProxy``
              (used by 广东广电).  Channels point at ``/proxy?...``.
* ``aptv``  - a TVBox vod-style spider exposing ``homeContent`` /
              ``categoryContent``.  It serves a direct-CDN HLS playlist
              (optimized from waastudios/ysptp-docker) so the player pulls
              segments straight from CCTV's CDN; channels point at
              ``/aptv/<slug>.m3u8``.
"""

import importlib
import os
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(APP_DIR, "live")
CATALOG_CACHE_SECONDS = 600

SOURCES = [
    {
        "key": "aptv",
        "module": "live.cctv",
        "kind": "aptv",
        "url": "",
        "ext": "",
        "timeout": 30,
    },
    {
        "key": "gdtv",
        "module": "live.gdtv",
        "kind": "live",
        "url": "https://gdtv-api.gdtv.cn/api/tv/v2/tvChannel?category=0",
        "ext": "",
        "timeout": 20,
    },
]


def log(message):
    print("[providers] %s" % message, flush=True)


class Provider:

    kind = "live"

    def __init__(self, config):
        self.key = config["key"]
        self.url = config["url"]
        self.ext = config["ext"]
        self.module_name = config["module"]
        self.kind = config.get("kind", "live")
        self.timeout = int(config.get("timeout") or 20)
        self.lock = threading.RLock()
        self.spider = None
        self.module = None
        self.error = None
        self.groups = []
        self.updated = 0.0

    def ensure(self):
        with self.lock:
            if self.spider is not None or self.error is not None:
                return self.spider
            try:
                module = importlib.import_module(self.module_name)
                spider = module.Spider()
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
            self.module = module
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

    def build_catalog(self, spider):
        """Hook for subclasses; returns a list of ``{name, channel}`` groups."""
        return spider.liveContent(self.url)

    def catalog(self, refresh=False):
        """Returns ``[{name, channel}]``; raises RuntimeError on failure."""
        with self.lock:
            if self.groups and not refresh and time.time() - self.updated < CATALOG_CACHE_SECONDS:
                return self.groups
            spider = self.ensure()
            if spider is None:
                raise RuntimeError(self.error or "provider unavailable")
            try:
                groups = self.build_catalog(spider)
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
            self.module = None
            self.groups = []
        if spider is not None:
            try:
                spider.destroy()
            except Exception:
                pass


class AptvProvider(Provider):
    """Adapter for the APTV / 央视频 spider, which is category based."""

    kind = "aptv"

    def build_catalog(self, spider):
        home = spider.homeContent({}) or {}
        categories = home.get("class") or []
        groups = []
        for category in categories:
            if not isinstance(category, dict):
                continue
            tid = str(category.get("type_id") or "")
            name = str(category.get("type_name") or tid)
            if not tid:
                continue
            try:
                result = spider.categoryContent(tid, 1, {}, {}) or {}
            except Exception as error:
                log("%s category %s failed: %s" % (self.key, tid, error))
                continue
            channels = []
            for card in result.get("list") or []:
                if not isinstance(card, dict):
                    continue
                slug = str(card.get("vod_id") or "").strip()
                if not slug:
                    continue
                display = str(card.get("vod_name") or slug)
                channels.append({
                    "name": display,
                    "tvgName": display,
                    "tvgId": slug,
                    "logo": str(card.get("vod_pic") or ""),
                    "urls": ["/aptv/%s.m3u8" % slug],
                })
            if channels:
                groups.append({"name": name, "channel": channels})
        return groups

    def aptv_playlist(self, slug):
        """Build the direct-CDN m3u8 for ``slug`` from the APTV spider."""
        spider = self.ensure()
        if spider is None:
            raise RuntimeError(self.error or "provider unavailable")
        module = self.module
        ch = module.CHANNEL_STATE.get(slug)
        if ch is None:
            raise RuntimeError("unknown channel: %s" % slug)
        module._ensure_channel(ch)
        playlist = module.build_playlist(ch)
        if not playlist:
            # 首拉可能尚未拿到数据, 给后台线程一点时间刷新
            for _ in range(20):
                playlist = module.build_playlist(ch)
                if playlist:
                    break
                time.sleep(1)
        if not playlist:
            raise RuntimeError("频道 %s 暂无数据: %s" % (slug, ch.last_error or "拉取中"))
        return playlist


PROVIDERS = [AptvProvider(c) if c.get("kind") == "aptv" else Provider(c) for c in SOURCES]
PROVIDER_MAP = {provider.key: provider for provider in PROVIDERS}
