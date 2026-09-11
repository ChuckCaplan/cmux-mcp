# Verification

Verified on macOS with Python 3.10, Claude Code 2.1.268, and installed cmux 0.64.22. This report summarizes results without including local paths, process identifiers, or session transcripts.

Before implementation, inspected the installed binary's `help`, `capabilities`, and command help for `rpc`, `read-screen`, `send`, `send-key`, `tree`, `events`, workspace creation, and the list commands. Live capabilities returned **303 RPC methods**. The installed key method is **`surface.send_key`**, not `surface.send_keys`.

| Required check | Observed result |
| --- | --- |
| Direct stdio JSON-RPC | Piped `initialize`, `notifications/initialized`, `tools/list`, and `tools/call`; correct IDs, seven tools, no notification response, only JSON-RPC on stdout, empty server stderr. |
| User-scope registration | Registered with `claude mcp add --scope user cmux -- python3 <absolute-path-to-server>`; Claude wrote the entry to its user configuration. |
| Connection health | `claude mcp list` reported `cmux: … - ✔ Connected`. |
| Real Claude Code session | Fresh `claude -p` session used the user registration and actually called all seven tools. Captured the stream-json tool calls/results; all ordinary calls exited 0, no permission denials. |
| CLI parity shapes | `['list-windows']`, `['rpc','system.ping']`, and `['tree','--all']` succeeded through direct MCP and Claude. Ping returned `{"pong":true}`. |
| Literal `--` separator | Ran `['send','--surface',test_uuid,'--',"printf 'CMUX_MCP_SEPARATOR_%s\\n' 'literal -- value'"]` through MCP, then Enter; read back the standalone line `CMUX_MCP_SEPARATOR_literal -- value`. |
| Streaming timeout | MCP `cmux` with `args:['events']`, `timeout_seconds:3` returned partial stdout in approximately **3.3s**, exit −15, `timed_out:true`, and the exact timeout note. Repeated successfully through Claude. |
| No events orphan | Observed the events child and its separate process group during the direct test, then confirmed that PID was absent with `ps -p` after timeout. A final filtered `ps` found no cmux events processes after the Claude test. |
| Actual terminal input/output | `cmux_send` typed a `printf` command into the disposable surface; `cmux_send_key` Enter executed it; `cmux_read_screen` returned its standalone output lines. Claude independently executed and read `CLAUDE_CMUX_MCP_OK`. |
| Exact read parameters | Generated 80 marker lines. `scrollback:true` included the earliest marker; `scrollback:false` excluded it; `lines:2` returned at most two lines. UUID and `surface:3` targeting both worked. |
| List wrappers | All four `cmux_list.what` values returned parsed JSON using `{}` RPC parameters. |
| Failure handling | Unknown CLI command and nonexistent surface UUID returned exit 1 with readable stderr, no traceback. The invalid surface produced cmux's own `not_found: Workspace not found` error. |

`python3 -B -m unittest -v test_cmux_mcp.py` passed **8 tests**. These additionally exercise `CMUX_BIN`, exact argv/Unicode/empty arguments, literal shell metacharacters, stdin EOF and large input, bounded stdout/stderr under flooding, invalid UTF-8, missing executable, malformed protocol/input errors, help fallback, wrapper parameter construction, and a timeout descendant that ignores SIGTERM and closes its pipes. `ps` confirmed both the fake leader and its stubborn descendant disappeared.

Destructive command reachability was tested **only with the runner mocked**: auth logout/sign-out, VM removal/destruction, and window close arguments reached the runner unchanged. None of those operations was executed. The only close operation was cleanup of the disposable `cmux-mcp-verification` workspace after checking its identity and contents. The active user workspace was preserved.

Not exercised: every individual command or all 303 RPC methods; real SSH/mosh/VM/agent-team sessions; remote hosts, authentication changes, or destructive operations; a 600-second wait. The server uses pipes rather than a PTY, so terminal-dependent behavior remains the installed CLI's responsibility. Output capping and stdin edge cases were exercised with a fake executable, while the required socket/terminal/parity/timeout checks used the real cmux binary.

The test execution sandbox initially blocked the socket with errno 1 and blocked `ps`; these checks were rerun outside that sandbox. No Claude Bash sandbox settings were changed. Raw test output and Claude session transcripts are intentionally excluded from the project.
