import sys,tempfile,unittest,copy,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import claude_live as live
import usage

class LiveTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  for key,val in [('EVENTS',Path(self.temp.name)/'events'),('CREDS',Path(self.temp.name)/'absent')]:
   p=patch.object(live,key,val);p.start();self.addCleanup(p.stop)
  usage._live_updated.clear()
  self.payload={'session_id':'test-session','rate_limits':{'five_hour':{'used_percentage':42,'resets_at':2000},'seven_day':{'used_percentage':49,'resets_at':3000}},'context_window':{'total_input_tokens':100},'cost':{'total_api_duration_ms':10},'transcript_path':'PRIVATE_PATH','cwd':'PRIVATE_WORKSPACE'}
 def prime(self):
  live.capture(self.payload,now=1000)
  self.payload['cost']['total_api_duration_ms']=20
  return live.capture(self.payload,now=1010)
 def test_initial_idle_session_is_only_a_baseline(self):
  item=live.capture(self.payload,now=1000)
  self.assertEqual(item['updated'],0)
  self.assertIsNone(live.latest(now=1000))
 def test_new_response_is_delivered(self):
  self.prime();item=live.latest(now=1011)
  self.assertEqual(item['updated'],1010);self.assertEqual(item['limits'][0]['used'],42)
 def test_replay_never_refreshes_timestamp(self):
  self.prime();live.capture(self.payload,now=1080)
  self.assertEqual(live.latest(now=1081)['updated'],1010)
  self.assertIsNone(live.latest(now=1101))
 def test_expired_window_not_invented_as_zero(self):
  self.prime();self.assertIsNone(live.latest(now=2100))
 def test_missing_window_is_not_zero(self):
  del self.payload['rate_limits']['five_hour']
  self.assertIsNone(live.capture(self.payload,now=1000))
 def test_private_payload_fields_are_not_saved(self):
  self.prime();text=next(live.EVENTS.glob('*.json')).read_text()
  self.assertNotIn('PRIVATE',text);self.assertNotIn('test-session',text)
 def test_credential_change_invalidates_feed(self):
  self.prime();live.CREDS.write_text('changed')
  self.assertIsNone(live.latest(now=1011))
 def test_older_api_does_not_replace_new_local(self):
  self.prime()
  data={'claude':{'ok':False,'updated':1000,'limits':[]}}
  result=usage.merge_live(data,now=1011)
  self.assertTrue(result['claude']['ok']);self.assertEqual(result['claude']['source'],'claude-code')
  self.assertFalse(data['claude']['ok'])
 def test_newer_api_is_kept(self):
  self.prime();data={'claude':{'ok':True,'updated':1012,'limits':[],'source':'api'}}
  self.assertIs(usage.merge_live(data,now=1013),data)
 def test_fresh_local_reading_survives_network_error(self):
  self.prime();data={'claude':{'ok':False,'updated':1010,'limits':[],'http_status':429}}
  self.assertTrue(usage.merge_live(data,now=1011)['claude']['ok'])
 def test_broken_event_ignored(self):
  live.EVENTS.mkdir();(live.EVENTS/'broken.json').write_text('{')
  self.assertIsNone(live.latest(now=1000))

if __name__ == "__main__":
    unittest.main()
