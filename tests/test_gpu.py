import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from science_innovation_exam.common.errors import ContractError, InfrastructureError
from science_innovation_exam.container_service.docker import DockerService
from science_innovation_exam.container_service.interface import ContainerService, ContainerSpec
from science_innovation_exam.runner import respect_task_limits
from science_innovation_exam.task import TaskPackage
from tests.helpers import _make_package, _write_json


class GPUContractTests(unittest.TestCase):
    def test_limit_is_not_a_request_and_declared_gpu_count_cannot_exceed_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_root = _make_package(Path(tmp))
            instruction = json.loads((task_root / 'instruction.json').read_text())
            instruction['limitation']['gpu'] = {'count': 1}
            _write_json(task_root / 'instruction.json', instruction)
            task = TaskPackage.load(task_root)
            self.assertEqual(task.gpu_count, 0)
            instruction['execution'] = {'gpu_count': 2}
            _write_json(task_root / 'instruction.json', instruction)
            task = TaskPackage.load(task_root)
            with self.assertRaisesRegex(ContractError, 'GPU count'):
                respect_task_limits(task, {'resources': {'cpus': 1, 'memory_mb': 512, 'timeout_seconds': 60}})

    def test_unsupported_backend_fails_instead_of_silently_running_on_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(InfrastructureError, 'support GPU'):
                ContainerService({}, tmp).prepare_task(SimpleNamespace(gpu_count=1))

    def test_local_docker_exposes_requested_gpu_only_to_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = DockerService({'container_service': {}}, tmp)
            service.docker = Mock()
            service.prepare_task(SimpleNamespace(gpu_count=2))
            spec = ContainerSpec('agent', 'image', ['true'], [], {}, network_mode='offline')
            service.start_agent(spec)
            self.assertEqual(service.docker.start.call_args.kwargs['extra'], ('--gpus', '2'))
            service.run_verifier(ContainerSpec('test', 'image', ['true'], [], {}))
            self.assertNotIn('extra', service.docker.run.call_args.kwargs)
            self.assertIn('local GPU', service.environment_prompt()[0])


if __name__ == '__main__':
    unittest.main()
