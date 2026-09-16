# OBS 收流与语音转文字服务

接收 OBS 额外推送的 RTMP/RTMPS 流，保存分段录像，自动提取音频，调用公司 Qwen3 ASR 或飞书 ASR。直播电脑不用运行 Python，后端通过 Docker 部署。

目标服务器：`118.196.114.88`（Ubuntu）。此地址来自用户，**交付代码不等于已部署成功**。

## 1. 架构和已实现能力

```text
OBS 多路推流插件 ── RTMP/RTMPS ── MediaMTX
                                      │ 完整分段 + 完成标记（持久磁盘）
                                      ▼
                             FFmpeg 提取 16k 单声道 WAV
                                      │ 最多 45 秒一段
                                      ▼
                        SQLite 任务队列 → 公司 ASR / 飞书 ASR
                                      │
                                      ▼
                           查询 API / XML 逐字稿 / 飞书导出脚本
```

- 收流、API、转写分别运行；ASR 网络失败不会阻止 MediaMTX 保存录像。
- 支持多路同时推流：`STREAM_PATHS` 显式配置额外路径，保留原 `STREAM_PATH`（默认 `live/main`）。拒绝未授权发布者及同名流被第二发布者顶替。
- MediaMTX 默认约 30 秒分段；视频关键帧间隔会影响实际分段长度。转写前再切成最多 45 秒。
- 音频统一为 16 kHz、单声道、PCM16 WAV；调用飞书时取出裸 PCM，不发送 WAV 文件头。
- SQLite 保留提交 UUID、音频 URL、任务阶段、结果、错误和重试时间；请求超时重试使用相同公司任务 ID 和 URL。
- 公司提交的完整请求体也会保存，热词配置变化不改变正在重试的请求；轮询超时从提交开始计算。
- 下载音频必须有文件专属 HMAC 签名和有效期，API 查询需要 Bearer 密钥；不公开原始视频目录。
- 默认公司 ASR，切换仅影响新建任务，不会悄悄把同一录音发给另一家供应商。
- 本版是分段转写，延迟包含分段时长、ASR 排队和处理时间，不是毫秒级字幕。

## 2. Ubuntu 部署

服务器需有 Docker Engine 和 Compose 插件；不要求安装 Python。首次构建需要能够访问镜像仓库、Debian 仓库和 PyPI。

将此目录上传到服务器，例如 `/opt/live-asr`，然后执行：

```bash
cd /opt/live-asr
bash deploy.sh 118.196.114.88
```

脚本构建镜像、生成随机凭据 `.env`、启动三个服务。已有 `.env` 不会被覆盖。不自动改防火墙或占用 80/443。

端口：

| 端口 | 用途 |
|---|---|
| 1935/TCP | RTMP 推流（初始配置） |
| 1936/TCP | RTMPS 推流（启用证书后） |
| 8088/TCP | 管理 API、ASR 签名音频下载 |

首次局域网/受限网络联调可使用 RTMP/HTTP。公网正式运行应配置域名、HTTPS 与 RTMPS；明文协议不提供传输加密。公司的生产 ASR 地址按其文档使用 HTTP，若公司提供 HTTPS 地址应替换配置。不要把所有 `.env` 内容发给直播人员，只提供推流用凭据。

### HTTPS 和 RTMPS

1. 为服务器配置域名与公认 CA 签发的证书。
2. 在现有反向代理中将该域名的 HTTPS 请求转到 `127.0.0.1:8088`；保留 `/audio/` 路径和查询字符串，支持 Range 请求，不记录音频签名查询参数。
3. 将 `.env` 的 `PUBLIC_BASE_URL` 改为实际 `https://域名`，重建 API、worker 容器。
4. 放入 `certs/server.crt`（完整链）和 `certs/server.key`，设 `RTMPS_ENABLED=true`，重建 media。此模式仅允许加密推流。
5. 配置 OBS 为 `rtmps://域名:1936/live`。更新证书后需要重新加载 media，安排在非开播时操作。

## 3. OBS 配置

安装与 OBS 版本匹配的 Multiple RTMP Outputs 插件，保留淘宝／京东输出，另加一项：

- 服务地址：`rtmp://118.196.114.88:1935/live`
- 推流密钥：`main?user=obs&pass=这里替换为.env中的PUBLISH_PASSWORD`
- 音频：AAC；视频推荐 H.264。服务器也接收纯 AAC 音频 RTMP，但 OBS 插件是否能输出纯音频取决于实际版本配置。
- 若插件支持共享现有编码器，可复用；仍会增加一份上行带宽。
- 选择包含主播声音的音轨。后端只取收到的第一条音轨，不会自动分离混在一起的主播、音乐、现场噪声。

先短推 1 分钟，在后端检查分段和转写，并确认现有直播正常。RTMP 地址必须可从直播电脑连接。

### 多直播间同时推流（现有 9030 部署）

在服务器现有 `.env` 中保留所有凭据和 HTTPS/RTMPS 设置，只增加或修改：

```dotenv
STREAM_PATH=live/main
STREAM_PATHS=live/taobao,live/jingdong
```

路径按逗号分隔，允许英文字母、数字、下划线和连字符，格式为 `应用名/流名`；不接受通配符。未填写 `STREAM_PATHS` 的旧部署仍然只开放原路径。

每路 OBS 的服务器地址都填 `rtmps://tbjdasr.ai.lab.yc345.tv:9030/live`。推流密钥如下，密码仍取 `/root/tbjdasr-obs-info.txt` 中原来的值：

| 直播间 | 推流密钥 | 查询的 room |
|---|---|---|
| 原测试流 | `main?user=obs&pass=原密码` | `live/main` |
| 淘宝 | `taobao?user=obs&pass=原密码` | `live/taobao` |
| 京东 | `jingdong?user=obs&pass=原密码` | `live/jingdong` |

不同直播间必须用不同流名；两台 OBS 同时使用 `main` 时，后一台会被拒绝。同一个 OBS 推送两次相同节目不会变成两个不同音源，服务按接收到的各路内容独立处理。

更新现有交付分支后，在项目目录执行（会重启收流，请安排在测试或停播时）：

```bash
git pull --ff-only origin codex/obs-asr-backend
# 编辑现有 .env，添加上面的 STREAM_PATHS；不要重新生成密钥
docker compose build media
docker compose up -d --force-recreate media api worker
```

保留现场 `compose.override.yaml`：media 的 `9030:1936`、api 的 `127.0.0.1:8088:8088` 不用修改；对外继续使用推流 9030 和 HTTPS 443。`PUBLIC_BASE_URL` 应继续为 HTTPS 域名，而非 RTMPS 地址。

`GET /v1/rooms` 返回各路任务状态计数，`configured` 仅表示服务配置允许此路径，不代表正在推流。按 `room` 查询任务、导出 XML 或调用飞书导出脚本即可分别查看各直播间；移除配置路径不会删除其历史任务。

录像按 `data/recordings/应用名/流名/` 分目录；音频和 ASR 请求 ID 独立。队列在各路之间轮流处理，临时失败只重试对应片段。当前仍为单 worker 顺序执行 HTTP 请求，公司异步 ASR 可同时有多段远端任务处理；吞吐受 ASR 限额、磁盘和带宽限制，不保证无限路数或实时转写。

## 4. 配置两种 ASR

### 公司 Qwen3 ASR（默认）

```dotenv
ASR_PROVIDER=company
COMPANY_ASR_URL=http://101.126.77.69:8080
COMPANY_ASR_HOST=qwen3-dual-asr.ai
COMPANY_ASR_UID=live-audio
PUBLIC_BASE_URL=http://118.196.114.88:8088
```

按公司文档使用 `POST /asr/v1/qwen3/submit`、`query`、`result`，每段独立 UUID，携带指定 Host。采用轮询，不依赖 callback 白名单。

**生产 ASR 必须能访问 PUBLIC_BASE_URL。** 它不能下载解析到私网的音频地址，因此不要填写 `localhost`、Docker 服务名或内网 IP。签名链接默认 7 天，提交时持久化；网络重试不更换 URL，避免同一 UUID 请求内容不同。

公司文档的内网测试环境可设 `COMPANY_ASR_URL=http://10.8.8.128:8001`、`COMPANY_ASR_HOST=`，是否在线由算法团队确认。

原始结果的 `result.utterances` 原样保存在任务 `raw` 字段。文档没有定义 utterances 每个字段的单位，因此本服务不猜测或伪造逐词时间戳；统一输出音频片段级时间。

### 飞书开发平台 ASR

```dotenv
ASR_PROVIDER=feishu
FEISHU_APP_ID=填写应用ID
FEISHU_APP_SECRET=通过安全方式填写应用Secret
```

应用需开通 `speech_to_text:speech`，并确认租户套餐支持 ASR。官方文档注明免费版不支持。服务用应用的 tenant_access_token 调用 `speech_to_text/v1/speech/file_recognize`，自动缓存并更新 token。这是识别服务身份，和创建文档的用户身份不同。

配置更新后：

```bash
docker compose up -d --force-recreate api worker
```

当前只运行一个 ASR worker，串行调度每批任务。长时间直播或多路扩展应先观测积压；不要直接增加 worker 副本，本版通过文件锁防止重复消费。

切换公司的生产／测试服务地址之前，应先处理完原服务的未完成任务，避免把旧 UUID 查询发送到另一套环境。

## 5. API

除 `/healthz` 和独立签名的 `/audio/...` 外，均要求 `Authorization: Bearer API_KEY`。密钥存放 `.env`，不要写进 URL。

| 接口 | 用途 |
|---|---|
| `GET /healthz` | API 进程存活，不代表 ASR 或推流在线 |
| `GET /v1/status` | 任务统计、worker 心跳、磁盘低于 5 GiB 提示 |
| `GET /v1/rooms` | 各路配置标记和独立任务状态计数（不代表在线状态） |
| `GET /v1/jobs?room=live/main&limit=100&offset=0` | 分页查询任务和文字 |
| `GET /v1/jobs/{id}` | 原始 ASR 结果、错误、阶段 |
| `POST /v1/jobs/{id}/retry` | 失败任务继续原阶段，保留远端任务 ID |
| `POST /v1/jobs/{id}/retry?new_remote_task=true` | 新远端任务 ID 和签名链接重新提交；用于原任务失败／不存在／URL过期 |
| `GET /v1/assets` | 原始分段提取状态与错误，最近 1000 条 |
| `POST /v1/assets/{id}/retry` | 重试失败的音频提取 |
| `GET /v1/transcript.xml?room=live/main&since=...&until=...` | 结构化逐字稿，时间参数为 UTC Unix 秒 |

自动重试临时网络错误、429 和部分 5xx，指数退避最多 300 秒；连续 8 次失败进入 failed，保留音频。公司排队／处理中每 5 秒查询，最多等待一天。不会把没有成功识别的片段写成正常逐字稿。

API XML 的 `<document>` 是传输根节点。实际写入飞书时导出脚本使用其内部 DocxXML，不传这个包装节点。

## 6. 飞书文档导出

`export_feishu.py` 可在已有 `lark-cli` 用户授权的管理电脑上执行。它按选择的时间范围生成快照，用 `--as user` 创建文档，固定放到用户指定文件夹：

`https://guanghe.feishu.cn/drive/folder/ZvZ0fN9YdlYt26dGCMDcDjo3nMc`

该脚本需要管理端 Python/httpx 和 lark-cli，**直播电脑不需要安装这些组件**。Docker 收流服务不包含 lark-cli，文档上传不是默认后台自动任务。

```bash
# 在已配置环境变量 API_KEY 的管理电脑运行，时间填写实际直播范围
python export_feishu.py --url https://你的域名 --room live/main --since 1789459200 --until 1789462800
```

脚本保存导出回执；相同快照再次导出返回已有文档地址。不确定的写入结果会停止并提示检查，避免盲目重试重复追加。未实现自动摘要，导出的是逐字稿和片段状态。

## 7. 数据、重启与故障恢复

`data/recordings/` 保存原始 fMP4 和 `.ready.json`；`data/audio/` 保存分段 WAV；`data/state.sqlite` 保存队列和结果。

默认不删除音视频，避免 ASR 长时间故障后无法补跑；运营需要安排容量和保留策略。6 Mbps 视频约 2.7 GB/小时，另加音频和文件开销。状态接口提示磁盘不足，但不会自动停止直播或删除历史文件。

正常断流会关闭最后的分段并生成完成标记；OBS 重连后生成新分段。后台通过仅在容器网络开放的 MediaMTX 管理接口核对在线状态，补回遗漏的完成标记；正在录制的最新文件不会被提前转写。`MEDIA_API_PASSWORD` 由初始化脚本生成，不要对公网映射 9997 端口。若管理接口不可用，补扫描暂停，已有标记仍正常处理。

异常断电后也可在停止收流时手动恢复：

```bash
docker compose stop media
docker compose run --rm worker python recover_recordings.py
docker compose start media
```

恢复脚本拒绝在 media 进程持锁时运行，只标记可探测到音频的已有文件，不修改原始文件；最末未落盘的数据仍可能丢失。独立 worker 正常重启会继续处理持久任务，不依赖内存队列。

## 8. 验证与边界

测试：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
MEDIAMTX_BIN=/path/to/mediamtx python -m pytest tests/test_rtmp_integration.py -q
```

已覆盖真实 RTMP（音频流及 H.264/AAC 流）、推流鉴权、最后一段保存、FFmpeg 提取、PCM 格式和切片、两家 ASR 协议的模拟 HTTP 测试、签名音频下载、任务幂等和持久恢复。

多路测试还覆盖两路同时推送不同频率音频、各路完整时长和内容隔离、未配置流名拒绝、跨直播间队列轮转、分路查询和文档隔离。开发机未运行 Docker daemon，因此本机验证使用原生 MediaMTX 和 FFmpeg，不代表已完成线上部署。

真实 ASR 识别成功与目标 Ubuntu 部署需要在现场完成联调，不用模拟结果冒充识别结果。公司生产 `/health` 首次出现 HTTP 502，随后重新请求返回 HTTP 200 和 `{"status":"ok"}`。飞书真实识别需配置应用凭据和权限。

参考：[公司 ASR 说明](https://guanghe.feishu.cn/docx/KKXPdK9b3oRdy9xnYpBcIcSxnRf)、[飞书文件 ASR](https://open.feishu.cn/document/server-docs/ai/speech_to_text-v1/file_recognize)、[MediaMTX 录制](https://mediamtx.org/docs/features/record)、[OBS 多路推流插件](https://github.com/sorayuki/obs-multi-rtmp)。
