"""Stands in for the ``herdr`` CLI in tests: forwards a few subcommands to the fake socket and prints the
same ``{"id": "cli:...", "result": ...}`` envelope the real CLI prints."""
import json
import os
import socket
import sys

a = sys.argv[1:]
sock = os.environ["HERDR_SOCKET_PATH"]
if a[:2] == ["--machine", a[1]] and a[0] == "--machine":
    # Another saved machine: its socket comes from HERDR_STUB_MACHINE_<id>; unset means it is down.
    if os.environ.get("HERDR_STUB_HANG_" + a[1]):
        import subprocess
        import time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])  # inherits stdout/stderr like herdr's bridge
        time.sleep(30)
    sock = os.environ.get("HERDR_STUB_MACHINE_" + a[1], "")
    a = a[2:]
if a[:2] == ["machine", "list"]:
    print(os.environ.get("HERDR_STUB_MACHINES", ""))
    sys.exit(0)
params, method = {}, None
if a[:2] == ["pane", "list"]:
    method = "pane.list"
elif a[:2] == ["pane", "get"]:
    method, params = "pane.get", {"pane_id": a[2]}
elif a[:2] == ["pane", "read"]:
    opts = dict(zip(a[3::2], a[4::2]))
    method, params = "pane.read", {"pane_id": a[2], "source": opts.get("--source"), "lines": int(opts.get("--lines", 0))}
elif a[:2] == ["pane", "report-metadata"]:
    tokens, i = {}, 3
    while i < len(a):
        if a[i] == "--token":
            k, _, v = a[i + 1].partition("=")
            tokens[k] = v
        elif a[i] == "--clear-token":
            tokens[a[i + 1]] = None
        i += 2
    method, params = "pane.report_metadata", {"pane_id": a[2], "tokens": tokens}
else:
    print(json.dumps({"id": "cli", "error": {"code": "unknown_command", "message": " ".join(a)}}))
    sys.exit(1)

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sock)
s.sendall((json.dumps({"id": "cli:stub", "method": method, "params": params}) + "\n").encode())
reply = s.makefile().readline().strip()
msg = json.loads(reply)
if "error" in msg:
    sys.stderr.write(reply + "\n")  # the real CLI reports errors on stderr
    sys.exit(1)
if method == "pane.read":
    sys.stdout.write(msg["result"]["read"]["text"])  # bare text, not an envelope
elif method != "pane.report_metadata":  # and says nothing at all when a metadata update succeeds
    print(reply)
