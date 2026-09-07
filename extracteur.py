import streamlit as st
import sqlite3
import pandas as pd
import json
import os
import urllib.parse
from google import genai
from google.genai import types
from PIL import Image

# ==========================================
# 1. CONFIGURATION & OPTIMISATION IA
# ==========================================
try:
    api_key = st.secrets["GEMINI_API_KEY"]
except Exception:
    api_key = os.environ.get("GEMINI_API_KEY", "")

client = genai.Client(api_key=api_key)

def optimiser_image(image):
    """Redimensionne l'image pour accélérer le traitement IA (max 1024px)"""
    image.thumbnail((1024, 1024))
    return image

def analyser_facture_rapide(image):
    """Appel Gemini optimisé avec un prompt strict pour une réponse rapide"""
    prompt = """Extrais les données de cette facture. 
    Réponds UNIQUEMENT avec un objet JSON valide contenant exactement ces clés :
    {"fournisseur": "nom", "date": "AAAA-MM-JJ", "montant_ht": 0.0, "montant_tva": 0.0, "montant_ttc": 0.0}"""
    
    response = client.models.generate_content(
        model="gemini-2.5-flash", # Modèle mis à jour et ultra-rapide
        contents=[optimiser_image(image), prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1),
    )
    return json.loads(response.text)

# ==========================================
# 2. GESTION DE LA BASE DE DONNÉES (PROD)
# ==========================================
def get_db_connection():
    return sqlite3.connect("compta_production.db")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        nom TEXT PRIMARY KEY
    )
    """)
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
        montant_tva REAL,
        FOREIGN KEY(client_nom) REFERENCES clients(nom)
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ==========================================
# 3. INTERFACE UTILISATEUR (UX/UI Cabinet)
# ==========================================
st.set_page_config(page_title="SaaS Expertise", page_icon="📊", layout="wide")

st.markdown("""
    <style>
    .stMetric { background-color: #f8fafc; padding: 15px; border-radius: 8px; border: 1px solid #e2e8f0; }
    div[data-testid="stSidebar"] { background-color: #0f172a; }
    div[data-testid="stSidebar"] * { color: #f8fafc !important; }
    </style>
""", unsafe_allow_html=True)

st.title("📊 Plateforme de Révision & Lettrage IA")

# --- SIDEBAR : GESTION DES DOSSIERS ---
with st.sidebar:
    st.header("🏢 Portefeuille Cabinet")
    
    with st.expander("➕ Nouveau Client", expanded=False):
        nouveau_nom = st.text_input("Raison sociale :")
        if st.button("Créer le dossier") and nouveau_nom:
            conn = get_db_connection()
            try:
                conn.execute("INSERT INTO clients (nom) VALUES (?)", (nouveau_nom.strip(),))
                conn.commit()
                st.success("Dossier créé.")
            except sqlite3.IntegrityError:
                st.error("Ce client existe déjà.")
            conn.close()
    
    st.divider()
    
    conn = get_db_connection()
    clients_dispo = pd.read_sql("SELECT nom FROM clients", conn)["nom"].tolist()
    conn.close()
    
    if not clients_dispo:
        st.warning("Commencez par créer un dossier client.")
        st.stop()
        
    client_actif = st.selectbox("Dossier actif :", clients_dispo)

# --- TABLEAU DE BORD (KPIs) ---
conn = get_db_connection()
stats = pd.read_sql("SELECT justificatif_recu, montant_tva FROM transactions WHERE client_nom = ?", conn, params=(client_actif,))
conn.close()

total_lignes = len(stats)
lignes_lettrees = stats['justificatif_recu'].sum()
tva_identifiee = stats['montant_tva'].sum() if not stats['montant_tva'].isna().all() else 0.0

col_kpi1, col_kpi2, col_kpi3 = st.columns(3)
col_kpi1.metric("Lignes Bancaires", total_lignes)
col_kpi2.metric("Taux de Lettrage", f"{int((lignes_lettrees/total_lignes)*100)}%" if total_lignes > 0 else "0%")
col_kpi3.metric("TVA Sécurisée", f"{tva_identifiee:.2f} €")
st.divider()

# --- ONGLETS MÉTIERS ---
tab_import, tab_ocr, tab_relances = st.tabs(["1. 📥 Import Bancaire (CSV)", "2. 🤖 Lettrage IA des Justificatifs", "3. 💬 Relances Clients"])

# ONGLET 1 : IMPORT DU RELEVÉ BANCAIRE
with tab_import:
    st.subheader(f"Import des flux pour {client_actif}")
    csv_upload = st.file_uploader("Glissez le relevé bancaire (CSV)", type=["csv"])
    
    if csv_upload:
        df_banque = pd.read_csv(csv_upload, sep=None, engine="python")
        st.dataframe(df_banque.head(3), use_container_width=True)
        
        col1, col2, col3 = st.columns(3)
        date_col = col1.selectbox("Colonne Date :", df_banque.columns)
        libelle_col = col2.selectbox("Colonne Libellé :", df_banque.columns)
        montant_col = col3.selectbox("Colonne Montant :", df_banque.columns)

        if st.button("Importer les écritures", type="primary"):
            conn = get_db_connection()
            cursor = conn.cursor()
            for _, row in df_banque.iterrows():
                try:
                    montant_float = float(str(row[montant_col]).replace(",", ".").replace("€", "").replace(" ", ""))
                    cursor.execute("""
                    INSERT INTO transactions (client_nom, date, montant, libelle, justificatif_recu)
                    VALUES (?, ?, ?, ?, 0)
                    """, (client_actif, str(row[date_col]), montant_float, str(row[libelle_col])))
                except ValueError:
                    continue
            conn.commit()
            conn.close()
            st.success("Écritures importées avec succès.")
            st.rerun()

# ONGLET 2 : LETTRAGE IA (Rapprochement Facture/Banque)
with tab_ocr:
    st.subheader("Traitement des pièces comptables")
    uploaded_files = st.file_uploader("Déposez un ou plusieurs justificatifs", type=["jpg", "jpeg", "png"], accept_multiple_files=True)
    
    if uploaded_files and st.button("Lancer le lettrage automatique", type="primary"):
        for uploaded_file in uploaded_files:
            image = Image.open(uploaded_file)
            with st.status(f"Analyse de {uploaded_file.name}...", expanded=True) as status:
                try:
                    justif = analyser_facture_rapide(image)
                    st.write(f"📝 Extraction : {justif['fournisseur']} - {justif['montant_ttc']} €")
                    
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("SELECT id, montant, libelle FROM transactions WHERE justificatif_recu = 0 AND client_nom = ?", (client_actif,))
                    attentes = cursor.fetchall()
                    
                    matched = False
                    for t_id, t_montant, t_libelle in attentes:
                        ecart = abs(justif["montant_ttc"] - t_montant)
                        if ecart < 0.05:
                            cursor.execute("""
                            UPDATE transactions 
                            SET justificatif_recu = 1, fournisseur = ?, montant_ht = ?, montant_tva = ?
                            WHERE id = ?
                            """, (justif["fournisseur"], justif["montant_ht"], justif["montant_tva"], t_id))
                            conn.commit()
                            matched = True
                            status.update(label=f"✅ Rapproché avec ligne bancaire #{t_id}", state="complete")
                            break
                    conn.close()
                    
                    if not matched:
                        status.update(label="⚠️ Aucun flux bancaire correspondant trouvé", state="error")
                        
                except Exception as e:
                    status.update(label=f"❌ Erreur de lecture : {e}", state="error")

    st.markdown("### Journal d'Achats (Vue lettrée)")
    conn = get_db_connection()
    df_journal = pd.read_sql("SELECT date, libelle, montant, justificatif_recu, fournisseur, montant_ht, montant_tva FROM transactions WHERE client_nom = ?", conn, params=(client_actif,))
    conn.close()
    
    if not df_journal.empty:
        df_journal["Statut"] = df_journal["justificatif_recu"].apply(lambda x: "🟢 Lettré" if x == 1 else "🔴 Manquant")
        st.dataframe(df_journal[["date", "libelle", "montant", "Statut", "fournisseur", "montant_ht", "montant_tva"]], use_container_width=True, hide_index=True)

# ONGLET 3 : RELANCES CLIENTS
with tab_relances:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT date, libelle, montant FROM transactions WHERE justificatif_recu = 0 AND client_nom = ?", (client_actif,))
    manquants = cursor.fetchall()
    conn.close()

    if manquants:
        st.warning(f"{len(manquants)} pièce(s) manquante(s) pour clôturer le mois.")
        
        lignes_texte = "\n".join([f"- {m[0]} : {m[1]} ({m[2]} €)" for m in manquants])
        msg = f"Bonjour,\n\nPour finaliser la saisie de votre dossier {client_actif}, merci de nous transmettre les {len(manquants)} justificatifs suivants :\n\n{lignes_texte}\n\nVous pouvez répondre à ce message avec les photos. Merci !"
        
        st.text_area("Brouillon de relance", value=msg, height=200)
        
        lien_wa = f"https://wa.me/?text={urllib.parse.quote(msg)}"
        st.link_button("📲 Envoyer la relance via WhatsApp", lien_wa, type="primary")
    elif total_lignes > 0:
        st.success("Dossier à jour. Aucun justificatif manquant.")