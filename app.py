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

from dose_tools import parse_file, gamma_1d, profile_metrics, Curve

st.set_page_config(page_title="Relative Dose 1D - Commissioning QA", layout="wide")

st.title("Relative Dose 1D — Commissioning vs Measurement QA")
st.caption(
    "Carica il file di **commissioning** (formato w2CAD, `.data`) e il file di "
    "**misura** (formato PTW, `.mcc`). Il confronto include l'analisi gamma "
    "(PDD e profili) e, solo per i profili, flatness / symmetry / penombra "
    "con verifica di tolleranza ±1%."
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
    commissioning_file = st.file_uploader(
        "File di commissioning (w2CAD)", type=["data", "dat", "txt"], key="commissioning_upload"
    )
    if commissioning_file is not None:
        curves = parse_file(commissioning_file.getvalue(), commissioning_file.name)
        st.session_state.commissioning_curves = curves
        if curves:
            st.success(f"{len(curves)} curva/e trovata/e nel file di commissioning.")
        else:
            st.error("Nessuna curva riconosciuta in questo file. Verifica il formato.")

with col_up2:
    st.subheader("2️⃣ Misura (.mcc)")
    measurement_file = st.file_uploader(
        "File di misura (PTW)", type=["mcc"], key="measurement_upload"
    )
    if measurement_file is not None:
        curves = parse_file(measurement_file.getvalue(), measurement_file.name)
        st.session_state.measurement_curves = curves
        if curves:
            st.success(f"{len(curves)} curva/e trovata/e nel file di misura.")
        else:
            st.error("Nessuna curva riconosciuta in questo file. Verifica il formato.")


# --------------------------------------------------------------------
# Curve selection
# --------------------------------------------------------------------
ref_curves: list[Curve] = st.session_state.commissioning_curves
eval_curves: list[Curve] = st.session_state.measurement_curves

if ref_curves and eval_curves:
    st.subheader("3️⃣ Seleziona le curve da confrontare")

    col_sel1, col_sel2 = st.columns(2)
    with col_sel1:
        ref_labels = [c.label for c in ref_curves]
        ref_idx = st.selectbox("Curva di commissioning (riferimento)", range(len(ref_labels)),
                                format_func=lambda i: ref_labels[i])
        ref_curve = ref_curves[ref_idx]

    with col_sel2:
        eval_labels = [c.label for c in eval_curves]
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

            try:
                ref_metrics = profile_metrics(ref_curve.data)
                eval_metrics = profile_metrics(eval_curve.data)

                rows = []
                all_ok = True
                for key in ref_metrics:
                    ref_val = ref_metrics[key]
                    eval_val = eval_metrics[key]

                    if key.endswith("[%]"):
                        # Flatness/Symmetry are already percentages:
                        # compare the absolute difference in percentage points.
                        diff = eval_val - ref_val
                        tol_ok = abs(diff) <= 1.0
                        diff_str = f"{diff:+.2f} pp"
                    else:
                        # mm quantities (field size, center, penumbra):
                        # compare the relative percent difference.
                        diff = 100.0 * (eval_val - ref_val) / ref_val if ref_val else float("nan")
                        tol_ok = abs(diff) <= 1.0
                        diff_str = f"{diff:+.2f}%"

                    all_ok = all_ok and tol_ok
                    rows.append({
                        "Parametro": key,
                        "Commissioning": round(ref_val, 3),
                        "Misura": round(eval_val, 3),
                        "Differenza": diff_str,
                        "Entro ±1%": "✅" if tol_ok else "❌",
                    })

                st.dataframe(rows, use_container_width=True, hide_index=True)

                if all_ok:
                    st.success("Tutti i parametri sono entro la tolleranza ±1% rispetto al commissioning.")
                else:
                    st.error("Uno o più parametri superano la tolleranza ±1% rispetto al commissioning.")

                st.caption(
                    "Nota: per Flatness e Symmetry (già espresse in %) la tolleranza è applicata come "
                    "differenza assoluta in punti percentuali. Per Field size, Center e Penombra (mm) "
                    "la tolleranza è applicata come differenza percentuale relativa al valore di "
                    "commissioning. Flatness/Symmetry sono calcolate come definizione IEC "
                    "((Dmax−Dmin)/(Dmax+Dmin)·100) sul volume centrale (80%) del campo; la penombra è "
                    "la distanza tra i livelli 80%-20% ai bordi del campo."
                )

            except ValueError as e:
                st.error(f"Impossibile calcolare i parametri del profilo: {e}")
        else:
            st.info("Flatness, symmetry e penombra sono calcolate solo per i profili (non per la PDD).")

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
