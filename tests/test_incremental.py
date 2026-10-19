"""Synthetic V44 regression cases; never accesses a real account or database."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_portable
import bootstrap
from store import StoreError, json_text, now
from raw_store import RawStore
from incremental import Incremental, digest
from work_analysis import WorkAnalysis
from work_completion import WorkCompletion
from work_lifecycle import WorkLifecycle
from work_priority import WorkPriority
from project_priority import ProjectPriority
from follow_through import FollowThrough
from work_reminders import WorkReminders, moment
from periodic_update import Periodic
from model import FIELDS


class IncrementalRegression(unittest.TestCase):
    tearDown = test_portable.PortableInstallation.tearDown
    initialize = test_portable.PortableInstallation.initialize

    def setUp(self):
        test_portable.PortableInstallation.setUp(self)
        installed = self.initialize()
        self.me = installed['person_id']
        self.service = Incremental(self.store)
        self.sid = RawStore(self.store).register_work_chat('feishu', 'oc_test_project', '测试工作来源',
            {'note': '合成测试工作资料', 'evidence': ['测试本人确认']}, '2020-01-01T00:00:00+08:00')['id']
        self.vid = self.add_source('fixture-initial', '请整理方案并提交交付说明。当前尚未提交。只做测试。', reviewed=True)
        self.refs = [dict(type='raw', id=self.vid, quote='请整理方案并提交交付说明')]
        self.pid = self.store.create('projects', {'name': '测试项目', 'owner_id': self.me,
                                                  'my_role': '测试牵头'})['id']
        self.ids = []
        for title in ('测试方案甲', '测试方案乙'):
            rid = self.store.create('tasks', {'title': title, 'owner_id': self.me,
                                             'project_id': self.pid, 'state': '进行中'})['id']
            self.ids.append(rid)
            with self.store.connect() as c:
                r = self.store._record(c, 'tasks', rid)
                token = WorkAnalysis(self.store).context_token(c, r)
            WorkCompletion(self.store).save_goal(dict(id=rid, revision=0, work_revision=r['revision'],
                context_token=token, goal=dict(summary='整理并提交本项方案交付说明', criteria=[
                    dict(key='submit', requirement='提交本项方案交付说明', sources=self.refs)]),
                reason='合成案例中的原始任务要求明确，按原目标判断完成。', change_sources=[]))
            with self.store.connect() as c:
                r = self.store._record(c, 'tasks', rid)
                token = WorkAnalysis(self.store).context_token(c, r)
            next_step = '建议整理方案交付说明，完成后提交。'
            check = dict(goal_revision=1, criteria=[dict(key='submit', status='unmet',
                reason='测试资料明确说明方案尚未提交。', sources=self.refs)], outcome='unmet',
                follow_up='本案例只有原任务，尚无独立后续。', timing=[
                    dict(field=field, work_id=rid, value=value, kind='suggested', sources=[],
                         reason='这是内部建议，不是业务约定截止。')
                    for field, value in [('analysis_next_step', next_step), ('review_on', '2020-01-02')]])
            WorkAnalysis(self.store).save(rid, r['revision'], dict(current='资料已收齐，方案尚未提交。',
                next_step=next_step, next_key='prepare-and-submit', timing_kind='suggested',
                timing_basis='按测试原文继续推进，无明确约定时间。', review_on='2020-01-02',
                rationale='原目标未完成，下一步继续整理交付说明。', sources=self.refs,
                context_token=token, completion_check=check))
        self.rid = self.ids[0]
        # Seed reviewed ledgers as persisted fixtures. The tested changes use shared APIs.
        self.seed_reviews()
        for row in WorkReminders(self.store).packet()['items']:
            WorkReminders(self.store).save_plan(row['id'], dict(revision=0,
                work_revision=row['work_revision'], context_token=row['context_token'],
                nodes=[dict(key='follow-up', action='核对测试方案进展', kind='suggested',
                            when='2030-01-01', basis='合成测试提醒，不会发出。')], notes=''))
        self.reset_checkpoint()

    def add_source(self, identity, body, reviewed=False, object_id='fixture-object'):
        vid = 'raw:' + identity
        payload = dict(object_id=object_id, body=body, title='测试工作来源', sender_id='ou_test_self',
                       sent_at=now(), message_type='text', sender_type='user')
        with self.store.connect() as c:
            c.execute('INSERT INTO raw_versions VALUES(?,?,?,?,?,?,?,?)',
                (vid, self.sid, object_id, 'message', json_text(payload), digest(payload), now(), ''))
            c.execute('INSERT OR REPLACE INTO raw_heads VALUES(?,?,?)', (self.sid, object_id, vid))
            if reviewed:
                c.execute('INSERT INTO wk_review_receipts VALUES(?,?,?,?,?,?)',
                    (vid, 'no_business_change', '合成案例的历史已整理版本', '{}', now(), 'fixture'))
        return vid

    def mutate_payload(self, kind, rid, changes):
        with self.store.connect() as c:
            value = json.loads(c.execute('SELECT payload FROM wk_records WHERE kind=? AND id=?', (kind, rid)).fetchone()[0])
            changes(value)
            c.execute('UPDATE wk_records SET payload=? WHERE kind=? AND id=?', (json_text(value), kind, rid))

    def seed_reviews(self):
        with self.store.connect() as c:
            for rid in self.ids:
                r = self.store._record(c, 'tasks', rid)
                a = deepcopy(r['_work_analysis'])
                a['context_token'] = WorkAnalysis(self.store).context_token(c, r, self.store._record(c, 'projects', self.pid))
                payload = json.loads(c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()[0])
                payload['_work_analysis'] = a
                c.execute("UPDATE wk_records SET payload=? WHERE kind='tasks' AND id=?", (json_text(payload), rid))
            for service, table, kind in ((WorkPriority(self.store), 'wk_work_priority_reviews', 'tasks'),
                                         (ProjectPriority(self.store), 'wk_project_priority_reviews', 'projects')):
                env = service.context(c)
                for rid, r in env['records'][kind].items():
                    a = dict(outcome='automatic', priority='普通', reason='正常测试推进，没有关键阻塞。',
                             sources=self.refs, review_on='2020-01-02', judgment_ids=[])
                    c.execute('INSERT OR REPLACE INTO ' + table + ' VALUES(?,?,?,?,?,?)',
                              (rid, 1, service.token(env, r, a), json_text(a), 'fixture', now()))
            follow = FollowThrough(self.store)
            for kind, ids in (('tasks', self.ids), ('projects', [self.pid])):
                for rid in ids:
                    payload = dict(actions=[], uncertainties=[], facets={})
                    c.execute('INSERT OR REPLACE INTO wk_followthrough_reviews VALUES(?,?,?,?,?,?)',
                              (kind, rid, '', json_text(payload), 1, now()))
            env = follow.context(c)
            for kind, rid in env['reviews']:
                c.execute('UPDATE wk_followthrough_reviews SET input_token=? WHERE kind=? AND record_id=?',
                          (follow.token(env, kind, rid), kind, rid))
            for row in c.execute('SELECT work_id FROM wk_reminder_plans').fetchall():
                r = self.store._record(c, 'tasks', row[0])
                p = self.store._record(c, 'projects', self.pid)
                c.execute('UPDATE wk_reminder_plans SET context_token=? WHERE work_id=?',
                          (WorkReminders(self.store)._context(c, r, p), row[0]))

    def reset_checkpoint(self):
        with self.store.connect() as c:
            c.execute("INSERT OR REPLACE INTO wk_meta VALUES('incremental_checkpoint',?)", (json_text(self.service.snapshot(c)),))

    def tokens(self):
        with self.store.connect() as c:
            r = self.store._record(c, 'tasks', self.rid)
            p = self.store._record(c, 'projects', self.pid)
            follow = FollowThrough(self.store)
            return (WorkAnalysis(self.store).context_token(c, r, p), follow.token(follow.context(c), 'tasks', self.rid),
                    WorkReminders(self.store)._context(c, r, p))

    def update_record(self, kind, rid, values):
        r = self.store.detail(kind, rid)['record']
        return self.store.update(kind, rid, values, r['revision'])

    def test_no_changes_has_no_catalog_or_expired_review_queue(self):
        packet = self.service.prepare()
        self.assertEqual([], packet['items'])
        self.assertEqual([], packet['work_index'])
        self.assertEqual([], packet['project_index'])
        self.assertEqual({}, packet['entity_index'])
        self.assertLess(len(json_text(packet)), 3000)
        self.assertTrue(all(not rows for rows in self.service.pending().values()))
        with self.store.connect() as c:
            for rid in self.ids:
                r = self.store._record(c, 'tasks', rid)
                self.assertFalse(WorkAnalysis(self.store).view(c, r, today='2040-01-01')['needs_review'])
                self.assertFalse(WorkPriority(self.store).view(c, r, today='2040-01-01')['needs_review'])
            self.assertFalse(ProjectPriority(self.store).view(c, self.store._record(c, 'projects', self.pid), today='2040-01-01')['needs_review'])

    def test_project_progress_does_not_reopen_all_work(self):
        before = self.tokens()
        self.update_record('projects', self.pid, {'progress': '项目一般说明补充，工作要求没有改变。'})
        self.assertEqual(before, self.tokens())
        pending = self.service.pending()
        self.assertEqual([], pending['analysis'])
        self.assertEqual([], pending['work_priority'])
        self.assertEqual([], pending['project_priority'])
        self.assertEqual([('projects', self.pid)], [(r['kind'], r['id']) for r in pending['follow_through']])

    def test_work_fact_change_only_reopens_affected_work_and_plan(self):
        before = self.tokens()
        self.update_record('tasks', self.rid, {'progress': '方案初稿已经整理，仍未提交。'})
        self.assertTrue(all(a != b for a, b in zip(before, self.tokens())))
        pending = self.service.pending()
        self.assertEqual([self.rid], pending['analysis'])
        self.assertEqual([self.rid], pending['reminder_plans'])
        self.assertEqual([], pending['work_priority'])

    def test_timestamp_and_unchanged_receipt_do_not_reopen_reviews(self):
        before = self.tokens()
        self.mutate_payload('tasks', self.rid, lambda r: r['_work_analysis'].update(at='2030-01-01', actor='再次核对', input_hash='metadata'))
        with self.store.connect() as c:
            c.execute('INSERT INTO wk_work_evidence_reviews VALUES(?,?,?,?,?)',
                (self.vid, 'fixture', '{}', json_text({'outcomes': [dict(work_id=self.rid, action='unchanged', change_ids=[])]}), now()))
        self.assertEqual(before, self.tokens())
        self.assertTrue(all(not rows for rows in self.service.pending().values()))

    def test_explicit_priority_impact_is_targeted_and_idempotent(self):
        doc = dict(kind='tasks', id=self.rid, reason='原文明确当前关键阻塞解除，需要复核本项排序。', sources=[self.vid])
        self.assertTrue(self.service.priority_impact(doc)['registered'])
        self.assertFalse(self.service.priority_impact(doc)['registered'])
        self.assertEqual([self.rid], self.service.pending()['work_priority'])
        self.assertEqual([], self.service.pending()['project_priority'])
        with self.store.connect() as c:
            self.assertEqual(1, c.execute('SELECT count(*) FROM wk_incremental_priority_impact_events').fetchone()[0])
        self.update_record('tasks', self.rid, {'priority': '低'})
        self.assertEqual([], self.service.pending()['work_priority'])

    def test_paused_source_cannot_authorize_new_priority(self):
        RawStore(self.store).decide_source(self.sid, 'paused', 1)
        with self.assertRaises(StoreError):
            self.service.priority_impact(dict(kind='tasks', id=self.rid,
                reason='来源已经暂停，不能依据它自动修改排序。', sources=[self.vid]))

    def test_source_revision_pauses_reminder_until_actual_review_receipt(self):
        vid = self.add_source('fixture-revision', '补充标点说明，不改变原工作事实、目标和安排。')
        self.assertTrue(all(not rows for rows in self.service.pending().values()))
        rm = WorkReminders(self.store)
        with self.store.connect() as c:
            r = self.store._record(c, 'tasks', self.rid)
            p = self.store._record(c, 'projects', self.pid)
            self.assertIn('未整理修订', rm._reason(c, r, p, self.me))
        packet = Periodic(self.store).packet()
        self.assertEqual([vid], [r['id'] for r in packet['items']])
        self.assertNotIn('work_review', packet['items'][0])
        check = deepcopy(r['_work_analysis']['completion_check']); check['timing'] = []
        WorkLifecycle(self.store).review(dict(version_id=vid, summary='核对本次修订，原工作事实、目标和安排均不变。',
            sources=[dict(type='raw', id=vid, quote='不改变原工作事实、目标和安排')],
            related=[dict(work_id=self.rid, revision=r['revision'], action='unchanged',
                reason='原文仅补充说明，没有新的业务事实。', changes={}, completion_check=check)]))
        Periodic(self.store).finish(dict(packet_id=packet['packet_id'], results=[dict(version_id=vid,
            disposition='no_business_change', reason='原工作事实、完成条件和安排保持不变。', change_ids=[])]))
        with self.store.connect() as c:
            self.assertEqual('', rm._reason(c, r, p, self.me))
        self.assertTrue(all(not rows for rows in self.service.pending().values()))

    def test_changed_person_and_answer_are_routed_without_full_review(self):
        self.update_record('people', self.me, {'role': '测试本人确认的新岗位'})
        with self.store.connect() as c:
            c.execute('INSERT INTO wk_judgment_cards VALUES(?,?,?,?,?,?,?,?)',
                ('fixture-decision', json_text(dict(question='测试问题', options=[], sources=[])),
                 'confirmed', 2, '本人已经确认，请保留原话。', '', now(), now()))
        packet = self.service.prepare()
        self.assertEqual([self.me], packet['changed_entities']['people'])
        self.assertEqual([], packet['changed_entities']['tasks'])
        self.assertEqual(['fixture-decision'], [r['id'] for r in packet['changed_decisions']])
        self.assertEqual([], self.service.pending()['analysis'])

    def test_checkpoint_requires_each_changed_answer_and_rejects_concurrent_edit(self):
        packet = self.service.prepare()
        self.update_record('people', self.me, {'role': '当前本人岗位'})
        seal = self.service.settle(packet['run_id'])
        doc = dict(run_id=packet['run_id'], snapshot_token=seal['snapshot_token'], acknowledgments=[])
        with patch.object(Periodic, 'review_check', return_value={'ready': True, 'checks': {}}):
            with self.assertRaises(StoreError): self.service.checkpoint(doc)
            doc['acknowledgments'] = [dict(kind='people', id=self.me, reason='已保存并核对本人修改的岗位，不重查其他人员。')]
            self.assertTrue(self.service.checkpoint(doc)['finished'])
            self.assertTrue(self.service.checkpoint(doc)['unchanged'])
            packet = self.service.prepare(); seal = self.service.settle(packet['run_id'])
            self.update_record('people', self.me, {'role': '期间再次修改岗位'})
            with self.assertRaises(StoreError):
                self.service.checkpoint(dict(run_id=packet['run_id'], snapshot_token=seal['snapshot_token'], acknowledgments=[]))
        self.assertEqual([self.me], self.service.prepare()['changed_entities']['people'])

    def test_new_unreviewed_source_cannot_be_checkpointed(self):
        packet = self.service.prepare()
        vid = self.add_source('fixture-late', '期间新到的资料不能跳过业务判断。', object_id='new-message')
        seal = self.service.settle(packet['run_id'])
        with patch.object(Periodic, 'review_check', return_value={'ready': True, 'checks': {}}):
            with self.assertRaises(StoreError):
                self.service.checkpoint(dict(run_id=packet['run_id'], snapshot_token=seal['snapshot_token'], acknowledgments=[]))
        self.assertEqual([vid], [r['id'] for r in self.service.prepare()['items']])

    def test_upgrade_backs_up_preserves_business_and_carries_real_pending(self):
        with self.store.connect() as c:
            c.execute("DELETE FROM wk_meta WHERE key IN ('incremental_version','incremental_source_delta')")
        self.seed_reviews()
        self.update_record('tasks', self.ids[1], {'progress': '迁移前已有新进展，仍须实际核对。'})
        with self.store.connect() as c:
            before = {k: self.store._records(c, k) for k in ('tasks', 'projects', 'people')}
            audit = c.execute('SELECT count(*) FROM wk_changes').fetchone()[0]
            nodes = WorkReminders(self.store)._entries(c, moment('2025-01-01T00:00:00+08:00'))
        profile_before = self.config.read_bytes()
        result = bootstrap.initialize()
        backup = Path(result['incremental']['backup'])
        self.assertTrue(backup.is_file())
        self.assertEqual(profile_before, self.config.read_bytes())
        with closing(sqlite3.connect(backup)) as c:
            self.assertEqual('ok', c.execute('PRAGMA quick_check').fetchone()[0])
            self.assertIsNone(c.execute("SELECT value FROM wk_meta WHERE key='incremental_version'").fetchone())
        with self.store.connect() as c:
            for kind, records in before.items():
                after = self.store._records(c, kind)
                for rid, r in records.items():
                    for field in (*FIELDS[kind], 'revision', '_provenance'):
                        self.assertEqual(r.get(field), after[rid].get(field), (kind, rid, field))
            self.assertEqual(audit, c.execute('SELECT count(*) FROM wk_changes').fetchone()[0])
            after_nodes = WorkReminders(self.store)._entries(c, moment('2025-01-01T00:00:00+08:00'))
            keys = ('id', 'work_id', 'key', 'when', 'notify_at', 'state', 'blocked_reason')
            self.assertEqual([{k: n[k] for k in keys} for n in nodes], [{k: n[k] for k in keys} for n in after_nodes])
        self.assertEqual([self.ids[1]], self.service.pending()['analysis'])
        count = len(list(backup.parent.iterdir()))
        self.assertTrue(bootstrap.initialize()['incremental']['already_enabled'])
        self.assertEqual(count, len(list(backup.parent.iterdir())))


if __name__ == '__main__':
    unittest.main()
