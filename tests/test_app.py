import copy
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from aihub.config import Config
from aihub.coordinator import Coordinator
from aihub.models import (ConflictError, ValidationError, parse_json, task_input,
                          text, validate_plan, version)
from aihub.ollama import OllamaClient, OllamaError, ModelResponseError
from aihub.server import make_server
from aihub.storage import Store

TASK = dict(title='Draft a function', role='coder', instructions='Draft a pure function as text only.', expected_output='A function and suggested tests.')
PLAN = dict(title='A small mission', summary='One proposed text-only task.', tasks=[dict(TASK,depends_on=[])])


class FakeClient:
    def __init__(self, replies=None):
        self.replies = list(replies or [json.dumps(PLAN)])
        self.calls = []

    def health(self):
        return dict(connected=True, version='test', error='')

    def chat(self, messages, schema=None, *, role=None):
        self.calls.append((copy.deepcopy(messages), schema))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value, {'eval_count': 10, 'eval_duration': 1000000000}


class ModelTests(unittest.TestCase):
    def test_valid_plan(self):
        self.assertEqual(validate_plan(PLAN), PLAN)

    def test_unknown_fields_at_every_level(self):
        for level in ('root', 'task'):
            value = copy.deepcopy(PLAN)
            (value if level == 'root' else value['tasks'][0])['extra'] = True
            with self.assertRaises(ValidationError): validate_plan(value)

    def test_unknown_and_non_string_roles(self):
        for role in ('coordinator', 'admin', 'Coder', None, [], {}):
            with self.subTest(role=role), self.assertRaises(ValidationError):
                task_input({**TASK, 'role': role})

    def test_missing_fields(self):
        with self.assertRaises(ValidationError): validate_plan({'tasks': []})

    def test_empty_or_too_many_tasks(self):
        for tasks in ([], [TASK] * 9, {}, 'task'):
            with self.assertRaises(ValidationError): validate_plan({**PLAN, 'tasks': tasks})

    def test_invalid_json_and_duplicates(self):
        for value in ('```json\n{}\n```', '{', '{"a":1,"a":2}', '{"x":NaN}', '[] trailing'):
            with self.assertRaises(ValidationError): parse_json(value)

    def test_input_types_lengths_controls(self):
        for value in (None, 1, True, ' ', 'x' * 3001, 'bad\x00input'):
            with self.assertRaises(ValidationError): text(value, 'Mission', 3000, 5)

    def test_version_rejects_bool_float_negative(self):
        for value in (True, -1, 1.0, '1'):
            with self.assertRaises(ValidationError): version(value)

    def test_config_bounds(self):
        for kw in ({'context':100000}, {'port':80}, {'model':''}):
            with self.assertRaises(ValueError): Config(**kw)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.sqlite3'
        self.store = Store(self.path)
        self.mid = self.store.create('A synthetic test mission')
        self.store.save_plan(self.mid, PLAN)
        self.tid = self.store.mission(self.mid)['tasks'][0]['id']

    def tearDown(self): self.temp.cleanup()

    def test_requires_approval_before_run(self):
        with self.assertRaises(ConflictError): self.store.change(self.tid, 0, 'run')
        self.assertEqual(self.store.task(self.tid)['status'], 'proposed')

    def test_full_lifecycle_and_persistence(self):
        self.store.change(self.tid, 0, 'approve')
        self.store.change(self.tid, 1, 'run')
        self.store.finish(self.tid, result='<script>untrusted output</script>', metrics={'eval_count':3})
        reopened = Store(self.path).mission(self.mid)
        self.assertEqual(reopened['tasks'][0]['result'], '<script>untrusted output</script>')
        self.assertEqual(reopened['counts']['completed'], 1)
        self.assertEqual(reopened['tasks'][0]['metrics']['eval_count'], 3)
        self.assertGreaterEqual(len(reopened['events']), 5)

    def test_edit_revokes_approval(self):
        self.store.change(self.tid, 0, 'approve')
        task = self.store.change(self.tid, 1, 'edit', {**TASK,'role':'writer'})
        self.assertEqual(task['status'], 'proposed')
        self.assertEqual(task['role'], 'writer')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 2, 'run')

    def test_rejection_and_reapproval(self):
        self.store.change(self.tid, 0, 'reject')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 1, 'run')
        self.store.change(self.tid, 1, 'reset')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 2, 'run')
        self.store.change(self.tid, 2, 'approve')
        self.assertEqual(self.store.task(self.tid)['status'], 'approved')

    def test_stale_and_double_run_rejected(self):
        self.store.change(self.tid, 0, 'approve')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 0, 'reject')
        self.store.change(self.tid, 1, 'run')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 1, 'run')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 2, 'edit', TASK)

    def test_failure_requires_review(self):
        self.store.change(self.tid, 0, 'approve')
        self.store.change(self.tid, 1, 'run')
        self.store.finish(self.tid, error='Connection failed')
        self.assertEqual(self.store.task(self.tid)['status'], 'failed')
        with self.assertRaises(ConflictError): self.store.change(self.tid, 3, 'run')
        self.store.change(self.tid, 3, 'reset')
        self.assertEqual(self.store.task(self.tid)['status'], 'proposed')

    def test_restart_recovers_interrupted_work(self):
        other = self.store.create('Interrupted plan')
        self.store.change(self.tid, 0, 'approve')
        self.store.change(self.tid, 1, 'run')
        Store(self.path).recover()
        self.assertEqual(self.store.mission(other)['status'], 'failed')
        self.assertEqual(self.store.task(self.tid)['status'], 'failed')

    def test_concurrent_approval_has_one_winner(self):
        barrier, results = threading.Barrier(2), []
        def approve():
            barrier.wait()
            try: self.store.change(self.tid, 0, 'approve'); results.append('ok')
            except ConflictError: results.append('conflict')
        workers = [threading.Thread(target=approve) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        self.assertCountEqual(results, ['ok','conflict'])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'db.sqlite3')

    def tearDown(self): self.temp.cleanup()

    def plan(self, replies):
        client = FakeClient(replies)
        coordinator = Coordinator(self.store, client)
        mid = coordinator.new_mission('A synthetic mission')
        coordinator.close()
        return self.store.mission(mid), client

    def test_plan_does_not_execute(self):
        mission, client = self.plan([json.dumps(PLAN)])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(mission['tasks'][0]['status'], 'proposed')
        self.assertFalse(mission['tasks'][0]['result'])

    def test_invalid_then_one_successful_repair(self):
        mission, client = self.plan(['{"extra":1}', json.dumps(PLAN)])
        self.assertEqual(mission['status'], 'ready')
        self.assertEqual(len(client.calls), 2)

    def test_two_invalid_responses_stop(self):
        mission, client = self.plan(['not json', 'still not json', json.dumps(PLAN)])
        self.assertEqual(mission['status'], 'failed')
        self.assertIn('one repair attempt', mission['error'])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(mission['tasks'], [])

    def test_connection_failure_not_retried(self):
        mission, client = self.plan([OllamaError('Offline')])
        self.assertEqual(mission['status'], 'failed')
        self.assertEqual(len(client.calls), 1)

    def test_unknown_role_never_saved(self):
        invalid = {**PLAN, 'tasks': [{**TASK,'role':'admin'}]}
        mission, _ = self.plan([json.dumps(invalid)] * 2)
        self.assertEqual(mission['tasks'], [])

    def test_approved_run_completes_as_text(self):
        mid = self.store.create('Synthetic mission')
        self.store.save_plan(mid, PLAN)
        task = self.store.mission(mid)['tasks'][0]
        client = FakeClient(['print("text only")'])
        coordinator = Coordinator(self.store, client)
        with self.assertRaises(ConflictError): coordinator.run_task(task['id'], 0)
        self.store.change(task['id'], 0, 'approve')
        self.assertEqual(len(client.calls), 0)
        coordinator.run_task(task['id'], 1)
        coordinator.close()
        saved = self.store.task(task['id'])
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['result'], 'print("text only")')

    def test_execution_failure_persisted(self):
        mid = self.store.create('Synthetic mission'); self.store.save_plan(mid, PLAN)
        task = self.store.mission(mid)['tasks'][0]
        self.store.change(task['id'], 0, 'approve')
        coordinator = Coordinator(self.store, FakeClient([OllamaError('Offline')]))
        coordinator.run_task(task['id'], 1); coordinator.close()
        self.assertEqual(self.store.task(task['id'])['status'], 'failed')

    def test_truncation_metrics_saved_without_partial_success(self):
        mid = self.store.create('Synthetic mission'); self.store.save_plan(mid, PLAN)
        task = self.store.mission(mid)['tasks'][0]
        self.store.change(task['id'], 0, 'approve')
        error=ModelResponseError('Response truncated; partial output not accepted.',{'done_reason':'length','eval_count':2400})
        coordinator=Coordinator(self.store,FakeClient([error]))
        coordinator.run_task(task['id'],1); coordinator.close()
        saved=self.store.task(task['id'])
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['result'],'')
        self.assertEqual(json.loads(saved['metrics'])['done_reason'],'length')
        with self.assertRaises(ConflictError): self.store.change(task['id'],saved['version'],'run')


class OllamaTests(unittest.TestCase):
    def setUp(self): self.client = OllamaClient(Config())

    def test_connection_error(self):
        self.client.opener.open = MagicMock(side_effect=urllib.error.URLError('refused'))
        with self.assertRaises(OllamaError): self.client._request('/api/version')

    def test_timeout_blocks_further_generation(self):
        self.client.opener.open = MagicMock(side_effect=socket.timeout())
        with self.assertRaises(OllamaError): self.client._request('/api/chat', {}, 1)
        self.assertTrue(self.client.blocked)
        with self.assertRaises(OllamaError): self.client.chat([])
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_http_error(self):
        self.client.opener.open = MagicMock(side_effect=urllib.error.HTTPError('url',500,'err',{},None))
        with self.assertRaisesRegex(OllamaError,'HTTP 500'): self.client._request('/api/version')

    def test_missing_or_cloud_model_refused(self):
        for models in ([], [{'name':'qwen3.5:9b','remote_host':'ollama.com'}]):
            with patch.object(self.client,'_request',side_effect=[{'version':'test'},{'models':models}]):
                self.assertFalse(self.client.health()['connected'])

    def test_format_and_per_request_context(self):
        response = dict(done=True,done_reason='stop',message={'content':'{}'})
        with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=response) as request:
            self.client.chat([], {'type':'object'})
            path, body, timeout = request.call_args.args
            self.assertEqual(body['options']['num_ctx'],4096)
            self.assertIn('format',body)
            self.assertNotIn('tools',body)

    def test_malformed_or_truncated_outputs(self):
        for response in ({'done':True,'done_reason':'length'}, {'done':False}, {'done':True,'message':{'content':''}}, {'done':True,'message':{'content':'ok','tool_calls':[{}]}}):
            with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=response):
                with self.assertRaises(ValidationError): self.client.chat([])

    def test_redirects_refused(self):
        from aihub.ollama import NoRedirect
        with self.assertRaises(OllamaError): NoRedirect().redirect_request(None,None,302,'',{},'https://example.com')

    def test_complete_response_and_role_budgets(self):
        response=dict(done=True,done_reason='stop',message={'content':'Complete answer','thinking':''},eval_count=42)
        for role,schema,budget in [('researcher',None,2400),('writer',None,2400),('coder',None,1000),(None,{'type':'object'},1400)]:
            with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=response) as request:
                content,metrics=self.client.chat([],schema,role=role)
                body=request.call_args.args[1]
                self.assertEqual(content,'Complete answer')
                self.assertEqual(body['options']['num_predict'],budget)
                self.assertEqual(body['options']['num_ctx'],4096)
                self.assertIs(body['think'],False)
                self.assertEqual('format' in body,schema is not None)
                self.assertEqual(metrics['done_reason'],'stop')

    def test_truncation_rejects_even_valid_looking_partial_output(self):
        response=dict(done=True,done_reason='length',message={'content':'{}','thinking':'internal'},eval_count=2400)
        with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=response):
            with self.assertRaises(ModelResponseError) as caught: self.client.chat([],role='researcher')
            self.assertEqual(caught.exception.metrics['eval_count'],2400)
            self.assertEqual(caught.exception.metrics['thinking_chars'],8)
            self.assertIn('Partial output was not accepted',str(caught.exception))

    def test_empty_and_malformed_final_answers(self):
        for message in ({'content':'   '},{'content':None},{'content':[]},'invalid',{'thinking':'only thought'}):
            response=dict(done=True,done_reason='stop',message=message)
            with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=response):
                with self.assertRaises(ModelResponseError): self.client.chat([])

    def test_unknown_finish_reason_and_incomplete_response(self):
        for done,reason in ((False,'stop'),(True,None),(True,'unexpected')):
            with patch.object(self.client,'health',return_value={'connected':True}), patch.object(self.client,'_request',return_value=dict(done=done,done_reason=reason,message={'content':'answer'})):
                with self.assertRaises(ModelResponseError): self.client.chat([])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name)/'http.sqlite3')
        cls.client = FakeClient([json.dumps(PLAN)] * 10)
        cls.coordinator = Coordinator(cls.store, cls.client)
        cls.server = make_server(Config(), cls.store, cls.coordinator, cls.client, port=0)
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.worker = threading.Thread(target=cls.server.serve_forever,daemon=True); cls.worker.start()
        cls.token = json.load(urllib.request.urlopen(cls.url+'/api/state'))['token']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.worker.join()
        cls.coordinator.close(); cls.temp.cleanup()

    def request(self,path,body=None,headers=None):
        request = urllib.request.Request(self.url+path, data=None if body is None else json.dumps(body).encode(), headers=headers or {'Content-Type':'application/json','X-AIHub-Token':self.token})
        try:
            with urllib.request.urlopen(request) as response: return response.status, response.read()
        except urllib.error.HTTPError as error: return error.code, error.read()

    def test_static_and_security_headers(self):
        with urllib.request.urlopen(self.url+'/') as response:
            self.assertIn(b'Mission control',response.read())
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
            self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')

    def test_health(self): self.assertEqual(self.request('/api/health')[0],200)

    def test_csrf_and_origin(self):
        self.assertEqual(self.request('/api/missions',{'prompt':'valid mission'},{'Content-Type':'application/json'})[0],403)
        self.assertEqual(self.request('/api/missions',{'prompt':'valid mission'},{'Content-Type':'application/json','X-AIHub-Token':self.token,'Origin':'https://evil.test'})[0],403)

    def test_host_rebinding(self): self.assertEqual(self.request('/api/state',headers={'Host':'evil.test'})[0],403)

    def test_invalid_mission_input(self):
        for body in ({'prompt':'x'},{'prompt':False},{'prompt':'valid mission','extra':1},{'prompt':'x'*3001}):
            self.assertEqual(self.request('/api/missions',body)[0],400)

    def test_unknown_path_and_traversal(self):
        for path in ('/../benchmark.py','/benchmark.py','/data/aihub.sqlite3','/api/missions/nope'):
            self.assertEqual(self.request(path)[0],404)

    def test_create_mission(self):
        status, value = self.request('/api/missions',{'prompt':'Draft a synthetic test function'})
        self.assertEqual(status,202)
        self.assertEqual(len(json.loads(value)['id']),32)

    def test_http_approval_and_edit_validation(self):
        mid = self.store.create('Synthetic mission'); self.store.save_plan(mid,PLAN)
        task = self.store.mission(mid)['tasks'][0]; path='/api/tasks/'+task['id']
        self.assertEqual(self.request(path+'/run',{'version':0})[0],409)
        self.assertEqual(self.request(path+'/edit',{'version':0,'task':{**TASK,'role':'admin'}})[0],400)
        self.assertEqual(self.request(path+'/approve',{'version':True})[0],400)
        self.assertEqual(self.request(path+'/approve',{'version':0})[0],200)
        self.assertEqual(self.request(path+'/approve',{'version':0})[0],409)

    def raw_request(self, path='/', method='GET', headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_cross_site_top_level_shell_navigation_allowed(self):
        headers = {'Sec-Fetch-Site':'cross-site', 'Sec-Fetch-Mode':'navigate',
                   'Sec-Fetch-Dest':'document', 'Sec-Fetch-User':'?1', 'Referer':'https://example.com/page'}
        status, response_headers, body = self.raw_request(headers=headers)
        self.assertEqual(status, 200)
        self.assertIn(b'Mission control', body)
        self.assertNotIn('Access-Control-Allow-Origin', response_headers)
        self.assertNotIn(self.token.encode(), body)
        self.assertNotIn(b'MVP Smoke Test', body)

    def test_direct_and_same_origin_navigation(self):
        for site in ('none','same-origin','same-site'):
            with self.subTest(site=site):
                self.assertEqual(self.raw_request(headers={'Sec-Fetch-Site':site,'Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})[0], 200)

    def test_cross_site_api_assets_and_embeds_remain_blocked(self):
        for path, mode, destination in [('/api/state','navigate','document'),
                ('/api/health','cors','empty'),('/app.js','no-cors','script'),
                ('/style.css','no-cors','style'),('/','navigate','iframe'),
                ('/','cors','empty'),('/','no-cors','image'),('/','navigate','frame'),
                ('/?q=1','navigate','document')]:
            with self.subTest(path=path,mode=mode,destination=destination):
                self.assertEqual(self.raw_request(path,headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':mode,'Sec-Fetch-Dest':destination})[0],403)

    def test_navigation_exception_never_allows_mutations(self):
        for site in ('cross-site','same-site'):
            headers={'Content-Type':'application/json','X-AIHub-Token':self.token,
                     'Sec-Fetch-Site':site,'Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
            for path in ('/','/api/missions'):
                self.assertEqual(self.raw_request(path,'POST',headers,'{"prompt":"test mission"}')[0],403)

    def test_explicit_foreign_origin_always_blocked(self):
        for origin in ('null','https://evil.test','http://localhost:'+str(self.server.server_port), self.url+'/',self.url+'0'):
            for path in ('/','/api/state'):
                headers={'Origin':origin,'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
                self.assertEqual(self.raw_request(path,headers=headers)[0],403)

    def test_same_origin_api_and_mutation_still_work(self):
        headers={'Origin':self.url,'Referer':self.url+'/', 'Sec-Fetch-Site':'same-origin',
                 'Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':'empty','Content-Type':'application/json','X-AIHub-Token':self.token}
        self.assertEqual(self.raw_request('/api/state',headers=headers)[0],200)
        mid=self.store.create('Header test mission'); self.store.save_plan(mid,PLAN)
        tid=self.store.mission(mid)['tasks'][0]['id']
        self.assertEqual(self.raw_request('/api/tasks/'+tid+'/approve','POST',headers,'{"version":0}')[0],200)

    def test_missing_metadata_supported_but_csrf_required(self):
        self.assertEqual(self.raw_request('/api/state')[0],200)
        self.assertEqual(self.raw_request('/api/missions','POST',{'Content-Type':'application/json'},'{"prompt":"test mission"}')[0],403)

    def test_referer_fallback_rejects_other_origins(self):
        for referer in ('https://evil.test/path', 'null', self.url.replace('127.0.0.1','localhost')+'/',self.url+'@evil.test/', 'http://[broken'):
            self.assertEqual(self.raw_request('/api/state',headers={'Referer':referer})[0],403)
        self.assertEqual(self.raw_request('/api/state',headers={'Referer':self.url+'/some/path?x=1'})[0],200)

    def test_localhost_only_redirects_shell_to_fixed_canonical_url(self):
        headers={'Host':'localhost:'+str(self.server.server_port),'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'}
        status,response_headers,body=self.raw_request(headers=headers)
        self.assertEqual(status,303)
        self.assertEqual(response_headers['Location'],self.url+'/')
        self.assertEqual(body,b'')
        self.assertNotIn('Access-Control-Allow-Origin',response_headers)
        for path in ('/api/state','/app.js','/?next=https://evil.test'):
            self.assertEqual(self.raw_request(path,headers=headers)[0],403)
        self.assertEqual(self.raw_request('/api/missions','POST',headers,'{}')[0],403)

    def test_host_allowlist_and_loopback_binding(self):
        self.assertEqual(self.server.server_address[0],'127.0.0.1')
        for host in ('evil.test', '127.0.0.1', self.url[7:]+'0', '127.0.0.1.evil.test:'+str(self.server.server_port), '[::1]:'+str(self.server.server_port)):
            self.assertEqual(self.raw_request(headers={'Host':host})[0],403)

    def test_cross_site_headers_cannot_be_omitted_from_exception(self):
        for headers in ({'Sec-Fetch-Site':'cross-site'}, {'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate'},
                        {'Sec-Fetch-Site':'cross-site','Sec-Fetch-Dest':'document'}, {'Sec-Fetch-Site':'bogus'}):
            self.assertEqual(self.raw_request(headers=headers)[0],403)

    def test_duplicate_security_headers_rejected(self):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.putrequest('GET','/')
            connection.putheader('Host','evil.test')
            connection.endheaders()
            response=connection.getresponse()
            self.assertEqual(response.status,403)
            response.read()
        finally: connection.close()

    def test_dashboard_bootstrap_after_external_navigation(self):
        self.assertEqual(self.raw_request(headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document'})[0],200)
        for path,dest in (('/style.css','style'),('/app.js','script'),('/api/state','empty'),('/api/health','empty')):
            headers={'Sec-Fetch-Site':'same-origin','Sec-Fetch-Mode':'cors','Sec-Fetch-Dest':dest,'Referer':self.url+'/'}
            status, response_headers, body=self.raw_request(path,headers=headers)
            self.assertEqual(status,200)
            self.assertNotIn('Access-Control-Allow-Origin',response_headers)
            if path=='/api/state': self.assertEqual(json.loads(body)['model'],'qwen3.5:9b')
        headers={'Origin':self.url,'Sec-Fetch-Site':'same-origin','Content-Type':'application/json','X-AIHub-Token':'wrong-token'}
        self.assertEqual(self.raw_request('/api/missions','POST',headers,'{"prompt":"test mission"}')[0],403)

    def test_navigation_diagnostics_do_not_log_secrets(self):
        headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document',
                 'Referer':'https://example.test/private?secret=query-secret#fragment',
                 'Cookie':'session=cookie-secret','X-AIHub-Token':'token-secret','User-Agent':'Opera test agent'}
        with self.assertLogs('aihub.server',level='INFO') as logs:
            self.assertEqual(self.raw_request(headers=headers)[0],200)
        output=' '.join(logs.output)
        self.assertIn('cross-site',output)
        self.assertIn('https://example.test',output)
        self.assertIn('Opera test agent',output)
        for secret in ('private','query-secret','cookie-secret','token-secret','fragment'):
            self.assertNotIn(secret,output)


if __name__ == '__main__': unittest.main()
