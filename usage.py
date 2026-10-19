from __future__ import annotations
import concurrent.futures
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import math
import copy
import tempfile
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
CACHE = STATE / 'usage.json'
HOME = Path(os.environ.get('USERPROFILE', str(Path.home())))
CLAUDE_CREDS = HOME / '.claude' / '.credentials.json'
CODEX_AUTH = Path(os.environ.get('CODEX_HOME', str(HOME / '.codex'))) / 'auth.json'
_next_attempt = {}
_failures = {}
_cooldown_until = {}
_last_attempt = {}
_server_until = {}
_poll_intervals = {}
_live_updated = {}
INTERVALS = {'claude': 60, 'codex': 60}
REQUEST_DEADLINE = 25
_clock_sample = None


def number(value, default=0):
    try:
        value=float(value)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError, OverflowError):
        return default


def atomic_json(path, data):
    """Unique temporary files avoid collisions; failed writes preserve the last file."""
    temporary=None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name+'.', suffix='.tmp', delete=False) as handle:
            temporary=Path(handle.name)
            json.dump(data,handle,allow_nan=False)
        temporary.replace(path)
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def diagnostic(provider, item):
    # Only allowlisted status metadata; never log responses, credentials or exceptions.
    try:
        logger=logging.getLogger('usage-widget')
        if not logger.handlers:
            STATE.mkdir(parents=True,exist_ok=True)
            logger.addHandler(RotatingFileHandler(STATE/'health.log',maxBytes=131072,backupCount=1,encoding='utf-8'))
            logger.setLevel(logging.INFO)
            logger.propagate=False
        logger.info(json.dumps({'time':time.time(),'provider':provider,'ok':item.get('ok'),
                               'http_status':item.get('http_status'),'duration':item.get('duration'),
                               'poll_interval':item.get('poll_interval'),'next_attempt':item.get('next_attempt'),'failures':item.get('failures',0)}))
    except OSError:
        pass


def poll_interval(provider):
    return _poll_intervals.get(provider, INTERVALS[provider])


def merge_live(data, now=None):
    from claude_live import latest
    now=time.time() if now is None else now
    event=latest(now)
    if event:
        _live_updated['claude']=event['updated']
        previous=data.get('claude',{})
        if event['updated']>(previous.get('updated') or 0) or (event['updated']==previous.get('updated') and not previous.get('ok')):
            data=copy.deepcopy(data)
            item=dict(previous)
            item.update(event)
            data['claude']=item
    return data


def schedule_success(provider, current, finished):
    scheduled=finished+poll_interval(provider)
    # Re-query just after the quota window resets, without inventing a new percentage.
    resets=[r.get('reset') for r in current.get('limits',[]) if isinstance(r,dict)]
    upcoming=[r for r in resets if isinstance(r,(int,float)) and math.isfinite(r) and r>finished]
    return min(scheduled,min(upcoming)+1) if upcoming else scheduled


def display_limits(data):
    """Only stable user-facing pools. Unknown/renamed API fields never become UI rows."""
    rows={}
    for row in data.get('limits') or []:
        if isinstance(row,dict):
            key=str(row.get('label','')).strip().casefold()
            if key in ('session','weekly') and key not in rows:
                rows[key]=row
    return [rows[key] for key in ('session','weekly') if key in rows]


def reconcile_clock(wall=None, monotonic=None):
    """Keep relative polling/backoff delays stable if Windows corrects its clock."""
    global _clock_sample
    wall=time.time() if wall is None else wall
    monotonic=time.monotonic() if monotonic is None else monotonic
    previous=_clock_sample
    _clock_sample=(wall,monotonic)
    if previous is None:
        return 0
    delta=(wall-previous[0])-(monotonic-previous[1])
    if abs(delta)<5:
        return 0
    for state in (_next_attempt,_cooldown_until,_server_until,_last_attempt):
        for key,value in tuple(state.items()):
            if value:
                state[key]=value+delta
    return delta


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authenticated requests must never forward their bearer token to another URL.
        return None


def due(provider, manual=False, now=None):
    now = time.time() if now is None else now
    if now < _server_until.get(provider, 0):
        return False
    if now < _cooldown_until.get(provider, 0):
        return False
    if manual:
        return now - _last_attempt.get(provider, 0) >= 10
    if now-(_live_updated.get(provider) or 0)<60:
        return False
    return now >= _next_attempt.get(provider, 0)


def retry_seconds(value):
    try:
        parsed=float(value)
        return max(0,parsed) if math.isfinite(parsed) else 0
    except (ValueError, TypeError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp()-time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return None


def limit(label, used, reset):
    parsed=float(used)
    if not math.isfinite(parsed) or not 0 <= parsed <= 100:
        raise ValueError('Invalid percentage')
    stamp=epoch(reset)
    if stamp is not None and (not math.isfinite(stamp) or stamp<0 or stamp>32503680000):
        raise ValueError('Invalid reset time')
    return {'label': str(label)[:100], 'used': parsed, 'reset': stamp}


def parse_claude(data):
    result=[]
    for key,label in (('five_hour','Session'),('seven_day','Weekly')):
        node=data.get(key)
        if isinstance(node,dict) and node.get('utilization') is not None:
            result.append(limit(label,node['utilization'],node.get('resets_at')))
    return result


def parse_codex(data):
    result = []
    def windows(group, prefix=''):
        for key, label in [('primary_window', 'Session'), ('secondary_window', 'Weekly')]:
            node = group.get(key) or {}
            if node.get('used_percent') is None:
                continue
            reset = node.get('reset_at')
            if reset is None and node.get('reset_after_seconds') is not None:
                reset = time.time() + node['reset_after_seconds']
            result.append(limit(prefix + label, node['used_percent'], reset))
    windows(data.get('rate_limit') or {})
    windows(data.get('code_review_rate_limit') or {}, 'Code review · ')
    for group in data.get('additional_rate_limits') or []:
        if isinstance(group, dict):
            windows(group.get('rate_limit') or group, (group.get('limit_name') or group.get('metered_feature') or 'Extra') + ' · ')
    return result


def _fetch(provider):
    path = CLAUDE_CREDS if provider == 'claude' else CODEX_AUTH
    try:
        auth = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'ok': False, 'error': 'Sign in to ' + provider.title(), 'retry': 60}
    if not isinstance(auth,dict):
        return {'ok':False,'error':'Sign in again','retry':300}
    if provider == 'claude':
        oauth = auth.get('claudeAiOauth') or {}
        if not isinstance(oauth, dict):
            return {'ok':False,'error':'Sign in to Claude Code','retry':300}
        expires = number(oauth.get('expiresAt'))
        if expires and expires <= time.time()*1000:
            return {'ok':False,'error':'Claude Code login expired','retry':300,'http_status':401}
        token = oauth.get('accessToken')
        url = 'https://api.anthropic.com/api/oauth/usage'
        headers = {'anthropic-beta': 'oauth-2025-04-20'}
    else:
        tokens = auth.get('tokens') or {}
        token = tokens.get('access_token') or auth.get('access_token')
        url = 'https://chatgpt.com/backend-api/codex/usage'
        headers = {}
        if tokens.get('account_id'):
            headers['ChatGPT-Account-Id'] = tokens['account_id']
    if not token:
        return {'ok': False, 'error': 'Sign in to ' + provider.title(), 'retry': 60}
    headers.update({'Authorization': 'Bearer ' + token, 'User-Agent': 'usage-widget/2.0'})
    try:
        opener=urllib.request.build_opener(NoRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
            data = json.load(response)
        rows = parse_claude(data) if provider == 'claude' else parse_codex(data)
        if not rows:
            return {'ok': False, 'error': 'Usage unavailable', 'retry': 60}
        return {'ok': True, 'limits': rows, 'updated': time.time(), 'source':'api'}
    except urllib.error.HTTPError as error:
        retry = 60
        server_retry = retry_seconds(error.headers.get('Retry-After'))
        if error.code == 429:
            retry = max(600, server_retry)
        message = {401: 'Sign in again', 403: 'Access unavailable', 429: 'Rate limited'}.get(error.code, f'HTTP {error.code}')
        status=error.code
        error.close()
        return {'ok': False, 'error': message, 'retry': retry, 'server_retry':server_retry,'http_status': status}
    except Exception as error:
        return {'ok': False, 'error': 'Connection unavailable', 'retry': 60}


def fetch(provider):
    """A hard deadline also covers DNS, TLS and a slowly streaming response.

    Credentials stay in the child process; only sanitized quota data crosses stdout.
    subprocess.run kills and reaps a timed-out child, so workers cannot accumulate.
    """
    executable = Path(sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    try:
        completed = subprocess.run(
            [str(executable), str(Path(__file__).resolve()), '--fetch', provider],
            capture_output=True, text=True, encoding='utf-8',
            timeout=REQUEST_DEADLINE, check=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'Request timed out', 'retry': 60}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {'ok': False, 'error': 'Connection unavailable', 'retry': 60}


def cached():
    try:
        raw=json.loads(CACHE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw,dict):
        return {}
    data={}
    now=time.time()
    for provider in INTERVALS:
        item=raw.get(provider)
        if not isinstance(item,dict):
            continue
        item=dict(item)
        rows=[]
        for row in item.get('limits',[]) if isinstance(item.get('limits'),list) else []:
            if not isinstance(row,dict):
                continue
            try:
                rows.append(limit(row['label'],row['used'],row.get('reset')))
            except (KeyError,ValueError,TypeError,OverflowError):
                continue
        item['limits']=display_limits({'limits':rows})
        learned=number(item.get('poll_interval'),INTERVALS[provider])
        if item.get('http_status')==429:
            learned=max(learned,300)
        _poll_intervals[provider]=int(max(INTERVALS[provider],min(300,learned)))
        item['poll_interval']=poll_interval(provider)
        item['updated']=max(0,min(now,number(item.get('updated'))))
        item['ok']=item.get('ok') is True and bool(item['limits']) and item['updated']>0
        item['stale']=not item['ok'] or now-item['updated']>poll_interval(provider)+30
        for field in ('attempted','retry_at','next_attempt','cooldown_until','server_until'):
            item[field]=max(0,number(item.get(field)))
        _failures[provider]=int(max(0,min(10,number(item.get('failures')))))
        _last_attempt[provider]=min(now,item['attempted'])
        _server_until[provider]=item['server_until']
        _cooldown_until[provider]=item['cooldown_until']
        _next_attempt[provider]=max(item['next_attempt'],item['retry_at'],item['cooldown_until'],
                                    item['server_until'],item['updated']+poll_interval(provider))
        data[provider]=item
    return data


def collect(previous=None, manual=False, on_result=None):
    previous = copy.deepcopy(previous or {})
    result = dict(previous)
    now = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {}
        for key in ('claude', 'codex'):
            if due(key, manual, now):
                _last_attempt[key] = now
                jobs[key] = pool.submit(fetch,key)
        completed_jobs = {job: key for key, job in jobs.items()}
        for job in concurrent.futures.as_completed(completed_jobs):
            key = completed_jobs[job]
            try:
                current = jobs[key].result()
            except Exception:
                current = {'ok':False,'error':'Connection unavailable','retry':60}
            if not isinstance(current,dict) or not isinstance(current.get('ok'),bool):
                current={'ok':False,'error':'Invalid provider response','retry':60}
            finished=time.time()
            current['duration']=round(max(0,finished-now),3)
            current['attempted'] = now
            current['trigger'] = 'manual' if manual else 'automatic'
            if current['ok']:
                _failures[key] = 0
                _cooldown_until.pop(key,None)
                _server_until.pop(key,None)
                _next_attempt[key] = schedule_success(key,current,finished)
            else:
                if current.get('server_retry',0)>0:
                    current['server_until']=time.time()+current['server_retry']
                    _server_until[key]=current['server_until']
                _failures[key] = _failures.get(key, 0) + 1
                delay = max(current.get('retry', 60), min(300, 60 * 2 ** min(_failures[key]-1, 4)))
                if current.get('http_status') == 429:
                    _poll_intervals[key]=300
                    delay = max(current.get('retry',600), min(3600,600 * 2 ** min(_failures[key]-1,3)))
                    current['cooldown_until'] = finished + delay
                    _cooldown_until[key] = finished + delay
                _next_attempt[key] = finished + delay
                current['retry_at'] = max(finished+delay,_server_until.get(key,0))
                old = previous.get(key, {})
                if old.get('limits'):
                    current.update(limits=old['limits'], updated=old.get('updated'), stale=True)
            current['poll_interval']=poll_interval(key)
            current['failures']=_failures[key]
            current['next_attempt']=max(_next_attempt[key],_server_until.get(key,0))
            result[key] = current
            atomic_json(CACHE,result)
            diagnostic(key,current)
            if on_result is not None:
                try:
                    on_result(copy.deepcopy({key: current}))
                except Exception:
                    pass
    atomic_json(CACHE,result)
    return result


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fetch' and sys.argv[2] in INTERVALS:
        print(json.dumps(_fetch(sys.argv[2])))
    else:
        print(json.dumps(collect(cached()), indent=2))
