# cmux MCP

A single-file Python 3 stdlib server exposing the **entire installed cmux CLI** to Claude Code. No pip dependencies, command filtering, or cmux changes.

Requires macOS, Python 3, cmux, and Claude Code. Live-tested against cmux 0.64.22 (102) on macOS with Python 3.10; the regression suite additionally runs on Python 3.10-3.14 across macOS and Linux in CI. Later cmux versions are expected to work: the server passes commands through to the installed CLI without interpreting them, and discovers commands and RPC method names at runtime via `cmux_help` and `cmux_methods`.

Register it, replacing the example path with the absolute path to your checkout:

```sh
claude mcp add --scope user cmux -- python3 /absolute/path/to/cmux-mcp/cmux_mcp.py
claude mcp list
```

Start a new Claude Code session after registration. No Bash sandbox settings are needed: Claude Code launches stdio MCP servers as direct subprocesses outside its Bash tool sandbox, so the CLI can reach cmux's Unix socket. cmux must be running with socket access enabled. A sandbox around Claude Code itself is a separate constraint.

The default binary is `/Applications/cmux.app/Contents/Resources/bin/cmux`. Set `CMUX_BIN` in the server environment to override it; its value is an executable path, not a command with arguments:

```sh
claude mcp add --scope user --env CMUX_BIN=/absolute/path/to/cmux cmux -- python3 /absolute/path/to/cmux_mcp.py
```

Use that alternative registration command when adding the server with an override. Other environment variables (including cmux socket/auth/context settings) are inherited unchanged.

| Tool | Arguments / operation |
| --- | --- |
| `cmux` | Required `args: string[]`; optional `stdin_text`, `timeout_seconds` (default 30, >0 and <=600) |
| `cmux_help` | Optional single-command `topic`; command help with fallback to general help |
| `cmux_methods` | `cmux capabilities`, including available RPC method names |
| `cmux_read_screen` | Optional `surface`, `lines`, `scrollback`; RPC `surface.read_text` |
| `cmux_send` | Required `text`, optional `surface`; RPC `surface.send_text` |
| `cmux_send_key` | Required `key`, optional `surface`; RPC `surface.send_key` (singular) |
| `cmux_list` | Required `what`: `windows`, `workspaces`, `panes`, or `tree` |

`args` goes directly to a subprocess argv list, without a shell. Spaces, quoting characters, JSON, empty arguments, and `--` remain intact. For example:

```json
{"args":["rpc","system.ping"]}
{"args":["tree","--all"]}
{"args":["send","--surface","surface:3","--","text with spaces"]}
{"args":["ssh","host","--","ls -la"]}
{"args":["events"],"timeout_seconds":3}
```

Every command has a timeout, including streaming commands (`events`, `feed tui`) and interactive commands (`ssh`, `mosh`, `vm shell`, agent teams, etc.). At the deadline the server sends SIGTERM to the child process group, waits 0.25s, and sends SIGKILL to the group even if its leader has exited. Pipe draining and child reaping are also bounded. The returned note says: `timed out after Ns; output may be partial; this command may stream or be interactive.` Timeouts set `timed_out` and `isError`, but are an expected way to sample a stream. The timeout does not undo actions already accepted by cmux or stop work launched inside the cmux app.

Input/output uses pipes, not a PTY. `stdin_text` supplies UTF-8 input followed by EOF; otherwise stdin is immediately EOF. Commands that require an attached terminal may report a CLI error. They are still passed through. Use cmux's surface send/read tools to interact with terminals managed by the app.

Tool content is a JSON text object containing `exit_code` (negative for a signal, null if no exit status is available), `stdout`, `stderr`, `timed_out`, and per-stream `truncated` flags. The server retains the first 128 KiB of each output stream and drains/discards the rest, with an explicit note when truncated. Invalid UTF-8 output is decoded with replacement characters. RPC wrappers and capabilities also include parsed `result` JSON when stdout is complete. CLI failures are readable tool results with `isError: true`; malformed MCP requests/arguments receive JSON-RPC errors.

Wrapper RPC keys are verified against the installed binary: `surface` maps to `surface_id`; other keys are exactly `text`, `key`, `lines`, and `scrollback`. `lines` defaults `scrollback` to true unless explicitly supplied. `cmux_list` sends `{}` to `window.list`, `workspace.list`, `pane.list`, or `system.tree`. cmux silently ignores unknown RPC keys, so wrapper arguments are checked for typos. Arbitrary raw RPC parameters remain available through `cmux`.

The transport follows [MCP 2024-11-05 stdio](https://modelcontextprotocol.io/specification/2024-11-05/basic/transports): one JSON-RPC object per line, notifications receive no replies, and stdout contains protocol messages only. See [the cmux socket reference](https://manaflow-ai-cmux.mintlify.app/automation/socket-api) for RPC context; the installed CLI's help/capabilities remain authoritative.

Calls are handled sequentially: while a command runs, the server does not read further requests or answer pings. MCP cancellation notifications do not interrupt commands; they run until completion or their configured timeout. Use a short `timeout_seconds` when sampling streams such as `events`. SIGINT or SIGTERM to the server terminates the active child process group and exits the server. Input lines have no size limit; this stdio transport assumes a trusted local client.

Run the stdlib regression suite with `python3 -B -m unittest -v test_cmux_mcp.py`. It needs no pip packages and no cmux install: a fake CLI is substituted through `CMUX_BIN`. [GitHub Actions](.github/workflows/tests.yml) runs it on every push and pull request to `main`. See [VERIFICATION.md](VERIFICATION.md) for completed live cmux and Claude Code tests and the untested scope. Only `cmux_mcp.py` is needed to run the server.

Licensed under the [MIT License](LICENSE).
