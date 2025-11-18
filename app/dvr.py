import argparse
import json
import os
import signal
import sys
import time
import subprocess
from datetime import datetime
from http.client import HTTPException
from zoneinfo import ZoneInfo

from requests import RequestException

# These imports come from the cloned tiktok-live-recorder repo inside the container
from core.tiktok_recorder import TikTokRecorder
from utils.enums import Mode
from utils.logger_manager import logger
from utils.custom_exceptions import LiveNotFound, TikTokRecorderError


def str_to_bool(value: str) -> bool:
    """
    Convert typical truthy / falsy strings to bool.
    """
    if isinstance(value, bool):
        return value
    value = value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Cannot interpret boolean value from: {value!r}")


def get_env(name: str, default=None):
    """
    Read environment variable with a default.
    """
    return os.environ.get(name, default)


def load_cookies(path: str) -> dict:
    """
    Load and normalise cookies from a file.

    Supported formats in cookies.json:

    1) Simple dict:
       { "session_ss": "VALUE" }

    2) Raw JSON string containing just the value:
       "VALUE"
       -> treated as { "session_ss": "VALUE" }

    3) JSON string with a cookie header:
       "session_ss=VALUE; other=XYZ"
       -> parsed into { "session_ss": "VALUE", "other": "XYZ" }

    4) JSON list of objects (browser-style export):
       [ { "name": "session_ss", "value": "VALUE" }, ... ]

    5) Non-JSON fallback: plain text in the file:
       - "VALUE" -> { "session_ss": "VALUE" }
       - "session_ss=VALUE; other=XYZ" -> parsed into dict
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"cookies file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()

    if not raw:
        raise ValueError(f"cookies file is empty: {path}")

    # Try JSON first
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw  # treat as plain string

    # Case 1: already a dict
    if isinstance(parsed, dict):
        return parsed

    # Case 2/3: parsed as a string
    if isinstance(parsed, str):
        s = parsed.strip()

        # If no "=", assume it's just the session_ss value
        if "=" not in s and ";" not in s:
            return {"session_ss": s}

        # Otherwise, parse as cookie header: "k=v; k2=v2"
        cookies: dict[str, str] = {}
        for chunk in s.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                k, v = chunk.split("=", 1)
                cookies[k.strip()] = v.strip()
        if cookies:
            return cookies
        raise ValueError(f"Could not parse cookies string from {path!r}")

    # Case 4: list (e.g. browser export)
    if isinstance(parsed, list):
        cookies: dict[str, str] = {}
        for item in parsed:
            if isinstance(item, dict) and "name" in item and "value" in item:
                cookies[item["name"]] = item["value"]
        if cookies:
            return cookies
        raise ValueError(f"Unsupported cookies list structure in {path!r}")

    raise ValueError(f"Unsupported cookies format in {path!r}: {type(parsed)}")


class DvrTikTokRecorder(TikTokRecorder):
    """
    Subclass of TikTokRecorder that:
      - uses a custom filename format based on timezone
      - stores recordings in per-date subfolders
      - optionally uploads finished segments to Google Drive via rclone
      - performs local and remote retention cleanup
    """

    def __init__(
        self,
        url,
        user,
        room_id,
        mode,
        automatic_interval,
        cookies,
        proxy,
        output,
        duration,
        use_telegram,
        timezone_name="UTC",
        enable_upload=False,
        gdrive_remote=None,
        gdrive_folder="BBAU2025_Live_Feed_Archive",
        local_retention_days=1.0,
        local_retention_hours=None,
        remote_retention_days=30,
    ):
        super().__init__(
            url=url,
            user=user,
            room_id=room_id,
            mode=mode,
            automatic_interval=automatic_interval,
            cookies=cookies,
            proxy=proxy,
            output=output,
            duration=duration,
            use_telegram=use_telegram,
        )
        self.timezone_name = timezone_name
        self.enable_upload = enable_upload
        self.gdrive_remote = gdrive_remote
        self.gdrive_folder = gdrive_folder
        self.local_retention_days = local_retention_days
        self.local_retention_hours = local_retention_hours
        self.remote_retention_days = remote_retention_days

    def _now(self) -> datetime:
        """
        Get current time in the configured timezone.
        """
        try:
            tz = ZoneInfo(self.timezone_name)
        except Exception:
            tz = ZoneInfo("UTC")
        return datetime.now(tz)

    def _format_base_filename(self, user: str, now: datetime) -> str:
        """
        Format base filename like:
        username-Monday-25-Feb-2025-10:00am
        """
        weekday = now.strftime("%A")
        day = now.strftime("%d").lstrip("0")
        month = now.strftime("%b")  # Feb
        year = now.strftime("%Y")
        time_str = now.strftime("%I:%M%p").lstrip("0").lower()  # 10:00am

        safe_user = user.replace("@", "")
        return f"{safe_user}-{weekday}-{day}-{month}-{year}-{time_str}"

    def _upload_to_gdrive(self, final_mp4: str, now: datetime) -> None:
        """
        Upload the given file to Google Drive via rclone, into:
          <remote>:<gdrive_folder>/<YYYY-MM-DD>/<filename>
        """
        if not self.enable_upload:
            return
        if not self.gdrive_remote or not self.gdrive_folder:
            logger.info("GDrive upload enabled but remote/folder not configured; skipping upload.")
            return

        remote = self.gdrive_remote
        date_folder = now.strftime("%Y-%m-%d")
        filename = os.path.basename(final_mp4)
        remote_path = f"{remote}:{self.gdrive_folder}/{date_folder}/{filename}"

        try:
            logger.info(f"Uploading {final_mp4} to Google Drive: {remote_path}")
            subprocess.run(
                [
                    "rclone",
                    "copyto",
                    final_mp4,
                    remote_path,
                    "--transfers",
                    "1",
                    "--checkers",
                    "2",
                    "--log-level",
                    "INFO",
                ],
                check=True,
            )
            logger.info("Upload to Google Drive completed.")
        except FileNotFoundError:
            logger.error("rclone not found inside container; cannot upload to Google Drive.")
        except subprocess.CalledProcessError as ex:
            logger.error(f"rclone upload failed: {ex}")

    def _cleanup_local_old_files(self) -> None:
        """
        Delete local .mp4 files older than the configured retention.
        Priority:
          - If local_retention_hours is set (non-None), use that.
          - Else use local_retention_days.
        """
        # Determine retention in seconds
        retention_seconds = 0.0
        used_hours = False

        try:
            if self.local_retention_hours is not None:
                hours = float(self.local_retention_hours)
                if hours <= 0:
                    return
                retention_seconds = hours * 60 * 60
                used_hours = True
            else:
                days = float(self.local_retention_days)
                if days <= 0:
                    return
                retention_seconds = days * 24 * 60 * 60
        except Exception:
            # Fallback: don't delete anything if config is weird
            logger.error("Invalid local retention value; skipping local cleanup.")
            return

        now_ts = time.time()
        base_dir = self.output or "."
        for root, _, files in os.walk(base_dir):
            for name in files:
                if not name.lower().endswith(".mp4"):
                    continue
                path = os.path.join(root, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                if now_ts - mtime > retention_seconds:
                    try:
                        if used_hours:
                            logger.info(
                                f"Deleting local file older than {self.local_retention_hours} hours: {path}"
                            )
                        else:
                            logger.info(
                                f"Deleting local file older than {self.local_retention_days} days: {path}"
                            )
                        os.remove(path)
                    except OSError as ex:
                        logger.error(f"Failed to delete local file {path}: {ex}")

    def _cleanup_remote_old_files(self) -> None:
        """
        Delete remote files older than remote_retention_days from Google Drive
        using rclone's --min-age filter.
        """
        return

        if not self.enable_upload:
            return
        if not self.gdrive_remote or not self.gdrive_folder:
            return

        try:
            days = max(0, int(self.remote_retention_days))
        except Exception:
            days = 30

        if days <= 0:
            return

        remote = self.gdrive_remote
        target = f"{remote}:{self.gdrive_folder}"
        age_flag = f"{days}d"

        try:
            logger.info(f"Cleaning up remote files older than {days} days in {target}")
            subprocess.run(
                [
                    "rclone",
                    "delete",
                    target,
                    "--min-age",
                    age_flag,
                    "--log-level",
                    "INFO",
                ],
                check=True,
            )
        except FileNotFoundError:
            logger.error("rclone not found inside container; cannot clean up Google Drive.")
        except subprocess.CalledProcessError as ex:
            logger.error(f"rclone remote cleanup failed: {ex}")

    def start_recording(self, user, room_id):
        """
        Start recording live, with custom filename and post-processing
        (upload + cleanup).
        This is based on the original TikTokRecorder.start_recording, with
        filename/timezone adjustments and extra hooks.
        """
        live_url = self.tiktok.get_live_url(room_id)
        if not live_url:
            raise LiveNotFound("Failed to retrieve live URL.")

        now = self._now()
        base_name = self._format_base_filename(user, now)
        date_folder = now.strftime("%Y-%m-%d")

        # Build per-date directory under self.output
        base_dir = self.output or ""
        # Strip trailing slashes for os.path.join sanity
        if base_dir.endswith("/") or base_dir.endswith("\\"):
            base_dir = base_dir[:-1]

        target_dir = os.path.join(base_dir, date_folder)
        os.makedirs(target_dir, exist_ok=True)

        output = os.path.join(target_dir, f"{base_name}_flv.mp4")

        if self.duration:
            logger.info(f"Started recording for {self.duration} seconds ")
        else:
            logger.info("Started recording...")

        buffer_size = 512 * 1024  # 512 KB buffer
        buffer = bytearray()

        logger.info("[PRESS CTRL + C ONCE TO STOP]")
        from utils.enums import Error, TimeOut  # local import to avoid circulars

        with open(output, "wb") as out_file:
            stop_recording = False
            while not stop_recording:
                try:
                    if not self.tiktok.is_room_alive(room_id):
                        logger.info("User is no longer live. Stopping recording.")
                        break

                    start_time = time.time()
                    for chunk in self.tiktok.download_live_stream(live_url):
                        buffer.extend(chunk)
                        if len(buffer) >= buffer_size:
                            out_file.write(buffer)
                            buffer.clear()

                        elapsed_time = time.time() - start_time
                        if self.duration and elapsed_time >= self.duration:
                            stop_recording = True
                            break

                except ConnectionError:
                    if self.mode == Mode.AUTOMATIC:
                        logger.error(Error.CONNECTION_CLOSED_AUTOMATIC)
                        time.sleep(TimeOut.CONNECTION_CLOSED * TimeOut.ONE_MINUTE)

                except (RequestException, HTTPException):
                    time.sleep(2)

                except KeyboardInterrupt:
                    logger.info("Recording stopped by user.")
                    stop_recording = True

                except Exception as ex:
                    logger.error(f"Unexpected error: {ex}\n")
                    stop_recording = True

                finally:
                    if buffer:
                        out_file.write(buffer)
                        buffer.clear()
                    out_file.flush()

        logger.info(f"Recording finished: {output}\n")
        from utils.video_management import VideoManagement
        from upload.telegram import Telegram

        VideoManagement.convert_flv_to_mp4(output)

        final_mp4 = output.replace("_flv.mp4", ".mp4")

        if self.use_telegram:
            Telegram().upload(final_mp4)

        # Upload to Google Drive (if enabled) and perform cleanup
        try:
            self._upload_to_gdrive(final_mp4, now)
        except Exception as ex:
            logger.error(f"GDrive upload step failed: {ex}")

        try:
            self._cleanup_local_old_files()
        except Exception as ex:
            logger.error(f"Local cleanup step failed: {ex}")

        try:
            self._cleanup_remote_old_files()
        except Exception as ex:
            logger.error(f"Remote cleanup step failed: {ex}")


def parse_args() -> argparse.Namespace:
    """
    Parse CLI arguments, using environment variables as defaults.
    Environment variables (all optional):
      - TLDVR_USERNAME
      - TLDVR_COOKIES_PATH
      - TLDVR_OUTPUT_DIR
      - TLDVR_SEGMENT_SECONDS
      - TLDVR_AUTO_RESTART
      - TLDVR_POLL_INTERVAL
      - TLDVR_TEST_SECONDS
      - TLDVR_USE_TELEGRAM
      - TLDVR_TIMEZONE
      - TLDVR_GDRIVE_REMOTE
      - TLDVR_GDRIVE_FOLDER
      - TLDVR_LOCAL_RETENTION_DAYS
      - TLDVR_LOCAL_RETENTION_HOURS
      - TLDVR_REMOTE_RETENTION_DAYS
      - TLDVR_UPLOAD_TEST_SEGMENTS
    """
    parser = argparse.ArgumentParser(
        description="TikTok Live DVR wrapper around tiktok-live-recorder."
    )

    parser.add_argument(
        "-u",
        "--username",
        help="TikTok username (without @). "
             "Can also be set via TLDVR_USERNAME.",
        default=get_env("TLDVR_USERNAME"),
    )

    parser.add_argument(
        "--cookies-path",
        help="Path to cookies.json inside the container. "
             "Default: TLDVR_COOKIES_PATH or /config/cookies.json.",
        default=get_env("TLDVR_COOKIES_PATH", "/config/cookies.json"),
    )

    parser.add_argument(
        "--output-dir",
        help="Directory where recordings will be written. "
             "Default: TLDVR_OUTPUT_DIR or /output.",
        default=get_env("TLDVR_OUTPUT_DIR", "/output"),
    )

    parser.add_argument(
        "--segment-seconds",
        type=int,
        help=(
            "Segment length in seconds. "
            "If > 0, each live is split into segments of this length. "
            "If 0, record the entire live in one file. "
            "Default: TLDVR_SEGMENT_SECONDS or 1800 (30 minutes)."
        ),
        default=int(get_env("TLDVR_SEGMENT_SECONDS", "1800")),
    )

    parser.add_argument(
        "--auto-restart",
        type=str_to_bool,
        help=(
            "If true, keep watching for new lives after one ends. "
            "Default: TLDVR_AUTO_RESTART or true."
        ),
        default=str_to_bool(get_env("TLDVR_AUTO_RESTART", "true")),
    )

    parser.add_argument(
        "--poll-interval",
        type=int,
        help=(
            "Seconds between checks when user is not live. "
            "Default: TLDVR_POLL_INTERVAL or 60."
        ),
        default=int(get_env("TLDVR_POLL_INTERVAL", "60")),
    )

    parser.add_argument(
        "--test-seconds",
        type=int,
        help=(
            "If > 0, run a single short recording of this many seconds "
            "for testing, then exit. Default: TLDVR_TEST_SECONDS or 0."
        ),
        default=int(get_env("TLDVR_TEST_SECONDS", "0")),
    )

    parser.add_argument(
        "--use-telegram",
        type=str_to_bool,
        help="If true, enable Telegram upload using telegram.json. "
             "Default: TLDVR_USE_TELEGRAM or false.",
        default=str_to_bool(get_env("TLDVR_USE_TELEGRAM", "false")),
    )

    parser.add_argument(
        "--timezone",
        help="IANA timezone name, e.g. Australia/Sydney.",
        default=get_env("TLDVR_TIMEZONE", "UTC"),
    )

    parser.add_argument(
        "--gdrive-remote",
        help="rclone remote name for Google Drive (e.g. gdrive).",
        default=get_env("TLDVR_GDRIVE_REMOTE"),
    )

    parser.add_argument(
        "--gdrive-folder",
        help="Google Drive folder name under the remote.",
        default=get_env("TLDVR_GDRIVE_FOLDER", "BBAU2025_Live_Feed_Archive"),
    )

    parser.add_argument(
        "--local-retention-days",
        type=float,
        help="Number of days to keep local recordings (default 1). Ignored if local-retention-hours is set.",
        default=float(get_env("TLDVR_LOCAL_RETENTION_DAYS", "1.0")),
    )

    parser.add_argument(
        "--local-retention-hours",
        type=float,
        help="Number of hours to keep local recordings. If set, this overrides local-retention-days.",
        default=None if get_env("TLDVR_LOCAL_RETENTION_HOURS") is None
        else float(get_env("TLDVR_LOCAL_RETENTION_HOURS")),
    )

    parser.add_argument(
        "--remote-retention-days",
        type=int,
        help="Number of days to keep recordings on Google Drive (default 30).",
        default=int(get_env("TLDVR_REMOTE_RETENTION_DAYS", "30")),
    )

    parser.add_argument(
        "--upload-test-segments",
        type=str_to_bool,
        help=(
            "If true, upload test segments (when test_seconds > 0) to Google Drive "
            "as well. Default: TLDVR_UPLOAD_TEST_SEGMENTS or false."
        ),
        default=str_to_bool(get_env("TLDVR_UPLOAD_TEST_SEGMENTS", "false")),
    )

    args = parser.parse_args()

    if not args.username:
        parser.error(
            "You must provide a TikTok username via --username or TLDVR_USERNAME."
        )

    return args


def make_recorder(args: argparse.Namespace) -> DvrTikTokRecorder:
    """
    Create a DvrTikTokRecorder instance configured for a single user in MANUAL mode.
    We do our own loop logic; we don't use TikTokRecorder.run().
    """
    output_dir = args.output_dir

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Load and normalise cookies into a dict
    cookies = load_cookies(args.cookies_path)

    # automatic_interval in original code is in minutes; we won't use automatic_mode,
    # but the constructor requires a value, so we pass something sane.
    automatic_interval_minutes = max(1, args.poll_interval // 60) or 1

    # Upload behaviour:
    # - Normal run (test_seconds == 0): always upload.
    # - Test run (test_seconds > 0): upload only if upload_test_segments is True.
    enable_upload = (args.test_seconds == 0) or args.upload_test_segments

    recorder = DvrTikTokRecorder(
        url=None,
        user=args.username,
        room_id=None,
        mode=Mode.MANUAL,
        automatic_interval=automatic_interval_minutes,
        cookies=cookies,
        proxy=None,
        output=output_dir,
        duration=None,
        use_telegram=args.use_telegram,
        timezone_name=args.timezone,
        enable_upload=enable_upload,
        gdrive_remote=args.gdrive_remote,
        gdrive_folder=args.gdrive_folder,
        local_retention_days=args.local_retention_days,
        local_retention_hours=args.local_retention_hours,
        remote_retention_days=args.remote_retention_days,
    )

    return recorder


def wait_until_live(recorder: TikTokRecorder, args: argparse.Namespace):
    """
    Poll until the user is live. Returns room_id if live, or None if we should stop
    (e.g. auto_restart is False and user is not live on first check).
    """
    username = recorder.user
    first_check = True

    while True:
        try:
            room_id = recorder.tiktok.get_room_id_from_user(username)
            if room_id and recorder.tiktok.is_room_alive(room_id):
                recorder.room_id = room_id
                logger.info(f"@{username} is live. room_id = {room_id}")
                return room_id

            logger.info(f"@{username} is not live.")

            if not args.auto_restart:
                # If this was just a one-shot run, don't wait forever.
                if first_check:
                    logger.info("auto_restart is disabled; exiting because user is offline.")
                return None

            first_check = False
            logger.info(f"Waiting {args.poll_interval} seconds before checking again...")
            time.sleep(args.poll_interval)

        except TikTokRecorderError as ex:
            logger.error(f"TikTokRecorderError while waiting for live: {ex}")
            if not args.auto_restart:
                return None
            time.sleep(args.poll_interval)

        except KeyboardInterrupt:
            logger.info("Interrupted while waiting for live. Exiting.")
            return None

        except Exception as ex:
            logger.error(f"Unexpected error while waiting for live: {ex}")
            time.sleep(args.poll_interval)


def run_test_mode(recorder: DvrTikTokRecorder, args: argparse.Namespace):
    """
    Run a short test recording for args.test_seconds, then exit.
    Useful to verify Docker, cookies, FFmpeg, etc.
    """
    username = recorder.user
    logger.info(
        f"Starting TEST recording for @{username} for {args.test_seconds} seconds."
    )

    room_id = wait_until_live(recorder, args)
    if not room_id:
        logger.info("User is not live; aborting test run.")
        return

    try:
        recorder.duration = args.test_seconds
        recorder.start_recording(username, room_id)
        logger.info("Test recording completed. Exiting.")
    except KeyboardInterrupt:
        logger.info("Test recording interrupted by user.")
    except Exception as ex:
        logger.error(f"Error during test recording: {ex}")


def run_dvr(recorder: DvrTikTokRecorder, args: argparse.Namespace):
    """
    Main DVR loop:
      - Wait for user to go live.
      - Record either:
          * one full live (no segmentation), or
          * repeated segments of segment_seconds.
      - If auto_restart is True, go back to waiting for the next live.
    """
    username = recorder.user
    segment_seconds = args.segment_seconds

    logger.info("Starting TikTok Live DVR loop.")
    logger.info(f"Target username: @{username}")
    logger.info(f"Output directory: {args.output_dir}")
    logger.info(f"Segment length: {segment_seconds} seconds (0 = no segmentation).")
    logger.info(f"Auto restart after lives: {args.auto_restart}")

    while True:
        room_id = wait_until_live(recorder, args)
        if room_id is None:
            logger.info("No live to record; exiting DVR loop.")
            return

        logger.info(f"Starting recording loop for @{username} (room_id {room_id}).")

        while True:
            try:
                if segment_seconds > 0:
                    recorder.duration = segment_seconds
                    logger.info(
                        f"Recording new segment for up to {segment_seconds} seconds."
                    )
                else:
                    recorder.duration = None
                    logger.info("Recording full live (no segmentation).")

                recorder.start_recording(username, room_id)

            except LiveNotFound as ex:
                logger.error(f"Live not found: {ex}")
                break

            except (ConnectionError, RequestException, HTTPException) as ex:
                logger.error(f"Network error during recording: {ex}")
                logger.info(f"Sleeping {args.poll_interval} seconds before retry.")
                time.sleep(args.poll_interval)

            except KeyboardInterrupt:
                logger.info("Recording interrupted by user. Exiting completely.")
                return

            except Exception as ex:
                logger.error(f"Unexpected error during recording: {ex}")
                logger.info(f"Sleeping {args.poll_interval} seconds before retry.")
                time.sleep(args.poll_interval)

            # After a segment (or full live) finishes:
            if segment_seconds <= 0:
                # No segmentation; we assume the live has ended or user stopped.
                logger.info("Finished recording full live; leaving inner loop.")
                break

            # Segmented mode: check if the live is still ongoing
            try:
                if not recorder.tiktok.is_room_alive(room_id):
                    logger.info("User is no longer live. Ending segment loop.")
                    break
                else:
                    logger.info("User is still live. Starting next segment.")
            except Exception as ex:
                logger.error(f"Error while checking live status: {ex}")
                break

        if not args.auto_restart:
            logger.info(
                "auto_restart is disabled; stopping after this live."
            )
            return

        logger.info(
            f"Live ended. Waiting {args.poll_interval} seconds before "
            "looking for the next live."
        )
        time.sleep(args.poll_interval)


def main():
    # Basic handling of SIGTERM for clean shutdown in Docker
    def handle_sigterm(signum, frame):
        logger.info("Received SIGTERM, shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    args = parse_args()
    recorder = make_recorder(args)

    if args.test_seconds > 0:
        run_test_mode(recorder, args)
    else:
        run_dvr(recorder, args)


if __name__ == "__main__":
    main()