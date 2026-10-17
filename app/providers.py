"""Loads and caches the bundled live providers.

Three shapes are supported:

* ``live``  - a CatVod live spider exposing ``liveContent`` / ``localProxy``
              (used by 广东广电).  Channels point at ``/proxy?...``.
* ``aptv``  - the legacy JCE + bkliveinfo spider (kept as a pure-stdlib
              fallback; channels point at ``/aptv/<slug>.m3u8``).
* ``ysp``   - the vendored ysp-live v9.5 engine (央视频设备协议).  It provides
              real 4K/8K, 7-day catchup and the aggregated EPG; channels point
              at ``/ysp/<slug>.m3u8`` and TS segments are relayed through
              ``/proxy.ts`` because the device protocol needs signed playback
              headers.
"""

import importlib
import os
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(APP_DIR, "live")
DATA_DIR = os.environ.get("CHINA_LIVE_DATA_DIR", os.path.join(APP_DIR, "data"))
CATALOG_CACHE_SECONDS = 600

SOURCES = [
    {
        "key": "ysp",
        "module": "live.ysp",
        "kind": "ysp",
        "url": "",
        "ext": "",
        "timeout": 30,
        # ysp engine knobs (see app/live/ysp/runtime.py)
        "engine_port": int(os.environ.get("YSP_ENGINE_PORT", "0") or 0),
        "no_device": os.environ.get("YSP_NO_DEVICE", "").lower() in ("1", "true", "yes"),
        "only_4k": os.environ.get("YSP_ONLY_4K", "").lower() in ("1", "true", "yes"),
        "links_per_device": int(os.environ.get("YSP_LINKS_PER_DEVICE", "6") or 0),
        "zap_debounce": float(os.environ.get("YSP_ZAP_DEBOUNCE", "10") or 0),
        "cache_mb": int(os.environ.get("YSP_CACHE_MB", "200") or 0),
        "no_engine": os.environ.get("YSP_NO_ENGINE", "").lower() in ("1", "true", "yes"),
    },
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
        self.config = config
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


class YspProvider(Provider):
    """Adapter for the vendored ysp-live v9.5 engine (央视频设备协议).

    Unlike the other providers this one boots a long-lived runtime: a
    registered CCTV device identity, a heartbeat keeper and the optional
    Node WASM companion engine.  The catalogue is static, so it is read
    straight from the engine's channel directory.
    """

    kind = "ysp"

    def ensure(self):
        with self.lock:
            if self.spider is not None or self.error is not None:
                return self.spider
            try:
                module = importlib.import_module(self.module_name)
                from live.ysp.runtime import YspRuntime
            except Exception as error:
                self.error = "import failed: %s" % error
                log("%s %s" % (self.key, self.error))
                return None
            try:
                runtime = YspRuntime(
                    data_dir=DATA_DIR,
                    port=self.port,
                    engine_port=self.config.get("engine_port") or 0,
                    no_device=bool(self.config.get("no_device")),
                    only_4k=bool(self.config.get("only_4k")),
                    links_per_device=self.config.get("links_per_device", 6),
                    zap_debounce=self.config.get("zap_debounce", 10.0),
                    cache_mb=self.config.get("cache_mb", 200),
                    start_engine=not self.config.get("no_engine"),
                    log=lambda msg: log("%s" % msg),
                )
                runtime.start()
            except Exception as error:
                self.error = "start failed: %s" % error
                log("%s %s" % (self.key, self.error))
                return None
            self.module = module
            self.spider = runtime
            return runtime

    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.port = int(os.environ.get("PORT", "8577") or 8577)

    def title(self):
        return "央视频 (设备协议 v9.5)"

    def build_catalog(self, runtime):
        groups = runtime.catalog()
        for group in groups:
            for channel in group["channel"]:
                slug = channel.pop("slug")
                channel["urls"] = ["/ysp/%s.m3u8" % slug]
                channel["catchup"] = bool(channel.get("catchup"))
        return groups

    def playlist(self, slug, host_header, proto="http", catchup=None):
        runtime = self.ensure()
        if runtime is None:
            raise RuntimeError(self.error or "provider unavailable")
        return runtime.playlist(slug, host_header, proto, catchup)

    def epg_xml(self):
        runtime = self.ensure()
        if runtime is None:
            raise RuntimeError(self.error or "provider unavailable")
        return runtime.epg_xml()

    def epg_status(self):
        runtime = self.ensure()
        return runtime.epg_status() if runtime is not None else {"available": False}

    def health(self):
        runtime = self.ensure()
        return runtime.health() if runtime is not None else {"error": self.error}

    def diag_lines(self):
        runtime = self.ensure()
        return runtime.diag_lines() if runtime is not None else "ysp: unavailable\n"

    def ts_relay_token(self, path_with_query):
        """Decode a ``/proxy.ts?=token`` request into the upstream TS URL."""
        runtime = self.ensure()
        if runtime is None:
            raise RuntimeError(self.error or "provider unavailable")
        return runtime.decode_ts_token(path_with_query)

    def playback_headers(self, real_url):
        runtime = self.ensure()
        return runtime.playback_headers(real_url) if runtime is not None else {}

    def ts_cache(self):
        runtime = self.ensure()
        return runtime.ts_cache() if runtime is not None else None

    def destroy(self):
        runtime, self.spider = self.spider, None
        self.module = None
        self.groups = []
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:
                pass


_KINDS = {"aptv": AptvProvider, "ysp": YspProvider}

PROVIDERS = [_KINDS[c["kind"]](c) if c.get("kind") in _KINDS else Provider(c)
             for c in SOURCES]
PROVIDER_MAP = {provider.key: provider for provider in PROVIDERS}
