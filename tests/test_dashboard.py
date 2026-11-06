"""Run the real handler with isolated, disposable HOME and test credentials."""
import http.cookiejar
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from http.client import HTTPConnection
import urllib.request
import urllib.error
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
from auth import hash_password, save_users
import telemetry

class DashboardTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  # Scope Telemetry.start patch strictly to DashboardTests lifetime via addClassCleanup
  telemetry_patch = patch.object(telemetry.Telemetry, 'start', return_value=None)
  telemetry_patch.start()
  cls.addClassCleanup(telemetry_patch.stop)

  cls.repo_root=Path(__file__).resolve().parents[1]
  cls.web_dir=cls.repo_root/'web'
  cls.temp=tempfile.TemporaryDirectory();cls.oldhome=os.environ.get('HOME');os.environ['HOME']=cls.temp.name
  cls.static_dir=Path(cls.temp.name)/'.config/dashboard/static'
  cls.static_dir.mkdir(parents=True, exist_ok=True)
  for page in ['admin.html', 'family.html', 'login.html']:
   shutil.copy2(cls.web_dir / page, cls.static_dir / page)
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
 def test_antra_public_assets_are_exact_and_head_safe(self):
  for filename, content_type in [('antra.css', 'text/css; charset=utf-8'), ('antra.js', 'application/javascript; charset=utf-8')]:
   source = (self.web_dir / filename).read_bytes()
   target = self.static_dir / filename
   target.write_bytes(source)
   self.addCleanup(target.unlink, missing_ok=True)
   for user in (None, 'testadmin', 'testfamily'):
    for prefix in ('/', '/static/'):
     with self.subTest(filename=filename, user=user, prefix=prefix):
      status, body, headers = self.request_resp('GET', prefix + filename + '?v=1', user=user)
      self.assertEqual(status, 200)
      self.assertEqual(body, source)
      self.assertEqual(headers['content-type'], content_type)
      self.assertEqual(headers['cache-control'], 'no-cache')
      status, body, headers = self.request_resp('HEAD', prefix + filename, user=user)
      self.assertEqual(status, 200)
      self.assertEqual(body, b'')
      self.assertEqual(int(headers['content-length']), len(source))

 def request_resp(self,method,path,user=None,body=None,headers=None):
  c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5);h=headers or {}
  if user:h['Cookie']='session_token='+self.mod.sign_session_token(user)
  c.request(method,path,body=body,headers=h);r=c.getresponse();result=(r.status,r.read(),{k.lower(): v for k, v in r.getheaders()});c.close();return result
 def request_full(self,method,path,user=None,body=None,headers=None):
  c=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5);h=headers or {}
  if user:h['Cookie']='session_token='+self.mod.sign_session_token(user)
  c.request(method,path,body=body,headers=h);r=c.getresponse();result=(r.status,r.read(),{k.lower(): v for k, v in r.getheaders()},r.msg.get_all('Set-Cookie') or []);c.close();return result
 def _make_multipart(self, files, boundary="----WebKitFormBoundaryTest123"):
  parts = []
  for field_name, filename, data, ctype in files:
   parts.append(f"--{boundary}".encode())
   parts.append(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"'.encode())
   parts.append(f"Content-Type: {ctype}".encode())
   parts.append(b"")
   parts.append(data)
  parts.append(f"--{boundary}--".encode())
  parts.append(b"")
  body = b"\r\n".join(parts)
  return body, f"multipart/form-data; boundary={boundary}"
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
 def test_agy_controls_are_admin_only(self):
  with patch.object(self.mod, 'AGY_SESSIONS') as mgr:
   for method,path,body in [('GET','/api/agy/sessions',None),('POST','/api/agy/sessions',b'{}'),('DELETE','/api/agy/sessions/abc',None)]:
    self.assertEqual(self.request(method,path,'testfamily',body)[0],403)
    self.assertIn(self.request(method,path,None,body)[0], (401, 403))
   mgr.list_agy_sessions.assert_not_called()
   mgr.create_agy_session.assert_not_called()
   mgr.close_agy_session.assert_not_called()
 def test_agy_create_list_close_http_contracts(self):
  with patch.object(self.mod, 'AGY_SESSIONS') as mgr:
   mgr.list_agy_sessions.return_value=[{"id": "default", "name": "AGY (Primary)", "is_primary": True}]
   mgr.create_agy_session.return_value='testident'
   mgr.close_agy_session.return_value=True
   # GET list
   code, data = self.request('GET','/api/agy/sessions','testadmin')
   self.assertEqual(code, 200)
   self.assertEqual(json.loads(data), {"online": True, "sessions": [{"id": "default", "name": "AGY (Primary)", "is_primary": True}], "max_sessions": 8})
   mgr.list_agy_sessions.assert_called_once()
   # POST create
   code, data = self.request('POST','/api/agy/sessions','testadmin',body=b'')
   self.assertEqual(code, 200)
   self.assertEqual(json.loads(data), {"status": "ok", "id": "testident"})
   mgr.create_agy_session.assert_called_once_with(ident=None)
   # DELETE close
   code, data = self.request('DELETE','/api/agy/sessions/testident','testadmin')
   self.assertEqual(code, 200)
   self.assertEqual(json.loads(data), {"status": "ok"})
   mgr.close_agy_session.assert_called_once_with('testident')
 def test_agy_manager_errors_mapped_through_http(self):
  with patch.object(self.mod, 'AGY_SESSIONS') as mgr:
   mgr.create_agy_session.side_effect = ValueError("Maximum of 8 AGY instances reached")
   code, data = self.request('POST','/api/agy/sessions','testadmin',body=b'')
   self.assertEqual(code, 500)
   self.assertIn("Maximum of 8 AGY instances reached", json.loads(data).get("message", ""))
   mgr.close_agy_session.side_effect = ValueError("Primary AGY session cannot be terminated")
   code, data = self.request('DELETE','/api/agy/sessions/default','testadmin')
   self.assertEqual(code, 500)
   self.assertIn("Primary AGY session cannot be terminated", json.loads(data).get("message", ""))
 def test_body_limits_and_malformed_requests(self):
  self.assertEqual(self.request('POST','/api/login',body=b'x'*8193)[0],413)
  self.assertEqual(self.request('POST','/api/login',body=b'[]')[0],400)
  self.assertEqual(self.request('POST','/api/login',body=b'{"username":{},"password":[] }')[0],400)
  self.assertEqual(self.request('POST','/api/agy/scroll','testfamily',body=b'{}')[0],403)
 def test_direct_dashboard_cannot_buffer_filebrowser_download(self):
  self.assertEqual(self.request('GET','/drive/api/raw/example','testadmin')[0],404)
 def test_conversion_authenticated_success_both_aliases(self):
  body, ctype = self._make_multipart([("files", "test.docx", b"dummy word content", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
  with patch.object(self.mod, 'convert_documents_batch') as mock_conv:
   mock_conv.return_value = (b"%PDF-1.4 test output", "test.pdf", "application/pdf", None)
   for path in ['/api/convert', '/api/convert/pdf']:
    for user in ['testadmin', 'testfamily']:
     status, data, headers = self.request_resp('POST', path, user=user, body=body, headers={'Content-Type': ctype})
     self.assertEqual(status, 200, f"Failed for {path} with user {user}")
     self.assertEqual(headers.get('content-type'), 'application/pdf')
     self.assertIn('test.pdf', headers.get('content-disposition', ''))
     self.assertEqual(data, b"%PDF-1.4 test output")
 def test_conversion_unauthenticated_denial_both_aliases(self):
  body, ctype = self._make_multipart([("files", "test.docx", b"dummy", "text/plain")])
  for path in ['/api/convert', '/api/convert/pdf']:
   status, _ = self.request('POST', path, user=None, body=body, headers={'Content-Type': ctype})
   self.assertEqual(status, 403, f"Unauthenticated request to {path} must be 403")
 def test_conversion_oversized_rejection_both_aliases(self):
  oversized_len = 32 * 1024 * 1024 + 1
  for path in ['/api/convert', '/api/convert/pdf']:
   c = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
   token = self.mod.sign_session_token('testadmin')
   c.putrequest('POST', path)
   c.putheader('Cookie', f'session_token={token}')
   c.putheader('Content-Length', str(oversized_len))
   c.endheaders()
   r = c.getresponse()
   self.assertEqual(r.status, 413, f"Oversized request to {path} must return 413")
   c.close()
 def test_conversion_input_count_rejection_both_aliases(self):
  files = [(f"files", f"doc_{i}.docx", b"content", "text/plain") for i in range(11)]
  body, ctype = self._make_multipart(files)
  with patch.object(self.mod, 'convert_documents_batch') as mock_conv:
   for path in ['/api/convert', '/api/convert/pdf']:
    status, data = self.request('POST', path, user='testadmin', body=body, headers={'Content-Type': ctype})
    self.assertEqual(status, 413, f"11 files to {path} must return 413")
    self.assertIn(b"At most 10 documents", data)
   mock_conv.assert_not_called()
 def test_conversion_shared_busy_slot_and_release_after_failure(self):
  slot, acquired = self.mod.ADMISSION.acquire('/api/convert')
  self.assertTrue(acquired)
  body, ctype = self._make_multipart([("files", "test.docx", b"dummy", "text/plain")])
  try:
   status, _ = self.request('POST', '/api/convert/pdf', user='testadmin', body=body, headers={'Content-Type': ctype})
   self.assertEqual(status, 429)
   status2, _ = self.request('POST', '/api/convert', user='testadmin', body=body, headers={'Content-Type': ctype})
   self.assertEqual(status2, 429)
  finally:
   if slot:
    slot.release()
  with patch.object(self.mod, 'convert_documents_batch') as mock_conv:
   mock_conv.return_value = (b"%PDF-test", "test.pdf", "application/pdf", None)
   status, _ = self.request('POST', '/api/convert/pdf', user='testadmin', body=body, headers={'Content-Type': ctype})
   self.assertEqual(status, 200)
  bad_files = [(f"files", f"doc_{i}.docx", b"c", "text/plain") for i in range(11)]
  bad_body, bad_ctype = self._make_multipart(bad_files)
  status, _ = self.request('POST', '/api/convert/pdf', user='testadmin', body=bad_body, headers={'Content-Type': bad_ctype})
  self.assertEqual(status, 413)
  with patch.object(self.mod, 'convert_documents_batch') as mock_conv:
   mock_conv.return_value = (b"%PDF-test", "test.pdf", "application/pdf", None)
   status, _ = self.request('POST', '/api/convert/pdf', user='testadmin', body=body, headers={'Content-Type': ctype})
   self.assertEqual(status, 200)

 def test_notes_api_unauthenticated_denial(self):
  status, _ = self.request('GET', '/api/notes')
  self.assertEqual(status, 401)
  status, _ = self.request('POST', '/api/notes', body=b'{"notes":"test"}', headers={'Content-Type': 'application/json'})
  self.assertEqual(status, 401)

 def test_notes_api_load_save_and_role_isolation(self):
  import json
  # Initial load is empty string
  status, data = self.request('GET', '/api/notes', user='testadmin')
  self.assertEqual(status, 200)
  self.assertEqual(json.loads(data), {'notes': ''})

  # Save admin notes
  payload = json.dumps({'notes': 'Admin confidential note'}).encode('utf-8')
  status, data = self.request('POST', '/api/notes', user='testadmin', body=payload, headers={'Content-Type': 'application/json'})
  self.assertEqual(status, 200)
  self.assertEqual(json.loads(data), {'status': 'saved'})

  # Verify admin load returns updated notes
  status, data = self.request('GET', '/api/notes', user='testadmin')
  self.assertEqual(status, 200)
  self.assertEqual(json.loads(data), {'notes': 'Admin confidential note'})

  # Verify family user gets isolated empty notes initially
  status, data = self.request('GET', '/api/notes', user='testfamily')
  self.assertEqual(status, 200)
  self.assertEqual(json.loads(data), {'notes': ''})

  # Save family notes
  family_payload = json.dumps({'notes': 'Family grocery list'}).encode('utf-8')
  status, data = self.request('POST', '/api/notes', user='testfamily', body=family_payload, headers={'Content-Type': 'application/json'})
  self.assertEqual(status, 200)

  # Check both retain isolated content
  status, data = self.request('GET', '/api/notes', user='testadmin')
  self.assertEqual(json.loads(data), {'notes': 'Admin confidential note'})
  status, data = self.request('GET', '/api/notes', user='testfamily')
  self.assertEqual(json.loads(data), {'notes': 'Family grocery list'})

 def test_notes_api_invalid_payload(self):
  import json
  # Missing 'notes' key
  status, data = self.request('POST', '/api/notes', user='testadmin', body=b'{"wrong":"key"}', headers={'Content-Type': 'application/json'})
  self.assertEqual(status, 400)

  # 'notes' not a string
  status, data = self.request('POST', '/api/notes', user='testadmin', body=b'{"notes": 12345}', headers={'Content-Type': 'application/json'})
  self.assertEqual(status, 400)

  # Not JSON
  status, data = self.request('POST', '/api/notes', user='testadmin', body=b'plain text', headers={'Content-Type': 'text/plain'})
  self.assertEqual(status, 400)

 def test_role_template_byte_exactness_and_isolation(self):
  # 1. /login matches web/login.html byte-for-byte
  status, data, headers = self.request_resp('GET', '/login')
  self.assertEqual(status, 200)
  self.assertEqual(headers.get('content-type'), 'text/html; charset=utf-8')
  self.assertEqual(data, (self.web_dir / 'login.html').read_bytes())

  # 2. Authenticated admin root/index matches web/admin.html byte-for-byte
  for path in ['/', '/index', '/index.html']:
   status, data, headers = self.request_resp('GET', path, user='testadmin')
   self.assertEqual(status, 200)
   self.assertEqual(headers.get('content-type'), 'text/html; charset=utf-8')
   self.assertEqual(data, (self.web_dir / 'admin.html').read_bytes())
   self.assertNotEqual(data, (self.web_dir / 'family.html').read_bytes())

  # 3. Authenticated family root/index matches web/family.html byte-for-byte
  for path in ['/', '/index', '/index.html']:
   status, data, headers = self.request_resp('GET', path, user='testfamily')
   self.assertEqual(status, 200)
   self.assertEqual(headers.get('content-type'), 'text/html; charset=utf-8')
   self.assertEqual(data, (self.web_dir / 'family.html').read_bytes())
   self.assertNotEqual(data, (self.web_dir / 'admin.html').read_bytes())

 def test_role_template_missing_file_returns_503(self):
  admin_target = self.static_dir / 'admin.html'
  admin_backup = admin_target.read_bytes()
  try:
   admin_target.unlink()
   status, data, headers = self.request_resp('GET', '/', user='testadmin')
   self.assertEqual(status, 503)
   self.assertEqual(headers.get('content-type'), 'text/plain; charset=utf-8')
   self.assertIn(b"Service Unavailable", data)
   # Assert no filesystem/internal path leak in response body
   self.assertNotIn(str(self.static_dir).encode(), data)
  finally:
   admin_target.write_bytes(admin_backup)

  login_target = self.static_dir / 'login.html'
  login_backup = login_target.read_bytes()
  try:
   login_target.unlink()
   status, data, headers = self.request_resp('GET', '/login')
   self.assertEqual(status, 503)
   self.assertEqual(headers.get('content-type'), 'text/plain; charset=utf-8')
   self.assertIn(b"Service Unavailable", data)
   self.assertNotIn(str(self.static_dir).encode(), data)
  finally:
   login_target.write_bytes(login_backup)

 def test_synthetic_login_cookie_and_logout_flow(self):
  with patch.object(self.mod, 'get_filebrowser_jwt', return_value='synthetic-jwt-token'):
   # Real temporary password verification over HTTP
   login_payload = json.dumps({'username': 'testadmin', 'password': 'test-password-long'}).encode('utf-8')
   status, data, headers, cookies = self.request_full('POST', '/api/login', body=login_payload, headers={'Content-Type': 'application/json'})
   self.assertEqual(status, 200)
   self.assertEqual(len(cookies), 3)

   cookie_dict = {}
   for ck in cookies:
    name = ck.split(';')[0].split('=')[0]
    cookie_dict[name] = ck

   self.assertIn('session_token', cookie_dict)
   self.assertIn('session_user', cookie_dict)
   self.assertIn('session_token_http', cookie_dict)

   self.assertIn('HttpOnly', cookie_dict['session_token'])
   self.assertIn('SameSite=Lax', cookie_dict['session_token'])
   self.assertIn('Max-Age=2592000', cookie_dict['session_token'])
   self.assertNotIn('Secure', cookie_dict['session_token'])

   self.assertIn('SameSite=Lax', cookie_dict['session_user'])
   self.assertNotIn('HttpOnly', cookie_dict['session_user'])
   self.assertNotIn('Secure', cookie_dict['session_user'])

   self.assertIn('HttpOnly', cookie_dict['session_token_http'])
   self.assertIn('SameSite=Lax', cookie_dict['session_token_http'])
   self.assertNotIn('Secure', cookie_dict['session_token_http'])

   # Repeat over HTTPS forwarded protocol
   status_s, _, _, cookies_s = self.request_full('POST', '/api/login', body=login_payload, headers={'Content-Type': 'application/json', 'X-Forwarded-Proto': 'https'})
   self.assertEqual(status_s, 200)
   self.assertEqual(len(cookies_s), 2)  # session_token and session_user, no fallback http token
   s_dict = {ck.split(';')[0].split('=')[0]: ck for ck in cookies_s}
   self.assertIn('Secure', s_dict['session_token'])
   self.assertIn('Secure', s_dict['session_user'])
   self.assertNotIn('session_token_http', s_dict)

   # Access role page using issued session_token cookie directly
   token_part = cookie_dict['session_token'].split(';')[0]
   status_page, page_data, _ = self.request_resp('GET', '/', headers={'Cookie': token_part})
   self.assertEqual(status_page, 200)
   self.assertEqual(page_data, (self.web_dir / 'admin.html').read_bytes())

   # Verify legacy GET /logout clears all three cookies including session_token_http
   status_l, _, headers_l, logout_cookies = self.request_full('GET', '/logout')
   self.assertEqual(status_l, 302)
   self.assertEqual(headers_l.get('location'), '/login')
   self.assertEqual(len(logout_cookies), 3)
   l_dict = {ck.split(';')[0].split('=')[0]: ck for ck in logout_cookies}
   for k in ['session_token', 'session_user', 'session_token_http']:
    self.assertIn(k, l_dict)
    self.assertIn('Max-Age=0', l_dict[k])

   # Verify POST /api/logout clears all three cookies as well
   status_p, _, _, post_logout_cookies = self.request_full('POST', '/api/logout')
   self.assertEqual(status_p, 200)
   self.assertEqual(len(post_logout_cookies), 3)
   p_dict = {ck.split(';')[0].split('=')[0]: ck for ck in post_logout_cookies}
   for k in ['session_token', 'session_user', 'session_token_http']:
    self.assertIn(k, p_dict)
    self.assertIn('Max-Age=0', p_dict[k])

   # -----------------------------------------------------------------
   # Browser cookie jar lifecycle test (models client-side cookie expiry)
   # -----------------------------------------------------------------
   base_url = f"http://127.0.0.1:{self.server.server_port}"
   cj = http.cookiejar.CookieJar()
   opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.ProxyHandler({})
   )

   # 1. Login via JSON real temp password into cookie jar
   req = urllib.request.Request(
    f"{base_url}/api/login",
    data=login_payload,
    headers={"Content-Type": "application/json"}
   )
   resp = opener.open(req, timeout=5)
   self.assertEqual(resp.status, 200)
   jar_cookies = {c.name: c for c in cj}
   self.assertIn("session_token", jar_cookies)
   self.assertIn("session_user", jar_cookies)
   self.assertIn("session_token_http", jar_cookies)

   # 2. Cookie-authenticated root request returns exact maintained admin bytes
   root_resp = opener.open(f"{base_url}/", timeout=5)
   self.assertEqual(root_resp.status, 200)
   self.assertEqual(root_resp.read(), (self.web_dir / "admin.html").read_bytes())

   # 3. GET /logout follows redirect to /login and clears jar cookies
   get_logout_resp = opener.open(f"{base_url}/logout", timeout=5)
   self.assertTrue(get_logout_resp.geturl().endswith("/login"))
   self.assertEqual(get_logout_resp.status, 200)
   # Assert jar has expired and removed the session cookies
   self.assertEqual(len(list(cj)), 0)

   # 4. Subsequent root access redirects unauthenticated client to /login
   root_after_get = opener.open(f"{base_url}/", timeout=5)
   self.assertTrue(root_after_get.geturl().endswith("/login"))
   # Protected API access returns 401
   with self.assertRaises(urllib.error.HTTPError) as ctx:
    opener.open(f"{base_url}/api/notes", timeout=5)
   self.assertEqual(ctx.exception.code, 401)

   # 5. Repeat login and test POST /api/logout jar clearance
   resp2 = opener.open(req, timeout=5)
   self.assertEqual(resp2.status, 200)
   self.assertEqual(len(list(cj)), 3)

   post_logout_req = urllib.request.Request(f"{base_url}/api/logout", data=b"")
   post_logout_resp = opener.open(post_logout_req, timeout=5)
   self.assertEqual(post_logout_resp.status, 200)
   self.assertEqual(len(list(cj)), 0)

   root_after_post = opener.open(f"{base_url}/", timeout=5)
   self.assertTrue(root_after_post.geturl().endswith("/login"))
   with self.assertRaises(urllib.error.HTTPError) as ctx2:
    opener.open(f"{base_url}/api/notes", timeout=5)
   self.assertEqual(ctx2.exception.code, 401)

 def test_portal_polling_asset_delivery_and_gating(self):
  static_dir = Path(self.temp.name) / '.config/dashboard/static'
  static_dir.mkdir(parents=True, exist_ok=True)
  portal_file = static_dir / 'portal-polling.js'

  # Missing asset -> 503
  if portal_file.exists(): portal_file.unlink()
  status, body, headers = self.request_resp('GET', '/static/portal-polling.js', user='testadmin')
  self.assertEqual(status, 503)

  # Unauthenticated -> 302 to /login
  status, body, headers = self.request_resp('GET', '/static/portal-polling.js')
  self.assertEqual(status, 302)
  self.assertEqual(headers.get('location'), '/login')

  # Family user -> 403
  status, body, headers = self.request_resp('GET', '/static/portal-polling.js', user='testfamily')
  self.assertEqual(status, 403)

  # Installed asset for admin -> exact bytes, no-cache, Content-Length
  sample_bytes = b'/* test portal polling helper */ console.log("polling");'
  portal_file.write_bytes(sample_bytes)
  status, body, headers = self.request_resp('GET', '/static/portal-polling.js', user='testadmin')
  self.assertEqual(status, 200)
  self.assertEqual(body, sample_bytes)
  self.assertEqual(headers.get('content-length'), str(len(sample_bytes)))
  self.assertIn('application/javascript', headers.get('content-type', ''))
  self.assertEqual(headers.get('cache-control'), 'no-cache')
