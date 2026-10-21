import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from .checks import Checks, json_values
from .run import Client, aging_official, references, merge, public_url, theme_of, THEMES

class DataChecksTests(unittest.TestCase):
    def test_suppression_is_not_zero(self):
        c=Checks();c.inspect({'total':3,'dom':'***','intl':3},'test','airport')
        self.assertEqual(c.counts['identity_checks'],0)
        self.assertEqual(c.counts['identity_skipped_missing_or_non_numeric'],1)

    def test_incorrect_total_detected(self):
        c=Checks();c.inspect({'total':10,'dom':8,'intl':8},'test','airport')
        self.assertEqual(c.counts['finding_error'],1)

    def test_age_known_denominator_and_rounding(self):
        c=Checks();c.inspect({'under15':1,'working':4,'over65':2,'known':7,'rate':28.57},'test','aging_rate')
        self.assertEqual(c.counts['finding_error'],0)
        c.inspect({'under15':1,'working':4,'over65':2,'known':10,'rate':20},'test','aging_rate')
        self.assertEqual(c.counts['finding_error'],1)

    def test_duplicate_keys_and_no_js_execution(self):
        values,dup=json_values('window.DATA={"a":1,"a":2}; dangerous();')
        self.assertEqual(dup,['a']);self.assertEqual(values,[('DATA',{'a':2})])
        self.assertEqual(json_values('const X={a:()=>steal()};')[0],[])

    def test_bad_geometry(self):
        c=Checks();c.inspect({'type':'Polygon','coordinates':[[[139,35],[140,35],[140,36]]]},'test','road')
        self.assertEqual(c.counts['finding_error'],1)

    def test_no_privileged_urls_or_external_asset_crawl(self):
        self.assertFalse(public_url('https://app-navi.biz/wp-admin/'))
        self.assertFalse(public_url('https://app-navi.biz/?rest_route=/x'))
        refs,_,_=references('const ROOT="../data/";fetch(ROOT+"index.json"); const x="https://evil.example/x.json";', 'https://app-navi.biz/maps/test/app.js')
        self.assertEqual(refs,['https://app-navi.biz/maps/data/index.json'])

    def test_sitemap_classification(self):
        self.assertEqual(theme_of('https://app-navi.biz/station-passenger-ranking-kanto-en/'),'station_ranking')
        self.assertEqual(theme_of('https://app-navi.biz/population-national/'),'population')
        self.assertEqual(len(THEMES),12)

    def test_failed_fetch_does_not_prevent_next_resource(self):
        with tempfile.TemporaryDirectory() as d:
            c=Client(Path(d),delay=0);c.allowed=lambda u:True
            def raw(u):
                if u.endswith('bad.json'):raise ValueError('broken')
                return b'{"x":1}',u,'application/json'
            c.raw=raw
            self.assertIsNone(c.get('https://app-navi.biz/maps/bad.json')[0])
            self.assertEqual(c.get('https://app-navi.biz/maps/good.json')[0],b'{"x":1}')
            self.assertEqual([r['status'] for r in c.records],['error','fetched'])

    def test_official_wrong_year_or_empty_cannot_pass(self):
        class Fake:
            def get(self,*args):
                return json.dumps({'GET_STATS':{'STATISTICAL_DATA':{'DATA_INF':{'DATA_OBJ':[]}}}}).encode(),{}
        d={'rows':[{'code':'01101','values':{'2025':{'under15':1,'working':2,'over65':3}}}]}
        r=aging_official(Fake(),d,Checks())
        self.assertNotEqual(r['status'],'matched')
        self.assertEqual(r['years']['2025']['missing'],3)

    def test_missing_theme_reports_never_all_verified(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);merge(root/'inputs',root/'out',{'groups':{}})
            r=json.loads((root/'out/report.json').read_text())
            self.assertFalse(r['full_official_verification'])
            self.assertEqual(len(r['missing_theme_reports']),12)

if __name__=='__main__':unittest.main()

class InfectionTests(unittest.TestCase):
    def test_dated_csv_wrong_week_rejected(self):
        from .infection import csv_table
        raw='title\r2026年40週\r,手足口病,\r,報告,定当\r総数,123,0.81\r'.encode('cp932')
        with self.assertRaises(ValueError):csv_table(raw,'手足口病',2026,39)
        self.assertEqual(csv_table(raw,'手足口病',2026,40)['総数'],{'count':'123','rate':'0.81'})

    def test_count_and_rate_not_interchanged_or_zeros_imputed(self):
        from .infection import compare
        class Fake:
            def get(self,*args):
                return 'title\n2026年40週\n,手足口病,\n,報告,定当\n総数,123,0.81\n北海道,-,-\n'.encode('cp932'),{}
        catalog={'prefs':{'01':'北海道'},'seriesIndex':{'2026-w40-手足口病':{'path':'2026/w40/d072.json'}}}
        records=[('https://app-navi.biz/maps/2026/w40/d072.json',{'periodType':'week','weeks':[40],'sources':['https://id-info.jihs.go.jp/x.csv'],'prefs':{'00':{'rate':.81},'01':{'rate':None}}})]
        r=compare(Fake(),catalog,records)
        self.assertEqual(r['numeric_values_compared'],1)
        self.assertEqual(r['numeric_mismatches'],0)
        self.assertEqual(r['official_non_numeric'],1)
        self.assertEqual(r['non_numeric_mapped_to_zero_review'],0)
