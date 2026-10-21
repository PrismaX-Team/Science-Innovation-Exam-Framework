"""Explicit post-crash cleanup. Never resume execution or recompute a score."""
from pathlib import Path
from datetime import datetime, timezone

from . import container_service
from .common.errors import InfrastructureError
from .common.serialization import atomic_write_json
from .resources import ResourceJournal, RunLock, read_record


def cleanup_run(directory, load_service=container_service.load):
    root = Path(directory).resolve(strict=True)
    with RunLock(root, 'cleanup'):
        journal = ResourceJournal(root)
        config = read_record(root / 'config.json')
        if config['container_service'] != journal.data['backend']:
            raise InfrastructureError('saved backend configuration does not match resource journal')
        errors = []
        try:
            if journal.pending():
                service = load_service(config, root)
                service.recover_resources()
        except Exception as error:
            # Provider exception text can contain commands/credentials. Retain only its type.
            errors.append(type(error).__name__)
        journal = ResourceJournal(root)
        pending = [key for key, _ in journal.pending()]
        if pending:
            errors.append('unresolved_resources')
        report = {'status': 'cleaned' if not errors else 'cleanup_pending',
                  'cleanup_status': 'completed' if not errors else 'pending',
                  'pending_resources': pending, 'errors': errors,
                  'retained_resources': [key for key, value in journal.data['resources'].items() if value.get('retained')],
                  'finished_at': datetime.now(timezone.utc).isoformat(), 'run_directory': str(root)}
        atomic_write_json(root / 'cleanup-result.json', report)
        result_path = root / 'result.json'
        if result_path.exists():
            # Preserve all historical score/status evidence, including previous cleanup errors.
            result = read_record(result_path)
            result['recovery_cleanup'] = report
        else:
            result = {'status': 'infrastructure_failed', 'score': None,
                      'run_directory': str(root), 'container_service': config['container_service']['type'],
                      'failure_phase': 'controller', 'failure_reason': 'controller_lost',
                      'cleanup_status': report['cleanup_status'], 'recovery_cleanup': report}
        atomic_write_json(result_path, result)
        return report
