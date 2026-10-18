"""Meaningful clean-install and isolation tests; no personal DB or chat access."""
from pathlib import Path
from unittest import mock
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
sys.path.insert(0, str(ROOT / 'scripts'))
import runtime_config
import bootstrap
from store import Store, StoreError
from raw_store import RawStore
from periodic_update import Periodic


class PortableInstallation(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='work-assistant-test-')
        self.root = Path(self.directory.name)
        self.config = self.root / 'profile' / 'config.local.json'
        self.patch_config = mock.patch.object(runtime_config, 'CONFIG', self.config)
        self.patch_bootstrap = mock.patch.object(bootstrap, 'CONFIG', self.config)
        self.patch_config.start(); self.patch_bootstrap.start()
        self.data = self.root / 'profile' / 'data'

    def tearDown(self):
        self.patch_bootstrap.stop(); self.patch_config.stop()
        # Preflight the exact isolated tree before TemporaryDirectory removes it.
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(resolved.name.startswith('work-assistant-test-'))
        for path in [self.root, *self.root.rglob('*')]:
            self.assertFalse(path.is_symlink())
            self.assertTrue(path.resolve() == resolved or resolved in path.resolve().parents)
        self.directory.cleanup()

    def initialize(self):
        result = bootstrap.initialize('测试本人', '2020-01-01', self.data)
        self.store = Store(self.data / 'knowledge.db')
        return result

    def test_clean_install_has_no_work_or_approved_sources(self):
        self.initialize()
        snapshot = self.store.snapshot()
        self.assertEqual(len(snapshot['records']['people']), 1)
        self.assertEqual(snapshot['records']['projects'], [])
        self.assertEqual(snapshot['records']['tasks'], [])
        self.assertEqual(RawStore(self.store).overview()['sources'], [])
        self.assertTrue(Periodic(self.store).review_check()['ready'])
        self.assertFalse(runtime_config.settings()['feishu']['enabled'])
        self.assertFalse(runtime_config.settings()['wechat']['enabled'])

    def test_reinstall_preserves_personal_changes_and_dates(self):
        self.initialize()
        created = self.store.create('projects', {'name': '本人已有项目', 'notes': '人工决定必须保留'})
        db_before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        config_before = self.config.read_bytes()
        result = bootstrap.initialize('其他名字', '2022-02-02', self.root / 'wrong-location')
        self.assertTrue(result['reused'])
        self.assertEqual(config_before, self.config.read_bytes())
        self.assertEqual(db_before, hashlib.sha256(self.store.path.read_bytes()).hexdigest())
        self.assertEqual(self.store.detail('projects', created['id'])['record']['notes'], '人工决定必须保留')

    def test_interrupted_initialization_can_resume_without_duplicate_self(self):
        with mock.patch.object(RawStore, 'initialize', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaises(RuntimeError): self.initialize()
        self.assertEqual(runtime_config.settings()['installation_state'], 'initializing')
        self.initialize()
        self.assertEqual(runtime_config.settings()['installation_state'], 'ready')
        self.assertEqual(len(self.store.snapshot()['records']['people']), 1)

    def test_unknown_existing_database_is_not_overwritten(self):
        self.data.mkdir(parents=True)
        db = self.data / 'knowledge.db'; db.write_bytes(b'other owner data')
        with self.assertRaises(ValueError): self.initialize()
        self.assertEqual(db.read_bytes(), b'other owner data')
        self.assertFalse(self.config.exists())

    def test_profile_database_identity_mismatch_blocks_reinstall(self):
        self.initialize()
        profile = runtime_config.settings(); profile['installation_id'] = 'different-install'
        bootstrap.save_settings(profile)
        with self.assertRaises(ValueError): bootstrap.initialize()

    def test_data_cannot_be_created_inside_source_checkout(self):
        with self.assertRaises(ValueError): bootstrap.initialize('测试', '2020-01-01', ROOT / 'data')

    def test_unconfigured_connectors_do_not_read_any_account(self):
        self.initialize()
        with mock.patch.object(runtime_config, 'lark_status') as login, mock.patch.object(runtime_config, 'executable_version') as version:
            with self.assertRaises(StoreError): runtime_config.require_feishu()
            with self.assertRaises(StoreError): runtime_config.require_wechat()
            login.assert_not_called(); version.assert_not_called()

    def test_binding_feishu_records_only_verified_self_and_is_idempotent(self):
        self.initialize()
        status = {'available': True, 'status': 'ready', 'app_id': 'cli_demo_app', 'open_id': 'ou_demo_self', 'name': '测试本人'}
        with mock.patch.object(bootstrap, 'lark_status', return_value=status):
            with self.assertRaises(ValueError): bootstrap.bind_feishu(False, True)
            bootstrap.bind_feishu(True, False)
            bootstrap.bind_feishu(True, False)
        self.assertFalse(runtime_config.settings()['feishu']['all_visible_work'])
        person = self.store.snapshot()['records']['people'][0]
        self.assertEqual(len(person['accounts']), 1)
        self.assertEqual(person['accounts'][0]['account_id'], 'ou_demo_self')
        self.assertEqual(person['accounts'][0]['source_scope'], 'cli_demo_app')

    def test_changed_feishu_identity_stops_collection(self):
        self.initialize()
        profile = runtime_config.settings()
        profile['feishu'] = {'enabled': True, 'confirmed_at': 'confirmed', 'open_id': 'ou_demo_self', 'app_id': 'cli_demo_app', 'all_visible_work': True}
        bootstrap.save_settings(profile)
        with mock.patch.object(runtime_config, 'lark_status', return_value={'available': True, 'open_id': 'ou_other_self', 'app_id': 'cli_demo_app'}):
            with self.assertRaises(StoreError): runtime_config.require_feishu()

    def test_discovery_needs_explicit_all_work_scope(self):
        self.initialize()
        profile = runtime_config.settings()
        profile['feishu'] = {'enabled': True, 'confirmed_at': 'confirmed', 'open_id': 'ou_demo_self', 'app_id': 'cli_demo_app', 'all_visible_work': False}
        bootstrap.save_settings(profile)
        reader = mock.Mock()
        with self.assertRaises(StoreError): Periodic(self.store).discover_feishu(reader)
        reader.assert_not_called()

    def test_wechat_directory_requires_separate_directory_confirmation(self):
        self.initialize()
        import wechat_directory
        with mock.patch.object(wechat_directory, 'require_wechat') as access:
            with self.assertRaises(ValueError): wechat_directory.inventory(False)
            access.assert_not_called()

    def test_unsupported_wechat_version_cannot_bind(self):
        self.initialize()
        directory = self.root / 'wx_demo_self_local' / 'db_storage'
        (directory / 'message').mkdir(parents=True); (directory / 'contact').mkdir()
        (directory / 'contact' / 'contact.db').write_bytes(b'not opened')
        executable = self.root / 'Weixin.exe'; executable.write_bytes(b'not executed')
        with mock.patch.object(bootstrap, 'executable_version', return_value='99.0.0.0'):
            with self.assertRaises(ValueError): bootstrap.bind_wechat(directory, executable, 'wx_demo_self', True)
        self.assertFalse(runtime_config.settings()['wechat']['enabled'])

    def test_paused_and_rejected_sources_cannot_be_reopened_by_registration(self):
        self.initialize()
        raw = RawStore(self.store)
        source = raw.register_work_chat('wechat', 'demo_work_chat', '工作群', {'note': '本人确认', 'evidence': ['本人原话']}, '2020-01-01T00:00:00+08:00')
        raw.decide_source(source['id'], 'rejected', source['revision'])
        with self.assertRaises(StoreError): raw.register_work_chat('wechat', 'demo_work_chat', '工作群', {'note': '旧导入', 'evidence': ['旧资料']}, '2020-01-01T00:00:00+08:00')
        with self.assertRaises(StoreError): raw.begin(source['id'])

    def test_empty_collection_skips_unconfigured_feishu_without_contacting_it(self):
        self.initialize()
        job = Periodic(self.store)
        with mock.patch.object(job, 'discover_feishu') as discovery:
            result = job._collect()
            discovery.assert_not_called()
        self.assertEqual(result['source_count'], 0)
        self.assertEqual(result['state'], 'collected')

    def test_release_allowlist_excludes_user_configuration_and_databases(self):
        from verify_release import files
        exported = {path.relative_to(ROOT).as_posix() for path in files()}
        self.assertNotIn('config.local.json', exported)
        self.assertTrue(all(not path.endswith(('.db', '.sqlite')) for path in exported))
        self.assertFalse(any('.venv/' in path or 'node_modules/' in path for path in exported))

    def test_schedule_registration_requires_real_active_task_and_preserves_its_file(self):
        self.initialize()
        codex = self.root / 'codex'
        path = codex / 'automations' / 'demo-task' / 'automation.toml'
        path.parent.mkdir(parents=True)
        path.write_text('id = "demo-task"\nstatus = "ACTIVE"\n', encoding='utf-8')
        before = path.read_bytes()
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(codex)}):
            with self.assertRaises(ValueError): bootstrap.record_schedule(['missing'], '每天检查')
            with self.assertRaises(ValueError): bootstrap.record_schedule(['../demo-task'], '每天检查')
            result = bootstrap.record_schedule(['demo-task'], '每天检查')
            self.assertTrue(result['schedule']['active'])
            self.assertIsNone(runtime_config.settings()['collection_until'])
            self.assertEqual(path.read_bytes(), before)
            path.write_text('id = "demo-task"\nstatus = "PAUSED"\n', encoding='utf-8')
            with self.assertRaises(ValueError): bootstrap.record_schedule(['demo-task'], '每天检查')
            from raw_store import schedule_status
            with self.store.connect() as conn:
                self.assertFalse(schedule_status(conn)['active'])

    def test_project_policy_is_generic_and_available_from_this_checkout(self):
        self.initialize()
        from project_priority import ProjectPriority
        with self.store.connect() as conn:
            policies = ProjectPriority(self.store).context(conn)['priority_policies']
        self.assertEqual(set(policies), {'docs/business-context.md'})
        self.assertIn('本人已确认的方向', policies['docs/business-context.md'])

    def test_http_api_of_clean_database_starts_without_external_accounts(self):
        self.initialize()
        runtime = self.root / 'runtime.json'
        env = dict(os.environ, WORK_KNOWLEDGE_CONFIG=str(self.config), PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen([sys.executable, '-X', 'utf8', str(ROOT / 'app' / 'server.py'), '--db', str(self.store.path), '--runtime', str(runtime)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            # The server writes one readiness line after the socket is bound.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(process.stdout.readline)
                try: line = pending.result(timeout=15)
                except concurrent.futures.TimeoutError:
                    process.terminate(); raise AssertionError('server did not start')
            info = json.loads(line)
            with urllib.request.urlopen(info['url'] + '/api/data', timeout=5) as response:
                payload = json.load(response)
            self.assertEqual(len(payload['records']['people']), 1)
            self.assertEqual(payload['records']['tasks'], [])
            with urllib.request.urlopen(info['url'] + '/', timeout=5) as response:
                self.assertIn('个人工作助手', response.read().decode('utf-8'))
        finally:
            process.terminate()
            process.communicate(timeout=15)


if __name__ == '__main__':
    unittest.main(verbosity=2)
