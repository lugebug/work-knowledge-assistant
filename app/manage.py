"""Codex/local CLI shares the exact workbench maintenance logic."""
import argparse
import json
from pathlib import Path
import sys
from store import Store, StoreError

sys.stdout.reconfigure(encoding="utf-8")
p = argparse.ArgumentParser(description="工作知识库查询和维护（仅修改独立知识库）")
p.add_argument("--db")
sub = p.add_subparsers(dest="command", required=True)
sub.add_parser("overview")
q = sub.add_parser("list"); q.add_argument("kind"); q.add_argument("--search", default="")
q = sub.add_parser("detail"); q.add_argument("kind"); q.add_argument("id")
for action in ["create", "update"]:
    q = sub.add_parser(action); q.add_argument("kind")
    if action == "update":
        q.add_argument("id"); q.add_argument("--revision", type=int, required=True)
    q.add_argument("--input", required=True, help="UTF-8 JSON file; object of editable values")
    q.add_argument("--reason", default="Codex按用户要求维护")
q = sub.add_parser("undo"); q.add_argument("change_id", type=int)
sub.add_parser("history")
sub.add_parser('project-review-status')
sub.add_parser('review-status')
sub.add_parser('judgment-reconcile')
sub.add_parser('follow-through-status')
q = sub.add_parser('follow-through-packet'); q.add_argument('--limit',type=int,default=20); q.add_argument('--targets',help='JSON数组，限定kind/id'); q.add_argument('--output',required=True)
q = sub.add_parser('follow-through-review'); q.add_argument('--input',required=True)
q = sub.add_parser('work-focus'); q.add_argument('--input', help='省略时读取；保存需 revision 和 followed_person_ids'); q.add_argument('--reason', default='用户调整工作关注人')
q = sub.add_parser("export"); q.add_argument("--output", required=True)
sub.add_parser('sources')
q = sub.add_parser('review-queue'); q.add_argument('--output'); q.add_argument('--summary', action='store_true')
q = sub.add_parser('review-complete'); q.add_argument('--input', required=True, help='用户明确答案，含version及items；事实整理仍使用enrich')
q = sub.add_parser('judgment-propose'); q.add_argument('--input', required=True)
q = sub.add_parser('judgment-decide'); q.add_argument('id'); q.add_argument('--version', required=True); q.add_argument('--input', required=True)
q = sub.add_parser('work-append'); q.add_argument('id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--input', required=True)
q = sub.add_parser('work-create'); q.add_argument('--input', required=True)
q = sub.add_parser('work-evidence-review'); q.add_argument('--input', required=True)
q = sub.add_parser('work-evidence-result'); q.add_argument('version_id')
q = sub.add_parser('work-goal-packet'); q.add_argument('id'); q.add_argument('--output')
q = sub.add_parser('work-goal-save'); q.add_argument('--input', required=True)
sub.add_parser('work-priority-status')
q = sub.add_parser('work-priority-packet'); q.add_argument('--limit', type=int, default=20); q.add_argument('--id', action='append', dest='ids'); q.add_argument('--output')
q = sub.add_parser('work-priority-review'); q.add_argument('--input', required=True)
sub.add_parser('project-priority-status')
q = sub.add_parser('project-priority-packet'); q.add_argument('--limit', type=int, default=20); q.add_argument('--id', action='append', dest='ids'); q.add_argument('--output')
q = sub.add_parser('project-priority-review'); q.add_argument('--input', required=True)
q = sub.add_parser('work-task-link'); q.add_argument('--input', required=True)
q = sub.add_parser('work-task-link-question'); q.add_argument('--input', required=True)
q = sub.add_parser('work-analysis-packet'); q.add_argument('--limit', type=int, default=20); q.add_argument('--id', action='append', dest='ids'); q.add_argument('--output')
q = sub.add_parser('work-analyze'); q.add_argument('id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--input', required=True)
q = sub.add_parser('work-next-decide'); q.add_argument('id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--input', required=True)
q = sub.add_parser('work-candidates'); q.add_argument('--input', required=True)
q = sub.add_parser('work-propose'); q.add_argument('--input', required=True)
q = sub.add_parser('work-proposal-decide'); q.add_argument('id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--input', required=True)
q = sub.add_parser('account-save'); q.add_argument('person_id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--account-id'); q.add_argument('--input', required=True)
q = sub.add_parser('account-enrich'); q.add_argument('person_id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--account-id', required=True); q.add_argument('--input', required=True)
q = sub.add_parser('source-search'); q.add_argument('platform'); q.add_argument('conversation_id'); q.add_argument('--since', required=True); q.add_argument('--until', required=True); q.add_argument('--keyword', required=True); q.add_argument('--limit', type=int, default=30)
q = sub.add_parser('source-document'); q.add_argument('id')
q = sub.add_parser('enrich'); q.add_argument('kind'); q.add_argument('id'); q.add_argument('--revision', type=int, required=True); q.add_argument('--input',required=True); q.add_argument('--reason',required=True)
args = p.parse_args()
try:
    store = Store(args.db); store.initialize()
    if args.command in ('follow-through-status','follow-through-packet','follow-through-review'):
        from follow_through import FollowThrough
        service=FollowThrough(store)
        if args.command=='follow-through-status':result=service.status()
        elif args.command=='follow-through-review':result=service.review(json.loads(Path(args.input).read_text('utf-8-sig')))
        else:
            targets=json.loads(Path(args.targets).read_text('utf-8-sig')) if args.targets else None
            result=service.packet(args.limit,targets)
            with Path(args.output).open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
            result={'pending':result['pending'],'items':len(result['items']),'output':str(Path(args.output).resolve())}
    elif args.command == 'project-review-status':
        from project_review import status
        result = status(store)
    elif args.command == 'review-status':
        from review_inventory import status
        result = status(store)
    elif args.command == 'judgment-reconcile':
        from judgment_cards import JudgmentCards
        result = JudgmentCards(store).reconcile()
    elif args.command == 'work-focus':
        from work_focus import WorkFocus
        focus = WorkFocus(store)
        result = focus.save(json.loads(Path(args.input).read_text('utf-8-sig')), 'Codex按用户要求维护', args.reason) if args.input else focus.get()
    elif args.command == 'judgment-propose':
        from judgment_cards import JudgmentCards
        result=JudgmentCards(store).propose(json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command == 'judgment-decide':
        from judgment_cards import JudgmentCards
        result=JudgmentCards(store).decide(args.id,args.version,json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command == 'work-analysis-packet':
        from work_analysis import WorkAnalysis
        result=WorkAnalysis(store).packet(args.limit,args.ids)
        if args.output:
            with Path(args.output).open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
            result={'output':str(Path(args.output).resolve()),'pending':result['pending'],'items':len(result['items'])}
    elif args.command in ('work-analyze','work-next-decide'):
        from work_analysis import WorkAnalysis
        document=json.loads(Path(args.input).read_text('utf-8-sig'))
        result=(WorkAnalysis(store).save(args.id,args.revision,document) if args.command=='work-analyze'
                else WorkAnalysis(store).decide(args.id,args.revision,document,'Codex按用户决定'))
    elif args.command in ('work-candidates','work-propose','work-proposal-decide'):
        from work_proposals import WorkProposals
        service=WorkProposals(store)
        document=json.loads(Path(args.input).read_text('utf-8-sig'))
        result=(service.candidates(document) if args.command=='work-candidates' else service.propose(document)
                if args.command=='work-propose' else service.decide(args.id,args.revision,document,'Codex按用户决定'))
    elif args.command in ('work-task-link', 'work-task-link-question'):
        from task_work_links import TaskWorkLinks
        service=TaskWorkLinks(store)
        document=json.loads(Path(args.input).read_text('utf-8-sig'))
        result=service.bind(document) if args.command=='work-task-link' else service.propose(document)
    elif args.command in ('work-goal-packet', 'work-goal-save'):
        from work_completion import WorkCompletion
        service = WorkCompletion(store)
        result = (service.packet(args.id) if args.command == 'work-goal-packet' else
                  service.save_goal(json.loads(Path(args.input).read_text('utf-8-sig'))))
        if getattr(args, 'output', None):
            with Path(args.output).open('x', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            result = {'output': str(Path(args.output).resolve())}
    elif args.command in ('project-priority-status', 'project-priority-packet', 'project-priority-review'):
        from project_priority import ProjectPriority
        service = ProjectPriority(store)
        result = (service.status() if args.command == 'project-priority-status' else
                  service.review(json.loads(Path(args.input).read_text('utf-8-sig'))) if args.command == 'project-priority-review' else
                  service.packet(args.limit, args.ids))
        if getattr(args, 'output', None):
            with Path(args.output).open('x', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            result = dict(output=args.output, pending=result['pending'], items=len(result['items']))
    elif args.command in ('work-priority-status', 'work-priority-packet', 'work-priority-review'):
        from work_priority import WorkPriority
        service = WorkPriority(store)
        result = (service.status() if args.command == 'work-priority-status' else
                  service.review(json.loads(Path(args.input).read_text('utf-8-sig'))) if args.command == 'work-priority-review' else
                  service.packet(args.limit, args.ids))
        if getattr(args, 'output', None):
            with Path(args.output).open('x', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            result = {'output': str(Path(args.output).resolve()), 'pending': result['pending'], 'items': len(result['items'])}
    elif args.command == 'work-evidence-review':
        from work_lifecycle import WorkLifecycle
        result=WorkLifecycle(store).review(json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command == 'work-evidence-result':
        from work_lifecycle import WorkLifecycle
        with store.connect() as conn:
            result=WorkLifecycle(store).get(conn,args.version_id)
    elif args.command == 'work-append':
        from work_model import Work
        result=Work(store).append(args.id,args.revision,json.loads(Path(args.input).read_text('utf-8-sig')), 'Codex')
    elif args.command == 'work-create':
        from work_model import Work
        result=Work(store).create_from_source(json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command == 'account-enrich':
        from work_model import Accounts
        result=Accounts(store).enrich(args.person_id,args.revision,args.account_id,json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command == 'account-save':
        from work_model import Accounts
        result=Accounts(store).save(args.person_id,args.revision,args.account_id,json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command in ('review-queue','review-complete'):
        from people_review import PeopleReview
        reviews=PeopleReview(store);reviews.initialize()
        if args.command=='review-complete':
            request=json.loads(Path(args.input).read_text('utf-8-sig'))
            result=reviews.complete(request.get('version'),request.get('items'))
        else:
            result=reviews.queue()
            if args.summary:result=result.get('summary') or dict(version=result['version'],completeness=result['completeness'])
            if args.output:
                Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
                result={'exported':str(Path(args.output).resolve())}
    elif args.command == 'sources':
        from work_sources import scope
        result = scope()
    elif args.command == 'source-search':
        from work_sources import search_messages
        with store.connect() as conn:
            result = search_messages(conn, args.platform, args.conversation_id, args.since, args.until, args.keyword, args.limit)
    elif args.command == 'source-document': result = store.document(args.id)
    elif args.command == 'enrich':
        from enrichment import apply
        result = apply(store,args.kind,args.id,args.revision,json.loads(Path(args.input).read_text('utf-8-sig')),args.reason)
    elif args.command == "overview":
        result = store.snapshot(); result["records"] = {k: len(v) for k, v in result["records"].items()}
    elif args.command == "list":
        data = store.snapshot()["records"]
        if args.kind not in data: raise StoreError("不支持的数据类别。")
        result = [r for r in data[args.kind] if args.search.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
    elif args.command == "detail": result = store.detail(args.kind, args.id)
    elif args.command in ["create", "update"]:
        data = json.loads(Path(args.input).read_text("utf-8-sig"))
        if args.command == "create": result = store.create(args.kind, data, "Codex", args.reason)
        else: result = store.update(args.kind, args.id, data, args.revision, "Codex", args.reason)
    elif args.command == "undo": result = store.undo(args.change_id, "Codex")
    elif args.command == "history": result = store.history()
    else:
        dest = Path(args.output)
        with dest.open("x", encoding="utf-8") as f: json.dump(store.export(), f, ensure_ascii=False, indent=2)
        result = {"exported": str(dest.resolve())}
    print(json.dumps(result, ensure_ascii=False, indent=2))
except (StoreError, ValueError, OSError) as exc:
    print(json.dumps({"error": str(exc)}, ensure_ascii=False)); sys.exit(1)
