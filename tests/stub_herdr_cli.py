"""Stands in for the ``herdr`` CLI in tests: forwards a few subcommands to the fake socket and prints the
same ``{"id": "cli:...", "result": ...}`` envelope the real CLI prints."""
import json
import os
import socket
import sys

a = sys.argv[1:]
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
s.connect(os.environ["HERDR_SOCKET_PATH"])
s.sendall((json.dumps({"id": "cli", "method": method, "params": params}) + "\n").encode())
print(s.makefile().readline().strip())
