import sys,tempfile,unittest,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import usage

class UsageTests(unittest.TestCase):
    def setUp(self):
        for state in (usage._next_attempt,usage._failures,usage._cooldown_until,usage._server_until,usage._last_attempt,usage._poll_intervals,usage._live_updated):
            state.clear()
        log_patch=patch.object(usage,'diagnostic');log_patch.start();self.addCleanup(log_patch.stop)
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        for name,val in [('STATE',Path(self.folder.name)),('CACHE',Path(self.folder.name)/'usage.json')]:
            p=patch.object(usage,name,val);p.start();self.addCleanup(p.stop)
    def test_slow_provider_does_not_delay_other_result(self):
        import threading
        release=threading.Event()
        received=[]
        def fetch(key):
            if key=='claude':
                self.assertTrue(release.wait(2))
            return {'ok':True,'updated':time.time(),'limits':[]}
        def partial(data):
            received.extend(data)
            if 'codex' in data:
                release.set()
        with patch.object(usage,'fetch',side_effect=fetch):
            usage.collect(on_result=partial)
        self.assertEqual(received,['codex','claude'])

    def test_hung_child_is_killed_and_reaped(self):
        script=Path(self.folder.name)/'sleep.py'
        script.write_text('import time; time.sleep(20)')
        start=time.monotonic()
        with patch.object(usage,'__file__',str(script)),patch.object(usage,'REQUEST_DEADLINE',0.3):
            result=usage.fetch('claude')
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'],'Request timed out')
        self.assertLess(time.monotonic()-start,3)

    def test_bad_cache_shapes_recover(self):
        for value in ['null','[]','{"claude":null}','{"claude":{"ok":true,"updated":null,"limits":null}}']:
            usage.CACHE.write_text(value)
            result=usage.cached()
            self.assertIsInstance(result,dict)
            self.assertFalse(result.get('claude',{}).get('ok',False))

    def test_invalid_cached_rows_rejected(self):
        usage.CACHE.write_text('{"claude":{"ok":true,"updated":1000,"limits":[null,{"label":"Session","used":"NaN"}]}}')
        self.assertFalse(usage.cached()['claude']['ok'])

    def test_restart_restores_backoff_and_debounce(self):
        import json
        usage.CACHE.write_text(json.dumps({'claude':{'updated':900,'attempted':999,'retry_at':1600,'failures':3}}))
        with patch.object(usage.time,'time',return_value=1000):
            usage.cached()
            self.assertFalse(usage.due('claude'))
            self.assertFalse(usage.due('claude',manual=True))
        self.assertEqual(usage._failures['claude'],3)
        self.assertEqual(usage._next_attempt['claude'],1600)

    def test_failed_atomic_save_preserves_old_file(self):
        usage.CACHE.write_text('{"old":true}')
        self.assertFalse(usage.atomic_json(usage.CACHE,{'bad':float('nan')}))
        self.assertEqual(usage.CACHE.read_text(),'{"old":true}')
        self.assertEqual(list(usage.CACHE.parent.glob('*.tmp')),[])

    def test_callback_failure_does_not_lose_result(self):
        def broken(data): raise RuntimeError('UI disconnected')
        with patch.object(usage,'fetch',return_value={'ok':True,'updated':1000,'limits':[]}):
            result=usage.collect(on_result=broken)
        self.assertTrue(result['claude']['ok'])
        self.assertTrue(result['codex']['ok'])

    def test_callback_cannot_mutate_saved_response(self):
        def mutate(data): data['claude' if 'claude' in data else 'codex']['ok']=False
        with patch.object(usage,'fetch',side_effect=lambda key:{'ok':True,'updated':1000,'limits':[]}):
            result=usage.collect(on_result=mutate)
        self.assertTrue(result['claude']['ok'])
        self.assertTrue(result['codex']['ok'])

    def test_bad_worker_shape_becomes_failure(self):
        with patch.object(usage,'fetch',return_value=None):
            result=usage.collect()
        self.assertFalse(result['claude']['ok'])

    def test_invalid_percentages_rejected(self):
        for value in ['NaN','Infinity',-1,101]:
            with self.assertRaises(ValueError): usage.limit('Session',value,None)

    def test_retry_header_nonfinite(self):
        for value in ['NaN','Infinity','-20']:
            self.assertEqual(usage.retry_seconds(value),0)

    def test_clock_correction_keeps_remaining_delay(self):
        usage._clock_sample=None
        usage.reconcile_clock(1000,100)
        usage._next_attempt['claude']=1060
        usage.reconcile_clock(410,110)
        self.assertEqual(usage._next_attempt['claude'],460)
        self.assertFalse(usage.due('claude',now=410))
        self.assertTrue(usage.due('claude',now=460))

    def test_redirect_refused(self):
        self.assertIsNone(usage.NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid'))

    def test_live_401_is_actionable_and_no_secret_returned(self):
        import json,urllib.error
        auth=Path(self.folder.name)/'credentials.json'
        auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'test-secret-not-real'}}))
        with patch.object(usage,'CLAUDE_CREDS',auth),patch.object(usage.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://example.invalid',401,'Unauthorized',{},None)
            result=usage._fetch('claude')
        self.assertEqual(result['error'],'Sign in again')
        self.assertNotIn('test-secret',json.dumps(result))

    def test_live_429_honors_server_retry(self):
        import json,urllib.error
        auth=Path(self.folder.name)/'credentials.json'
        auth.write_text(json.dumps({'claudeAiOauth':{'accessToken':'test-only'}}))
        with patch.object(usage,'CLAUDE_CREDS',auth),patch.object(usage.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://example.invalid',429,'Too many',{'Retry-After':'1200'},None)
            result=usage._fetch('claude')
        self.assertEqual(result['retry'],1200)
        self.assertEqual(result['server_retry'],1200)

    def test_offline_then_recovery(self):
        old={'claude':{'limits':[{'label':'Session','used':40,'reset':None}],'updated':900}}
        with patch.object(usage.time,'time',return_value=1000),patch.object(usage,'fetch',return_value={'ok':False,'error':'Connection unavailable','retry':60}):
            failed=usage.collect(old)
        self.assertTrue(failed['claude']['stale'])
        self.assertEqual(failed['claude']['updated'],900)
        with patch.object(usage.time,'time',return_value=1061),patch.object(usage,'fetch',side_effect=lambda key:{'ok':True,'updated':1061,'limits':[{'label':'Session','used':41,'reset':None}]}):
            recovered=usage.collect(failed)
        self.assertTrue(recovered['claude']['ok'])
        self.assertFalse(recovered['claude'].get('stale',False))
        self.assertEqual(usage._failures['claude'],0)

    def test_unknown_pools_never_become_display_rows(self):
        data={'limits':[{'label':'Iguana Necktie','used':0},{'label':'Weekly','used':49},
                        {'label':'Nimbus Quill','used':0},{'label':'Session','used':42},
                        {'label':'Another Future Name','used':100}]}
        self.assertEqual([r['label'] for r in usage.display_limits(data)],['Session','Weekly'])
        rows=usage.parse_claude({'iguana_necktie':{'utilization':'malformed'},'five_hour':{'utilization':42},'seven_day':{'utilization':49}})
        self.assertEqual([r['used'] for r in rows],[42,49])

    def test_old_cache_unknown_rows_removed_without_faking_freshness(self):
        import json
        usage.CACHE.write_text(json.dumps({'claude':{'ok':True,'updated':900,'limits':[
            {'label':'Session','used':42},{'label':'Iguana Necktie','used':0},{'label':'Weekly','used':49}]}}))
        with patch.object(usage.time,'time',return_value=1000): result=usage.cached()['claude']
        self.assertEqual([r['label'] for r in result['limits']],['Session','Weekly'])
        self.assertEqual(result['updated'],900)

    def test_429_learns_slower_cadence_through_success_and_restart(self):
        with patch.object(usage.time,'time',return_value=1000),patch.object(usage,'fetch',side_effect=lambda key:{'ok':False,'http_status':429,'retry':600}):
            failed=usage.collect()
        self.assertEqual(usage.poll_interval('claude'),300)
        with patch.object(usage.time,'time',return_value=1601),patch.object(usage,'fetch',side_effect=lambda key:{'ok':True,'updated':1601,'limits':[{'label':'Session','used':42}]}):
            recovered=usage.collect(failed)
        self.assertEqual(usage._next_attempt['claude'],1901)
        usage._poll_intervals.clear()
        with patch.object(usage.time,'time',return_value=1800):
            restored=usage.cached()
        self.assertEqual(usage.poll_interval('claude'),300)
        self.assertFalse(restored['claude']['stale'])

    def test_legacy_429_cache_migrates_to_slower_cadence(self):
        import json
        usage.CACHE.write_text(json.dumps({'claude':{'http_status':429,'cooldown_until':2000,'updated':900}}))
        with patch.object(usage.time,'time',return_value=1000): usage.cached()
        self.assertEqual(usage.poll_interval('claude'),300)
        self.assertFalse(usage.due('claude',manual=True,now=1000))

    def test_reset_schedules_early_refresh(self):
        usage._poll_intervals['claude']=300
        self.assertEqual(usage.schedule_success('claude',{'limits':[{'reset':1015}]},1000),1016)
        self.assertEqual(usage.schedule_success('claude',{'limits':[{'reset':900}]},1000),1300)

    def test_local_updates_suppress_only_automatic_requests(self):
        usage._live_updated['claude']=990
        self.assertFalse(usage.due('claude',now=1000))
        self.assertTrue(usage.due('claude',manual=True,now=1000))

    def test_parse_all_pools(self):
        rows=usage.parse_claude({'five_hour':{'utilization':5},'seven_day':{'utilization':44},'new_pool':{'utilization':0}})
        self.assertEqual([r['used'] for r in rows],[5,44])
    def test_auto_cadence(self):
        with patch.object(usage.time,'time',return_value=1000),patch.object(usage,'fetch',return_value={'ok':True,'updated':1000,'limits':[]}) as f:
            usage.collect();self.assertEqual(f.call_count,2)
            self.assertEqual(usage._next_attempt,{'claude':1060,'codex':1060})
            usage.collect();self.assertEqual(f.call_count,2)
        self.assertTrue(usage.due('claude',now=1060))
        self.assertTrue(usage.due('codex',now=1060))
        self.assertTrue(usage.due('claude',now=1300))
    def test_manual_bypasses_normal_timer(self):
        usage._next_attempt.update(claude=1300,codex=1060)
        with patch.object(usage.time,'time',return_value=1010),patch.object(usage,'fetch',return_value={'ok':True,'updated':1010,'limits':[]}) as f:
            usage.collect(manual=True);self.assertEqual(f.call_count,2)
    def test_manual_bypasses_local_backoff_only(self):
        usage._cooldown_until['claude']=1600
        self.assertFalse(usage.due('claude',manual=True,now=1000))
        self.assertFalse(usage.due('claude',now=1000))
        usage._server_until['claude']=1200
        self.assertFalse(usage.due('claude',manual=True,now=1000))
        self.assertFalse(usage.due('claude',manual=True,now=1201))
        self.assertTrue(usage.due('claude',manual=True,now=1601))
    def test_429_keeps_last_measurement(self):
        old={'claude':{'limits':[{'label':'Session','used':5,'reset':None}],'updated':900}}
        with patch.object(usage.time,'time',return_value=1000),patch.object(usage,'fetch',return_value={'ok':False,'error':'Rate limited','http_status':429,'retry':600,'server_retry':120}):
            result=usage.collect(old)
        self.assertEqual(result['claude']['updated'],900)
        self.assertTrue(result['claude']['stale'])
        self.assertEqual(usage._cooldown_until['claude'],1600)
        self.assertEqual(usage._server_until['claude'],1120)
    def test_retry_after_date(self):
        with patch.object(usage.time,'time',return_value=0):
            self.assertEqual(usage.retry_seconds('Thu, 01 Jan 1970 00:02:00 GMT'),120)
        self.assertEqual(usage.retry_seconds('900'),900)
        self.assertEqual(usage.retry_seconds(None),0)
    def test_worker_failure_does_not_drop_other_provider(self):
        def fetch(key):
            if key=='claude': raise ValueError('bad data')
            return {'ok':True,'updated':1000,'limits':[]}
        with patch.object(usage,'fetch',side_effect=fetch):
            result=usage.collect()
        self.assertFalse(result['claude']['ok']);self.assertTrue(result['codex']['ok'])
    def test_restart_preserves_server_wait(self):
        import json
        usage.CACHE.write_text(json.dumps({'claude':{'cooldown_until':1600,'server_until':1400,'updated':900}}))
        with patch.object(usage.time,'time',return_value=1000):
            usage.cached()
            self.assertFalse(usage.due('claude',manual=True))
    def test_manual_double_click_deduplicates(self):
        usage._last_attempt['claude']=1000
        self.assertFalse(usage.due('claude',manual=True,now=1001))
        self.assertTrue(usage.due('claude',manual=True,now=1010))

if __name__ == "__main__":
    unittest.main()
