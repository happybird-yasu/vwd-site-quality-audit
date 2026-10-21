"""Boundary regression tests: use synthetic split wards, never remap statistics."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from .boundary_fix import official_geometry, NEW

class BoundaryTests(unittest.TestCase):
    def test_official_islands_and_ward_names_preserved(self):
        ring = [[137,34],[138,34],[138,35],[137,34]]
        source = []
        for code in sorted(NEW):
            source.append({'type':'Feature','properties':{'N03_001':'静岡県','N03_003':None,'N03_004':'浜松市','N03_005':code,'N03_007':code},'geometry':{'type':'Polygon','coordinates':[ring]}})
        source.append(source[0])  # Detached island remains part of same ward.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'official.zip'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('N03-20250101_22.geojson', json.dumps({'features':source}))
            out = official_geometry(p)['features']
        self.assertEqual({f['properties']['N03_007'] for f in out},NEW)
        self.assertEqual(len(out[0]['geometry']['coordinates']),2)
        self.assertEqual(out[0]['geometry']['coordinates'][0],[ring])
        self.assertEqual(out[0]['properties']['N03_003'],'浜松市')
        self.assertEqual(out[0]['properties']['N03_004'],'22138')

    def test_missing_official_ward_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'official.zip'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('N03-20250101_22.geojson','{"features":[]}')
            with self.assertRaises(ValueError):official_geometry(p)

if __name__=='__main__':unittest.main()
