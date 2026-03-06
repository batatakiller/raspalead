import streamlit as st
import sqlite3
import pandas as pd
import threading
import time
import os
import random
from streamlit.runtime.scriptrunner import add_script_run_ctx
from playwright.sync_api import sync_playwright

# --- Setup Constants and Directories ---
DATA_DIR = "/app/data"
# Fallback for local testing if /app/data doesn't exist and we're not in Docker
if not os.path.exists(DATA_DIR) and not os.environ.get('DOCKER_CONTAINER'):
    DATA_DIR = "data"
    
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "leads.db")
DEBUG_IMG_PATH = os.path.join(DATA_DIR, "debug.png")
STATUS_FILE = os.path.join(DATA_DIR, "status.txt")

# --- Database Functions ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            phone TEXT,
            website TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, phone)
        )
    ''')
    conn.commit()
    conn.close()

def save_lead(name, phone, website):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO leads (name, phone, website)
            VALUES (?, ?, ?)
        ''', (name, phone, website))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def get_leads_df():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT id, name, phone, website, timestamp FROM leads ORDER BY timestamp DESC", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame(columns=["id", "name", "phone", "website", "timestamp"])

# --- Status Helper ---
def update_status(msg):
    with open(STATUS_FILE, "w") as f:
        f.write(msg)

def get_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            return f.read()
    return "Aguardando início..."

# --- Playwright Scraper (Background Thread) ---
def scrape_maps(search_term, proxy_url, stop_event):
    update_status("Iniciando navegador...")
    page = None
    browser = None
    try:
        with sync_playwright() as p:
            browser_args = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"]
            }
            if proxy_url and proxy_url.strip():
                browser_args["proxy"] = {"server": proxy_url.strip()}
            
            browser = p.chromium.launch(**browser_args)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="pt-BR"
            )
            page = context.new_page()
            
            update_status("Acessando Google Maps...")
            page.goto("https://www.google.com/maps")
            page.wait_for_load_state("networkidle")
            time.sleep(random.uniform(2, 4))
            
            # Aceitar cookies (se a janela aparecer na Europa/LGPD)
            try:
                page.click("button:has-text('Rejeitar tudo'), button:has-text('Reject all')", timeout=3000)
            except:
                pass
            try:
                page.click("button:has-text('Aceitar tudo'), button:has-text('Accept all')", timeout=3000)
            except:
                pass
            
            update_status(f"Buscando por: {search_term}")
            page.fill("input#searchboxinput", search_term)
            time.sleep(random.uniform(1, 2))
            page.keyboard.press("Enter")
            
            update_status("Aguardando carregamento da lista...")
            
            # Tirar uma screenshot de debug assim que a tela de resultados tentar carregar
            page.wait_for_timeout(3000)
            page.screenshot(path=DEBUG_IMG_PATH)
            
            # Espera até o painel "feed" com os resultados aparecer
            page.wait_for_selector('div[role="feed"]', timeout=30000)
            
            processed_names = set()
            scroll_attempts = 0
            
            while not stop_event.is_set():
                update_status("Carregando resultados...")
                page.wait_for_timeout(random.uniform(2000, 3000))
                
                # Screenshot contínuo de debug
                page.screenshot(path=DEBUG_IMG_PATH)

                # Busca os links dos itens listados
                links = page.locator('a[href*="/maps/place/"]').element_handles()
                new_links_processed = False
                
                for link in links:
                    if stop_event.is_set():
                        break
                    
                    try:
                        name = link.get_attribute("aria-label")
                        if not name or name in processed_names:
                            continue
                        
                        update_status(f"Extraindo: {name}")
                        
                        # Clica para abrir detalhes
                        link.scroll_into_view_if_needed()
                        # Pequeno sleep antes do click para parecer humano
                        time.sleep(random.uniform(0.5, 1.5))
                        link.click()
                        new_links_processed = True
                        
                        # Espera os detalhes carregarem
                        page.wait_for_timeout(random.uniform(1500, 2500))
                        
                        phone = ""
                        website = ""
                        
                        # Extrair telefone (variantes PT e EN)
                        try:
                            # Utiliza regex para varrer botões de telefone
                            phone_locators = ['button[data-tooltip*="telefone"]', 'button[data-tooltip*="phone"]']
                            for sel in phone_locators:
                                el = page.locator(sel).first
                                if el.count() > 0:
                                    # Muitas vezes o numero ta no aria-label 
                                    aria = el.get_attribute('aria-label')
                                    if aria:
                                        phone = aria.replace("Telefone:", "").replace("Phone:", "").strip()
                                        break
                        except:
                            pass
                        
                        # Extrair site
                        try:
                            web_locators = ['a[data-tooltip*="website"]', 'a[data-tooltip*="site"]']
                            for sel in web_locators:
                                el = page.locator(sel).first
                                if el.count() > 0:
                                    website = el.get_attribute("href")
                                    if website:
                                        break
                        except:
                            pass
                            
                        # Salvar no SQLite
                        save_lead(name, phone, website)
                        processed_names.add(name)
                        
                        # Volta o foco para o feed com um leve sleep
                        time.sleep(random.uniform(1, 2))
                        
                    except Exception as item_ex:
                        print(f"Erro item: {item_ex}")
                        # Tirar screenshot se um erro de extração muito bizarro ocorrer
                        page.screenshot(path=DEBUG_IMG_PATH)
                
                # Controle de Scroll para carregar mais itens
                if new_links_processed:
                    scroll_attempts = 0
                else:
                    scroll_attempts += 1
                
                if scroll_attempts > 3:
                     # Checar se atingimos o fim da lista
                     content = page.content()
                     if "Chegou ao fim da lista" in content or "reached the end of the list" in content:
                         update_status("Fim da lista alcançado.")
                         break
                     
                     if scroll_attempts > 6:
                         update_status("Múltiplas tentativas de scroll sem novos itens. Parando.")
                         break
                
                # Fazer o scroll do feed
                update_status("Rolando a página para carregar mais...")
                page.mouse.wheel(0, 3000)
                time.sleep(1)
            
            if stop_event.is_set():
                update_status("Extração cancelada pelo usuário.")
            else:
                update_status("Extração Finalizada. ✅")
                
            browser.close()
            
    except Exception as e:
        update_status(f"Erro crítico: {str(e)}")
        try:
            if page:
                page.screenshot(path=DEBUG_IMG_PATH)
        except:
            pass
        if browser:
            try:
                browser.close()
            except:
                pass
    finally:
        st.session_state.is_running = False

# --- UI Streamlit ---
def main():
    st.set_page_config(page_title="Maps Lead Scraper", page_icon="🗺️", layout="wide")
    
    # Initialize UI state
    if "is_running" not in st.session_state:
        st.session_state.is_running = False
    if "stop_event" not in st.session_state:
        st.session_state.stop_event = threading.Event()
        
    init_db()
    
    st.title("🗺️ Google Maps Lead Scraper")
    st.markdown("Extraia leads diretamente do Google Maps via Playwright (Headless).")
    
    with st.sidebar:
        st.header("Configurações")
        search_term = st.text_input("Termo de Busca:", placeholder="Ex: Arquitetos em São Paulo")
        
        st.markdown("---")
        st.subheader("Configurações de Rede (VPS)")
        st.markdown("Para evitar Captchas/Challenges do Google em Datacenters, insira um Proxy.")
        proxy_url = st.text_input("URL Proxy Autenticado (Opcional):", placeholder="http://user:pass@ip:port")
        
        st.markdown("---")
        if not st.session_state.is_running:
            if st.button("🚀 Iniciar Extração", type="primary", use_container_width=True):
                if search_term:
                    update_status("Preparando...")
                    st.session_state.is_running = True
                    st.session_state.stop_event.clear()
                    
                    t = threading.Thread(target=scrape_maps, args=(search_term, proxy_url, st.session_state.stop_event))
                    add_script_run_ctx(t)
                    t.start()
                    st.rerun()
                else:
                    st.warning("Preencha o termo de busca.")
        else:
            if st.button("⏹️ Parar Extração", type="secondary", use_container_width=True):
                st.session_state.stop_event.set()
                st.session_state.is_running = False
                update_status("Cancelando a extração...")
                st.rerun()
                
        st.markdown("---")
        st.subheader("Ferramentas de Debug")
        st.markdown("Caso as capturas parem, veja a tela do robô atual.")
        if st.button("📸 Ver Screenshot de Debug"):
            if os.path.exists(DEBUG_IMG_PATH):
                st.image(DEBUG_IMG_PATH, caption="Última visão do navegador")
            else:
                st.info("Nenhuma screenshot disponível ainda.")

    # Dashboard Principal
    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.subheader("Leads Capturados (Em Tempo Real)")
        df_leads = get_leads_df()
        st.dataframe(df_leads, use_container_width=True, height=400)
    
    with col2:
        st.info("Status da Operação:\n\n**" + get_status() + "**")
        
        st.markdown("---")
        st.markdown("### Exportação")
        if not df_leads.empty:
            # Generate Excel strictly in memory for download
            output = pd.ExcelWriter(os.path.join(DATA_DIR, 'leads_export.xlsx'), engine='openpyxl')
            df_leads.to_excel(output, index=False, sheet_name='Leads')
            output.close()
            
            with open(os.path.join(DATA_DIR, 'leads_export.xlsx'), 'rb') as f:
                st.download_button(
                    label="📥 Baixar como Excel (.xlsx)",
                    data=f,
                    file_name="leads_export.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )
                     
        if st.button("🔄 Atualizar Tabela Manualmente"):
            st.rerun()

    # Auto-refresh loop when running
    if st.session_state.is_running:
        time.sleep(3)
        st.rerun()

if __name__ == "__main__":
    main()
