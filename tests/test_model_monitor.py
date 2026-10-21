"""Controlled transport faults, no external API, scientific task or model credentials."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, Request, build_opener

from science_innovation_exam import runner
from science_innovation_exam.agents import prepare_agent
from science_innovation_exam.config import DEFAULTS, load_config
from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.container_service.interface import ContainerService
from science_innovation_exam.model_activity import GatewayActivity, ProtocolProgress
from science_innovation_exam.model_gateway import _Server, _Trace
from science_innovation_exam.model_monitor import ModelFault, ModelMonitor
from tests.helpers import _make_package, _write_json
from tests.test_container_service import FakeContainerService, FakeHandle


def options(**extra):
    value = dict(DEFAULTS['model_monitor'])
    value.update(check_seconds=0.02, progress_write_seconds=0.02,
                 first_response_warning_seconds=0.08, first_response_timeout_seconds=0.25,
                 idle_warning_seconds=0.08, idle_timeout_seconds=0.25,
                 request_warning_seconds=0.5, request_timeout_seconds=1.0,
                 observation_warning_seconds=0.1, observation_timeout_seconds=0.3,
                 connect_timeout_seconds=0.1, retry_backoff_seconds=0.01)
    value.update(extra)
    return value


@contextmanager
def gateway(root, mode, settings=None):
    stop = threading.Event()
    requests = []

    class API(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers['Content-Length'])))
            if mode == 'silent':
                stop.wait(5)
                return
            if mode == 'errors' or (mode == 'recover' and len(requests) < 2):
                self.send_response(503)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if mode in ('broken', 'progress', 'slow_complete'):
                self.wfile.write(b'data: {"type":"response.created"}\n\n')
                self.wfile.flush()
            if mode == 'broken':
                return
            try:
                if mode == 'slow_complete':
                    stop.wait(0.12)  # Long thinking is allowed below the configured deadline.
                elif mode == 'benchmark':
                    for _ in range(10):
                        self.wfile.write(b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n')
                        self.wfile.flush()
                        stop.wait(0.01)
                elif mode == 'stalled_after_event':
                    self.wfile.write(b'data: {"type":"response.created"}\n\n')
                    self.wfile.flush()
                if mode in ('keepalive', 'progress', 'stalled_after_event'):
                    while not stop.wait(0.025):
                        payload = (b'data: {"type":"response.output_text.delta","delta":"SECRET_REPLY"}\n\n'
                                   if mode == 'progress' else b': ping\n\ndata: {"type":"response.in_progress"}\n\n')
                        self.wfile.write(payload)
                        self.wfile.flush()
                    return
                self.wfile.write(b'data: {"type":"response.completed"}\n\n')
                self.wfile.flush()
            except OSError:
                pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), API)
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    settings = settings or options()
    state = root / 'agent-state/model-activity.json'
    trace = _Trace(root / 'model-calls.jsonl')
    activity = GatewayActivity(state, settings, trace)
    server = _Server(('127.0.0.1', 0), 'http://127.0.0.1:%d/v1' % upstream.server_port,
                     settings['connect_timeout_seconds'], trace, None, activity)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = 'http://127.0.0.1:%d/v1/responses' % server.server_port
    try:
        yield url, activity, requests
    finally:
        stop.set()
        server.shutdown()
        upstream.shutdown()
        server.server_close()
        upstream.server_close()
        worker.join(2)
        thread.join(2)
        activity.close()
        trace.close()


def call(url):
    request = Request(url, data=b'{"model":"fake","stream":true,"input":"SECRET_PROMPT"}',
                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer SECRET_KEY'})
    try:
        return build_opener(ProxyHandler({})).open(request, timeout=3).read()
    except OSError:
        return b''


class ModelMonitorTests(unittest.TestCase):
    def test_transport_faults_and_retries_have_precise_metadata(self):
        cases = [('silent', 'first_response_timeout', 1), ('keepalive', 'first_response_timeout', 1),
                 ('stalled_after_event', 'idle_timeout', 1), ('broken', 'upstream_incomplete_stream', 1),
                 ('progress', 'request_timeout', 1), ('errors', 'upstream_http_error', 3),
                 ('recover', None, 2), ('slow_complete', None, 1)]
        for mode, reason, count in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with gateway(root, mode, options(retry_attempts=2) if mode in ('errors', 'recover') else None) as (url, activity, requests):
                    began = time.monotonic()
                    call(url)
                    until = time.monotonic() + 1
                    while not activity.finished and time.monotonic() < until:
                        time.sleep(0.005)
                    with activity.lock:
                        record = activity.finished[-1]
                    self.assertEqual(record['reason'], reason, record)
                    self.assertEqual(len(requests), count)
                    self.assertLess(time.monotonic() - began, 2)
                    if mode == 'broken':
                        self.assertEqual(record['http_status'], 200)
                        self.assertFalse(record['protocol_complete'])
                    if mode in ('keepalive', 'stalled_after_event'):
                        self.assertGreater(record['response_bytes'], 0)
                    if reason and reason.endswith('timeout'):
                        self.assertEqual(record['upstream_cancellation'], 'unknown')
                metadata = (root / 'model-calls.jsonl').read_text() + (root / 'agent-state/model-activity.json').read_text()
                for secret in ('SECRET_PROMPT', 'SECRET_REPLY', 'SECRET_KEY'):
                    self.assertNotIn(secret, metadata)

    def test_protocol_fragmentation_heartbeat_and_completion(self):
        parser = ProtocolProgress('text/event-stream')
        self.assertFalse(parser.feed(b': ping\r\n\r\ndata: {"type":"response.in_progress"}\n\n'))
        self.assertFalse(parser.feed(b'data: {"type":"response.output_'))
        self.assertTrue(parser.feed(b'text.delta","delta":"x"}\n\n'))
        self.assertFalse(parser.complete)
        self.assertFalse(parser.feed(b'data: [DONE]\n\n'))
        self.assertTrue(parser.feed(b'data: {"type":"response.completed"}\n\n'))
        self.assertTrue(parser.complete)
        chat = ProtocolProgress('text/event-stream')
        self.assertTrue(chat.feed(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":null}]}\n\n'))
        chat.feed(b'data: {"choices":[{"finish_reason":"stop"}]}\n\n')
        self.assertTrue(chat.complete)
        large = ProtocolProgress('text/event-stream')
        large.feed(b'data: ' + b'x' * 270000)
        self.assertLess(len(large.buffer), 262144)
        self.assertTrue(large.feed(b'\n\ndata: {"type":"response.completed"}\n\n'))
        self.assertTrue(large.complete)

    def test_retries_and_backoff_cannot_extend_request_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = options(retry_attempts=100, retry_backoff_seconds=0.05,
                               request_warning_seconds=0.1, request_timeout_seconds=0.3,
                               first_response_timeout_seconds=1)
            with gateway(Path(temporary), 'errors', settings) as (url, activity, requests):
                began = time.monotonic()
                call(url)
                until = time.monotonic() + 1
                while not activity.finished and time.monotonic() < until:
                    time.sleep(0.005)
                self.assertLess(time.monotonic() - began, 1)
                self.assertEqual(activity.finished[-1]['reason'], 'request_timeout')
                self.assertGreater(len(requests), 1)
                self.assertLess(len(requests), 101)

    def test_external_supervisor_stops_even_if_gateway_watchdog_is_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = options()
            with gateway(root, 'keepalive', {**settings, 'enabled': False}) as (url, activity, _):
                client = threading.Thread(target=call, args=(url,), daemon=True)
                client.start()
                monitor = ModelMonitor(ContainerService({}, root), root, settings)
                try:
                    with self.assertRaises(ModelFault) as failure:
                        until = time.monotonic() + 2
                        while time.monotonic() < until:
                            monitor.check()
                            time.sleep(0.01)
                    self.assertEqual(failure.exception.reason, 'first_response_timeout')
                    self.assertEqual(failure.exception.phase, 'model_api')
                    self.assertTrue(client.is_alive())
                    events = (root / 'model-monitor.jsonl').read_text()
                    self.assertIn('warning', events)
                    self.assertIn('terminated', events)
                finally:
                    # The harness stops the fake API; no request is left outside this test.
                    activity.options['enabled'] = True
                    client.join(2)

    def test_missing_stale_invalid_and_intermittent_observations_remain_unknown(self):
        snapshot = {'schema_version': '1.0', 'gateway_id': 'one', 'sequence': 1,
                    'active': [], 'finished': [], 'failure': None}
        for mode in ('missing', 'stale', 'invalid', 'intermittent'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                clock = [0.0]

                class Backend:
                    calls = 0

                    def read_model_activity(self, timeout_seconds):
                        self.calls += 1
                        if mode == 'missing' or (mode == 'intermittent' and self.calls % 2 == 0):
                            raise OSError('sensitive error must not enter diagnostics')
                        return {} if mode == 'invalid' else snapshot

                with patch('science_innovation_exam.model_monitor.time.monotonic', side_effect=lambda: clock[0]):
                    monitor = ModelMonitor(Backend(), root, options())
                    with self.assertRaises(ModelFault) as failure:
                        for step in range(10):
                            clock[0] = step * 0.1
                            monitor.check(force=True)
                    self.assertEqual(failure.exception.phase, 'model_observation')
                    self.assertNotIn('sensitive', (root / 'model-monitor.jsonl').read_text())

    def test_idle_training_is_not_an_api_timeout_and_clocks_need_no_synchronization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clock = [10000.0]

            class Backend:
                def read_model_activity(self, _timeout):
                    return {'schema_version': '1.0', 'gateway_id': 'remote', 'sequence': int(clock[0]),
                            'active': [], 'finished': [], 'failure': None}

            with patch('science_innovation_exam.model_monitor.time.monotonic', side_effect=lambda: clock[0]):
                monitor = ModelMonitor(Backend(), root, options())
                monitor.check()
                clock[0] += 100000
                monitor.check()
                self.assertEqual(monitor.summary()['warnings'], 0)

    def test_a_blocked_backend_reader_cannot_block_stop_or_spawn_unbounded_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            stop = threading.Event()

            class Backend:
                calls = 0

                def read_model_activity(self, _timeout):
                    self.calls += 1
                    stop.wait(2)
                    raise OSError('read stopped')

            backend = Backend()
            monitor = ModelMonitor(backend, Path(temporary), options(query_timeout_seconds=0.02,
                                   observation_warning_seconds=0.02, observation_timeout_seconds=0.06))
            began = time.monotonic()
            try:
                with self.assertRaises(ModelFault) as failure:
                    while time.monotonic() - began < 1:
                        monitor.check(force=True)
                self.assertEqual(failure.exception.phase, 'model_observation')
                self.assertLess(time.monotonic() - began, 0.5)
                self.assertEqual(backend.calls, 1)
            finally:
                stop.set()

    def test_runner_classifies_fault_stops_without_scoring_and_records_cleanup(self):
        for cleanup_fails in (False, True):
            with self.subTest(cleanup_fails=cleanup_fails), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = _make_package(root)
                path = root / 'config.yaml'
                _write_json(path, {'task': str(task), 'output': str(root / 'runs'),
                                  'agent': {'type': 'codex', 'bundle': 'unused', 'model': 'fake'},
                                  'model_monitor': options()})
                backends = []

                class Running(FakeHandle):
                    def poll(self):
                        return None

                class Backend(FakeContainerService):
                    def start_agent(self, spec):
                        return Running(self.log)

                    def read_model_activity(self, _timeout):
                        row = {'request_id': 'owned', 'elapsed_seconds': 1, 'first_event_seconds': None,
                               'progress_age_seconds': None, 'reason': None}
                        return {'schema_version': '1.0', 'gateway_id': 'fake', 'sequence': 1,
                                'active': [row], 'finished': [], 'failure': None}

                    def cleanup(self):
                        super().cleanup()
                        if cleanup_fails:
                            raise OSError('cleanup unavailable')

                def load(config, directory):
                    backends.append(Backend(config, directory))
                    return backends[-1]

                def launch(config, *args, **kwargs):
                    config = copy.deepcopy(config)
                    config['agent'] = {'type': 'scripted', 'command': ['python3', '-c', 'pass']}
                    return prepare_agent(config, *args, **kwargs)

                with patch.object(runner, 'prepare_agent', side_effect=launch):
                    result = runner.run(path, load_service=load,
                                        prepare_image=lambda *a, **kw: ('sha256:fake', 'sie-cpu:local-tag'))
                self.assertEqual(result['status'], 'infrastructure_failed')
                self.assertIsNone(result['score'])
                self.assertEqual(result['failure_phase'], 'model_api')
                self.assertEqual(result['failure_reason'], 'request_timeout')
                self.assertEqual(result['cleanup_status'], 'pending' if cleanup_fails else 'completed')
                self.assertIn('stop', backends[0].log)
                self.assertNotIn('run_verifier:test', backends[0].log)
                self.assertEqual(result, json.loads((Path(result['run_directory']) / 'result.json').read_text()))

    def test_config_rejects_unknown_nonfinite_and_inverted_thresholds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'config.yaml'
            for invalid in ({'enabled': 1}, {'check_seconds': 0}, {'retry_attempts': -1},
                            {'idle_warning_seconds': 1000}, {'typo': 1}, {'request_timeout_seconds': float('inf')}):
                _write_json(path, {'task': 'unused', 'output': 'runs',
                                  'agent': {'type': 'scripted', 'command': ['true']}, 'model_monitor': invalid})
                with self.assertRaises(ContractError):
                    load_config(path)

    def test_local_snapshot_rejects_symlinks_fifo_and_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'agent-state/model-activity.json'
            state.parent.mkdir()
            service = ContainerService({}, root)
            state.symlink_to(root / 'absent')
            with self.assertRaises(OSError):
                service.read_model_activity(0.1)
            state.unlink()
            os.mkfifo(state)
            with self.assertRaises(ValueError):
                service.read_model_activity(0.1)
            state.unlink()
            state.write_bytes(b'x' * 1048577)
            with self.assertRaises(ValueError):
                service.read_model_activity(0.1)
