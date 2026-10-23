import io
import json
import unittest
import zipfile
from .station_year_investigation import parse_edition,candidates


class EditionTests(unittest.TestCase):
    def test_old_root_and_new_nested_utf8_directories(self):
        p={'S12_001':'駅A','S12_002':'会社A','S12_003':'線A','S12_053':622,'S12_051':1,'S12_057':664,'S12_055':1}
        payload=json.dumps({'features':[{'properties':p}]},ensure_ascii=False).encode('utf-8')
        for prefix in ['', 'S12-25_GML/']:
            b=io.BytesIO()
            with zipfile.ZipFile(b,'w') as z:
                z.writestr(prefix+'UTF-8/stations.geojson',payload)
                z.writestr(prefix+'Shift-JIS/stations.geojson',payload.decode().encode('cp932'))
            idx,meta=parse_edition(b.getvalue())
            self.assertEqual(candidates(idx,('駅A','会社A','線A'),2022)['value'],622)
            self.assertEqual(candidates(idx,('駅A','会社A','線A'),2023)['value'],664)
            self.assertNotIn(2024,meta['statistical_year_fields'])

    def test_ambiguity_and_missing_keys_do_not_become_matches(self):
        key=('駅A','会社A','線A')
        idx={key:[{'S12_053':1,'S12_051':1},{'S12_053':2,'S12_051':1}]}
        self.assertTrue(candidates(idx,key,2022)['ambiguous'])
        self.assertIsNone(candidates(idx,key,2022)['value'])
        self.assertIsNone(candidates(idx,('不明','',''),2022)['value'])


if __name__=='__main__':unittest.main()
