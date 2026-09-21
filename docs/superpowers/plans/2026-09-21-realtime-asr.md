# Feishu realtime ASR implementation plan

Goal: Add a genuine live PCM reader and stream_recognize preview without disrupting durable recording, TXT and Feishu session archival.
Architecture: Isolated optional realtime worker reads allowlisted MediaMTX paths via internal authenticated RTSP. Send 200ms PCM16/16k/mono packets with ordered sequence IDs and bounded sessions. Persist provisional/final preview separately; expose authenticated polling API. Existing recording/file ASR is authoritative archive and fallback, avoiding double text in documents. Preview may differ from archived text and requires extra ASR calls.

- [ ] Verify official protocol and live provider with synthetic speech; never assume incremental response semantics.
- [ ] Test and implement stream protocol, error handling, terminal packet and cancellation.
- [ ] Add isolated SQLite preview state and authenticated API with explicit staleness.
- [ ] Add bounded FFmpeg live reader, room filtering, reconnect, watchdog and shutdown.
- [ ] Configure optional Compose service/internal RTSP and document operational limits.
- [ ] Run unit and real stream checks, code review, push/deploy and verify health.

Scope: low latency preview endpoint; formal document latency remains recording + file ASR + sync cadence. No partial recognition is appended to formal documents. No new frontend. The user has authorized implementing this feature; ordinary design choices are resolved here without another approval gate.
