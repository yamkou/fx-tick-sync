from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from test_phase3b import Clock, healthy, FakeAuth
from fxtick.config import ConfigError
from fxtick.collectors.monitoring import Check, Severity, IncidentKey, NotificationRoute, Channel, MonitoringPolicy
from fxtick.watchdog.messages import AlertObservation, format_notification
from fxtick.watchdog.monitor import WatchdogEvent, ExternalMonitor, event_dict, read_event
from fxtick.watchdog.config import NodePolicy, MonitorConfig
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.store import SQLiteState
from fxtick.watchdog.notifications import LineMessagingProvider, EmailProvider
from fxtick.watchdog.providers import GenericWebhookProvider
from fxtick.watchdog.recovery import RecoveryTarget, RecoveryKind, RecoveryPlanner
from fxtick.platform.scheduled_task import CollectorTask


class SecretFixture:
    def get(self,reference):
        return {'token-ref':'test-only-not-a-real-token','recipient-ref':'test-recipient',
                'sender-ref':'test@example.invalid','endpoint-ref':'https://example.invalid/push'}[reference]


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.now=Clock()()
        self.event=WatchdogEvent('event-one',IncidentKey('london-01',Check.HEARTBEAT),Severity.CRITICAL,
            self.now,180,self.now-timedelta(seconds=180),AlertObservation(self.now-timedelta(seconds=180),
            self.now-timedelta(seconds=200),self.now-timedelta(seconds=210),180))
        self.route=NotificationRoute('operations',Channel.LINE)

    def test_smartphone_critical_format(self):
        text=format_notification(self.event)
        self.assertIn('[CRITICAL] london-01 DOWN',text)
        self.assertIn('Last heartbeat:',text); self.assertIn('Last tick:',text); self.assertIn('Last write:',text)
        self.assertIn('heartbeat timeout >= 180 sec',text)
        self.assertIn('UTC',text)

    def test_smartphone_recovery_format(self):
        event=replace(self.event,severity=Severity.RECOVERY,outage_seconds=502,first_seen_at=self.now-timedelta(seconds=502))
        text=format_notification(event)
        self.assertIn('[RECOVERY] london-01 ONLINE',text)
        self.assertIn('Downtime: 8m22s',text)

    def test_unknown_observations_are_explicit(self):
        self.assertIn('UNKNOWN',format_notification(replace(self.event,observation=None)))

    def test_event_context_roundtrip_and_legacy_read(self):
        raw=event_dict(self.event)
        self.assertEqual(read_event(json.dumps(raw)).observation,self.event.observation)
        del raw['observation']
        self.assertIsNone(read_event(json.dumps(raw)).observation)

    def test_line_messaging_adapter_fake_transport(self):
        calls=[]
        class FakeLine:
            def push_text(self,**kwargs): calls.append(kwargs); return True
        provider=LineMessagingProvider(FakeLine(),SecretFixture(),'token-ref','recipient-ref')
        self.assertTrue(provider.send(self.event,self.route))
        self.assertIn('[CRITICAL]',calls[0]['text']); self.assertEqual(calls[0]['event_id'],'event-one')
        self.assertNotIn('test-only-not-a-real-token',calls[0]['text'])

    def test_line_failure_is_sanitized(self):
        class Broken:
            def push_text(self,**kwargs): raise RuntimeError('sensitive-test-value')
        with self.assertNoLogs():
            self.assertFalse(LineMessagingProvider(Broken(),SecretFixture(),'token-ref','recipient-ref').send(self.event,self.route))

    def test_email_fake_transport(self):
        calls=[]
        class FakeEmail:
            def send_text(self,**kwargs): calls.append(kwargs); return True
        provider=EmailProvider(FakeEmail(),SecretFixture(),'sender-ref','recipient-ref','token-ref')
        self.assertTrue(provider.send(self.event,self.route))
        self.assertEqual(calls[0]['subject'],'[CRITICAL] london-01 DOWN')

    def test_email_failure_does_not_raise(self):
        class Broken:
            def send_text(self,**kwargs): raise RuntimeError('sensitive-test-value')
        with self.assertNoLogs():
            self.assertFalse(EmailProvider(Broken(),SecretFixture(),'sender-ref','recipient-ref','token-ref').send(self.event,self.route))

    def test_webhook_fake_delivery_contains_phone_message(self):
        calls=[]
        class Poster:
            def post(self,endpoint,body,headers): calls.append(json.loads(body)); return 204
        provider=GenericWebhookProvider('endpoint-ref',SecretFixture(),'token-ref',Poster())
        self.assertTrue(provider.send(self.event,self.route))
        self.assertIn('[CRITICAL]',calls[0]['message'])
        self.assertNotIn('test-only-not-a-real-token',json.dumps(calls[0]))

    def test_all_severities_format(self):
        for severity in Severity:
            self.assertIn('['+severity.value+']',format_notification(replace(self.event,severity=severity)))

    def test_component_recovery_does_not_claim_whole_node_online(self):
        event=replace(self.event,incident=IncidentKey('london-01',Check.DISK),severity=Severity.RECOVERY)
        self.assertIn('DISK RECOVERED',format_notification(event))
        self.assertNotIn('ONLINE',format_notification(event))


class RecoveryAndPersistenceTests(unittest.TestCase):
    def test_warning_critical_reminder_recovery_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            clock=Clock(); path=Path(root)/'state.sqlite'; state=SQLiteState(path)
            node=NodePolicy('london-01',health=MonitoringPolicy(cooldown_seconds=600))
            config=MonitorConfig('tokyo-01',(node,))
            monitor=ExternalMonitor(config,state,FakeAuth(),clock)
            try:
                monitor.receive(Heartbeat(healthy(),'boot-one',1).encode(),'test-proof')
                clock.advance(120); self.assertEqual(monitor.evaluate()[0].severity,Severity.WARNING)
                clock.advance(60); self.assertEqual(monitor.evaluate()[0].severity,Severity.CRITICAL)
                self.assertEqual(monitor.evaluate(),[])
                clock.advance(600); self.assertEqual(monitor.evaluate()[0].severity,Severity.CRITICAL)
                monitor.receive(Heartbeat(healthy(clock()),'boot-one',2).encode(),'test-proof')
                self.assertEqual(monitor.evaluate()[0].severity,Severity.RECOVERY)
                state.close(); state=SQLiteState(path); monitor=ExternalMonitor(config,state,FakeAuth(),clock)
                self.assertEqual(monitor.evaluate(),[])
                self.assertEqual(state.latest('london-01').sequence,2)
            finally: state.close()

    def test_recovery_plans_never_execute(self):
        incident=IncidentKey('london-01',Check.COLLECTOR)
        planner=RecoveryPlanner((RecoveryTarget('london-01','collector-one',tuple(RecoveryKind)),))
        for action in RecoveryKind:
            plan=planner.plan(incident,action,'collector-one',Clock()())
            self.assertTrue(plan.requires_approval); self.assertFalse(plan.executable)

    def test_unknown_recovery_target_and_os_reboot_denied(self):
        planner=RecoveryPlanner(())
        with self.assertRaises(ConfigError): planner.plan(IncidentKey('london-01',Check.COLLECTOR),RecoveryKind.MT5_RESTART,'terminal-one',Clock()())
        with self.assertRaises(ValueError): RecoveryKind('os-reboot')

    def task(self):
        return CollectorTask('london-01',r'C:\Approved Runtime\python.exe',r'C:\Collector\sender.py',
                             r'C:\Collector\collector.json',r'C:\Collector')

    def test_windows_task_plan_disabled_and_least_privilege(self):
        root=ET.fromstring(self.task().xml()); ns={'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('t:Settings/t:Enabled',ns).text,'false')
        self.assertEqual(root.find('t:Principals/t:Principal/t:RunLevel',ns).text,'LeastPrivilege')
        self.assertIsNotNone(root.find('t:Triggers/t:BootTrigger',ns))
        self.assertIn('--config',root.find('t:Actions/t:Exec/t:Arguments',ns).text)

    def test_windows_task_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'task.xml'; self.task().write_new(path); original=path.read_bytes()
            with self.assertRaises(FileExistsError): self.task().write_new(path)
            self.assertEqual(path.read_bytes(),original)

    def test_windows_task_rejects_relative_or_shell_command(self):
        with self.assertRaises(ConfigError): replace(self.task(),python_executable='python.exe')
        with self.assertRaises(ConfigError): replace(self.task(),python_executable=r'C:\Windows\cmd.bat')


if __name__=='__main__': unittest.main()
