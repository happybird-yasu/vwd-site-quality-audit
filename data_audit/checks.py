"""Pure data checks. Missing/suppressed values are never coerced to zero."""
import json
import math
import re
from collections import Counter, defaultdict

ASSIGN = re.compile(r'(?:\b(?:const|let|var)\s+([\w$]+)|\bwindow\.([\w$]+))\s*=\s*')


def json_values(text, is_json=False):
    duplicates = []
    def pairs(items):
        out = {}
        for k, v in items:
            if k in out and len(duplicates) < 30:
                duplicates.append(k)
            out[k] = v
        return out
    decoder = json.JSONDecoder(object_pairs_hook=pairs)
    if is_json:
        return [('root', decoder.decode(text))], duplicates
    result = []
    end = -1
    for m in ASSIGN.finditer(text):
        if m.start() < end:
            continue
        start = m.end()
        if start >= len(text) or text[start] not in '{[':
            continue
        try:
            obj, n = decoder.raw_decode(text[start:])
        except ValueError:
            continue  # JavaScript object literal is not necessarily JSON.
        result.append((m[1] or m[2], obj))
        end = start + n
    return result, duplicates


def numeric(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Checks:
    def __init__(self):
        self.counts = Counter()
        self.findings = []

    def add(self, severity, kind, path, **detail):
        self.counts['finding_' + severity] += 1
        if len(self.findings) < 1500:
            self.findings.append(dict(severity=severity, kind=kind, path=path, **detail))

    def equal(self, path, name, total, parts, tolerance=1e-6):
        if not numeric(total) or not all(numeric(v) for v in parts):
            self.counts['identity_skipped_missing_or_non_numeric'] += 1
            return
        self.counts['identity_checks'] += 1
        if abs(total - sum(parts)) > tolerance:
            self.add('error', name, path, total=total, parts=parts, sum=sum(parts))

    def inspect(self, obj, source, theme):
        stack = [(source, obj)]
        visited = 0
        while stack:
            path, value = stack.pop()
            visited += 1
            if visited > 2000000:
                self.add('unchecked', 'object_scan_limit', source)
                break
            if isinstance(value, list):
                # GeoJSON vertex arrays are validated as a separate geometry walk.
                stack.extend((path + '/' + str(i), v) for i, v in enumerate(value))
            elif isinstance(value, dict):
                self.counts['objects'] += 1
                keys = set(value)
                if {'total', 'dom', 'intl'} <= keys:
                    self.equal(path, 'airport_total', value['total'], [value['dom'], value['intl']])
                if {'total', 'domestic', 'foreign'} <= keys:
                    self.equal(path, 'port_total', value['total'], [value['domestic'], value['foreign']])
                if {'total_springs', 'used_springs', 'unused_springs'} <= keys:
                    self.equal(path, 'onsen_spring_total', value['total_springs'], [value['used_springs'], value['unused_springs']])
                if {'school_count_total', 'main_school_count', 'branch_school_count'} <= keys:
                    self.equal(path, 'school_main_branch', value['school_count_total'], [value['main_school_count'], value['branch_school_count']])
                if {'school_count_total', 'national_school_count', 'public_school_count', 'private_school_count'} <= keys:
                    self.equal(path, 'school_ownership', value['school_count_total'], [value[k] for k in ['national_school_count', 'public_school_count', 'private_school_count']])
                if {'students_total', 'national_students', 'public_students', 'private_students'} <= keys:
                    self.equal(path, 'student_ownership', value['students_total'], [value[k] for k in ['national_students', 'public_students', 'private_students']])
                if {'under15', 'working', 'over65', 'known'} <= keys:
                    self.equal(path, 'aging_denominator', value['known'], [value['under15'], value['working'], value['over65']])
                    if numeric(value.get('rate')) and numeric(value['known']) and value['known'] > 0 and numeric(value['over65']):
                        self.counts['rate_checks'] += 1
                        calculated = 100 * value['over65'] / value['known']
                        if abs(value['rate'] - calculated) > .00501:
                            self.add('error', 'aging_rate', path, displayed=value['rate'], calculated=calculated)
                lat = value.get('lat', value.get('latitude'))
                lon = value.get('lng', value.get('lon', value.get('longitude')))
                if lat is not None or lon is not None:
                    if numeric(lat) and numeric(lon):
                        self.counts['point_checks'] += 1
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            self.add('error', 'invalid_coordinate', path, lat=lat, lon=lon)
                if value.get('type') in ['Point', 'MultiPoint', 'LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'] and 'coordinates' in value:
                    self.geometry(value, path)
                if value.get('type') == 'FeatureCollection':
                    self.counts['geojson_features'] += len(value.get('features', []))
                if {'prefectureRows', 'nationalRows'} <= keys:
                    self.school_aggregation(value, path)
                stack.extend((path + '/' + str(k), v) for k, v in value.items() if k != 'coordinates')
            elif isinstance(value, float) and not math.isfinite(value):
                self.add('error', 'non_finite_number', path)
        self.counts['scanned_nodes'] += visited

    def geometry(self, g, path):
        self.counts['geometries_checked'] += 1
        coords = g['coordinates']
        todo = [coords]
        while todo:
            v = todo.pop()
            if not isinstance(v, list):
                self.add('error', 'geometry_non_array', path)
                return
            if len(v) >= 2 and numeric(v[0]) and numeric(v[1]):
                if not (-180 <= v[0] <= 180 and -90 <= v[1] <= 90):
                    self.add('error', 'geometry_coordinate_range', path, example=v[:2])
                    return
            elif not v:
                self.counts['empty_geometry_parts'] += 1
            else:
                todo.extend(v)
        if g['type'] in ['Polygon', 'MultiPolygon']:
            polygons = [coords] if g['type'] == 'Polygon' else coords
            for polygon in polygons:
                for ring in polygon:
                    if len(ring) < 4 or ring[0] != ring[-1]:
                        self.add('error', 'unclosed_polygon_ring', path)
                        return

    def school_aggregation(self, obj, path):
        groups = defaultdict(list)
        for row in obj['prefectureRows']:
            groups[(row.get('year'), row.get('school_type'))].append(row)
        fields = ['school_count_total', 'main_school_count', 'branch_school_count',
                  'national_school_count', 'public_school_count', 'private_school_count']
        for row in obj['nationalRows']:
            key = (row.get('year'), row.get('school_type'))
            rows = groups.get(key, [])
            names = [r.get('prefecture') for r in rows]
            if len(names) != len(set(names)):
                self.add('error', 'duplicate_prefecture_in_school_group', path, group=key)
                continue
            # Official 1965 scope excludes Okinawa; missing Okinawa must stay missing.
            expected = 46 if row.get('year') == 1965 and '46' in row.get('geographic_scope', '') else 47
            for field in fields:
                parts = [r[field] for r in rows if numeric(r.get(field))]
                if len(parts) == expected:
                    self.equal(path + '/' + str(key), 'school_prefecture_sum_' + field, row.get(field), parts)
                else:
                    self.add('unchecked', 'school_aggregation_coverage', path, group=key, field=field, expected=expected, observed=len(parts))

    def result(self):
        return {'counts': dict(self.counts), 'findings': self.findings,
                'findings_truncated': sum(v for k,v in self.counts.items() if k.startswith('finding_')) > len(self.findings)}
