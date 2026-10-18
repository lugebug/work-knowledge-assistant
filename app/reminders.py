"""Shared reminder CLI for Codex and the local scheduled check."""
import argparse
import json
from pathlib import Path
import sys

from store import Store, StoreError
from work_reminders import WorkReminders


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description='工作提醒：核对计划、发送及用户调整')
    p.add_argument('--db')
    sub = p.add_subparsers(dest='command', required=True)
    q = sub.add_parser('status'); q.add_argument('--output'); q.add_argument('--summary', action='store_true')
    q = sub.add_parser('packet'); q.add_argument('--id', action='append', dest='ids'); q.add_argument('--output', required=True)
    q = sub.add_parser('plan'); q.add_argument('id'); q.add_argument('--input', required=True)
    q = sub.add_parser('configure'); q.add_argument('--input', required=True)
    q = sub.add_parser('control'); q.add_argument('id'); q.add_argument('action', choices=['pause','resume','snooze']); q.add_argument('--until', default='')
    q = sub.add_parser('dispatch'); q.add_argument('--dry-run', action='store_true')
    q = sub.add_parser('cloud-connect'); q.add_argument('--input', required=True)
    sub.add_parser('cloud-sync')
    sub.add_parser('cloud-status')
    q = sub.add_parser('cloud-receipt'); q.add_argument('id'); q.add_argument('--input', required=True)
    args = p.parse_args(); service = WorkReminders(Store(args.db)); service.initialize()
    if args.command.startswith('cloud-'):
        from cloud_reminders import CloudReminders
        cloud = CloudReminders(service.store); cloud.initialize()
        if args.command == 'cloud-connect':
            result = cloud.connect(json.loads(Path(args.input).read_text(encoding='utf-8-sig')))
        elif args.command == 'cloud-receipt':
            result = cloud.record_receipt(args.id, json.loads(Path(args.input).read_text(encoding='utf-8-sig')))
        elif args.command == 'cloud-sync':
            result = cloud.sync()
        else:
            result = cloud.status()
    elif args.command == 'packet':
        result = service.packet(args.ids)
    elif args.command == 'plan':
        result = service.save_plan(args.id, json.loads(Path(args.input).read_text(encoding='utf-8-sig')))
    elif args.command == 'configure':
        result = service.configure(json.loads(Path(args.input).read_text(encoding='utf-8-sig')))
    elif args.command == 'control':
        result = service.control(args.id, args.action, args.until)
    elif args.command == 'dispatch':
        result = service.dispatch(dry_run=args.dry_run)
    else:
        result = service.status()
        with service.store.connect() as c:
            has_cloud = c.execute("SELECT 1 FROM wk_meta WHERE key='work_reminders_cloud_v39'").fetchone()
        if has_cloud:
            from cloud_reminders import CloudReminders
            result['cloud'] = CloudReminders(service.store).status()
            result['delivery_mode'] = 'feishu_cloud'
        result['plans_needing_review'] = service.packet()['pending']
        runtime = service.store.path.parent / 'workbench-runtime' / 'reminders.json'
        try:
            result['worker'] = json.loads(runtime.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            result['worker'] = {'state': 'not_observed'}
        if args.summary:
            result.pop('entries')
    if getattr(args, 'output', None):
        with Path(args.output).open('x', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        result = {'output': str(Path(args.output).resolve()), 'pending': result.get('pending'),
                  'items': len(result.get('items', result.get('entries', [])))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get('failed_batches') or result.get('delivery_attention'):
        sys.exit(2)


if __name__ == '__main__':
    try:
        main()
    except (StoreError, OSError, ValueError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False)); sys.exit(1)
