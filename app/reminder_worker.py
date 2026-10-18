"""Small dispatcher hosted by the existing workbench, without a model call."""
import json
import os
from pathlib import Path
import threading

from store import now
from work_reminders import WorkReminders
from cloud_reminders import CloudReminders


def start_worker(store):
    service = WorkReminders(store)
    service.initialize()
    cloud = CloudReminders(store)
    cloud.initialize()
    stop = threading.Event()
    runtime = store.path.parent / 'workbench-runtime' / 'reminders.json'
    runtime.parent.mkdir(exist_ok=True)

    def run():
        while not stop.is_set():
            try:
                with store.connect() as c:
                    cloud_settings = cloud.settings(c)
                result = cloud.sync(min_interval=120) if cloud_settings else service.dispatch()
                state = 'needs_attention' if result.get('delivery_attention') else 'retry_pending' if result.get('failed_batches') else 'stopped' if result.get('stopped') else 'running'
                report = dict(state=state, checked_at=now(), pid=os.getpid(), database=str(store.path), result=result)
            except Exception as exc:
                report = dict(state='error', checked_at=now(), pid=os.getpid(), database=str(store.path),
                              error=f'提醒检查未完成：{type(exc).__name__}；需核对服务日志与提醒状态。')
            # Atomic replacement prevents status readers seeing a half-written file.
            temp = runtime.with_name(f'reminders-{os.getpid()}.tmp')
            try:
                temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                temp.replace(runtime)
            except OSError:
                pass
            stop.wait(30)

    worker = threading.Thread(target=run, name='work-reminders', daemon=True)
    worker.start()
    return stop, worker
