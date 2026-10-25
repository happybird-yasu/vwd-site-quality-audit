import csv
import io
import json
import unittest
import zipfile
import openpyxl
from .official_extra import port_table, medical_table, mineral_index, lodging_facilities
from .official_night import PREFECTURES


def xlsx(rows):
    w=openpyxl.Workbook();s=w.active
    for row in rows:s.append(row)
    b=io.BytesIO();w.save(b);return b.getvalue()


class ExtraTests(unittest.TestCase):
    def test_single_route_port_starts_new_record(self):
        raw=xlsx([
            ['都道府県','港湾',None,None,'種別','計'],
            ['北海道',None,'網走',None,'計',140287],
            [None,None,None,None,'外',197],
            [None,None,None,None,'内',140090],
            [None,None,'稚内',None,'内',252831],
            [None,None,'釧路',None,'外',18830],
        ])
        r=port_table(raw)
        self.assertEqual(r['北海道','網走'],{'total':140287,'foreign':197,'domestic':140090})
        self.assertEqual(r['北海道','稚内'],{'domestic':252831})
        self.assertNotIn('total',r['北海道','稚内'])
        self.assertEqual(r['北海道','釧路'],{'foreign':18830})

    def test_medical_date_and_separate_counts(self):
        rows=[['令和元年12月末現在'],['施設数','病院','一般診療所','歯科診療所']]
        rows += [[None,None,p,11,999,22,888,33] for p in ['全国']+PREFECTURES]
        b=xlsx(rows)
        r=medical_table(b,2019)
        self.assertEqual(r['北海道'],{'hospital':11,'clinic':22,'dental':33})
        with self.assertRaisesRegex(ValueError,'year/month'):medical_table(b,2024)

    def test_duplicate_mineral_source_is_rejected(self):
        header=['id','mineral_j','mine_name','work_status','lat','lon','map_pub_year','map_uri']
        s=io.StringIO();w=csv.writer(s);w.writerow(header)
        for _ in range(2):w.writerow(['1','銅','','2','35','135','2017','https://www.gsj.jp/'])
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:z.writestr('source/mine_jp.csv',s.getvalue().encode('cp932'))
        with self.assertRaisesRegex(ValueError,'Duplicate'):mineral_index(b.getvalue())

    def test_fiscal_year_guard_and_suppression(self):
        s=io.StringIO();w=csv.writer(s);w.writerow(['2024年度 旅館・ホテル'])
        for p in ['全国']+PREFECTURES:w.writerow([p,100,200,'-',4,5,6])
        raw=s.getvalue().encode('cp932');r=lodging_facilities(raw,2024)
        self.assertIsNone(r['北海道'][2])
        with self.assertRaisesRegex(ValueError,'fiscal year'):lodging_facilities(raw,2021)


if __name__=='__main__':unittest.main()
