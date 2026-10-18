"""Manual RAW maintenance CLI; no scheduler and no source-app writes."""
import argparse
import json
from pathlib import Path
import sys
from store import Store, StoreError
from raw_store import RawStore
from raw_collectors import update_one,collect_run

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser();p.add_argument('--db')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status')
    q=sub.add_parser('register-work-chats');q.add_argument('--input',required=True)
    q=sub.add_parser('register-work-documents');q.add_argument('--input',required=True)
    q=sub.add_parser('import-user-document');q.add_argument('--input',required=True)
    q=sub.add_parser('exclude-chat');q.add_argument('--input',required=True)
    q=sub.add_parser('backfill');q.add_argument('--source',required=True);q.add_argument('--since',required=True);q.add_argument('--until',required=True)
    q=sub.add_parser('sync');q.add_argument('--source',action='append');q.add_argument('--platform',choices=['feishu','wechat']);q.add_argument('--retry-failed',action='store_true')
    q=sub.add_parser('version');q.add_argument('id')
    sub.add_parser('changes')
    q=sub.add_parser('propose-wechat');q.add_argument('--input',required=True)
    q=sub.add_parser('decide-source');q.add_argument('id');q.add_argument('--state',choices=['approved','paused','rejected'],required=True);q.add_argument('--revision',type=int,required=True)
    args=p.parse_args();raw=RawStore(Store(args.db));raw.initialize();raw.recover_interrupted()
    if args.command=='status':result=raw.overview()
    elif args.command=='register-work-chats':result=[raw.register_work_chat(**entry) for entry in json.loads(Path(args.input).read_text('utf-8-sig'))]
    elif args.command=='register-work-documents':result=[raw.register_work_document(**entry) for entry in json.loads(Path(args.input).read_text('utf-8-sig'))]
    elif args.command=='import-user-document':
        from user_materials import import_document
        result=import_document(raw,json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command=='exclude-chat':result=raw.exclude_chat(**json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command=='backfill':result=collect_run(raw,raw.begin(args.source,since_at=args.since,until_at=args.until))
    elif args.command=='version':result=raw.version(args.id)
    elif args.command=='changes':result=raw.changes()
    elif args.command=='propose-wechat':result=raw.propose_wechat(**json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command=='decide-source':result=raw.decide_source(args.id,args.state,args.revision)
    else:
        if not args.source and not args.platform:p.error('sync需要明确--source或--platform')
        sources=[s for s in raw.overview()['sources'] if s['state']=='approved' and (not args.source or s['id'] in args.source) and (not args.platform or s['platform']==args.platform)]
        if args.source and set(args.source)!={s['id'] for s in sources}:raise StoreError('来源不在已批准名单内；未读取正文。',403)
        result=[]
        for s in sources:
            previous=s['latest_run'];retry=previous['id'] if args.retry_failed and previous and previous['state'] in ('failed','interrupted') else None
            if args.retry_failed and not retry:continue
            print(json.dumps({'source':s['name'],'state':'starting'},ensure_ascii=False),flush=True)
            result.append(update_one(raw,s['id'],retry))
            if result[-1].get('failure_scope')=='wechat_shared_read':
                print(json.dumps({'state':'blocked','message':'微信共用读取条件未通过，本轮停止重复尝试；其余来源完成位置保持，修复后可继续更新。'},ensure_ascii=False),flush=True)
                break
        if not result:result={'message':'没有符合条件的待更新来源。'}
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 1 if (isinstance(result,list) and any(r.get('state')=='failed' for r in result)) or (isinstance(result,dict) and result.get('state')=='failed') else 0

if __name__=='__main__':
    try:sys.exit(main())
    except (StoreError,ValueError,OSError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False));sys.exit(1)
