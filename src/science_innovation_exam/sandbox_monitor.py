"""Bounded execution/platform observations, independent of model progress."""
from dataclasses import asdict
import json
import math
import threading
import time

from .common.errors import ContractError, InfrastructureError
from .common.serialization import atomic_write_json
from .container_service.interface import AgentObservation


SANDBOX_DEFAULTS = {"check_seconds": 5, "query_timeout_seconds": 5,
                    "warning_seconds": 30, "timeout_seconds": 120, "recovery_seconds": 30}


def check_sandbox_monitor(options):
    if set(options) != set(SANDBOX_DEFAULTS):
        raise ContractError("unknown sandbox_monitor fields")
    for key, value in options.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ContractError("sandbox_monitor.%s must be positive" % key)
    if options['warning_seconds'] > options['timeout_seconds']:
        raise ContractError("sandbox_monitor warning must not exceed timeout")


class SandboxFault(InfrastructureError):
    def __init__(self, reason, exit_code=None):
        super().__init__("agent sandbox: " + reason)
        self.reason, self.exit_code = reason, exit_code


class SandboxMonitor:
    def __init__(self, handle, root, options):
        self.handle, self.root, self.options = handle, root, options
        self.started = self.next_check = time.monotonic()
        self.query = None
        self.unhealthy_since = self.healthy_since = None
        self.warned = False
        self.checks = self.failures = 0
        self.latest = None
        self.last_event = None

    def read(self):
        # At most one outstanding query even when a plugin ignores the deadline.
        if self.query is None:
            done, result = threading.Event(), {}
            def query():
                try:
                    result['value'] = self.handle.observe(self.options['query_timeout_seconds'])
                except Exception:
                    result['value'] = AgentObservation('unknown', reason='status_query_failed')
                finally:
                    done.set()
            self.query = done, result, time.monotonic()
            threading.Thread(target=query, daemon=True).start()
        done, result, began = self.query
        ready = done.wait(self.options['query_timeout_seconds'])
        if ready:
            self.query = None
        if not ready or time.monotonic() - began > self.options['query_timeout_seconds']:
            return AgentObservation('unknown', reason='status_query_timeout')
        value = result['value']
        try:
            if not isinstance(value, AgentObservation) or value.state not in ('running', 'exited', 'missing', 'failed', 'unknown'):
                raise ValueError()
            if value.exit_code is not None and type(value.exit_code) is not int:
                raise ValueError()
            if value.state == 'exited' and value.exit_code is None:
                raise ValueError()
            if not isinstance(value.reason, str) or len(value.reason) > 128 or not isinstance(value.evidence, dict):
                raise ValueError()
            if len(json.dumps(asdict(value), allow_nan=False)) > 16384:
                raise ValueError()
        except (ValueError, TypeError):
            return AgentObservation('unknown', reason='invalid_status_observation')
        return value

    def event(self, kind, reason, **fields):
        with (self.root / 'sandbox-monitor.jsonl').open('a', encoding='utf-8') as output:
            output.write(json.dumps({'event': kind, 'reason': reason,
                         'elapsed_seconds': round(time.monotonic() - self.started, 3), **fields}) + '\n')

    def fail(self, reason, code=None):
        self.event('terminated', reason)
        raise SandboxFault(reason, code)

    def check(self, force=False):
        now = time.monotonic()
        if not force and now < self.next_check:
            return self.latest
        self.checks += 1
        value = self.read()
        now = time.monotonic()
        self.next_check = now + self.options['check_seconds']
        self.latest = value
        atomic_write_json(self.root / 'sandbox-observed.json', asdict(value))
        signature = json.dumps(asdict(value), sort_keys=True)
        if signature != self.last_event:
            self.event('observation', value.reason or value.state, observation=asdict(value))
            self.last_event = signature
        if value.state in ('missing', 'failed'):
            self.fail(value.reason or 'sandbox_' + value.state, value.exit_code)
        if value.state == 'exited':
            if value.exit_code == 137:
                self.fail('agent_exit_137_unknown', 137)
            return value
        if value.state == 'unknown':
            self.failures += 1
            self.healthy_since = None
            if self.unhealthy_since is None:
                self.unhealthy_since = now
        elif self.unhealthy_since is not None:
            if self.healthy_since is None:
                self.healthy_since = now
            # Only sustained health resets the outage episode; one good poll does not.
            if now - self.healthy_since >= self.options['recovery_seconds']:
                self.event('recovered', 'status_stable')
                self.unhealthy_since = self.healthy_since = None
                self.warned = False
        if self.unhealthy_since is not None:
            age = now - self.unhealthy_since
            if age >= self.options['warning_seconds'] and not self.warned:
                self.event('warning', 'sandbox_status_unstable')
                self.warned = True
            if age >= self.options['timeout_seconds']:
                self.fail('sandbox_status_unstable')
        return value

    def summary(self):
        return {'checks': self.checks, 'failed_queries': self.failures,
                'state': self.latest.state if self.latest else 'unobserved',
                'unstable': self.unhealthy_since is not None}
