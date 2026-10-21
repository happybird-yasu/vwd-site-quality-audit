"""Read-only, resumable-by-theme audit. All failures become explicit report entries."""
from __future__ import annotations
import argparse
from collections import Counter, deque
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlparse, urldefrag, urlencode
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup
from .checks import Checks, json_values, numeric

SITE = 'https://app-navi.biz'
THEMES = {
 'station': ('station-map', '駅'), 'station_ranking': ('station-passenger-ranking', '駅乗降客ランキング'),
 'port': ('port-passenger', '港'), 'airport': ('airport-passengers', '空港'),
 'onsen': ('onsen-map', '温泉'), 'schools': ('school-count-map', '学校'),
 'medical': ('medical-count-map', '医療機関'), 'road': ('road-traffic', '道路交通量'),
 'aging_rate': ('aging-rate-map', '高齢化率'), 'infection': ('infection-weekly', '感染症'),
 'minerals': ('mineral-map', '鉱物'), 'hotel_ryokan': ('hotel-ryokan', 'ホテル・旅館'),
}
UA = 'VWD-Data-Integrity-Audit/1.0 (read-only site owner quality check)'
EXT = re.compile(r'\.(?:json|geojson|csv|js|html)(?:\.gz)?(?:\?[^\s\'"<>]*)?$', re.I)
QUOTED = re.compile(r'''["']([^"'<>\s\r\n]+)["']''')
OFFICIAL = re.compile(r'https?://[^\s"\'<>]+')


def now(): return datetime.now(timezone.utc).isoformat()
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def theme_of(url):
    slug = urlparse(url).path.rstrip('/').split('/')[-1]
    for theme, (prefix, _) in THEMES.items():
        if slug == prefix or slug.startswith(prefix + '-'):
            return theme
    return 'population' if slug.startswith('population-') else 'unknown'


def public_url(url):
    p = urlparse(url)
    return p.scheme == 'https' and p.netloc == 'app-navi.biz' and not p.username and not any(
        x in p.path.lower() for x in ['/wp-admin', '/wp-json', '/wp-login', '/xmlrpc', '/.git']) and 'rest_route=' not in p.query


def official_url(url):
    p = urlparse(url)
    host = p.hostname or ''
    return p.scheme == 'https' and not p.username and (host.endswith('.go.jp') or host == 'e-stat.go.jp' or host.endswith('.e-stat.go.jp') or host.endswith('.gsj.jp'))


class Client:
    def __init__(self, out, delay=3.0, minutes=150):
        self.out = out
        self.out.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.deadline = time.monotonic() + minutes * 60
        self.last = 0
        self.session = requests.Session()
        self.session.headers['User-Agent'] = UA
        self.session.max_redirects = 4
        self.records = []
        self.cache = {}
        self.robots = {}
        self.bytes = 0

    def raw(self, url, limit=25*1024*1024):
        if time.monotonic() >= self.deadline:
            raise TimeoutError('audit time budget exhausted')
        last = None
        for attempt in range(2):
            wait = self.delay - (time.monotonic() - self.last)
            if wait > 0: time.sleep(wait)
            started = time.monotonic()
            self.last = started
            try:
                with self.session.get(url, timeout=(10, 15), stream=True) as r:
                    r.raise_for_status()
                    if not (public_url(r.url) or official_url(r.url)):
                        raise ValueError('redirect outside allowed public/official hosts')
                    chunks = []
                    size = 0
                    for part in r.iter_content(65536):
                        size += len(part)
                        if size > limit: raise ValueError('response exceeds byte limit')
                        if time.monotonic() - started > 60: raise TimeoutError('response wall-clock deadline')
                        chunks.append(part)
                    return b''.join(chunks), r.url, r.headers.get('Content-Type', '')
            except Exception as e:
                last = e
        raise last

    def allowed(self, url):
        p = urlparse(url)
        origin = p.scheme + '://' + p.netloc
        if origin not in self.robots:
            robots_url = origin + '/robots.txt'
            parser = RobotFileParser()
            try:
                raw, _, _ = self.raw(robots_url, 1024*1024)
                parser.parse(raw.decode('utf-8-sig').splitlines())
                self.robots[origin] = parser
            except requests.HTTPError as e:
                # 404/410 means no published robots restrictions; other errors stay unverified.
                if e.response is not None and e.response.status_code in [404, 410]:
                    parser.parse([]);self.robots[origin] = parser
                else:
                    self.robots[origin] = None
            except Exception:
                self.robots[origin] = None
        parser = self.robots[origin]
        return parser is not None and parser.can_fetch(UA, url)

    def get(self, url, kind='site'):
        if url in self.cache: return self.cache[url]
        rec = {'url':url, 'kind':kind, 'checked_at':now()}
        self.records.append(rec)
        try:
            if not (public_url(url) or official_url(url)):
                raise ValueError('not an allowed public URL')
            if not self.allowed(url):
                rec.update(status='unchecked', error='robots denied or robots unavailable')
                self.cache[url] = (None, rec)
                return None, rec
            if self.bytes > 2*1024**3: raise ValueError('theme total byte budget exhausted')
            raw, final, ctype = self.raw(url)
            self.bytes += len(raw)
            digest = hashlib.sha256(raw).hexdigest()
            path = self.out/'responses'/(digest+'.bin.gz')
            path.parent.mkdir(exist_ok=True)
            if not path.exists(): path.write_bytes(gzip.compress(raw, mtime=0))
            rec.update(status='fetched', final_url=final, content_type=ctype, bytes=len(raw), sha256=digest, file=str(path.relative_to(self.out)))
            self.cache[url] = (raw,rec)
        except Exception as e:
            rec.update(status='error', error=(type(e).__name__+': '+str(e))[:600])
            self.cache[url] = (None,rec)
        return self.cache[url]


def sitemap(client, url, seen=None, depth=0):
    seen = seen or set()
    if depth > 5 or url in seen or not public_url(url): return []
    seen.add(url)
    raw, _ = client.get(url, 'sitemap')
    if raw is None: return []
    root = ET.fromstring(raw)
    locs = [e.text.strip() for e in root.iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc') if e.text]
    if root.tag.endswith('sitemapindex'):
        urls = []
        for u in locs: urls.extend(sitemap(client,u,seen,depth+1))
        return urls
    return [u for u in locs if public_url(u)]


def discover(out, delay):
    c = Client(out, delay, 20)
    urls = sitemap(c, SITE+'/post-sitemap.xml')
    if not urls:
        write(out/'fetch_manifest.json',c.records)
        raise ValueError('No sitemap URLs discovered; inventory cannot be called complete')
    groups = {t:[] for t in THEMES}
    excluded, unknown = [], []
    for u in sorted(set(urls)):
        t = theme_of(u)
        if t in groups:groups[t].append(u)
        elif t == 'population':excluded.append(u)
        else:unknown.append(u)
    result = {'generated_at':now(), 'groups':groups, 'counts':{t:len(v) for t,v in groups.items()}, 'excluded_population':excluded, 'unknown_urls':unknown, 'sitemap_articles':len(set(urls))}
    write(out/'targets.json',result);write(out/'fetch_manifest.json',c.records)
    print(json.dumps({'counts':result['counts'],'unknown':unknown,'total':result['sitemap_articles']},ensure_ascii=False),flush=True)


def text_of(raw):
    if raw[:2] == b'\x1f\x8b':
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:raw=f.read(30*1024*1024+1)
        if len(raw)>30*1024*1024:raise ValueError('expanded gzip limit')
    return raw.decode('utf-8-sig')


def references(text, base, html=False):
    refs = set()
    consumed = set()
    if html:
        soup = BeautifulSoup(text,'html.parser')
        for node in soup.select('iframe,script[src]'):
            val = node.get('src') or node.get('data-src')
            if val:refs.add(urljoin(base,val))
        for a in soup.select('a[href]'):
            u=urljoin(base,a['href'])
            if public_url(u) and urlparse(u).path.startswith(('/maps/','/cards/')) and EXT.search(u):refs.add(u)
    constants = dict(re.findall(r'''(?:const|let|var)\s+(\w+)\s*=\s*["']([^"'\r\n]+)["']''',text))
    # Resolve concrete fetch(ROOT + "file.json") expressions without executing code.
    for m in re.finditer(r'''\b(?:fetch|loadScript|getJSON)\(\s*(\w+)\s*\+\s*["']([^"'\r\n]+)["']''',text):
        if m[1] in constants:
            refs.add(urljoin(base, constants[m[1]]+m[2]));consumed.add(m[2])
    for m in QUOTED.finditer(text):
        v=m[1]
        if v in consumed or not EXT.search(v) or any(x in v for x in ['${','{','}', '<', '=>', ' ']):continue
        refs.add(urljoin(base,v))
    # Computed paths are explicit remaining work, rather than guessed requests.
    dynamic = sorted(set(m[1] for m in re.finditer(r'`([^`]*\$\{[^`]+\.(?:json|csv|geojson)[^`]*)`',text)))
    urls = set()
    for u in refs:
        u=urldefrag(u)[0]
        if public_url(u) and urlparse(u).path.startswith(('/maps/','/cards/')):
            path=urlparse(u).path.lower()
            if not any(x in path for x in ['.min.js','/vendor/','/locales/','/i18n/']): urls.add(u)
    sources = {u.rstrip('.,);') for u in OFFICIAL.findall(text) if official_url(u.rstrip('.,);'))}
    return sorted(urls), sources, dynamic


def aging_official(c, data, checks):
    result = {'scope':'three age counts for every published municipality/year; rates checked separately', 'years':{}, 'status':'not_checked'}
    remap={e['newCode']:e['oldCode'] for e in data.get('meta',{}).get('codeRemapping2015',{}).get('entries',[])}
    for year in [2015,2020,2025]:
        rec = {'compared':0,'expected':0,'missing':0,'mismatches':0,'non_numeric_official':0,'examples':[]}
        result['years'][str(year)] = rec
        query={'IndicatorCode':'0201010010000010010,0201010010000010020,0201010010000010030', 'Time':str(year)+'CY00', 'Cycle':'3','RegionalRank':'4','IsSeasonalAdjustment':'1','MetaGetFlg':'Y','SectionHeaderFlg':'1'}
        u='https://dashboard.e-stat.go.jp/api/1.0/Json/getData?'+urlencode(query)
        raw, fetch = c.get(u,'official_age_counts')
        if raw is None:
            rec.update(status='not_checked', error=fetch.get('error'));continue
        try:
            response=json.loads(text_of(raw))
            objs=response['GET_STATS']['STATISTICAL_DATA']['DATA_INF']['DATA_OBJ']
            if isinstance(objs,dict):objs=[objs]
            values={}
            for item in objs:
                v=item['VALUE']
                if v.get('@time') != str(year)+'CY00':continue
                values[(str(v['@regionCode']),v['@indicator'])]=v.get('$')
            fields=[('0201010010000010010','under15'),('0201010010000010020','working'),('0201010010000010030','over65')]
            for row in data['rows']:
                stats=row.get('values',{}).get(str(year))
                if not stats:continue
                code=remap.get(row['code'],row['code']) if year==2015 else row['code']
                for ind,field in fields:
                    if not numeric(stats.get(field)):continue
                    rec['expected']+=1
                    value=values.get((code,ind))
                    if value is None:rec['missing']+=1;continue
                    if not re.fullmatch(r'\d+',str(value)):rec['non_numeric_official']+=1;continue
                    rec['compared']+=1
                    if stats[field]!=int(value):
                        rec['mismatches']+=1
                        if len(rec['examples'])<25:rec['examples'].append({'code':row['code'],'field':field,'map':stats[field],'official':value})
            rec['status']='matched' if rec['expected']>0 and rec['compared']==rec['expected'] and rec['mismatches']==0 else 'mismatch_or_incomplete'
        except Exception as e:rec.update(status='error',error=str(e)[:500])
    result['status']='matched' if all(r.get('status')=='matched' for r in result['years'].values()) else 'incomplete_or_mismatch'
    return result


def audit(theme, targets, out, delay, minutes, max_assets):
    c=Client(out,delay,minutes);checks=Checks()
    urls=targets.get('groups',{}).get(theme,[])
    q=deque((u,0) for u in urls)
    seen=set();scheduled=set(urls);sources=set();dynamic=[];objects=0;age_data=None;infection_catalog=None;infection_series=[]
    errors=[];page_count=0;expected=len(urls)
    def checkpoint():
        write(out/'fetch_manifest.json',c.records)
        write(out/'checks.json',checks.result())
        write(out/'progress.json',{'theme':theme,'seen':len(seen),'queued':len(q),'generated_at':now(),'article_expected':expected,'article_fetched':page_count})
    while q and len(seen)<max_assets and time.monotonic()<c.deadline-min(20*60,minutes*60*.15):
        u,depth=q.popleft()
        if u in seen:continue
        seen.add(u)
        raw,rec=c.get(u,'article' if u in urls else 'site_asset')
        if raw is None:
            errors.append({'url':u,'reason':rec.get('error')});continue
        if u in urls:page_count+=1
        try:
            text=text_of(raw)
            content_type=rec.get('content_type','').lower()
            path=urlparse(u).path.lower()
            html=('text/html' in content_type or path.endswith('.html') or text.lstrip().startswith(('<!DOCTYPE','<!doctype','<html')))
            is_json=path.endswith(('.json','.geojson','.json.gz','.geojson.gz'))
            values,duplicates=json_values(text,is_json=is_json)
            for key in duplicates:checks.add('error','duplicate_json_key',u,key=key)
            if is_json and not values:checks.add('error','empty_json',u)
            for name,obj in values:
                objects+=1;checks.inspect(obj,u+'#'+name,theme)
                if name=='AGING_DATA' and isinstance(obj,dict):age_data=obj
                if name=='INFECTION_DATA' and isinstance(obj,dict):infection_catalog=obj
                if theme=='infection' and isinstance(obj,dict) and 'periodType' in obj and 'prefs' in obj:infection_series.append((u,obj))
            rec['parsed_data_objects']=len(values)
            refs,src,computed=references(text,u,html)
            sources.update(src)
            if computed:dynamic.append({'url':u,'expressions':computed[:25]})
            if depth<5:
                for ref in refs:
                    if ref not in scheduled:
                        scheduled.add(ref);q.append((ref,depth+1))
            elif refs:checks.add('unchecked','dependency_depth_limit',u,unfollowed=len(refs))
        except Exception as e:
            errors.append({'url':u,'reason':type(e).__name__+': '+str(e)[:500]})
            checks.add('error','parse_or_check_failed',u,error=str(e)[:500])
        if len(seen)%20==0:
            checkpoint();print(theme,'processed',len(seen),'queued',len(q),'errors',len(errors),flush=True)
    if q:checks.add('unchecked','crawl_budget_or_asset_limit',theme,remaining=len(q))
    if not urls:checks.add('unchecked','no_articles_discovered',theme)
    if objects==0:checks.add('unchecked','no_parseable_data_objects',theme)
    official={'status':'not_implemented_for_this_theme','note':'Source availability is not numerical verification. Full official-value comparison remains required.'}
    if theme=='aging_rate' and age_data:
        try:official=aging_official(c,age_data,checks)
        except Exception as e:official={'status':'error','error':str(e)[:500]}
    if theme=='infection' and infection_catalog:
        try:
            from .infection import compare
            official=compare(c,infection_catalog,infection_series)
        except Exception as e:official={'status':'error','error':str(e)[:500]}
    source_results=[]
    for u in sorted(sources)[:12]:
        if time.monotonic()>=c.deadline:break
        raw,r=c.get(u,'official_source_link')
        source_results.append({'url':u,'status':r['status'],'sha256':r.get('sha256'),'error':r.get('error')})
    checkpoint()
    complete=bool(urls) and page_count==expected and not q and not errors
    status='issues_found' if checks.counts['finding_error'] or errors else ('partial_checks' if not complete else 'checks_completed_official_scope_incomplete')
    result={'theme':theme,'label':THEMES[theme][1],'generated_at':now(),'status':status,
        'article_expected':expected,'article_fetched':page_count,'unique_resources_attempted':len(seen),
        'parseable_data_objects':objects,'crawl_complete':complete,'remaining_queue':len(q),
        'checks':checks.result(),'errors':errors,'official_comparison':official,
        'official_source_urls':sorted(sources),'source_fetch_checks':source_results,
        'computed_data_paths_not_executed':dynamic,
        'limitations':['Static GET and data checks only; no browser rendering or hidden dynamic requests executed.',
          'Internal consistency, source-link availability and file integrity do not prove official numerical accuracy.',
          'Missing, suppressed and nonnumeric cells were not converted to zero.',
          'Full historical-boundary, unit, aggregation and original-source coverage remain pending unless explicitly verified.']}
    write(out/'report.json',result)
    lines=['# '+THEMES[theme][1]+' データ確認', '', '生成: '+now(), '',
       '**公式値と全件一致したという判定ではありません。**', '',
       '- 状態: '+status, '- 記事: '+str(page_count)+' / '+str(expected),
       '- 取得を試みたリソース: '+str(len(seen)), '- データオブジェクト: '+str(objects),
       '- エラー・取得失敗: '+str(len(errors)), '- 内部検査: '+json.dumps(dict(checks.counts),ensure_ascii=False),
       '- 公式数値照合: '+official['status'], '- 未処理キュー: '+str(len(q)), '',
       '## 確認範囲と残件', '', *['- '+x for x in result['limitations']], '',
       '詳細: report.json / checks.json / fetch_manifest.json。原本応答は responses/ にSHA256付きで保存。',
       'エラーは記録して継続。本番・WordPressへの書込みは行っていません。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ['theme','status','article_fetched','unique_resources_attempted','official_comparison']},ensure_ascii=False),flush=True)


def merge(inputs,out,targets):
    reports={}
    for p in sorted(inputs.rglob('report.json')):
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
            if d.get('theme') in THEMES:reports[d['theme']]=d
        except Exception:continue
    expected=targets.get('groups',{})
    missing=[t for t in THEMES if t not in reports]
    result={'generated_at':now(),'full_official_verification':False,'themes':reports,
            'missing_theme_reports':missing,'unknown_sitemap_urls':targets.get('unknown_urls',[]),
            'planned_articles':sum(len(v) for v in expected.values())}
    write(out/'report.json',result)
    lines=['# 地図データ 夜間チェック結果', '', '生成: '+now(), '',
       '**取得・内部整合の検査と、公式資料との数値照合を区別しています。未確認を「問題なし」とは判定しません。**', '',
       '| テーマ | 記事取得 | 取得/解析エラー | 内部不整合 | 未処理 | 公式数値照合 |',
       '|---|---:|---:|---:|---:|---|']
    for theme,(_,label) in THEMES.items():
        d=reports.get(theme)
        if not d:
            lines.append('| '+label+' | 未実行/結果なし | — | — | — | 未確認 |');continue
        lines.append('| '+label+' | '+str(d['article_fetched'])+'/'+str(d['article_expected'])+' | '+str(len(d['errors']))+' | '+str(d['checks']['counts'].get('finding_error',0))+' | '+str(d['remaining_queue'])+' | '+d['official_comparison']['status']+' |')
    lines+=['','## 朝に見る順番','','1. 各テーマの `errors` と `checks.findings`（取得・構造・計算の問題）。',
       '2. 公式値比較の不一致と欠測。内部整合のみ確認したテーマは別途公式照合が必要。',
       '3. 動的パス・時間上限・欠落テーマなどの未確認範囲。','',
       '対象追加候補: '+str(targets.get('unknown_urls',[])),
       '本番・WordPressは変更していません。取得失敗・テーマ単位の失敗があっても、他テーマと集計を継続します。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')


def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['discover','audit','merge']);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--theme',choices=sorted(THEMES));p.add_argument('--targets',type=Path);p.add_argument('--inputs',type=Path)
    p.add_argument('--delay',type=float,default=3);p.add_argument('--minutes',type=float,default=150);p.add_argument('--max-assets',type=int,default=1800)
    a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
    if a.mode=='discover':discover(a.out,a.delay)
    elif a.mode=='audit':audit(a.theme,json.loads(a.targets.read_text(encoding='utf-8')),a.out,a.delay,a.minutes,a.max_assets)
    else:merge(a.inputs,a.out,json.loads(a.targets.read_text(encoding='utf-8')))

if __name__=='__main__':main()
