export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export interface PublicConfig {
  server?: {
    host?: string;
    port?: number;
    workers?: number;
  };
  dashboard?: Record<string, never>;
  stream?: {
    command?: string;
    encoder?: string;
    output_dir?: string;
    ffmpeg_log_dir?: string;
    public_hls_url?: string;
    bitrate?: string;
    audio_bitrate?: string;
    auto_recover?: boolean;
    auto_restart_on_exit?: boolean;
    watchdog_restart_cooldown?: number;
    startup_grace_seconds?: number;
    playlist_stale_seconds?: number;
    min_assessment_seconds?: number;
    health_sample_interval?: number;
    success_score_threshold?: number;
    failure_score_threshold?: number;
    confirmed_failure_samples?: number;
    failure_ramp_seconds?: number;
    links?: string[];
  };
  arangodb?: {
    enabled?: boolean;
    url?: string;
    database?: string;
    username?: string;
  };
}

export interface FeedEvent {
  ts?: number;
  level?: string;
  message?: string;
  extra?: Record<string, unknown>;
}

export interface LogEntry {
  ts?: number;
  level?: string;
  line?: string;
}

export interface ChildProcess {
  pid?: number;
  name?: string;
  cpu?: number;
  rss?: number;
}

export interface ManagedProcess {
  managed?: boolean;
  pid?: number | null;
  started_at?: number | null;
  age?: number | null;
  cpu?: number | null;
  rss?: number | null;
  cmd?: string;
  children?: ChildProcess[];
}

export interface ExternalProcess {
  pid?: number;
  cmd?: string;
  age?: number | null;
}

export interface HlsMetrics {
  output_dir?: string;
  playlist?: string;
  playlist_exists?: boolean;
  playlist_ready?: boolean;
  playlist_age?: number | null;
  playlist_modified_at?: number | null;
  playlist_line_count?: number;
  segments?: number;
  bytes?: number;
  latest_segment_modified_at?: number | null;
  oldest_segment_modified_at?: number | null;
  target_duration?: string | number | null;
  media_sequence?: string | number | null;
  segment_window_seconds?: number | null;
  playlist_segment_count?: number;
  playlist_segments?: string[];
  first_segment?: string | null;
  last_segment?: string | null;
  last_segment_size?: number | null;
  public_hls_url?: string;
  dashboard_hls_url?: string;
}

export interface HealthEvidence {
  has_child?: boolean;
  playlist_exists?: boolean;
  playlist_ready?: boolean;
  playlist_fresh?: boolean;
  playlist_age?: number | null;
  segment_delta?: number;
  bytes_delta?: number;
  playlist_moved?: boolean;
  media_sequence_advanced?: boolean;
  progress_seen?: boolean;
  recent_error_count?: number;
  ramp?: number;
  reasons?: string[];
}

export interface HealthSample {
  ts?: number;
  score?: number;
  decision?: string;
  playlist_age?: number | null;
  segments?: number;
  bytes?: number;
  bytes_delta?: number;
  media_sequence?: string | number | null;
  segment_delta?: number;
  playlist_moved?: boolean;
  recent_error_count?: number;
}

export interface HealthAssessment {
  state?: string;
  level?: string;
  decision?: string;
  message?: string;
  score?: number;
  confidence?: number;
  assessment_elapsed?: number;
  assessment_remaining?: number;
  consecutive_bad_samples?: number;
  consecutive_good_samples?: number;
  evidence?: HealthEvidence;
  samples?: HealthSample[];
  recent_errors?: LogEntry[];
}

export interface RuntimeStats {
  stream_starts?: number;
  stream_restarts?: number;
  watchdog_restarts?: number;
  start_failures?: number;
  last_exit_code?: number | null;
  arango_dropped_writes?: number;
  arango_write_failures?: number;
  app_started_at?: number;
  app_uptime_seconds?: number;
  arango_queue_depth?: number;
}

export interface StatusPayload {
  ok: boolean;
  config: PublicConfig;
  managed_process?: ManagedProcess;
  existing_processes?: ExternalProcess[];
  hls?: HlsMetrics;
  health?: HealthAssessment;
  events?: FeedEvent[];
  logs?: LogEntry[];
  errors?: LogEntry[];
  server_time?: number;
  runtime?: RuntimeStats;
}

export interface ArangoStatus {
  ok?: boolean;
  connected?: boolean;
  version?: unknown;
  error?: string;
}

export interface GpuInfo {
  index?: number | null;
  name?: string | null;
  uuid?: string | null;
  driver_version?: string | null;
  pstate?: string | null;
  temperature_c?: number | null;
  gpu_utilization_pct?: number | null;
  memory_utilization_pct?: number | null;
  memory_total_mb?: number | null;
  memory_used_mb?: number | null;
  memory_free_mb?: number | null;
  memory_used_pct?: number | null;
  power_draw_w?: number | null;
  power_limit_w?: number | null;
  power_used_pct?: number | null;
  graphics_clock_mhz?: number | null;
  memory_clock_mhz?: number | null;
  encoder_session_count?: number | null;
  encoder_average_fps?: number | null;
  encoder_average_latency_ms?: number | null;
}

export interface GpuProcess {
  gpu_uuid?: string | null;
  gpu_index?: number | null;
  pid?: number;
  type?: string | null;
  process_name?: string | null;
  used_memory_mb?: number | null;
  sm_pct?: number | null;
  mem_pct?: number | null;
  enc_pct?: number | null;
  dec_pct?: number | null;
  is_ffmpeg?: boolean;
}

export interface GpuSummary {
  gpu_count?: number;
  driver_version?: string | null;
  max_temperature_c?: number | null;
  max_gpu_utilization_pct?: number | null;
  max_memory_used_pct?: number | null;
  power_draw_w?: number | null;
  power_limit_w?: number | null;
  encoder_session_count?: number | null;
  encoder_utilization_pct?: number | null;
  process_count?: number;
  ffmpeg_process_count?: number;
  stream_gpu_active?: boolean;
}

export interface NvidiaCommandSummary {
  command?: string;
  returncode?: number;
  elapsed_ms?: number;
  stdout?: string;
  stderr?: string;
}

export interface GpuTelemetryPayload {
  ok?: boolean;
  checked_at?: number;
  collector_interval_seconds?: number;
  available?: boolean;
  level?: string;
  message?: string;
  diagnosis?: string[];
  errors?: string[];
  summary?: GpuSummary;
  gpus?: GpuInfo[];
  processes?: GpuProcess[];
  commands?: Record<string, NvidiaCommandSummary>;
  cached?: boolean;
  cache_age_seconds?: number;
}
