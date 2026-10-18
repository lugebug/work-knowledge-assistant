"""Read a minute's text artifacts when no linked Docx is available."""
import hashlib
import json
from pathlib import Path
import re

from store import StoreError


def collect_minute(run, read, artifact_dir):
    source = run['source']; token = source['identity']
    if source['state'] != 'approved' or not re.fullmatch(r'[A-Za-z0-9]{10,100}', token):
        raise StoreError('妙记文字来源未批准或身份无效。')
    if artifact_dir is None:
        raise StoreError('妙记文字采集目录未配置。')
    root = (Path(artifact_dir) / token).resolve()
    root.mkdir(parents=True, exist_ok=True)
    args = ['minutes', '+detail', '--minute-tokens', token, '--summary', '--todo',
            '--transcript', '--output-dir', '.', '--overwrite']
    data = read(args, root)
    minutes = data.get('minutes', [])
    if len(minutes) != 1 or minutes[0].get('minute_token') != token:
        raise StoreError('妙记返回身份不一致；未保存为完整来源。')
    minute = minutes[0]; artifacts = minute.get('artifacts') or {}
    if not isinstance(artifacts.get('transcript_file'), str) or not artifacts['transcript_file']:
        raise StoreError('妙记文字转写未成功读取；保留失败等待重试。')
    transcript = Path(artifacts['transcript_file'])
    if not transcript.is_absolute():
        transcript = root / transcript
    transcript = transcript.resolve()
    folder = transcript.parent
    named_artifact = folder.name == 'artifact-' + token or (folder.name.startswith('artifact-') and folder.name.endswith('-' + token))
    if folder.parent != root or not named_artifact or transcript.name != 'transcript.txt' or not transcript.is_relative_to(root):
        raise StoreError('妙记转写文件位置无效；未读取范围外文件。')
    if not transcript.is_file():
        raise StoreError('妙记未返回可读取的转写文件；保留失败等待重试。')
    text = transcript.read_text('utf-8-sig')
    if not text.strip():
        raise StoreError('妙记转写正文为空；未登记完整。')
    title = minute.get('title') or source['name']
    body = title + '\n\n[AI摘要；须与转写核对]\n' + str(artifacts.get('summary') or '')
    body += '\n\n[会议待办；不代表执行完成]\n' + json.dumps(artifacts.get('todos') or [], ensure_ascii=False, sort_keys=True)
    body += '\n\n[逐字转写；说话人标签不直接对应实名]\n' + text
    body = re.sub(r'(?im)((?:密码|口令|password|api[_ -]?key|access[_ -]?token|secret)\s*[:：=]\s*)\S+', r'\1[已隐藏]', body)
    body = re.sub(r'((?:密码|口令)\s*(?:为|是)\s*)[A-Za-z0-9_!@#$%&*+./=-]{4,}', r'\1[已隐藏]', body)
    record = {'source_id': source['id'], 'object_id': 'feishu:' + token, 'kind': 'document',
        'title': title, 'body': body, 'revision': hashlib.sha256(body.encode('utf-8')).hexdigest(),
        'url': source['config']['url'], 'event_at': '', 'sender': '', 'content_mode': 'minute_text'}
    return [record], {'complete': True, 'pages': 1, 'revision': record['revision'],
        'coverage': '妙记可读摘要、待办及完整文字转写；未抓取音视频'}
