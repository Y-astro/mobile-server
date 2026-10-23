import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from laptop import LaptopClient,LaptopError,validate_id
spec=importlib.util.spec_from_file_location('laptop_bridge',Path(__file__).resolve().parents[1]/'laptop/bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
IDENT='a'*32

class ClientTests(unittest.TestCase):
 def test_session_ids_reject_commands_and_options(self):
  for value in ('../astro','-oProxyCommand=evil',IDENT+';id','A'*32,None,'a'*31):
   with self.assertRaises(LaptopError):validate_id(value)
  self.assertEqual(validate_id(IDENT),IDENT)
 def test_ssh_is_pinned_and_never_requests_password(self):
  with tempfile.TemporaryDirectory() as root:
   root=Path(root);(root/'key').touch();(root/'known').touch()
   config=root/'client.json';config.write_text(json.dumps(dict(host='100.111.177.30',user='astro',identity_file=str(root/'key'),known_hosts_file=str(root/'known'))))
   args=LaptopClient(config).ssh_args('attach '+IDENT,terminal=True)
   for value in ('-tt','BatchMode=yes','IdentitiesOnly=yes','StrictHostKeyChecking=yes','ConnectTimeout=3'):self.assertIn(value,args)
   self.assertEqual(args[-2:],['astro@100.111.177.30','attach '+IDENT])
 def test_offline_status_is_cached_and_not_a_fake_online_state(self):
  client=LaptopClient()
  with patch.object(client,'_call',side_effect=LaptopError('Laptop offline')) as call:
   self.assertFalse(client.status()['online']);self.assertFalse(client.status()['online']);self.assertEqual(call.call_count,1)
 def test_malformed_response_and_invalid_close_fail_closed(self):
  client=LaptopClient()
  with patch.object(client,'ssh_args',return_value=['ssh']),patch('laptop.subprocess.run',return_value=subprocess.CompletedProcess([],0,'not json','')):
   self.assertRaises(LaptopError,client._call,'list')
  with patch.object(client,'_call') as call:
   self.assertRaises(LaptopError,client.operation,'close','../other');call.assert_not_called()
 def test_overlapping_mutations_are_bounded(self):
  client=LaptopClient();client._operation.acquire()
  try:
   with self.assertRaises(LaptopError) as error:client.operation('create')
   self.assertEqual(error.exception.status,429)
  finally:client._operation.release()

class BridgeTests(unittest.TestCase):
 def test_rejects_arbitrary_forced_commands(self):
  for args in ([],['sh'],['create',';rm'],['attach',IDENT,'x'],['attach','../other'],['close','-tother']):
   with self.assertRaises(ValueError):bridge.dispatch(args)
 def test_only_managed_existing_session_can_be_attached(self):
  with patch.object(bridge,'sessions',return_value=[dict(id=IDENT)]):self.assertEqual(bridge.dispatch(['attach',IDENT]),('attach','laptop-'+IDENT))
  with patch.object(bridge,'sessions',return_value=[]):self.assertRaises(ValueError,bridge.dispatch,['attach',IDENT])
 def test_limit_and_close_do_not_touch_unrelated_sessions(self):
  with patch.object(bridge,'sessions',return_value=[dict(id=str(i)) for i in range(4)]),patch.object(bridge,'tmux') as tmux:
   self.assertRaises(RuntimeError,bridge.dispatch,['create']);tmux.assert_not_called()
  with patch.object(bridge,'sessions',return_value=[]),patch.object(bridge,'tmux') as tmux:
   self.assertTrue(bridge.dispatch(['close',IDENT])['closed']);tmux.assert_not_called()
 def test_kitty_start_failure_rolls_back_just_its_session(self):
  with patch.object(bridge,'sessions',return_value=[]),patch.object(bridge,'desktop_environment',return_value={}),patch.object(bridge,'tmux') as tmux,patch.object(bridge,'launch_kitty',side_effect=RuntimeError('No desktop')):
   self.assertRaises(RuntimeError,bridge.dispatch,['create'])
   args=tmux.call_args.args;self.assertEqual(args[0],'kill-session');self.assertRegex(args[-1],r'^laptop-[0-9a-f]{32}$')
