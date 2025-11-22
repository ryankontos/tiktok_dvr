#!/usr/bin/env python3
"""
TikTok Live DVR wrapper around Michele0303/tiktok-live-recorder.

Features:
- Segment-based recording (e.g. 15-minute chunks)
- Auto restart when streamer goes live again
- Local disk cleanup by age (hours)
- Optional Google Drive upload via rclone
- Heartbeat file with last activity timestamps
- Watchdog thread:
    - Discord alert + hard exit if no uploads in N minutes
    - Optional memory soft-limit restart
    - Periodic status reports to Discord
- Hard timeout around each recording segment to avoid permanent hangs
- Nice filenames:
    username - Fri 21/11/25, 22:00 - 22:15.mp4
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
    remote_retention_days: Optional[int]

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
        rrd = env("TLDVR_REMOTE_RETENTION_DAYS")
        remote_retention_days = int(rrd) if rrd and int(rrd) > 0 else None

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
            remote_retention_days=remote_retention_days,
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


# ---------------------------- Discord notify --------------------------------


def notify_discord(
    cfg: Config,
    message: str,
    level: str = "info",
    channel: str = "alert",  # "alert" or "status"
) -> None:
    """
    Send a notification to Discord.

    channel:
        "alert"  -> alerts / errors
        "status" -> periodic heartbeat/status

    If specific webhook for the channel is not set, falls back to the other.
    """
    if channel == "status":
        webhook = cfg.discord_status_webhook_url or cfg.discord_alert_webhook_url
    else:
        webhook = cfg.discord_alert_webhook_url or cfg.discord_status_webhook_url

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


# ----------------------------- Heartbeat ------------------------------------

_last_upload_ts_lock = threading.Lock()
_last_upload_ts: Optional[float] = None

_last_segment_end_ts_lock = threading.Lock()
_last_segment_end_ts: Optional[float] = None


def update_last_upload_time() -> None:
    global _last_upload_ts
    with _last_upload_ts_lock:
        _last_upload_ts = time.time()


def set_last_segment_end(t: float) -> None:
    global _last_segment_end_ts
    with _last_segment_end_ts_lock:
        _last_segment_end_ts = t


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
    Fri 21/11/25, 22:00 - 22:15
    """
    day = start_dt.strftime("%a")  # Mon, Tue, ...
    date_part = start_dt.strftime("%d/%m/%y")  # 21/11/25
    start_time = start_dt.strftime("%H:%M")
    end_time = end_dt.strftime("%H:%M")
    return f"{day} {date_part}, {start_time} - {end_time}"


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
    cmd = ["rclone", "copy", str(local_path), remote_path, "--log-level", "INFO"]
    log(cfg, f"Uploading {local_path} to Google Drive: {remote_path}/{local_path.name}")
    try:
        subprocess.run(cmd, check=True)
        log(cfg, "Upload to Google Drive completed.")
    except subprocess.CalledProcessError as e:
        log_error(cfg, f"rclone upload failed: {e}")
        notify_discord(
            cfg,
            f"DVR: rclone upload failed for `{local_path.name}`. Check logs on the server.",
            level="error",
            channel="alert",
        )


def run_rclone_remote_cleanup(cfg: Config) -> None:
    if not cfg.gdrive_remote or not cfg.gdrive_folder or cfg.remote_retention_days is None:
        return

    remote_root = f"{cfg.gdrive_remote}:{cfg.gdrive_folder}"
    min_age = f"{cfg.remote_retention_days}d"
    cmd = [
        "rclone",
        "delete",
        remote_root,
        "--min-age",
        min_age,
        "--log-level",
        "INFO",
    ]
    log(cfg, f"Cleaning up remote files older than {cfg.remote_retention_days} days in {remote_root}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        log_error(cfg, f"rclone remote cleanup failed: {e}")
        # Don't spam Discord on every failure; just log.


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
    - Alerts + force-exits if no uploads in N minutes.
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

        # Upload stall detection
        with _last_upload_ts_lock:
            lu = _last_upload_ts

        if lu is not None:
            stall_minutes = (now - lu) / 60.0
            if stall_minutes > cfg.watchdog_upload_stall_minutes:
                msg = (
                    f"DVR watchdog: no successful upload for "
                    f"{stall_minutes:.1f} minutes (threshold {cfg.watchdog_upload_stall_minutes}m). "
                    "Assuming hang and restarting process."
                )
                log_error(cfg, msg)
                notify_discord(cfg, msg, level="error", channel="alert")
                os._exit(1)

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
                    os._exit(1)
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
    log(cfg, f"Segment length: {cfg.segment_seconds} seconds (0 = no segmentation).")
    if cfg.test_seconds > 0:
        log(
            cfg,
            f"TEST mode active: overriding segment length to {cfg.test_seconds} seconds; "
            f"upload_test_segments={cfg.upload_test_segments}",
        )
    log(cfg, f"Auto restart after lives: {cfg.auto_restart}")
    log(cfg, f"Local retention: {cfg.local_retention_hours} hours")
    if cfg.remote_retention_days is not None:
        log(cfg, f"Remote retention: {cfg.remote_retention_days} days")
    else:
        log(cfg, "Remote retention: disabled")

    if cfg.discord_alert_webhook_url or cfg.discord_status_webhook_url:
        notify_discord(
            cfg,
            f"DVR started for @{cfg.username} on host `{os.uname().nodename}`",
            level="ok",
            channel="status",
        )

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

    while True:
        try:
            # Resolve room id and check live
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
                time.sleep(cfg.poll_interval)
                continue

            if not room_id or not api.is_room_alive(room_id):
                log(cfg, f"@{cfg.username} is not live.")
                if not cfg.auto_restart:
                    log(cfg, "Auto restart disabled; exiting.")
                    write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="idle_offline")
                    return
                log(cfg, f"Waiting {cfg.poll_interval} seconds before recheck.")
                write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="waiting_for_live")
                time.sleep(cfg.poll_interval)
                continue

            log(cfg, f"@{cfg.username} is live. room_id = {room_id}")
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

                # Worker exited; see if any mp4 appeared
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
                    write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="segment_no_file")
                    try:
                        if not api.is_room_alive(room_id):
                            log(cfg, "User appears to be offline after empty segment.")
                            break
                    except Exception:
                        pass
                    time.sleep(5)
                    continue

                # Rename to nice format
                nice_name = f"{cfg.username} - {label}.mp4"
                nice_path = seg_dir / nice_name

                try:
                    newest_mp4.rename(nice_path)
                    log(cfg, f"Renamed {newest_mp4.name} -> {nice_path.name}")
                except Exception as e:
                    log_error(cfg, f"Failed to rename {newest_mp4} to {nice_path}: {e}")
                    nice_path = newest_mp4  # fallback

                last_segment_end = datetime.now(cfg.timezone)
                set_last_segment_end(time.time())

                # Upload (optionally skip if test mode & not uploading tests)
                if cfg.test_seconds > 0 and not cfg.upload_test_segments:
                    log(
                        cfg,
                        f"TEST mode: not uploading segment {nice_path.name} to Google Drive "
                        "(TLDVR_UPLOAD_TEST_SEGMENTS=false).",
                    )
                else:
                    run_rclone_copy(cfg, nice_path, date_dir)
                    last_upload_dt = datetime.now(cfg.timezone)
                    update_last_upload_time()

                # Local cleanup
                cleanup_local_files(cfg)

                # Optional remote cleanup
                if cfg.remote_retention_days is not None:
                    run_rclone_remote_cleanup(cfg)

                write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="ok")

                # Check if still live before next segment
                try:
                    if not api.is_room_alive(room_id):
                        log(cfg, "User is no longer live. Will handle according to AUTO_RESTART.")
                        break
                except Exception as e:
                    log_error(cfg, f"Error while checking live status: {e}")

            # end inner while (per-live-session loop)

            if not cfg.auto_restart:
                log(cfg, "Auto restart disabled; exiting after live finished.")
                write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="stopped_no_autorestart")
                return

            log(cfg, f"Waiting {cfg.poll_interval} seconds before checking if @{cfg.username} goes live again.")
            write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="waiting_for_next_live")
            time.sleep(cfg.poll_interval)

        except KeyboardInterrupt:
            log(cfg, "Interrupted by user (Ctrl+C). Exiting DVR loop.")
            write_heartbeat(cfg, last_segment_start, last_segment_end, last_upload_dt, status="terminated_manual")
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
            time.sleep(10)


if __name__ == "__main__":
    # Make sure Ctrl+C actually kills us, not just children.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    main()