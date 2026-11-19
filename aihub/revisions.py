"""Transactional v4 history, approval receipts and human decisions."""
import json
import hashlib
from uuid import uuid4
from .models import ConflictError, ValidationError, text, version
from .context import digest
from .providers import REGISTRY

DDL = (
    "CREATE TABLE execution_attempts(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), run_version INTEGER NOT NULL, phase TEXT NOT NULL, queued_at TEXT NOT NULL, started_at TEXT, ended_at TEXT, UNIQUE(task_id,run_version))",
    "CREATE TABLE task_details(task_id TEXT PRIMARY KEY REFERENCES tasks(id), priority TEXT NOT NULL, notes TEXT NOT NULL, effort TEXT NOT NULL)",
    "CREATE TABLE handoffs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), task_version INTEGER NOT NULL, provider TEXT NOT NULL, snapshot TEXT NOT NULL, prompt_hash TEXT NOT NULL, context_hash TEXT NOT NULL, approved_at TEXT NOT NULL, UNIQUE(task_id,task_version))",
    "CREATE TABLE handoff_events(id INTEGER PRIMARY KEY, handoff_id TEXT NOT NULL REFERENCES handoffs(id), action TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE TABLE result_revisions(id INTEGER PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), version INTEGER NOT NULL, origin TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id,version))",
    "CREATE TABLE result_reviews(id INTEGER PRIMARY KEY, revision_id INTEGER NOT NULL REFERENCES result_revisions(id), decision TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX review_by_revision ON result_reviews(revision_id,id)",
    "CREATE INDEX tasks_by_mission ON tasks(mission_id,position)",
    "CREATE TABLE mission_settings(mission_id TEXT PRIMARY KEY REFERENCES missions(id), project_id TEXT, privacy TEXT NOT NULL)",
    "CREATE TABLE project_history(id INTEGER PRIMARY KEY, project_id TEXT NOT NULL, profile TEXT NOT NULL, version INTEGER NOT NULL)",
    """CREATE TRIGGER archive_profile BEFORE UPDATE ON projects BEGIN
       INSERT INTO project_history(project_id,profile,version) VALUES(OLD.id,OLD.profile,OLD.version); END""",
    """CREATE TRIGGER archive_removed_profile BEFORE DELETE ON projects BEGIN
       INSERT INTO project_history(project_id,profile,version) VALUES(OLD.id,OLD.profile,OLD.version); END""",
)


def migrate(db):
    db.execute("BEGIN IMMEDIATE")
    if db.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise RuntimeError("Expected schema 3 before v4 migration")
    for statement in DDL:
        db.execute(statement)
    db.execute("""INSERT INTO result_revisions(task_id,version,origin,body,created_at)
                  SELECT id,version,result_origin,result,updated_at FROM tasks WHERE result<>''""")
    for row in db.execute("SELECT * FROM tasks WHERE status IN ('approved','awaiting_external')").fetchall():
        db.execute("INSERT INTO task_history(task_id,snapshot) VALUES(?,?)",(row["id"],json.dumps(dict(row))))
        db.execute("UPDATE tasks SET status='proposed',approval_context='',version=version+1 WHERE id=?", (row["id"],))
    for table in ('handoffs','handoff_events','result_revisions','result_reviews','project_history'):
        for op in ('UPDATE','DELETE'):
            db.execute(f"""CREATE TRIGGER immutable_{table}_{op} BEFORE {op} ON {table}
                           BEGIN SELECT RAISE(ABORT,'Historical record is immutable'); END""")
    db.execute("""CREATE TRIGGER invalidate_receipt AFTER UPDATE OF status ON tasks
                  WHEN OLD.status IN ('approved','awaiting_external') AND NEW.status IN ('proposed','failed','rejected')
                  BEGIN INSERT INTO handoff_events(handoff_id,action,created_at)
                  SELECT id,'invalidated',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM handoffs
                  WHERE task_id=NEW.id AND OLD.worker<>'ollama' ORDER BY rowid DESC LIMIT 1; END""")
    db.execute("PRAGMA user_version=4")


class RevisionStore:
    def receipt(self, db, task, snapshot, stamp):
        prompt = snapshot["execution"]["prompt"]
        db.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?)",
                   (uuid4().hex, task["id"], task["version"], REGISTRY[task["worker"]].id,
                    json.dumps(snapshot), hashlib.sha256(prompt.encode()).hexdigest(), digest(snapshot), stamp))

    def current_result(self, db, tid):
        return db.execute("""SELECT r.*, COALESCE((SELECT decision FROM result_reviews WHERE revision_id=r.id
                         ORDER BY id DESC LIMIT 1),'unreviewed') AS decision FROM result_revisions r
                         WHERE task_id=? ORDER BY id DESC LIMIT 1""",(tid,)).fetchone()

    def record_result(self, db, tid):
        db.execute("""INSERT INTO result_revisions(task_id,version,origin,body,created_at)
                   SELECT id,version,result_origin,result,updated_at FROM tasks WHERE id=?""",(tid,))

    def review_result(self, tid, revision, expected, decision, note, stamp):
        version(expected)
        if type(revision) is not int or decision not in ('accepted','rejected','changes_requested'):
            raise ValidationError("Invalid review")
        note=text(note,"Review note",1600,0 if decision=="accepted" else 1)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            task=db.execute("SELECT * FROM tasks WHERE id=?",(tid,)).fetchone()
            result=self.current_result(db,tid)
            if not task or task["version"]!=expected or task["status"]!="completed" or not result or result["id"]!=revision:
                raise ConflictError("Review no longer matches current result")
            self.invalidate(db,task)
            db.execute("INSERT INTO result_reviews(revision_id,decision,note,created_at) VALUES(?,?,?,?)",(revision,decision,note,stamp))
            db.execute("UPDATE tasks SET version=version+1,updated_at=? WHERE id=?",(stamp,tid))
            db.execute("UPDATE missions SET updated_at=? WHERE id=?",(stamp,task["mission_id"]))
            self.event(db,task["mission_id"],"review_"+decision,tid)

    def approved_prompt(self, tid, handoff_id):
        with self.connection() as db:
            db.execute('BEGIN')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not task or task['status']!='approved' or task['worker']=='ollama':
                raise ConflictError('Approve the current external prompt before copying')
            receipt=db.execute('SELECT id,context_hash,snapshot FROM handoffs WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(tid,)).fetchone()
            if not receipt or receipt['id']!=handoff_id or receipt['context_hash']!=task['approval_context'] or digest(self.snapshot(db,task))!=receipt['context_hash']:
                raise ConflictError('This external prompt changed. Refresh and approve again')
            return json.loads(receipt['snapshot'])['execution']['prompt']

    def claim_local(self, tid, run_version, stamp):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute("UPDATE execution_attempts SET phase='running_locally',started_at=? WHERE task_id=? AND run_version=? AND phase='queued'",
                               (stamp,tid,run_version)).rowcount
            if changed:
                db.execute('UPDATE missions SET updated_at=? WHERE id=(SELECT mission_id FROM tasks WHERE id=?)',(stamp,tid))
            return bool(changed)

    def cancel_queued(self, tid, expected, stamp):
        version(expected)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not task or task['version']!=expected or task['status']!='running':
                raise ConflictError('Refresh and check the current attempt')
            changed=db.execute("UPDATE execution_attempts SET phase='cancelled',ended_at=? WHERE task_id=? AND phase='queued'",(stamp,tid)).rowcount
            if not changed: raise ConflictError('Generation already started')
            db.execute("UPDATE tasks SET status='failed',error='Queued attempt cancelled by user',approval_context='',version=version+1,updated_at=? WHERE id=?",(stamp,tid))
            db.execute('UPDATE missions SET updated_at=? WHERE id=?',(stamp,task['mission_id']))
            self.event(db,task['mission_id'],'cancelled',tid)

    def update_details(self, tid, expected, priority, notes, effort, stamp):
        version(expected)
        if priority not in ('low','normal','high'):
            raise ValidationError('Unknown priority')
        notes = text(notes,'Coordinator notes',1600,0)
        effort = text(effort,'Estimated effort',80,0)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
            if not task or task['version'] != expected:
                raise ConflictError('Task changed. Refresh before saving.')
            db.execute('INSERT OR REPLACE INTO task_details VALUES(?,?,?,?)',(tid,priority,notes,effort))
            db.execute('UPDATE tasks SET version=version+1,updated_at=? WHERE id=?',(stamp,tid))
            db.execute('UPDATE missions SET updated_at=? WHERE id=?',(stamp,task['mission_id']))
            self.event(db,task['mission_id'],'coordination_notes_updated',tid)

    def mission_page(self, search, project_id, page):
        if type(page) is not int or not 0 <= page <= 1000000:
            raise ValidationError('Invalid page number')
        search = text(search,'Search',160,0)
        if type(project_id) is not str or (project_id and (len(project_id)!=32 or not all(c in '0123456789abcdef' for c in project_id))):
            raise ValidationError('Invalid project filter')
        with self.connection() as db:
            rows = db.execute("""SELECT m.id,m.title,m.status,m.updated_at,m.created_at,s.project_id
                FROM missions m LEFT JOIN mission_settings s ON m.id=s.mission_id
                WHERE instr(lower(m.title),lower(?))>0 AND (?='' OR s.project_id=?)
                ORDER BY m.created_at DESC,m.id LIMIT 26 OFFSET ?""",(search,project_id,project_id,page*25)).fetchall()
            return dict(missions=[dict(r) for r in rows[:25]],more=len(rows)>25)

    def overview(self):
        with self.connection() as db:
            states=dict(db.execute("SELECT status,count(*) FROM tasks GROUP BY status").fetchall())
            workers=dict(db.execute("SELECT worker,count(*) FROM tasks GROUP BY worker").fetchall())
            queue=[dict(r) for r in db.execute("""SELECT t.id,t.mission_id,t.title,t.worker,t.status,
                       COALESCE((SELECT v.decision FROM result_reviews v JOIN result_revisions r ON r.id=v.revision_id
                        WHERE r.id=(SELECT max(id) FROM result_revisions WHERE task_id=t.id)
                        ORDER BY v.id DESC LIMIT 1),'unreviewed') AS review
                       FROM tasks t WHERE t.status IN ('proposed','completed','awaiting_external')
                       ORDER BY t.updated_at DESC LIMIT 100""")]
            decisions=dict(db.execute("""SELECT COALESCE((SELECT decision FROM result_reviews v WHERE v.revision_id=r.id
                      ORDER BY id DESC LIMIT 1),'unreviewed'),count(*) FROM tasks t JOIN result_revisions r
                      ON r.id=(SELECT max(id) FROM result_revisions WHERE task_id=t.id)
                      WHERE t.status='completed' GROUP BY 1"""))
            avg_local=db.execute("""SELECT avg(json_extract(metrics,'$.total_duration'))/1000000000.0 FROM tasks
                      WHERE status='completed' AND worker='ollama' AND json_type(metrics,'$.total_duration') IN ('real','integer')
                      AND json_extract(metrics,'$.total_duration')>0""").fetchone()[0]
            return dict(states=states,workers=workers,queue=queue,reviews=decisions,local_seconds=avg_local,
                        missions=db.execute("SELECT count(*) FROM missions").fetchone()[0])

    def ledger(self,tid):
        self.task(tid)
        with self.connection() as db:
            revisions=[dict(r) for r in db.execute("SELECT * FROM result_revisions WHERE task_id=? ORDER BY id DESC LIMIT 50",(tid,))]
            for row in revisions:
                row["reviews"]=[dict(r) for r in db.execute("SELECT * FROM result_reviews WHERE revision_id=? ORDER BY id",(row["id"],))]
            receipts=[dict(r) for r in db.execute("SELECT * FROM handoffs WHERE task_id=? ORDER BY rowid DESC LIMIT 50",(tid,))]
            for row in receipts:
                row["events"]=[dict(r) for r in db.execute("SELECT action,created_at FROM handoff_events WHERE handoff_id=? ORDER BY id",(row["id"],))]
            attempts=[dict(r) for r in db.execute('SELECT * FROM execution_attempts WHERE task_id=? ORDER BY id DESC LIMIT 50',(tid,))]
            return dict(revisions=revisions,handoffs=receipts,attempts=attempts)
