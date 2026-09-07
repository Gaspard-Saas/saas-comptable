import json
import os
import sqlite3
import time
import urllib.parse
import pandas as pd
import streamlit as st
from google import genai
from google.genai import types
from PIL import Image

# 1. Configuration sécurisée de l'IA Gemini
try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = os.environ.get("GEMINI_API_KEY", "TA_CLE_API_GEMINI")

client = genai.Client(api_key=api_key)


def get_db_connection():
    return sqlite3.connect("comptabilite.db")


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT client_nom FROM transactions LIMIT 1")
    except sqlite3.OperationalError:
        cursor.execute("DROP TABLE IF EXISTS transactions")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_nom TEXT,
        date TEXT,
        montant REAL,
        libelle TEXT,
        justificatif_recu INTEGER DEFAULT 0,
        fournisseur TEXT,
        montant_ht REAL,
        montant_tva REAL
    )
    """)
    cursor.execute("SELECT COUNT(*) FROM transactions")
    if cursor.fetchone()[0] == 0:
        reset_db()
    conn.close()


def reset_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions")
    cursor.executemany(
        """
    INSERT INTO transactions (client_nom, date, montant, libelle, justificatif_recu)
    VALUES (?, ?, ?, ?, ?)
    """,
        [
            ("Boulangerie Dupuis", "2026-08-24", 25.00, "CB AUCHAN RONCQ", 0),
            ("Boulangerie Dupuis", "2026-08-22", 84.50, "CB TOTAL ENERGIES", 0),
            ("Boulangerie Dupuis", "2026-08-20", 142.00, "PRLV Adobe Systems", 0),
            ("Garage Martin", "2026-08-25", 350.00, "PRLV RENAULT PARTS", 0),
            ("Garage Martin", "2026-08-21", 45.00, "CB TOTAL ENERGIES", 0),
        ],
    )
    conn.commit()
    conn.close()


init_db()


def analyser_avec_secours(image, prompt):
    modeles_secours = [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
    ]
    derniere_erreur = None

    for m in modeles_secours:
        try:
            response = client.models.generate_content(
                model=m,
                contents=[image, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                ),
            )
            return json.loads(response.text)
        except Exception as e:
            derniere_erreur = e
            time.sleep(1)
            continue

    raise derniere_erreur


# 2. Interface Streamlit
st.set_page_config(page_title="SaaS Comptable IA", layout="wide")
st.title("🤖 SaaS Comptable - Automation Multi-Clients")

# Sidebar - Gestion du Portefeuille
with st.sidebar:
    st.header("🏢 Portefeuille Client")

    conn = get_db_connection()
    clients_dispo = pd.read_sql(
        "SELECT DISTINCT client_nom FROM transactions", conn
    )["client_nom"].tolist()
    conn.close()

    client_actif = st.selectbox(
        "Sélectionner un dossier :",
        clients_dispo if clients_dispo else ["Aucun client"],
    )

    st.divider()
    st.header("⚙️ Administration")
    if st.button("🔄 Réinitialiser les données de démo"):
        reset_db()
        st.success("Base réinitialisée !")
        st.rerun()

st.caption(f"Dossier sélectionné : **{client_actif}**")

tab1, tab2, tab3 = st.tabs(
    [
        "📊 Rapprochement & Traitement",
        "📥 Import Relevé Bancaire",
        "💬 Relances & Rapports",
    ]
)

# ONGLET 1 : Rapprochement & Traitement
with tab1:
    st.subheader(f"📥 Déposer un justificatif pour {client_actif}")
    uploaded_file = st.file_uploader(
        "Importer un ticket/facture (JPG, PNG)", type=["jpg", "jpeg", "png"]
    )

    if uploaded_file is not None:
        image = Image.open(uploaded_file)
        st.image(image, caption="Aperçu du justificatif", width=250)

        if st.button("🚀 Lancer le rapprochement IA"):
            with st.spinner("Analyse Gemini et rapprochement BDD..."):
                prompt = """Analyse l'image et extrais au format JSON :
                - 'fournisseur' (string)
                - 'date' (AAAA-MM-JJ)
                - 'montant_ht' (float)
                - 'montant_tva' (float)
                - 'montant_ttc' (float)"""

                try:
                    justificatif = analyser_avec_secours(image, prompt)

                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT id, montant, libelle FROM transactions WHERE"
                        " justificatif_recu = 0 AND client_nom = ?",
                        (client_actif,),
                    )
                    attentes = cursor.fetchall()

                    matched = False
                    for t_id, t_montant, t_libelle in attentes:
                        ecart = abs(justificatif["montant_ttc"] - t_montant)
                        nom_ok = (
                            justificatif["fournisseur"].lower()
                            in t_libelle.lower()
                            or t_libelle.lower()
                            in justificatif["fournisseur"].lower()
                        )

                        if ecart < 0.01 and nom_ok:
                            cursor.execute(
                                """
                            UPDATE transactions 
                            SET justificatif_recu = 1, fournisseur = ?, montant_ht = ?, montant_tva = ?
                            WHERE id = ?
                            """,
                                (
                                    justificatif["fournisseur"],
                                    justificatif["montant_ht"],
                                    justificatif["montant_tva"],
                                    t_id,
                                ),
                            )
                            conn.commit()
                            matched = True
                            st.success(
                                f"✅ Rapprochement validé pour {client_actif}"
                                f" (Transaction #{t_id})"
                            )
                            st.balloons()
                            break

                    conn.close()

                    if not matched:
                        st.warning(
                            f"⚠️ Document lu ({justificatif.get('fournisseur')},"
                            f" {justificatif.get('montant_ttc')} €) mais aucun"
                            f" mouvement en attente pour {client_actif}."
                        )

                except Exception as err:
                    st.error(f"Erreur d'analyse : {err}")

    st.divider()
    st.subheader(f"📋 Relevé Bancaire - {client_actif}")

    conn = get_db_connection()
    df = pd.read_sql(
        "SELECT id, date, libelle, montant, justificatif_recu, fournisseur,"
        " montant_ht, montant_tva FROM transactions WHERE client_nom = ?",
        conn,
        params=(client_actif,),
    )
    conn.close()

    df["Statut"] = df["justificatif_recu"].apply(
        lambda x: "✅ Reçu" if x == 1 else "❌ Manquant"
    )
    df_display = df[[
        "id",
        "date",
        "libelle",
        "montant",
        "Statut",
        "fournisseur",
        "montant_ht",
        "montant_tva",
    ]]

    st.dataframe(df_display, use_container_width=True)

    csv_data = df_display.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button(
        label="📁 Télécharger le Journal d'Achats (CSV)",
        data=csv_data,
        file_name=f"journal_achats_{client_actif.replace(' ', '_')}.csv",
        mime="text/csv",
    )

# ONGLET 2 : Import Relevé Bancaire réel (CSV)
with tab2:
    st.subheader(
        f"📥 Importer un nouveau relevé bancaire pour {client_actif}"
    )
    st.write(
        "Téléversez un fichier CSV issu de la banque (colonnes requises : Date,"
        " Libellé, Montant)."
    )

    csv_upload = st.file_uploader("Fichier CSV de la banque", type=["csv"])
    if csv_upload is not None:
        try:
            df_banque = pd.read_csv(csv_upload, sep=None, engine="python")
            st.write("Aperçu du fichier importé :", df_banque.head(3))

            date_col = st.selectbox("Colonne Date :", df_banque.columns)
            libelle_col = st.selectbox("Colonne Libellé :", df_banque.columns)
            montant_col = st.selectbox("Colonne Montant :", df_banque.columns)

            if st.button("➕ Injecter dans la BDD Client"):
                conn = get_db_connection()
                cursor = conn.cursor()
                for _, row in df_banque.iterrows():
                    cursor.execute(
                        """
                    INSERT INTO transactions (client_nom, date, montant, libelle, justificatif_recu)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                        (
                            client_actif,
                            str(row[date_col]),
                            float(str(row[montant_col]).replace(",", ".")),
                            str(row[libelle_col]),
                        ),
                    )
                conn.commit()
                conn.close()
                st.success(
                    f"✅ {len(df_banque)} transactions ajoutées au dossier"
                    f" {client_actif} !"
                )
                st.rerun()
        except Exception as e:
            st.error(f"Erreur de lecture du fichier CSV : {e}")

# ONGLET 3 : Relances Client & Rapports
with tab3:
    st.subheader(f"📲 Relances & Bilan Synthétique - {client_actif}")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT date, libelle, montant FROM transactions WHERE"
        " justificatif_recu = 0 AND client_nom = ?",
        (client_actif,),
    )
    manquants = cursor.fetchall()
    conn.close()

    if manquants:
        st.warning(
            f"Il reste **{len(manquants)} transaction(s)** sans justificatif"
            f" pour {client_actif}."
        )

        liste_achats = "\n".join(
            [f"• {m[0]} : {m[1]} ({m[2]} €)" for m in manquants]
        )
        message_whatsapp = (
            f"Bonjour {client_actif} 👋,\n\nIl vous manque {len(manquants)}"
            " justificatif(s) pour votre comptabilité :\n\n"
            f"{liste_achats}\n\nMerci de m'envoyer les justificatifs"
            " directement en réponse !"
        )

        st.text_area(
            "Message de relance WhatsApp :", value=message_whatsapp, height=160
        )

        texte_encodes = urllib.parse.quote(message_whatsapp)
        lien_whatsapp = f"https://wa.me/?text={texte_encodes}"
        st.markdown(
            f"[📲 Envoyer directement via WhatsApp Web]({lien_whatsapp})",
            unsafe_allow_html=True,
        )
    else:
        st.success(f"🎉 Le dossier {client_actif} est entièrement à jour !")