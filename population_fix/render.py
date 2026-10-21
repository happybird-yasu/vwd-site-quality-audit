"""Apply regenerated data to COPIES of the current published pages (never the live site).

* POP_DATA inside each regional HTML is replaced (same schema, same formatting).
* Attribute JSON (sex/age/nationality) is rewritten; age_unknown.json is a new,
  additive file that the page does not read.
* Two display defects are fixed in the copies only:
  1. nationality/foreign/2019 was unconditionally blocked (info=null);
  2. renderRanking hid the official total when the top-10 list was empty.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

CODE_RE = re.compile(r"^\d{5}$")

OLD_BLOCK_2019 = (
    "    if(currentAttr==='nationality'&&currentSub==='foreign'&&currentYear==='2019'){\n"
    "      info=null;\n"
    "    }\n"
)

NO_DATA_RE = re.compile(
    r"  if\(!info\|\|!info\.top10\|\|!info\.top10\.length\)\{\n"
    r"    const noData=document\.createElement\('div'\);\n"
    r"    noData\.innerHTML=.*?\n"
    r"    panel\.appendChild\(noData\);\n"
    r"    return;\n"
    r"  \}\n", re.S)

NEW_BLOCK_RANKING = """  if(!info||typeof info.total!=='number'){
    const noData=document.createElement('div');
    noData.innerHTML=`<div class="eyebrow">${currentYear}${t('year_suffix')} ／ ${dirLabel}${attrLabel}</div><h2>${name}</h2><div class="placeholder" style="padding-top:20px">${t('no_published_data')}</div>`;
    panel.appendChild(noData);
    return;
  }
  if(!info.top10||!info.top10.length){
    const totalOnly=document.createElement('div');
    totalOnly.innerHTML=`<div class="eyebrow">${currentYear}${t('year_suffix')} ／ ${dirLabel}${attrLabel}</div><h2>${name}</h2><div class="total-num">${info.total.toLocaleString()}<span>${t('total_people',{direction:dirLabel})}</span></div><div class="placeholder" style="padding-top:20px">${t('ranking_unavailable')}</div>`+`<p class="rank-note" style="font-size:12px;line-height:1.7;opacity:.75;margin-top:14px">${currentAttr==='age'?t('age_unknown_note'):''}</p>`;
    panel.appendChild(totalOnly);
    return;
  }
"""

MESSAGES = {
    "no_published_data": {
        "ja": "この地域・年・条件の値を表示できません。",
        "en": "Figures for this area, year and condition cannot be shown.",
        "de": "Die Werte für dieses Gebiet, dieses Jahr und diese Bedingung können nicht angezeigt werden.",
        "es": "No se pueden mostrar las cifras de esta zona, año y condición.",
        "fr": "Les valeurs pour ce territoire, cette année et cette condition ne peuvent pas être affichées.",
    },
    "ranking_unavailable": {
        "ja": "上位10件の内訳は表示できません（個別の市区町村に分けた値が公式表にない、または集約・秘匿されているため）。上の人数は公式表の総数です。",
        "en": "The top-10 breakdown cannot be shown (the official table has no values by individual municipality, or they are aggregated or withheld). The figure above is the official total.",
        "de": "Die Top-10-Aufschlüsselung kann nicht angezeigt werden (die amtliche Tabelle enthält keine Werte je Gemeinde, oder sie sind aggregiert oder unterdrückt). Die obige Zahl ist die amtliche Gesamtzahl.",
        "es": "No se puede mostrar el desglose de los 10 principales (la tabla oficial no tiene valores por municipio, o están agregados u ocultos). La cifra superior es el total oficial.",
        "fr": "Le détail des 10 premiers ne peut pas être affiché (le tableau officiel n'a pas de valeurs par commune, ou elles sont agrégées ou masquées). Le chiffre ci-dessus est le total officiel.",
    },
    "ranking_note": {
        "ja": "ランキングは公式表に個別掲載された市区町村・区の上位です。「その他」などの集約行に含まれる自治体は順位に反映されないため、全自治体の完全な順位ではありません。上の人数は公式表の総数で、ランキングの合計ではありません。",
        "en": "The ranking shows the top municipalities and wards listed individually in the official table. Places grouped under \"others\" or other aggregate rows are not ranked, so this is not a complete ranking of all municipalities. The figure above is the official total, not the sum of the ranking.",
        "de": "Die Rangliste zeigt die in der amtlichen Tabelle einzeln aufgeführten Gemeinden und Bezirke mit den höchsten Werten. Orte in Sammelzeilen wie „Sonstige“ werden nicht gerankt; es ist daher keine vollständige Rangliste aller Gemeinden. Die obige Zahl ist die amtliche Gesamtzahl, nicht die Summe der Rangliste.",
        "es": "El ranking muestra los municipios y distritos con valores más altos que figuran de forma individual en la tabla oficial. Los lugares incluidos en filas agregadas como «otros» no se clasifican, por lo que no es un ranking completo de todos los municipios. La cifra superior es el total oficial, no la suma del ranking.",
        "fr": "Le classement présente les communes et arrondissements les plus élevés listés individuellement dans le tableau officiel. Les lieux regroupés dans des lignes agrégées comme « autres » ne sont pas classés ; ce n'est donc pas un classement complet de toutes les communes. Le chiffre ci-dessus est le total officiel, et non la somme du classement.",
    },
    "age_unknown_note": {
        "ja": "年齢別の4区分には「不詳」が含まれません（公式表では別掲）。4区分の合計が総数より少ないことがあります。",
        "en": "The four age groups exclude \"unknown\" ages (listed separately in the official table), so they may add up to less than the total.",
        "de": "Die vier Altersgruppen enthalten kein „unbekanntes“ Alter (in der amtlichen Tabelle gesondert ausgewiesen); ihre Summe kann daher unter der Gesamtzahl liegen.",
        "es": "Los cuatro grupos de edad excluyen la edad «desconocida» (se indica aparte en la tabla oficial), por lo que su suma puede ser menor que el total.",
        "fr": "Les quatre groupes d'âge excluent l'âge « inconnu » (indiqué séparément dans le tableau officiel) ; leur somme peut donc être inférieure au total.",
    },
}


END_RE = re.compile(r"    \)\.join\(''\);\n  panel\.appendChild\(content\);\n")
END_NEW = """    ).join('')+`<p class="rank-note" style="font-size:12px;line-height:1.7;opacity:.75;margin-top:14px">${t('ranking_note')}${currentAttr==='age'?' '+t('age_unknown_note'):''}</p>`;
  panel.appendChild(content);
"""


class PatchError(Exception):
    pass


def replace_const_json(html: str, name: str, obj) -> str:
    m = re.search(r"const " + re.escape(name) + r"\s*=\s*", html)
    if not m:
        raise PatchError(f"const {name} not found")
    _, end = json.JSONDecoder().raw_decode(html[m.end():])
    return html[:m.end()] + json.dumps(obj, ensure_ascii=False) + html[m.end() + end:]


def read_const_json(html: str, name: str):
    m = re.search(r"const " + re.escape(name) + r"\s*=\s*", html)
    if not m:
        raise PatchError(f"const {name} not found")
    return json.JSONDecoder().raw_decode(html[m.end():])[0]


def read_slug_map(html: str) -> dict:
    m = re.search(r"const SLUG_MAP\s*=\s*\{([^}]*)\}", html)
    return dict(re.findall(r"'(\d{2})'\s*:\s*'([a-z_]+)'", m.group(1))) if m else {}


def patch_display(html: str) -> str:
    """Apply the two JS display fixes; fail loudly if the expected code is not found."""
    if html.count(OLD_BLOCK_2019) != 1:
        raise PatchError("2019-foreign block not found exactly once")
    html = html.replace(OLD_BLOCK_2019, "")
    if len(NO_DATA_RE.findall(html)) != 1:
        raise PatchError("renderRanking no-data block not found exactly once")
    html = NO_DATA_RE.sub(lambda m: NEW_BLOCK_RANKING, html)
    if len(END_RE.findall(html)) != 1:
        raise PatchError("renderRanking list end not found exactly once")
    return END_RE.sub(lambda m: END_NEW, html)


def patch_messages_json(data: dict, lang: str) -> dict:
    out = dict(data)
    out.pop("foreign_2019_unavailable", None)
    for key, per_lang in MESSAGES.items():
        out[key] = per_lang[lang]
    return out


def patch_i18n_js(js: str, lang: str) -> str:
    """Patch the Japanese fallback dictionary embedded in js/i18n.js."""
    js, n1 = re.subn(r'"foreign_2019_unavailable":"[^"]*",', "", js)
    extra = "".join(',"%s":%s' % (k, json.dumps(MESSAGES[k]["ja"], ensure_ascii=False))
                    for k in ("ranking_unavailable", "ranking_note", "age_unknown_note"))
    ja = json.dumps(MESSAGES["no_published_data"]["ja"], ensure_ascii=False)
    js, n2 = re.subn(r'"no_published_data":"[^"]*"', lambda m: '"no_published_data":' + ja + extra, js)
    if n1 != 1 or n2 != 1:
        raise PatchError(f"i18n.js patterns not found ({lang}: {n1},{n2})")
    return js


def _filter_leaf(d: dict, keep) -> dict:
    return {k: v for k, v in d.items() if keep(k)}


def filter_pop(pop: dict, slug_of) -> dict:
    return pop


def region_pop(pop: dict, slug: str, region_of) -> dict:
    return {y: {d: _filter_leaf(c, lambda k: region_of(k) == slug) for d, c in dirs.items()}
            for y, dirs in pop.items()}


def region_attr(attr: dict, slug: str, region_of) -> dict:
    return {y: {d: {s: _filter_leaf(c, lambda k: region_of(k) == slug) for s, c in subs.items()}
                for d, subs in dirs.items()} for y, dirs in attr.items()}


def merge_pop(old: dict, new: dict, years, prefixes) -> dict:
    """Replace only the given years' destination codes (by prefecture prefix); keep the rest."""
    out = json.loads(json.dumps(old))
    for y in years:
        for d in ("inflow", "outflow"):
            cur = out.setdefault(y, {}).setdefault(d, {})
            for c in [c for c in cur if c[:2] in prefixes]:
                del cur[c]
            cur.update({c: r for c, r in new.get(y, {}).get(d, {}).items() if c[:2] in prefixes})
            out[y][d] = dict(sorted(cur.items()))
    return out


def merge_attr(old: dict, new: dict, years, prefixes) -> dict:
    out = json.loads(json.dumps(old))
    for y in years:
        for d in ("inflow", "outflow"):
            for sub in set(old.get(y, {}).get(d, {})) | set(new.get(y, {}).get(d, {})):
                cur = out.setdefault(y, {}).setdefault(d, {}).setdefault(sub, {})
                for c in [c for c in cur if c[:2] in prefixes]:
                    del cur[c]
                cur.update({c: r for c, r in new.get(y, {}).get(d, {}).get(sub, {}).items()
                            if c[:2] in prefixes})
                out[y][d][sub] = dict(sorted(cur.items()))
    return out


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
