"""Compare weekly infection figures with each series' own dated official CSV.

No medical interpretation. Monthly/seasonal aggregates require a separate method
and are explicitly outside this adapter. Sentinel rates are never summed.
"""
import csv
import io
import json
import re
import unicodedata
from .checks import numeric


def norm(s):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(s)))


def csv_table(raw, disease, year, week):
    text = None
    for enc in ('utf-8-sig','cp932'):
        try:text=raw.decode(enc);break
        except UnicodeDecodeError:continue
    if text is None:raise ValueError('Unsupported official CSV encoding')
    rows=list(csv.reader(io.StringIO(text, newline="")))
    if not re.search(str(year)+r'年\s*0?'+str(week)+r'週', '\n'.join(','.join(r) for r in rows[:3])):
        raise ValueError('Official CSV header does not match the requested year/week')
    column = None
    header = None
    for i,row in enumerate(rows[:8]):
        for j,cell in enumerate(row):
            if norm(cell)==norm(disease):column=j;header=i;break
        if column is not None:break
    if column is None:raise ValueError('Disease not found in official CSV header: '+disease)
    metrics = rows[header+1]
    cols=[]
    for j in range(column,min(column+2,len(metrics))):
        name=norm(metrics[j])
        if '定当' in name or '定点当' in name:cols.append(('rate',j))
        elif name in ['報告','報告数','報告数(当該週)','当該週']:cols.append(('count',j))
    if not cols:raise ValueError('Official metric headings not recognized')
    result={}
    for row in rows[header+2:]:
        if not row or not row[0].strip():continue
        result[norm(row[0])] = {kind:row[j].strip() if j<len(row) else '' for kind,j in cols}
    return result


def compare(client, catalog, series):
    result={'status':'not_checked','scope':'weekly records only; each series uses its own dated official CSV',
       'series_expected':0,'series_compared':0,'numeric_values_compared':0,'numeric_mismatches':0,
       'missing_or_unmatched':0,'non_weekly_excluded':0,'official_non_numeric':0,
       'non_numeric_mapped_to_zero_review':0,'examples':[],'errors':[]}
    lookup={}
    for key,meta in catalog.get('seriesIndex',{}).items():
        # Disease names may themselves contain hyphens; only split the first two.
        pieces=key.split('-',2)
        if len(pieces)==3:lookup[meta.get('path')]=(int(pieces[0]),pieces[2])
    for url,data in series:
        if data.get('periodType')!='week' or len(data.get('weeks',[]))!=1:
            result['non_weekly_excluded']+=1;continue
        result['series_expected']+=1
        try:
            entry=next((v for p,v in lookup.items() if p and url.split('?',1)[0].endswith(p)),None)
            if entry is None:raise ValueError('Series not matched to the published disease index')
            year,disease=entry;week=data['weeks'][0]
            sources=[u for u in data.get('sources',[]) if u.lower().endswith('.csv')]
            if len(sources)!=1:raise ValueError('Expected exactly one weekly official CSV source')
            raw,rec=client.get(sources[0],'official_infection_csv')
            if raw is None:raise ValueError(rec.get('error','Source fetch failed'))
            table=csv_table(raw,disease,year,week)
            result['series_compared']+=1
            for code,values in data.get('prefs',{}).items():
                name='総数' if code=='00' else catalog.get('prefs',{}).get(code)
                official=table.get(norm(name))
                if official is None:result['missing_or_unmatched']+=1;continue
                for field,token in official.items():
                    # Some notifiable datasets use cases; do not compare a rate to a count.
                    field_map='cases' if field=='count' and 'cases' in values else field
                    if field_map not in values:continue
                    actual=values[field_map]
                    if not re.fullmatch(r'\d+(?:\.\d+)?',token):
                        result['official_non_numeric']+=1
                        if actual==0:result['non_numeric_mapped_to_zero_review']+=1
                        continue
                    if not numeric(actual):result['missing_or_unmatched']+=1;continue
                    result['numeric_values_compared']+=1
                    if abs(float(token)-actual)>.000001:
                        result['numeric_mismatches']+=1
                        if len(result['examples'])<30:result['examples'].append({'series':url,'code':code,'field':field,'map':actual,'official':token})
        except Exception as e:
            result['errors'].append({'series':url,'reason':str(e)[:500]})
    if result['series_expected'] and result['series_compared']==result['series_expected'] and result['numeric_values_compared'] and not result['numeric_mismatches'] and not result['missing_or_unmatched']:
        result['status']='weekly_numeric_cells_matched_non_numeric_and_aggregates_pending'
    else:result['status']='incomplete_or_mismatch'
    return result
