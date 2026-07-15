from __future__ import annotations

import math

MM_PER_INCH = 25.4
MM_PER_MIL = 0.0254
MM_PER_OZ_EQ = 0.034798
UM_PER_MM = 1000.0

SUPPORTED_UNITS = ("um", "mm", "mil", "inch", "oz")
UNIT_PRECISION = {"um": 1, "mm": 3, "mil": 3, "inch": 5, "oz": 3}
NOMINAL_COPPER_WEIGHTS_OZ = (0.25, 1.0 / 3.0, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)


def to_display(mm_value: float, unit: str) -> float:
    if unit == "um":
        return mm_value * UM_PER_MM
    if unit == "mm":
        return mm_value
    if unit == "mil":
        return mm_value / MM_PER_MIL
    if unit == "inch":
        return mm_value / MM_PER_INCH
    if unit == "oz":
        return mm_value / MM_PER_OZ_EQ
    raise ValueError(f"Unsupported unit: {unit}")


def from_display(display_value: float, unit: str) -> float:
    if unit == "um":
        return display_value / UM_PER_MM
    if unit == "mm":
        return display_value
    if unit == "mil":
        return display_value * MM_PER_MIL
    if unit == "inch":
        return display_value * MM_PER_INCH
    if unit == "oz":
        return display_value * MM_PER_OZ_EQ
    raise ValueError(f"Unsupported unit: {unit}")


def format_thickness(mm_value: float, unit: str) -> str:
    return f"{format_value(mm_value, unit)} {unit}"


def format_value(mm_value: float, unit: str) -> str:
    display_value = nominal_copper_weight_oz(mm_value) if unit == "oz" else to_display(mm_value, unit)
    return f"{display_value:.{UNIT_PRECISION[unit]}f}"


def nominal_copper_weight_oz(mm_value: float, *, tolerance_ratio: float = 0.05) -> float:
    """Return a standard PCB copper weight for rounded foil thicknesses when close enough."""
    exact_weight = to_display(mm_value, "oz")
    if exact_weight <= 0:
        return exact_weight
    nearest = min(NOMINAL_COPPER_WEIGHTS_OZ, key=lambda weight: abs(weight - exact_weight))
    if abs(nearest - exact_weight) / nearest <= tolerance_ratio:
        return nearest
    return exact_weight


def format_compact_value(mm_value: float, unit: str) -> str:
    text = format_value(mm_value, unit)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def format_compact_thickness(mm_value: float, unit: str) -> str:
    return f"{format_compact_value(mm_value, unit)} {unit}"


def thickness_unit_for_layer(selected_unit: str, *, is_copper: bool) -> str:
    if selected_unit == "oz" and not is_copper:
        return "mm"
    return selected_unit


def total_unit(selected_unit: str) -> str:
    if selected_unit == "oz":
        return "mm"
    return selected_unit


def secondary_unit(primary_unit: str, *, is_copper: bool) -> str:
    if primary_unit == "oz" and is_copper:
        return "um"
    if primary_unit == "mil":
        return "mm"
    return "mil"


def format_stackup_thickness(mm_value: float, selected_unit: str, *, is_copper: bool) -> str:
    primary_unit = thickness_unit_for_layer(selected_unit, is_copper=is_copper)
    alternate_unit = secondary_unit(primary_unit, is_copper=is_copper)
    return f"{format_value(mm_value, primary_unit)} {primary_unit} ({format_value(mm_value, alternate_unit)} {alternate_unit})"


def format_total_thickness(mm_value: float, selected_unit: str) -> str:
    primary_unit = total_unit(selected_unit)
    alternate_unit = secondary_unit(primary_unit, is_copper=False)
    return f"{format_value(mm_value, primary_unit)} {primary_unit} ({format_value(mm_value, alternate_unit)} {alternate_unit})"


def format_roughness_um(roughness_um: float | None) -> str:
    if roughness_um is None:
        return ""
    return f"Ra <= {roughness_um:.1f} um"


def snap_copper_weight_oz(oz_value: float) -> float:
    if oz_value <= 0:
        return 0.25
    snapped_steps = max(1, math.floor((oz_value / 0.25) + 0.5))
    return snapped_steps * 0.25


def snap_copper_thickness_mm(mm_value: float) -> float:
    snapped_oz = snap_copper_weight_oz(to_display(mm_value, "oz"))
    return from_display(snapped_oz, "oz")


def format_frequency_ghz(freq_ghz: float) -> str:
    if freq_ghz < 1:
        return f"{freq_ghz * 1000:.0f} MHz"
    if freq_ghz.is_integer():
        return f"{freq_ghz:.0f} GHz"
    return f"{freq_ghz:.2f} GHz"
