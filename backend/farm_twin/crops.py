"""Crop parameters for the rotation engine (spec v0.1, "Crop parameters").

EVERY value here is ASSUMED: approximate figures from general agronomic knowledge
(FAO crop-water ranges, typical East African varieties), not sourced value by value.
They need agronomist review before the demo makes claims. The engine labels them so.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

# Bump whenever any crop value changes, so saved results stay traceable.
CROP_CATALOGUE_VERSION = "0.2.0"
CROP_PARAMS_STATUS = "assumed"  # becomes "reviewed" after agronomist review (q_crop = 1)


@dataclass(frozen=True)
class Crop:
    key: str
    name: str
    family: str
    days: int               # crop duration; cassava = per slot it occupies
    water_need_mm: float    # WR, per season slot
    dry_spell_tol_days: float  # d
    tmax_opt_c: float
    tmax_crit_c: float
    net_n_kg_ha: float      # ΔN per season slot (fixation minus removal, residues left)
    residue_index: float    # res
    food: float
    market: float
    waterlog_sensitive: bool
    slots: int = 1          # cassava fills two consecutive slots
    # FAO-56 (Allen et al. 1998) single crop coefficients and stage lengths (days): used to turn
    # the season's ET0 into the crop's water need (v0.2). Kc ini is the standard FAO value
    # (not captured when the tables were read online); mid/end values are as printed.
    kc_ini: float = 0.0
    kc_mid: float = 0.0
    kc_end: float = 0.0
    st_ini: int = 0
    st_dev: int = 0
    st_mid: int = 0
    st_late: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


CROPS: dict[str, Crop] = {c.key: c for c in (
    Crop("maize", "Maize", "cereal", 110, 500, 10, 33, 38, -40, 0.7, 0.9, 0.6, False),
    Crop("sorghum", "Sorghum", "cereal", 110, 400, 21, 35, 40, -30, 0.8, 0.7, 0.4, False),
    Crop("finger_millet", "Finger millet", "cereal", 110, 350, 18, 34, 39, -25, 0.6, 0.7, 0.5, False),
    Crop("beans", "Beans", "legume", 85, 350, 10, 30, 35, +5, 0.3, 0.8, 0.7, True),
    Crop("groundnut", "Groundnut", "legume", 105, 450, 14, 33, 38, +15, 0.3, 0.6, 0.9, True),
    Crop("cowpea", "Cowpea", "legume", 80, 300, 21, 35, 40, +20, 0.5, 0.6, 0.5, False),
    Crop("sunflower", "Sunflower", "oilseed", 100, 450, 18, 33, 38, -35, 0.5, 0.1, 0.8, False),
    Crop("cassava", "Cassava", "root", 180, 450, 30, 35, 40, -15, 0.4, 0.9, 0.5, True, slots=2),
)}

# FAO-56 Table 11 (stage lengths, regions as noted) and Table 12 (Kc). Where a range is printed
# the value below is a choice, noted here: maize Kc end 0.5 (range 0.60-0.35, field-dried to
# high-moisture harvest); cowpea Kc end 0.5 (0.60-0.35); sunflower Kc mid 1.0 (low end, rain-fed
# sparse stand); sorghum Kc mid 1.05 (1.0-1.1). Cassava uses first-year values.
_FAO56 = {  # crop: (kc_ini, kc_mid, kc_end, (ini, dev, mid, late) days)
    "maize": (0.30, 1.20, 0.50, (30, 50, 60, 40)),          # East Africa
    "sorghum": (0.30, 1.05, 0.55, (20, 35, 40, 30)),
    "finger_millet": (0.30, 1.00, 0.30, (20, 30, 55, 35)),
    "beans": (0.40, 1.15, 0.35, (20, 30, 40, 20)),
    "groundnut": (0.40, 1.15, 0.60, (25, 35, 45, 25)),      # West Africa
    "cowpea": (0.40, 1.05, 0.50, (20, 30, 30, 20)),
    "sunflower": (0.35, 1.00, 0.35, (25, 35, 45, 25)),
    "cassava": (0.30, 0.80, 0.30, (20, 40, 90, 60)),        # year 1, tropics
}
FAO56_SOURCE = "FAO Irrigation and Drainage Paper 56, Tables 11 and 12"

CROPS = {k: replace(c, kc_ini=v[0], kc_mid=v[1], kc_end=v[2], st_ini=v[3][0], st_dev=v[3][1],
                    st_mid=v[3][2], st_late=v[3][3]) for k, c in CROPS.items()
         for v in (_FAO56[k],)}

# Unordered pairs that share a soil-borne disease, penalised when one follows the other.
# Values are disease codes; display names live in the locale catalogs.
DISEASE_PAIRS: dict[frozenset, str] = {
    frozenset({"sunflower", "beans"}): "sclerotinia",  # i18n key disease.sclerotinia
}
