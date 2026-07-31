# -*- coding: utf-8 -*-
"""
Streamlit app for 1D relative-dose QA.

Workflow
--------
1. Upload a commissioning file (w2CAD ``.data``, TPS export) and a
   measurement file (PTW ``.mcc``, Verisoft export).
2. Each file may contain several curves (different field sizes, depths,
   PDD/profile, inplane/crossplane...): pick the curve to compare from
   each file.
3. Run the analysis:
     * Gamma index (dose difference / DTA / threshold / interpolation
       are all configurable) -- available for both PDD and profiles.
     * For profiles only: flatness, symmetry and penumbra are computed
       for both curves and compared against a +/-1% tolerance.
"""

from __future__ import annotations

import os
import streamlit as st
import numpy as np
import matplotlib.pyplot as plt

from dose_tools import (
    parse_file, gamma_1d, profile_metrics, dose_at_depth, Curve,
    match_curves_by_field, build_energy_report_pdf,
)

st.set_page_config(page_title="Relative Dose 1D - Commissioning QA", layout="wide")

# --------------------------------------------------------------------
# Fixed logo: drop a file named logo.png/.jpg/.jpeg into an "assets"
# folder next to this script and it will be picked up automatically
# for the PDF report header -- no need to upload it each time.
# --------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
_LOGO_CANDIDATES = ["logo.png", "logo.jpg", "logo.jpeg"]


def _load_fixed_logo() -> bytes | None:
    for name in _LOGO_CANDIDATES:
        path = os.path.join(APP_DIR, "assets", name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
    return None


FIXED_LOGO_BYTES = _load_fixed_logo()

st.title("Relative Dose 1D — Commissioning vs Measurement QA")
st.caption(
    "Upload one or more **reference** files — measured commissioning (w2CAD `.data`, "
    "measured bulk multi-field) and/or curves **calculated by the TPS** (bulk multi-field "
    "`_calculated`, e.g. Acuros/AAA) — and one or more **measurement** files (PTW `.mcc` "
    "format). All curves found in the uploaded files are collected into a single list to "
    "choose what to compare. The comparison includes the gamma analysis (PDD and profiles) "
    "and, for profiles only, flatness / symmetry / penumbra with a ±1% tolerance check."
)

# --------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------
if "commissioning_curves" not in st.session_state:
    st.session_state.commissioning_curves = []
if "measurement_curves" not in st.session_state:
    st.session_state.measurement_curves = []


# --------------------------------------------------------------------
# File upload
# --------------------------------------------------------------------
col_up1, col_up2 = st.columns(2)

with col_up1:
    st.subheader("1️⃣ Commissioning / TPS")
    commissioning_files = st.file_uploader(
        "Reference files: measured commissioning (w2CAD .data, measured bulk) "
        "and/or TPS-calculated curves (bulk `_calculated`, e.g. Acuros/AAA) — can be mixed",
        type=None,
        key="commissioning_upload", accept_multiple_files=True,
    )
    if commissioning_files:
        curves = []
        errors = []
        for f in commissioning_files:
            file_curves = parse_file(f.getvalue(), f.name)
            if file_curves:
                curves.extend(file_curves)
            else:
                errors.append(f.name)
        st.session_state.commissioning_curves = curves
        if curves:
            n_calc = sum(1 for c in curves if c.origin == "CALCULATED")
            n_meas = len(curves) - n_calc
            detail = []
            if n_meas:
                detail.append(f"{n_meas} measured")
            if n_calc:
                detail.append(f"{n_calc} TPS-calculated")
            st.success(
                f"{len(curves)} curve(s) found in {len(commissioning_files)} file(s) "
                f"({', '.join(detail)})."
            )
        if errors:
            st.error("No curve recognized in: " + ", ".join(errors))
    else:
        st.session_state.commissioning_curves = []

with col_up2:
    st.subheader("2️⃣ Measurement (.mcc)")
    measurement_files = st.file_uploader(
        "Measurement files (PTW)", type=["mcc"],
        key="measurement_upload", accept_multiple_files=True,
    )
    if measurement_files:
        curves = []
        errors = []
        for f in measurement_files:
            file_curves = parse_file(f.getvalue(), f.name)
            if file_curves:
                curves.extend(file_curves)
            else:
                errors.append(f.name)
        st.session_state.measurement_curves = curves
        if curves:
            st.success(f"{len(curves)} curve(s) found in {len(measurement_files)} measurement file(s).")
        if errors:
            st.error("No curve recognized in: " + ", ".join(errors))
    else:
        st.session_state.measurement_curves = []


# --------------------------------------------------------------------
# Curve selection
# --------------------------------------------------------------------
ref_curves: list[Curve] = st.session_state.commissioning_curves
eval_curves: list[Curve] = st.session_state.measurement_curves

if ref_curves and eval_curves:
    st.subheader("3️⃣ Select the curves to compare")

    col_sel1, col_sel2 = st.columns(2)
    with col_sel1:
        ref_labels = [f"{c.source} — {c.label}" for c in ref_curves]
        ref_idx = st.selectbox("Commissioning curve (reference)", range(len(ref_labels)),
                                format_func=lambda i: ref_labels[i])
        ref_curve = ref_curves[ref_idx]

    with col_sel2:
        eval_labels = [f"{c.source} — {c.label}" for c in eval_curves]
        eval_idx = st.selectbox("Measurement curve (to evaluate)", range(len(eval_labels)),
                                 format_func=lambda i: eval_labels[i])
        eval_curve = eval_curves[eval_idx]

    # Let the user confirm/override the curve type used for the analysis.
    detected_type = ref_curve.curve_type if ref_curve.curve_type in ("PDD", "PROFILE") else "PROFILE"
    curve_type = st.radio(
        "Curve type for the analysis",
        options=["PDD", "PROFILE"],
        index=0 if detected_type == "PDD" else 1,
        horizontal=True,
        help="Automatically detected from the file metadata; you can correct it if needed.",
    )

    if ref_curve.curve_type != eval_curve.curve_type:
        st.warning(
            f"Warning: the commissioning curve was recognized as "
            f"**{ref_curve.curve_type}** while the measurement curve as "
            f"**{eval_curve.curve_type}**. Check that you selected the correct pair."
        )

    # ------------------------------------------------------------
    # Gamma parameters
    # ------------------------------------------------------------
    st.subheader("4️⃣ Gamma analysis parameters")
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        dose_t = st.number_input("Dose [%]", value=2.0, min_value=0.1, step=0.5)
    with g2:
        dist_t = st.number_input("DTA [mm]", value=2.0, min_value=0.1, step=0.5)
    with g3:
        dose_threshold = st.number_input("Dose threshold [%]", value=0.0, min_value=0.0, step=1.0)
    with g4:
        interp = st.number_input("Interpolated points", value=5, min_value=0, step=1)

    run = st.button("▶️ Run analysis", type="primary")

    if run:
        # ------------------------------------------------------------
        # Gamma analysis (PDD or PROFILE)
        # ------------------------------------------------------------
        gamma, gamma_percent, evaluated_points = gamma_1d(
            ref_curve.data, eval_curve.data,
            dose_t=dose_t, dist_t=dist_t,
            dose_threshold=dose_threshold, interp=int(interp),
        )

        eval_on_ref_positions = np.interp(
            ref_curve.data[:, 0], eval_curve.data[:, 0], eval_curve.data[:, 1], left=np.nan, right=np.nan
        )
        difference = ref_curve.data[:, 1] - eval_on_ref_positions

        st.subheader("📈 Results — Gamma Analysis")

        fig, axes = plt.subplots(1, 3, figsize=(16, 4.2))

        axes[0].plot(ref_curve.data[:, 0], ref_curve.data[:, 1], label="Commissioning", lw=1.8)
        axes[0].plot(eval_curve.data[:, 0], eval_curve.data[:, 1], label="Measurement", lw=1.8, alpha=0.8)
        axes[0].set_xlabel("Position [mm]")
        axes[0].set_ylabel("Dose [%]")
        axes[0].set_title(f"{curve_type} — Overlaid curves")
        axes[0].grid(alpha=0.3)
        axes[0].legend()

        axes[1].plot(ref_curve.data[:, 0], difference, color="crimson", lw=1.5)
        axes[1].axhline(0, color="k", lw=0.7, alpha=0.5)
        axes[1].set_xlabel("Position [mm]")
        axes[1].set_ylabel("Difference [%]")
        axes[1].set_title("Difference (Commissioning − Measurement)")
        axes[1].grid(alpha=0.3)

        axes[2].plot(gamma[:, 0], gamma[:, 1], color="green", lw=1.2, marker=".")
        axes[2].axhline(1, color="green", ls="--", alpha=0.5)
        axes[2].set_xlabel("Position [mm]")
        axes[2].set_ylabel("Gamma")
        axes[2].set_title("Gamma Index")
        axes[2].grid(alpha=0.3)

        fig.tight_layout()
        st.pyplot(fig)

        m1, m2, m3 = st.columns(3)
        m1.metric("Gamma pass rate", f"{gamma_percent:.1f}%")
        m2.metric("Total points", f"{ref_curve.data.shape[0]}")
        m3.metric("Evaluated points", f"{evaluated_points}")

        if gamma_percent >= 95:
            st.success(f"Pass rate {gamma_percent:.1f}% — criterion {dose_t:g}%/{dist_t:g}mm passed (≥95%).")
        else:
            st.error(f"Pass rate {gamma_percent:.1f}% — criterion {dose_t:g}%/{dist_t:g}mm NOT passed (<95%).")

        # ------------------------------------------------------------
        # Profile-only metrics: flatness, symmetry, penumbra
        # ------------------------------------------------------------
        if curve_type == "PROFILE":
            st.subheader("📐 Results — Flatness / Symmetry / Penumbra")

            # Parameters subject to the ±1% tolerance check.
            TOLERANCE_KEYS = ("Flatness [%]", "Symmetry [%]")
            TOLERANCE_PP = 1.0  # percentage points

            SHAPE_LABEL = {
                "full": "full profile",
                "left": "partial profile (left/negative side only)",
                "right": "partial profile (right/positive side only)",
            }

            try:
                ref_metrics = profile_metrics(ref_curve.data)
                eval_metrics = profile_metrics(eval_curve.data)

                ref_shape = ref_metrics.pop("_shape")
                eval_shape = eval_metrics.pop("_shape")

                if ref_shape != "full" or eval_shape != "full":
                    st.info(
                        f"ℹ️ Commissioning: **{SHAPE_LABEL[ref_shape]}** — Measurement: "
                        f"**{SHAPE_LABEL[eval_shape]}**. For partial profiles, field size, "
                        "flatness and penumbra are estimated assuming a symmetric field about "
                        "the central axis; symmetry cannot be computed from a single side and "
                        "is reported as N/A."
                    )

                rows = []
                tolerance_ok = True
                for key in ref_metrics:
                    ref_val = ref_metrics[key]
                    eval_val = eval_metrics[key]

                    ref_is_nan = ref_val is None or (isinstance(ref_val, float) and np.isnan(ref_val))
                    eval_is_nan = eval_val is None or (isinstance(eval_val, float) and np.isnan(eval_val))

                    if ref_is_nan or eval_is_nan:
                        diff_str = "N/A"
                        verifica_str = "N/A"
                    else:
                        if key.endswith("[%]"):
                            # Flatness/Symmetry are already percentages:
                            # difference expressed in percentage points.
                            diff = eval_val - ref_val
                            diff_str = f"{diff:+.2f} pp"
                        elif key == "Center [mm]":
                            # Center is conventionally ~0 (nominal central
                            # axis), so a relative % is not meaningful here.
                            diff = eval_val - ref_val
                            diff_str = f"{diff:+.2f} mm"
                        else:
                            # mm quantities (field size, penumbra):
                            # difference expressed as relative percent.
                            diff = 100.0 * (eval_val - ref_val) / ref_val if ref_val else float("nan")
                            diff_str = f"{diff:+.2f}%"

                        if key in TOLERANCE_KEYS:
                            tol_ok = abs(diff) <= TOLERANCE_PP
                            tolerance_ok = tolerance_ok and tol_ok
                            verifica_str = "✅" if tol_ok else "❌"
                        else:
                            # Reported for reference only -- no pass/fail check.
                            verifica_str = "—"

                    rows.append({
                        "Parameter": key,
                        "Commissioning": "N/A" if ref_is_nan else round(ref_val, 3),
                        "Measurement": "N/A" if eval_is_nan else round(eval_val, 3),
                        "Difference": diff_str,
                        "Within ±1%": verifica_str,
                    })

                st.dataframe(rows, use_container_width=True, hide_index=True)

                if tolerance_ok:
                    st.success("Flatness and Symmetry are within the ±1% tolerance vs. commissioning "
                               "(N/A excluded from the check).")
                else:
                    st.error("Flatness and/or Symmetry exceed the ±1% tolerance vs. commissioning.")

                # Dedicated alert: field size must match between commissioning
                # and measurement (no tolerance check, just a warning if different).
                ref_fs = ref_metrics["Field size [mm]"]
                eval_fs = eval_metrics["Field size [mm]"]
                fs_diff_percent = 100.0 * (eval_fs - ref_fs) / ref_fs if ref_fs else float("nan")
                if abs(fs_diff_percent) > TOLERANCE_PP:
                    st.warning(
                        f"⚠️ The measured field size ({eval_fs:.1f} mm) differs from the "
                        f"commissioning one ({ref_fs:.1f} mm) by {fs_diff_percent:+.2f}%. "
                        "Check that you selected the correct pair of curves (same field)."
                    )

                st.caption(
                    "Note: the ±1% tolerance is only checked for Flatness and Symmetry (absolute "
                    "difference in percentage points). Field size, Center and Penumbra are reported "
                    "for reference without a tolerance check; a separate warning is shown for Field "
                    "size if the difference vs. commissioning exceeds 1%. Flatness/Symmetry are "
                    "computed per the IEC definition ((Dmax−Dmin)/(Dmax+Dmin)·100) over the central "
                    "volume (80%) of the field; penumbra is the distance between the 80%-20% dose "
                    "levels at the field edges. For partial profiles (only one side measured), a "
                    "symmetric field about the central axis is assumed."
                )

            except ValueError as e:
                st.error(f"Unable to compute the profile parameters: {e}")
        else:
            # ------------------------------------------------------------
            # PDD-only metric: dose at a given depth (default 100 mm)
            # ------------------------------------------------------------
            st.subheader("📏 Results — Dose at a specific depth")

            depth_check = st.number_input(
                "Comparison depth [mm]", value=100.0, min_value=0.0, step=1.0,
                help="Typically 100 mm (maximum/reference dose). Editable for other depths of interest.",
            )

            ref_d = dose_at_depth(ref_curve.data, depth_check)
            eval_d = dose_at_depth(eval_curve.data, depth_check)

            if np.isnan(ref_d) or np.isnan(eval_d):
                st.error(
                    f"Depth {depth_check:g} mm is outside the measured range of at least one "
                    "of the two curves: cannot compute the dose at this depth."
                )
            else:
                diff_pp = eval_d - ref_d
                tol_ok = abs(diff_pp) <= 1.0

                d1, d2, d3 = st.columns(3)
                d1.metric(f"Commissioning @ {depth_check:g}mm", f"{ref_d:.2f}%")
                d2.metric(f"Measurement @ {depth_check:g}mm", f"{eval_d:.2f}%", delta=f"{diff_pp:+.2f} pp")
                d3.metric("Within ±1%", "✅" if tol_ok else "❌")

                if tol_ok:
                    st.success(
                        f"Dose at {depth_check:g}mm within tolerance: difference {diff_pp:+.2f} "
                        "percentage points (≤ ±1%)."
                    )
                else:
                    st.error(
                        f"Dose at {depth_check:g}mm OUT of tolerance: difference {diff_pp:+.2f} "
                        "percentage points (> ±1%)."
                    )

                st.caption(
                    "Note: the ±1% tolerance is applied as the absolute difference in percentage "
                    "points between the (0-100% normalized) dose of commissioning and measurement, "
                    "interpolated at the specified depth."
                )

    # --------------------------------------------------------------------
    # Per-energy report: all curves (PDD + profiles) of the same beam
    # --------------------------------------------------------------------
    st.divider()
    st.subheader("5️⃣ PDF report per energy")
    st.caption(
        "Generate a PDF report with charts and tables for **all** the loaded curves "
        "(PDD and profiles), automatically matching commissioning and measurement by "
        "field size. Includes per-field charts and cumulative charts overlaying all "
        "field sizes (one for PDDs, one for profiles). Useful to collect the full "
        "characterization of an energy/beam in a single document."
    )

    energy_label = st.text_input("Energy / beam name", value="", placeholder="e.g. 6X FFF, 6X, 10X...")

    matches, unmatched_ref = match_curves_by_field(ref_curves, eval_curves)

    if matches:
        preview_rows = [
            {
                "Field size": m["field_size"],
                "Depth [mm]": (f"{m['depth_mm']:g}" if m.get("depth_mm") is not None else "—"),
                "Type": m["curve_type"],
                "Commissioning": m["ref"].source,
                "Measurement": m["eval"].source,
            }
            for m in matches
        ]
        st.write(f"**{len(matches)} pair(s) automatically matched by field size "
                 "(and depth, for profiles):**")
        st.dataframe(preview_rows, use_container_width=True, hide_index=True)
    else:
        st.warning("No commissioning/measurement pair with a matching field size was found.")

    if unmatched_ref:
        unmatched_labels = [f"{c.source} — {c.label}" for c in unmatched_ref]
        st.caption("⚠️ Commissioning curves without a matching measurement (same field size): "
                   + "; ".join(unmatched_labels))

    r1, r2 = st.columns(2)
    with r1:
        report_depth = st.number_input("Depth for the PDD check in the report [mm]", value=100.0, min_value=0.0, step=1.0)
    with r2:
        report_tol = st.number_input("Main tolerance [%]", value=1.0, min_value=0.1, step=0.5)

    r3, r4 = st.columns(2)
    with r3:
        if not FIXED_LOGO_BYTES:
            st.caption(
                "ℹ️ No logo found. To include one in the report, add a "
                "`logo.png` (or `.jpg`/`.jpeg`) file to the `assets/` folder next to "
                "`app.py` and reload the page."
            )
    with r4:
        physicist_name = st.text_input(
            "Medical Physicist name (optional)", value="",
            help="If filled in, it is reported next to the signature line in the report.",
        )

    if matches and st.button("📄 Generate PDF report", type="primary"):
        with st.spinner("Generating report..."):
            report_bytes = build_energy_report_pdf(
                energy_label=energy_label or "Unspecified energy",
                matches=matches,
                gamma_dose_t=dose_t, gamma_dist_t=dist_t,
                gamma_dose_threshold=dose_threshold, gamma_interp=int(interp),
                pdd_depth_mm=report_depth, tolerance_pp=report_tol,
                logo_bytes=FIXED_LOGO_BYTES,
                physicist_name=physicist_name,
            )
        safe_label = "".join(c if c.isalnum() else "_" for c in (energy_label or "report"))
        st.download_button(
            "⬇️ Download PDF report",
            data=report_bytes,
            file_name=f"report_{safe_label}.pdf",
            mime="application/pdf",
        )
        st.success("Report generated. Use the button above to download it.")

else:
    st.info("Upload both sets of files (commissioning and measurement) to proceed.")

with st.expander("ℹ️ Information on supported formats"):
    st.markdown(
        """
- **`.data` (w2CAD)** — typical Eclipse TPS export. Curves are
  delimited by `$STOM ... $ENOM` (profiles) or `$STOD ... $ENOD` (PDD).
- **`.mcc` (PTW Verisoft)** — curves are delimited by
  `BEGIN_SCAN_DATA ... END_SCAN_DATA`, with metadata `SCAN_CURVETYPE`,
  `SCAN_DEPTH`, `FIELD_INPLANE`/`FIELD_CROSSPLANE` and the numeric data
  between `BEGIN_DATA` and `END_DATA`.
- **Bulk multi-field export (TPS, usually without extension)** — a
  single file with **all field sizes** in one matrix: columns =
  field size (mm), rows = depth (PDD) or off-axis distance (profiles).
  For profiles the file may contain several blocks, one per measurement
  depth (`Curves at depth [mm]: ...`). Automatically recognized from the
  content (no extension needed). There are two variants, automatically
  distinguished and reported in the curve label:
  - **measured** (`data: OPD` / `OPP`);
  - **TPS-calculated** (`data: OPD_calculated` / `OPP_calculated`,
    e.g. Acuros/AAA algorithm) — labeled `TPS-calc (algorithm)`,
    useful to compare measurements against the TPS calculation as well
    as against commissioning.
- A single file can contain **several curves** (different fields/depths/
  directions): pick the one you need from the dropdown menus above, or
  use the per-energy report to include them all at once.
        """
    )
