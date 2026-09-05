from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_phase3b import Clock, healthy
from fxtick.config import ConfigError
from fxtick.watchdog.config import NodePolicy, MonitorConfig
from fxtick.watchdog.heartbeat import Heartbeat
from fxtick.watchdog.monitor import ExternalMonitor
from fxtick.watchdog.store import SQLiteState
from fxtick.watchdog.auth import SenderKeys, HMACAuthenticator, sign, HMACProof, SignedHeartbeatTransport
from fxtick.watchdog.secrets import EnvironmentSecrets


class FakeKeys:
    def get(self, reference):
        return {'key-ref': 'unit-test-only-not-a-real-key-00001',
                'old-ref': 'unit-test-only-not-a-real-key-00002',
                'endpoint-ref': 'https://example.invalid/v1/heartbeat'}[reference]


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory(); self.addCleanup(self.root.cleanup)
        self.path = str(Path(self.root.name)/'state.sqlite')
        self.state = SQLiteState(self.path); self.addCleanup(lambda: self.state.close())
        self.clock = Clock(); self.keys = FakeKeys()
        self.senders = (SenderKeys('london-01', (('key-one','key-ref'), ('key-old','old-ref')), ('boot-one','boot-two')),)
        self.auth = HMACAuthenticator(self.senders, self.keys, self.clock)
        self.config = MonitorConfig('tokyo-01', (NodePolicy('london-01'),))
        self.monitor = ExternalMonitor(self.config, self.state, self.auth, self.clock)
        self.heartbeat = Heartbeat(healthy(), 'boot-one', 1)

    def proof(self, heartbeat=None, **kwargs):
        return sign(heartbeat or self.heartbeat, 'key-one', 'key-ref', self.keys, self.clock(), **kwargs)

    def test_valid_authenticated_heartbeat(self):
        self.assertTrue(self.monitor.receive(self.heartbeat.encode(), self.proof()))

    def test_invalid_hmac_has_no_write(self):
        proof = replace(self.proof(), signature='0'*64)
        with self.assertRaises(ConfigError): self.monitor.receive(self.heartbeat.encode(), proof)
        self.assertIsNone(self.state.latest('london-01'))

    def test_exact_payload_is_bound(self):
        self.assertFalse(self.auth.verify('london-01','boot-one',self.heartbeat.encode()+b' ',self.proof()))

    def test_signature_uses_constant_time_comparison(self):
        with patch('fxtick.watchdog.auth.hmac.compare_digest', return_value=True) as compare:
            self.assertTrue(self.auth.verify('london-01','boot-one', self.heartbeat.encode(), self.proof()))
            compare.assert_called_once()

    def test_expired_timestamp(self):
        proof = self.proof(); self.clock.advance(91)
        self.assertFalse(self.auth.verify('london-01','boot-one',self.heartbeat.encode(),proof))

    def test_future_timestamp(self):
        self.clock.advance(91); proof = self.proof(); self.clock.advance(-91)
        self.assertFalse(self.auth.verify('london-01','boot-one',self.heartbeat.encode(),proof))

    def test_nonce_reuse_with_new_sequence_rejected(self):
        nonce = '1'*32
        self.monitor.receive(self.heartbeat.encode(), self.proof(nonce=nonce))
        second = replace(self.heartbeat, sequence=2)
        with self.assertRaises(ConfigError): self.monitor.receive(second.encode(), self.proof(second, nonce=nonce))
        self.assertEqual(self.state.latest('london-01').sequence, 1)

    def test_nonce_replay_persists_after_restart(self):
        nonce = '2'*32
        self.monitor.receive(self.heartbeat.encode(), self.proof(nonce=nonce))
        self.state.close(); self.state = SQLiteState(self.path)
        self.monitor = ExternalMonitor(self.config,self.state,self.auth,self.clock)
        second = replace(self.heartbeat, sequence=2)
        with self.assertRaises(ConfigError): self.monitor.receive(second.encode(), self.proof(second, nonce=nonce))

    def test_retired_boot_still_rejected(self):
        for boot, seq in (('boot-one',1), ('boot-two',2)):
            beat = replace(self.heartbeat, boot_id=boot, sequence=seq)
            self.monitor.receive(beat.encode(),self.proof(beat))
        beat = replace(self.heartbeat,sequence=3)
        with self.assertRaises(ConfigError): self.monitor.receive(beat.encode(),self.proof(beat))

    def test_unenrolled_boot_and_collector_rejected(self):
        proof=self.proof()
        self.assertFalse(self.auth.verify('london-01','unknown-boot',self.heartbeat.encode(),proof))
        self.assertFalse(self.auth.verify('unknown-node','boot-one',self.heartbeat.encode(),proof))

    def test_rotation_accepts_overlap_and_revokes_old_key(self):
        proof=sign(self.heartbeat,'key-old','old-ref',self.keys,self.clock())
        self.assertTrue(self.auth.verify('london-01','boot-one',self.heartbeat.encode(),proof))
        rotated=HMACAuthenticator((SenderKeys('london-01',(('key-one','key-ref'),),('boot-one',)),),self.keys,self.clock)
        self.assertFalse(rotated.verify('london-01','boot-one',self.heartbeat.encode(),proof))

    def test_auth_headers_reject_malformed_fields(self):
        headers=self.proof().headers(); headers['X-FX-Timestamp']='1e2'
        with self.assertRaises(ConfigError): HMACProof.from_headers(headers)

    def test_proof_repr_omits_proof_values(self):
        proof=self.proof()
        self.assertNotIn(proof.signature,repr(proof)); self.assertNotIn(proof.nonce,repr(proof))

    def test_environment_secret_not_in_error(self):
        provider=EnvironmentSecrets({'key-ref':'FX_TEST_ONLY_MISSING_ENV'})
        with patch.dict('os.environ',{},clear=True), self.assertRaises(ConfigError) as error: provider.get('key-ref')
        self.assertNotIn('FX_TEST_ONLY_MISSING_ENV',str(error.exception))

    def test_signed_transport_fake_post(self):
        class Poster:
            def post(inner, endpoint, body, headers):
                proof=HMACProof.from_headers(headers)
                self.assertTrue(self.auth.verify('london-01','boot-one',body,proof))
                self.assertNotIn(self.keys.get('key-ref'),body.decode())
                return 204
        sender=SignedHeartbeatTransport('endpoint-ref','key-one','key-ref',self.keys,self.clock,Poster())
        self.assertTrue(sender.send(self.heartbeat))

    def test_backup_preserves_receipt_without_overwriting(self):
        self.monitor.receive(self.heartbeat.encode(),self.proof())
        target=Path(self.root.name)/'backup.sqlite'
        self.state.backup_to(target)
        other=SQLiteState(target)
        try: self.assertEqual(other.latest('london-01').sequence,1)
        finally: other.close()
        before=target.read_bytes()
        with self.assertRaises(FileExistsError): self.state.backup_to(target)
        self.assertEqual(target.read_bytes(),before)

    def test_corrupt_database_is_not_reset(self):
        path=Path(self.root.name)/'corrupt.sqlite'; path.write_bytes(b'not-a-database')
        with self.assertRaises(ConfigError): SQLiteState(path)
        self.assertEqual(path.read_bytes(),b'not-a-database')


if __name__ == '__main__': unittest.main()
