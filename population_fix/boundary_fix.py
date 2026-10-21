"""Add official 2025 Hamamatsu boundaries to an unpublished candidate copy.

The legacy shared geometry is never replaced. No statistical values are modified.
Run: python -m population_fix.boundary_fix INPUT OUTPUT OFFICIAL_ZIP GEO_DIR
"""
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile
from .render import read_const_json, replace_const_json

OLD = {str(c) for c in range(22131, 22138)}
NEW = {'22138', '22139', '22140'}
YEARS = ('2019', '2021', '2025')
SOURCE = 'https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-N03-2025.html'
NOTES = {
 'ja': '浜松市は2019年・2021年を旧7区、2025年を新3区で表示します。区の範囲が異なるため、区別の人数をそのまま年比較することはできません。境界出典：',
 'en': 'Hamamatsu uses the former seven wards for 2019/2021 and the new three wards for 2025. Ward boundaries differ, so ward totals are not directly comparable across these years. Boundary source: ',
 'de': 'Hamamatsu wird 2019/2021 mit den früheren sieben und 2025 mit den neuen drei Bezirken dargestellt. Wegen der geänderten Grenzen sind die Bezirkszahlen nicht direkt über die Jahre vergleichbar. Grenzquelle: ',
 'es': 'Hamamatsu muestra los siete distritos anteriores en 2019/2021 y los tres nuevos en 2025. Sus límites son distintos, por lo que las cifras por distrito no son directamente comparables entre esos años. Fuente de límites: ',
 'fr': 'Hamamatsu affiche les sept anciens arrondissements en 2019/2021 et les trois nouveaux en 2025. Leurs limites diffèrent : les effectifs par arrondissement ne sont donc pas directement comparables entre ces années. Source des limites : ',
}

def once(s, old, new):
    if s.count(old) != 1:
        raise ValueError('Expected exactly one occurrence: ' + old[:100])
    return s.replace(old, new)


def official_geometry(path):
    if path.suffix == '.gz':
        data = json.loads(gzip.decompress(path.read_bytes()))
    else:
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read('N03-20250101_22.geojson'))
    features = []
    for code in sorted(NEW):
        source = [f for f in data['features'] if f['properties']['N03_007'] == code]
        if not source:
            raise ValueError('Missing official ward ' + code)
        polygons = []
        for f in source:
            g = f['geometry']
            if g['type'] == 'Polygon':
                polygons.append(g['coordinates'])
            elif g['type'] == 'MultiPolygon':
                polygons.extend(g['coordinates'])
            else:
                raise ValueError('Unsupported official geometry')
        # Adapt N03 2025's separate city/ward fields to the legacy renderer.
        p = dict(source[0]['properties'])
        p['N03_003'], p['N03_004'] = p['N03_004'], p['N03_005']
        features.append({'type': 'Feature', 'properties': p,
                         'geometry': {'type': 'MultiPolygon', 'coordinates': polygons}})
    return {'type': 'FeatureCollection', 'features': features}


DRAW = '''let populationBaseGeometry=null,populationNewWards=null;
function redrawPopulationGeometry(){
  if(!populationBaseGeometry)return;
  const previousCode=selectedCode;
  if(geojsonLayer)map.removeLayer(geojsonLayer);
  selectedLayer=null;selectedCode=null;selectedName=null;
  const features=populationBaseGeometry.features.filter(f=>currentYear!=='2025'||!['22131','22132','22133','22134','22135','22136','22137'].includes(f.properties.N03_007));
  if(currentYear==='2025')features.push(...populationNewWards.features);
  geojsonLayer=L.geoJSON({type:'FeatureCollection',features},{
    style:f=>style(f,false),
    onEachFeature:(f,layer)=>{
      const p=f.properties,code=p.N03_007;
      const name=(p.N03_001||'')+(p.N03_003||'')+(p.N03_004||'');
      layer.bindTooltip(name,{sticky:true});
      layer.on('click',()=>selectCity(code,name,layer));
    }
  }).addTo(map);
  if(previousCode){
    geojsonLayer.eachLayer(layer=>{
      if(layer.feature.properties.N03_007===previousCode){
        const p=layer.feature.properties;
        selectCity(previousCode,(p.N03_001||'')+(p.N03_003||'')+(p.N03_004||''),layer);
      }
    });
    if(!selectedCode)document.getElementById('detailPanel').innerHTML=populationEmptyPanel;
  }
}
const populationEmptyPanel=document.getElementById('detailPanel').innerHTML;
Promise.all([
  vwdLoadPopulationGeometry('/maps/shared/chubu_hokuriku.geojson','/maps/shared/chubu_hokuriku-20261004-loading1.geojson.gz'),
  vwdLoadPopulationGeometry('/maps/population/geometry/hamamatsu-2025.geojson','/maps/population/geometry/hamamatsu-2025.geojson.gz')
]).then(([geo,wards])=>{
    populationBaseGeometry=geo;populationNewWards=wards;
    redrawPopulationGeometry();
'''


def build(src, dest, official, geo_dir):
    if dest.exists():
        raise ValueError('Output must be a new directory')
    shutil.copytree(src, dest)
    root = dest / 'site/maps/population'
    geometry = official_geometry(official)
    outgeo = root / 'geometry'
    outgeo.mkdir()
    (outgeo/'SOURCE.txt').write_text(
        'MLIT National Land Numerical Information N03 (2025-01-01)\n'
        + SOURCE + '\nCC BY 4.0. Hamamatsu wards extracted; coordinates unchanged. '
        'Polygon parts grouped by ward; name fields adapted for the legacy renderer.\n'
        'Original ZIP SHA256: abf0672d126c44923e1d69e99cf0d6688707db3ddf55a08e09ccbff32b806ed4\n')
    raw = json.dumps(geometry, ensure_ascii=False, separators=(',', ':')).encode()
    (outgeo/'hamamatsu-2025.geojson').write_bytes(raw)
    (outgeo/'hamamatsu-2025.geojson.gz').write_bytes(gzip.compress(raw, mtime=0))
    # Existing polygons cover all other statistical municipalities.
    polygons = set()
    for p in geo_dir.glob('*.geojson'):
        polygons.update(f['properties']['N03_007'] for f in json.loads(p.read_text())['features'])
    index = {}
    statistical_codes = {y: set() for y in YEARS}
    oldindex = read_const_json((root/'ja/national.html').read_text(), 'SEARCH_INDEX')
    regions = {c['code'][:2]: (c['region'], c['slug']) for c in oldindex}
    coverage = {y: set() for y in YEARS}
    for p in (root/'ja').glob('*.html'):
        if p.stem == 'national':
            continue
        pop = read_const_json(p.read_text(), 'POP_DATA')
        for y, dirs in pop.items():
            for code, record in dirs['inflow'].items():
                statistical_codes[y].add(code)
                allowed = (polygons - OLD | NEW) if y == '2025' else polygons
                if code not in allowed:
                    continue
                coverage[y].add(code)
                region, slug = regions[code[:2]]
                entry = index.setdefault(code, dict(code=code,name=record['name'],region=region,slug=slug,years=[]))
                if y not in entry['years']:
                    entry['years'].append(y)
    entries = [index[c] for c in sorted(index)]
    patched = 0
    for p in root.glob('*/*.html'):
        s = p.read_text()
        s = replace_const_json(s, 'SEARCH_INDEX', entries)
        s = once(s, 'let currentYear="2025",', 'let currentYear=(["2019","2021","2025"].includes(new URLSearchParams(location.search).get("year"))?new URLSearchParams(location.search).get("year"):"2025"),')
        s = once(s, 'SEARCH_INDEX.filter(c=>', 'SEARCH_INDEX.filter(c=>c.years.includes(currentYear)&&')
        s = s.replace("+'.html?city='+el.dataset.code", "+'.html?city='+el.dataset.code+'&year='+currentYear")
        # National ranking links also preserve the selected statistical year.
        s = s.replace("+'.html?city='+r.code+'\"", "+'.html?city='+r.code+'&year='+currentYear+'\"")
        s = s.replace("+'.html?city='+r.code}", "+'.html?city='+r.code+'&year='+currentYear}")
        if p.stem == 'national':
            # Initial prefecture cards must use URL-selected year as well.
            s = s.replace('let PREF_DATA=PREF_ALL["2025"];', 'let PREF_DATA=PREF_ALL[(["2019","2021","2025"].includes(new URLSearchParams(location.search).get("year"))?new URLSearchParams(location.search).get("year"):"2025")];')
        else:
            s = s.replace('POP_DATA["2025"]&&POP_DATA["2025"]["inflow"]&&POP_DATA["2025"]["inflow"][code]', 'POP_DATA[currentYear]&&POP_DATA[currentYear]["inflow"]&&POP_DATA[currentYear]["inflow"][code]')
            if p.stem == 'chubu_hokuriku':
                translated = p.parent.name != 'ja'
                start = s.index("I18n.ready.then(()=>fetch('/maps/shared/chubu_hokuriku.geojson')") if translated else s.index("vwdLoadPopulationGeometry('/maps/shared/chubu_hokuriku.geojson'")
                end = s.index('    const params=new URLSearchParams(window.location.search);', start)
                draw = DRAW
                if translated:
                    ja = (src/'site/maps/population/ja/chubu_hokuriku.html').read_text()
                    helper = ja[ja.index('function vwdLoadPopulationGeometry'):ja.index('function vwdPopulationReady')]
                    draw = helper + draw.replace(".geojson.gz')\n]).then", ".geojson.gz'), I18n.ready\n]).then")
                    draw = draw.replace('layer.bindTooltip(name,', 'layer.bindTooltip(PlaceNames.text(code,name),')
                s = s[:start] + draw + s[end:]
                if translated:
                    close = s.index('  }));', start)
                    s = s[:close] + s[close:].replace('  }));', '  });', 1)
                s = once(s, '    currentYear=y;', '    currentYear=y;\n    redrawPopulationGeometry();')
                marker = '<div class="years" id="yearButtons"></div>'
                note = '<p style="font-size:12px;line-height:1.7">'+NOTES[p.parent.name]+'<a href="'+SOURCE+'" target="_blank" rel="noopener">国土数値情報 N03 (2025)</a></p>'
                s = once(s, marker, note+'\n    '+marker)
            else:
                s = once(s, '    currentYear=y;', '    currentYear=y;\n    if(geojsonLayer)geojsonLayer.setStyle(f=>style(f,f.properties.N03_007===selectedCode));')
        s = s.replace('    currentYear=y;', "    currentYear=y;\n    document.getElementById('suggestions').style.display='none';")
        p.write_text(s)
        patched += 1
    report = {'source': SOURCE, 'source_sha256': hashlib.sha256(official.read_bytes()).hexdigest(),
              'new_ward_codes': sorted(NEW), 'pages_patched': patched,
              'search_coverage': {y: len(c) for y,c in coverage.items()},
              'geometry_bytes': len(raw), 'compressed_geometry_bytes': (outgeo/'hamamatsu-2025.geojson.gz').stat().st_size,
              'statistics_unchanged': True}
    (dest/'hamamatsu_boundary_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    yearly = {y: ((polygons - OLD) | NEW if y == '2025' else polygons) for y in YEARS}
    boundary = {
        'polygons_available': bool(polygons),
        'geometry_policy': '2019/2021 legacy; 2025 legacy minus old7 plus official new3',
        'years': {y: {
            'data_codes': len(statistical_codes[y]),
            'without_current_polygon': len(statistical_codes[y] - yearly[y]),
            'without_current_polygon_examples': sorted(statistical_codes[y] - yearly[y]),
            'polygon_count': len(yearly[y]),
        } for y in YEARS},
        'polygons_without_2025_data': len(yearly['2025'] - statistical_codes['2025']),
        'polygons_without_2025_data_examples': sorted(yearly['2025'] - statistical_codes['2025']),
        'polygon_count': len(yearly['2025']),
    }
    (dest/'boundary_report.json').write_text(json.dumps(boundary, ensure_ascii=False, indent=2))
    (dest/'HAMAMATSU_BOUNDARIES.txt').write_text(NOTES['ja'] + SOURCE + '\n'
        '数値は修正済み候補から変更なし。共有境界と地名表示スクリプトは既存サーバー資産を使う差分パッケージ。\n'
        '年別対応の詳細は boundary_report.json。以前のレポートにある新3区の境界不足は、この追補で解消。\n')
    # Remove the inherited stale deployment archive; package only the new site.
    (dest/'deploy-candidate.zip').unlink(missing_ok=True)
    with zipfile.ZipFile(dest/'deploy-candidate.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted((dest/'site').rglob('*')):
            if p.is_file():
                z.write(p,p.relative_to(dest/'site'))
    return report

if __name__ == '__main__':
    print(json.dumps(build(*(Path(a) for a in sys.argv[1:])), ensure_ascii=False, indent=2))
