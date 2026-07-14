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

import streamlit as st
import numpy as np
import matplotlib.pyplot as plt

from dose_tools import parse_file, gamma_1d, profile_metrics, dose_at_depth, Curve

st.set_page_config(page_title="Relative Dose 1D - Commissioning QA", layout="wide")

st.title("Relative Dose 1D — Commissioning vs Measurement QA")
st.caption(
    "Carica uno o più file di **commissioning** (formato w2CAD, `.data`) e uno o più "
    "file di **misura** (formato PTW, `.mcc`). Tutte le curve trovate nei file caricati "
    "vengono raccolte in un unico elenco da cui scegliere cosa confrontare. Il confronto "
    "include l'analisi gamma (PDD e profili) e, solo per i profili, flatness / symmetry / "
    "penombra con verifica di tolleranza ±1%."
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
    st.subheader("1️⃣ Commissioning (.data)")
    commissioning_files = st.file_uploader(
        "File di commissioning (w2CAD)", type=["data", "dat", "txt"],
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
            st.success(f"{len(curves)} curva/e trovata/e in {len(commissioning_files)} file di commissioning.")
        if errors:
            st.error("Nessuna curva riconosciuta in: " + ", ".join(errors))
    else:
        st.session_state.commissioning_curves = []

with col_up2:
    st.subheader("2️⃣ Misura (.mcc)")
    measurement_files = st.file_uploader(
        "File di misura (PTW)", type=["mcc"],
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
            st.success(f"{len(curves)} curva/e trovata/e in {len(measurement_files)} file di misura.")
        if errors:
            st.error("Nessuna curva riconosciuta in: " + ", ".join(errors))
    else:
        st.session_state.measurement_curves = []


# --------------------------------------------------------------------
# Curve selection
# --------------------------------------------------------------------
ref_curves: list[Curve] = st.session_state.commissioning_curves
eval_curves: list[Curve] = st.session_state.measurement_curves

if ref_curves and eval_curves:
    st.subheader("3️⃣ Seleziona le curve da confrontare")

    col_sel1, col_sel2 = st.columns(2)
    with col_sel1:
        ref_labels = [f"{c.source} — {c.label}" for c in ref_curves]
        ref_idx = st.selectbox("Curva di commissioning (riferimento)", range(len(ref_labels)),
                                format_func=lambda i: ref_labels[i])
        ref_curve = ref_curves[ref_idx]

    with col_sel2:
        eval_labels = [f"{c.source} — {c.label}" for c in eval_curves]
        eval_idx = st.selectbox("Curva di misura (da valutare)", range(len(eval_labels)),
                                 format_func=lambda i: eval_labels[i])
        eval_curve = eval_curves[eval_idx]

    # Let the user confirm/override the curve type used for the analysis.
    detected_type = ref_curve.curve_type if ref_curve.curve_type in ("PDD", "PROFILE") else "PROFILE"
    curve_type = st.radio(
        "Tipo di curva per l'analisi",
        options=["PDD", "PROFILE"],
        index=0 if detected_type == "PDD" else 1,
        horizontal=True,
        help="Rilevato automaticamente dai metadati del file; puoi correggerlo se necessario.",
    )

    if ref_curve.curve_type != eval_curve.curve_type:
        st.warning(
            f"Attenzione: la curva di commissioning è stata riconosciuta come "
            f"**{ref_curve.curve_type}** mentre quella di misura come "
            f"**{eval_curve.curve_type}**. Verifica di aver selezionato la coppia corretta."
        )

    # ------------------------------------------------------------
    # Gamma parameters
    # ------------------------------------------------------------
    st.subheader("4️⃣ Parametri analisi gamma")
    g1, g2, g3, g4 = st.columns(4)
    with g1:
        dose_t = st.number_input("Dose [%]", value=3.0, min_value=0.1, step=0.5)
    with g2:
        dist_t = st.number_input("DTA [mm]", value=2.0, min_value=0.1, step=0.5)
    with g3:
        dose_threshold = st.number_input("Soglia dose [%]", value=10.0, min_value=0.0, step=1.0)
    with g4:
        interp = st.number_input("Punti interpolati", value=10, min_value=0, step=1)

    run = st.button("▶️ Esegui analisi", type="primary")

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

        st.subheader("📈 Risultati — Analisi Gamma")

        fig, axes = plt.subplots(1, 3, figsize=(16, 4.2))

        axes[0].plot(ref_curve.data[:, 0], ref_curve.data[:, 1], label="Commissioning", lw=1.8)
        axes[0].plot(eval_curve.data[:, 0], eval_curve.data[:, 1], label="Misura", lw=1.8, alpha=0.8)
        axes[0].set_xlabel("Posizione [mm]")
        axes[0].set_ylabel("Dose [%]")
        axes[0].set_title(f"{curve_type} — Curve sovrapposte")
        axes[0].grid(alpha=0.3)
        axes[0].legend()

        axes[1].plot(ref_curve.data[:, 0], difference, color="crimson", lw=1.5)
        axes[1].axhline(0, color="k", lw=0.7, alpha=0.5)
        axes[1].set_xlabel("Posizione [mm]")
        axes[1].set_ylabel("Differenza [%]")
        axes[1].set_title("Differenza (Commissioning − Misura)")
        axes[1].grid(alpha=0.3)

        axes[2].plot(gamma[:, 0], gamma[:, 1], color="green", lw=1.2, marker=".")
        axes[2].axhline(1, color="green", ls="--", alpha=0.5)
        axes[2].set_xlabel("Posizione [mm]")
        axes[2].set_ylabel("Gamma")
        axes[2].set_title("Indice Gamma")
        axes[2].grid(alpha=0.3)

        fig.tight_layout()
        st.pyplot(fig)

        m1, m2, m3 = st.columns(3)
        m1.metric("Gamma pass rate", f"{gamma_percent:.1f}%")
        m2.metric("Punti totali", f"{ref_curve.data.shape[0]}")
        m3.metric("Punti valutati", f"{evaluated_points}")

        if gamma_percent >= 95:
            st.success(f"Pass rate {gamma_percent:.1f}% — criterio {dose_t:g}%/{dist_t:g}mm superato (≥95%).")
        else:
            st.error(f"Pass rate {gamma_percent:.1f}% — criterio {dose_t:g}%/{dist_t:g}mm NON superato (<95%).")

        # ------------------------------------------------------------
        # Profile-only metrics: flatness, symmetry, penumbra
        # ------------------------------------------------------------
        if curve_type == "PROFILE":
            st.subheader("📐 Risultati — Flatness / Symmetry / Penumbra")

            # Parameters subject to the ±1% tolerance check.
            TOLERANCE_KEYS = ("Flatness [%]", "Symmetry [%]")
            TOLERANCE_PP = 1.0  # percentage points

            SHAPE_LABEL = {
                "full": "profilo completo",
                "left": "profilo parziale (solo lato sinistro/negativo)",
                "right": "profilo parziale (solo lato destro/positivo)",
            }

            try:
                ref_metrics = profile_metrics(ref_curve.data)
                eval_metrics = profile_metrics(eval_curve.data)

                ref_shape = ref_metrics.pop("_shape")
                eval_shape = eval_metrics.pop("_shape")

                if ref_shape != "full" or eval_shape != "full":
                    st.info(
                        f"ℹ️ Commissioning: **{SHAPE_LABEL[ref_shape]}** — Misura: "
                        f"**{SHAPE_LABEL[eval_shape]}**. Per i profili parziali, field size, "
                        "flatness e penombra sono stimati assumendo un campo simmetrico rispetto "
                        "all'asse centrale; la symmetry non è calcolabile da un solo lato e viene "
                        "riportata come N/A."
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
                        "Parametro": key,
                        "Commissioning": "N/A" if ref_is_nan else round(ref_val, 3),
                        "Misura": "N/A" if eval_is_nan else round(eval_val, 3),
                        "Differenza": diff_str,
                        "Verifica ±1%": verifica_str,
                    })

                st.dataframe(rows, use_container_width=True, hide_index=True)

                if tolerance_ok:
                    st.success("Flatness e Symmetry sono entro la tolleranza ±1% rispetto al commissioning "
                               "(N/A escluso dalla verifica).")
                else:
                    st.error("Flatness e/o Symmetry superano la tolleranza ±1% rispetto al commissioning.")

                # Dedicated alert: field size must match between commissioning
                # and measurement (no tolerance check, just a warning if different).
                ref_fs = ref_metrics["Field size [mm]"]
                eval_fs = eval_metrics["Field size [mm]"]
                fs_diff_percent = 100.0 * (eval_fs - ref_fs) / ref_fs if ref_fs else float("nan")
                if abs(fs_diff_percent) > TOLERANCE_PP:
                    st.warning(
                        f"⚠️ Il field size misurato ({eval_fs:.1f} mm) differisce da quello di "
                        f"commissioning ({ref_fs:.1f} mm) di {fs_diff_percent:+.2f}%. "
                        "Verifica di aver selezionato la coppia di curve corretta (stesso campo)."
                    )

                st.caption(
                    "Nota: la tolleranza ±1% è verificata solo per Flatness e Symmetry (differenza "
                    "assoluta in punti percentuali). Field size, Center e Penombra sono riportati per "
                    "riferimento senza verifica di tolleranza; per il Field size viene mostrato un "
                    "avviso separato se la differenza rispetto al commissioning supera l'1%. "
                    "Flatness/Symmetry sono calcolate come definizione IEC "
                    "((Dmax−Dmin)/(Dmax+Dmin)·100) sul volume centrale (80%) del campo; la penombra è "
                    "la distanza tra i livelli 80%-20% ai bordi del campo. Per i profili parziali "
                    "(solo un lato misurato) si assume un campo simmetrico rispetto all'asse centrale."
                )

            except ValueError as e:
                st.error(f"Impossibile calcolare i parametri del profilo: {e}")
        else:
            # ------------------------------------------------------------
            # PDD-only metric: dose at a given depth (default 100 mm)
            # ------------------------------------------------------------
            st.subheader("📏 Risultati — Dose a profondità specifica")

            depth_check = st.number_input(
                "Profondità di confronto [mm]", value=100.0, min_value=0.0, step=1.0,
                help="Tipicamente 100 mm (dose massima/riferimento). Modificabile per altre profondità di interesse.",
            )

            ref_d = dose_at_depth(ref_curve.data, depth_check)
            eval_d = dose_at_depth(eval_curve.data, depth_check)

            if np.isnan(ref_d) or np.isnan(eval_d):
                st.error(
                    f"La profondità {depth_check:g} mm è fuori dal range misurato in almeno una "
                    "delle due curve: impossibile calcolare la dose a questa profondità."
                )
            else:
                diff_pp = eval_d - ref_d
                tol_ok = abs(diff_pp) <= 1.0

                d1, d2, d3 = st.columns(3)
                d1.metric(f"Commissioning @ {depth_check:g}mm", f"{ref_d:.2f}%")
                d2.metric(f"Misura @ {depth_check:g}mm", f"{eval_d:.2f}%", delta=f"{diff_pp:+.2f} pp")
                d3.metric("Entro ±1%", "✅" if tol_ok else "❌")

                if tol_ok:
                    st.success(
                        f"Dose a {depth_check:g}mm entro tolleranza: differenza {diff_pp:+.2f} punti "
                        "percentuali (≤ ±1%)."
                    )
                else:
                    st.error(
                        f"Dose a {depth_check:g}mm FUORI tolleranza: differenza {diff_pp:+.2f} punti "
                        "percentuali (> ±1%)."
                    )

                st.caption(
                    "Nota: la tolleranza ±1% è applicata come differenza assoluta in punti percentuali "
                    "tra la dose (normalizzata 0-100%) di commissioning e di misura, interpolata alla "
                    "profondità indicata."
                )

else:
    st.info("Carica entrambi i file (commissioning e misura) per procedere.")

with st.expander("ℹ️ Informazioni sui formati supportati"):
    st.markdown(
        """
- **`.data` (w2CAD)** — export tipico del TPS Eclipse. Le curve sono
  delimitate da `$STOM ... $ENOM` (profili) o `$STOD ... $ENOD` (PDD).
- **`.mcc` (PTW Verisoft)** — le curve sono delimitate da
  `BEGIN_SCAN_DATA ... END_SCAN_DATA`, con metadati `SCAN_CURVETYPE`,
  `SCAN_DEPTH`, `FIELD_INPLANE`/`FIELD_CROSSPLANE` e i dati numerici tra
  `BEGIN_DATA` e `END_DATA`.
- Un singolo file può contenere **più curve** (campi/profondità/direzioni
  diverse): seleziona quella desiderata dai menu a tendina sopra.
        """
    )
