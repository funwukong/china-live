"""In-process runtime for the vendored ysp-live v9.5 engine.

china-live ships its own HTTP gateway, so the upstream standalone server is not
used.  Instead this module boots the upstream :class:`Resolver` (CCTV device
protocol), the Node.js WASM companion engine and the EPG aggregator, then
exposes the handful of primitives the gateway needs:

* :meth:`YspRuntime.catalog`      -- channel groups for the M3U
* :meth:`YspRuntime.playlist`     -- the four-layer fallback chain per channel
* :meth:`YspRuntime.ts_relay`     -- the ``/proxy.ts`` stream relay
* :meth:`YspRuntime.epg_xml`      -- aggregated XMLTV
* :meth:`YspRuntime.health`       -- engine/session/device diagnostics

The engine's own ``Handler``/``main()`` are left untouched; only the module
globals it expects at runtime (``server_port``, ``resolver``,
``active_backend_channels``, ``global_ts_cache``) are set from here.
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_PATH = os.path.join(HERE, "engine.py")
EPG_PATH = os.path.join(HERE, "epg.py")
ENGINE_JS = os.path.join(HERE, "engine.js")

# Upstream reads the EPG module by its original name.
_EPG_MODULE_NAME = "epg_agg"

# The Node companion only speaks to CCTV-family CDNs; upstream already
# allow-lists the hosts, we only cap how long we let it run for.
ENGINE_BOOT_TIMEOUT = 20.0

# ``/proxy.ts`` accepts a *stateless* base64 token, so it is a public open
# relay unless the target host is pinned. Upstream relies on "only we issue
# tokens", which is not a boundary -- anyone can base64 their own URL. We
# therefore allow-list the same CCTV-family suffixes the upstream engine proxy
# uses (plus nothing else), which blocks SSRF against 127.0.0.1, the cloud
# metadata address and RFC1918 hosts.
TS_RELAY_ALLOWED_SUFFIXES = (
    ".cctv.cn", ".cctv.com", ".yangshipin.cn", ".cntv.cn", ".cntv.com",
    ".myqcloud.com", ".qqlive.com", ".gtimg.cn", ".alicdn.com", ".alikunlun.com",
)

# Served while the first EPG pull is still warming up in the background.
_EMPTY_EPG = (
    "<?xml version='1.0' encoding='utf-8'?>\n"
    "<tv generator-info-name=\"china-live-epg\"></tv>\n"
)


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class YspEngineError(RuntimeError):
    pass


class YspRuntime:
    """Owns the vendored engine's process-wide state for this gateway."""

    def __init__(self, data_dir, port=8577, engine_port=0,
                 no_device=False, only_4k=False, links_per_device=6,
                 zap_debounce=10.0, cache_mb=200, timeout=10.0,
                 start_engine=True, log=print):
        self.data_dir = data_dir
        self.port = port
        self.engine_port = engine_port or (port + 1)
        self.no_device = no_device
        self.only_4k = only_4k
        self.links_per_device = links_per_device
        self.zap_debounce = zap_debounce
        self.cache_mb = cache_mb
        self.timeout = timeout
        self.start_engine = start_engine
        self.log = log

        self.engine = None
        self.resolver = None
        self._engine_proc = None
        self._lock = threading.RLock()
        self._ready = False
        self._last_error = ""
        self._device_ready_at = 0.0
        self._epg_wanted = set()

    # ------------------------------------------------------------------ boot

    def start(self):
        """Load the engine and kick off background workers. Never raises."""
        with self._lock:
            if self.engine is not None:
                return
            os.makedirs(self.data_dir, exist_ok=True)
            # The engine imports ``epg_agg`` by name at module scope.
            try:
                _load_module(_EPG_MODULE_NAME, EPG_PATH)
            except Exception as error:
                self._last_error = "epg load failed: %s" % error
                self.log("[ysp] %s (EPG 将不可用)" % self._last_error)
            self.engine = _load_module("ysp_engine", ENGINE_PATH)
            self.engine.server_port = self.port
            self.engine.engine_port = self.engine_port

            if self.cache_mb > 0:
                self.engine.global_ts_cache = self.engine.TsSegmentCache(
                    max_mb=self.cache_mb)

            if self.no_device:
                self.engine.active_backend_channels = set()
            elif self.only_4k:
                self.engine.active_backend_channels = set(
                    self.engine.TRUE_4K_CHANNELS)
            else:
                self.engine.active_backend_channels = set(
                    self.engine.BACKEND_CHANNELS)

            self.engine.resolve_paths(self._engine_args())
            self._epg_wanted = {
                self.engine.TVG_IDS.get(slug, slug.upper())
                for slug in self.engine.global_channel_dir.canonical_slugs
            }
            self._rewire_epg()

            if not self.no_device:
                self.resolver = self.engine.Resolver(self._engine_args())
                self.engine.resolver = self.resolver
                self.resolver.start_heartbeat()
                self.resolver.start_refresh_worker()
                threading.Thread(target=self._init_device,
                                 daemon=True, name="ysp-device-init").start()

            if self.start_engine:
                self._start_companion_engine()
            # Warm the EPG off the request path; a cold pull of both upstream
            # XMLTV feeds takes ~2 minutes and must not block the first /epg.xml.
            threading.Thread(target=self._warm_epg, daemon=True,
                             name="ysp-epg-warm").start()

    def _engine_args(self):
        return argparse.Namespace(
            host="0.0.0.0",
            port=self.port,
            timeout=float(self.timeout),
            insecure_tls=False,
            cache_ttl=60.0,
            stale_while_refresh_ttl=0.0,
            refresh_error_cooldown=5.0,
            playlist_cache_ttl=2.0,
            background_refresh_queue_limit=4,
            http_workers=16,
            http_queue_limit=1000,
            identity_reset_error_threshold=0,
            identity_reset_cooldown=300.0,
            refresh_interval=1.0,
            control_step_jitter_min_ms=0,
            control_step_jitter_max_ms=0,
            heartbeat_interval=30.0,
            heartbeat_ttl_guard=60.0,
            session_ttl=7200.0,
            meta_json=os.path.join(self.data_dir, "proxy-cache-state-rs.json"),
            device_json=os.path.join(self.data_dir, "device-state-rs.json"),
            links_per_device=max(0, int(self.links_per_device)),
            zap_debounce=float(self.zap_debounce),
        )

    def _rewire_epg(self):
        """Point the engine's aggregator at the vendored copy + full ID set."""
        aggregator = self.engine.epg_aggregator
        if aggregator is None:
            return
        aggregator.wanted_ids = set(self._epg_wanted)

    def _init_device(self):
        """Register the device identity; keeps retrying with backoff."""
        engine = self.engine
        attempt = 0
        while self.resolver is not None and not self._ready:
            try:
                self.resolver.ensure_slot_session(0)
                self._ready = True
                self._last_error = ""
                self._device_ready_at = time.time()
                engine.set_resolver_ready(True)
                self.log("[ysp] 设备身份就绪, %d 路高码率频道已激活"
                         % len(engine.active_backend_channels))
                return
            except Exception as error:
                attempt += 1
                self._last_error = "%s: %s" % (type(error).__name__, str(error)[:160])
                engine.set_resolver_ready(False)
                if "HTTP 400" in str(error) and self.resolver is not None:
                    try:
                        self.resolver.rotate_device_on_400("init:" + str(error)[:60])
                    except Exception:
                        pass
                if attempt == 1:
                    self.log("[ysp] 设备注册失败 (%s), 高码率暂走 1080p 回退"
                             % self._last_error)
                time.sleep(min(15.0 * (1.5 ** min(attempt - 1, 4)), 60.0))

    def _warm_epg(self):
        aggregator = self.engine.epg_aggregator
        if aggregator is None:
            return
        try:
            started = time.time()
            ok = aggregator.refresh_now()
            self.log("[ysp] EPG 预热 %s (%.0fs)"
                     % ("完成" if ok else "失败", time.time() - started))
        except Exception as error:
            self.log("[ysp] EPG 预热异常: %s" % error)

    # --------------------------------------------------------- companion JS

    def _start_companion_engine(self):
        """Spawn the Node WASM engine (4th-layer fallback) if node exists."""
        node = shutil.which("node")
        if not node:
            self.log("[ysp] 未检测到 node, WASM 兜底引擎未启用 "
                     "(设备协议/1080p 仍可用)")
            return
        if not os.path.isfile(ENGINE_JS):
            self.log("[ysp] 缺少 engine.js, WASM 兜底引擎未启用")
            return
        env = dict(os.environ)
        env["PORT"] = str(self.engine_port)
        env["HOST"] = "127.0.0.1"
        env.setdefault("YSP_ENGINE_PORT", str(self.engine_port))
        try:
            self._engine_proc = subprocess.Popen(
                [node, ENGINE_JS],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as error:
            self._engine_proc = None
            self.log("[ysp] WASM 引擎启动失败: %s" % error)
            return
        threading.Thread(target=self._await_companion, daemon=True,
                         name="ysp-engine-boot").start()

    def _await_companion(self):
        if not _port_listening(self.engine_port):
            deadline = time.time() + ENGINE_BOOT_TIMEOUT
            while time.time() < deadline and not _port_listening(self.engine_port):
                if self._engine_proc is not None and self._engine_proc.poll() is not None:
                    self.log("[ysp] WASM 引擎进程已退出 (code=%s)"
                             % self._engine_proc.returncode)
                    self._engine_proc = None
                    return
                time.sleep(0.3)
        if _port_listening(self.engine_port):
            self.log("[ysp] WASM 兜底引擎就绪 (127.0.0.1:%d)" % self.engine_port)

    def stop(self):
        with self._lock:
            self.resolver = None
            if self.engine is not None:
                self.engine.resolver = None
                self.engine.set_resolver_ready(False)
            proc, self._engine_proc = self._engine_proc, None
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ------------------------------------------------------------- catalogue

    def catalog(self):
        """Return ``[{name, channel}]`` groups for the M3U builder."""
        groups = {}
        for meta in self.engine.global_channel_dir.unique_channels():
            group = groups.setdefault(meta["group"], [])
            slug = meta["slug"]
            group.append({
                "name": meta["name"],
                "tvgName": meta["name"],
                "tvgId": meta["tvg_id"],
                "logo": meta.get("logo", ""),
                "slug": slug,
                "catchup": slug in self.engine.TIMESHIFT_SUPPORTED,
            })
        return [{"name": name, "channel": channels}
                for name, channels in groups.items()]

    def slug_of(self, slug):
        engine = self.engine
        raw = engine.normalize_channel(slug)
        return engine.ALIAS_MAP.get(raw, raw)

    # -------------------------------------------------------------- playlist

    def playlist(self, slug, host_header, proto="http", catchup=None):
        """Four-layer fallback: device protocol -> 1080p -> Node WASM engine.

        ``catchup`` is an optional ``(start_ts, end_ts)`` pair for timeshift.
        Raises :class:`YspEngineError` when every layer fails.
        """
        engine = self.engine
        canonical = self.slug_of(slug)
        channel = engine.get_channel_instance(canonical)
        if channel is None:
            raise YspEngineError("未知频道: %s" % slug)

        # Layer 0 -- 7-day catchup (JCE timeshift).
        if catchup:
            start_ts, end_ts = catchup
            try:
                url = engine.jce_timeshift_url(
                    channel.pid, channel.sid, start_ts, end_ts, channel.defn)
                return engine.fetch_abs_playlist(url)
            except Exception as error:
                self.log("[ysp] %s 时移获取失败: %s, 回退直播"
                         % (canonical, str(error)[:80]))

        # Layer 1 -- device protocol (real 4K/8K, signed playback headers).
        if (canonical in engine.active_backend_channels and engine.resolver_ready()
                and self.resolver is not None
                and not self.resolver.is_channel_in_cooldown(canonical)):
            try:
                body = self.resolver.fetch_channel_playlist(
                    canonical, host_header, proto)
                if body and "#EXTM3U" in body:
                    return body
            except Exception as error:
                self._note_channel_failure(canonical, error)
                self.log("[ysp] %s 高码率受限 (%s), 平滑回退 1080p"
                         % (canonical, str(error)[:100]))

        # Layer 2 -- 1080p JCE / bkliveinfo.
        now = time.time()
        with channel.lock:
            cached = channel.last_built_playlist
            cached_age = now - channel.last_built_time
        if cached and cached_age < 15.0 and "#EXTM3U" in cached:
            return cached
        engine.ensure_channel(channel)
        body = engine.build_playlist(channel, holdback=True)
        if body and "#EXTM3U" in body:
            return body

        # Layer 3 -- Node WASM companion engine.
        body = engine.fetch_upstream_engine_playlist(
            canonical, host_header, proto)
        if body and "#EXTM3U" in body:
            return body

        raise YspEngineError("频道 %s 暂无数据 (%s)"
                             % (channel.name, channel.last_error or "拉取中"))

    def _note_channel_failure(self, channel, error):
        if self.resolver is None:
            return
        with self.resolver.state_lock:
            previous = self.resolver.state.channel_failures.get(channel)
            count = (previous[2] + 1) if previous else 1
            self.resolver.state.channel_failures[channel] = (
                time.time(), str(error), count)
        cooldown = 15 if count <= 1 else (60 if count == 2 else 120)
        self.log("[ysp] %s 进入冷却 %ds" % (channel, cooldown))

    # ------------------------------------------------------------- TS relay

    def decode_ts_token(self, path_or_query):
        """Turn a ``/proxy.ts?=token`` (or ``/proxy?ts=token``) into a real URL."""
        token = self.engine.proxy_ts_token(path_or_query)
        if not token:
            raise YspEngineError("missing proxy ts token")
        real_url = self.engine.decode_proxy_ts_url(token)
        if not _ts_host_allowed(real_url):
            raise YspEngineError("forbidden ts host")
        return real_url

    def playback_headers(self, real_url):
        if self.resolver is None:
            return {}
        try:
            return self.resolver.playback_headers_for_ts_url(real_url)
        except Exception:
            return {}

    def ts_cache(self):
        return getattr(self.engine, "global_ts_cache", None)

    # ------------------------------------------------------------------ EPG

    def epg_xml(self, wait_seconds=8.0):
        """Return aggregated XMLTV.

        A cold pull of both upstream feeds takes ~2.5 minutes, so the first
        request must not block on it: we wait briefly for the background warm
        thread, then fall back to a well-formed (empty) XMLTV rather than
        holding the connection. Players treat that as "no guide yet" and keep
        playing.
        """
        aggregator = self.engine.epg_aggregator
        if aggregator is None:
            raise YspEngineError("EPG aggregator not available")
        xml = aggregator.get_xml()
        if xml is None:
            deadline = time.time() + max(0.0, wait_seconds)
            while time.time() < deadline:
                time.sleep(0.5)
                xml = aggregator.get_xml()
                if xml is not None:
                    break
        if xml is None:
            return _EMPTY_EPG
        return xml

    def epg_status(self):
        aggregator = self.engine.epg_aggregator
        if aggregator is None:
            return {"available": False}
        last = aggregator.last_refresh_time()
        return {
            "available": True,
            "channels": len(aggregator.wanted_ids),
            "age_seconds": int(time.time() - last) if last else None,
            "last_error": aggregator.last_error(),
        }

    # -------------------------------------------------------------- status

    def device_ready(self):
        return self._ready

    def health(self):
        engine = self.engine
        resolver = self.resolver
        session = resolver.state.app_session if resolver is not None else None
        return {
            "device_ready": self._ready,
            "device_error": self._last_error,
            "device_ready_age": int(time.time() - self._device_ready_at)
                               if self._device_ready_at else None,
            "session": {
                "valid": bool(session and resolver.app_session_fresh(session)),
                "guid": session.cloud_guid if session else "",
                "version": session.version if session else "",
                "heartbeat_count": session.heartbeat_count if session else 0,
                "last_heartbeat": session.last_heartbeat_result if session else "",
            },
            "high_bitrate_channels": sorted(engine.active_backend_channels),
            "engine_js": _port_listening(self.engine_port),
            "engine_js_port": self.engine_port,
            "identity_resets": resolver.state.identity_reset_count
                               if resolver is not None else 0,
            "last_error": resolver.state.last_error if resolver is not None else "",
        }

    def diag_lines(self):
        engine = self.engine
        info = [
            "====================================================",
            "ysp-live v9.5 引擎 (vendor: waastudios/ysptp-docker)",
        ]
        device = self.engine.device_state_summary() \
            if hasattr(engine, "device_state_summary") else None
        if device is None and self.resolver is not None:
            device = self.resolver.status_json().get("device_state")
        if device:
            info.append("设备: %s %s (%s)" % (
                device.get("brand", "?"), device.get("model", "?"),
                "已注册" if device.get("cloud_guid_present") else "未注册"))
        info.append("高码率频道: %d 路" % len(engine.active_backend_channels))
        info.append("设备就绪: %s%s" % (
            "是" if self._ready else "否",
            " (%s)" % self._last_error if self._last_error else ""))
        info.append("WASM 兜底引擎: %s"
                    % ("就绪" if _port_listening(self.engine_port) else "未启用"))
        epg = self.epg_status()
        if epg.get("available"):
            age = epg["age_seconds"]
            info.append("EPG: %d 个频道, %s" % (
                epg["channels"],
                "%d 秒前刷新" % age if age is not None else "预热中"))
            if epg.get("last_error"):
                info.append("EPG 上次错误: %s" % epg["last_error"])
        else:
            info.append("EPG: 未启用")
        return "\n".join(info) + "\n"


def _ts_host_allowed(real_url):
    """True only for the CCTV/CDN families the engine can actually sign for."""
    try:
        host = (urllib.parse.urlsplit(real_url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == suffix[1:] or host.endswith(suffix)
               for suffix in TS_RELAY_ALLOWED_SUFFIXES)


def _port_listening(port, host="127.0.0.1"):
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            return sock.connect_ex((host, port)) == 0
    except Exception:
        return False