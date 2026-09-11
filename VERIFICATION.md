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

`python3 -B -m unittest -v test_cmux_mcp.py` passed **12 tests**. These additionally exercise `CMUX_BIN`, exact argv/Unicode/empty arguments, literal shell metacharacters, stdin EOF and large input, bounded stdout/stderr under flooding, invalid UTF-8, missing executable, malformed protocol/input errors, help fallback, wrapper parameter construction, and a timeout descendant that ignores SIGTERM and closes its pipes. `ps` confirmed both the fake leader and its stubborn descendant disappeared. Four tests added in the revision below cover strict-JSON tool content, schema-driven numeric bounds, unsupported schema types, and output preservation across an injected read failure.

Destructive command reachability was tested **only with the runner mocked**: auth logout/sign-out, VM removal/destruction, and window close arguments reached the runner unchanged. None of those operations was executed. The only close operation was cleanup of the disposable `cmux-mcp-verification` workspace after checking its identity and contents. The active user workspace was preserved.

Not exercised: every individual command or all 303 RPC methods; real SSH/mosh/VM/agent-team sessions; remote hosts, authentication changes, or destructive operations; a 600-second wait. The server uses pipes rather than a PTY, so terminal-dependent behavior remains the installed CLI's responsibility. Output capping and stdin edge cases were exercised with a fake executable, while the required socket/terminal/parity/timeout checks used the real cmux binary.

The test execution sandbox initially blocked the socket with errno 1 and blocked `ps`; these checks were rerun outside that sandbox. No Claude Bash sandbox settings were changed. Raw test output and Claude session transcripts are intentionally excluded from the project.

## Post-review revision (2026-09-11)

A code review of the initial release produced one correctness fix and three smaller changes, all against installed cmux 0.64.22.

| Change | Detail |
| --- | --- |
| Strict JSON on tool content | cmux stdout is parsed with `parse_constant` and `parse_float` guards, and tool content is serialized with `allow_nan=False`. Previously `NaN`, `Infinity`, `-Infinity`, or an exponent overflow such as `1e400` in cmux output round-tripped into `content[0].text` as tokens that Python accepts but strict parsers (including the client's `JSON.parse`) reject. Such output now degrades to raw `stdout` with no parsed `result`; a string containing the text `NaN` is unaffected. |
| Schema-driven numeric bounds | `validate_arguments` reads `minimum`/`exclusiveMinimum`/`maximum`/`exclusiveMaximum` from the tool schema instead of hardcoding them. Shipped behavior is unchanged: `timeout_seconds` still requires `0 < v <= 600`, `lines` still requires `v >= 1`, and `bool` is still rejected. |
| Unsupported schema types | A schema type with no validator raised a bare `KeyError`. It now raises an explicit `RuntimeError`, still reported as `-32603`, since an unsupported schema is a server defect rather than a client argument error. |
| Output assembly | A newline now separates captured stderr from an appended `Could not run cmux:` message. |

`README.md` also gained an explicit paragraph on sequential execution: while a command runs the server does not read further requests or answer pings, and MCP cancellation notifications do not interrupt a running command. Input lines remain uncapped, which assumes a trusted local client. These are documented limitations, not defects.

| Re-verified after the revision | Observed result |
| --- | --- |
| Regression suite | 12 tests pass. Ran the full suite 5 times and the timing-sensitive injected-read-failure test 8 times standalone; no flakiness. |
| Live end-to-end, real binary | Piped `initialize`, `tools/list`, and `tools/call` for `cmux_methods`, `cmux_list panes`, `rpc system.ping`, and `events` with a 2s timeout into a fresh `cmux_mcp.py` subprocess. Every response frame **and** every `content[0].text` strict-parsed with node's `JSON.parse`. `isError` was true only for the timeout. Server exited 0 with empty stderr. |
| Input hardening | A request carrying a non-finite `timeout_seconds` was rejected at the transport as `-32700`, and non-finite values reaching validation are rejected as `-32602`. |
| Stdin backpressure | 4 MiB of `stdin_text` against a child that exits without reading, and against one that reads 10 bytes then exits, both returned in roughly 0.3s via `EPIPE` rather than blocking to the deadline. |
| No orphans | Re-confirmed after the change, including with sub-millisecond timeouts. |

Not re-run after the revision: the Claude Code session rows and the live terminal send/read round-trip in the table above, which describe the pre-change build. The revision does not touch RPC parameter construction, the subprocess lifecycle, or the process-group termination path. A running server keeps whichever revision it was started with; restart the client to load new code.
