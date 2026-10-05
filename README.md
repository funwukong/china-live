# China Live

聚合央视官网、央视频、广东广电三个直播源，以 Docker 方式对外提供统一的 M3U 订阅。服务监听 **8577** 端口。

## 包含的源

- 央视频（`ysp`，`live/yangshipin.py`）：央视频道 + 卫视频道，约 56 路
- 央视官网（`cctv`，`live/cctv.py`）：CCTV-1 ~ CCTV-17 及海外版，约 20 路
- 广东广电（`gdtv`，`live/gdtv.py`）：广东本地频道，约 18 路

各源的 spider 来自 `China/live/` 仓库，原样保留；`app/base/` 用标准库 + requests / websocket-client 实现了 TVBox 的 `base.spider` / `base.net` 接口，使这些 spider 可以在播放器之外独立运行。

## 接口

- `/`：首页，列出各源订阅链接
- `/all.m3u`：聚合订阅，一次导入全部频道（约 94 路）
- `/ysp.m3u`、`/cctv.m3u`、`/gdtv.m3u`：单源订阅
- `/proxy?sp=<源>&...`：频道实际播放地址（302 跳转到 CDN 的 m3u8，EPG 接口返回 JSON）
- `/health`：健康探针，容器健康检查用
- `/diag`：诊断，列出每个源的频道数与状态
- `/sources`：JSON 列出已加载的源

播放器直连各电视台 CDN 拉视频分片，本服务只下发 m3u8 清单，不跑视频流量。

## 部署

### Docker Compose（推荐）

```bash
cd china-live
docker compose up -d --build
```

访问 http://localhost:8577/ ，聚合订阅为 http://localhost:8577/all.m3u 。

改宿主端口（例如 8080）：

```bash
PORT=8080 docker compose up -d
```

### docker run

```bash
docker build -t china-live .
docker run -d --name china-live --restart unless-stopped \
  -e TZ=Asia/Shanghai -p 8577:8577 china-live
```

### 直接运行（不装 Docker）

```bash
pip install -r requirements.txt
python app/server.py            # 默认端口 8577
PORT=8080 python app/server.py  # 自定义端口
```

## 说明

- 端口与监听地址由 `PORT` / `HOST` 环境变量控制，容器内默认 `0.0.0.0:8577`。
- 频道清单每 10 分钟刷新；播放地址按各 spider 自身的缓存策略刷新（`cctv`/`gdtv` 即时，`ysp` 60 秒）。
- 部分频道（如 CCTV5）受地区版权限制可能返回空，这是上游限制，非服务问题。
- 镜像基于 `python:3.12-slim`，依赖见 `requirements.txt`（requests、lxml、pycryptodome、websocket-client、pywasm==0.4.7）。
