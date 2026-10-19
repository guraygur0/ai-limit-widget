import os,sys,runpy,tempfile,json,time,unittest
from pathlib import Path
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM']='offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import usage
import claude_live
from PySide6.QtWidgets import QApplication
app=QApplication([])
m=runpy.run_path(str(ROOT/'widget.pyw'))
class UITests(unittest.TestCase):
 def setUp(self):
  usage._poll_intervals.clear()
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.p=Path(self.temp.name)
  pa=patch.object(claude_live,'EVENTS',self.p/'events');pa.start();self.addCleanup(pa.stop)
  for key,val in [('STATE',self.p),('CACHE',self.p/'usage.json')]:
   pa=patch.object(usage,key,val);pa.start();self.addCleanup(pa.stop)
  self.globals=m['settings'].__globals__
  pa=patch.dict(self.globals,SETTINGS=self.p/'window.json');pa.start();self.addCleanup(pa.stop)
 def test_bad_settings(self):
  for content in ['null','[]','{"scale": "NaN", "x":null,"y":"bad"}','{"scale":-99}']:
   (self.p/'window.json').write_text(content)
   w=m['Widget'](preview=True)
   self.assertGreater(w.width(),0);self.assertLess(w.width(),1000);w.close()
 def test_partial_independent(self):
  w=m['Widget'](preview=True);w.busy=True;w.requested_providers=['claude','codex']
  w.receive_partial({'codex':{'ok':True,'updated':time.time(),'limits':[]}})
  self.assertEqual(w.requested_providers,['claude']);self.assertTrue(w.busy);w.close()
 def test_stale_after_sleep(self):
  w=m['Widget'](preview=True);w.busy=True
  w.data={'claude':{'ok':True,'updated':time.time()-120,'limits':[]}}
  w.tick();self.assertTrue(w.data['claude']['stale']);w.close()
 def test_offscreen_position_recovered(self):
  w=m['Widget'](preview=True);w.move(-50000,-50000);w.ensure_on_screen()
  self.assertTrue(QApplication.primaryScreen().availableGeometry().contains(w.pos()));w.close()
 def test_hidden_nimbus(self):
  w=m['Widget'](preview=True)
  self.assertEqual(w.visible_limits({'limits':[{'label':'Nimbus Quill','used':0},{'label':'Weekly','used':29}]}),[{'label':'Weekly','used':29}]);w.close()
 def test_new_pool_does_not_change_card_height(self):
  w=m['Widget'](preview=True)
  core={'ok':True,'limits':[{'label':'Session','used':42},{'label':'Weekly','used':49}]}
  extra=dict(core,limits=core['limits']+[{'label':'Iguana Necktie','used':0},{'label':'Future Name','used':100}])
  self.assertEqual(w.section_height(core),w.section_height(extra));w.close()
 def test_retry_status_is_explicit(self):
  self.assertIn('Rate limited · Retry in 2:05',m['Widget'].failure_text({'http_status':429,'retry_at':1125},now=1000))
 def test_old_data_is_explicit_even_during_slow_cadence(self):
  w=m['Widget'](preview=True);w.busy=True;usage._poll_intervals['claude']=300
  w.data={'claude':{'ok':True,'updated':time.time()-180,'limits':[]}}
  w.tick();self.assertTrue(w.data['claude'].get('stale',False));w.close()
if __name__ == "__main__":
    unittest.main()
