"""SQLite v2 additions. All dependency/approval checks run in write transactions."""
import json
import uuid
from .providers import REGISTRY
from .context import SEEDS, project_input, dependency_ids, digest, execution_messages, context_budget
from .models import ConflictError, ValidationError, parse_json


class ProjectStore:
    def migrate_v2(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 3:
                raise RuntimeError('Database is newer than this AIHub version.')
            if version==3:return
            db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, profile TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS task_history (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, snapshot TEXT NOT NULL)')
            for table, additions in [('missions', [('project_context', "TEXT NOT NULL DEFAULT '{}'")]), ('tasks', [('dependencies', "TEXT NOT NULL DEFAULT '[]'"), ('approval_context', "TEXT NOT NULL DEFAULT ''"), ('used_context', "TEXT NOT NULL DEFAULT ''")])]:
                columns = {r['name'] for r in db.execute('PRAGMA table_info('+table+')')}
                for name, definition in additions:
                    if name not in columns:
                        db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
            if version < 2:
                for profile in SEEDS:
                    db.execute('INSERT INTO projects(id,profile) VALUES(?,?)', (uuid.uuid4().hex,json.dumps(profile)))
            db.execute('PRAGMA user_version=2')

    def list_projects(self):
        with self.connection() as db:
            return [dict(id=r['id'],version=r['version'],**json.loads(r['profile'])) for r in db.execute('SELECT * FROM projects ORDER BY rowid')]

    def save_project(self, profile, pid=None, version=None):
        profile = project_input(profile)
        with self.connection() as db:
            if pid is None:
                pid=uuid.uuid4().hex
                db.execute('INSERT INTO projects(id,profile) VALUES(?,?)',(pid,json.dumps(profile)))
            elif db.execute('UPDATE projects SET profile=?,version=version+1 WHERE id=? AND version=?',(json.dumps(profile),pid,version)).rowcount != 1:
                raise ConflictError('Project changed or was deleted. Refresh before editing.')
        return pid

    def delete_project(self,pid,version):
        with self.connection() as db:
            if db.execute('DELETE FROM projects WHERE id=? AND version=?',(pid,version)).rowcount != 1:
                raise ConflictError('Project changed or was deleted.')

    def profile_history(self, pid):
        with self.connection() as db:
            return [dict(version=r['version'],profile=json.loads(r['profile'])) for r in db.execute(
                'SELECT version,profile FROM project_history WHERE project_id=? ORDER BY id DESC LIMIT 100',(pid,))]

    def snapshot(self,db,task):
        mission=db.execute('SELECT * FROM missions WHERE id=?',(task['mission_id'],)).fetchone()
        results=[]
        for tid in dependency_ids(parse_json(task['dependencies'])):
            dep=db.execute('SELECT * FROM tasks WHERE id=? AND mission_id=?',(tid,task['mission_id'])).fetchone()
            if not dep or dep['status']!='completed' or not isinstance(dep['result'],str) or not dep['result'].strip() or len(dep['result'])>40000:
                raise ConflictError('Dependency missing, incomplete, rejected, failed or has an invalid/empty result: '+tid)
            artifact=self.current_result(db,tid)
            decision=artifact['decision'] if artifact else 'unreviewed'
            if decision in ('rejected','changes_requested') or (task['role']!='reviewer' and decision!='accepted' and (self.config is not None or dep['worker']!='ollama')):
                raise ConflictError('Dependency needs an accepted human review: '+tid)
            if not artifact or artifact['body']!=dep['result']: raise ConflictError('Result revision differs from saved output')
            results.append(dict(content_hash=digest(dep['result']),review=decision, revision_id=artifact['id'] if artifact else None,id=tid,title=dep['title'],version=dep['version'],label='UNVERIFIED MANUALLY IMPORTED EXTERNAL OUTPUT' if dep['result_origin'].startswith('manual_') else 'UNVERIFIED AI-GENERATED OUTPUT',result=dep['result']))
        snapshot=dict(original_user_instructions=mission['prompt'],project_context=json.loads(mission['project_context']),task={k:task[k] for k in ('title','role','instructions','expected_output')},dependency_results=results)
        policy=db.execute('SELECT privacy FROM mission_settings WHERE mission_id=?',(task['mission_id'],)).fetchone()
        privacy=policy[0] if policy else 'external_allowed'
        snapshot['policy']=dict(version=4,privacy=privacy,dependency='unreviewed_allowed' if task['role']=='reviewer' else 'accepted_required' if self.config is not None else 'legacy_local_preview')
        if self.config and task['worker']=='ollama': snapshot['policy'].update(model=self.config.model,context=self.config.context)
        if task['worker']!='ollama':
            if privacy=='local_only': raise ConflictError('External disclosure prohibited for this mission')
            from .manual import default_prompt
            from .models import text
            prompt=text(task['manual_prompt'] or default_prompt(snapshot,task['worker']),'Complete manual prompt (shorten/split oversized handoffs)',60000)
            snapshot['execution']=dict(provider=REGISTRY[task['worker']].id,worker=task['worker'],mode='manual copy/paste; no automatic execution',prompt=prompt)
        return snapshot

    def handoff(self,tid,context=4096):
        with self.connection() as db:
            db.execute('BEGIN')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not task: raise KeyError('Task not found')
            try:
                snapshot=self.snapshot(db,task)
                budget=dict(input_upper_bound=len(snapshot['execution']['prompt'].encode()),output_budget=None,context='Manual desktop session — check its limits yourself') if task['worker']!='ollama' else context_budget(execution_messages(snapshot),context,2400 if task['role'] in ('researcher','writer') else 1000)
                return dict(ready=True,snapshot=snapshot,digest=digest(snapshot),budget=budget)
            except (ConflictError,ValidationError) as exc:
                return dict(ready=False,error=str(exc))

    def execution_snapshot(self,tid):
        with self.connection() as db:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            snapshot=self.snapshot(db,task)
            if task['status']!='running' or digest(snapshot)!=task['approval_context']:
                raise ConflictError('Handoff changed since approval; review again.')
            return snapshot

    def descendants(self,db,tid,mid):
        graph={r['id']:json.loads(r['dependencies']) for r in db.execute('SELECT id,dependencies FROM tasks WHERE mission_id=?',(mid,))}
        found=set()
        while True:
            extra={key for key,deps in graph.items() if key not in found and (tid in deps or any(d in found for d in deps))}
            if not extra: return found
            found.update(extra)

    def validate_dependencies(self,db,task,deps):
        dependency_ids(deps)
        descendants=self.descendants(db,task['id'],task['mission_id'])
        for tid in deps:
            if tid==task['id'] or tid in descendants:
                raise ValidationError('Circular dependency rejected.')
            if not db.execute('SELECT 1 FROM tasks WHERE id=? AND mission_id=?',(tid,task['mission_id'])).fetchone():
                raise ValidationError('Dependency must exist in the same mission.')

    def invalidate(self,db,task):
        for tid in self.descendants(db,task['id'],task['mission_id']):
            row=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if row['status']=='running': raise ConflictError('A downstream task is running. Wait before changing its prerequisite.')
            db.execute('INSERT INTO task_history(task_id,snapshot) VALUES(?,?)',(tid,json.dumps(dict(row))))
            status='failed' if row['status']=='completed' else 'proposed' if row['status'] in ('approved','awaiting_external') else row['status']
            self.event(db,task['mission_id'],'prerequisite_changed',tid)
            db.execute("UPDATE tasks SET status=?,approval_context='',manual_prompt='',manual_started_at='',version=version+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),error='Prerequisite changed; previous output is stale. Review again.' WHERE id=?",(status,tid))
