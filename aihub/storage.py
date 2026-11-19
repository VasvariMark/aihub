"""Transactional SQLite persistence; no user-provided filesystem paths."""
from contextlib import contextmanager, closing
import datetime as dt
import json
import sqlite3
import uuid
from pathlib import Path

from .models import ConflictError
from .projects import ProjectStore
from .context import digest
from .manual import ManualBridge


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


from .revisions import RevisionStore, migrate
from .providers import recommendation

class Store(ManualBridge, ProjectStore, RevisionStore):
    def __init__(self, path, config=None):
        self.config=config
        self.path = str(path)
        if Path(path).exists() and Path(path).stat().st_size:
            with closing(sqlite3.connect(self.path)) as source:
                schema=source.execute('PRAGMA user_version').fetchone()[0]
                if schema>4: raise RuntimeError('Unsupported future database schema')
                if schema==4: return
                if schema < 4:
                    label='pre-v02-' if source.execute('PRAGMA user_version').fetchone()[0]<2 else 'pre-v03-manual-'
                    if schema==3: label='pre-v04-'
                    backup = Path(str(path)+'.'+label+uuid.uuid4().hex+'.sqlite3')
                    with backup.open('xb'): pass
                    with closing(sqlite3.connect(backup)) as target: source.backup(target)
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS missions (
                  id TEXT PRIMARY KEY, prompt TEXT NOT NULL, title TEXT NOT NULL,
                  summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                  error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id), position INTEGER NOT NULL,
                  title TEXT NOT NULL, role TEXT NOT NULL, instructions TEXT NOT NULL, expected_output TEXT NOT NULL,
                  status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                  metrics TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL,
                  task_id TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
        self.migrate_v2()
        self.migrate_manual()
        with self.connection() as db: migrate(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def event(self, db, mission_id, action, task_id=None):
        db.execute('INSERT INTO events(mission_id,task_id,action,created_at) VALUES(?,?,?,?)', (mission_id, task_id, action, now()))

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE execution_attempts SET phase='outcome_unknown',ended_at=? WHERE phase IN ('queued','running_locally')",(now(),))
            db.execute("UPDATE missions SET status='failed',error='Planning interrupted by application restart.',updated_at=? WHERE status='planning'", (now(),))
            db.execute("UPDATE tasks SET status='failed',error='Execution interrupted. Review and re-approve before retrying.',version=version+1,updated_at=? WHERE status='running'", (now(),))

    def create(self, prompt, project_id=None, privacy='external_allowed'):
        if privacy not in ('external_allowed','local_only'): raise ValueError('Invalid privacy')
        mid, stamp = uuid.uuid4().hex, now()
        with self.connection() as db:
            profile={}
            if project_id:
                row=db.execute('SELECT profile FROM projects WHERE id=?',(project_id,)).fetchone()
                if not row: raise KeyError('Project not found')
                profile=json.loads(row['profile'])
                if profile.get('project_status')=='archived': raise ConflictError('Project is archived')
                if profile.get('privacy')=='local_only': privacy='local_only'
            db.execute('INSERT INTO missions(id,prompt,title,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (mid, prompt, prompt[:100], 'planning', stamp, stamp))
            self.event(db, mid, 'mission_created')
            db.execute('UPDATE missions SET project_context=? WHERE id=?',(json.dumps(profile),mid))
            db.execute('INSERT INTO mission_settings VALUES(?,?,?)',(mid,project_id,privacy))
        return mid

    def save_plan(self, mid, plan):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT status FROM missions WHERE id=?', (mid,)).fetchone()['status'] != 'planning':
                raise ConflictError('Mission is no longer planning.')
            stamp = now()
            db.execute("UPDATE missions SET title=?,summary=?,status='ready',error='',updated_at=? WHERE id=?", (plan['title'], plan['summary'], stamp, mid))
            ids=[uuid.uuid4().hex for _ in plan['tasks']]
            for position, task in enumerate(plan['tasks']):
                db.execute('INSERT INTO tasks(id,mission_id,position,title,role,instructions,expected_output,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (ids[position], mid, position, task['title'], task['role'], task['instructions'], task['expected_output'], 'proposed', stamp, stamp))
            for position,task in enumerate(plan['tasks']):
                indices=task.get('depends_on',[])
                if any(type(i) is not int or not 1<=i<=len(ids) for i in indices):
                    raise ConflictError('Invalid dependency task number')
                deps=[ids[i-1] for i in indices]
                row=db.execute('SELECT * FROM tasks WHERE id=?',(ids[position],)).fetchone()
                self.validate_dependencies(db,row,deps)
                db.execute('UPDATE tasks SET dependencies=? WHERE id=?',(json.dumps(deps),ids[position]))
            self.event(db, mid, 'plan_validated')

    def fail_plan(self, mid, error):
        with self.connection() as db:
            db.execute("UPDATE missions SET status='failed',error=?,updated_at=? WHERE id=?", (error[:2000], now(), mid))
            self.event(db, mid, 'planning_failed')

    def list_missions(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT m.id,title,status,error,created_at,updated_at,s.project_id FROM missions m LEFT JOIN mission_settings s ON s.mission_id=m.id ORDER BY created_at DESC LIMIT 100')]

    def mission(self, mid):
        with self.connection() as db:
            row = db.execute('SELECT * FROM missions WHERE id=?', (mid,)).fetchone()
            if not row:
                raise KeyError('Mission not found.')
            result = dict(row)
            result['tasks'] = [dict(t) for t in db.execute('SELECT * FROM tasks WHERE mission_id=? ORDER BY position', (mid,))]
            for task in result['tasks']:
                current=self.current_result(db,task['id']) if task['result'] else None
                task['revision_id']=current['id'] if current else None
                task['review_state']=current['decision'] if current else 'unreviewed'
                receipt=db.execute('SELECT id FROM handoffs WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(task['id'],)).fetchone()
                task['handoff_id']=receipt[0] if receipt else None
                policy=db.execute('SELECT privacy FROM mission_settings WHERE mission_id=?',(mid,)).fetchone()
                privacy=policy[0] if policy else 'external_allowed'
                task['privacy']=privacy
                details=db.execute('SELECT priority,notes,effort FROM task_details WHERE task_id=?',(task['id'],)).fetchone()
                task['details']=dict(details) if details else dict(priority='normal',notes='',effort='')
                task['routing']=recommendation(task['role'],privacy,json.loads(result['project_context']).get('preferred_worker'))
                attempt=db.execute('SELECT phase FROM execution_attempts WHERE task_id=? ORDER BY id DESC LIMIT 1',(task['id'],)).fetchone()
                task['execution_state']=attempt['phase'] if attempt and task['status'] in ('failed','running') else 'succeeded' if task['status']=='completed' else task['status']
                task['metrics'] = json.loads(task['metrics'])
                task['dependencies'] = json.loads(task['dependencies'])
                task['used_context'] = json.loads(task['used_context']) if task['used_context'] else None
            result['project_context']=json.loads(result['project_context'])
            counts = {status: sum(t['status'] == status for t in result['tasks']) for status in ('proposed','approved','running','awaiting_external','completed','failed','rejected')}
            result['counts'] = counts
            result['events'] = [dict(e) for e in db.execute('SELECT task_id,action,created_at FROM events WHERE mission_id=? ORDER BY id DESC LIMIT 100', (mid,))]
            return result

    def task(self, tid):
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not row:
                raise KeyError('Task not found.')
            return dict(row)

    def change(self, tid, expected_version, action, edited=None, handoff_digest=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not row:
                raise KeyError('Task not found.')
            task = dict(row)
            if task['version'] != expected_version:
                raise ConflictError('Task changed. Refresh and review it again.')
            if action=='run' and task['worker']!='ollama':raise ConflictError('Manual Codex tasks cannot run locally. Confirm external start instead.')
            allowed = {'approve': ('proposed',), 'reject': ('proposed','approved'),
                       'run': ('approved',), 'edit': ('proposed','approved','failed','completed'), 'reset': ('approved','failed','rejected','awaiting_external')}
            if action not in allowed or task['status'] not in allowed[action]:
                raise ConflictError(f"Cannot {action} a {task['status']} task.")
            if action in ('approve','run'):
                snapshot=self.snapshot(db,task)
                fingerprint=digest(snapshot)
                if action=='approve':
                    if (self.config is not None or task['worker']!='ollama' or snapshot['dependency_results'] or snapshot['project_context']) and handoff_digest!=fingerprint:
                        raise ConflictError('Review the exact handoff/project context before approval.')
                    if task['worker']!='ollama': self.receipt(db,task,snapshot,now())
                    db.execute('UPDATE tasks SET approval_context=? WHERE id=?',(fingerprint,tid))
                elif task['approval_context']!=fingerprint:
                    raise ConflictError('Dependency context changed; return to review and approve again.')
                if action=='run':
                    db.execute("INSERT INTO execution_attempts(task_id,run_version,phase,queued_at) VALUES(?,?,'queued',?)",(tid,expected_version+1,now()))
                    db.execute('UPDATE tasks SET used_context=? WHERE id=?',(json.dumps(snapshot),tid))
            if action in ('edit','reset','reject'):
                self.invalidate(db,task)
                db.execute("UPDATE tasks SET approval_context='' WHERE id=?",(tid,))
            if action in ('edit','reset','run'):
                db.execute('INSERT INTO task_history(task_id,snapshot) VALUES(?,?)',(tid,json.dumps(task)))
            if edited is not None and 'dependencies' in edited:
                self.validate_dependencies(db,task,edited['dependencies'])
                db.execute('UPDATE tasks SET dependencies=? WHERE id=?',(json.dumps(edited['dependencies']),tid))
            status = {'approve':'approved','reject':'rejected','run':'running','edit':'proposed','reset':'proposed'}[action]
            db.execute('UPDATE tasks SET status=?,version=version+1,updated_at=? WHERE id=?', (status, now(), tid))
            if action in ('edit','reset','run'):
                db.execute("UPDATE tasks SET error='',result='',metrics='{}' WHERE id=?", (tid,))
                db.execute("UPDATE tasks SET result_origin='',manual_started_at='' WHERE id=?",(tid,))
                if action=='edit':db.execute("UPDATE tasks SET manual_prompt='' WHERE id=?",(tid,))
            if edited:
                db.execute('UPDATE tasks SET title=?,role=?,instructions=?,expected_output=? WHERE id=?', (edited['title'], edited['role'], edited['instructions'], edited['expected_output'], tid))
            db.execute('UPDATE missions SET updated_at=? WHERE id=?', (now(), task['mission_id']))
            self.event(db, task['mission_id'], action, tid)
        return self.task(tid)

    def finish(self, tid, result='', error='', metrics=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            if not task or task['status'] != 'running':
                raise ConflictError('Task is not running.')
            status = 'failed' if error else 'completed'
            db.execute('UPDATE tasks SET status=?,result=?,error=?,metrics=?,result_origin=?,version=version+1,updated_at=? WHERE id=?',
                       (status, result, error[:2000], json.dumps(metrics or {}), 'ollama' if result else '', now(), tid))
            db.execute('UPDATE missions SET updated_at=? WHERE id=?', (now(), task['mission_id']))
            db.execute("UPDATE execution_attempts SET phase=?,ended_at=? WHERE task_id=? AND phase IN ('queued','running_locally')",('failed' if error else 'succeeded',now(),tid))
            if not error: self.record_result(db,tid)
            self.event(db, task['mission_id'], status, tid)
