"""Claude Code statusLine bridge: quota/counter fingerprints only, no transcript reads."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import time
from usage import atomic_json, limit

ROOT=Path(__file__).resolve().parent
EVENTS=ROOT/'state'/'claude-events'
CREDS=Path(os.environ.get('USERPROFILE',str(Path.home())))/'.claude'/'.credentials.json'
FRESH_FOR=90


def credential_revision():
    try: return CREDS.stat().st_mtime_ns
    except OSError: return None


def read(path):
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError): return {}


def capture(payload,now=None):
    now=time.time() if now is None else now
    if not isinstance(payload,dict) or not payload.get('session_id'):
        return None
    rates=payload.get('rate_limits')
    if not isinstance(rates,dict): return None
    rows=[]
    for key,label in (('five_hour','Session'),('seven_day','Weekly')):
        node=rates.get(key)
        if not isinstance(node,dict) or node.get('used_percentage') is None: return None
        try: rows.append(limit(label,node['used_percentage'],node.get('resets_at')))
        except (TypeError,ValueError,OverflowError): return None
    if any(r['reset'] is not None and r['reset']<=now for r in rows): return None
    counters=payload.get('context_window') or {}
    cost=payload.get('cost') or {}
    if not isinstance(counters,dict) or not isinstance(cost,dict): return None
    # These counters change on API responses, not on a status line repaint or timer.
    signature=[rows,counters.get('total_input_tokens'),counters.get('total_output_tokens'),cost.get('total_api_duration_ms')]
    fingerprint=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
    name=hashlib.sha256(str(payload['session_id']).encode()).hexdigest()[:24]+'.json'
    path=EVENTS/name
    old=read(path)
    revision=credential_revision()
    if old.get('fingerprint')==fingerprint and old.get('credential_revision')==revision:
        return old  # Replaying unchanged session state must NOT make it younger.
    # First observation establishes a baseline; an idle old session is not a new reading.
    updated=now if old and old.get('credential_revision')==revision else 0
    record={'limits':rows,'fingerprint':fingerprint,'updated':updated,'credential_revision':revision}
    atomic_json(path,record)
    # Bound our own per-session records; never inspect or retain conversation text.
    try:
        owned=[p for p in EVENTS.glob('*.json') if re.fullmatch(r'[0-9a-f]{24}\.json',p.name)]
        for old_path in sorted(owned,key=lambda p:p.stat().st_mtime,reverse=True)[64:]:
            if old_path.parent.resolve()==EVENTS.resolve():
                old_path.unlink(missing_ok=True)
    except OSError:
        pass
    return record


def latest(now=None):
    now=time.time() if now is None else now
    try:
        paths=sorted(EVENTS.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:32]
    except OSError: return None
    best=None
    revision=credential_revision()
    for path in paths:
        item=read(path)
        try:
            stamp=float(item.get('updated') or 0)
            if not 0<=now-stamp<=FRESH_FOR: continue
            if item.get('credential_revision')!=revision: continue
            rows=[limit(r['label'],r['used'],r.get('reset')) for r in item.get('limits',[])]
            if [r['label'] for r in rows]!=['Session','Weekly']: continue
            if any(r['reset'] is not None and r['reset']<=now for r in rows): continue
            if best is None or stamp>best['updated']:
                best={'ok':True,'limits':rows,'updated':stamp,'stale':False,'source':'claude-code'}
        except (TypeError,ValueError,KeyError,OverflowError): continue
    return best


def main():
    try:
        payload=json.loads(sys.stdin.read(262144))
        item=capture(payload)
        if item:
            print(' | '.join(f"{r['label']} {100-r['used']:.0f}% left" for r in item['limits']))
    except (OSError,ValueError,TypeError):
        pass  # Never interfere with Claude's work or emit private payloads/errors.


if __name__=='__main__': main()
