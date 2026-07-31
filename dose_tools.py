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
    origin: str = ""                 # 'MEASURED' / 'CALCULATED' / '' (unknown, e.g. w2CAD/mcc)
    algorithm: str = ""              # e.g. 'Acurous_18.0.1' (TPS calculation algorithm, if known)
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
        if self.origin == "CALCULATED":
            algo_note = f" ({self.algorithm})" if self.algorithm else ""
            parts.append(f"TPS-calc{algo_note}")
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
    """Decode uploaded file bytes into a list of stripped text lines.
    Real-world PTW .mcc exports are sometimes not valid UTF-8 (stray
    extended-ASCII bytes in free-text metadata fields), so fall back to
    latin-1 (which never raises) if strict UTF-8 decoding fails.
    ``utf-8-sig`` transparently strips a leading BOM if present (some
    TPS bulk exports are saved with one) while still decoding plain
    UTF-8 files normally."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")
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


def _parse_mcc_block_metadata(block: list[str]) -> dict:
    """Parse ``KEY=VALUE`` metadata lines within an mcc scan block."""
    meta = {}
    for l in block:
        if "=" in l and not l.startswith(("BEGIN_DATA", "END_DATA")):
            key, _, value = l.partition("=")
            meta[key.strip().upper()] = value.strip()
    return meta


def _parse_scan_block(block: list[str], source: str = "") -> "Curve | None":
    """Parse a single curve out of the lines found between a
    ``BEGIN_SCAN``/``BEGIN_SCAN_DATA`` and its matching end tag: reads
    the KEY=VALUE metadata and the BEGIN_DATA/END_DATA numeric rows."""
    meta = _parse_mcc_block_metadata(block)

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
        return None

    rows = []
    for row_line in block[start_data:end_data]:
        parts = row_line.split()
        if len(parts) >= 2:
            try:
                rows.append([float(parts[0]), float(parts[1])])
            except ValueError:
                pass

    if not rows:
        return None

    data = _normalize(np.array(rows))
    return Curve(
        data=data,
        curve_type=curve_type if curve_type != "UNKNOWN" else _guess_type(data),
        direction=direction,
        depth_mm=depth_mm,
        field_size=field_size,
        source=source,
    )


def parse_mcc(lines: list[str], source: str = "") -> list[Curve]:
    """Extract every scan found in a PTW (.mcc) file.

    Real-world CC-Export files (PTW BeamScan/Mephysto) wrap ALL curves of
    a session in a single outer ``BEGIN_SCAN_DATA ... END_SCAN_DATA``
    block, with one ``BEGIN_SCAN <n> ... END_SCAN <n>`` sub-block per
    curve (PDD, inplane profile, crossplane profile, ...). Older/simpler
    exports may instead use one ``BEGIN_SCAN_DATA ... END_SCAN_DATA``
    block directly per curve (no nested BEGIN_SCAN) -- that flat layout
    is supported as a fallback.
    """
    n = len(lines)
    curves: list[Curve] = []

    i = 0
    while i < n:
        tokens = lines[i].split()
        if tokens and tokens[0] == "BEGIN_SCAN":
            end_idx = None
            for j in range(i + 1, n):
                t2 = lines[j].split()
                if t2 and t2[0] == "END_SCAN":
                    end_idx = j
                    break
            if end_idx is None:
                i += 1
                continue
            curve = _parse_scan_block(lines[i + 1:end_idx], source)
            if curve is not None:
                curves.append(curve)
            i = end_idx + 1
            continue
        i += 1

    if not curves:
        # Fallback: flat format, one BEGIN_SCAN_DATA/END_SCAN_DATA per curve.
        i = 0
        while i < n:
            if lines[i] == "BEGIN_SCAN_DATA":
                try:
                    end_scan = lines.index("END_SCAN_DATA", i + 1)
                except ValueError:
                    break
                curve = _parse_scan_block(lines[i + 1:end_scan], source)
                if curve is not None:
                    curves.append(curve)
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
# Bulk multi-field-size matrix export (TPS "batch" commissioning download)
# --------------------------------------------------------------------------
#
# Format observed: a metadata header (machine / algorithm / beam data /
# "data: OPD" for PDD or "data: OPP" for profiles / column & row legends),
# followed by one or more data blocks. Each block is a CSV-like matrix:
# the header row lists FIELD SIZES as columns (one number per field side,
# e.g. "100.0" -> a 100x100mm field), each subsequent row is one position
# (depth for PDD, offaxis distance for profiles) with one dose value per
# field-size column. Cells are blank where that particular field size
# wasn't sampled at that exact position (different field sizes are often
# scanned with different position spacing, interleaved into a shared
# position axis).
#
# Profiles additionally repeat this block once per measurement depth,
# each preceded by a "Curves at depth [mm]: <value>" line. PDD exports
# have a single block (no depth marker, since depth IS the row axis).

def _is_bulk_header_row(line: str) -> bool:
    """A bulk-matrix header row: first (unlabeled) column is empty, the
    rest are all parseable numbers (the field sizes)."""
    if "," not in line:
        return False
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 2 or parts[0] != "":
        return False
    try:
        for p in parts[1:]:
            if p != "":
                float(p)
        return True
    except ValueError:
        return False


def is_bulk_matrix_format(lines: list[str]) -> bool:
    """Detect the bulk multi-field-size matrix export by its metadata
    header, regardless of file extension (these exports often have no
    extension at all)."""
    for l in lines[:20]:
        if l.lower().startswith("column legend") or l.lower().startswith("row legend"):
            return True
    return False


def parse_bulk_matrix(lines: list[str], source: str = "") -> list[Curve]:
    """Extract every (field size) curve out of a bulk multi-field-size
    matrix export, across all depth blocks (profiles) or the single
    block (PDD)."""
    n = len(lines)

    data_type = "UNKNOWN"
    origin = "MEASURED"
    algorithm = ""
    for l in lines[:20]:
        ll = l.lower()
        if ll.startswith("data:"):
            val = l.split(":", 1)[1].strip().upper()
            # "OPD"/"OPP" = measured; "OPD_calculated"/"OPP_calculated" = TPS-calculated
            if val.startswith("OPD"):
                data_type = "PDD"
            elif val.startswith("OPP"):
                data_type = "PROFILE"
            if "CALCULATED" in val:
                origin = "CALCULATED"
        elif ll.startswith("algorithm:"):
            algorithm = l.split(":", 1)[1].strip()

    curves: list[Curve] = []
    depth_for_block = None

    idx = 0
    while idx < n:
        line = lines[idx]

        if line.lower().startswith("curves at depth"):
            try:
                depth_for_block = float(line.split(":", 1)[1].strip())
            except ValueError:
                depth_for_block = None
            idx += 1
            continue

        if _is_bulk_header_row(line):
            field_sizes = [p.strip() for p in line.split(",")][1:]

            data_rows = []
            j = idx + 1
            while j < n and lines[j].strip() != "" and not lines[j].lower().startswith("curves at depth"):
                data_rows.append(lines[j])
                j += 1

            columns_data = {k: [] for k in range(len(field_sizes))}
            for row_line in data_rows:
                parts = row_line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    pos = float(parts[0])
                except ValueError:
                    continue
                for k, val_str in enumerate(parts[1:]):
                    if k >= len(field_sizes):
                        break
                    val_str = val_str.strip()
                    if val_str == "":
                        continue
                    try:
                        val = float(val_str)
                    except ValueError:
                        continue
                    columns_data[k].append((pos, val))

            for k, fs in enumerate(field_sizes):
                rows = columns_data.get(k, [])
                if len(rows) < 2:
                    continue
                rows.sort(key=lambda t: t[0])
                arr = _normalize(np.array(rows, dtype=float))

                try:
                    fs_val = float(fs)
                    fs_label = f"{fs_val:g}x{fs_val:g}"
                except ValueError:
                    fs_label = fs

                curves.append(
                    Curve(
                        data=arr,
                        curve_type=data_type,
                        depth_mm=depth_for_block if data_type == "PROFILE" else None,
                        field_size=fs_label,
                        origin=origin,
                        algorithm=algorithm,
                        source=source,
                    )
                )

            idx = j
            continue

        idx += 1

    for i, c in enumerate(curves, start=1):
        c.build_label(i)

    return curves


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------

def parse_file(file_bytes: bytes, filename: str) -> list[Curve]:
    """Parse a commissioning/measurement file into a list of Curves.
    Recognizes three formats: w2CAD (.data), PTW mcc (.mcc), and the
    bulk multi-field-size matrix export (detected by content, since
    those exports often have no file extension at all)."""
    lines = bytes_to_lines(file_bytes)
    lower = filename.lower()

    # Content-based detection first: the bulk matrix format has no
    # reliable extension, so check its metadata header regardless of name.
    if is_bulk_matrix_format(lines):
        return parse_bulk_matrix(lines, source=filename)

    if lower.endswith(".mcc"):
        return parse_mcc(lines, source=filename)
    elif lower.endswith(".data") or lower.endswith(".dat") or lower.endswith(".txt"):
        # Try w2CAD first; if nothing found, try mcc-style as a fallback.
        curves = parse_w2cad(lines, source=filename)
        if not curves:
            curves = parse_mcc(lines, source=filename)
        return curves
    else:
        # Unknown extension: try every parser.
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


def dose_at_depth(pdd: np.ndarray, depth_mm: float = 100.0) -> float:
    """Interpolated dose [%] at a given depth on a PDD curve
    (positions assumed in mm, dose normalized 0-100%). Returns NaN if
    `depth_mm` falls outside the measured range."""
    x, y = pdd[:, 0], pdd[:, 1]
    if depth_mm < np.min(x) or depth_mm > np.max(x):
        return float("nan")
    return float(np.interp(depth_mm, x, y))


# --------------------------------------------------------------------------
# Multi-curve ("per energy") report: matching + PDF generation
# --------------------------------------------------------------------------

def parse_field_size(fs: str):
    """Parse a 'AxB' field-size string (any numeric precision, e.g.
    '100x100' or '400.00x400.00') into a rounded-mm tuple usable for
    matching, or None if it can't be parsed."""
    if not fs:
        return None
    parts = fs.replace(",", ".").lower().replace(" ", "").split("x")
    if len(parts) != 2:
        return None
    try:
        a, b = float(parts[0]), float(parts[1])
        return (round(a), round(b))
    except ValueError:
        return None


def match_curves_by_field(ref_curves, eval_curves, depth_tolerance_mm: float = 0.5):
    """Best-effort auto-matching of commissioning (ref) curves to
    measurement (eval) curves sharing the same curve type (PDD/PROFILE)
    and the same field size (parsed via ``parse_field_size``).

    For PROFILE curves, the measurement depth is also required to match
    (within `depth_tolerance_mm`) whenever both sides carry depth
    metadata -- this matters for bulk commissioning exports that hold
    the same field size at several depths, where matching by field size
    alone could silently pair the wrong depth. If depth metadata is
    missing on either side, matching falls back to field size only.

    Measured CROSSPLANE profile curves (i.e. from the measurement/.mcc
    side) are excluded from auto-matching entirely -- they are never
    considered as candidates, so the corresponding commissioning curve
    (if any) is reported as unmatched rather than paired with a
    crossplane measurement.

    If BOTH a measured commissioning curve and a TPS-calculated curve
    exist for the same field size (and depth, for profiles), both are
    matched against the same measurement curve as separate rows (rather
    than one silently winning based on upload order) -- the matched
    eval curve is only "claimed" per reference origin, so a measured
    and a calculated reference curve can each match it independently.

    Returns
    -------
    matches : list of dict
        Each dict has keys: label, field_size, curve_type, depth_mm,
        origin, algorithm, ref, eval.
    unmatched_ref : list of Curve
        Commissioning curves for which no matching measurement curve was found.
    """
    matches = []
    unmatched_ref = []
    used_eval_keys = set()  # (id(eval_curve), ref.origin)

    for r in ref_curves:
        if r.curve_type not in ("PDD", "PROFILE"):
            continue
        r_fs = parse_field_size(r.field_size)

        candidates = []
        for e in eval_curves:
            if (id(e), r.origin) in used_eval_keys:
                continue
            if e.curve_type != r.curve_type:
                continue
            # Exclude measured CROSSPLANE profiles from auto-matching.
            if e.curve_type == "PROFILE" and e.direction == "CROSSPLANE":
                continue
            e_fs = parse_field_size(e.field_size)
            if r_fs is None or e_fs is None or r_fs != e_fs:
                continue
            candidates.append(e)

        found = None
        if r.curve_type == "PROFILE" and candidates:
            both_have_depth = [e for e in candidates if r.depth_mm is not None and e.depth_mm is not None]
            if both_have_depth:
                depth_matched = [e for e in both_have_depth
                                  if abs(e.depth_mm - r.depth_mm) <= depth_tolerance_mm]
                found = depth_matched[0] if depth_matched else None
            else:
                # Neither side (or only one side) has depth metadata: fall
                # back to matching by field size only, as before.
                found = candidates[0]
        elif candidates:
            found = candidates[0]

        if found is not None:
            used_eval_keys.add((id(found), r.origin))
            depth_note = f" @ {r.depth_mm:g}mm" if r.curve_type == "PROFILE" and r.depth_mm is not None else ""
            if r.origin == "MEASURED":
                origin_note = " (measured)"
            elif r.origin == "CALCULATED":
                origin_note = f" (TPS-calc{f', {r.algorithm}' if r.algorithm else ''})"
            else:
                origin_note = ""
            label = f"{r.field_size or '?'} - {r.curve_type}{depth_note}{origin_note}"
            matches.append({
                "label": label,
                "field_size": r.field_size,
                "curve_type": r.curve_type,
                "depth_mm": r.depth_mm if r.curve_type == "PROFILE" else None,
                "origin": r.origin,
                "algorithm": r.algorithm,
                "ref": r,
                "eval": found,
            })
        else:
            unmatched_ref.append(r)

    return matches, unmatched_ref


def render_comparison_figure(ref_data: np.ndarray, eval_data: np.ndarray,
                              gamma: np.ndarray, curve_type: str, title: str) -> bytes:
    """Render the standard 3-panel (overlay / difference / gamma) comparison
    figure used in the PDF report, returning PNG bytes. Uses a taller
    aspect ratio and larger fonts than a typical on-screen chart so it
    reads well at nearly full page width in print."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import io as _io

    eval_on_ref = np.interp(ref_data[:, 0], eval_data[:, 0], eval_data[:, 1], left=np.nan, right=np.nan)
    difference = ref_data[:, 1] - eval_on_ref

    fig, axes = plt.subplots(1, 3, figsize=(13, 7))

    axes[0].plot(ref_data[:, 0], ref_data[:, 1], label="Commissioning", lw=2.0)
    axes[0].plot(eval_data[:, 0], eval_data[:, 1], label="Measurement", lw=2.0, alpha=0.8)
    axes[0].set_xlabel("Position [mm]", fontsize=11)
    axes[0].set_ylabel("Dose [%]", fontsize=11)
    axes[0].set_title(f"{curve_type} — Overlaid curves", fontsize=12)
    axes[0].grid(alpha=0.3)
    axes[0].tick_params(labelsize=9)
    axes[0].legend(fontsize=10)

    axes[1].plot(ref_data[:, 0], difference, color="crimson", lw=1.8)
    axes[1].axhline(0, color="k", lw=0.8, alpha=0.5)
    axes[1].set_xlabel("Position [mm]", fontsize=11)
    axes[1].set_ylabel("Difference [%]", fontsize=11)
    axes[1].set_title("Difference", fontsize=12)
    axes[1].grid(alpha=0.3)
    axes[1].tick_params(labelsize=9)

    axes[2].plot(gamma[:, 0], gamma[:, 1], color="green", lw=1.4, marker=".", markersize=4)
    axes[2].axhline(1, color="green", ls="--", alpha=0.5)
    axes[2].set_xlabel("Position [mm]", fontsize=11)
    axes[2].set_ylabel("Gamma", fontsize=11)
    axes[2].set_title("Gamma Index", fontsize=12)
    axes[2].grid(alpha=0.3)
    axes[2].tick_params(labelsize=9)

    fig.suptitle(title, fontsize=14)
    fig.tight_layout()

    buf = _io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def render_cumulative_figure(matches: list, curve_type: str, title: str) -> bytes | None:
    """Render a single chart overlaying ALL field sizes of a given curve
    type (PDD or PROFILE), commissioning as solid lines and measurement
    as dashed lines, one color per field size. Returns None if there are
    no curves of that type in `matches`."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import io as _io

    subset = [m for m in matches if m["curve_type"] == curve_type]
    if not subset:
        return None

    fig, ax = plt.subplots(figsize=(11, 5.5))
    # matplotlib.cm.get_cmap() was removed in matplotlib >=3.9; the
    # `matplotlib.colormaps[...]` mapping is the current stable API.
    cmap = matplotlib.colormaps["tab10"] if len(subset) <= 10 else matplotlib.colormaps["tab20"]

    for i, m in enumerate(subset):
        color = cmap(i % cmap.N)
        ref = m["ref"].data
        ev = m["eval"].data
        depth_mm = m.get("depth_mm")
        origin = m.get("origin", "")
        if origin == "MEASURED":
            origin_note = " (meas.)"
        elif origin == "CALCULATED":
            origin_note = " (TPS-calc)"
        else:
            origin_note = ""
        if curve_type == "PROFILE" and depth_mm is not None:
            label = f"{m['field_size'] or f'curve {i + 1}'} @ {depth_mm:g}mm{origin_note}"
        else:
            label = f"{m['field_size'] or f'curve {i + 1}'}{origin_note}"
        ax.plot(ref[:, 0], ref[:, 1], color=color, lw=1.6, linestyle="-", label=label)
        ax.plot(ev[:, 0], ev[:, 1], color=color, lw=1.3, linestyle="--")

    ax.set_xlabel("Position [mm]")
    ax.set_ylabel("Dose [%]")
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.legend(title="Field size (— commissioning / -- measurement)", fontsize=8,
              ncol=2 if len(subset) > 5 else 1, loc="best")
    fig.tight_layout()

    buf = _io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def build_energy_report_pdf(energy_label: str,
                             matches: list,
                             gamma_dose_t: float = 3.0,
                             gamma_dist_t: float = 2.0,
                             gamma_dose_threshold: float = 0.0,
                             gamma_interp: int = 1,
                             pdd_depth_mm: float = 100.0,
                             tolerance_pp: float = 1.0,
                             logo_bytes: bytes = None,
                             physicist_name: str = "") -> bytes:
    """Build a PDF QA report for a set of commissioning-vs-measurement curve
    pairs belonging to the same energy/beam: gamma analysis, PDD
    dose-at-depth or profile flatness/symmetry checks, a summary table,
    cumulative multi-field-size overview charts (one for all PDDs, one for
    all profiles), a per-field-size section with its own (large) comparison
    chart and metrics table, a final approval/signature block, a logo in
    the page header (if `logo_bytes` is provided) and page numbers in the
    footer of every page. Returns the PDF as bytes.
    """
    import io as _io
    from datetime import datetime
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Image as RLImage,
    )
    from PIL import Image as PILImage

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=20, spaceAfter=4)
    meta_style = ParagraphStyle("MetaX", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#666666"))
    h2_style = ParagraphStyle("H2X", parent=styles["Heading2"], spaceBefore=14, spaceAfter=6)
    h3_style = ParagraphStyle("H3X", parent=styles["Heading3"], spaceBefore=10, spaceAfter=4)
    note_style = ParagraphStyle("NoteX", parent=styles["Normal"], fontSize=8, textColor=colors.HexColor("#666666"), italic=True)
    cell_style = ParagraphStyle("CellX", parent=styles["Normal"], fontSize=7.5, leading=9)
    cell_header_style = ParagraphStyle("CellHX", parent=cell_style, textColor=colors.white, fontName="Helvetica-Bold")
    mcell_style = ParagraphStyle("MCellX", parent=styles["Normal"], fontSize=8.5, leading=10)
    mcell_header_style = ParagraphStyle("MCellHX", parent=mcell_style, textColor=colors.white, fontName="Helvetica-Bold")

    PASS_BG = colors.HexColor("#C6EFCE")
    FAIL_BG = colors.HexColor("#FFC7CE")
    HEADER_BG = colors.HexColor("#1F4E78")

    TOP_MARGIN = 30 * mm if logo_bytes else 20 * mm
    content_width = A4[0] - 2 * 18 * mm

    def _png_flowable(png_bytes, max_width):
        im = PILImage.open(_io.BytesIO(png_bytes))
        iw, ih = im.size
        aspect = ih / iw
        return RLImage(_io.BytesIO(png_bytes), width=max_width, height=max_width * aspect)

    def _cell(text, style):
        return Paragraph(str(text), style)

    story = []

    # -- Cover / header -------------------------------------------------
    story.append(Paragraph(f"QA Report — {energy_label}", title_style))
    story.append(Paragraph(f"Generated on {datetime.now().strftime('%d/%m/%Y %H:%M')}", meta_style))
    story.append(Paragraph(
        f"Gamma parameters: Dose {gamma_dose_t:g}% | DTA {gamma_dist_t:g}mm | "
        f"Threshold {gamma_dose_threshold:g}% | Interp. {gamma_interp} | "
        f"Main tolerance ±{tolerance_pp:g}%", meta_style))
    story.append(Spacer(1, 10))

    # -- Precompute gamma / metrics for every match ----------------------
    # Order: all PDDs first, then all profiles, each group sorted by field
    # size (profiles further sorted by depth within the same field size).
    def _field_size_sort_key(fs_str):
        parsed = parse_field_size(fs_str)
        return parsed if parsed is not None else (float("inf"), float("inf"))

    matches = sorted(
        matches,
        key=lambda m: (
            0 if m["curve_type"] == "PDD" else 1,
            _field_size_sort_key(m["field_size"]),
            m.get("depth_mm") if m.get("depth_mm") is not None else -1,
        ),
    )

    results = []
    for m in matches:
        ref_curve = m["ref"]
        eval_curve = m["eval"]
        curve_type = m["curve_type"]
        field_size = m["field_size"] or "?"
        depth_mm = m.get("depth_mm")
        origin = m.get("origin", "")
        algorithm = m.get("algorithm", "")
        if origin == "MEASURED":
            origin_note = " (measured)"
        elif origin == "CALCULATED":
            origin_note = f" (TPS-calc{f', {algorithm}' if algorithm else ''})"
        else:
            origin_note = ""
        display_label = f"{field_size} @ {depth_mm:g}mm" if (curve_type == "PROFILE" and depth_mm is not None) else field_size
        display_label += origin_note

        gamma, gamma_percent, evaluated_points = gamma_1d(
            ref_curve.data, eval_curve.data,
            dose_t=gamma_dose_t, dist_t=gamma_dist_t,
            dose_threshold=gamma_dose_threshold, interp=int(gamma_interp),
        )

        main_pass = None
        detail_text = ""
        metric_rows = None  # list of (param, ref, eval, diff_str, verdict_str_or_None)

        if curve_type == "PDD":
            ref_d = dose_at_depth(ref_curve.data, pdd_depth_mm)
            eval_d = dose_at_depth(eval_curve.data, pdd_depth_mm)
            if np.isnan(ref_d) or np.isnan(eval_d):
                detail_text = "Depth out of range"
                metric_rows = [(f"Dose @ {pdd_depth_mm:g}mm", "N/A", "N/A", "N/A", None)]
            else:
                diff_pp = eval_d - ref_d
                main_pass = abs(diff_pp) <= tolerance_pp
                detail_text = f"D{pdd_depth_mm:g}mm: {diff_pp:+.2f}pp"
                metric_rows = [(f"Dose @ {pdd_depth_mm:g}mm", f"{ref_d:.3f}", f"{eval_d:.3f}",
                                 f"{diff_pp:+.2f} pp", main_pass)]
        else:  # PROFILE
            try:
                ref_metrics = profile_metrics(ref_curve.data)
                eval_metrics = profile_metrics(eval_curve.data)
                ref_shape = ref_metrics.pop("_shape")
                eval_shape = eval_metrics.pop("_shape")

                TOLERANCE_KEYS = ("Flatness [%]", "Symmetry [%]")
                tolerance_ok = True
                any_checked = False
                metric_rows = []
                for key in ref_metrics:
                    rv, ev = ref_metrics[key], eval_metrics[key]
                    rv_nan = isinstance(rv, float) and np.isnan(rv)
                    ev_nan = isinstance(ev, float) and np.isnan(ev)
                    if rv_nan or ev_nan:
                        metric_rows.append((key, "N/A", "N/A", "N/A", None))
                        continue
                    if key.endswith("[%]"):
                        diff = ev - rv
                        diff_str = f"{diff:+.2f} pp"
                    elif key == "Center [mm]":
                        diff = ev - rv
                        diff_str = f"{diff:+.2f} mm"
                    else:
                        diff = 100.0 * (ev - rv) / rv if rv else float("nan")
                        diff_str = f"{diff:+.2f}%"
                    verdict = None
                    if key in TOLERANCE_KEYS:
                        any_checked = True
                        verdict = abs(diff) <= tolerance_pp
                        tolerance_ok = tolerance_ok and verdict
                    metric_rows.append((key, f"{rv:.3f}", f"{ev:.3f}", diff_str, verdict))

                main_pass = tolerance_ok if any_checked else None
                detail_text = "Flatness/Symmetry " + (
                    "OK" if main_pass else "OUT OF TOL." if main_pass is not None else "N/A")
                if ref_shape != "full" or eval_shape != "full":
                    detail_text += " (partial profile)"
            except ValueError as e:
                metric_rows = [(f"Error: {e}", "", "", "", None)]
                detail_text = "Error computing metrics"

        results.append({
            "field_size": field_size, "display_label": display_label, "curve_type": curve_type,
            "ref_curve": ref_curve, "eval_curve": eval_curve,
            "gamma": gamma, "gamma_percent": gamma_percent, "evaluated_points": evaluated_points,
            "main_pass": main_pass, "detail_text": detail_text,
            "metric_rows": metric_rows,
        })

    # -- Summary table ------------------------------------------------------
    # Every cell is wrapped in a Paragraph so long filenames/labels wrap
    # onto multiple lines within their column instead of overlapping
    # neighbouring cells.
    story.append(Paragraph("Summary", h2_style))
    header_cells = ["Field size", "Type", "Commissioning", "Measurement", "Gamma [%]", "Verdict", "Details"]
    table_data = [[_cell(h, cell_header_style) for h in header_cells]]
    row_colors = []
    for res in results:
        verdict_str = "N/A" if res["main_pass"] is None else ("OK" if res["main_pass"] else "OUT OF TOL.")
        gp = res["gamma_percent"]
        gp_str = "N/A" if np.isnan(gp) else f"{gp:.1f}"
        table_data.append([
            _cell(res["display_label"], cell_style),
            _cell(res["curve_type"], cell_style),
            _cell(res["ref_curve"].source, cell_style),
            _cell(res["eval_curve"].source, cell_style),
            _cell(gp_str, cell_style),
            _cell(verdict_str, cell_style),
            _cell(res["detail_text"], cell_style),
        ])
        row_colors.append(res["main_pass"])

    summary_table = Table(table_data, repeatRows=1, hAlign="LEFT",
                           colWidths=[20 * mm, 15 * mm, 30 * mm, 30 * mm, 16 * mm, 20 * mm, 43 * mm])
    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7B7B7")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F5F5")]),
    ])
    for i, pass_val in enumerate(row_colors, start=1):
        if pass_val is True:
            ts.add("BACKGROUND", (5, i), (5, i), PASS_BG)
        elif pass_val is False:
            ts.add("BACKGROUND", (5, i), (5, i), FAIL_BG)
    summary_table.setStyle(ts)
    story.append(summary_table)

    # -- Cumulative overview charts ---------------------------------------
    cum_pdd_png = render_cumulative_figure(matches, "PDD", f"{energy_label} — All PDD field sizes")
    cum_profile_png = render_cumulative_figure(matches, "PROFILE", f"{energy_label} — All profile field sizes")

    if cum_pdd_png or cum_profile_png:
        story.append(PageBreak())
        story.append(Paragraph("Cumulative charts (all field sizes)", h2_style))
        if cum_pdd_png:
            story.append(Paragraph("PDD — all field sizes", h3_style))
            story.append(_png_flowable(cum_pdd_png, content_width))
            story.append(Spacer(1, 8))
        if cum_profile_png:
            story.append(Paragraph("Profiles — all field sizes", h3_style))
            story.append(_png_flowable(cum_profile_png, content_width))

    # -- Per-field-size sections (large chart, minimal wasted space) --------
    gamma_result_style = ParagraphStyle("GammaResultX", parent=styles["Normal"], fontSize=10.5, spaceAfter=4)

    for res in results:
        story.append(PageBreak())
        title = f"{res['display_label']} — {res['curve_type']}"
        story.append(Paragraph(title, h2_style))
        story.append(Paragraph(
            f"Commissioning: {res['ref_curve'].source} — Measurement: {res['eval_curve'].source}", meta_style))
        story.append(Spacer(1, 4))

        gp = res["gamma_percent"]
        gp_str = "N/A" if np.isnan(gp) else f"{gp:.1f}%"
        gp_ok = (not np.isnan(gp)) and gp >= 95.0
        gp_color_hex = "#2E7D32" if gp_ok else "#C62828"
        story.append(Paragraph(
            f'Gamma Index ({gamma_dose_t:g}%/{gamma_dist_t:g}mm): '
            f'<font color="{gp_color_hex}"><b>{gp_str}</b></font> '
            f'({res["evaluated_points"]} points evaluated)',
            gamma_result_style))
        story.append(Spacer(1, 4))

        fig_title = f"{res['display_label']} — {res['curve_type']}"
        png_bytes = render_comparison_figure(res["ref_curve"].data, res["eval_curve"].data,
                                              res["gamma"], res["curve_type"], fig_title)
        story.append(_png_flowable(png_bytes, content_width))
        story.append(Spacer(1, 10))

        mt_header = ["Parameter", "Commissioning", "Measurement", "Difference", f"Within ±{tolerance_pp:g}%"]
        mt_data = [[_cell(h, mcell_header_style) for h in mt_header]]
        mt_verdicts = []
        for param, rv, ev, diff_str, verdict in res["metric_rows"]:
            verdict_str = "—" if verdict is None else ("OK" if verdict else "OUT OF TOL.")
            mt_data.append([_cell(param, mcell_style), _cell(rv, mcell_style), _cell(ev, mcell_style),
                             _cell(diff_str, mcell_style), _cell(verdict_str, mcell_style)])
            mt_verdicts.append(verdict)

        mt = Table(mt_data, hAlign="LEFT", colWidths=[45 * mm, 30 * mm, 30 * mm, 30 * mm, 30 * mm])
        mts = TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B7B7B7")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ])
        for i, verdict in enumerate(mt_verdicts, start=1):
            if verdict is True:
                mts.add("BACKGROUND", (4, i), (4, i), PASS_BG)
            elif verdict is False:
                mts.add("BACKGROUND", (4, i), (4, i), FAIL_BG)
        mt.setStyle(mts)
        story.append(mt)

        if "partial profile" in res["detail_text"]:
            story.append(Spacer(1, 6))
            story.append(Paragraph(
                "Note: a partial profile was detected — field size/flatness are estimated "
                "assuming a symmetric field; symmetry cannot be verified.", note_style))

    # -- Approval / signature block -----------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Approval", h2_style))
    story.append(Spacer(1, 30))

    sign_label_style = ParagraphStyle("SignLabelX", parent=styles["Normal"], fontSize=11)
    physicist_line = physicist_name if physicist_name else ""
    approval_table = Table(
        [
            [Paragraph("Date:", sign_label_style), ""],
            [Paragraph("Medical Physicist Signature:", sign_label_style),
             Paragraph(physicist_line, sign_label_style)],
        ],
        colWidths=[75 * mm, 95 * mm],
        rowHeights=[18 * mm, 18 * mm],
    )
    approval_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, 0), 0.8, colors.black),
        ("LINEBELOW", (1, 1), (1, 1), 0.8, colors.black),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(approval_table)

    # -- Page header (logo) and footer (title + page numbers) on every page --
    footer_title = f"QA Report - {energy_label}"

    def _draw_header_footer(canvas: Canvas, doc):
        width, height = A4
        canvas.saveState()

        if logo_bytes:
            try:
                img_reader = ImageReader(_io.BytesIO(logo_bytes))
                iw, ih = img_reader.getSize()
                logo_h = 16 * mm
                logo_w = logo_h * (iw / ih)
                canvas.drawImage(img_reader, (width - logo_w) / 2, height - 12 * mm - logo_h,
                                  width=logo_w, height=logo_h, mask="auto", preserveAspectRatio=True)
            except Exception:
                pass

        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(18 * mm, 10 * mm, footer_title)
        canvas.drawRightString(width - 18 * mm, 10 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    buf = _io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=TOP_MARGIN, bottomMargin=16 * mm,
        title=f"QA Report - {energy_label}",
    )
    doc.build(story, onFirstPage=_draw_header_footer, onLaterPages=_draw_header_footer)
    buf.seek(0)
    return buf.getvalue()



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


def _nearest_index(x: np.ndarray, value: float = 0.0) -> int:
    return int(np.argmin(np.abs(x - value)))


def detect_profile_shape(profile: np.ndarray, edge_fraction: float = 0.15) -> str:
    """Detect whether a profile covers the full field (both sides of the
    central axis, conventionally x=0 in these file formats) or only one
    side ("half profile" -- a common commissioning shortcut that assumes
    a symmetric field and only scans from the central axis out to one
    edge).

    Heuristic: compares how far the measured positions extend into
    negative x vs positive x. If one side has negligible extent relative
    to the other, only that other side was actually measured.

    Note: this deliberately does NOT use the dose peak position, because
    real beams can have a "horn" (off-axis dose maximum) so the peak is
    not always at the true central axis -- using it as a proxy for "is
    this a half scan" is unreliable. The geometric position x=0 is used
    instead, consistent with the central-axis convention observed in
    both w2CAD and PTW mcc exports.

    Returns
    -------
    'full', 'right' (only the positive/right side was measured) or
    'left' (only the negative/left side was measured).
    """
    x = profile[:, 0]
    xmin, xmax = float(np.min(x)), float(np.max(x))
    neg_span = max(0.0, -xmin)
    pos_span = max(0.0, xmax)
    total_span = neg_span + pos_span
    if total_span <= 0:
        return "full"
    if neg_span / total_span <= edge_fraction:
        return "right"
    if pos_span / total_span <= edge_fraction:
        return "left"
    return "full"


def field_edges(profile: np.ndarray, level_percent: float = 50.0):
    """(left_edge, right_edge) positions where the profile crosses
    `level_percent` of its maximum dose. For a half profile (see
    ``detect_profile_shape``), the missing edge is estimated by mirroring
    the measured edge around the central axis (x=0), assuming a
    symmetric field."""
    x, y = profile[:, 0], profile[:, 1]
    level = np.max(y) * level_percent / 100.0
    shape = detect_profile_shape(profile)

    if shape == "full":
        peak_idx = int(np.argmax(y))
        left = _find_crossing(x, y, level, "left", peak_idx)
        right = _find_crossing(x, y, level, "right", peak_idx)
    else:
        center_idx = _nearest_index(x, 0.0)
        center_x = x[center_idx]
        if shape == "right":
            right = _find_crossing(x, y, level, "right", center_idx)
            left = 2 * center_x - right
        else:
            left = _find_crossing(x, y, level, "left", center_idx)
            right = 2 * center_x - left
    return left, right


def penumbra(profile: np.ndarray, low: float = 20.0, high: float = 80.0):
    """(left_penumbra_mm, right_penumbra_mm): distance between the
    `low`% and `high`% dose levels at each field edge (default 20-80%).
    For a half profile, the penumbra of the un-measured side cannot be
    determined and is returned as NaN."""
    x, y = profile[:, 0], profile[:, 1]
    ymax = np.max(y)
    shape = detect_profile_shape(profile)
    ref_idx = int(np.argmax(y)) if shape == "full" else _nearest_index(x, 0.0)

    if shape in ("full", "left"):
        l_high = _find_crossing(x, y, high / 100.0 * ymax, "left", ref_idx)
        l_low = _find_crossing(x, y, low / 100.0 * ymax, "left", ref_idx)
        left_pen = abs(l_low - l_high)
    else:
        left_pen = float("nan")

    if shape in ("full", "right"):
        r_high = _find_crossing(x, y, high / 100.0 * ymax, "right", ref_idx)
        r_low = _find_crossing(x, y, low / 100.0 * ymax, "right", ref_idx)
        right_pen = abs(r_low - r_high)
    else:
        right_pen = float("nan")

    return left_pen, right_pen


def flatness_symmetry(profile: np.ndarray, field_level: float = 50.0,
                       central_fraction: float = 0.8):
    """Flatness (IEC-style, (Dmax-Dmin)/(Dmax+Dmin)*100) and symmetry
    (point-to-point mirrored dose difference, %) computed over the
    central `central_fraction` of the field width (default 80%).

    Half profiles (only one side of the central axis measured -- see
    ``detect_profile_shape``): the field width/central region is
    estimated by mirroring the measured edge around the central axis
    (x=0, assumes a symmetric field -- the usual reason a half scan was
    taken in the first place). Flatness is then computed from the
    available side only (equivalent to the full-profile result under
    the symmetry assumption). Symmetry itself cannot be verified from a
    single side and is returned as NaN.
    """
    x, y = profile[:, 0], profile[:, 1]
    shape = detect_profile_shape(profile)

    left, right = field_edges(profile, field_level)
    field_size = right - left
    margin = (1 - central_fraction) / 2 * field_size

    if shape == "full":
        xin, xout = left + margin, right - margin
        mask = (x >= xin) & (x <= xout)
    else:
        center_x = x[_nearest_index(x, 0.0)]
        if shape == "right":
            xin, xout = center_x, right - margin
        else:  # 'left'
            xin, xout = left + margin, center_x
        mask = (x >= xin) & (x <= xout)

    if mask.sum() < 2:
        raise ValueError("Not enough points in the central field region "
                          "to compute flatness/symmetry.")

    Dmax = float(np.max(y[mask]))
    Dmin = float(np.min(y[mask]))
    flatness = 100.0 * (Dmax - Dmin) / (Dmax + Dmin)

    if shape == "full":
        center = (left + right) / 2.0
        x_central = x[mask]
        y_central = y[mask]
        y_mirror = np.interp(2 * center - x_central, x, y)
        D_center = float(np.interp(center, x, y))
        symmetry = 100.0 * float(np.max(np.abs(y_central - y_mirror))) / D_center if D_center else float("nan")
    else:
        # Center offset and symmetry cannot be verified with only one
        # side of the profile measured; report the nominal central axis.
        center = x[_nearest_index(x, 0.0)]
        symmetry = float("nan")

    return flatness, symmetry, field_size, center, shape


def profile_metrics(profile: np.ndarray) -> dict:
    """Compute flatness, symmetry, field size, center and penumbra
    (left/right) for a normalized dose profile (N,2) array. Works for
    both full and half (single-side) profiles -- see
    ``detect_profile_shape``. For half profiles, symmetry (and the
    penumbra of the un-measured side) are returned as NaN."""
    flatness, symmetry, field_size, center, shape = flatness_symmetry(profile)
    left_pen, right_pen = penumbra(profile)
    return {
        "Flatness [%]": flatness,
        "Symmetry [%]": symmetry,
        "Field size [mm]": field_size,
        "Center [mm]": center,
        "Left penumbra [mm]": left_pen,
        "Right penumbra [mm]": right_pen,
        "_shape": shape,
    }
