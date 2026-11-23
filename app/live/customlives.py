"""External M3U live-source spider (lytvs.top custom_lives.m3u).

Fetches a remote M3U playlist, keeps only 1080p entries (``[1080P]`` in the
display name) and exposes them as a standalone ``live`` provider.  Direct URLs
are returned as-is; no proxy relay is required.
"""

import json
import re
import time
import urllib.parse

from base.spider import Spider as BaseSpider


M3U_URL = "https://lytvs.top/py/custom_lives.m3u"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 15
CATALOG_CACHE_SECONDS = 600
QUALITY_TAG = re.compile(r"\[(\d{3,4})P\]", re.I)


class Spider(BaseSpider):

    def init(self, extend=""):
        self.source_url = extend or M3U_URL
        self._cache = None
        self._cache_at = 0.0

    def getName(self):
        return "自訂聚合源 (1080P)"

    def liveContent(self, url):
        now = time.time()
        if self._cache is None or now - self._cache_at > CATALOG_CACHE_SECONDS:
            self._cache = self._parse_m3u(self.source_url)
            self._cache_at = now
        return self._cache

    def localProxy(self, param):
        return [501, "text/plain", "custom_lives: direct playback only"]

    def destroy(self):
        self._cache = None

    def _parse_m3u(self, url):
        result = self.net.req(url, {
            "method": "GET",
            "headers": {"User-Agent": USER_AGENT},
            "timeout": REQUEST_TIMEOUT * 1000,
            "buffer": 3,
        })
        if result.get("error"):
            raise ValueError("custom_lives fetch failed: %s" % result["error"])
        if not 200 <= int(result.get("code") or 0) < 300:
            raise ValueError("custom_lives HTTP %s" % result.get("code"))
        content = result.get("content")
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        lines = content.splitlines()
        groups = {}
        current = None
        number = 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF"):
                current = self._parse_extinf(line)
                continue
            if line.startswith("#"):
                continue
            if current is None:
                continue
            name, group, tvg_name, logo = current
            current = None
            quality = self._quality(name)
            if quality != 1080:
                continue
            number += 1
            bucket = groups.setdefault(group, [])
            bucket.append({
                "name": name,
                "tvgName": tvg_name or name,
                "tvgId": tvg_name or name,
                "number": "{:02d}".format(number),
                "logo": logo or "",
                "urls": [line],
                "parse": 0,
                "format": "application/x-mpegURL",
                "ua": USER_AGENT,
            })
        if not groups:
            raise ValueError("custom_lives: no 1080p channels")
        ordered = []
        for group in sorted(groups):
            ordered.append({"name": group, "channel": groups[group]})
        all_channels = [c for g in ordered for c in g["channel"]]
        return [{"name": "全部頻道", "channel": all_channels}] + ordered

    @staticmethod
    def _parse_extinf(line):
        attrs = dict(re.findall(r'(\S+?)="([^"]*)"', line))
        title = line.split(",", 1)[1].strip() if "," in line else attrs.get("tvg-name", "")
        name = attrs.get("tvg-name") or title
        group = attrs.get("group-title") or "未分組"
        return name, group, title, attrs.get("tvg-logo", "")

    @staticmethod
    def _quality(name):
        match = QUALITY_TAG.search(name or "")
        return int(match.group(1)) if match else 0
