# OBS 收流与 ASR 服务实施计划

目标：在现有 OBS 输出之外接收额外一路 H.264/AAC RTMP/RTMPS，分段转写并提供结果 API、XML 文档导出。

结构：独立 MediaMTX 录制进程先持久化视频分段，完成钩子生成磁盘完成标记。Worker 扫描标记、生成 16 kHz 单声道 PCM16 WAV（每段最多 45 秒），SQLite 保存任务、重试时间、公司 ASR UUID 和请求 URL。API 提供鉴权查询、重试及限时音频下载。收流不等待 ASR。

实现顺序：
- [x] tests/test_service.py：音频格式、超长分段、签名、任务幂等、恢复、两家 ASR HTTP 合约和 XML 转义测试，先运行得到缺失实现失败。
- [x] app/config.py、security.py、store.py、audio.py：配置与持久化，转换原始片段并插入去重任务。
- [x] app/providers.py、worker.py：公司 submit/query/result，飞书 tenant token 与 file_recognize，持久状态、指数退避、失败补跑。
- [x] app/api.py、export_feishu.py：管理端 Bearer 鉴权、短期音频签名、状态与 XML 导出；通过用户身份 CLI 导出到指定飞书文件夹。
- [x] media_entry.py、segment_hook.py、compose.yaml、Dockerfile：收流配置、钩子、卷和端口、证书配置。
- [x] 本地使用实际 FFmpeg 与 MediaMTX 推流测试；外部 ASR 使用契约测试，健康检查可读，未获真实凭据前不声称真实识别成功。
- [x] README.md 和部署压缩包，说明公网音频 URL、服务器配置、OBS 地址与生产验证边界。

边界：分段识别而非实时字幕；公司结果中的 utterances 保留原样（文档未定义每个字段的时间单位）；飞书无逐词时间戳。默认不跨供应商自动切换。原始分段不自动过期，避免误删待处理数据，磁盘容量由状态接口告警。
