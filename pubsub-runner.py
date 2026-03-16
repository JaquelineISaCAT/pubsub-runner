#!/usr/bin/env python3
import hmac
import errno
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import fcntl


BASE_DIR = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8788
ENDPOINT_PATH = "/pubsub"
SHARED_TOKEN = os.environ.get("SHARED_TOKEN", "SECRET")
DELAY_SECONDS = 1800
STALE_PENDING_GRACE_SECONDS = 300
MAX_BODY_BYTES = 1048576
LOCK_FILE = BASE_DIR / "lock"
PENDING_FILE = BASE_DIR / "pending"
LOG_FILE = BASE_DIR / "openclaw-doorbell.log"
# Customize the message passed to `openclaw agent --message` here.
OPENCLAW_AGENT_MESSAGE = (
    "retrieve unread email metadata in jaqueline.aime.grimper@gmail.com using gog skill, and launch email-router workflow"
)
DOCKER_COMMAND = [
    "docker",
    "exec",
    "openclaw-u4wa-openclaw-1",
    "bash",
    "-lc",
    f"openclaw agent --agent gog-main --message {shlex.quote(OPENCLAW_AGENT_MESSAGE)}",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_runtime_files() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)


def log_line(message: str) -> None:
    ensure_runtime_files()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {message}\n")


def pending_marker_text(created_at: str, worker_pid: int | None = None) -> str:
    lines = [created_at]
    if worker_pid is not None:
        lines.append(f"pid={worker_pid}")
    return "\n".join(lines) + "\n"


def read_pending_marker() -> tuple[datetime | None, int | None]:
    if not PENDING_FILE.exists():
        return None, None

    try:
        raw = PENDING_FILE.read_text(encoding="utf-8")
    except OSError:
        return None, None

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    created_at = None
    worker_pid = None

    if lines:
        try:
            created_at = datetime.fromisoformat(lines[0])
        except ValueError:
            created_at = None
        else:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

    for line in lines[1:]:
        if not line.startswith("pid="):
            continue
        try:
            worker_pid = int(line.split("=", 1)[1])
        except ValueError:
            worker_pid = None
        break

    return created_at, worker_pid


def marker_age_seconds(created_at: datetime | None) -> float | None:
    if created_at is not None:
        return max(0.0, (datetime.now(timezone.utc) - created_at).total_seconds())

    try:
        stat_result = PENDING_FILE.stat()
    except OSError:
        return None

    return max(0.0, time.time() - stat_result.st_mtime)


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    else:
        return True


def pending_marker_is_stale() -> tuple[bool, str | None]:
    if not PENDING_FILE.exists():
        return False, None

    created_at, worker_pid = read_pending_marker()
    age_seconds = marker_age_seconds(created_at)

    if worker_pid is not None:
        if pid_is_running(worker_pid):
            return False, None
        if age_seconds is None or age_seconds >= 10:
            return True, f"worker pid {worker_pid} is no longer running"
        return False, None

    threshold = DELAY_SECONDS + STALE_PENDING_GRACE_SECONDS
    if age_seconds is not None and age_seconds > threshold:
        return True, f"marker age {int(age_seconds)}s exceeded threshold {threshold}s"

    return False, None


def clear_stale_pending_marker() -> bool:
    is_stale, reason = pending_marker_is_stale()
    if not is_stale:
        return False

    try:
        PENDING_FILE.unlink(missing_ok=True)
    except OSError as exc:
        log_line(f"failed to remove stale pending marker: {exc!r}")
        return False

    log_line(f"removed stale pending marker: {reason}")
    return True


def recover_pending_state() -> None:
    ensure_runtime_files()
    with LOCK_FILE.open("r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        clear_stale_pending_marker()


def spawn_detached_worker() -> int:
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--run-pending"],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
    return process.pid


def schedule_if_needed() -> bool:
    ensure_runtime_files()
    with LOCK_FILE.open("r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        clear_stale_pending_marker()
        if PENDING_FILE.exists():
            return False

        created_at = utc_now()
        PENDING_FILE.write_text(pending_marker_text(created_at), encoding="utf-8")
        try:
            worker_pid = spawn_detached_worker()
        except Exception:
            PENDING_FILE.unlink(missing_ok=True)
            raise

        PENDING_FILE.write_text(pending_marker_text(created_at, worker_pid), encoding="utf-8")
        log_line(f"scheduled delayed run in {DELAY_SECONDS} seconds")
        return True


def run_pending_job() -> int:
    log_line(f"pending worker started; pid={os.getpid()}; sleeping for {DELAY_SECONDS} seconds")
    time.sleep(DELAY_SECONDS)

    result = None
    try:
        result = subprocess.run(
            DOCKER_COMMAND,
            capture_output=True,
            text=True,
            check=False,
        )
        log_line(f"docker exec finished with exit code {result.returncode}")
        if result.stdout.strip():
            log_line(f"docker stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            log_line(f"docker stderr: {result.stderr.strip()}")
        return result.returncode
    except Exception as exc:
        log_line(f"docker exec failed before completion: {exc!r}")
        return 1
    finally:
        try:
            PENDING_FILE.unlink(missing_ok=True)
        except Exception as exc:
            log_line(f"failed to remove pending marker: {exc!r}")
        else:
            status = "unknown" if result is None else str(result.returncode)
            log_line(f"pending marker cleared after worker exit; status={status}")


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenClawPubSubRunner/1.0"

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            pending = PENDING_FILE.exists()
            body = f'{{"ok":true,"pending":{"true" if pending else "false"}}}'.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path != ENDPOINT_PATH:
            self.send_response(404)
            self.end_headers()
            return

        token = parse_qs(parsed.query).get("token", [""])[0]
        if not hmac.compare_digest(token, SHARED_TOKEN):
            self.send_response(403)
            self.end_headers()
            log_line(f"rejected request from {self.client_address[0]}: invalid token")
            return

        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length > MAX_BODY_BYTES:
            self.send_response(413)
            self.end_headers()
            log_line(f"rejected request from {self.client_address[0]}: body too large")
            return

        if content_length:
            self.rfile.read(content_length)

        scheduled = schedule_if_needed()
        action = "scheduled" if scheduled else "already-pending"
        log_line(f"accepted request from {self.client_address[0]}: {action}")

        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def main() -> int:
    ensure_runtime_files()
    recover_pending_state()

    if len(sys.argv) > 1 and sys.argv[1] == "--run-pending":
        return run_pending_job()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Listening on http://{HOST}:{PORT}{ENDPOINT_PATH}", flush=True)
    print(f"Using lock file {LOCK_FILE}", flush=True)
    print(f"Using pending file {PENDING_FILE}", flush=True)
    print(f"Writing logs to {LOG_FILE}", flush=True)
    print(f"Delay is {DELAY_SECONDS} seconds", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
