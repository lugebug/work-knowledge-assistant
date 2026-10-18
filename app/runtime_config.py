"""Personal installation configuration. No deployment identity is shipped."""
from pathlib import Path
import datetime as dt
import json
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(os.environ.get('WORK_KNOWLEDGE_CONFIG') or ROOT / 'config.local.json')
SUPPORTED_WECHAT = ('4.1.15.55',)
CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def settings():
    if not CONFIG.is_file():
        return {}
    data = json.loads(CONFIG.read_text('utf-8-sig'))
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('个人安装配置格式不受支持，请运行 doctor。')
    return data


def default_data_dir():
    return Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share') / 'WorkKnowledgeAssistant' / 'default'


def database_path():
    configured = settings().get('data_dir')
    return Path(configured or default_data_dir()).resolve() / 'knowledge.db'


def history_since():
    return settings().get('history_since') or dt.datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def owner_account_ids():
    config = settings()
    return tuple(value for value in (config.get('wechat', {}).get('account_id'), config.get('feishu', {}).get('open_id')) if value)


def lark_command():
    config = settings()
    node = config.get('node_executable') or shutil.which('node')
    script = config.get('lark_script') or str(ROOT / '.tools' / 'node_modules' / '@larksuite' / 'cli' / 'scripts' / 'run.js')
    if not node or not Path(node).is_file() or not Path(script).is_file():
        raise ValueError('未找到本安装的飞书 CLI，请运行 install.ps1。')
    return [str(node), str(script)]


def lark_status():
    result = subprocess.run([*lark_command(), 'auth', 'status'], capture_output=True, encoding='utf-8',
                            errors='replace', timeout=25, creationflags=CREATE_NO_WINDOW)
    try:
        data = json.loads(result.stdout)
        user = data.get('identities', {}).get('user', {})
        return {'available': result.returncode == 0 and user.get('available') is True,
                'status': user.get('status', 'unknown'), 'app_id': data.get('appId', ''),
                'open_id': user.get('openId', ''), 'name': user.get('userName', '')}
    except (ValueError, AttributeError):
        return {'available': False, 'status': 'not_configured'}


def require_feishu(discovery=False):
    # Imported lazily to avoid the Store -> runtime_config import cycle.
    from store import StoreError
    config = settings().get('feishu', {})
    if not config.get('enabled') or not config.get('confirmed_at'):
        raise StoreError('尚未确认本人的飞书账号和读取范围；未读取飞书资料。', 403)
    if discovery and not config.get('all_visible_work'):
        raise StoreError('未授权自动纳入全部可见飞书资料；只可读取已单独批准的来源。', 403)
    try:
        current = lark_status()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise StoreError('无法核对飞书登录身份，请运行 doctor；未读取资料。', 503) from None
    if not current.get('available'):
        raise StoreError('飞书登录尚未就绪，请由本人完成登录授权。', 503)
    if current.get('open_id') != config.get('open_id') or current.get('app_id') != config.get('app_id'):
        raise StoreError('飞书登录账号或应用已变化，请重新确认本人的接入；未读取资料。', 403)
    return current


def executable_version(path):
    if os.name != 'nt':
        raise ValueError('首版微信接入只支持 Windows。')
    env = dict(os.environ, WK_EXECUTABLE_TO_CHECK=str(Path(path).resolve()))
    command = "(Get-Item -LiteralPath $env:WK_EXECUTABLE_TO_CHECK).VersionInfo.FileVersion"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, encoding='utf-8', errors='replace', timeout=15,
                            env=env, creationflags=CREATE_NO_WINDOW)
    if result.returncode:
        raise ValueError('无法读取所选微信程序的版本。')
    return result.stdout.strip()


def require_wechat():
    from store import StoreError
    config = settings().get('wechat', {})
    if not config.get('enabled') or not config.get('confirmed_at'):
        raise StoreError('尚未确认本人的微信接入；未读取微信文件或进程。', 403)
    database = Path(config.get('database_dir') or '__not_configured__')
    if not config.get('account_id') or not (database / 'message').is_dir():
        raise StoreError('微信账号或消息目录未配置，请运行 doctor。', 503)
    try:
        version = executable_version(config['executable'])
    except (KeyError, ValueError, OSError, subprocess.SubprocessError):
        raise StoreError('微信程序无法核对；未读取微信文件或进程。', 503) from None
    if version not in SUPPORTED_WECHAT:
        raise StoreError('微信版本 ' + version + ' 尚未验证；当前支持 ' + ', '.join(SUPPORTED_WECHAT) + '。未尝试读取，需适配后再接入。', 503)
    return config
