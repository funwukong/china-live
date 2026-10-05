# China Live

聚合央视频（APTV 源）与广东广电两个直播源，以 Docker 方式对外提供统一的 M3U 订阅。服务监听 **8577** 端口。

## 包含的源

- 央视频（`aptv`，`live/cctv.py`，由 APTV 源码改造）：央视频道 + 卫视频道 + CGTN + 4K + 付费剧场，约 63 路
- 广东广电（`gdtv`，`live/gdtv.py`）：广东本地频道，约 18 路

`live/cctv.py` 即 APTV.py 源码，自包含纯标准库，内置 JCE 时移 + bkliveinfo(cKey) 双协议与本地 HLS 分片中继（`127.0.0.1:19876`）。由于中继地址只在本机有效，`server.py` 会反向代理并把 playlist 里的分片地址改写成本服务的地址。`app/base/` 用标准库 + requests / websocket-client 实现了 TVBox 的 `base.spider` / `base.net` 接口，供广东广电源在播放器之外运行。

## 接口

- `/`：首页，列出各源订阅链接
- `/all.m3u`：聚合订阅，一次导入全部频道（约 81 路）
- `/aptv.m3u`、`/gdtv.m3u`：单源订阅
- `/aptv/<频道>.m3u8`：央视频 HLS 清单（反向代理本地中继，已改写分片地址）
- `/aptv/chunk/<频道>/<序号>.ts`：央视频视频分片
- `/proxy?sp=gdtv&id=<频道>`：广东广电播放地址（302 跳转到 CDN 的 m3u8）
- `/health`：健康探针，容器健康检查用
- `/diag`：诊断，列出每个源的频道数与状态
- `/sources`：JSON 列出已加载的源

广东广电源只下发 m3u8 清单，播放器直连其 CDN；央视频源因中继在服务本机，分片会经本服务转发。

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
- 频道清单每 10 分钟刷新；央视频分片由 APTV 后台线程每 2 秒刷新。
- 部分频道（如 CCTV5）受地区版权限制可能返回空，这是上游限制，非服务问题。
- 镜像基于 `python:3.12-slim`；`live/cctv.py`（APTV）为纯标准库，仅广东广电源需要 `requests` / `websocket-client`。
- 央视频源的中继端口从 19876 起在容器内自动选择，不对外暴露，只经 8577 反向代理。
