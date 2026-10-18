"""Read source freshness separately from the count of already-ingested changes."""
import json


def in_connection(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
        return {'available':False,'platforms':[],'attention':0}
    rows=c.execute('''SELECT s.id,s.name,s.platform,s.config,y.success_at,y.covered_until,
        r.state run_state,r.started_at,r.error,r.result
        FROM raw_sources s LEFT JOIN raw_sync y ON y.source_id=s.id
        LEFT JOIN raw_runs r ON r.id=(SELECT z.id FROM raw_runs z WHERE z.source_id=s.id
          ORDER BY z.started_at DESC,z.rowid DESC LIMIT 1)
        WHERE s.state='approved' ORDER BY s.platform,s.name''').fetchall()
    groups={}
    attention=[]
    for row in rows:
        if json.loads(row['config']).get('manual_only'):
            continue
        group=groups.setdefault(row['platform'],dict(platform=row['platform'],sources=0,failed=0,
            running=0,never_read=0,never_read_other=0,local_missing=0,oldest_coverage='',newest_coverage='',latest_success=''))
        group['sources']+=1
        failed=row['run_state'] in ('failed','interrupted')
        missing=json.loads(row['result'] or '{}').get('receipt',{}).get('local_table_found') is False
        group['failed']+=int(failed)
        group['running']+=int(row['run_state']=='running')
        group['never_read']+=int(not row['success_at'])
        group['never_read_other']+=int(not row['success_at'] and not missing)
        group['local_missing']+=int(missing)
        if row['covered_until']:
            group['oldest_coverage']=min(filter(None,[group['oldest_coverage'],row['covered_until']]))
            group['newest_coverage']=max(group['newest_coverage'],row['covered_until'])
        if row['success_at']:
            group['latest_success']=max(group['latest_success'],row['success_at'])
        if failed or missing or not row['success_at']:
            attention.append(dict(id=row['id'],name=row['name'],platform=row['platform'],
                reason=row['error'] or ('本机缺少会话记录' if missing else '尚未成功读取'),
                covered_until=row['covered_until'],last_attempt=row['started_at'],state=row['run_state']))
    return {'available':True,'platforms':list(groups.values()),'attention':len(attention),
            'items':attention,'description':'待整理只计算已取得的资料；来源读取失败或覆盖时间较早时，最新进展可能尚未入库。'}
