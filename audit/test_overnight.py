import json,os,tempfile,unittest
from unittest import mock
import run_audit as r
class OvernightTests(unittest.TestCase):
 def test_bad_page_does_not_stop_next_and_checkpoint_records_failure(self):
  sample=[{'url':'https://app-navi.biz/a/','id':'a'},{'url':'https://app-navi.biz/b/','id':'b'}]
  with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{'VWD_CHECKPOINT_DIR':d}), mock.patch.object(r,'fetch_with_retry',return_value=(mock.Mock(status_code=200,text='x'),None)), mock.patch.object(r,'parse_page',side_effect=[ValueError('broken'),{'title':'ok'}]),mock.patch.object(r.time,'sleep'):
   pages,failures,budget=r.crawl_pages(sample,set())
   self.assertEqual([x['id'] for x in pages],['b']);self.assertEqual(len(failures),1)
   with open(os.path.join(d,'crawl-checkpoint.json')) as f:c=json.load(f)
   self.assertEqual(c['pending'],[]);self.assertIn('parse_error',c['failures'][0]['reason'])
 def test_unprocessed_urls_remain_pending(self):
  with tempfile.TemporaryDirectory() as d,mock.patch.dict(os.environ,{'VWD_CHECKPOINT_DIR':d}):
   r.save_crawl_checkpoint([{'url':'a'},{'url':'b'}],[{'url':'a'}],[])
   with open(os.path.join(d,'crawl-checkpoint.json')) as f:c=json.load(f)
   self.assertEqual(c['pending'],['b']);self.assertEqual(c['phase'],'crawl_only_not_magi_classified')
