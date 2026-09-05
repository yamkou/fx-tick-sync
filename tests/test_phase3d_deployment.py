import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fxtick.config import load_config
from fxtick.watchdog.production_config import load_production_config


class DeploymentTemplateTests(unittest.TestCase):
    def test_monitor_template_resolves_without_credentials_or_state_creation(self):
        path=Path('deployment/monitor/production.example.json')
        before=set(path.parent.rglob('*'))
        with patch.dict(os.environ,{},clear=True): config=load_production_config(path)
        self.assertEqual(config.state_path,path.parent.resolve()/'runtime'/'state'/'monitor.sqlite')
        self.assertEqual({s.collector_id for s in config.senders},{'london-01','london-02','frankfurt-01'})
        self.assertEqual(set(path.parent.rglob('*')),before)

    def test_collector_template_has_separate_roots_and_ten_logical_terminals(self):
        path=Path('deployment/collector/deployment.example.json')
        config=load_config(path)
        self.assertEqual(len(config.terminals),10)
        self.assertEqual(len(config.paths.resolve(path.parent)),5)

    def test_environment_templates_have_no_values(self):
        for path in Path('deployment').glob('*/environment.example.env'):
            for line in path.read_text().splitlines():
                if line and not line.startswith('#'):
                    self.assertEqual(line.split('=',1)[1],'')


if __name__=='__main__': unittest.main()
