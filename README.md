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
| `GET /v1/transcript.txt?room=live/main&since=...&until=...` | UTF-8 文本逐字稿，支持按直播间和时间范围读取 |
| `GET /v1/transcript.xml?room=live/main&since=...&until=...` | 结构化逐字稿，时间参数为 UTC Unix 秒 |

自动重试临时网络错误、429 和部分 5xx，指数退避最多 300 秒；连续 8 次失败进入 failed，保留音频。公司排队／处理中每 5 秒查询，最多等待一天。不会把没有成功识别的片段写成正常逐字稿。

API XML 的 `<document>` 是传输根节点。实际写入飞书时导出脚本使用其内部 DocxXML，不传这个包装节点。

### 直播过程中自动产出文字

当前部署采用每小时归档：`RECORD_SEGMENT_DURATION=1h`、`REALTIME_ASR_ENABLED=false`。MediaMTX 关闭上一段后继续录下一段，独立 worker 提取音频并转写上一小时；下播不足一小时的尾段也会自动处理。小时录音内部继续按 ASR 可接受的短音频分段请求，不把整小时直接提交给短音频接口。转写和飞书写入需要额外时间，不承诺满一小时立即全部出字。需要短周期时可设置 `RECORD_SEGMENT_DURATION=30s`；视频关键帧可能让切段略晚。

worker 自动把已入库片段汇总到：

```text
data/transcripts/live/main.txt
data/transcripts/live/taobao.txt
data/transcripts/live/jingdong.txt
```

每 2 秒核对一次，按开始时间排列，原子替换文件；排队、失败片段明确标注状态。识别重试成功后替换对应状态，不重复追加文字。数据库为事实来源，重启后可以重建文件。各路文件包含该路全部历史片段，不按每场直播自动拆文档；有时间范围需求用查询接口或飞书导出脚本。没有片段的直播间暂不生成文件。

通过需要 Bearer API_KEY 的 `GET /v1/transcript.txt?room=live/taobao` 读取当前文字，`since` / `until` 可选择直播时间范围；API 每次最多 10000 段，超过时要求缩小范围，自动落盘文件没有这个截断限制。`GET /v1/status` 的 `documents_unhealthy` 与 `worker.documents` 用于检查自动文件输出是否健康。

正常停止推流时，最后不足 30 秒的片段也会关闭并送去识别；等待队列处理完成再导出最终文档。异常断电仍可能丢失尚未落盘的媒体数据。此功能自动生成服务器 TXT，飞书在线文档继续使用下面的用户身份导出脚本，不会擅自使用应用身份创建文档。

更新代码后需重建镜像并重建 worker/API；沿用现场 `.env`、证书和 `compose.override.yaml`。已存在的 worker 不会仅因 Git 拉取而加载新代码。

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

持续转写回归包括真实双路 RTMP 在推流结束前出现 TXT，以及尾段最终进入文档；ASR 返回在测试中明确模拟，不等于已通过公司生产识别联调。

## 9. 按场次同步快照到飞书文档和多维表格

管理端新增 `session_export.py`：为指定直播间和时间范围创建一篇用户身份的飞书文档，存入固定文件夹 `ZvZ0fN9YdlYt26dGCMDcDjo3nMc`，文档完成后才在目标多维表格创建场次记录。逐字稿不写入多维表格。

目标数据表需具备以下字段，写入前脚本会校验实际结构：

| 字段 | 类型 |
|---|---|
| 场次编号 | 文本 |
| 场次名称 | 文本 |
| 直播间 | 文本 |
| 开始时间 | 日期时间（Asia/Shanghai） |
| 结束时间 | 日期时间（Asia/Shanghai） |
| 转写状态 | 文本 |
| 转写文档 | 文本／超链接样式 |

在已授权 `lark-cli` 用户身份的管理端设置环境变量 `API_KEY`，执行：

```bash
python session_export.py \
  --url https://tbjdasr.ai.lab.yc345.tv \
  --room live/taobao \
  --session-id taobao-20260917-001 \
  --title '淘宝直播 2026-09-17 第1场' \
  --since 1789610400 --until 1789614000 \
  --base-url '飞书多维表格的完整链接' --table-id '目标数据表ID'
```

以上场次编号和时间为用法示例，需要替换为真实场次范围，时间使用 UTC Unix 秒。目标链接通过 `base +url-resolve` 解析；如果链接中包含目标表，`--table-id` 可省略。字段结构不匹配时停止，不会擅自修改现有表。

场次边界目前由参数明确指定，不根据 ASR 完成时间推断开播停播，也尚未自动按断线时长合并场次。导出前确认该时间范围录音已完成提取；所有查到的片段均成功转写才允许同步快照。录音缺失或仍在提取的片段不在任务列表中，不能仅凭一次导出认定源音频覆盖完整。

回执保存在 `generated/feishu-sessions/`，包含文档 ID、表格记录 ID 和内容摘要。保留此目录，单一管理端负责同步快照。相同场次、目标和内容重跑复用已有结果；内容变化时停止要求核对，不自动覆盖人工编辑。文档追加或记录创建结果不确定时停止，不会盲目重试产生重复内容；须核对远端结果和对应回执再恢复。

该入口未接入自动调度，Docker worker 不包含 lark-cli，也不会复制管理端的用户凭据。多维表格目标确认后需要实际联调 CLI 响应结构和用户权限。

多维表格状态为“已同步快照”，不会声称录音无缺失或整场已完成。晚到片段需要核对原文档后进行明确更新；当前脚本不自动覆盖已有快照。时间范围尚未结束时拒绝导出。首次建文档前查询远端场次编号，发现已存在时停止，避免因本地回执缺失而重复创建。

## 10. 按值班表归档到飞书

目标表：[直播场次与转写](https://guanghe.feishu.cn/base/Dj7lbtbChamBA7sLbtAczWYMntc?table=tblcnhzKQ6Ivv9VS)。
转写文档：[指定目录](https://guanghe.feishu.cn/drive/folder/Lwiof1pEglCsKgdpAU3caUUpnfh)。

`feishu_sessions.json` 已填写资源位置。当前整体排班位于「7月整体排班」工作表（不是历史「天猫」页）。
已确认淘宝、京东、天猫是同一场直播，统一使用「天猫」排班。当前只用 `live/taobao` 作为归档音频来源，每个班次生成一篇文档和一条记录。
`live/jingdong` 不重复归档；`live/main` 用途尚未明确，也不纳入正式场次。其他推流仍可正常收录，原始数据不删除。
`room_groups` 的键是归档来源推流路径，值是排班组（天猫、初中、高中、小学）。同一场直播只配置一路来源；当前不做自动切换或混合多路音频，所选来源转写失败时暂缓归档。

在已安装并授权 `lark-cli` **用户身份**的机器运行（Python 后端运行环境；无需在直播 Windows 安装 Python）：

```bash
python scheduled_export.py --date 2026-09-17             # 只预览
# API_KEY 通过环境变量提供，不写进 Git 或命令行参数
python scheduled_export.py --date 2026-09-17 --apply     # 写入已结束且全部转写成功的场次
```

一条推流的一个排班时段对应一篇文档和一条场次记录。人员保留排班原文，不是实际声纹识别。
排班起止与实际采集片段起止分别保存；同一跨班片段出现在相邻场次，文档明确提示交班边界。
失败/未完成的 ASR 或没有音频时不会创建空文档。已有场次使用回执防重复；不确定写入要先核对云端，勿删回执强制重试。
这是按日执行的快照归档命令。需要持续增量同步时，使用下文的常驻服务，并完成服务器上的用户授权。
排班更改不会静默覆盖已归档文档。原始逐字稿、调班备注及源单元格保留以便核对。

### 飞书持续转写的超时与重复流处理

线上实际直播约 33 秒片段曾持续返回飞书 HTTP 504；相同录音取 10/15 秒实测成功。
飞书适配器现在每次发送最多 15 秒 PCM，保持全部样本顺序，全部子请求成功后才保存该片段全文。
子请求失败时保持任务重试语义，不把部分文字标成整段成功。30 秒收流切片配置不需要改变。

同一场直播有重复推流时，在服务器 `.env` 设置 `ASR_STREAM_PATHS=live/taobao`。
它必须是已配置收流路径的子集；留空仍转写所有路径。未选路径继续收流保存录音，已有任务保留但不调度；
尚未提取的录音标为 ignored，未来重新纳入选择后可继续提取。状态接口显示 `asr_stream_paths`。
切换此配置后仅重建 api/worker；无需重启正在收流的 media。

## 11. 常驻增量同步到飞书

`feishu_sync_service.py` 每轮读取已提取的音频任务，每轮结束等待 2 秒，排班每 5 分钟刷新。仅处理 `feishu_sessions.json` 映射的 `live/taobao → 天猫`；每个排班场次创建一个文档和一条多维表格记录，并持续追加成功转写的片段。文档归属已授权用户，存入配置的转写文件夹。

服务器安装：

```sh
npm install -g @larksuite/cli@1.0.65
python3 -m venv .sync-venv
.sync-venv/bin/pip install httpx==0.28.1
# 配置名为 livestream 的 CLI profile，并完成用户 OAuth 授权。
# 应用密钥用 --app-secret-stdin 输入，不写在命令参数或日志里。
cp deploy/tbjdasr-feishu-sync.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tbjdasr-feishu-sync
systemctl status tbjdasr-feishu-sync
```

部署目录不是 `/data/vonjan/program/tbjdasr.ailab` 时，先修改 unit 的 WorkingDirectory 和 ExecStart。CLI 负责刷新用户令牌；用户撤销授权或刷新令牌失效后需重新授权，不能改用 bot 绕过。

`data/feishu-sync/health.json` 保存每轮同步状态，`journalctl -u tbjdasr-feishu-sync` 查看日志。`data/feishu-sync/sessions/` 是持久化回执，必须备份并随部署保留，不能清空后重建。仅启动一个同步实例。启动失败或授权失效不影响录音与 ASR，但飞书同步会停滞并在 health 中报错。

片段按时间追加，未完成片段不阻塞后续成功文字，晚到结果标为补录；失败片段不阻塞后续成功文字，表格标记“有失败片段”。排班移除旧日期后仍保留已创建场次并补同步迟到结果。晚到的早期片段标为“补录片段”。已结束场次只标记“已同步现有录音”，不声称采集完整。排班人员或已同步文字变化、创建结果不确定时停止该场次并要求核对；追加响应丢失后读取文档确认内容，禁止盲目重复追加。

旧的 `scheduled_export.py` 是一次性已结束场次快照工具；不要与常驻服务同时归档同一场次。可用 `--once` 运行一轮，进行部署验收。

## 12. 飞书实时识别并写入 TXT / 飞书文档（stream_recognize）

启用 `REALTIME_ASR_ENABLED=true` 与 `REALTIME_ARCHIVE_ENABLED=true`（默认）后，实时进程追读 MediaMTX 正在写入的 fMP4 录音，转换成 16 kHz 单声道 PCM16，归档模式将音频聚合为每包 1 秒调用 `stream_recognize`，请求至少间隔 1.05 秒，避免批量落盘和过多小包导致限流（官方建议 100–200ms，本部署实测 1 秒包可用，用延迟换取较低请求频率）；每 5 秒音频请求一次最终结果，成功后立即进入正式任务库；TXT 自动更新，飞书常驻服务每 2 秒检查并追加到对应场次文档。新场次需要创建文档，实际延迟包含录音落盘、识别、网络及飞书写入时间，不保证固定秒数。

实时与关闭录音后的补转写使用同一音频源、相同任务编号和固定切片边界，避免双份文字。实时最终请求前先保存 WAV；失败或崩溃超时后，文件 ASR 接管同一任务。中间修订文字不写入正式文档。晚到成功片段标为补录，不阻塞后续实时文字。已有历史录音的切片边界保持不变。

前提：现有 Feishu 应用开通 `speech_to_text:speech`，租户版本支持流式 ASR（免费版不支持）；租户上限 20 路，需要为其他业务留余量。沿用 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 获取 tenant token，不需要新增文档用户 OAuth 权限。

```sh
# .env 设置 REALTIME_ASR_ENABLED=true；REALTIME_ARCHIVE_ENABLED=true；ASR_STREAM_PATHS=live/taobao
# 构建共享镜像后，使 media 开启内部 RTSP，api/realtime 载入新配置。
# media 重建会短暂断流，应在停播时操作。
docker compose build media
docker compose up -d media api worker realtime
```

RTSP 的 8554 端口仅在 Docker 网络内使用，不需要在安全组开放。worker 用户沿用 `MEDIA_API_PASSWORD`，仅获选定直播间的读取权限。现有 RTMPS 推流地址和推流密钥不变。

实时入口（沿用 API_KEY 鉴权）：

```sh
curl -H "Authorization: Bearer $API_KEY" \
  'https://tbjdasr.ai.lab.yc345.tv/v1/realtime?room=live/taobao&limit=20'
```

客户端每秒查询一次，以 `segments[].id` 为键替换该段文字；`final=false` 表示可能修订，`error` 表示该段识别中断。`stale=true`、`source.status`、`worker.ok` 用于显示断流、出错或无输入，不能把旧文字当作当前直播。返回最近最多 limit 段（按时间正序），`since` 可筛选开始时间。实时状态保留 24 小时；正式任务和文档不随预览清理。归档模式 start 来自录音文件时间加音频偏移；仅预览模式是接收端时间，均不是逐词时间戳。

**验收方式：** 持续推一段已知语音，直接查看 TXT 和对应飞书文档是否在停播前出现文字；检查最终任务 `raw.source=stream_recognize`，确认没有仅验证到文件补转写。重跑同步后文档内容不应重复，全部关闭录音应有对应成功任务。

遇到 HTTP 429 或业务码 10024/99991400 时，至少冷却 60 秒，并尊重飞书给出的恢复时间。限流期间实时文字会延迟，原录音与文件识别补转写仍保留，不能承诺持续秒级出字。请求超时不重发相同音频包；已经保存的最终片段 WAV 交由文件 ASR 补转写，其余部分由关闭录音提取补齐。

`REALTIME_ARCHIVE_ENABLED=false` 可退回上一版仅预览模式：独立 RTSP 读流，正式归档使用文件 ASR；这不是默认验收模式。关闭实时识别前保留已有任务及回执，未完成 streaming 任务由 worker 超时接管。


## 13. 每小时录制，每日一份文档

生产配置：

```dotenv
RECORD_SEGMENT_DURATION=1h
REALTIME_ASR_ENABLED=false
```

`feishu_sessions.json` 的 `document_grouping` 为 `daily`。文档按北京时间每天一份，`chapter_layout=timeline` 按排班时间顺序分章。标题例如 `08:00–12:00 · 张三`、`12:00–18:00 · 李四`、`18:00–次日00:00 · 王五`。同一主播连续排班合并为一章，间隔后再次上场单独成章；晚到的场次和文字分别插回时间顺序对应的位置，不按识别完成顺序排列。主播按排班表判断，淘宝、天猫、京东同一场直播仍映射到 `live/taobao` 的天猫排班。跨午夜的新排班按日期分开；跨交班短片段按开始时间唯一归属，可能带有交班前后声音，保留时间戳供核对。

多维表格继续每场一条记录，同日各场次链接指向同一份文档。原有场次回执和历史文档保留；已开始按旧方式归档的场次仍写回原文档，新场次使用每日文档。新文档和章节通过持久化回执以及失败读回保护防止重复；不覆盖用户编辑。

录制保存在 `data/recordings`，即使 ASR 或文档服务暂时失败也继续录制。录制文件包含 OBS 发送的音视频，空间占用取决于推流码率；现有配置不自动删除录音。失败转写沿用队列重试/失败状态，不宣称未成功片段已完整归档。

同步服务读取配置，不需要新建定时任务；更新后重启 `tbjdasr-feishu-sync.service`。修改 `.env` 后重建相关容器才能生效，勿在正在直播时直接重启 media。

## 14. 一次性 30 分钟 ASR 对比

`benchmark_asr.py` 独立等待下一次 `live/taobao` 推流，从同一条录音解码出完全相同的 PCM，比较飞书实时、飞书每30秒片段、录完后飞书文件、录完后公司整段 ASR。测试输出为独立目录中的 `capture.wav`、各模式 TXT、`events.jsonl`、`progress.json`。不更改正式一小时录制配置，不写正式排班文档。

公司 ASR 通过已有签名音频 URL 读取完整 WAV；只增加一条 `live/asr-benchmark`、`state=benchmark` 的非运行任务用于提供音频，不进入正常 worker 队列。地址签名不写日志。公司和飞书录后组在采集完成后同时启动，飞书短接口仍拆分请求。

启动示例（必须使用具备生产配置、FFmpeg、共享数据目录的容器）：

```sh
python benchmark_asr.py --output /data/benchmarks/unique-run --seconds 1800 --wait-seconds 7200
```

每次使用新目录，不自动重复推送测试。下播不足30分钟会标记 incomplete；处理时间上限为采集结束后一小时。飞书几组并发可能共享额度，报告单列限流次数；成功处理音频比例不是识别准确率，字数也不是准确率。需要人工比对原始录音与各组文本才能评价准确性。

### 从测试切换为正式归档

飞书同步配置可设置 `archive_id`（字母、数字、下划线或连字符）及
`archive_since`（Unix 秒时间戳）。指定独立批次后，同步器使用独立文档回执和场次编号，
仅纳入开始时间不早于该时间戳的音频任务；旧文档、旧记录和原始录音保留。
起点应选新推流录音文件的开始时间，避免截断已有音频任务。
部署专用配置可放在 `data/` 下，通过同步服务的 `--config` 参数指定；无需修改推流地址。
此设置控制归档边界，不改变每小时录音完成后进行转写的时机。
