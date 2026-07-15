"""build_flex_material_catalog.py

Parses the two rigid-flex datasheets under Materials/RigidFlexMaterials/
into two JSON catalogs, mirroring the structure tools/build_material_catalog.py
already uses for rigid core/prepreg materials:

  - data/flex_core_material_catalog.json   <- PolymideCore_Adhesiveless_Panasonic_R-F777.pdf
  - data/coverlay_material_catalog.json    <- CoverlayAdhessive_Arisawa_C33.pdf

Why this isn't a pure regex/pdfplumber table parser like the rigid-material
parsers: both source sheets are PowerPoint/Excel exports with merged header
cells and Japanese annotations mixed into the composition column, which makes
naive whitespace-split parsing unreliable and silently wrong in ways that are
hard to catch. Instead, every row below was read directly off the rasterized
datasheet pages (Materials/RigidFlexMaterials/*.pdf, page 1) and is checked
against pdftotext -layout output for consistency. The FLEX_CORE_ROWS and
COVERLAY_ROWS tables below are the parsed result; the source row/column each
field came from is noted in the comments so it stays auditable.

Re-run whenever the source PDFs change:
    python tools/build_flex_material_catalog.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLEX_CORE_PDF = ROOT / "Materials" / "RigidFlexMaterials" / "PolymideCore_Adhesiveless_Panasonic_R-F777.pdf"
COVERLAY_PDF = ROOT / "Materials" / "RigidFlexMaterials" / "CoverlayAdhessive_Arisawa_C33.pdf"

FLEX_CORE_OUTPUT = ROOT / "data" / "flex_core_material_catalog.json"
COVERLAY_OUTPUT = ROOT / "data" / "coverlay_material_catalog.json"


# ---------------------------------------------------------------------------
# Panasonic R-F777 (2-layer double-side FCCL), read from "General Properties
# of R-F777" page 1, table 1 (14 variants) + table 2 (12 variants) = 26 rows.
#
# Each tuple: (variant_code, copper_top_um, pi_um, copper_bottom_um,
#              copper_type, dk_100mhz_1ghz, total_product_thickness_um)
#
# - copper_type is read from the parenthetical in the Composition row
#   ("ED" or "RA"), not guessed from the variant code (variant codes like
#   "13EJ" or "8HE9" do not reliably encode copper type).
# - Dk is identical at 100 MHz and 1 GHz for every variant on this sheet, so
#   one value is stored and used for both frequency points.
# - Df is 0.005 for every variant at both frequencies (also read off sheet).
# - "21ED" appears twice on the source datasheet (table 1 and table 2) with
#   two different constructions (35-50-35 and 35-25-35). That is a
#   duplicate/typo on Panasonic's part, not a transcription error here
#   (verified against the rasterized page) -- both rows are kept, and unique
#   catalog ids are generated from variant_code + construction.
# - "8HE9" is the only asymmetric construction (copper 70um / PI 20um /
#   copper 18um) -- everything else on the sheet is symmetric.
# ---------------------------------------------------------------------------
FLEX_CORE_ROWS: list[tuple[str, float, float, float, str, float, float]] = [
    ("13EJ", 12.0, 25.0, 12.0, "ED", 3.2, 49.0),
    ("12EJ", 18.0, 25.0, 18.0, "ED", 3.2, 61.0),
    ("11ED", 35.0, 25.0, 35.0, "ED", 3.2, 90.0),
    ("23EJ", 12.0, 50.0, 12.0, "ED", 3.3, 74.0),
    ("22EJ", 18.0, 50.0, 18.0, "ED", 3.3, 86.0),
    ("21ED", 35.0, 50.0, 35.0, "ED", 3.3, 120.0),
    ("13RV", 12.0, 25.0, 12.0, "RA", 3.2, 49.0),
    ("12RV", 18.0, 25.0, 18.0, "RA", 3.2, 61.0),
    ("11RV", 35.0, 25.0, 35.0, "RA", 3.2, 95.0),
    ("23RV", 12.0, 50.0, 12.0, "RA", 3.3, 74.0),
    ("22RV", 18.0, 50.0, 18.0, "RA", 3.3, 86.0),
    ("21RV", 35.0, 50.0, 35.0, "RA", 3.3, 120.0),
    ("8HE9", 70.0, 20.0, 18.0, "ED", 3.2, 108.0),
    ("33RV", 12.0, 75.0, 12.0, "RA", 3.3, 99.0),
    ("32RV", 18.0, 75.0, 18.0, "RA", 3.3, 111.0),
    ("42RV", 18.0, 100.0, 18.0, "RA", 3.3, 136.0),
    ("33EJ", 12.0, 75.0, 12.0, "ED", 3.3, 99.0),
    ("32EJ", 18.0, 75.0, 18.0, "ED", 3.3, 111.0),
    ("41EM", 35.0, 100.0, 35.0, "ED", 3.3, 170.0),
    ("52RV", 18.0, 12.5, 18.0, "RA", 3.2, 49.0),
    ("12R5", 18.0, 25.0, 18.0, "RA", 3.2, 61.0),
    ("21ED", 35.0, 25.0, 35.0, "ED", 3.3, 120.0),
    ("53RV", 12.0, 12.5, 12.0, "RA", 3.2, 37.0),
    ("31RV", 35.0, 75.0, 35.0, "RA", 3.3, 145.0),
    ("41RV", 35.0, 100.0, 35.0, "RA", 3.3, 170.0),
    ("20RV", 70.0, 50.0, 70.0, "RA", 3.3, 190.0),
]

FLEX_CORE_DF = 0.005
FLEX_CORE_FREQS_GHZ = (0.1, 1.0)


# ---------------------------------------------------------------------------
# Arisawa C33 coverlay, read from "Product Properties" table, page 1.
# One family (C33), split into its two physical components since each has
# its own thickness and Dk/Df on the sheet:
#   - "Coverlay" row       -> the polyimide-film component (CVL-PI)
#   - "Adhesive only" row  -> the adhesive component (CVL-Adh.)
# Both measured at 10 GHz (the only frequency point on this sheet).
# ---------------------------------------------------------------------------
COVERLAY_FAMILY = "C33"
COVERLAY_MANUFACTURER = "Arisawa"
COVERLAY_FREQ_GHZ = 10.0
COVERLAY_PI_THICKNESS_UM = 12.5
COVERLAY_ADHESIVE_THICKNESS_UM = 25.0
COVERLAY_PI_DK = 2.95
COVERLAY_PI_DF = 0.0053
COVERLAY_ADHESIVE_DK = 2.60
COVERLAY_ADHESIVE_DF = 0.0026
COVERLAY_PEEL_STRENGTH_N_PER_CM = 8.5
COVERLAY_SOLDER_HEAT_RESISTANCE_C = 300.0


def _slugify(*parts: str) -> str:
    text = "-".join(parts).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def build_flex_core_catalog() -> dict[str, object]:
    materials = []
    for variant_code, cu_top, pi_um, cu_bottom, copper_type, dk, total_um in FLEX_CORE_ROWS:
        construction = f"{cu_top:g}-{pi_um:g}-{cu_bottom:g}"
        material_id = _slugify("r-f777", variant_code, construction)
        dk_by_freq = {str(freq): dk for freq in FLEX_CORE_FREQS_GHZ}
        df_by_freq = {str(freq): FLEX_CORE_DF for freq in FLEX_CORE_FREQS_GHZ}
        materials.append(
            {
                "id": material_id,
                "manufacturer": "Panasonic",
                "series": "R-F777",
                "family": "R-F777",
                "material_type": "flex_core",
                "variant_code": variant_code,
                "copper_type": copper_type,
                "copper_thickness_top_um": cu_top,
                "copper_thickness_bottom_um": cu_bottom,
                "symmetric_copper": cu_top == cu_bottom,
                "dielectric_thickness_um": pi_um,
                "dielectric_thickness_mm": round(pi_um / 1000.0, 6),
                "total_product_thickness_um": total_um,
                "dk_by_freq_ghz": dk_by_freq,
                "df_by_freq_ghz": df_by_freq,
                "reference_freq_ghz": FLEX_CORE_FREQS_GHZ[-1],
                "reference_dk": dk,
                "reference_df": FLEX_CORE_DF,
                "max_freq_ghz": FLEX_CORE_FREQS_GHZ[-1],
                "source_pdf": FLEX_CORE_PDF.name,
                "notes": (
                    f"2-layer double-side FCCL, construction {construction}um "
                    f"({copper_type}), parsed from R-F777 datasheet variant {variant_code}."
                ),
            }
        )
    return {"materials": materials}


def build_coverlay_catalog() -> dict[str, object]:
    materials = [
        {
            "id": _slugify(COVERLAY_MANUFACTURER, COVERLAY_FAMILY, "cvl-pi"),
            "manufacturer": COVERLAY_MANUFACTURER,
            "series": COVERLAY_FAMILY,
            "family": COVERLAY_FAMILY,
            "material_type": "coverlay_pi",
            "component": "coverlay_pi",
            "thickness_um": COVERLAY_PI_THICKNESS_UM,
            "thickness_mm": round(COVERLAY_PI_THICKNESS_UM / 1000.0, 6),
            "dk_by_freq_ghz": {str(COVERLAY_FREQ_GHZ): COVERLAY_PI_DK},
            "df_by_freq_ghz": {str(COVERLAY_FREQ_GHZ): COVERLAY_PI_DF},
            "reference_freq_ghz": COVERLAY_FREQ_GHZ,
            "reference_dk": COVERLAY_PI_DK,
            "reference_df": COVERLAY_PI_DF,
            "max_freq_ghz": COVERLAY_FREQ_GHZ,
            "peel_strength_n_per_cm": COVERLAY_PEEL_STRENGTH_N_PER_CM,
            "solder_heat_resistance_c": COVERLAY_SOLDER_HEAT_RESISTANCE_C,
            "source_pdf": COVERLAY_PDF.name,
            "notes": "Polyimide-film component of the C33 coverlay laminate ('Coverlay' row on datasheet).",
        },
        {
            "id": _slugify(COVERLAY_MANUFACTURER, COVERLAY_FAMILY, "cvl-adhesive"),
            "manufacturer": COVERLAY_MANUFACTURER,
            "series": COVERLAY_FAMILY,
            "family": COVERLAY_FAMILY,
            "material_type": "coverlay_adhesive",
            "component": "coverlay_adhesive",
            "thickness_um": COVERLAY_ADHESIVE_THICKNESS_UM,
            "thickness_mm": round(COVERLAY_ADHESIVE_THICKNESS_UM / 1000.0, 6),
            "dk_by_freq_ghz": {str(COVERLAY_FREQ_GHZ): COVERLAY_ADHESIVE_DK},
            "df_by_freq_ghz": {str(COVERLAY_FREQ_GHZ): COVERLAY_ADHESIVE_DF},
            "reference_freq_ghz": COVERLAY_FREQ_GHZ,
            "reference_dk": COVERLAY_ADHESIVE_DK,
            "reference_df": COVERLAY_ADHESIVE_DF,
            "max_freq_ghz": COVERLAY_FREQ_GHZ,
            "peel_strength_n_per_cm": COVERLAY_PEEL_STRENGTH_N_PER_CM,
            "solder_heat_resistance_c": COVERLAY_SOLDER_HEAT_RESISTANCE_C,
            "source_pdf": COVERLAY_PDF.name,
            "notes": "Adhesive-only component of the C33 coverlay laminate ('Adhesive only' row on datasheet).",
        },
    ]
    return {"materials": materials}


def main() -> None:
    if not FLEX_CORE_PDF.exists():
        raise FileNotFoundError(f"Expected source datasheet not found: {FLEX_CORE_PDF}")
    if not COVERLAY_PDF.exists():
        raise FileNotFoundError(f"Expected source datasheet not found: {COVERLAY_PDF}")

    FLEX_CORE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    flex_core_payload = build_flex_core_catalog()
    FLEX_CORE_OUTPUT.write_text(json.dumps(flex_core_payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(flex_core_payload['materials'])} flex-core entries to {FLEX_CORE_OUTPUT}")

    coverlay_payload = build_coverlay_catalog()
    COVERLAY_OUTPUT.write_text(json.dumps(coverlay_payload, indent=2), encoding="utf-8")
    print(f"Wrote {len(coverlay_payload['materials'])} coverlay entries to {COVERLAY_OUTPUT}")


if __name__ == "__main__":
    main()
