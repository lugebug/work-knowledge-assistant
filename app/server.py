"""Loopback-only web workbench. Python standard library; no model/API key."""
import argparse
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from store import Store, StoreError
from raw_store import RawStore
from raw_collectors import collect_run
from people import People
from people_review import PeopleReview
from judgment_cards import JudgmentCards
from work_model import Work, Accounts
from work_focus import WorkFocus
from work_analysis import WorkAnalysis
from work_proposals import WorkProposals
from work_lifecycle import WorkLifecycle

STATIC = Path(__file__).resolve().parent / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkKnowledge/1.0"

    def log_message(self, fmt, *args):
        # Avoid logging queries or user-supplied content.
        print(f"{self.log_date_time_string()} {self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else ''}", flush=True)

    def _headers(self, status, content_type, length, download=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if download:
            self.send_header("Content-Disposition", 'attachment; filename="work-knowledge-export.json"')
        self.end_headers()

    def _json(self, data, status=200, download=False):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(raw), download)
        self.wfile.write(raw)

    def _host(self):
        port = self.server.server_port
        if self.headers.get("Host") not in [f"127.0.0.1:{port}", f"localhost:{port}"]:
            raise StoreError("仅允许本机工作台访问。", 403)

    def do_GET(self):
        try:
            self._host()
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            pick = lambda name: query.get(name, [""])[0]
            if path == "/api/health":
                return self._json({"ok": True, "application": "work-knowledge", "pid": os.getpid()})
            if path == '/api/version':
                return self._json({'change_token':self.server.store.change_token(),'instance':os.getpid()})
            if path == "/api/session":
                return self._json({"token": self.server.token})
            if path == "/api/data":
                return self._json(self.server.store.snapshot())
            if path == "/api/detail":
                return self._json(self.server.store.detail(pick("kind"), pick("id")))
            if path == "/api/document":
                return self._json(self.server.store.document(pick("id")))
            if path == "/api/history":
                return self._json(self.server.store.history())
            if path == '/api/raw/status':
                return self._json(self.server.raw.overview())
            if path == '/api/raw/changes':
                return self._json(self.server.raw.changes())
            if path == '/api/raw/version':
                return self._json(self.server.raw.version(pick('id')))
            if path == '/api/people/source':
                return self._json(self.server.people.source(pick('id')))
            if path == '/api/people/reviews':
                return self._json(self.server.reviews.queue())
            if path == '/api/judgments':
                return self._json(self.server.judgments.queue())
            if path == "/api/export":
                return self._json(self.server.store.export(), download=True)
            if path == '/judgments.js':
                raw = (STATIC / 'judgments.js').read_bytes()
                self._headers(200, 'text/javascript; charset=utf-8', len(raw))
                self.wfile.write(raw)
                return
            assets = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
                      "/boot.js": ("boot.js", "text/javascript"), "/work.js": ("work.js", "text/javascript"), "/dimensions.js": ("dimensions.js", "text/javascript"), "/reviews.js": ("reviews.js", "text/javascript"), "/review_fields.js": ("review_fields.js", "text/javascript"), "/review_workspace.js": ("review_workspace.js", "text/javascript"), "/people.js": ("people.js", "text/javascript"), "/raw.js": ("raw.js", "text/javascript"), "/projects.js": ("projects.js", "text/javascript"), "/relationships.js": ("relationships.js", "text/javascript"), "/app.js": ("app.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}
            assets.update({'/work_analysis.js': ('work_analysis.js', 'text/javascript'), '/relevance.js': ('relevance.js', 'text/javascript'), '/live.js': ('live.js', 'text/javascript'), '/work_focus.js': ('work_focus.js', 'text/javascript'), '/work_focus.css': ('work_focus.css', 'text/css')})
            if path not in assets:
                raise StoreError("页面不存在。", 404)
            name, mime = assets[path]
            raw = (STATIC / name).read_bytes()
            self._headers(200, mime + "; charset=utf-8", len(raw))
            self.wfile.write(raw)
        except StoreError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "读取失败，请检查本地服务日志后重试。"}, 500)

    def do_POST(self):
        try:
            self._host()
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
            if origin and origin not in allowed:
                raise StoreError("不接受其他网页提交的修改。", 403)
            if not secrets.compare_digest(self.headers.get("X-Work-Knowledge-Token", ""), self.server.token):
                raise StoreError("页面会话已失效，请刷新后重试。", 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise StoreError("请求格式不正确。", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1000000:
                raise StoreError("请求内容过大或为空。", 413)
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise StoreError("请求格式不正确。")
            path = urlsplit(self.path).path
            if path == "/api/create":
                result = self.server.store.create(data.get("kind"), data.get("values"), reason=data.get("reason", ""))
            elif path == "/api/update":
                result = self.server.store.update(data.get("kind"), data.get("id"), data.get("values"), data.get("revision"), reason=data.get("reason", ""))
            elif path == "/api/undo":
                result = self.server.store.undo(data.get("change_id"))
            elif path == '/api/work/append':
                result = Work(self.server.store).append(data.get('id'), data.get('revision'), data.get('values'))
            elif path == '/api/work/focus':
                result = WorkFocus(self.server.store).save(data)
            elif path == '/api/work/evidence-review':
                result = WorkLifecycle(self.server.store).review(data)
            elif path == '/api/work/next-decision':
                result = WorkAnalysis(self.server.store).decide(data.get('id'), data.get('revision'), data.get('answer'))
            elif path == '/api/work/proposal-decision':
                result = WorkProposals(self.server.store).decide(data.get('id'), data.get('revision'), data.get('answer'))
            elif path == '/api/people/account':
                result = Accounts(self.server.store).save(data.get('person_id'), data.get('revision'), data.get('account_id'), data.get('values'))
            elif path == '/api/judgments/decide':
                result = self.server.judgments.decide(data.get('id'), data.get('version'), data.get('answer'))
            elif path == '/api/raw/update':
                run=self.server.raw.begin(data.get('source_id'),data.get('retry_of'))
                threading.Thread(target=collect_run,args=(self.server.raw,run),daemon=True).start()
                result={'run_id':run['id'],'state':'running'}
            elif path == '/api/raw/source-state':
                result=self.server.raw.decide_source(data.get('source_id'),data.get('state'),data.get('revision'))
            elif path == '/api/people/review-relation':
                result=self.server.people.review_relation(data.get('id'),data.get('revision'),data.get('state'),data.get('values'))
            elif path == '/api/people/reviews':
                result=self.server.reviews.decide(data.get('version'),data.get('items'))
            elif path == '/api/reviews/complete':
                result=self.server.reviews.complete(data.get('version'),data.get('items'))
            elif path == '/api/people/create-relation':
                result=self.server.people.create_relation(data.get('values'))
            elif path == '/api/people/review-account':
                result=self.server.people.review_account(data.get('id'),data.get('revision'),data.get('person_id'),data.get('decision'))
            else:
                raise StoreError("操作不存在。", 404)
            self._json(result)
        except StoreError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._json({"error": "请求格式不正确。"}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._json({"error": "保存失败，未确认完成。请刷新检查或查看本地日志。"}, 500)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--runtime")
    args = p.parse_args()
    store = Store(args.db)
    store.initialize()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.store, server.token = store, secrets.token_urlsafe(32)
    server.raw=RawStore(store)
    server.raw.initialize()
    server.raw.recover_interrupted()
    server.people=People(store)
    server.people.initialize()
    server.reviews=PeopleReview(store)
    server.reviews.initialize()
    server.judgments=JudgmentCards(store)
    server.judgments.initialize()
    info = {"url": f"http://127.0.0.1:{server.server_port}", "pid": os.getpid(), "database": str(store.path)}
    if args.runtime:
        target = Path(args.runtime)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False), flush=True)
    from reminder_worker import start_worker
    reminder_stop, reminder_thread = start_worker(store)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reminder_stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
