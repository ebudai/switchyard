#!/usr/bin/env python3
"""SYRD-487: baseline golden for the queued-notification eligibility boundary.

The 56 cases were first run against public main 08c066bf, then against the
scratch extraction. They record decisions, SQL and parameters, ledger writes,
log text, late-bound workflow and decoder, and both supersession dispositions.
The probe refuses spawns, signals, account lookups and socket connections; its
host-path recorder proves itself and then requires zero accesses.
"""

from __future__ import annotations

import ast
import builtins
import grp
import hashlib
import io
import json
import os
import pathlib
import pwd
import socket
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
GOLDEN = "99e4b2b1dc1cbda96745f5a4d603b326ba4993c37a1b1a1fc8bdb1612dc120be"
MOVED = {"_current_ticket_state", "_current_target_role", "_superseding_awaiting_role",
         "_drop_superseded_notification", "_queue_identity_key", "_announced_queue_identity",
         "_superseded_queue_notice", "_notification_is_current", "_serial_gate_stage",
         "_finish_current_blocker"}
RULES = {"TERMINAL_STATES", "NUDGE_ELIGIBLE_STATES", "SUPERSEDABLE_REMINDER_KINDS",
         "DIRECTOR_BOUND_KINDS", "SUPERSEDED_BY_AWAITING_ROLE", "LEGACY_SERIAL_STAGE",
         "LEGACY_NON_SERIAL_ROLES", "SUPERSEDED_QUEUE_NOTICE", "UNNAMED_RESERVATION"}


def assert_ownership(nl: object) -> None:
    from_module = nl.NotificationEligibility
    assert from_module.__module__.endswith("notification_eligibility")
    assert not any(hasattr(nl.TicketBoardNotifyListener, name) for name in MOVED)
    assert all(hasattr(from_module, name) for name in MOVED)
    for name in RULES:
        assert getattr(nl, name) == getattr(sys.modules[from_module.__module__], name)
    source = (ROOT / "scripts/ticket_board/notification_eligibility.py").read_text()
    assert "from . import notify_listener" not in source
    assert "import notify_listener" not in source
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "NotificationEligibility")
    assert MOVED == {n.name for n in cls.body if isinstance(n, ast.FunctionDef) and n.name.startswith("_") and n.name not in {"__init__"}}


def run_probe(nl: object) -> int:
    def refused(*a, **k): raise AssertionError('execution guard refusal')
    subprocess.run = subprocess.Popen = refused
    os.kill = os.system = refused
    pwd.getpwnam = pwd.getpwuid = grp.getgrnam = grp.getgrgid = refused
    socket.socket.connect = socket.socket.connect_ex = refused
    hits=[]
    def guarded(real, name):
     def f(path,*args,**kwargs):
      value=os.fsdecode(path) if isinstance(path,(str,bytes,os.PathLike)) else ''
      if value.startswith(('/usr/local/lib/switchyard','/etc','/var','/opt','/proc')):
       hits.append([name,value]); raise FileNotFoundError(2,'execution guard refused',value)
      return real(path,*args,**kwargs)
     return f
    for target,name in [(os,'stat'),(os,'lstat'),(os,'access'),(os,'open'),(os,'listdir'),(os,'scandir'),(builtins,'open'),(io,'open')]:
     setattr(target,name,guarded(getattr(target,name),name))
    try: pathlib.Path('/proc/self/stat').read_text()
    except FileNotFoundError: pass
    assert hits; hits.clear()
    class Result:
     def __init__(self,row): self.row=row
     def fetchone(self): return self.row
    class Conn:
     def __init__(self, **rows): self.rows=rows; self.calls=[]
     def execute(self, query, params=None):
      q=' '.join(query.split()); self.calls.append([q, list(params) if params is not None else None])
      if 'FROM ticket_board.tickets t' in q: key='ticket'
      elif 'ticket_notification_queue q' in q: key='wait'
      elif 'SELECT queued_for_assignee' in q: key='queue'
      elif 'SELECT EXISTS (' in q: key='exists'
      elif 'finish_current_stage_blocker' in q: key='blocker'
      else: key='write'
      return Result(self.rows.get(key))
    class Logger:
     def __init__(self): self.logs=[]
     def info(self,msg,*args): self.logs.append(['info',msg%args])
     def warning(self,msg,*args): self.logs.append(['warning',msg%args])
     def debug(self,msg,*args): self.logs.append(['debug',msg%args])
     def error(self,msg,*args): self.logs.append(['error',msg%args])
    log=Logger()
    l=nl.TicketBoardNotifyListener(conninfo='', sender=lambda *a:None, activity_gate=lambda _:False, target_exists=lambda _:True, logger=log)
    owner=getattr(l,'eligibility',l)
    out=[]
    def case(name,method,*args,workflow=None,**rows):
     l.workflow=workflow
     conn=Conn(**rows)
     value=getattr(owner,method)(conn,*args) if method in {'_current_ticket_state','_superseding_awaiting_role','_superseded_queue_notice','_notification_is_current','_finish_current_blocker'} else getattr(owner,method)(*args)
     out.append([name,value,conn.calls,log.logs[:]])
     log.logs.clear()
    T=('in_progress','main',False,False,False)
    case('state tuple','_current_ticket_state','SYRD-1',ticket=T)
    case('state dict','_current_ticket_state','SYRD-1',ticket={'state':b'audit','assignee':b'audit','manually_controlled':0,'parked':0,'has_unresolved_blockers':1})
    case('state gone','_current_ticket_state','SYRD-1')
    for name,kind,state,assignee in [('triage legacy','triage','analysis','unassigned'),('handoff implementer','transition','in_progress','main'),('handoff audit','transition','audit','audit'),('director bound','escalation','audit','audit'),('unknown','transition','done','main')]:
     out.append([name,owner._current_target_role(kind,state,assignee)])
    wait=('director','2026-01-01T00:02:00+00:00','2026-01-01T00:01:00+00:00',True,True)
    for name,kind,row in [('wait after queue','idle_reminder',wait),('wait inactive','nudge',(*wait[:3],False,True)),('wait before queue','escalation',(*wait[:3],True,False)),('wait dict','nudge',dict(zip(('awaiting_role','awaiting_since_at','queued_at','wait_is_active','established_after_queueing'),wait))),('other kind','transition',wait),('wait missing','nudge',None)]:
     case(name,'_superseding_awaiting_role',9,'SYRD-1',kind,wait=row)
    for payload in ['broken{','[]','{}','{"queued_for":"main","reserved_by":"active work"}']:
     out.append(['announced '+payload,owner._announced_queue_identity(payload)])
    for args in [(' Main ','active work'),('main',''),('OPS','SYRD-3')]: out.append(['queue key',args,owner._queue_identity_key(*args)])
    queue_payload='{"queued_for":"main","reserved_by":"active work"}'
    for name,row in [('queue same',('main','')),('queue changed',('ops','SYRD-2')),('queue dict',{'queued_for_assignee':'ops','queued_behind_ticket':'SYRD-2'}),('queue missing',None)]:
     case(name,'_superseded_queue_notice','SYRD-1',queue_payload,queue=row)
    case('queue invalid','_superseded_queue_notice','SYRD-1','{}',queue=('ops','SYRD-2'))
    terminal={'stages':[{'name':'done','terminal':True},{'name':'in_progress','terminal':False,'kind':'implementer'}]}
    notices=[
     ('plain current','{"id":"SYRD-1","kind":"transition","state":"in_progress","assignee":"main"}',T,{}),
     ('wrong ticket','{"id":"SYRD-2","kind":"transition"}',T,{}),
     ('wrong state','{"kind":"transition","state":"audit"}',T,{}),
     ('wrong owner','{"kind":"transition","assignee":"ops"}',T,{}),
     ('terminal matching','{"kind":"transition","state":"done"}',('done','unassigned',False,False,False),{}),
     ('terminal stale','{"kind":"transition","state":"audit"}',('done','unassigned',False,False,False),{}),
     ('invalid nonterminal','broken{',T,{}),
     ('invalid terminal','broken{',('done','unassigned',False,False,False),{}),
     ('awaiting yes','{"kind":"awaiting_role","awaiting_role":"main","awaiting_since_at":"T1","expires_at":"T2"}',T,{'exists':(True,)}),
     ('awaiting blocked','{"kind":"awaiting_role","awaiting_role":"main","awaiting_since_at":"T1","expires_at":"T2"}',('in_progress','main',False,False,True),{'exists':(True,)}),
     ('escalation yes','{"kind":"escalation"}',T,{}),
     ('escalation held','{"kind":"escalation"}',('in_progress','main',True,False,False),{}),
     ('manual transition','{"kind":"transition","state":"in_progress","assignee":"main"}',('in_progress','main',True,False,False),{}),
     ('parked transition','{"kind":"transition","state":"in_progress","assignee":"main"}',('in_progress','main',False,True,False),{}),
     ('manual reminder','{"kind":"idle_reminder"}',('in_progress','main',True,False,False),{}),
    ]
    for name,payload,ticket,extra in notices:
     case(name,'_notification_is_current','SYRD-1','main',payload,ticket=ticket,**extra)
    for name,payload in [('serial legacy','{"kind":"transition","new_state":"in_progress","assignee":"main"}'),('serial review','{"kind":"transition","new_state":"audit","assignee":"main"}'),('serial wrong role','{"kind":"transition","new_state":"in_progress","assignee":"ops"}'),('serial malformed','broken{')]:
     out.append([name,owner._serial_gate_stage('main',payload)])
    for name,row in [('blocker tuple',('SYRD-9',)),('blocker dict',{'id':'SYRD-10'}),('blocker missing',None)]:
     case(name,'_finish_current_blocker','SYRD-1','main','{"kind":"transition","new_state":"in_progress","assignee":"main"}',blocker=row)
    # The disposition consumes the same in-memory ledger state and writes the same trace/discard statements.
    conn=Conn(); l.ledger._traced_gate_defer_notifications.add(99)
    owner._drop_superseded_notification(conn,notification_id=99,ticket_id='SYRD-1',target_role='main',kind='idle_reminder',detail={'awaiting_role':'director'},phase='claim')
    out.append(['drop default',conn.calls,log.logs[:],sorted(l.ledger._traced_gate_defer_notifications)]); log.logs.clear()
    conn=Conn(); owner._drop_superseded_notification(conn,notification_id=100,ticket_id='SYRD-1',target_role='main',kind='transition',detail={'held_seconds':900},phase='prior_turn_hold',reason='owner_already_acted')
    out.append(['drop override',conn.calls,log.logs[:]]); log.logs.clear()
    workflow={'roles':[{'name':'director','serial':False},{'name':'main','serial':True}],
              'stages':[{'name':'analysis','terminal':False,'kind':'system','owners':['director'],'notify':{'kind':'assignee'}},
                        {'name':'in_progress','terminal':False,'kind':'implementer','owners':['main'],'notify':{'kind':'assignee'}},
                        {'name':'done','terminal':True,'kind':'system','owners':[],'notify':{'kind':'none'}}]}
    l.workflow=workflow
    out.append(['declared triage',owner._current_target_role('triage','analysis','unassigned')])
    out.append(['declared transition',owner._current_target_role('transition','in_progress','main')])
    out.append(['declared serial',owner._serial_gate_stage('main','{"kind":"transition","new_state":"in_progress","assignee":"main"}')])
    conn=Conn(ticket=T)
    out.append(['declared current',owner._notification_is_current(conn,'SYRD-1','main','{"kind":"transition","state":"in_progress","assignee":"main"}'),conn.calls])
    workflow['roles'][1]['serial']=False
    out.append(['declared serial changed late',owner._serial_gate_stage('main','{"kind":"transition","new_state":"in_progress","assignee":"main"}')])
    original_decoder=l._decode_text
    l._decode_text=lambda value: 'patched:'+str(value)
    conn=Conn(ticket=(b'in_progress',b'main',False,False,False))
    out.append(['decoder patched late',owner._current_ticket_state(conn,'SYRD-1')])
    l._decode_text=original_decoder
    raw=json.dumps(out,sort_keys=True,default=str,separators=(',',':'))
    assert len(out) == 56
    assert hashlib.sha256(raw.encode()).hexdigest() == GOLDEN
    assert not hits
    return len(out)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--installed-import":
        sys.path.insert(0, str(ROOT / "scripts"))
        from ticket_board import notify_listener as nl
    else:
        sys.path.insert(0, str(ROOT))
        from scripts.ticket_board import notify_listener as nl
    assert_ownership(nl)
    count = run_probe(nl)
    print(f"notification_eligibility_boundary_test: {count} cases ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
