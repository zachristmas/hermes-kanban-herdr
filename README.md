# hermes-kanban-herdr

A Hermes dashboard plugin that adds a **Herdr panes** section to the kanban task drawer: the
panes working on a task, one open at a time, with a live, read-only view of the open pane.

![Herdr panes section in the task drawer](docs/herdr-panes.png)

## What it does

- Lists the [Herdr](https://herdr.dev) panes linked to the task, with agent and status.
- Click a pane to open it (accordion: opening one closes the other). The terminal text streams
  live while it is open.
- **link a pane** attaches any existing pane to the task; **unlink** detaches it.
- Panes that have closed stay listed under the task as history.
- Lines wrap, and the view follows new output only while you are at the bottom: scroll up and it stays put
  (with a **↓ latest** button), even as old lines scroll off the top of the window.
- The live view itself is still read-only: the plugin never sends keys or input to a pane.
  **close** and **reopen** are the exception (see below) — this plugin can end a pane's terminal
  and start a new agent process in its place.

## How panes get linked

1. **Tag** (automatic): the pane carries Herdr tokens `kanban_task=<task_id>` (and optionally
   `kanban_board=<board>`):

   ```
   herdr pane report-metadata <pane_id> --source kanban-link \
     --token kanban_task=<task_id> --token kanban_board=<board>
   ```

   Run it without `--ttl-ms` so the tag persists. The `herdr-dispatch` skill can do this when
   it opens a pane for a card.
2. **Manual**: use **link a pane** in the drawer. This sets the same tokens and also stores the
   link in `<HERMES_HOME>/plugin-data/kanban-herdr/links.json` (override with `KANBAN_HERDR_DATA`).

Tokens are a hint for display, not authorization.

## Closing and reopening a pane

Each live pane row has a **close** button, and each closed/history row that can be resumed has a
**reopen** button.

- **close** asks for confirmation, then ends the pane's terminal (`herdr pane close`). Before
  closing, the plugin checks whether the pane is running a supported agent (currently Claude Code
  or Codex) with a resumable session id, and if so captures `{agent kind, session id, cwd,
  workspace}` into `<HERMES_HOME>/plugin-data/kanban-herdr/closed_sessions.json` (override with
  `KANBAN_HERDR_DATA`, same as `links.json`). A pane with nothing resumable (a plain shell, or an
  agent kind this plugin doesn't know how to resume) still closes; it just has no reopen option
  afterward. **This is destructive and not undoable by this plugin**: once a pane is closed, Herdr
  itself has no server-side record of the agent that was in it, so if the session wasn't captured
  first (or capture failed), the work in that pane is gone.
- **reopen** looks up the captured record for that pane's history row, creates a new Herdr tab in
  the same workspace, starts a fresh agent process there, and resumes the captured session into
  it (`claude --resume <id>` / `codex resume <id>`). The new pane is tagged with the task's tokens
  immediately, so it shows up as a live pane on the next refresh. The captured record is consumed
  on a successful reopen — it cannot be replayed into a second pane.

Only Claude Code and Codex sessions are supported; closing a pane running anything else (or a
plain shell) is safe but gives no reopen option. The close/reopen actions always go through the
`herdr` CLI directly (never a cached/stale view), so they need the same `herdr` command this
plugin already resolves for everything else (see "Install" below).

**Security implications of close/reopen:** this plugin can now terminate a running agent's
terminal and launch a new process (a new agent CLI invocation, resuming a specific session) on
the dashboard host, triggered by anyone who can open the task drawer. That is a materially larger
capability than the rest of this plugin (which only reads pane text and sets/clears two Herdr
tokens). It does not grant arbitrary command execution — only `herdr pane close`, `herdr tab
create` and `herdr agent start --kind {claude,codex} -- --resume/resume <captured id>` ever run,
with no attacker-controlled arguments beyond ids this plugin itself captured from Herdr — but
anyone who can reach the dashboard's close/reopen endpoints can end another person's agent session
and start a new one in its place. Treat dashboard access accordingly.

## Agent tool: `kanban_herdr_link`

Hermes agents that dispatch work get a tool, so they tag a pane without composing the CLI command:

```
kanban_herdr_link(action="link", pane_id="w1:p2", task_id="t_1a2b3c4d", board="connect")
```

- `link` sets the tag and reads it back (`verified: true`); a stale board from an earlier card is
  cleared when no board is given. `unlink` removes it only if it points at that task. `show` reads it.
- `machine` (a saved machine's label or id) reaches a pane on another machine.
- Bad ids are refused before anything runs, an unknown pane gives `pane_not_found`, and the tool
  never raises. It only sets or clears the two tokens; it never sends input to a pane or closes one.
- It appears when `kanban-herdr` is enabled and the `herdr` command is found. The toolset is
  `kanban_herdr`; plugin toolsets are on by default. Restart the gateway or open a new session to
  pick it up.

Agents that aren't Hermes (Claude Code, Codex) can run the CLI command from "How panes get linked".

## Install

```
git clone https://github.com/zachristmas/hermes-kanban-herdr ~/.hermes/plugins/kanban-herdr
```

Add `kanban-herdr` to `plugins.enabled` in the Hermes config, then **restart the dashboard**.
Unlike front-end-only plugins, this one has a backend (`plugin_api.py`), and Hermes mounts
plugin routes only when the dashboard starts; a rescan is not enough. Front-end changes after
that only need a hard refresh.

The dashboard host must be able to reach Herdr. On macOS and Linux the plugin uses Herdr's unix
socket (`HERDR_SOCKET_PATH`, default `~/.config/herdr/herdr.sock`). On Windows, Hermes' Python has
no unix sockets, so the plugin runs the `herdr` CLI instead (found on `PATH`, under
`~/.herdr/packages/standalone/releases/*/`, or via `HERDR_BIN`). If Herdr can't be reached, the
section says so.

**Windows and saved machines.** Windows OpenSSH (9.5) stalls during authentication when its stdin is a pipe
and the process has no interactive desktop, which is how a boot-time dashboard service starts it; Herdr's
remote probes then time out ("server status probe timed out") even though `ssh host` works in a terminal.
For remote calls the plugin therefore puts Git for Windows' newer `ssh.exe` first on `PATH` when it finds
`C:\Program Files\Git\usr\bin\ssh.exe`. Point `KANBAN_HERDR_SSH_DIR` at another folder holding `ssh.exe`,
or set it to an empty string to use whatever `ssh` is on `PATH`. A remote that still doesn't answer is
named as not reachable and doesn't hold up the local panes.

## Other machines

Panes on the machines saved in Herdr (`herdr machine list`, the ones `herdr --machine <label>`
reaches) are listed too, labelled with the machine name, and can be linked and watched the same
way. They are reached through the `herdr` CLI over SSH, so their live view polls about every
1.5 s, and a machine that doesn't answer is named in the section rather than hiding the rest.
Disabled machines are skipped. Panes are referenced as `<machine id>~<pane id>`.

## Security notes

- Pane text can contain anything the terminal printed, including secrets. The stream is served
  only behind the dashboard's own login (same auth as the rest of the plugin API).
- Pane ids and task/board ids are validated; at most 4 concurrent streams; text is stripped of
  control characters and capped at 256 KB per snapshot.
- The view is a snapshot of the pane's recent screen text, not a raw byte stream. Colours are not
  rendered. With the socket it refreshes on each Herdr event (coalesced); with the CLI transport
  it polls about every 0.7 s.

## Platform

Developed against Herdr 0.9.3 on macOS (socket) and run on Windows (CLI transport). Both use the
same API shapes, because the CLI prints the same JSON the socket returns.

## Testing

```
python3 -m unittest discover -s tests      # API tests against a fake Herdr socket, run over both transports (68)
```

Browser tests (Playwright, `data-testid` selectors) need a dashboard with the plugin enabled
and `HERDR_SOCKET_PATH` pointing at the fake server:

```
python3 tests/fake_herdr.py /tmp/khd-fake.sock &
HERDR_SOCKET_PATH=/tmp/khd-fake.sock hermes dashboard --port 9229 --no-open &
FAKE_HERDR_SOCK=/tmp/khd-fake.sock npm run e2e
```

The remote-machine browser test needs a second fake registered as a saved machine; it is skipped
unless `HERDR_E2E_REMOTE=1` (see the `HERDR_BIN` / `HERDR_STUB_MACHINE*` variables in
`tests/stub_herdr_cli.py`).

Unix socket paths are length-limited on macOS; keep the fake socket path short.

## License

MIT
