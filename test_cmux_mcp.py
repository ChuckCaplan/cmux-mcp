"""Stdlib regression tests. Run: python3 -B -m unittest -v test_cmux_mcp.py"""

import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cmux_mcp as server


FAKE_CLI = r'''
import json, os, signal, sys, time
mode = sys.argv[1] if len(sys.argv) > 1 else ''
if mode == 'flood':
    for _ in range(100):
        os.write(1, b'o' * 4096)
        os.write(2, b'e' * 4096)
elif mode == 'hang':
    child = os.fork()
    if child == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.write(2, ('descendant=%d\n' % os.getpid()).encode())
        os.close(0); os.close(1); os.close(2)
        while True:
            time.sleep(1)
    os.write(1, ('leader=%d\n' % os.getpid()).encode())
    while True:
        time.sleep(1)
elif mode == 'bad-utf8':
    os.write(1, b'hello\xffworld')
elif mode == 'fail':
    print('intentional CLI failure', file=sys.stderr)
    sys.exit(7)
elif mode == 'read-error':
    os.write(1, b'captured stdout')
    os.write(2, b'captured stderr')
    time.sleep(0.1)
    os.write(1, b'trigger another read')
    time.sleep(10)
else:
    print(json.dumps({'args':sys.argv[1:], 'stdin':sys.stdin.read()}))
'''


def payload(result):
    return json.loads(result['content'][0]['text'])


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cmux-mcp-test-')
        self.binary = Path(self.temp.name) / 'fake cmux'
        self.binary.write_text('#!' + sys.executable + '\n' + FAKE_CLI)
        self.binary.chmod(0o700)
        self.env = patch.dict(os.environ, {'CMUX_BIN': str(self.binary)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_verbatim_argv_stdin_and_no_shell(self):
        marker = Path(self.temp.name) / 'must-not-exist'
        args = ['ssh', 'host', '--', 'ls -la', '', '"quotes"', "'quotes'",
                '{"hello": "world"}', '$(touch %s)' % marker, '`touch %s`' % marker,
                '; touch %s' % marker, '\\n', '日本語']
        input_text = 'stdin with spaces\nUnicode ☃\n' * 20000
        # Check large stdin without generating correspondingly large stdout.
        small = 'stdin with spaces\nUnicode ☃\n'
        result = payload(server.call_tool('cmux', {'args': args, 'stdin_text': small}))
        self.assertEqual(json.loads(result['stdout']), {'args': args, 'stdin': small})
        self.assertFalse(marker.exists())
        result = server.run_cmux([], stdin_text=input_text)
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['truncated']['stdout'])

    def test_flood_is_drained_and_bounded(self):
        result = server.run_cmux(['flood'], timeout_seconds=3)
        self.assertEqual(result['exit_code'], 0)
        for stream in ('stdout', 'stderr'):
            self.assertEqual(len(result[stream]), server.OUTPUT_LIMIT)
            self.assertTrue(result['truncated'][stream])
            self.assertIn(stream + ' truncated', result['note'])

    def assert_dead(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            check = subprocess.run(['ps', '-p', str(pid), '-o', 'pid=,stat=,command='],
                                   capture_output=True, text=True, timeout=2)
            if not check.stdout.strip():
                return
            time.sleep(0.05)
        self.fail('Surviving process: ' + check.stdout)

    def test_timeout_kills_descendant_after_leader_exits(self):
        start = time.monotonic()
        result = server.run_cmux(['hang'], timeout_seconds=0.5)
        self.assertLess(time.monotonic() - start, 3)
        self.assertTrue(result['timed_out'])
        self.assertIn('timed out after 0.5s; output may be partial;', result['note'])
        self.assertIn('leader=', result['stdout'])
        self.assertIn('descendant=', result['stderr'])
        for stream in ('stdout', 'stderr'):
            self.assert_dead(int(result[stream].strip().split('=')[1]))

    def test_errors_and_utf8(self):
        result = server.call_tool('cmux', {'args': ['fail']})
        self.assertTrue(result['isError'])
        self.assertEqual(payload(result)['exit_code'], 7)
        self.assertIn('intentional CLI failure', payload(result)['stderr'])
        self.assertEqual(server.run_cmux(['bad-utf8'])['stdout'], 'hello\ufffdworld')
        self.assertIsNone(server.run_cmux(['nul\x00'])['exit_code'])
        with patch.dict(os.environ, {'CMUX_BIN': '/nonexistent/cmux'}):
            result = server.run_cmux([])
            self.assertIsNone(result['exit_code'])
            self.assertIn('Could not run cmux', result['stderr'])

    def test_input_validation(self):
        for args in ({}, {'args': 'help'}, {'args': [1]}, {'args': [], 'bogus': 1}):
            with self.subTest(args=args), self.assertRaises(server.RPCError):
                server.call_tool('cmux', args)
        for timeout in (0, -1, 601, True, '3', None, float('nan'), float('inf'), 10**1000):
            with self.subTest(timeout=str(timeout)[:20]), self.assertRaises(server.RPCError):
                server.call_tool('cmux', {'args': [], 'timeout_seconds': timeout})
        for args in ({'lines': True}, {'lines': 0}, {'scrollback': 'true'}, {'surface_id': 'surface:1'}):
            with self.subTest(args=args), self.assertRaises(server.RPCError):
                server.call_tool('cmux_read_screen', args)

    def test_numeric_validation_uses_schema_bounds(self):
        for timeout in (0.1, 30, 600):
            server.validate_arguments('cmux', {'args': [], 'timeout_seconds': timeout})
        cases = [
            ('cmux', 'timeout_seconds', {'args': []},
             {'type': 'number', 'minimum': -2, 'exclusiveMaximum': 700},
             (-2, 0, 650, 699.5), (-2.1, 700)),
            ('cmux_read_screen', 'lines', {},
             {'type': 'integer', 'exclusiveMinimum': -3, 'maximum': 2},
             (-2, 0, 2), (-3, 3, 1.5)),
        ]
        for name, key, base, spec, accepted, rejected in cases:
            original = server.TOOL_BY_NAME[name]['inputSchema']['properties'][key]
            with patch.dict(original, spec, clear=True):
                for value in accepted:
                    with self.subTest(name=name, accepted=value):
                        server.validate_arguments(name, {**base, key: value})
                for value in rejected:
                    with self.subTest(name=name, rejected=value), self.assertRaises(server.RPCError) as error:
                        server.validate_arguments(name, {**base, key: value})
                    self.assertEqual(error.exception.code, -32602)

    def test_unsupported_schema_is_an_internal_error(self):
        properties = server.TOOL_BY_NAME['cmux']['inputSchema']['properties']
        request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'cmux', 'arguments': {'args': [], 'future': {}}}}
        incoming = io.TextIOWrapper(io.BytesIO((json.dumps(request) + '\n').encode()))
        with patch.dict(properties, {'future': {'type': 'object'}}), \
                patch.object(sys, 'stdin', incoming), \
                patch.object(sys, 'stderr', io.StringIO()) as errors, \
                patch.object(server, 'write_message') as write:
            server.serve()
        self.assertEqual(write.call_args.args[0]['error']['code'], -32603)
        self.assertIn('Unsupported schema type: object', errors.getvalue())

    def test_tool_content_remains_strict_json(self):
        for name, arguments in [('cmux_methods', {}), ('cmux_list', {'what': 'tree'})]:
            for token in ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400'):
                stdout = '{"nested": [0, {"value": %s}]}' % token
                successful = {'exit_code': 0, 'stdout': stdout, 'stderr': '', 'timed_out': False,
                              'truncated': {'stdout': False, 'stderr': False}}
                with self.subTest(name=name, token=token), \
                        patch.object(server, 'run_cmux', return_value=successful):
                    response = server.call_tool(name, arguments)
                    result = json.loads(response['content'][0]['text'], parse_constant=server.reject_constant)
                    self.assertEqual(result['stdout'], stdout)
                    self.assertNotIn('result', result)
                    self.assertFalse(response['isError'])
        stdout = '{"small": 1.5, "large": 1e300, "text": "NaN and Infinity"}'
        successful = {'exit_code': 0, 'stdout': stdout, 'stderr': '', 'timed_out': False,
                      'truncated': {'stdout': False, 'stderr': False}}
        with patch.object(server, 'run_cmux', return_value=successful):
            response = server.call_tool('cmux_methods', {})
        result = json.loads(response['content'][0]['text'], parse_constant=server.reject_constant)
        self.assertEqual(result['result'], json.loads(stdout))

    def test_read_error_preserves_output_and_cleans_up(self):
        original_popen, original_read = subprocess.Popen, os.read
        processes, output_fds, captured = [], set(), set()

        def start(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            output_fds.update((process.stdout.fileno(), process.stderr.fileno()))
            return process

        def read(fd, size):
            if fd in output_fds and captured == output_fds:
                raise OSError('injected read failure')
            chunk = original_read(fd, size)
            if fd in output_fds and chunk:
                captured.add(fd)
            return chunk

        with patch.object(server.subprocess, 'Popen', side_effect=start), \
                patch.object(server.os, 'read', side_effect=read):
            response = server.call_tool('cmux', {'args': ['read-error'], 'timeout_seconds': 3})
        result = payload(response)
        self.assertTrue(response['isError'])
        self.assertFalse(result['timed_out'])
        self.assertTrue(result['stdout'].startswith('captured stdout'))
        self.assertEqual(result['stderr'], 'captured stderr\nCould not run cmux: injected read failure')
        self.assertIsNotNone(processes[0].returncode)
        self.assert_dead(processes[0].pid)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)

    def test_wrapper_arguments_and_destructive_construction_only(self):
        successful = {'exit_code': 0, 'stdout': '{}', 'stderr': '', 'timed_out': False,
                      'truncated': {'stdout': False, 'stderr': False}}
        with patch.object(server, 'run_cmux', return_value=successful) as run:
            server.call_tool('cmux_read_screen', {'surface': 'surface:3', 'lines': 2})
            self.assertEqual(run.call_args.args[0][:2], ['rpc', 'surface.read_text'])
            self.assertEqual(json.loads(run.call_args.args[0][2]),
                             {'surface_id': 'surface:3', 'lines': 2, 'scrollback': True})
            server.call_tool('cmux_read_screen', {'lines': 2, 'scrollback': False})
            self.assertFalse(json.loads(run.call_args.args[0][2])['scrollback'])
            for tool, argument, method in [('cmux_send', 'text', 'surface.send_text'),
                                           ('cmux_send_key', 'key', 'surface.send_key')]:
                server.call_tool(tool, {argument: 'enter', 'surface': 'surface:3'})
                self.assertEqual(run.call_args.args[0][:2], ['rpc', method])
                self.assertEqual(json.loads(run.call_args.args[0][2]),
                                 {argument: 'enter', 'surface_id': 'surface:3'})
            for what, method in server.LIST_METHODS.items():
                server.call_tool('cmux_list', {'what': what})
                run.assert_called_with(['rpc', method, '{}'])
            # Never execute these; only verify construction with the runner mocked.
            for args in (['auth', 'logout'], ['vm', 'rm', 'unexecuted'],
                         ['close-window', '--window', 'unexecuted'],
                         ['rpc', 'auth.sign_out'], ['rpc', 'vm.destroy', '{"id":"unexecuted"}']):
                server.call_tool('cmux', {'args': args})
                run.assert_called_with(args=args)

    def test_help_fallback(self):
        failed = {'exit_code': 1, 'stdout': '', 'stderr': 'unknown topic', 'timed_out': False}
        ok = {'exit_code': 0, 'stdout': 'general help', 'stderr': '', 'timed_out': False}
        with patch.object(server, 'run_cmux', side_effect=[failed, ok]) as run:
            result = server.call_tool('cmux_help', {'topic': 'unavailable'})
            self.assertEqual([c.args[0] for c in run.call_args_list], [['unavailable', '--help'], ['help']])
            self.assertFalse(result['isError'])
            self.assertEqual(payload(result)['topic_help_attempt'], failed)

    def test_protocol_pipe_and_notification_execution(self):
        messages = [
            {'jsonrpc': '2.0', 'id': 0, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'method': 'unknown-notification'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'cmux', 'arguments': {'args': ['fail']}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'unknown'},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'unknown'}},
            {'jsonrpc': '2.0', 'id': None, 'method': 'ping'},
        ]
        data = ''.join(json.dumps(m) + '\n' for m in messages) + '{bad json}\n'
        process = subprocess.run([sys.executable, '-B', str(Path(server.__file__).resolve())],
                                 input=data, text=True, capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(process.stderr, '')
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [0, 2, 3, 4, 5, None, None])
        self.assertEqual(len(responses[1]['result']['tools']), 7)
        self.assertTrue(responses[2]['result']['isError'])
        self.assertEqual(responses[3]['error']['code'], -32601)
        self.assertEqual(responses[-1]['error']['code'], -32700)
        with patch.object(server, 'call_tool', return_value={}) as call:
            server.dispatch({'jsonrpc': '2.0', 'method': 'tools/call', 'params': {'name': 'cmux', 'arguments': {'args': []}}})
            call.assert_called_once_with('cmux', {'args': []})


if __name__ == '__main__':
    unittest.main()
