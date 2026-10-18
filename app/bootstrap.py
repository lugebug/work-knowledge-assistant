"""Idempotent clean-install bootstrap and account binding for the Codex guide."""
from pathlib import Path
from contextlib import closing
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid

from runtime_config import (ROOT, CONFIG, settings, default_data_dir, database_path,
                            lark_status, executable_version, SUPPORTED_WECHAT, CREATE_NO_WINDOW)


def save_settings(data):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG.with_name(CONFIG.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, CONFIG)


def stamp():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')


def attach_owner_account(platform, account_id, name, source_scope=''):
    """Use the same source-backed account transaction as ordinary work review."""
    from store import Store, json_text
    from people import People, stable_id
    from work_model import Accounts
    store = Store(database_path())
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        pid = json.loads(conn.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()[0])
        body = json_text({'platform': platform, 'account_id': account_id, 'name': name,
                          'source_scope': source_scope, 'confirmed_by': '本机用户确认当前登录身份'})
        sid = People.capture(conn, '本人平台账号确认', body, '首次接入与身份核对')
        source = dict(id=stable_id('owner-account', platform, account_id, source_scope), person_id=pid,
                      platform=platform, account_id=account_id, name=name, source_scope=source_scope,
                      _dossier_sources=[{'type': 'profile', 'id': sid, 'quote': body}])
        return Accounts(store).seed(conn, source)


def normalize_since(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    if parsed > dt.datetime.now(parsed.tzinfo):
        raise ValueError('历史起点不能在未来。')
    return parsed.isoformat(timespec='seconds')


def initialize(name='我', since=None, data_dir=None):
    existing = settings()
    if existing and existing.get('installation_state', 'ready') == 'ready':
        db = database_path()
        if not db.is_file():
            raise ValueError('已有安装配置但数据库不存在；请恢复备份或使用新的配置路径，不能覆盖旧安装。')
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute("SELECT value FROM wk_meta WHERE key='installation_id'").fetchone()
            if not row or json.loads(row[0]) != existing['installation_id']:
                raise ValueError('个人配置和数据库不属于同一次安装；未更改资料。')
        # Reinstall only updates dependencies and code, never profile, dates or user records.
        return {'ok': True, 'reused': True, 'database': str(db), 'profile_name': existing['display_name']}
    if existing and existing.get('installation_state') != 'initializing':
        raise ValueError('未知的安装阶段，请运行 doctor。')
    data = Path(existing['data_dir'] if existing else data_dir or default_data_dir()).expanduser().resolve()
    if data == ROOT or ROOT in data.parents:
        raise ValueError('个人数据库必须位于代码目录外，避免更新或发布时带上资料。')
    if not name.strip() or len(name.strip()) > 100:
        raise ValueError('请填写1至100字的本人称呼。')
    db = data / 'knowledge.db'
    if not existing and db.exists():
        raise ValueError('目标目录已有数据库；请选择原 config.local.json 或使用新的个人目录。')
    if not existing and data.exists() and any(data.iterdir()):
        raise ValueError('首次安装请选择空的个人数据目录；不会接管未知目录。')
    data.mkdir(parents=True, exist_ok=True)
    profile = existing or {'schema_version': 1, 'installation_id': uuid.uuid4().hex,
               'display_name': name.strip(), 'data_dir': str(data), 'timezone': 'Asia/Shanghai',
               'history_since': normalize_since(since or dt.date.today().replace(day=1).isoformat()),
               'collection_until': None, 'created_at': stamp(),
               'installation_state': 'initializing',
               'feishu': {'enabled': False, 'all_visible_work': False}, 'wechat': {'enabled': False}}
    if not existing:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG.open('x', encoding='utf-8') as stream:
            json.dump(profile, stream, ensure_ascii=False, indent=2)
    name = profile['display_name']
    # Exclusive creation prevents a concurrent installer from clobbering a database.
    if not db.exists():
        with db.open('xb'):
            pass
    with closing(sqlite3.connect(db)) as conn, conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'installation_bootstrap' not in tables:
            raise ValueError('未完成安装的目录中存在未知数据库；未改动。')
        conn.execute('CREATE TABLE IF NOT EXISTS installation_bootstrap(installation_id TEXT PRIMARY KEY)')
        identity = conn.execute('SELECT installation_id FROM installation_bootstrap').fetchone()
        if identity and identity[0] != profile['installation_id']:
            raise ValueError('未完成安装的数据库身份不匹配。')
        conn.execute('INSERT OR IGNORE INTO installation_bootstrap VALUES(?)', (profile['installation_id'],))
        conn.executescript((ROOT / 'app' / 'schema.sql').read_text('utf-8').replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS '))
    from store import Store, json_text
    from people import People
    from people_review import PeopleReview
    from raw_store import RawStore
    from periodic_update import Periodic
    from work_lifecycle import WorkLifecycle
    from work_completion import WorkCompletion
    from work_priority import WorkPriority
    from project_priority import ProjectPriority
    from follow_through import FollowThrough
    from cloud_reminders import CloudReminders
    store = Store(db)
    store.initialize()
    People(store).initialize()
    PeopleReview(store).initialize()
    RawStore(store).initialize()
    Periodic(store).initialize()
    CloudReminders(store).initialize()
    people = store.snapshot()['records']['people']
    if people:
        if len(people) != 1 or people[0].get('role') != '本人' or people[0].get('name') != name:
            raise ValueError('未完成安装中出现未知人员数据，未覆盖。')
        pid = people[0]['id']
    else:
        person = store.create('people', {'name': name, 'state': '内部同事', 'role': '本人'},
                              actor='首次安装', reason='本机用户设置自己的个人工作库')
        pid = person.get('id') or person.get('record', {}).get('id')
    if not pid:
        raise ValueError('本人档案创建结果无法核对。')
    with store.connect() as conn:
        for key, value in {'current_user_id': pid, 'work_model_version': 28,
                           'work_analysis_version': 34, 'installation_id': profile['installation_id']}.items():
            conn.execute('INSERT INTO wk_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json_text(value)))
    for service in (WorkLifecycle, WorkCompletion, WorkPriority, ProjectPriority, FollowThrough):
        service(store).enable()
    readback = store.snapshot()
    if len(readback['records']['people']) != 1 or readback['records']['tasks'] or readback['records']['projects']:
        raise ValueError('空库初始化读回不一致。')
    with closing(sqlite3.connect(db)) as conn:
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('数据库检查失败。')
    profile['installation_state'] = 'ready'
    save_settings(profile)
    return {'ok': True, 'reused': False, 'database': str(db), 'profile_name': name.strip(),
            'person_id': pid, 'work_count': 0, 'project_count': 0, 'approved_sources': 0,
            'next': '由本人选择历史起点、完成账号登录并确认工作来源后，再进行首次采集。'}


def bind_feishu(confirm_owner=False, all_visible_work=False):
    config = settings()
    if not config or not confirm_owner:
        raise ValueError('先安装，并由本人确认当前飞书登录身份。')
    current = lark_status()
    if not current.get('available') or not current.get('app_id') or not current.get('open_id'):
        raise ValueError('飞书尚未登录就绪，请按接入指引完成本人登录。')
    old = config.get('feishu', {})
    if old.get('open_id') and (old['open_id'], old.get('app_id')) != (current['open_id'], current['app_id']):
        raise ValueError('本安装已绑定其他飞书身份；切换账号请建立独立个人库，不能混用旧来源。')
    config['feishu'] = dict(enabled=True, app_id=current['app_id'], open_id=current['open_id'],
                            display_name=current['name'], all_visible_work=bool(all_visible_work), confirmed_at=stamp())
    attach_owner_account('feishu', current['open_id'], current['name'] or config['display_name'], current['app_id'])
    save_settings(config)
    return {'ok': True, 'account_name': current['name'], 'discovery_enabled': bool(all_visible_work),
            'messages_read': 0, 'next': '可以采集本人已批准的来源。'}


def bind_wechat(database_dir, executable, account, confirm_owner=False):
    config = settings()
    if not config or not confirm_owner:
        raise ValueError('先安装，并由本人确认微信账号及只读接入。')
    database = Path(database_dir).expanduser().resolve()
    program = Path(executable).expanduser().resolve()
    if not (database / 'message').is_dir() or not (database / 'contact' / 'contact.db').is_file():
        raise ValueError('请选择该账号的 db_storage 目录。')
    if not program.is_file() or program.name.lower() != 'weixin.exe':
        raise ValueError('请选择正在使用的 Weixin.exe。')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{3,100}', account):
        raise ValueError('请填写核实过的本人微信账号身份，不能使用昵称代替。')
    if not database.parent.name.startswith(account + '_') and database.parent.name != account:
        raise ValueError('账号与所选微信数据目录不相符，请核对。')
    version = executable_version(program)
    if version not in SUPPORTED_WECHAT:
        raise ValueError('该微信版本尚未验证：' + version + '；本次未读取进程或聊天。')
    old = config.get('wechat', {})
    if old.get('account_id') and old['account_id'] != account:
        raise ValueError('本安装已绑定其他微信账号；请使用独立个人库。')
    config['wechat'] = dict(enabled=True, database_dir=str(database), executable=str(program),
                            account_id=account, version=version, confirmed_at=stamp())
    attach_owner_account('wechat', account, config['display_name'])
    save_settings(config)
    return {'ok': True, 'version': version, 'messages_read': 0,
            'next': '先由本人确认工作联系人和群，再读取指定来源的文字。'}


def configure_tools(node=None, lark_script=None):
    config = settings()
    if not config:
        raise ValueError('请先运行 init。')
    if node:
        config['node_executable'] = str(Path(node).resolve())
    if lark_script:
        config['lark_script'] = str(Path(lark_script).resolve())
    save_settings(config)
    return {'ok': True, 'tools_configured': True}


def record_schedule(automation_ids, description, until=None):
    """Record real, active Codex jobs without creating or editing Codex files."""
    import tomllib
    from store import Store, json_text
    from raw_store import schedule_status
    config = settings()
    if config.get('installation_state') != 'ready':
        raise ValueError('请先完成安装。')
    if not description.strip() or not automation_ids:
        raise ValueError('请提供实际任务身份和运行安排说明。')
    automation_root = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'automations'
    ids = list(dict.fromkeys(automation_ids))
    for identity in ids:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', identity):
            raise ValueError('任务身份格式不正确。')
        path = automation_root / identity / 'automation.toml'
        if not path.is_file() or path.is_symlink() or automation_root.resolve() not in path.resolve().parents:
            raise ValueError('未找到实际创建的本机定期任务：' + identity)
        entry = tomllib.loads(path.read_text('utf-8-sig'))
        if entry.get('status') != 'ACTIVE' or entry.get('id', identity) != identity:
            raise ValueError('任务尚未启用或身份不一致：' + identity)
    end = None
    if until:
        parsed = dt.datetime.fromisoformat(until)
        if parsed.tzinfo is None:
            raise ValueError('截止时间须包含时区；长期运行则省略 --until。')
        end = parsed.isoformat(timespec='seconds')
    data = {'automation_ids': ids, 'description': description.strip(), 'trial_end': end}
    with Store(database_path()).connect() as conn:
        conn.execute('INSERT INTO wk_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     ('periodic-v21', json_text(data)))
    config['collection_until'] = end
    save_settings(config)
    with Store(database_path()).connect() as conn:
        return {'ok': True, 'schedule': schedule_status(conn), 'codex_files_modified': False}


def discover_local(roots=None):
    # Filesystem paths and application version only. No database bodies or process memory.
    candidates = []
    if os.name == 'nt':
        command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | Select-Object -ExpandProperty ExecutablePath -Unique | ConvertTo-Json -Compress"
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, encoding='utf-8', errors='replace', timeout=20, creationflags=CREATE_NO_WINDOW)
        try:
            paths = json.loads(result.stdout or '[]')
            for path in paths if isinstance(paths, list) else [paths]:
                if path:
                    version = executable_version(path)
                    candidates.append({'executable': path, 'version': version, 'supported': version in SUPPORTED_WECHAT})
        except ValueError:
            pass
    search = [Path(p).expanduser() for p in roots or []]
    search += [Path.home() / 'Documents' / 'xwechat_files', Path.home() / 'xwechat_files']
    # Only conventional top-level locations, never a recursive disk/contact scan.
    if os.name == 'nt':
        search += [Path(letter + ':/xwechat_files') for letter in 'CDEFG']
    accounts = []
    for root in dict.fromkeys(search):
        if root.is_dir():
            for folder in root.glob('*/db_storage'):
                if (folder / 'message').is_dir():
                    accounts.append({'database_dir': str(folder.resolve()), 'folder_name': folder.parent.name})
    return {'applications': candidates, 'account_directories': accounts,
            'database_contents_read': False, 'process_memory_read': False,
            'next': '请选择并确认属于本人的账号；多个账号不得自动混用。'}


def doctor():
    config = settings()
    checks = [{'item': 'Python', 'ok': sys.version_info >= (3, 11), 'detail': sys.version.split()[0]},
              {'item': '个人配置', 'ok': bool(config)},
              {'item': '个人数据库', 'ok': database_path().is_file()},
              {'item': '微信版本适配', 'ok': False, 'detail': '支持版本：' + ', '.join(SUPPORTED_WECHAT)}]
    if database_path().is_file():
        with closing(sqlite3.connect(database_path().as_uri() + '?mode=ro', uri=True)) as conn:
            check = conn.execute('PRAGMA quick_check').fetchone()[0]
        checks.append({'item': '数据库完整性', 'ok': check == 'ok'})
    import importlib.util
    missing = [module for module in ('apsw', 'zstandard', 'cryptography') if importlib.util.find_spec(module) is None]
    checks.append({'item': '微信读取依赖', 'ok': not missing, 'missing': missing})
    wechat = config.get('wechat', {})
    if wechat.get('enabled'):
        try:
            from runtime_config import require_wechat
            require_wechat()
            checks[3]['ok'] = True
        except Exception as error:
            checks[3]['detail'] = str(error)
    try:
        current = lark_status()
        expected = config.get('feishu', {})
        bound = bool(expected.get('enabled') and current.get('available') and
                     current.get('open_id') == expected.get('open_id') and current.get('app_id') == expected.get('app_id'))
        checks.append({'item': '飞书本人身份', 'ok': bound, 'login_status': current.get('status'),
                       'all_visible_work': bool(expected.get('all_visible_work'))})
    except (ValueError, OSError, subprocess.SubprocessError):
        checks.append({'item': '飞书本人身份', 'ok': False, 'detail': '尚未安装或接入；可稍后完成。'})
    return {'installed': bool(config) and config.get('installation_state', 'ready') == 'ready' and database_path().is_file(), 'checks': checks,
            'collection_ready': any(x['ok'] for x in checks if x['item'] == '飞书本人身份') or
                                bool(checks[3]['ok'] and not missing),
            'content_read': False, 'reminders_enabled_by_install': False}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init'); init.add_argument('--name', default='我'); init.add_argument('--since'); init.add_argument('--data-dir')
    sub.add_parser('doctor')
    sub.add_parser('paths')
    sub.add_parser('feishu-status')
    tools = sub.add_parser('configure-tools'); tools.add_argument('--node'); tools.add_argument('--lark-script')
    schedule = sub.add_parser('record-schedule'); schedule.add_argument('--automation-id', action='append', required=True); schedule.add_argument('--description', required=True); schedule.add_argument('--until')
    feishu = sub.add_parser('bind-feishu'); feishu.add_argument('--confirm-owner', action='store_true'); feishu.add_argument('--all-visible-work', action='store_true')
    wechat = sub.add_parser('bind-wechat'); wechat.add_argument('--database-dir', required=True); wechat.add_argument('--executable', required=True); wechat.add_argument('--account', required=True); wechat.add_argument('--confirm-owner', action='store_true')
    scan = sub.add_parser('discover-local'); scan.add_argument('--root', action='append')
    args = parser.parse_args()
    if args.command == 'init': result = initialize(args.name, args.since, args.data_dir)
    elif args.command == 'doctor': result = doctor()
    elif args.command == 'paths': result = {'config': str(CONFIG), 'database': str(database_path()), 'root': str(ROOT)}
    elif args.command == 'feishu-status': result = lark_status()
    elif args.command == 'configure-tools': result = configure_tools(args.node, args.lark_script)
    elif args.command == 'record-schedule': result = record_schedule(args.automation_id, args.description, args.until)
    elif args.command == 'bind-feishu': result = bind_feishu(args.confirm_owner, args.all_visible_work)
    elif args.command == 'bind-wechat': result = bind_wechat(args.database_dir, args.executable, args.account, args.confirm_owner)
    elif args.command == 'discover-local': result = discover_local(args.root)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
