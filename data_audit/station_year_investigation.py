"""Investigate station edition/year confusion. Evidence only, never corrections."""
import collections
import io
import json
from pathlib import Path
import re
import zipfile
from .run import Client, now, write, SITE
from .official_night import fetch, load, number


def parse_edition(raw):
    z=zipfile.ZipFile(io.BytesIO(raw))
    names=[n for n in z.namelist() if n.lower().endswith('.geojson')]
    utf=[n for n in names if '/UTF-8/' in n]
    if utf:names=utf
    if len(names)!=1:raise ValueError('Official GeoJSON selection ambiguous')
    b=z.read(names[0])
    try:o=json.loads(b.decode('utf-8-sig'))
    except UnicodeDecodeError:o=json.loads(b.decode('cp932'))
    idx=collections.defaultdict(list);fields=set()
    for f in o['features']:
        p=f['properties'];fields.update(p)
        idx[(p['S12_001'],p['S12_002'],p['S12_003'])].append(p)
    available=[y for y in range(2011,2025) if f'S12_{9+4*(y-2011):03}' in fields]
    return idx,dict(rows=len(o['features']),unique_station_keys=len(idx),statistical_year_fields=available)


def candidates(index,key,year):
    field=f'S12_{9+4*(year-2011):03}'
    rows=index.get(key,[]);values={p[field] for p in rows if field in p}
    codes={p.get(f'S12_{7+4*(year-2011):03}') for p in rows if field in p}
    return dict(value=next(iter(values)) if len(values)==1 else None,
                values=sorted(values,key=str),availability_codes=sorted(codes,key=str),
                candidates=len(rows),ambiguous=len(values)>1)


def investigate(c):
    report=dict(started_at=now(),errors=[],editions={},datasets={},production_changed=False,
        field_definition='S12_053=2022, S12_057=2023, S12_061=2024; availability codes retained separately',
        limitations=['A matching value alone is not proof of the original extraction process.',
                    'Source edition, statistical year and publication date are distinct.',
                    'No-data codes and ambiguous station keys are not certified passenger observations.'])
    indices={}
    for edition in [23,24,25]:
        url=f'https://nlftp.mlit.go.jp/ksj/gml/data/S12/S12-{edition}/S12-{edition}_GML.zip'
        try:
            index,meta=parse_edition(fetch(c,url));indices[edition]=index
            report['editions'][str(edition)]=dict(url=url,**meta)
        except Exception as e:report['errors'].append(dict(scope=url,error=str(e)))
    for name,url,table in [
        ('map2023',SITE+'/maps/station/2023/all_stations_light_v2_2023.json',False),
        ('ranking_ja',SITE+'/cards/station-ja/assets/data/stations.json',True),
        ('ranking_multilingual',SITE+'/cards/station/assets/data/stations.json',True)]:
        try:
            data=load(c,url)
            rows=[dict(zip(data['meta']['fields'],r)) for r in data['rows']] if table else data
            counts=collections.Counter();exceptions=[];examples=[]
            for i,row in enumerate(rows):
                key=(row['name'],row['operator'],row['line']);actual=row.get('f23')
                evidence={}
                for edition,index in indices.items():
                    for year in [2021,2022,2023,2024]:
                        if year in report['editions'][str(edition)]['statistical_year_fields']:
                            e=candidates(index,key,year);label=f'S12-{edition}/statistical-{year}'
                            evidence[label]=e
                            if number(actual) is not None and not e['ambiguous'] and number(e['value']) is not None and actual==e['value']:
                                counts['raw_value_matches_'+label]+=1
                latest=evidence.get('S12-25/statistical-2023',{})
                if latest.get('availability_codes')!=[1]:counts['2023_unobserved_or_missing_or_ambiguous']+=1
                if number(actual) is None:counts['published_non_numeric']+=1
                elif latest.get('value') is not None and actual!=latest['value']:
                    counts['different_from_latest_2023']+=1
                    if evidence.get('S12-25/statistical-2022',{}).get('value')==actual:
                        counts['different_2023_but_matches_latest_2022']+=1
                        if len(examples)<12:examples.append(dict(station=key,published_f23=actual,official_2022=evidence.get('S12-25/statistical-2022'),official_2023=latest))
                    else:exceptions.append(dict(row=i,station=key,published_f23=actual,official=evidence))
            report['datasets'][name]=dict(url=url,rows=len(rows),counts=dict(counts),
                exceptions_to_latest_2022_explanation=exceptions,examples=examples)
        except Exception as e:report['errors'].append(dict(scope=name,error=str(e)))
    report['code_evidence']=json.loads(Path(__file__).with_name('station-year-code-evidence.json').read_text())
    # Archive the explanatory official page; extraction is evidence, not an instruction.
    page='https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-S12-2024.html'
    try:
        from bs4 import BeautifulSoup
        soup=BeautifulSoup(fetch(c,page),'html.parser')
        text=soup.get_text(' ',strip=True)
        snippets=[]
        for term in ['整備年度','統計年度','2023','2022']:
            pos=text.find(term)
            if pos>=0:snippets.append(dict(term=term,text=text[max(0,pos-100):pos+260]))
        report['official_documentation']=dict(url=page,snippets=snippets)
    except Exception as e:report['errors'].append(dict(scope=page,error=str(e)))
    report['finished_at']=now();report['status']='investigation_incomplete' if report['errors'] else 'evidence_collected_requires_review'
    return report


def main():
    out=Path('station-year-report');c=Client(out,delay=3,minutes=45)
    try:r=investigate(c)
    except Exception as e:r=dict(status='failed',errors=[str(e)],production_changed=False)
    write(out/'report.json',r);write(out/'fetch_manifest.json',c.records)
    lines=['# Station year investigation',f"Status: {r['status']}",'No production changes or automated correction.',
           'Full exceptions, original values, availability codes and source hashes are in report.json and fetch_manifest.json.']
    for name,d in r.get('datasets',{}).items():
        lines += ['',f'## {name}: {d["rows"]} rows',*[f'- {k}: {v}' for k,v in d['counts'].items()],
                  f'- Exceptions to the simple2022 explanation: {len(d["exceptions_to_latest_2022_explanation"])}']
    lines+=['','## Errors',json.dumps(r.get('errors',[]),ensure_ascii=False)]
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__=='__main__':main()
