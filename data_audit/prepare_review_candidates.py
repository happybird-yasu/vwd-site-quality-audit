"""Prepare local review files from public GETs; no deployment capability."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
from .run import Client,write,SITE,now
from .official_night import fetch,load,airport_rows,compact,PREFECTURES
from .official_extra import lodging_facilities


def prepare(out):
    c=Client(out/'evidence',delay=3,minutes=20);changes=[];checks=[]
    hotel_url='https://www.e-stat.go.jp/stat-search/file-download?statInfId=000040359176&fileKind=1'
    hotel=lodging_facilities(fetch(c,hotel_url),2024)
    new_rooms=hotel['宮崎県'][1]
    if new_rooms is None:raise ValueError('Miyazaki official room count unavailable')
    if sum(hotel[p][1] for p in PREFECTURES)!=hotel['全国'][1]:raise ValueError('Official national/prefecture room totals disagree')
    def change(path,key,field,row,new,source):
        old=row.get(field)
        if old!=new:
            changes.append(dict(file=path,key=key,field=field,old=old,new=new,source=source));row[field]=new
    for lang in ['ja','en','de','es','fr']:
        master_path=f'maps/lodging-trends/{lang}/data/prefecture_master.json'
        card_path=f'cards/lodging-trends/{lang}/data/regional_summary.json'
        master=copy.deepcopy(load(c,SITE+'/'+master_path));cards=copy.deepcopy(load(c,SITE+'/'+card_path))
        index={r['prefecture_code']:r for r in master}
        if len(index)!=47 or len(cards)!=10:raise ValueError('Unexpected lodging data coverage')
        row=index['45'];base=row['hotel_rooms_fy2019']
        change(master_path,'45','hotel_rooms_fy2024',row,int(new_rooms),hotel_url)
        change(master_path,'45','hotel_rooms_change_2024_2019',row,int(new_rooms-base),hotel_url)
        change(master_path,'45','hotel_rooms_index_fy2024',row,round(new_rooms/base*100,2),hotel_url)
        change(master_path,'45','hotel_rooms_change_rate_fy2024',row,round((new_rooms-base)/base,4),hotel_url)
        for code,r in index.items():
            if r['hotel_rooms_fy2024']!=hotel[PREFECTURES[int(code)-1]][1]:raise ValueError('Additional lodging source difference '+lang+'/'+code)
        for card in cards:
            total=sum(index[code]['hotel_rooms_fy2024'] for code in card['prefecture_codes'])
            change(card_path,card['region_slug'],'hotel_rooms_fy2024',card,total,hotel_url)
            change(card_path,card['region_slug'],'hotel_rooms_index_fy2024',card,round(total/card['hotel_rooms_fy2019']*100,4),hotel_url)
        national=next(r for r in cards if r['region_slug']=='national')
        if national['hotel_rooms_fy2024']!=hotel['全国'][1]:raise ValueError('Candidate national rooms do not match official national row')
        write(out/'candidate'/master_path,master);write(out/'candidate'/card_path,cards)
        checks.append(dict(language=lang,prefecture_room_values_matched=47,regional_totals_verified=10))
    airport_url='https://www.mlit.go.jp/koku/content/001905536.xlsx'
    reference=airport_rows(fetch(c,airport_url),include_kind=True)
    path='maps/airport/airports.json';airports=copy.deepcopy(load(c,SITE+'/'+path))
    for row in airports:
        if row.get('p2023') is None and 'heliport' in row.get('name_en','').lower():
            src=reference.get((compact(row['name']),2023,'heli'))
            if not src or any(not isinstance(v,(int,float)) for v in src.values()):raise ValueError('Heliport source unavailable '+row['name'])
            change(path,row['name'],'p2023',row,src,airport_url)
    count=0
    for row in airports:
        name='新石垣' if row['name']=='石垣' and row.get('iata')=='ISG' else row['name']
        kind='heli' if 'heliport' in row.get('name_en','').lower() else 'airport'
        for year in [2019,2021,2023,2024]:
            src=reference.get((compact(name),year,kind))
            if not src:raise ValueError('Airport source missing '+name)
            for field in ['intl','dom','total']:
                if (row.get('p'+str(year)) or {}).get(field)!=src[field]:raise ValueError('Candidate airport differs from official source '+name)
                count+=1
    write(out/'candidate'/path,airports);checks.append(dict(airport_values_verified=count))
    write(out/'evidence/fetch_manifest.json',c.records)
    files=[]
    for p in sorted((out/'candidate').rglob('*.json')):
        files.append(dict(path=str(p.relative_to(out/'candidate')),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    write(out/'change-manifest.json',dict(prepared_at=now(),production_changed=False,changes=changes,checks=checks,files=files,
        limitations=['Review files only; no server or WordPress writes.',
            'Validation covers declared room/airport fields, not every website datum.',
            'Station corrections and methodological caveats are separate pending work.']))
    print(json.dumps(dict(files=len(files),changed_fields=len(changes),checks=checks),ensure_ascii=False))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);prepare(p.parse_args().out)
