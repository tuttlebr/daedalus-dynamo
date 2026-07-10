#!/usr/bin/env python3
"""Expose agentgateway file-descriptor and TCP socket metrics from /proc."""

from __future__ import annotations

import os
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROC_ROOT = Path(os.environ.get("PROC_ROOT", "/proc"))
TARGET_EXECUTABLE = os.environ.get("TARGET_EXECUTABLE", "/app/agentgateway")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9090"))
POD_NAME = os.environ.get("POD_NAME", "unknown")
POD_NAMESPACE = os.environ.get("POD_NAMESPACE", "unknown")

TCP_STATES = {
    "01": "established",
    "02": "syn_sent",
    "03": "syn_recv",
    "04": "fin_wait1",
    "05": "fin_wait2",
    "06": "time_wait",
    "07": "close",
    "08": "close_wait",
    "09": "last_ack",
    "0A": "listen",
    "0B": "closing",
    "0C": "new_syn_recv",
}


def _metric_labels() -> str:
    namespace = POD_NAMESPACE.replace("\\", "\\\\").replace('"', '\\"')
    pod = POD_NAME.replace("\\", "\\\\").replace('"', '\\"')
    return f'namespace="{namespace}",pod="{pod}"'


def find_agentgateway_pid(proc_root: Path = PROC_ROOT) -> int | None:
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if any(arg.decode("utf-8", "replace") == TARGET_EXECUTABLE for arg in args):
            return int(entry.name)
    return None


def read_nofile_limits(pid: int, proc_root: Path = PROC_ROOT) -> tuple[int, int]:
    for line in (proc_root / str(pid) / "limits").read_text().splitlines():
        fields = line.split()
        if fields[:3] == ["Max", "open", "files"]:
            soft = int(fields[3])
            hard = int(fields[4])
            return soft, hard
    raise RuntimeError("Max open files is missing from process limits")


def read_open_fds(pid: int, proc_root: Path = PROC_ROOT) -> int:
    fd_dir = proc_root / str(pid) / "fd"
    return len(list(fd_dir.iterdir()))


def read_tcp_states(pid: int, proc_root: Path = PROC_ROOT) -> Counter[str]:
    """Count TCP sockets in the pod network namespace shared with agentgateway."""
    states: Counter[str] = Counter()
    for table in ("tcp", "tcp6"):
        path = proc_root / str(pid) / "net" / table
        try:
            lines = path.read_text().splitlines()[1:]
        except FileNotFoundError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) <= 3:
                continue
            states[TCP_STATES.get(fields[3].upper(), f"unknown_{fields[3].lower()}")] += 1
    return states


def render_metrics(proc_root: Path = PROC_ROOT) -> tuple[str, bool]:
    labels = _metric_labels()
    pid = find_agentgateway_pid(proc_root)
    if pid is None:
        return (
            "# HELP daedalus_gateway_fd_exporter_up Whether the agentgateway process is visible.\n"
            "# TYPE daedalus_gateway_fd_exporter_up gauge\n"
            f"daedalus_gateway_fd_exporter_up{{{labels}}} 0\n",
            False,
        )

    soft, hard = read_nofile_limits(pid, proc_root)
    open_fds = read_open_fds(pid, proc_root)
    states = read_tcp_states(pid, proc_root)
    headroom = max(soft - open_fds, 0)
    utilization = open_fds / soft if soft else 1.0
    lines = [
        "# HELP daedalus_gateway_fd_exporter_up Whether the agentgateway process is visible.",
        "# TYPE daedalus_gateway_fd_exporter_up gauge",
        f"daedalus_gateway_fd_exporter_up{{{labels}}} 1",
        "# HELP daedalus_gateway_process_open_fds Open file descriptors held by agentgateway.",
        "# TYPE daedalus_gateway_process_open_fds gauge",
        f"daedalus_gateway_process_open_fds{{{labels}}} {open_fds}",
        "# HELP daedalus_gateway_process_soft_limit_fds RLIMIT_NOFILE soft limit for agentgateway.",
        "# TYPE daedalus_gateway_process_soft_limit_fds gauge",
        f"daedalus_gateway_process_soft_limit_fds{{{labels}}} {soft}",
        "# HELP daedalus_gateway_process_hard_limit_fds RLIMIT_NOFILE hard limit for agentgateway.",
        "# TYPE daedalus_gateway_process_hard_limit_fds gauge",
        f"daedalus_gateway_process_hard_limit_fds{{{labels}}} {hard}",
        "# HELP daedalus_gateway_process_fd_headroom_fds File descriptors remaining before the soft limit.",
        "# TYPE daedalus_gateway_process_fd_headroom_fds gauge",
        f"daedalus_gateway_process_fd_headroom_fds{{{labels}}} {headroom}",
        "# HELP daedalus_gateway_process_fd_utilization_ratio Fraction of the soft FD limit in use.",
        "# TYPE daedalus_gateway_process_fd_utilization_ratio gauge",
        f"daedalus_gateway_process_fd_utilization_ratio{{{labels}}} {utilization:.9f}",
        "# HELP daedalus_gateway_pod_tcp_sockets TCP sockets in the gateway pod network namespace by state.",
        "# TYPE daedalus_gateway_pod_tcp_sockets gauge",
    ]
    for state in TCP_STATES.values():
        lines.append(
            f'daedalus_gateway_pod_tcp_sockets{{{labels},state="{state}"}} {states[state]}'
        )
    return "\n".join(lines) + "\n", True


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in ("/healthz", "/metrics"):
            self.send_error(404)
            return
        try:
            body, healthy = render_metrics()
        except (OSError, RuntimeError, ValueError) as exc:
            body = f"gateway FD exporter error: {exc}\n"
            healthy = False
        status = 200 if path == "/metrics" or healthy else 503
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header(
            "content-type",
            "text/plain; version=0.0.4; charset=utf-8" if path == "/metrics" else "text/plain",
        )
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
