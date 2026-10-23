"""Run the real handler with isolated, disposable HOME and test credentials."""
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from http.client import HTTPConnection
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from auth import hash_password, save_users

class DashboardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory();cls.oldhome=os.environ.get('HOME');os.environ['HOME']=cls.temp.name
  save_users(Path(cls.temp.name)/'.config/dashboard/users.json', {'testadmin':{'password_hash':hash_password('test-password-long'), 'role':'admin'},'testfamily':{'password_hash':hash_password('family-password-long'),'role':'family'}})
  spec=importlib.util.spec_from_file_location('isolated_dashboard',Path(__file__).resolve().parents[1]/'app/dashboard.py');cls.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.mod)
  cls.server=cls.mod.ThreadingHTTPServer(('127.0.0.1',0),cls.mod.DashboardHandler)
  cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
 @classmethod
 def tearDownClass(cls):
  cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()
  if cls.oldhome is not None:os.environ['HOME']=cls.oldhome
 def request(self,method,path,user=None,body=None,headers=None):
  c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5);h=headers or {}
  if user:h['Cookie']='session_token='+self.mod.sign_session_token(user)
  c.request(method,path,body=body,headers=h);r=c.getresponse();result=(r.status,r.read());c.close();return result
 def test_admin_gate_independent_of_forwarded_encoding(self):
  for uri in ['/terminal/ws','/%74erminal/ws','/foo/../terminal/ws','/TERMINAL/ws','/%76ault/']:
   self.assertEqual(self.request('GET','/api/auth-check-admin','testfamily',headers={'X-Forwarded-Uri':uri})[0],403)
   self.assertEqual(self.request('GET','/api/auth-check-admin','testadmin',headers={'X-Forwarded-Uri':uri})[0],200)
  self.assertEqual(self.request('GET','/api/auth-check','testfamily')[0],200)
  self.assertEqual(self.request('GET','/api/auth-check?override=true','testadmin')[0],200)
  self.assertEqual(self.request('GET','/api/auth-check-admin?foo=1','testfamily')[0],403)
 def test_laptop_controls_are_admin_only(self):
  with patch.object(self.mod, 'LAPTOP_CLIENT') as client:
   for method,path,body in [('GET','/api/laptop/terminals',None),('POST','/api/laptop/terminals',b'{}'),('DELETE','/api/laptop/terminals/'+'a'*32,None)]:
    self.assertEqual(self.request(method,path,'testfamily',body)[0],403)
   client.status.assert_not_called();client.operation.assert_not_called()
 def test_laptop_create_list_close_use_resource_api(self):
  with patch.object(self.mod, 'LAPTOP_CLIENT') as client:
   client.status.return_value={'online':True,'sessions':[],'max_sessions':4}
   client.operation.return_value={'id':'a'*32}
   self.assertEqual(self.request('GET','/api/laptop/terminals','testadmin')[0],200)
   self.assertEqual(self.request('POST','/api/laptop/terminals','testadmin',b'{}')[0],201)
   self.assertEqual(self.request('DELETE','/api/laptop/terminals/'+'a'*32,'testadmin')[0],200)
   self.assertEqual(client.operation.call_args_list[0].args,('create',None))
   self.assertEqual(client.operation.call_args_list[1].args,('close','a'*32))
 def test_body_limits_and_malformed_requests(self):
  self.assertEqual(self.request('POST','/api/login',body=b'x'*8193)[0],413)
  self.assertEqual(self.request('POST','/api/login',body=b'[]')[0],400)
  self.assertEqual(self.request('POST','/api/login',body=b'{"username":{},"password":[] }')[0],400)
  self.assertEqual(self.request('POST','/api/agy/scroll','testfamily',body=b'{}')[0],403)
 def test_direct_dashboard_cannot_buffer_filebrowser_download(self):
  self.assertEqual(self.request('GET','/drive/api/raw/example','testadmin')[0],404)
