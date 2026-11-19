"""Offline v4 contract tests with fresh databases and synthetic reports."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aihub.config import Config
from aihub.context import SEEDS
from aihub.models import ConflictError, ValidationError
from aihub.storage import Store, now
from aihub.providers import REGISTRY
from tests.test_v2 import plan


class Workflow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'fixture.sqlite3'
        self.store = Store(self.path, Config())
        self.mid = self.store.create('Synthetic report. Saturday at ten.')
        self.store.save_plan(self.mid, plan())
        self.a,self.b,self.c = [t['id'] for t in self.store.mission(self.mid)['tasks']]

    def act(self, tid, action, **kwargs):
        return self.store.manual_action(tid,self.store.task(tid)['version'],action,**kwargs)

    def approve(self, tid):
        p=self.store.handoff(tid)
        self.assertTrue(p['ready'],p.get('error'))
        self.store.change(tid,self.store.task(tid)['version'],'approve',handoff_digest=p['digest'])

    def start(self, tid, worker):
        self.act(tid,'worker',worker=worker)
        self.approve(tid)
        hid=self.store.ledger(tid)['handoffs'][0]['id']
        self.act(tid,'start_external',handoff_id=hid,confirm=True)
        return hid

    def finish(self, tid, worker):
        hid=self.start(tid,worker)
        self.act(tid,'import_result',handoff_id=hid,confirm=True,result='Synthetic <b>report</b>.')
        return self.store.ledger(tid)['revisions'][0]['id']

    def decide(self,tid,revision,decision):
        self.store.review_result(tid,revision,self.store.task(tid)['version'],decision,'Checked fixture',now())

    def test_three_provider_chain(self):
        with patch('subprocess.Popen',side_effect=AssertionError),patch('socket.create_connection',side_effect=AssertionError):
            first=self.finish(self.a,'manual_claude')
            self.assertFalse(self.store.handoff(self.b)['ready'])
            self.decide(self.a,first,'accepted')
            self.finish(self.b,'manual_codex')
            preview=self.store.handoff(self.c)
            self.assertTrue(preview['ready'])
            self.assertEqual(preview['snapshot']['dependency_results'][0]['review'],'unreviewed')
            self.finish(self.c,'manual_gemini')
            self.assertEqual(self.store.mission(self.mid)['counts']['completed'],3)

    def test_mismatched_and_repeated_import(self):
        hid=self.start(self.a,'manual_gemini')
        for wrong in [None,'other-task']:
            with self.assertRaises(ConflictError):
                self.act(self.a,'import_result',handoff_id=wrong,result='wrong',confirm=True)
        self.act(self.a,'import_result',handoff_id=hid,result='valid',confirm=True)
        with self.assertRaises(ConflictError):
            self.act(self.a,'import_result',handoff_id=hid,result='again',confirm=True)
        self.assertEqual(len(self.store.ledger(self.a)['revisions']),1)

    def test_review_correction_and_reopen(self):
        revision=self.finish(self.a,'manual_claude')
        for decision in ['changes_requested','rejected','accepted']:
            self.decide(self.a,revision,decision)
        self.approve(self.b)
        self.act(self.a,'correct_result',result='New report',reason='Clarification',confirm=True)
        self.assertEqual(self.store.task(self.b)['status'],'proposed')
        with self.assertRaises(ConflictError): self.decide(self.a,revision,'accepted')
        ledger=Store(self.path,Config()).ledger(self.a)
        self.assertEqual(len(ledger['revisions'][1]['reviews']),3)
        self.assertEqual(ledger['revisions'][0]['reviews'],[])

    def test_local_privacy_and_archived_project(self):
        profile=dict(SEEDS[0],name='Demo',privacy='local_only')
        pid=self.store.save_project(profile)
        mid=self.store.create('Private mission',pid,'external_allowed')
        self.store.save_plan(mid,plan())
        tid=self.store.mission(mid)['tasks'][0]['id']
        with self.assertRaises(ConflictError): self.act(tid,'worker',worker='manual_gemini')
        self.store.save_project(dict(profile,project_status='archived'),pid,0)
        self.assertEqual(self.store.profile_history(pid)[0]['profile'],profile)
        with self.assertRaises(ConflictError): self.store.create('Archived',pid)

    def test_switch_revokes_approval_and_receipt_is_preserved(self):
        self.act(self.a,'worker',worker='manual_claude')
        self.approve(self.a)
        first=self.store.ledger(self.a)['handoffs'][0]
        self.act(self.a,'worker',worker='manual_gemini')
        self.assertEqual(self.store.task(self.a)['status'],'proposed')
        self.approve(self.a)
        latest,old=self.store.ledger(self.a)['handoffs']
        self.assertNotEqual(first['id'],latest['id'])
        self.assertEqual(old['snapshot'],first['snapshot'])
        self.assertEqual(old['events'][-1]['action'],'invalidated')
        self.assertIn('Gemini',json.loads(latest['snapshot'])['execution']['prompt'])

    def test_local_model_configuration_changes_block_run(self):
        self.approve(self.a)
        other=Store(self.path,Config(context=8192))
        with self.assertRaises(ConflictError): other.change(self.a,1,'run')

    def test_bad_provider_and_credential_inputs(self):
        for worker in ['api','cli',[],None]:
            with self.assertRaises(ValidationError): self.act(self.a,'worker',worker=worker)
        self.act(self.a,'worker',worker='manual_claude')
        with self.assertRaises(ValidationError): self.act(self.a,'prompt',prompt='sk-'+('x'*30))
        self.assertEqual({p.mode for p in REGISTRY.values()},{'local','manual'})

    def test_overview_omits_report_bodies(self):
        self.finish(self.a,'manual_codex')
        data=self.store.overview()
        self.assertNotIn('<b>report',json.dumps(data))
        self.assertEqual(data['states']['completed'],1)

    def test_private_notes_do_not_enter_handoff(self):
        self.store.update_details(self.a,0,'high','Private reasoning','20 min',now())
        p=self.store.handoff(self.a)
        self.assertNotIn('Private reasoning',json.dumps(p))
        self.assertEqual(self.store.mission(self.mid)['tasks'][0]['details']['priority'],'high')
        with self.assertRaises(ConflictError): self.store.update_details(self.a,0,'low','','',now())

    def test_mission_search_returns_disjoint_pages(self):
        for i in range(26): self.store.create('Catalog item '+str(i))
        first=self.store.mission_page('Catalog','',0)
        second=self.store.mission_page('Catalog','',1)
        self.assertTrue(first['more'])
        self.assertEqual(len(second['missions']),1)
        self.assertFalse({r['id'] for r in first['missions']} & {r['id'] for r in second['missions']})
        with self.assertRaises(ValidationError): self.store.mission_page('','',-1)

    def test_immutable_receipt_and_rollback(self):
        self.start(self.a,'manual_gemini')
        with self.assertRaises(sqlite3.IntegrityError), self.store.connection() as db:
            db.execute("UPDATE handoffs SET prompt_hash='wrong'")
        self.assertEqual(len(self.store.ledger(self.a)['handoffs'][0]['prompt_hash']),64)

    def test_cancelled_attempt_cannot_claim_retry(self):
        self.approve(self.a)
        original=self.store.change(self.a,1,'run')
        self.store.cancel_queued(self.a,original['version'],now())
        self.store.change(self.a,3,'reset')
        self.approve(self.a)
        again=self.store.change(self.a,5,'run')
        self.assertFalse(self.store.claim_local(self.a,original['version'],now()))
        self.assertTrue(self.store.claim_local(self.a,again['version'],now()))
        with self.assertRaises(ConflictError): self.store.cancel_queued(self.a,again['version'],now())
        self.store.recover()
        self.assertEqual([a['phase'] for a in self.store.ledger(self.a)['attempts']],['outcome_unknown','cancelled'])

    def test_copy_rechecks_exact_approval(self):
        self.act(self.a,'worker',worker='manual_claude')
        self.approve(self.a)
        receipt=self.store.ledger(self.a)['handoffs'][0]
        self.assertEqual(self.store.approved_prompt(self.a,receipt['id']),json.loads(receipt['snapshot'])['execution']['prompt'])
        self.act(self.a,'prompt',prompt='Another request')
        with self.assertRaises(ConflictError): self.store.approved_prompt(self.a,receipt['id'])

    def test_failed_migration_rolls_back(self):
        from unittest.mock import patch
        from aihub.revisions import DDL,migrate
        with self.store.connection() as db:
            db.execute('PRAGMA user_version=3')
        with patch('aihub.revisions.DDL',('CREATE TABLE rollback_marker(id INTEGER)',DDL[0])):
            with self.assertRaises(sqlite3.OperationalError),self.store.connection() as db:
                migrate(db)
        with self.store.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],3)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='rollback_marker'").fetchone())

    def test_future_and_corrupt_database_rejected(self):
        from contextlib import closing
        with closing(sqlite3.connect(self.path)) as db: db.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError): Store(self.path)
        bad=self.path.with_name('corrupt.db')
        bad.write_bytes(b'invalid database')
        with self.assertRaises(sqlite3.DatabaseError): Store(bad)
