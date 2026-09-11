#!/usr/bin/env python3
"""Unrestricted cmux CLI bridge: Python 3 stdlib, newline-delimited stdio MCP."""

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time


DEFAULT_CMUX_BIN = "/Applications/cmux.app/Contents/Resources/bin/cmux"
PROTOCOL_VERSION = "2024-11-05"
OUTPUT_LIMIT = 128 * 1024  # Bytes retained per stream; excess is still drained.
TERMINATE_GRACE = 0.25
KILL_DRAIN_GRACE = 0.25


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }


SURFACE = {
    "type": "string",
    "description": "Target surface UUID or short ref (surface:3); sent as RPC surface_id. Omit for cmux's default target.",
}
LIST_METHODS = {
    "windows": "window.list",
    "workspaces": "workspace.list",
    "panes": "pane.list",
    "tree": "system.tree",
}
TOOLS = [
    tool(
        "cmux",
        "Full, unrestricted access to the installed cmux CLI. Every args element is "
        "passed verbatim as one argv element, including spaces, JSON, flags, and -- "
        "separators; no shell parsing or command allowlist. Use cmux_help and "
        "cmux_methods to discover commands. Returns exit_code, stdout, stderr, "
        "timed_out, and truncation flags. Every command has a timeout (default 30s, "
        "maximum 600s). Streaming/interactive commands (events, feed tui, ssh, mosh, "
        "vm shell, agent teams, etc.) are accepted; a timeout is an expected outcome "
        "for them. On timeout the process group is terminated then killed and "
        "captured partial output is returned with a note. Pipes, not a terminal/PTY, "
        "are provided; stdin_text is optional UTF-8 input, followed by EOF. "
        "Output is capped at 128 KiB per stream with explicit truncation notices.",
        {
            "args": {"type": "array", "items": {"type": "string"}},
            "stdin_text": {"type": "string"},
            "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600, "default": 30},
        },
        ("args",),
    ),
    tool(
        "cmux_help",
        "Discover the installed CLI. Without topic, run cmux help. With a single "
        "command topic (e.g. read-screen), try cmux <topic> --help and fall back to "
        "cmux help on failure or empty output. For nested topics, use cmux with "
        "explicit args, e.g. [\"vm\",\"shell\",\"--help\"]. Each attempt has a 30s timeout.",
        {"topic": {"type": "string"}},
    ),
    tool("cmux_methods", "Run cmux capabilities to discover current socket RPC method names. "
         "All methods remain reachable through cmux args [\"rpc\", method, json_params]. "
         "30s timeout; returns CLI output and parsed JSON when complete."),
    tool(
        "cmux_read_screen",
        "Read real terminal text via cmux rpc surface.read_text. Verified RPC keys: "
        "surface_id (from surface), lines (integer), scrollback (boolean). Omitted "
        "surface uses cmux's default target. lines limits the last N lines; when "
        "lines is supplied, scrollback defaults to true, unless explicitly set. "
        "cmux silently ignores unknown RPC parameters: these are the exact keys "
        "used. Returns CLI output plus parsed result JSON when untruncated. 30s timeout.",
        {"surface": SURFACE, "lines": {"type": "integer", "minimum": 1}, "scrollback": {"type": "boolean"}},
    ),
    tool(
        "cmux_send",
        "Type text via cmux rpc surface.send_text. Verified RPC keys: text, "
        "surface_id (from optional surface). text is passed unchanged in JSON; "
        "use an actual newline to press Enter, or cmux_send_key. Omitted surface "
        "uses cmux's default target. Unknown RPC keys are silently ignored by cmux, "
        "so this wrapper sends only these exact keys. Returns CLI output and parsed "
        "result JSON when complete. 30s timeout.",
        {"text": {"type": "string"}, "surface": SURFACE}, ("text",),
    ),
    tool(
        "cmux_send_key",
        "Send a key via cmux rpc surface.send_key (singular, verified on the installed "
        "binary). Verified RPC keys: key, surface_id (from optional surface). "
        "Examples: enter, tab, escape, ctrl+c. Key names are passed unchanged. "
        "Omitted surface uses cmux's default target. Unknown RPC keys are silently "
        "ignored by cmux, so only these exact keys are sent. Returns CLI output "
        "and parsed result JSON when complete. 30s timeout.",
        {"key": {"type": "string"}, "surface": SURFACE}, ("key",),
    ),
    tool(
        "cmux_list",
        "List cmux objects as JSON: windows -> window.list, workspaces -> "
        "workspace.list, panes -> pane.list, tree -> system.tree. Each verified "
        "RPC is called with {} (no invented parameters), using cmux's default "
        "window/workspace context. For other scopes use the unrestricted cmux tool. "
        "Returns CLI output plus parsed result JSON when complete. 30s timeout.",
        {"what": {"type": "string", "enum": list(LIST_METHODS)}}, ("what",),
    ),
]
TOOL_BY_NAME = {entry["name"]: entry for entry in TOOLS}


def kill_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run_cmux(args, stdin_text=None, timeout_seconds=30):
    """Drain all pipes with a deadline and bounded memory, including after SIGKILL."""
    result = {
        "exit_code": None, "stdout": "", "stderr": "", "timed_out": False,
        "truncated": {"stdout": False, "stderr": False},
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    notes = []
    process = None
    completed = False
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_seconds
    try:
        input_bytes = memoryview(stdin_text.encode("utf-8") if stdin_text is not None else b"")
        process = subprocess.Popen(
            [os.environ.get("CMUX_BIN", DEFAULT_CMUX_BIN)] + args,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, bufsize=0,
        )
        for name in ("stdout", "stderr"):
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        if process.stdin is not None:
            if input_bytes:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()

        kill_at = None
        drain_until = None
        while True:
            now = time.monotonic()
            if not result["timed_out"] and now >= deadline:
                result["timed_out"] = True
                notes.append(
                    "timed out after %gs; output may be partial; "
                    "this command may stream or be interactive." % timeout_seconds
                )
                kill_group(process, signal.SIGTERM)
                kill_at = now + TERMINATE_GRACE
            if kill_at is not None and now >= kill_at:
                # Do this even if the group leader exited on SIGTERM: descendants
                # may ignore SIGTERM and may have closed their inherited pipes.
                kill_group(process, signal.SIGKILL)
                kill_at = None
                drain_until = now + KILL_DRAIN_GRACE
            if drain_until is not None and now >= drain_until:
                break
            if not selector.get_map() and process.poll() is not None:
                if not result["timed_out"] or drain_until is not None:
                    break
            next_deadline = kill_at or drain_until or deadline
            wait = max(0, min(0.05, next_deadline - now))
            for event, _ in selector.select(wait):
                stream, name = event.fileobj, event.data
                if name == "stdin":
                    try:
                        written = os.write(stream.fileno(), input_bytes[:4096])
                        input_bytes = input_bytes[written:]
                    except BrokenPipeError:
                        input_bytes = input_bytes[len(input_bytes):]
                    except BlockingIOError:
                        continue
                    if not input_bytes:
                        selector.unregister(stream)
                        stream.close()
                else:
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    remaining = OUTPUT_LIMIT - len(buffers[name])
                    buffers[name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        result["truncated"][name] = True
        try:
            result["exit_code"] = process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            notes.append("Process did not become reapable within 1s of SIGKILL.")
        completed = True
    except (OSError, ValueError) as exc:
        result["stderr"] = "Could not run cmux: %s" % exc
    finally:
        if process is not None:
            if not completed:
                kill_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        selector.close()
    for name, data in buffers.items():
        captured = data.decode("utf-8", errors="replace")
        if captured and result[name] and not captured.endswith("\n"):
            captured += "\n"
        result[name] = captured + result[name]
        if result["truncated"][name]:
            notes.append("%s truncated at %d bytes; remaining output discarded." % (name, OUTPUT_LIMIT))
    if notes:
        result["note"] = " ".join(notes)
    return result


class RPCError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_arguments(name, arguments):
    if not isinstance(name, str) or name not in TOOL_BY_NAME:
        raise RPCError(-32602, "Unknown tool: %s" % name)
    if not isinstance(arguments, dict):
        raise RPCError(-32602, "arguments must be an object")
    schema = TOOL_BY_NAME[name]["inputSchema"]
    for key in schema["required"]:
        if key not in arguments:
            raise RPCError(-32602, "Missing required argument: " + key)
    for key, value in arguments.items():
        spec = schema["properties"].get(key)
        if spec is None:
            raise RPCError(-32602, "Unknown argument: " + key)
        kind = spec["type"]
        validator = {
            "string": lambda: isinstance(value, str),
            "array": lambda: isinstance(value, list) and all(isinstance(item, str) for item in value),
            "number": lambda: type(value) is int or (type(value) is float and math.isfinite(value)),
            "integer": lambda: type(value) is int,
            "boolean": lambda: type(value) is bool,
        }.get(kind)
        if validator is None:
            # An unsupported server schema is an implementation error, not bad input.
            raise RuntimeError("Unsupported schema type: %s" % kind)
        valid = validator()
        if valid and kind in ("number", "integer"):
            valid = (
                ("minimum" not in spec or value >= spec["minimum"])
                and ("exclusiveMinimum" not in spec or value > spec["exclusiveMinimum"])
                and ("maximum" not in spec or value <= spec["maximum"])
                and ("exclusiveMaximum" not in spec or value < spec["exclusiveMaximum"])
            )
        if not valid or ("enum" in spec and value not in spec["enum"]):
            raise RPCError(-32602, "Invalid argument %s; expected %s" % (key, json.dumps(spec)))


def add_json_result(result):
    if result["exit_code"] == 0 and not result["timed_out"] and not result["truncated"]["stdout"]:
        try:
            result["result"] = json.loads(
                result["stdout"], parse_constant=reject_constant, parse_float=finite_float,
            )
        except (ValueError, RecursionError):
            pass
    return result


def call_tool(name, arguments):
    validate_arguments(name, arguments)
    if name == "cmux":
        result = run_cmux(**arguments)
    elif name == "cmux_help":
        topic = arguments.get("topic")
        result = run_cmux([topic, "--help"] if topic else ["help"])
        if topic and (result["exit_code"] != 0 or result["timed_out"] or not result["stdout"].strip()):
            attempt = result
            result = run_cmux(["help"])
            result["topic_help_attempt"] = attempt
            result["note"] = (result.get("note", "") + " Topic help unavailable; fell back to cmux help.").strip()
    elif name == "cmux_methods":
        result = add_json_result(run_cmux(["capabilities"]))
    else:
        params = {}
        if name == "cmux_list":
            method = LIST_METHODS[arguments["what"]]
        else:
            method = {
                "cmux_read_screen": "surface.read_text",
                "cmux_send": "surface.send_text",
                "cmux_send_key": "surface.send_key",
            }[name]
            params = {"surface_id" if key == "surface" else key: value for key, value in arguments.items()}
            if name == "cmux_read_screen" and "lines" in params:
                params.setdefault("scrollback", True)
        result = add_json_result(run_cmux(["rpc", method, json.dumps(params, ensure_ascii=True)]))
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=True, allow_nan=False)}],
        "isError": result["exit_code"] != 0 or result["timed_out"],
    }


def dispatch(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        raise RPCError(-32600, "Invalid JSON-RPC request")
    params = message.get("params", {})
    if not isinstance(params, dict):
        raise RPCError(-32602, "params must be an object")
    method = message["method"]
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "cmux-mcp", "version": "1.0.0"},
            "instructions": "Use cmux_help and cmux_methods to discover the installed CLI. "
            "The cmux tool exposes all commands without filtering. Streaming and interactive "
            "commands may time out and return partial output. RPC parameters must be exact: "
            "cmux silently ignores unknown keys.",
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(params.get("name"), params.get("arguments", {}))
    if method == "ping" or method.startswith("notifications/"):
        return {}
    raise RPCError(-32601, "Method not found: " + method)


def write_message(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def reject_constant(value):
    raise ValueError("Invalid JSON constant: " + value)


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds finite float range: " + value)
    return number


def serve():
    for line in sys.stdin.buffer:
        try:
            message = json.loads(line, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            write_message({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        notification = isinstance(message, dict) and "id" not in message
        request_id = message.get("id") if isinstance(message, dict) else None
        try:
            if request_id is not None and type(request_id) not in (str, int):
                request_id = None
                raise RPCError(-32600, "id must be a string, integer, or null")
            response = {"jsonrpc": "2.0", "id": request_id, "result": dispatch(message)}
        except RPCError as exc:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:
            print("cmux-mcp: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "Internal error"}}
        if not notification:
            write_message(response)


def stop(_signum, _frame):
    # Raising unwinds run_cmux's finally, killing/reaping the current child group.
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        serve()
    except BrokenPipeError:
        # Avoid an additional flush error during interpreter shutdown.
        os._exit(0)
