# 多直播间收流实施说明

目标：同一 RTMPS 入口按不同流名接收多个直播间，独立录音、任务和逐字稿，兼容现有 live/main。

采用显式 STREAM_PATHS 逗号列表，与原 STREAM_PATH 合并去重。相比放开任意路径，此方式保留发布路径白名单；相比每路单独部署，不需要更多端口和进程。共用现有发布凭据，不增加每路用户管理。媒体和 API 的线上端口覆盖保持不变。

实施与验证：
- [x] tests/test_multistream.py：验证多个路径授权、原路径保留、非法路径拒绝、任务按直播间轮流选取、统计和文档隔离。
- [x] app/config.py 和 media_entry.py：集中解析路径列表，将每个路径写入 MediaMTX paths 和发布权限。
- [x] app/store.py：按每路队列排名选择待处理任务；增加按 room 聚合任务统计。
- [x] app/api.py：增加需认证的 GET /v1/rooms，列出配置路径及历史任务计数，明确不代表在线状态。
- [x] tests/test_rtmp_integration.py：同时发送 live/main 和 live/second，拒绝未知路径，验证每路完整音频时长及无串流。
- [x] 更新 .env.example、init_config.py、README.md：两路配置、保留密钥、9030/443 实例、更新命令。
- [x] 运行全部测试及原生 MediaMTX/FFmpeg 集成测试，提交并推送现有交付分支。

边界：一个 worker 顺序执行 ASR HTTP 操作；公司异步 ASR 可有多个远端任务同时等待处理。不是无限吞吐保证；实际容量取决于带宽、磁盘、FFmpeg 和 ASR 限额。飞书逐字稿继续通过导出脚本按 room 分别生成。

验证记录：26 项测试通过（含 4 个原生 MediaMTX 集成用例），2 条现有依赖弃用警告；只读审查未发现阻断问题。未连接或更新线上服务器，现场更新后仍需真实 ASR 联调。
