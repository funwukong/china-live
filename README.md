# China Live

聚合多个直播源，以 Docker 方式对外提供统一的 M3U 订阅。服务监听 **8577** 端口。

## 包含的源

- **ysp**（`live/ysp/`，央视频 ysp-live v9.5 设备协议）：30 路央视/CGTN/4K/8K 频道，**真 4K/8K**、7 天回看、聚合 EPG
- **aptv**（`live/cctv.py`，纯标准库 JCE+bkliveinfo 回退）：63 路央视/卫视/付费剧场
- **gdtv**（`live/gdtv.py`）：广东广电约 18 路

一次导入 `/all.m3u` 共 111 路。

### ysp 源：ysp-live v9.5 设备协议

`live/ysp/` 是从 [waastudios/ysptp-docker](https://github.com/waastudios/ysptp-docker) 融合进来的央视频引擎（原为独立 8767 单端口服务，这里改为在 china-live 进程内以 provider 形式驱动，共用 8577 单端口）：

- `engine.py` — 央视频设备协议核心（设备注册、心跳、AES-GCM/RSA-OAEP 签名、真 4K/8K 直连源）
- `epg.py` — 上游 XMLTV 节目单聚合（epg.pw + zsdc 两源互补合并）
- `engine.js` — Node.js WebAssembly 伴生引擎（第 4 层兜底）
- `runtime.py` — china-live 适配层：把引擎包成 provider，暴露取流 / TS 中继 / EPG / 诊断

每个频道的取流走四层兜底：**真 4K/8K 设备协议 → 1080p JCE/bkliveinfo → Node WASM 引擎 → 报错**。设备身份与播放缓存持久化在 `/app/data`，容器重建不丢注册。

> `engine.py` 原样内置（仅改了一行 EPG 导入路径），便于后续同步上游修复。

## 接口

- `/`：首页，列出各源订阅链接
- `/all.m3u`：聚合订阅，一次导入全部频道（约 111 路）
- `/ysp.m3u`、`/aptv.m3u`、`/gdtv.m3u`：单源订阅
- `/epg.xml`：聚合节目单（ysp 源频道，每 6 小时刷新，启动后台预热）
- `/ysp/<频道>.m3u8`：央视频 HLS 清单（真 4K/8K 走本地 `/proxy.ts` 中继，1080p 直连央视 CDN）
- `/ysp/<频道>.m3u8?playseek=<起>-<止>`：7 天回看（如 `playseek=20261016000000-20261016010000`）
- `/aptv/<频道>.m3u8`：旧 APTV 直连清单
- `/proxy?sp=gdtv&id=<频道>`：广东广电播放地址（302 跳 CDN）
- `/proxy.ts?=...`：ysp 真 4K/8K 分片中继（带签名的播放头，仅放行央视/CDN 域名）
- `/health`：健康探针（JSON，含设备/会话/EPG 状态）
- `/diag`：诊断，各源频道数 + 引擎详情
- `/sources`：JSON 列出已加载的源

## 部署

### Docker Compose（推荐）

```bash
cd china-live
docker compose up -d --build
```

访问 http://localhost:8577/ ，聚合订阅为 http://localhost:8577/all.m3u ，节目单 http://localhost:8577/epg.xml 。

改宿主端口（例如 8080）：

```bash
PORT=8080 docker compose up -d
```

### docker run

```bash
docker build -t china-live .
docker run -d --name china-live --restart unless-stopped \
  -e TZ=Asia/Shanghai -p 8577:8577 -v ./data:/app/data china-live
```

### 直接运行（不装 Docker）

```bash
pip install -r requirements.txt
python app/server.py            # 默认端口 8577
PORT=8080 python app/server.py  # 自定义端口
```

> 直接运行时若装了 Node，`live/ysp/engine.js` 会自动作为兜底引擎在 `PORT+1` 拉起；未装 Node 也不影响，设备协议和 1080p 回退照常。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` / `HOST` | 8577 / 0.0.0.0 | 监听地址 |
| `CHINA_LIVE_DATA_DIR` | `/app/data` | 设备身份/缓存持久化目录 |
| `YSP_NO_DEVICE` | — | 设 1 关闭设备协议，仅 1080p |
| `YSP_ONLY_4K` | — | 设 1 仅 4K/8K 走设备协议 |
| `YSP_LINKS_PER_DEVICE` | 6 | 单设备高码率并发路数（0=不限制） |
| `YSP_ZAP_DEBOUNCE` | 10 | 换台后先出 1080p，停留 N 秒升码率 |
| `YSP_CACHE_MB` | 200 | `/proxy.ts` 内存分片缓存 |
| `YSP_NO_ENGINE` | — | 设 1 关闭 Node WASM 伴生引擎 |
| `YSP_ENGINE_PORT` | 0 | 伴生引擎端口（0 = `PORT+1`） |

## 说明

- 端口与监听地址由 `PORT` / `HOST` 控制；容器内默认 `0.0.0.0:8577`。
- 央视频设备协议会注册一台虚拟 Android TV 设备并保持心跳，设备身份写入 `/app/data/device-state-rs.json`；重建容器不丢。
- 换台防抖：切换频道后先下发 1080p 清单，停留约 10 秒后自动请求真 4K/8K 并切流，避免触发上游风控限流。
- 节目单首拉两个上游 XMLTV 需约 2.5 分钟，服务启动后台预热；预热完成前 `/epg.xml` 先返回空表（合法 XMLTV），不影响播放。
- 部分频道（如 CCTV5）受地区版权限制可能返回空，这是上游限制，非服务问题。
- 镜像基于 `python:3.12-slim` + `nodejs`（WASM 伴生引擎）；`live/cctv.py` 与 `live/ysp/engine.py` 为纯标准库，只有广东广电源需要 `requests` / `websocket-client`。
- `/proxy.ts` 目标主机有白名单（仅央视系/CDN 域名），避免被当作任意 URL 的开放代理（SSRF）。

## 上游与致谢

- 引擎来自 [waastudios/ysptp-docker](https://github.com/waastudios/ysptp-docker)（ysp-live v9.5）
- 频道算法源自 IPTV 总部；live_id 由 Gary's Club 社区提供

本项目仅供学习交流，请勿用于商业或非法用途。