"""Selection and classification rules (single source of truth, documented in README).

RANKING TIER: "polygon_unit" - one ranking row = one individually published
municipality or ward that has its own polygon / data key on the map
(市・町・村・特別区・政令市の区).  Never ranked, never summed with them:
  * 総数 (00000) and 都道府県計 (xx000)
  * 政令市の市全体 / 特別区部計 (aggregates of wards)
  * 「その他の区」「その他の市町村」 (aggregates of unlisted units)
A designated-city total therefore never appears next to its own wards, so
nothing is counted twice.  Rows are classified from their actual code + label,
not from assumptions about which prefectures list wards.
"""
from __future__ import annotations

import re

from population_audit.official import DESIGNATED_CITY_CODES

TIER = "polygon_unit"
TOP_N = 10

SLUG_MAP = {
    "01": "hokkaido", "02": "tohoku", "03": "tohoku", "04": "tohoku", "05": "tohoku",
    "06": "tohoku", "07": "tohoku", "08": "kanto", "09": "kanto", "10": "kanto",
    "11": "kanto", "12": "kanto", "13": "kanto", "14": "kanto", "15": "koshinetsu",
    "19": "koshinetsu", "20": "koshinetsu", "16": "chubu_hokuriku", "17": "chubu_hokuriku",
    "18": "chubu_hokuriku", "21": "chubu_hokuriku", "22": "chubu_hokuriku",
    "23": "chubu_hokuriku", "24": "kinki", "25": "kinki", "26": "kinki", "27": "kinki",
    "28": "kinki", "29": "kinki", "30": "kinki", "31": "chugoku", "32": "chugoku",
    "33": "chugoku", "34": "chugoku", "35": "chugoku", "36": "shikoku", "37": "shikoku",
    "38": "shikoku", "39": "shikoku", "40": "kyushu_okinawa", "41": "kyushu_okinawa",
    "42": "kyushu_okinawa", "43": "kyushu_okinawa", "44": "kyushu_okinawa",
    "45": "kyushu_okinawa", "46": "kyushu_okinawa", "47": "kyushu_okinawa",
}
SLUGS = ["hokkaido", "tohoku", "kanto", "koshinetsu", "chubu_hokuriku", "kinki",
         "chugoku", "shikoku", "kyushu_okinawa"]

# public attribute subgroup -> official columns (all-sexes block / sex totals)
SUB_COLUMNS = {
    ("nationality", "japanese"): [10],
    ("nationality", "foreign"): [11],
    ("sex", "male"): [18],
    ("sex", "female"): [27],
    ("age", "0-19"): [10, 11],
    ("age", "20-39"): [12, 13],
    ("age", "40-59"): [14, 15],
    ("age", "60+"): [16],
}
TOTAL_COL = 9
AGE_UNKNOWN_COL = 17

_OTHER = re.compile(r"その他|Others?", re.I)


def classify_origin(code: str, label: str) -> str:
    """Kind of an origin row, decided from the actual row."""
    if code == "00000":
        return "total"
    if code.endswith("000"):
        return "prefecture_total"
    if code in DESIGNATED_CITY_CODES:
        return "city_total" if code != "13100" else "special_wards_total"
    if _OTHER.search(label or ""):
        return "other_wards" if "区" in (label or "") else "other_municipalities"
    return "individual"


def is_ranked_unit(code: str, label: str) -> bool:
    return classify_origin(code, label) == "individual"


def region_of(code: str) -> str | None:
    return SLUG_MAP.get(code[:2])
