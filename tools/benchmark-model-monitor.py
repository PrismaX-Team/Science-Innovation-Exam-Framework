#!/usr/bin/env python3
"""Compare monitoring on/off with identical local fake SSE; no paid API or cluster.

Each mode uses a separate process, identical concurrency/stream, disk-backed
fixtures and a local snapshot reader. This does not measure D platform/WebDAV load.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / 'src'), str(root)]
runtime = sorted((root / 'storage/controller-runtime').glob('cpython-%s%s-*/site-packages' % sys.version_info[:2]))
if runtime:
    sys.path.insert(1, str(runtime[-1]))
from science_innovation_exam.common.host import require_disk
from science_innovation_exam.container_service.interface import ContainerService
from science_innovation_exam.model_monitor import ModelFault, ModelMonitor
from tests.test_model_monitor import call, gateway, options


def measure(mode, concurrency, calls):
    scratch = root / 'storage/test-tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    require_disk(scratch)
    settings = options(enabled=mode == 'on', check_seconds=0.05, progress_write_seconds=0.05,
                       request_warning_seconds=30, request_timeout_seconds=60,
                       first_response_warning_seconds=30, first_response_timeout_seconds=60,
                       idle_warning_seconds=30, idle_timeout_seconds=60,
                       observation_warning_seconds=30, observation_timeout_seconds=60)
    with tempfile.TemporaryDirectory(prefix='monitor-benchmark-', dir=scratch) as temporary:
        directory = Path(temporary)
        with gateway(directory, 'benchmark', settings) as (url, activity, _):
            if mode == 'off':
                activity.path = None
            monitor = ModelMonitor(ContainerService({}, directory), directory, settings)
            stop = threading.Event()

            def observe():
                while not stop.wait(settings['check_seconds']):
                    monitor.check()

            observer = threading.Thread(target=observe, daemon=True)
            if mode == 'on':
                observer.start()

            def request(_):
                started = time.perf_counter()
                req = Request(url, data=b'{"model":"fake","stream":true}')
                with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
                    first = response.read(1)
                    first_seconds = time.perf_counter() - started
                    body = first + response.read()
                return first_seconds, time.perf_counter() - started, len(body)

            cpu = time.process_time()
            began = time.perf_counter()
            try:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    measurements = list(pool.map(request, range(calls)))
            finally:
                stop.set()
                if mode == 'on':
                    observer.join(2)
            elapsed = time.perf_counter() - began
            cpu = time.process_time() - cpu
            until = time.monotonic() + 2
            while activity.active and time.monotonic() < until:
                time.sleep(0.01)
            return {'mode': mode, 'calls': calls, 'concurrency': concurrency,
                    'check_seconds': settings['check_seconds'],
                    'progress_write_seconds': settings['progress_write_seconds'],
                    'wall_seconds': elapsed, 'cpu_seconds': cpu,
                    'process_peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    'first_byte_median_seconds': statistics.median(x[0] for x in measurements),
                    'request_median_seconds': statistics.median(x[1] for x in measurements),
                    'throughput_bytes_per_second': sum(x[2] for x in measurements) / elapsed,
                    'snapshot_writes': activity.writes, 'snapshot_bytes_written': activity.write_bytes,
                    'trace_bytes': (directory / 'model-calls.jsonl').stat().st_size,
                    'local_snapshot_queries': monitor.checks, 'platform_queries': 0}


def measure_detection():
    scratch = root / 'storage/test-tmp'
    settings = options(query_timeout_seconds=0.05)
    with tempfile.TemporaryDirectory(prefix='monitor-detection-', dir=scratch) as temporary:
        directory = Path(temporary)
        # Disable the internal watchdog, retaining state publication, to measure
        # independent controller detection rather than an upstream socket error.
        with gateway(directory, 'keepalive', {**settings, 'enabled': False}) as (url, activity, _):
            client = threading.Thread(target=call, args=(url,), daemon=True)
            monitor = ModelMonitor(ContainerService({}, directory), directory, settings)
            began = time.perf_counter()
            client.start()
            try:
                while time.perf_counter() - began < 2:
                    monitor.check()
                    time.sleep(0.005)
            except ModelFault as fault:
                return {'fault': 'keepalive_only', 'phase': fault.phase, 'reason': fault.reason,
                        'configured_first_response_timeout_seconds': settings['first_response_timeout_seconds'],
                        'from_client_start_seconds': time.perf_counter() - began,
                        'snapshot_queries': monitor.checks}
            finally:
                activity.options['enabled'] = True
                client.join(2)
            raise RuntimeError('controller did not detect the injected fault')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--calls', type=int, default=40)
    parser.add_argument('--mode', choices=['on', 'off', 'detection'])
    parser.add_argument('--output', type=Path, default=root / 'storage/model-monitor-benchmark.json')
    args = parser.parse_args()
    if args.concurrency < 1 or args.calls < 1:
        parser.error('concurrency and calls must be positive')
    if args.mode:
        print(json.dumps(measure_detection() if args.mode == 'detection' else measure(args.mode, args.concurrency, args.calls)))
        return
    report = {'scope': 'local fake API; fresh process per mode; no cluster/provider acceptance', 'measurements': []}
    for mode in ('off', 'on'):
        completed = subprocess.run([sys.executable, '-B', __file__, '--mode', mode,
                                    '--concurrency', str(args.concurrency), '--calls', str(args.calls)],
                                   check=True, stdout=subprocess.PIPE, text=True, timeout=120)
        report['measurements'].append(json.loads(completed.stdout))
    completed = subprocess.run([sys.executable, '-B', __file__, '--mode', 'detection'],
                               check=True, stdout=subprocess.PIPE, text=True, timeout=15)
    report['fault_detection'] = json.loads(completed.stdout)
    require_disk(args.output.parent)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
