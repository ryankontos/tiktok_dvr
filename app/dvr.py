#!/usr/bin/env python3
"""
TikTok Live DVR wrapper around Michele0303/tiktok-live-recorder.

Features:
- Segment-based recording (e.g. 15-minute chunks)
- Auto restart when streamer goes live again
- Local disk cleanup by age (hours)
- Optional Google Drive upload via rclone
- Startup catch-up upload of any cached recordings
- Heartbeat file with last activity timestamps
- Watchdog thread:
    - Discord alert + hard restart if no segments in N minutes while live
    - Optional memory soft-limit restart
    - Periodic status reports to Discord
- Hard timeout around each recording segment to avoid permanent hangs
- Nice filenames:
    Mon 21/11 1:00am - 1:15am.mp4
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple

from zoneinfo import ZoneInfo

import shutil

# Optional memory guard
try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover
    psutil = None  # we just skip memory-based watchdog if unavailable

import requests  # used for Discord webhook

# Optional systemd integration (will be harmless if library is missing)
try:
    from systemd import daemon as sd_daemon  # type: ignore
except ImportError:  # pragma: no cover
    sd_daemon = None

# --- Make tiktok-live-recorder importable ------------------------------------

# Adjust this if you installed the recorder somewhere else.
DEFAULT_TLR_SRC = "/opt/tiktok-live-recorder/src"
if DEFAULT_TLR_SRC not in sys.path:
    sys.path.append(DEFAULT_TLR_SRC)

from core.tiktok_api import TikTokAPI
from core.tiktok_recorder import TikTokRecorder
from utils.enums import Mode
from utils.custom_exceptions import LiveNotFound, UserLiveError, TikTokRecorderError

# ---------------------------------------------------------------------------

APP_ROOT = Path(__file__).resolve().parent.parent  # /root/tiktok_dvr
DEFAULT_OUTPUT_DIR = APP_ROOT / "recordings"
HEARTBEAT_FILE = DEFAULT_OUTPUT_DIR / ".tldvr_heartbeat.json"


# ------------------------------ Config --------------------------------------


@dataclass
class Config:
    username: str

    segment_seconds: int
    auto_restart: bool
    poll_interval: int

    test_seconds: int
    upload_test_segments: bool

    timezone: ZoneInfo
    cookies_path: Path
    output_dir: Path

    gdrive_remote: Optional[str]
    gdrive_folder: Optional[str]

    local_retention_hours: float

    # Watchdog / reliability
    watchdog_upload_stall_minutes: int
    memory_soft_limit_mb: int
    segment_timeout_grace_seconds: int

    # Discord notifications
    discord_alert_webhook_url: Optional[str]
    discord_status_webhook_url: Optional[str]
    status_report_minutes: int

    @classmethod
    def from_env(cls) -> "Config":
        def env(name: str, default: Optional[str] = None) -> Optional[str]:
            return os.environ.get(name, default)

        username = env("TLDVR_USERNAME")
        if not username:
            raise SystemExit("TLDVR_USERNAME must be set (TikTok username without @)")

        segment_seconds = int(env("TLDVR_SEGMENT_SECONDS", "900"))
        auto_restart = env("TLDVR_AUTO_RESTART", "true").lower() == "true"
        poll_interval = int(env("TLDVR_POLL_INTERVAL", "60"))

        test_seconds = int(env("TLDVR_TEST_SECONDS", "0"))
        upload_test_segments = env("TLDVR_UPLOAD_TEST_SEGMENTS", "false").lower() == "true"

        tz_name = env("TLDVR_TIMEZONE", "UTC")
        try:
            timezone = ZoneInfo(tz_name)
        except Exception:
            timezone = ZoneInfo("UTC")

        cookies_path = Path(env("TLDVR_COOKIES_PATH", str(APP_ROOT / "config" / "cookies.json")))
        output_dir = Path(env("TLDVR_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))

        gdrive_remote = env("TLDVR_GDRIVE_REMOTE")
        gdrive_folder = env("TLDVR_GDRIVE_FOLDER")

        lrh = float(env("TLDVR_LOCAL_RETENTION_HOURS", "10"))

        watchdog_upload_stall_minutes = int(env("TLDVR_WATCHDOG_UPLOAD_STALL_MINUTES", "30"))
        memory_soft_limit_mb = int(env("TLDVR_MEMORY_SOFT_LIMIT_MB", "800"))
        segment_timeout_grace_seconds = int(env("TLDVR_SEGMENT_TIMEOUT_GRACE_SECONDS", "60"))

        # Backwards compatible:
        # - TLDVR_DISCORD_WEBHOOK_URL = original single webhook
        # - TLDVR_DISCORD_ALERT_WEBHOOK_URL / TLDVR_DISCORD_STATUS_WEBHOOK_URL = split channels
        base_webhook = env("TLDVR_DISCORD_WEBHOOK_URL")
        alert_webhook = env("TLDVR_DISCORD_ALERT_WEBHOOK_URL") or base_webhook
        status_webhook = env("TLDVR_DISCORD_STATUS_WEBHOOK_URL") or base_webhook

        status_report_minutes = int(env("TLDVR_STATUS_REPORT_MINUTES", "60"))

        return cls(
            username=username,
            segment_seconds=segment_seconds,
            auto_restart=auto_restart,
            poll_interval=poll_interval,
            test_seconds=test_seconds,
            upload_test_segments=upload_test_segments,
            timezone=timezone,
            cookies_path=cookies_path,
            output_dir=output_dir,
            gdrive_remote=gdrive_remote,
            gdrive_folder=gdrive_folder,
            local_retention_hours=lrh,
            watchdog_upload_stall_minutes=watchdog_upload_stall_minutes,
            memory_soft_limit_mb=memory_soft_limit_mb,
            segment_timeout_grace_seconds=segment_timeout_grace_seconds,
            discord_alert_webhook_url=alert_webhook,
            discord_status_webhook_url=status_webhook,
            status_report_minutes=status_report_minutes,
        )


# ------------------------------ Logging -------------------------------------


def _now_str(tz: ZoneInfo) -> str:
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def log(cfg: Config, msg: str) -> None:
    print(f"[*] {_now_str(cfg.timezone)} - {msg}", flush=True)


def log_error(cfg: Config, msg: str) -> None:
    print(f"[!] {_now_str(cfg.timezone)} - {msg}", flush=True)


# ------------------------------ Restart helper ------------------------------


def restart_self() -> None:
    """
    Hard-restart the current process in-place.

    Used by the watchdog when it decides the process is wedged.
    """
    try:
        python = sys.executable
        os.execv(python, [python] + sys.argv)
    except Exception:
        # If execv fails for some reason, just exit.
        os._exit(1)


# ---------------------------- Discord notify --------------------------------


def notify_discord(
    cfg: Config,
    message: str,
    level: str = "info",
    channel: str = "alert",  # "alert" or "status"
) -> None:
    """
    Send a notification to Discord.

    - Alerts (failures, errors, watchdog restarts, recording start/stop, etc.) -> alerts webhook
    - Everything else (startup, normal uploads, status, catch-up, segment start/end) -> status webhook

    If the relevant webhook URL is not set, the message is silently skipped.
    No cross-fallback: status messages never go to the alerts channel, and
    alert messages never go to the status channel.
    """
    if channel == "status":
        webhook = cfg.discord_status_webhook_url
    else:
        webhook = cfg.discord_alert_webhook_url

    if not webhook:
        return

    prefix = {
        "info": "ℹ️",
        "warn": "⚠️",
        "error": "❌",
        "ok": "✅",
    }.get(level, "ℹ️")

    payload = {"content": f"{prefix} {message}"}
    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        if resp.status_code >= 400:
            log_error(cfg, f"Discord webhook returned HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:  # pragma: no cover
        log_error(cfg, f"Failed to send Discord notification: {e}")


# ----------------------------- Heartbeat / state ----------------------------

_last_upload_ts_lock = threading.Lock()
_last_upload_ts: Optional[float] = None

_last_segment_end_ts_lock = threading.Lock()
_last_segment_end_ts: Optional[float] = None

_is_live_lock = threading.Lock()
_is_live: bool = False


def update_last_upload_time() -> None:
    global _last_upload_ts
    with _last_upload_ts_lock:
        _last_upload_ts = time.time()


def set_last_segment_end(t: float) -> None:
    global _last_segment_end_ts
    with _last_segment_end_ts_lock:
        _last_segment_end_ts = t


def set_live_state(is_live: bool) -> None:
    global _is_live
    with _is_live_lock:
        _is_live = is_live


def get_live_state() -> bool:
    with _is_live_lock:
        return _is_live


HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)


def write_heartbeat(
    cfg: Config,
    last_segment_start: Optional[datetime],
    last_segment_end: Optional[datetime],
    last_upload: Optional[datetime],
    status: str,
) -> None:
    try:
        payload = {
            "username": cfg.username,
            "timezone": str(cfg.timezone),
            "last_segment_start_iso": last_segment_start.isoformat() if last_segment_start else None,
            "last_segment_end_iso": last_segment_end.isoformat() if last_segment_end else None,
            "last_upload_iso": last_upload.isoformat() if last_upload else None,
            "status": status,
            "updated_at_iso": datetime.now(cfg.timezone).isoformat(),
        }
        HEARTBEAT_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as e:  # pragma: no cover
        log_error(cfg, f"Failed to write heartbeat file: {e}")


# ---------------------------- Helpers ---------------------------------------


def load_cookies(cfg: Config) -> dict:
    if not cfg.cookies_path.exists():
        raise SystemExit(f"Cookies file not found: {cfg.cookies_path}")

    raw = cfg.cookies_path.read_text().strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # User pasted raw session cookie string; wrap it.
        data = {"session_ss": raw}

    if not isinstance(data, dict):
        raise SystemExit("cookies.json must contain a JSON object or raw cookie string.")

    return data


def ensure_output_dir(cfg: Config) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)


def build_segment_paths(cfg: Config, start_dt: datetime) -> Tuple[Path, str]:
    """
    Return (directory_path, date_subdir_name)

    Directory: recordings/YYYY-MM-DD/
    """
    date_dir = start_dt.date().isoformat()  # YYYY-MM-DD
    dir_path = cfg.output_dir / date_dir

    return dir_path, date_dir


def format_time_range_label(start_dt: datetime, end_dt: datetime) -> str:
    """
    Mon 21/11 1:00am - 1:30am
    """
    day = start_dt.strftime("%a")  # Mon, Tue, ...
    date_part = start_dt.strftime("%d/%m")  # 21/11

    def fmt_12h(dt: datetime) -> str:
        # 12-hour time with am/pm, no leading zero on hour.
        t = dt.strftime("%I:%M%p")  # e.g. "01:05PM"
        t = t.lstrip("0")           # "1:05PM"
        return t.lower()            # "1:05pm"

    start_time = fmt_12h(start_dt)
    end_time = fmt_12h(end_dt)
    return f"{day} {date_part} {start_time} - {end_time}"


def get_newest_mp4_in_dir(dir_path: Path, before: set[Path]) -> Optional[Path]:
    after = set(p for p in dir_path.glob("*.mp4") if p.is_file())
    new_files = list(after - before)
    if not new_files:
        return None
    return max(new_files, key=lambda p: p.stat().st_mtime)


def run_rclone_copy(cfg: Config, local_path: Path, remote_subdir: str) -> None:
    if not cfg.gdrive_remote or not cfg.gdrive_folder:
        log(cfg, "GDrive remote/folder not configured; skipping upload.")
        return

    remote_path = f"{cfg.gdrive_remote}:{cfg.gdrive_folder}/{remote_subdir}"
    cmd = [
        "rclone",
        "copy",
        str(local_path),
        remote_path,
        "--log-level",
        "INFO",
    ]
    log(cfg, f"Uploading {local_path} to Google Drive: {remote_path}/{local_path.name}")
    try:
        subprocess.run(cmd, check=True)
        log(cfg, "Upload to Google Drive completed.")
        notify_discord(
            cfg,
            f"Uploaded segment `{local_path.name}` to Google Drive (`{remote_subdir}`).",
            level="ok",
            channel="status",
        )
    except subprocess.CalledProcessError as e:
        log_error(cfg, f"rclone upload failed: {e}")
        notify_discord(
            cfg,
            f"DVR: rclone upload failed for `{local_path.name}`. Check logs on the server.",
            level="error",
            channel="alert",
        )


def cleanup_local_files(cfg: Config) -> None:
    """
    Delete local mp4s older than cfg.local_retention_hours.
    """
    if cfg.local_retention_hours <= 0:
        return

    cutoff = time.time() - cfg.local_retention_hours * 3600.0
    for date_dir in cfg.output_dir.glob("*"):
        if not date_dir.is_dir():
            continue
        for f in date_dir.glob("*.mp4"):
            try:
                mtime = f.stat().st_mtime
            except FileNotFoundError:
                continue
            if mtime < cutoff:
                log(cfg, f"Deleting local file older than {cfg.local_retention_hours} hours: {f}")
                try:
                    f.unlink()
                except Exception as e:
                    log_error(cfg, f"Failed to delete {f}: {e}")


def get_disk_usage_summary(path: Path) -> str:
    try:
        du = shutil.disk_usage(str(path))
        used_gb = du.used / (1024 ** 3)
        total_gb = du.total / (1024 ** 3)
        pct = du.used * 100.0 / du.total
        return f"{used_gb:.1f} / {total_gb:.1f} GiB used ({pct:.1f}%)"
    except Exception:
        return "unknown"


def catch_up_pending_uploads(cfg: Config) -> None:
    """
    On startup, upload any existing recordings under output_dir to Google Drive.

    This uses `rclone copy <local_root> <remote_root>` which is idempotent:
    already-uploaded files are skipped based on size+mtime, so it's safe to
    run on every start.

    Respects:
    - test mode (TLDVR_TEST_SECONDS & TLDVR_UPLOAD_TEST_SEGMENTS)
    - missing GDrive config
    """
    if not cfg.gdrive_remote or not cfg.gdrive_folder:
        log(cfg, "No GDrive remote configured; skipping startup catch-up upload.")
        return

    if cfg.test_seconds > 0 and not cfg.upload_test_segments:
        log(cfg, "Test mode with uploads disabled; skipping startup catch-up upload.")
        return

    # Any recordings at all?
    try:
        has_files = any(cfg.output_dir.rglob("*.mp4"))
    except Exception:
        has_files = False

    if not has_files:
        log(cfg, "No local recordings found; nothing to catch up.")
        return

    local_root = cfg.output_dir
    remote_root = f"{cfg.gdrive_remote}:{cfg.gdrive_folder}"

    log(cfg, f"Running startup catch-up upload from {local_root} to {remote_root} ...")
    cmd = ["rclone", "copy", str(local_root), remote_root, "--log-level", "INFO"]
    try:
        subprocess.run(cmd, check=True)
        log(cfg, "Startup catch-up upload completed.")
        update_last_upload_time()
        notify_discord(
            cfg,
            f"DVR startup catch-up upload completed for @{cfg.username}.",
            level="ok",
            channel="status",
        )
    except FileNotFoundError:
        log_error(cfg, "rclone not found in PATH; cannot perform startup catch-up upload.")
        notify_discord(
            cfg,
            "DVR: rclone not found; startup catch-up upload skipped. Check system setup.",
            level="error",
            channel="alert",
        )
    except subprocess.CalledProcessError as e:
        log_error(cfg, f"Startup rclone catch-up failed: {e}")
        notify_discord(
            cfg,
            "DVR: startup catch-up upload failed; see server logs for details.",
            level="error",
            channel="alert",
        )


# ------------------------- Segment worker (child) ----------------------------


def _segment_worker(
    username: str,
    room_id: Optional[str],
    cookies: dict,
    output_dir: str,
    segment_seconds: int,
) -> None:
    """
    Runs in a separate process so that if TikTokRecorder hangs,
    the parent can kill this entire process without leaking resources.
    """
    try:
        recorder = TikTokRecorder(
            url=None,
            user=username,
            room_id=room_id,
            mode=Mode.MANUAL,
            automatic_interval=0,
            cookies=cookies,
            proxy=None,
            output=output_dir,
            duration=segment_seconds,
            use_telegram=False,
        )
        recorder.manual_mode()
    except (UserLiveError, LiveNotFound):
        sys.exit(2)
    except TikTokRecorderError:
        sys.exit(3)
    except Exception:
        sys.exit(4)


# ----------------------- Watchdog thread ------------------------------------


def watchdog_loop(cfg: Config) -> None:
    """
    Background watchdog that:
    - Alerts + force-restarts the process if no completed segments in N minutes while live.
    - Optional memory guard.
    - Periodic status reports to Discord.
    - Sends systemd WATCHDOG notifications if available.
    """
    last_status_sent = 0.0
    status_interval = cfg.status_report_minutes * 60 if cfg.status_report_minutes > 0 else None

    while True:
        time.sleep(60)

        now = time.time()

        # Systemd watchdog heartbeat, if library present (harmless otherwise)
        if sd_daemon is not None:  # pragma: no cover
            try:
                sd_daemon.notify("WATCHDOG=1")
            except Exception:
                pass

        # Segment stall detection (only when we believe we're live)
        with _last_segment_end_ts_lock:
            se = _last_segment_end_ts

        if get_live_state() and se is not None:
            stall_minutes = (now - se) / 60.0
            if stall_minutes > cfg.watchdog_upload_stall_minutes:
                msg = (
                    f"DVR watchdog: no completed segment for "
                    f"{stall_minutes:.1f} minutes while live "
                    f"(threshold {cfg.watchdog_upload_stall_minutes}m). "
                    "Assuming hang and restarting process."
                )
                log_error(cfg, msg)
                notify_discord(cfg, msg, level="error", channel="alert")
                restart_self()

        # Memory guard
        rss_mb = None
        if psutil is not None:
            try:
                proc = psutil.Process(os.getpid())
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                if rss_mb > cfg.memory_soft_limit_mb:
                    msg = (
                        f"DVR watchdog: memory usage {rss_mb:.1f} MB exceeds "
                        f"soft limit {cfg.memory_soft_limit_mb} MB. Restarting process."
                    )
                    log_error(cfg, msg)
                    notify_discord(cfg, msg, level="error", channel="alert")
                    restart_self()
            except Exception:
                pass

        # Periodic status report
        if status_interval is not None and now - last_status_sent > status_interval:
            with _last_upload_ts_lock:
                lu = _last_upload_ts
            with _last_segment_end_ts_lock:
                se = _last_segment_end_ts

            def fmt_age(ts: Optional[float]) -> str:
                if ts is None:
                    return "never"
                minutes = (now - ts) / 60.0
                return f"{minutes:.1f} min ago"

            disk_summary = get_disk_usage_summary(cfg.output_dir)

            msg = (
                f"DVR status for @{cfg.username}: "
                f"last upload: {fmt_age(lu)}, "
                f"last segment end: {fmt_age(se)}, "
                f"live: {get_live_state()}, "
                f"disk: {disk_summary}"
            )
            if rss_mb is not None:
                msg += f", RSS: {rss_mb:.1f} MB"

            notify_discord(cfg, msg, level="info", channel="status")
            last_status_sent = now


# ------------------------------ Main loop -----------------------------------


def main() -> None:
    cfg = Config.from_env()
    ensure_output_dir(cfg)

    log(cfg, f"USERNAME: @{cfg.username}")
    log(cfg, f"Output directory: {cfg.output_dir}")
    log(cfg, f"Segment length: {cfg.segment_seconds} seconds (must be > 0 in normal mode).")
    if cfg.test_seconds > 0:
        log(
            cfg,
            f"TEST mode active: overriding segment length to {cfg.test_seconds} seconds; "
            f"upload_test_segments={cfg.upload_test_segments}",
        )
    if cfg.test_seconds == 0 and cfg.segment_seconds <= 0:
        raise SystemExit("TLDVR_SEGMENT_SECONDS must be > 0 when not in test mode.")

    # Warn if watchdog stall is shorter than or comparable to segment length
    if cfg.watchdog_upload_stall_minutes * 60 <= cfg.segment_seconds + cfg.segment_timeout_grace_seconds:
        log_error(
            cfg,
            "WARNING: TLDVR_WATCHDOG_UPLOAD_STALL_MINUTES is shorter than or equal to "
            "segment length + grace. This may cause false-positive watchdog restarts.",
        )

    log(cfg, f"Auto restart after lives: {cfg.auto_restart}")
    log(cfg, f"Local retention: {cfg.local_retention_hours} hours")

    if cfg.discord_alert_webhook_url or cfg.discord_status_webhook_url:
        notify_discord(
            cfg,
            f"DVR started for @{cfg.username} on host `{os.uname().nodename}`",
            level="ok",
            channel="status",
        )

    # Startup catch-up: upload any cached recordings that haven't made it to Drive.
    catch_up_pending_uploads(cfg)

    cookies = load_cookies(cfg)
    api = TikTokAPI(proxy=None, cookies=cookies)

    # Inform systemd we're ready (if using Type=notify; harmless otherwise)
    if sd_daemon is not None:  # pragma: no cover
        try:
            sd_daemon.notify("READY=1")
        except Exception:
            pass

    # Start watchdog thread
    t = threading.Thread(target=watchdog_loop, args=(cfg,), daemon=True)
    t.start()

    last_segment_start: Optional[datetime] = None
    last_segment_end: Optional[datetime] = None
    last_upload_dt: Optional[datetime] = None
    set_live_state(False)

    while True:
        try:
            # Resolve room id
            try:
                room_id = api.get_room_id_from_user(cfg.username)
            except Exception as e:
                log_error(cfg, f"Failed to get room_id for @{cfg.username}: {e}")
                notify_discord(
                    cfg,
                    f"DVR: failed to get room_id for @{cfg.username}: {e}",
                    level="error",
                    channel="alert",
                )
                set_live_state(False)
                time.sleep(cfg.poll_interval)
                continue

            # Check live status (with error handling)
            alive = False
            if room_id:
                try:
                    alive = api.is_room_alive(room_id)
                except Exception as e:
                    log_error(cfg, f"Error while checking live status for @{cfg.username}: {e}")
                    notify_discord(
                        cfg,
                        f"DVR: error checking live status for @{cfg.username}: {e}",
                        level="warn",
                        channel="status",
                    )
                    set_live_state(False)
                    time.sleep(cfg.poll_interval)
                    continue

            if not room_id or not alive:
                log(cfg, f"@{cfg.username} is not live.")
                set_live_state(False)
                if not cfg.auto_restart:
                    log(cfg, "Auto restart disabled; exiting.")
                    write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="idle_offline")
                    notify_discord(
                        cfg,
                        f"DVR stopped for @{cfg.username}: streamer is offline and auto-restart is disabled.",
                        level="warn",
                        channel="alert",
                    )
                    return
                log(cfg, f"Waiting {cfg.poll_interval} seconds before recheck.")
                write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="waiting_for_live")
                time.sleep(cfg.poll_interval)
                continue

            log(cfg, f"@{cfg.username} is live. room_id = {room_id}")
            set_live_state(True)
            notify_discord(
                cfg,
                f"Recording started for @{cfg.username} (room_id={room_id}).",
                level="ok",
                channel="alert",
            )
            log(cfg, "Starting TikTok Live DVR loop.")

            # Inner loop: while live, record segment after segment.
            while True:
                # Determine actual duration for this segment
                if cfg.test_seconds > 0:
                    seg_seconds = cfg.test_seconds
                else:
                    seg_seconds = cfg.segment_seconds

                seg_start = datetime.now(cfg.timezone)
                seg_end = seg_start + timedelta(seconds=seg_seconds)
                last_segment_start = seg_start

                seg_dir, date_dir = build_segment_paths(cfg, seg_start)
                seg_dir.mkdir(parents=True, exist_ok=True)

                label = format_time_range_label(seg_start, seg_end)
                log(cfg, f"Recording new segment for up to {seg_seconds} seconds. Label: {label}")
                notify_discord(
                    cfg,
                    f"Segment started for @{cfg.username}: {label}",
                    level="info",
                    channel="status",
                )

                # Snapshot mp4s before segment
                before_mp4s = set(p for p in seg_dir.glob("*.mp4") if p.is_file())

                # Spawn worker process
                from multiprocessing import Process

                worker = Process(
                    target=_segment_worker,
                    args=(
                        cfg.username,
                        room_id,
                        cookies,
                        str(seg_dir) + os.sep,
                        seg_seconds,
                    ),
                )
                worker.start()

                # Wait with hard timeout
                max_wait = seg_seconds + cfg.segment_timeout_grace_seconds
                log(cfg, f"Started recording for {seg_seconds} seconds (max wait {max_wait}s)")
                log(cfg, "[PRESS CTRL + C ONCE TO STOP]")

                worker.join(timeout=max_wait)

                if worker.is_alive():
                    # Hung – kill worker
                    worker.terminate()
                    worker.join(timeout=10)
                    log_error(
                        cfg,
                        f"Recording worker exceeded max wait ({max_wait}s) and was terminated. "
                        "This usually means TikTok or the network froze mid-segment.",
                    )
                    notify_discord(
                        cfg,
                        f"DVR: recording segment hung and had to be killed. "
                        f"Username=@{cfg.username}, label={label}.",
                        level="error",
                        channel="alert",
                    )
                    write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="segment_hung")
                    time.sleep(5)
                    try:
                        if not api.is_room_alive(room_id):
                            log(cfg, "User appears to be offline after hung segment.")
                            break
                    except Exception:
                        pass
                    continue

                # Segment finished (worker exited normally)
                last_segment_end = datetime.now(cfg.timezone)
                set_last_segment_end(time.time())
                notify_discord(
                    cfg,
                    f"Segment ended for @{cfg.username}: {label}",
                    level="info",
                    channel="status",
                )

                # Worker exited; see if any mp4 appeared
                newest_mp4 = get_newest_mp4_in_dir(seg_dir, before_mp4s)

                if newest_mp4 is None:
                    # Give filesystem a moment and try again once
                    time.sleep(3)
                    newest_mp4 = get_newest_mp4_in_dir(seg_dir, before_mp4s)

                if worker.exitcode not in (0, 2, 3, 4) and worker.exitcode is not None:
                    log_error(cfg, f"Segment worker exited with unexpected code {worker.exitcode}.")
                    notify_discord(
                        cfg,
                        f"DVR: segment worker exited with code {worker.exitcode} for @{cfg.username}.",
                        level="error",
                        channel="alert",
                    )

                if newest_mp4 is None:
                    log_error(cfg, "No new mp4 file detected after segment. Skipping upload.")
                    notify_discord(
                        cfg,
                        f"DVR: segment finished but no mp4 file was produced for @{cfg.username} "
                        f"(label={label}).",
                        level="warn",
                        channel="alert",
                    )
                    write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="segment_no_file")
                    try:
                        if not api.is_room_alive(room_id):
                            log(cfg, "User appears to be offline after empty segment.")
                            break
                    except Exception as e:
                        log_error(cfg, f"Error while checking live status after empty segment: {e}")
                    time.sleep(5)
                    continue

                # Clean filename: Day + DD/MM + clean 12h time label
                nice_name = f"{label}.mp4"
                nice_path = seg_dir / nice_name

                # Always *attempt* to end up with a file named `nice_name`,
                # so Drive never sees TK_* filenames in normal operation.
                if newest_mp4.name != nice_name:
                    try:
                        newest_mp4.rename(nice_path)
                        log(cfg, f"Renamed {newest_mp4.name} -> {nice_path.name}")
                    except Exception as e:
                        log_error(cfg, f"Failed to rename {newest_mp4} to {nice_path}: {e}")
                        # Fallback: copy to the clean name, still upload that.
                        try:
                            shutil.copy2(newest_mp4, nice_path)
                            log(cfg, f"Copied {newest_mp4.name} -> {nice_path.name} after rename failure")
                        except Exception as e2:
                            log_error(
                                cfg,
                                f"Failed to copy segment to clean name {nice_path}: {e2}. "
                                f"Will upload original filename {newest_mp4.name}.",
                            )
                            nice_path = newest_mp4
                else:
                    # It already has the nice name
                    log(cfg, f"Segment already named {nice_path.name}")
                    nice_path = newest_mp4

                # Upload (optionally skip if test mode & not uploading tests)
                if cfg.test_seconds > 0 and not cfg.upload_test_segments:
                    log(
                        cfg,
                        f"TEST mode: not uploading segment {nice_path.name} to Google Drive "
                        "(TLDVR_UPLOAD_TEST_SEGMENTS=false).",
                    )
                    notify_discord(
                        cfg,
                        f"Test mode: skipping upload of `{nice_path.name}` for @{cfg.username}.",
                        level="info",
                        channel="status",
                    )
                else:
                    run_rclone_copy(cfg, nice_path, date_dir)
                    last_upload_dt = datetime.now(cfg.timezone)
                    update_last_upload_time()

                # Local cleanup: delete raw TK_* mp4 artefacts for this segment dir
                try:
                    for raw in seg_dir.glob("TK_*.mp4"):
                        if raw != nice_path and raw.is_file():
                            log(cfg, f"Deleting raw TK mp4 artefact: {raw}")
                            try:
                                raw.unlink()
                            except Exception as e:
                                log_error(cfg, f"Failed to delete raw TK mp4 file {raw}: {e}")
                    for raw_flv in seg_dir.glob("TK_*_flv.mp4"):
                        if raw_flv.is_file():
                            log(cfg, f"Deleting raw FLV mp4 artefact: {raw_flv}")
                            try:
                                raw_flv.unlink()
                            except Exception as e:
                                log_error(cfg, f"Failed to delete raw FLV mp4 file {raw_flv}: {e}")
                except Exception as e:
                    log_error(cfg, f"Error while cleaning TK_* artefacts in {seg_dir}: {e}")

                # Local age-based cleanup only (remote never cleaned by script)
                cleanup_local_files(cfg)

                write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="ok")

                # Check if still live before next segment
                try:
                    if not api.is_room_alive(room_id):
                        log(cfg, "User is no longer live. Will handle according to AUTO_RESTART.")
                        break
                except Exception as e:
                    log_error(cfg, f"Error while checking live status: {e}")

            # end inner while (per-live-session loop)

            set_live_state(False)
            notify_discord(
                cfg,
                f"Recording stopped for @{cfg.username}. Stream appears to be offline.",
                level="warn",
                channel="alert",
            )

            if not cfg.auto_restart:
                log(cfg, "Auto restart disabled; exiting after live finished.")
                write_heartbeat(
                    cfg,
                    last_segment_start,
                    last_segment_end,
                    last_upload_dt,
                    status="stopped_no_autorestart",
                )
                return

            log(cfg, f"Waiting {cfg.poll_interval} seconds before checking if @{cfg.username} goes live again.")
            write_heartbeat(
                cfg,
                last_segment_start,
                last_segment_end,
                last_upload_dt,
                status="waiting_for_next_live",
            )
            time.sleep(cfg.poll_interval)

        except KeyboardInterrupt:
            log(cfg, "Interrupted by user (Ctrl+C). Exiting DVR loop.")
            set_live_state(False)
            write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="terminated_manual")
            notify_discord(
                cfg,
                f"DVR terminated manually for @{cfg.username} (Ctrl+C).",
                level="warn",
                channel="alert",
            )
            return
        except Exception as e:
            log_error(cfg, f"Unexpected error in main loop: {e}")
            notify_discord(
                cfg,
                f"DVR: unexpected error in main loop: {e}",
                level="error",
                channel="alert",
            )
            write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="error_main_loop")
            # Leave some time before we hammer TikTok / ourselves again
            time.sleep(10)


if __name__ == "__main__":
    # Make sure Ctrl+C actually kills us, not just children.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()
