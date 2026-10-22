import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from tor_proxy import TorProxy

class TorTests(unittest.TestCase):
 def test_only_verified_tor_reply_is_accepted(self):
  proxy=TorProxy()
  with patch('tor_proxy.bounded_command',return_value=json.dumps({'IP':'203.0.113.1','IsTor':False})):
   self.assertRaises(ValueError,proxy._lookup,True)
   self.assertEqual(proxy._lookup(False),'203.0.113.1')
  with patch('tor_proxy.bounded_command',return_value=json.dumps({'IP':'203.0.113.2','IsTor':True})):
   self.assertEqual(proxy._lookup(True),'203.0.113.2')
 def test_stale_or_disabled_proxy_cannot_claim_verified_exit(self):
  with tempfile.TemporaryDirectory() as home:
   proxy=TorProxy(home);proxy._started=True
   proxy._status.update(verified_via_tor=True,masked_ip='203.0.113.2',checked_at=time.time()-121)
   self.assertFalse(proxy.snapshot()['verified_via_tor'])
   self.assertIsNone(proxy.snapshot()['masked_ip'])
   folder=Path(home)/'.config/services_disabled';folder.mkdir(parents=True);(folder/'tor').touch()
   proxy._status['checked_at']=time.time()
   self.assertFalse(proxy.snapshot()['enabled']);self.assertFalse(proxy.snapshot()['verified_via_tor'])
   self.assertFalse(proxy.snapshot()['full_device_routing'])
