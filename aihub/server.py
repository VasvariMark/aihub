"""Small same-origin HTTP application, bound exclusively to IPv4 loopback."""
import sqlite3
from .providers import REGISTRY
from .storage import now
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

from .models import ValidationError, ConflictError, fields, parse_json, task_input, text, version
from .context import project_input

STATIC = Path(__file__).resolve().parent / 'static'
LOG = logging.getLogger(__name__)


def make_server(config, store, coordinator, client, port=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'AIHub/0.4'

        def log_message(self, fmt, *args):
            # Do not log mission prompts, outputs or arbitrary request URLs.
            LOG.info('http method=%s', self.command)

        def respond(self, code, value, content_type='application/json; charset=utf-8', location=None):
            payload = json.dumps(value, ensure_ascii=False).encode('utf-8') if isinstance(value, (dict, list)) else value
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            if location is not None:
                self.send_header('Location', location)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def boundary(self, mutation=False):
            expected = f'127.0.0.1:{self.server.server_port}'
            canonical = 'http://' + expected
            if self.command == 'GET' and self.path == '/':
                def logged_origin(name):
                    value = self.headers.get(name)
                    if value is None:
                        return None
                    try:
                        parsed = urlsplit(value)
                        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username is not None:
                            return '<invalid or opaque>'
                        return (parsed.scheme + '://' + parsed.netloc)[:160]
                    except ValueError:
                        return '<invalid>'
                # Navigation diagnostics only. Never log cookies, CSRF tokens,
                # mission content, or the Referer's path/query/fragment.
                LOG.info('navigation host=%r origin=%r referer_origin=%r fetch_site=%r mode=%r dest=%r user=%r agent=%r',
                         (self.headers.get('Host') or '')[:160], logged_origin('Origin'), logged_origin('Referer'),
                         (self.headers.get('Sec-Fetch-Site') or '')[:32], (self.headers.get('Sec-Fetch-Mode') or '')[:32],
                         (self.headers.get('Sec-Fetch-Dest') or '')[:32], (self.headers.get('Sec-Fetch-User') or '')[:8],
                         (self.headers.get('User-Agent') or '')[:240])
            for name in ('Host', 'Origin', 'Referer', 'Sec-Fetch-Site', 'Sec-Fetch-Mode',
                         'Sec-Fetch-Dest', 'Sec-Fetch-User', 'X-AIHub-Token'):
                if len(self.headers.get_all(name, [])) > 1:
                    raise PermissionError('Ambiguous security headers refused.')
            site = self.headers.get('Sec-Fetch-Site')
            mode = self.headers.get('Sec-Fetch-Mode')
            destination = self.headers.get('Sec-Fetch-Dest')
            # Only the public, data-free shell may be opened from another site.
            # Fetch/XHR, iframe, assets and every API endpoint get no exception.
            document_navigation = (not mutation and self.command == 'GET' and self.path == '/'
                                   and mode == 'navigate' and destination == 'document')
            host = self.headers.get('Host')
            localhost_redirect = (host == f'localhost:{self.server.server_port}'
                                  and not mutation and self.command == 'GET' and self.path == '/'
                                  and mode in (None, 'navigate') and destination in (None, 'document'))
            if host != expected and not localhost_redirect:
                raise PermissionError('Use the exact 127.0.0.1 application URL.')
            origin = self.headers.get('Origin')
            if origin is not None and origin != canonical:
                raise PermissionError('Cross-origin request refused.')
            if site not in (None, 'none', 'same-origin', 'same-site', 'cross-site'):
                raise PermissionError('Invalid fetch metadata refused.')
            if site in ('cross-site', 'same-site') and not document_navigation:
                raise PermissionError('Cross-site request refused.')
            referer = self.headers.get('Referer')
            if referer is not None and not document_navigation:
                try:
                    parsed = urlsplit(referer)
                    valid = parsed.scheme == 'http' and parsed.netloc == expected
                except ValueError:
                    valid = False
                if not valid:
                    raise PermissionError('Cross-origin referer refused.')
            if mutation and not hmac.compare_digest(self.headers.get('X-AIHub-Token', ''), token):
                raise PermissionError('Missing or invalid request token. Reload the dashboard.')
            return canonical + '/' if localhost_redirect else None

        def body(self):
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                raise ValidationError('Content-Type must be application/json.')
            if self.headers.get('Transfer-Encoding'):
                raise ValidationError('Chunked request bodies are not supported.')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError as exc:
                raise ValidationError('Invalid content length.') from exc
            maximum=300_000 if re.fullmatch(r'/api/tasks/[a-f0-9]{32}/manual/(prompt|import_result|correct_result)',self.path) else 24_000
            if not 1 <= size <= maximum:
                raise ValidationError(f'Request body must be 1–{maximum} bytes.')
            value = parse_json(self.rfile.read(size))
            if type(value) is not dict:
                raise ValidationError('Expected a JSON object.')
            return value

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, mutation):
            self.connection.settimeout(10)
            try:
                redirect = self.boundary(mutation)
                if redirect:
                    return self.respond(303, b'', location=redirect)
                path = self.path
                if not mutation:
                    if path in ('/', '/app.js', '/style.css'):
                        name, mime = {'/': ('index.html','text/html'), '/app.js': ('app.js','text/javascript'), '/style.css': ('style.css','text/css')}[path]
                        return self.respond(200, (STATIC/name).read_bytes(), mime+'; charset=utf-8')
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/copy/([a-f0-9]{32})',path)
                    if match: return self.respond(200,dict(prompt=store.approved_prompt(*match.groups())))
                    if path == '/api/overview': return self.respond(200,store.overview())
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/ledger',path)
                    if match: return self.respond(200,store.ledger(match[1]))
                    if path == '/api/state':
                        return self.respond(200, dict(providers={key:p.public() for key,p in REGISTRY.items()},token=token, model=config.model, context=config.context, roles=['researcher','writer','coder','reviewer'], missions=store.list_missions()))
                    if path == '/api/health':
                        return self.respond(200, client.health())
                    match=re.fullmatch(r'/api/projects/([a-f0-9]{32})/history',path)
                    if match: return self.respond(200,store.profile_history(match[1]))
                    if path == '/api/projects':
                        return self.respond(200,store.list_projects())
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/handoff',path)
                    if match:
                        return self.respond(200,store.handoff(match[1],config.context))
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/history',path)
                    if match:return self.respond(200,store.task_history(match[1]))
                    match = re.fullmatch(r'/api/missions/([a-f0-9]{32})', path)
                    if match:
                        return self.respond(200, store.mission(match[1]))
                else:
                    body = self.body()
                    if path=='/api/mission-search':
                        fields(body,('search','project','page'))
                        return self.respond(200,store.mission_page(body['search'],body['project'],body['page']))
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/cancel',path)
                    if match:
                        fields(body,('version',))
                        store.cancel_queued(match[1],version(body['version']),now())
                        return self.respond(200,{'ok':True})
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/details',path)
                    if match:
                        fields(body,('version','priority','notes','effort'))
                        store.update_details(match[1],body['version'],body['priority'],body['notes'],body['effort'],now())
                        return self.respond(200,{'ok':True})
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/review',path)
                    if match:
                        fields(body,('revision','version','decision','note'))
                        store.review_result(match[1],body['revision'],body['version'],body['decision'],body['note'],now())
                        return self.respond(200,{'ok':True})
                    match=re.fullmatch(r'/api/tasks/([a-f0-9]{32})/manual/(worker|prompt|start_external|import_result|correct_result|reject_result)',path)
                    if match:
                        tid,action=match.groups()
                        extra={'worker':('worker',),'prompt':('prompt',),'start_external':('confirm',),'import_result':('result','confirm'),'correct_result':('result','reason','confirm'),'reject_result':('reason','confirm')}[action]
                        if action in ('start_external','import_result') and 'handoff_id' in body: extra+=('handoff_id',)
                        fields(body,('version',)+extra)
                        store.manual_action(tid,version(body['version']),action,**{k:body[k] for k in extra})
                        return self.respond(200,{'ok':True})
                    if path=='/api/projects':
                        profile=project_input(body)
                        return self.respond(201,dict(id=store.save_project(profile)))
                    match=re.fullmatch(r'/api/projects/([a-f0-9]{32})/(edit|delete)',path)
                    if match:
                        pid,action=match.groups()
                        fields(body,('version','profile') if action=='edit' else ('version',))
                        revision=version(body['version'])
                        if action=='edit': store.save_project(project_input(body['profile']),pid,revision)
                        else: store.delete_project(pid,revision)
                        return self.respond(200,{'ok':True})
                    if path == '/api/missions':
                        fields(body, ('prompt',)+tuple(k for k in ('project_id','privacy') if k in body))
                        if body.get('privacy','external_allowed') not in ('external_allowed','local_only'): raise ValidationError('Invalid disclosure policy')
                        project_id=body.get('project_id')
                        if project_id is not None and (type(project_id) is not str or not re.fullmatch('[a-f0-9]{32}',project_id)):
                            raise ValidationError('Invalid project ID')
                        prompt = text(body['prompt'], 'Mission', 3000, 5)
                        mid = coordinator.new_mission(prompt,project_id,body['privacy']) if 'privacy' in body else coordinator.new_mission(prompt,project_id) if project_id else coordinator.new_mission(prompt)
                        return self.respond(202, {'id': mid})
                    match = re.fullmatch(r'/api/tasks/([a-f0-9]{32})/(approve|reject|edit|run|reset)', path)
                    if match:
                        tid, action = match.groups()
                        required=('version', 'task') if action=='edit' else ('version', 'handoff_digest') if action=='approve' and 'handoff_digest' in body else ('version',)
                        fields(body,required)
                        if 'handoff_digest' in body and (type(body['handoff_digest']) is not str or not re.fullmatch('[a-f0-9]{64}',body['handoff_digest'])):
                            raise ValidationError('Invalid handoff digest')
                        revision = version(body['version'])
                        edited = task_input(body['task']) if action == 'edit' else None
                        if action == 'run':
                            coordinator.run_task(tid, revision)
                        else:
                            if action=='approve':
                                preview=store.handoff(tid,config.context)
                                if not preview['ready']: raise ConflictError(preview['error'])
                            store.change(tid, revision, action, edited,body.get('handoff_digest'))
                        return self.respond(202 if action == 'run' else 200, {'ok': True})
                self.respond(404, {'error': 'Not found.'})
            except sqlite3.OperationalError:
                self.respond(503, {'error':'Storage unavailable. Check free space and database locks, then refresh before retrying.'})
            except PermissionError as exc:
                self.respond(403, {'error': str(exc)})
            except ValidationError as exc:
                self.respond(400, {'error': str(exc)})
            except ConflictError as exc:
                self.respond(409, {'error': str(exc)})
            except KeyError:
                self.respond(404, {'error': 'Not found.'})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                LOG.warning('http connection interrupted')
            except Exception:
                LOG.exception('http unexpected_error')
                self.respond(500, {'error': 'Internal error. Check the local log.'})

    server = ThreadingHTTPServer(('127.0.0.1', config.port if port is None else port), Handler)
    server.daemon_threads = True
    return server
