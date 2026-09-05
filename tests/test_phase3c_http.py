from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from concurrent.futures import Future, TimeoutError

from test_phase3b import Clock, healthy
from test_phase3c_auth import FakeKeys
from fxtick.config import ConfigError
from fxtick.watchdog.config import MonitorConfig, NodePolicy
from fxtick.watchdog.auth import SenderKeys, sign
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.http import HeartbeatApplication, ReceiverConfig, TokenBucketLimiter
from fxtick.watchdog.runtime import MonitorRuntime
from fxtick.watchdog.production_config import load_production_config


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(); self.addCleanup(self.root.cleanup)
        self.clock=Clock(); self.keys=FakeKeys()
        config=MonitorConfig('tokyo-01',(NodePolicy('london-01'),))
        senders=(SenderKeys('london-01',(('key-v1','key-ref'),),('boot-one',)),)
        self.runtime=MonitorRuntime(config,str(Path(self.root.name)/'state.sqlite'),senders,self.keys,clock=self.clock)
        self.runtime.start(); self.addCleanup(self.runtime.stop)
        until=time.monotonic()+3
        while not self.runtime.health()['ready'] and time.monotonic()<until: time.sleep(.01)
        self.assertTrue(self.runtime.health()['ready'])
        self.app=HeartbeatApplication(self.runtime.inbox,self.runtime.health,['london-01'])
        self.beat=Heartbeat(healthy(),'boot-one',1)

    def env(self,beat=None):
        beat=beat or self.beat; body=beat.encode()
        proof=sign(beat,'key-v1','key-ref',self.keys,self.clock())
        return {'REMOTE_ADDR':'127.0.0.1','wsgi.url_scheme':'http','REQUEST_METHOD':'POST',
                'PATH_INFO':'/v1/heartbeat','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),
                'wsgi.input':io.BytesIO(body),**{'HTTP_'+k.upper().replace('-','_'):v for k,v in proof.headers().items()}}

    def request(self,env=None):
        captured=[]
        body=self.app(env or self.env(),lambda status,headers:captured.append((status,headers)))
        return int(captured[0][0].split()[0]),json.loads(b''.join(body))

    def test_valid_authenticated_http(self):
        self.assertEqual(self.request()[0],202)

    def test_invalid_hmac(self):
        env=self.env(); env['HTTP_X_FX_SIGNATURE']='0'*64
        self.assertEqual(self.request(env)[0],403)

    def test_expired_timestamp(self):
        env=self.env(); self.clock.advance(91)
        self.assertEqual(self.request(env)[0],403)

    def test_replay_request(self):
        env=self.env(); body=env['wsgi.input'].getvalue()
        self.assertEqual(self.request(env)[0],202)
        env['wsgi.input']=io.BytesIO(body)
        self.assertEqual(self.request(env)[0],403)

    def test_unknown_collector(self):
        env=self.env(replace(self.beat,snapshot=healthy(name='frankfurt-01')))
        self.assertEqual(self.request(env)[0],403)

    def test_oversized_body_rejected_without_read(self):
        class NeverRead:
            def read(self,*args): raise AssertionError('Body should not be read')
        env=self.env(); env.update(CONTENT_LENGTH='65537',**{'wsgi.input':NeverRead()})
        self.assertEqual(self.request(env)[0],413)

    def test_malformed_json(self):
        env=self.env(); env.update(CONTENT_LENGTH='1',**{'wsgi.input':io.BytesIO(b'{')})
        self.assertEqual(self.request(env)[0],400)

    def test_invalid_schema(self):
        env=self.env(); raw=json.loads(self.beat.encode()); raw['schema_version']=2
        body=json.dumps(raw).encode(); env.update(CONTENT_LENGTH=str(len(body)),**{'wsgi.input':io.BytesIO(body)})
        self.assertEqual(self.request(env)[0],400)

    def test_naive_timestamp_rejected(self):
        env=self.env(); raw=json.loads(self.beat.encode()); raw['health']['observed_at']='2026-09-07T00:00:00'
        body=json.dumps(raw).encode(); env.update(CONTENT_LENGTH=str(len(body)),**{'wsgi.input':io.BytesIO(body)})
        self.assertEqual(self.request(env)[0],400)

    def test_malformed_framing_and_length(self):
        for key,value,status in (('HTTP_TRANSFER_ENCODING','chunked',400),('CONTENT_LENGTH','',411),
                                 ('CONTENT_LENGTH','2,2',400),('HTTP_CONTENT_ENCODING','gzip',400),('CONTENT_TYPE','text/plain',415)):
            env=self.env(); env[key]=value
            self.assertEqual(self.request(env)[0],status)

    def test_short_body(self):
        env=self.env(); env['wsgi.input']=io.BytesIO(b'{}')
        self.assertEqual(self.request(env)[0],400)

    def test_wrong_method_and_unknown_path(self):
        env=self.env(); env['REQUEST_METHOD']='GET'; self.assertEqual(self.request(env)[0],405)
        env=self.env(); env['PATH_INFO']='/other'; self.assertEqual(self.request(env)[0],404)

    def test_untrusted_forwarded_tls_does_not_bypass_transport_rule(self):
        env=self.env(); env.update(REMOTE_ADDR='192.0.2.1',HTTP_X_FORWARDED_PROTO='https')
        self.assertEqual(self.request(env)[0],403)

    def test_trusted_server_https_scheme(self):
        env=self.env(); env.update(REMOTE_ADDR='192.0.2.1',**{'wsgi.url_scheme':'https'})
        self.assertEqual(self.request(env)[0],202)

    def test_rate_limiter_rejects_before_auth(self):
        class Deny:
            def allow(self,client): return False
        self.app.limiter=Deny()
        self.assertEqual(self.request()[0],429)

    def test_monitor_health_endpoint(self):
        env=self.env(); env.update(PATH_INFO='/healthz',REQUEST_METHOD='GET')
        status,value=self.request(env)
        self.assertEqual(status,200)
        self.assertTrue(value['process_alive']); self.assertTrue(value['db_accessible'])
        self.assertIsNotNone(value['last_evaluation_time'])
        self.assertEqual(value['notification_state'],'disabled')

    def test_stopped_worker_health_and_receive_are_unavailable(self):
        self.runtime.stop()
        env=self.env(); env.update(PATH_INFO='/healthz',REQUEST_METHOD='GET')
        self.assertEqual(self.request(env)[0],503)
        self.assertEqual(self.request()[0],503)

    def test_notification_degradation_does_not_reject_heartbeat(self):
        self.app.health=lambda:{'ready':True,'healthy':False}
        env=self.env(); env.update(PATH_INFO='/healthz',REQUEST_METHOD='GET')
        self.assertEqual(self.request(env)[0],503)
        self.assertEqual(self.request()[0],202)

    def test_ingress_timeout_never_acknowledges_receipt(self):
        class ImmediateTimeout:
            def result(self,timeout): raise TimeoutError()
            def cancel(self): pass
        class Inbox:
            def submit(self,*args): return ImmediateTimeout()
        self.app.inbox=Inbox()
        self.assertEqual(self.request()[0],503)

    def test_capacity_error_is_unavailable(self):
        class Full:
            def submit(self,*args): raise ConfigError('capacity')
        self.app.inbox=Full()
        self.assertEqual(self.request()[0],503)

    def test_error_responses_do_not_echo_signature(self):
        env=self.env(); env['HTTP_X_FX_SIGNATURE']='sensitive-test-value'
        with self.assertNoLogs(): status,value=self.request(env)
        self.assertEqual(status,400); self.assertNotIn('sensitive-test-value',json.dumps(value))

    def test_db_failure_after_start_makes_health_unavailable(self):
        with patch('fxtick.watchdog.runtime.SQLiteState.check',side_effect=RuntimeError('sensitive-test-value')):
            self.runtime.worker.join(2)
        env=self.env(); env.update(PATH_INFO='/healthz',REQUEST_METHOD='GET')
        status,value=self.request(env)
        self.assertEqual(status,503); self.assertFalse(value['db_accessible'])
        self.assertNotIn('sensitive-test-value',json.dumps(value))


class RuntimeConfigTests(unittest.TestCase):
    def test_stale_evaluation_is_unhealthy_even_with_live_worker(self):
        runtime=MonitorRuntime(MonitorConfig('tokyo-01',(NodePolicy('london-01'),)),'unused.sqlite',
            (SenderKeys('london-01',(('key-v1','key-ref'),),('boot-one',)),),FakeKeys(),monotonic=lambda:100)
        class Alive:
            def is_alive(self): return True
        runtime.worker=Alive(); runtime.evaluated_monotonic=0
        runtime.status.update(db_accessible=True,notification_state='healthy')
        self.assertFalse(runtime.health()['ready'])

    def test_bounded_token_bucket_and_refill(self):
        now=[0]
        limiter=TokenBucketLimiter(ReceiverConfig(burst=1,requests_per_minute=60,max_clients=1),lambda:now[0])
        self.assertTrue(limiter.allow('a')); self.assertFalse(limiter.allow('a')); self.assertFalse(limiter.allow('b'))
        now[0]=1; self.assertTrue(limiter.allow('a'))
        now[0]=100; self.assertTrue(limiter.allow('b'))

    def test_corrupt_db_worker_fails_closed_without_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'state.sqlite'; path.write_bytes(b'corrupt')
            runtime=MonitorRuntime(MonitorConfig('tokyo-01',(NodePolicy('london-01'),)),path,
                (SenderKeys('london-01',(('key-v1','key-ref'),),('boot-one',)),),FakeKeys())
            runtime.start(); runtime.worker.join(2)
            try:
                self.assertFalse(runtime.health()['ready']); self.assertFalse(runtime.health()['db_accessible'])
                self.assertEqual(path.read_bytes(),b'corrupt')
            finally: runtime.stop()

    def test_example_config_only_no_secret_access(self):
        with patch.dict('os.environ',{},clear=True):
            config=load_production_config('configs/production-monitor.example.json')
        self.assertEqual(len(config.senders),3)
        self.assertEqual(config.listen_host,'127.0.0.1')

    def test_runner_check_never_starts_runtime(self):
        from monitor_server import main
        with patch('monitor_server.MonitorRuntime') as runtime:
            self.assertEqual(main(['--config','configs/production-monitor.example.json','--check']),0)
        runtime.assert_not_called()

    def test_bad_receiver_limits(self):
        for kwargs in ({'max_payload_bytes':65537},{'acceptance_timeout_seconds':0},{'burst':True}):
            with self.assertRaises(ConfigError): ReceiverConfig(**kwargs)

    def test_inline_secret_and_public_bind_rejected(self):
        original=json.loads(Path('configs/production-monitor.example.json').read_text())
        with tempfile.TemporaryDirectory() as root:
            for field,value in (('token','sensitive-test-value'),('listen_host','0.0.0.0'),('auth_window_seconds',1000)):
                data={**original,field:value}; path=Path(root)/'config.json'; path.write_text(json.dumps(data))
                with self.assertRaises(ConfigError): load_production_config(path)


if __name__=='__main__': unittest.main()
