# -*- coding: utf-8 -*-
"""
dose_tools.py

Parsing and analysis engine for 1D relative-dose QA (PDD and profiles),
adapted from the "relative_dose_1d" project (L. A. Olivares Jimenez) and
extended to:

    * extract ALL curves contained in a file (a w2CAD ``.data`` file or a
      PTW ``.mcc`` file can hold several scans: several field sizes,
      depths, PDD + inplane + crossplane profiles, etc.), so the caller
      can pick the one it needs instead of always getting the first one;
    * compute the gamma index with a vectorized implementation (fast
      enough for an interactive Streamlit app);
    * compute flatness, symmetry and penumbra for profiles.

Two input formats are supported:

    * ``.data`` (w2CAD, TPS Eclipse export) -> used here as the
      **commissioning** reference.
      Curves are delimited by ``$STOM ... $ENOM`` (profile) or
      ``$STOD ... $ENOD`` (PDD). Data rows look like ``< pos  dose ... >``.

    * ``.mcc`` (PTW Verisoft export) -> used here as the **measurement**
      to be evaluated.
      Curves are delimited by ``BEGIN_SCAN_DATA ... END_SCAN_DATA``, with
      metadata as ``KEY=VALUE`` lines and numeric data between
      ``BEGIN_DATA`` and ``END_DATA``.

Every parsed curve is returned as a ``Curve`` dataclass carrying a
(N, 2) numpy array (position [mm], normalized dose [%]) plus metadata
useful to build a human readable label (curve type, field size, depth,
scan direction).

NOTE on assumptions
--------------------
Real-world w2CAD/mcc exports vary between TPS/software versions. The
parsers below are deliberately tolerant (they skip anything they don't
recognize) but the geometric conventions (e.g. that $STOM == profile,
$STOD == PDD) follow the original project's documented behaviour and
common PTW/Varian usage. If a specific file does not parse as expected,
the file is likely a variant of the format; check the raw text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np


# --------------------------------------------------------------------------
# Data container
# --------------------------------------------------------------------------

@dataclass
class Curve:
    """A single parsed dose curve."""
    data: np.ndarray                 # (N, 2) -> [position_mm, dose_percent]
    curve_type: str                  # 'PDD' or 'PROFILE'
    direction: str = ""              # 'INPLANE' / 'CROSSPLANE' / 'DIAGONAL' / ''
    depth_mm: float | None = None
    field_size: str = ""             # e.g. "100x100"
    label: str = ""                  # human readable, built at the end
    source: str = ""                 # file name

    def build_label(self, index: int) -> str:
        parts = [f"#{index}", self.curve_type]
        if self.direction:
            parts.append(self.direction)
        # "depth" only makes sense as a fixed parameter for a profile
        # (a PDD's x-axis IS depth, so showing it there is meaningless/misleading).
        if self.curve_type == "PROFILE" and self.depth_mm is not None:
            parts.append(f"depth={self.depth_mm:g}mm")
        if self.field_size:
            parts.append(f"field={self.field_size}")
        self.label = " | ".join(parts)
        return self.label


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------

def _normalize(data: np.ndarray) -> np.ndarray:
    data = data.astype(float)
    m = np.nanmax(data[:, 1])
    if m > 0:
        data[:, 1] = 100.0 * data[:, 1] / m
    return data


def bytes_to_lines(file_bytes: bytes) -> list[str]:
    """Decode uploaded file bytes into a list of stripped text lines."""
    text = file_bytes.decode("utf-8", errors="replace")
    return [line.strip() for line in text.splitlines()]


# --------------------------------------------------------------------------
# w2CAD (.data) parser  -> commissioning data
# --------------------------------------------------------------------------

def _parse_percent_metadata(block_lines: list[str]) -> dict:
    """Parse ``%key: value`` metadata lines found at the top of a w2CAD
    data block (e.g. ``%title: Measured Profiles``,
    ``%axis legend: Offaxis distance``, ``%field size: 100``,
    ``%measurement depth: 300``)."""
    meta = {}
    for l in block_lines:
        if l.startswith("%") and ":" in l:
            key, _, value = l[1:].partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta


def _w2cad_curve_type(meta: dict, tag_guess: str) -> str:
    """Determine PDD vs PROFILE for a w2CAD block.

    IMPORTANT: real-world w2CAD exports do not reliably use ``$STOM`` for
    profiles and ``$STOD`` for PDDs -- some TPS/scanner software exports
    everything under ``$STOD`` regardless of curve type. The ``%title``
    and ``%axis legend`` metadata lines are a much more reliable
    discriminator and are used first; the tag is only a fallback."""
    axis_legend = meta.get("axis legend", "").lower()
    title = meta.get("title", "").lower()

    if "depth" in axis_legend or "depth dose" in title or "pdd" in title:
        return "PDD"
    if ("offaxis" in axis_legend or "off-axis" in axis_legend
            or "distance" in axis_legend or "profile" in title):
        return "PROFILE"
    return tag_guess


def parse_w2cad(lines: list[str], source: str = "") -> list[Curve]:
    """Extract every curve found in a w2CAD (.data) file."""
    curves: list[Curve] = []

    last_field_size = ""

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # Field size tag: value is on the following non-empty line.
        if line == "$FLSZ":
            j = i + 1
            while j < n and lines[j] == "":
                j += 1
            if j < n:
                parts = lines[j].split()
                if len(parts) >= 2:
                    last_field_size = f"{parts[0]}x{parts[1]}"
            i = j + 1
            continue

        if line in ("$STOM", "$STOD"):
            tag_guess = "PROFILE" if line == "$STOM" else "PDD"
            end_tag = "$ENOM" if line == "$STOM" else "$ENOD"
            try:
                end_index = lines.index(end_tag, i + 1)
            except ValueError:
                i += 1
                continue

            block = lines[i + 1:end_index]

            # Split the block into leading "%metadata" lines and the
            # actual "<pos dose ...>" data rows.
            meta = _parse_percent_metadata(block)

            rows = []
            for row_line in block:
                if row_line.startswith("<"):
                    content = row_line.strip("<>").split()
                    if len(content) >= 2:
                        try:
                            rows.append([float(content[0]), float(content[1])])
                        except ValueError:
                            pass

            if rows:
                curve_type = _w2cad_curve_type(meta, tag_guess)

                field_size = last_field_size
                if "field size" in meta:
                    fs_val = meta["field size"]
                    try:
                        fs_num = float(fs_val)
                        field_size = f"{fs_num:g}x{fs_num:g}"
                    except ValueError:
                        field_size = fs_val

                depth_mm = None
                if "measurement depth" in meta:
                    try:
                        depth_mm = float(meta["measurement depth"])
                    except ValueError:
                        pass

                data = _normalize(np.array(rows))
                curves.append(
                    Curve(
                        data=data,
                        curve_type=curve_type,
                        depth_mm=depth_mm,
                        field_size=field_size,
                        source=source,
                    )
                )
            i = end_index + 1
            continue

        i += 1

    for idx, c in enumerate(curves, start=1):
        c.build_label(idx)

    return curves


# --------------------------------------------------------------------------
# PTW (.mcc) parser -> measurement data
# --------------------------------------------------------------------------

def _mcc_curve_type(curvetype: str) -> tuple[str, str]:
    """Map SCAN_CURVETYPE to (curve_type, direction)."""
    ct = curvetype.upper()
    if "DEPTH" in ct or "PDD" in ct:
        return "PDD", ""
    if "CROSSPLANE" in ct:
        return "PROFILE", "CROSSPLANE"
    if "INPLANE" in ct:
        return "PROFILE", "INPLANE"
    if "DIAGONAL" in ct:
        return "PROFILE", "DIAGONAL"
    if "PROFILE" in ct:
        return "PROFILE", ""
    return "UNKNOWN", ""


def parse_mcc(lines: list[str], source: str = "") -> list[Curve]:
    """Extract every scan found in a PTW (.mcc) file."""
    curves: list[Curve] = []

    n = len(lines)
    i = 0
    while i < n:
        if lines[i] == "BEGIN_SCAN_DATA":
            try:
                end_scan = lines.index("END_SCAN_DATA", i + 1)
            except ValueError:
                break
            block = lines[i + 1:end_scan]

            meta = {}
            for l in block:
                if "=" in l:
                    key, _, value = l.partition("=")
                    meta[key.strip().upper()] = value.strip()

            curve_type, direction = _mcc_curve_type(meta.get("SCAN_CURVETYPE", ""))

            depth_mm = None
            if "SCAN_DEPTH" in meta:
                try:
                    depth_mm = float(meta["SCAN_DEPTH"])
                except ValueError:
                    pass

            field_size = ""
            fi = meta.get("FIELD_INPLANE")
            fc = meta.get("FIELD_CROSSPLANE")
            if fi and fc:
                field_size = f"{fi}x{fc}"

            try:
                start_data = block.index("BEGIN_DATA") + 1
                end_data = block.index("END_DATA")
            except ValueError:
                i = end_scan + 1
                continue

            rows = []
            for row_line in block[start_data:end_data]:
                parts = row_line.split()
                if len(parts) >= 2:
                    try:
                        rows.append([float(parts[0]), float(parts[1])])
                    except ValueError:
                        pass

            if rows:
                data = _normalize(np.array(rows))
                curves.append(
                    Curve(
                        data=data,
                        curve_type=curve_type if curve_type != "UNKNOWN" else _guess_type(data),
                        direction=direction,
                        depth_mm=depth_mm,
                        field_size=field_size,
                        source=source,
                    )
                )
            i = end_scan + 1
            continue
        i += 1

    for idx, c in enumerate(curves, start=1):
        c.build_label(idx)

    return curves


def _guess_type(data: np.ndarray) -> str:
    """Fallback heuristic if SCAN_CURVETYPE metadata is missing:
    a PDD is monotonic-ish along a single axis starting near the surface,
    a profile is symmetric around a central peak. We use the position of
    the maximum: near an edge -> PDD-like, near the middle -> profile."""
    x = data[:, 0]
    y = data[:, 1]
    peak_idx = int(np.argmax(y))
    frac = peak_idx / max(len(x) - 1, 1)
    if 0.2 < frac < 0.8:
        return "PROFILE"
    return "PDD"


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

def parse_file(file_bytes: bytes, filename: str) -> list[Curve]:
    """Parse a .data or .mcc file (by extension) into a list of Curves."""
    lines = bytes_to_lines(file_bytes)
    lower = filename.lower()
    if lower.endswith(".mcc"):
        return parse_mcc(lines, source=filename)
    elif lower.endswith(".data") or lower.endswith(".dat") or lower.endswith(".txt"):
        # Try w2CAD first; if nothing found, try mcc-style as a fallback.
        curves = parse_w2cad(lines, source=filename)
        if not curves:
            curves = parse_mcc(lines, source=filename)
        return curves
    else:
        # Unknown extension: try both parsers.
        curves = parse_w2cad(lines, source=filename)
        if not curves:
            curves = parse_mcc(lines, source=filename)
        return curves


# --------------------------------------------------------------------------
# Gamma index (vectorized)
# --------------------------------------------------------------------------

def gamma_1d(ref: np.ndarray,
             eval_curve: np.ndarray,
             dose_t: float = 3.0,
             dist_t: float = 2.0,
             dose_threshold: float = 0.0,
             interp: int = 10):
    """
    1D gamma index (global, dose values assumed already normalized 0-100%).

    Vectorized re-implementation of the original ``gamma_1D`` function
    from tools.py (same parameters and same result, but avoids the
    Python-level double loop so it stays responsive in an interactive
    app).

    Returns
    -------
    gamma : ndarray (M, 2)
        [position, gamma_value] for every reference point (nan outside
        overlap or below threshold).
    gamma_percent : float
        Percentage of evaluated points with gamma <= 1.
    evaluated_points : int
        Number of reference points actually evaluated.
    """
    ref = np.asarray(ref, dtype=float)
    ev = np.asarray(eval_curve, dtype=float)

    min_pos = max(ref[:, 0].min(), ev[:, 0].min())
    max_pos = min(ref[:, 0].max(), ev[:, 0].max())

    n_eval = ev.shape[0]
    n_interp_pts = (interp + 1) * (n_eval - 1) + 1
    interp_x = np.linspace(ev[0, 0], ev[-1, 0], n_interp_pts, endpoint=True)
    interp_y = np.interp(interp_x, ev[:, 0], ev[:, 1])

    gamma_vals = np.full(ref.shape[0], np.nan)

    in_range = (ref[:, 0] >= min_pos) & (ref[:, 0] <= max_pos)
    above_threshold = ref[:, 1] >= dose_threshold
    valid = in_range & above_threshold

    if np.any(valid):
        rx = ref[valid, 0][:, None]
        ry = ref[valid, 1][:, None]

        dx = rx - interp_x[None, :]
        dd = ry - interp_y[None, :]

        g_matrix = np.sqrt((dx / dist_t) ** 2 + (dd / dose_t) ** 2)
        gamma_vals[valid] = np.min(g_matrix, axis=1)

    finite = ~np.isnan(gamma_vals)
    evaluated_points = int(np.sum(finite))
    if evaluated_points > 0:
        passed = int(np.sum(gamma_vals[finite] <= 1))
        gamma_percent = 100.0 * passed / evaluated_points
    else:
        gamma_percent = float("nan")

    gamma = np.column_stack((ref[:, 0], gamma_vals))
    return gamma, gamma_percent, evaluated_points


# --------------------------------------------------------------------------
# Profile metrics: flatness, symmetry, penumbra
# --------------------------------------------------------------------------

def _find_crossing(x: np.ndarray, y: np.ndarray, level: float,
                    side: str, center_idx: int) -> float:
    """Linear-interpolated position where the profile crosses `level`,
    searching outward from the peak on the given side ('left'/'right')."""
    if side == "left":
        sub_x = x[:center_idx + 1]
        sub_y = y[:center_idx + 1]
        below = np.where(sub_y <= level)[0]
        if below.size == 0:
            return float(sub_x[0])
        i = int(below[-1])
        if i == len(sub_x) - 1:
            return float(sub_x[i])
        x1, x2 = sub_x[i], sub_x[i + 1]
        y1, y2 = sub_y[i], sub_y[i + 1]
    else:
        sub_x = x[center_idx:]
        sub_y = y[center_idx:]
        below = np.where(sub_y <= level)[0]
        if below.size == 0:
            return float(sub_x[-1])
        i = int(below[0])
        if i == 0:
            return float(sub_x[0])
        x1, x2 = sub_x[i - 1], sub_x[i]
        y1, y2 = sub_y[i - 1], sub_y[i]

    if y2 == y1:
        return float(x1)
    return float(x1 + (level - y1) * (x2 - x1) / (y2 - y1))


def field_edges(profile: np.ndarray, level_percent: float = 50.0):
    """(left_edge, right_edge) positions where the profile crosses
    `level_percent` of its maximum dose."""
    x, y = profile[:, 0], profile[:, 1]
    center_idx = int(np.argmax(y))
    level = np.max(y) * level_percent / 100.0
    left = _find_crossing(x, y, level, "left", center_idx)
    right = _find_crossing(x, y, level, "right", center_idx)
    return left, right


def penumbra(profile: np.ndarray, low: float = 20.0, high: float = 80.0):
    """(left_penumbra_mm, right_penumbra_mm): distance between the
    `low`% and `high`% dose levels at each field edge (default 20-80%)."""
    x, y = profile[:, 0], profile[:, 1]
    center_idx = int(np.argmax(y))
    ymax = np.max(y)

    l_high = _find_crossing(x, y, high / 100.0 * ymax, "left", center_idx)
    l_low = _find_crossing(x, y, low / 100.0 * ymax, "left", center_idx)
    r_high = _find_crossing(x, y, high / 100.0 * ymax, "right", center_idx)
    r_low = _find_crossing(x, y, low / 100.0 * ymax, "right", center_idx)

    return abs(l_low - l_high), abs(r_low - r_high)


def flatness_symmetry(profile: np.ndarray, field_level: float = 50.0,
                       central_fraction: float = 0.8):
    """Flatness (IEC-style, (Dmax-Dmin)/(Dmax+Dmin)*100) and symmetry
    (point-to-point mirrored dose difference, %) computed over the
    central `central_fraction` of the field width (default 80%)."""
    x, y = profile[:, 0], profile[:, 1]
    left, right = field_edges(profile, field_level)
    field_size = right - left
    margin = (1 - central_fraction) / 2 * field_size
    xin, xout = left + margin, right - margin

    mask = (x >= xin) & (x <= xout)
    if mask.sum() < 2:
        raise ValueError("Not enough points in the central field region "
                          "to compute flatness/symmetry.")

    Dmax = float(np.max(y[mask]))
    Dmin = float(np.min(y[mask]))
    flatness = 100.0 * (Dmax - Dmin) / (Dmax + Dmin)

    center = (left + right) / 2.0
    x_central = x[mask]
    y_central = y[mask]
    y_mirror = np.interp(2 * center - x_central, x, y)
    D_center = float(np.interp(center, x, y))
    symmetry = 100.0 * float(np.max(np.abs(y_central - y_mirror))) / D_center if D_center else float("nan")

    return flatness, symmetry, field_size, center


def profile_metrics(profile: np.ndarray) -> dict:
    """Compute flatness, symmetry, field size, center and penumbra
    (left/right) for a normalized dose profile (N,2) array."""
    flatness, symmetry, field_size, center = flatness_symmetry(profile)
    left_pen, right_pen = penumbra(profile)
    return {
        "Flatness [%]": flatness,
        "Symmetry [%]": symmetry,
        "Field size [mm]": field_size,
        "Center [mm]": center,
        "Left penumbra [mm]": left_pen,
        "Right penumbra [mm]": right_pen,
    }
