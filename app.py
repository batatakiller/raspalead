import streamlit as st
import sqlite3
import pandas as pd
import threading
import time
import os
import random
import re
from streamlit.runtime.scriptrunner import add_script_run_ctx
from playwright.sync_api import sync_playwright

# --- Setup Constants and Directories ---
DATA_DIR = "/app/data"
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
            email TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(name, phone)
        )
    ''')
    # Migração simples: adicionar coluna email se não existir
    try:
        c.execute('ALTER TABLE leads ADD COLUMN email TEXT')
    except:
        pass
    conn.commit()
    conn.close()

def save_lead(name, phone, website, email):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT OR IGNORE INTO leads (name, phone, website, email)
            VALUES (?, ?, ?, ?)
        ''', (name, phone, website, email))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")

def get_leads_df():
    try:
        conn = sqlite3.connect(DB_PATH)
        df = pd.read_sql_query("SELECT id, name, phone, website, email, timestamp FROM leads ORDER BY timestamp DESC", conn)
        conn.close()
        return df
    except:
        return pd.DataFrame(columns=["id", "name", "phone", "website", "email", "timestamp"])

# --- Status Helper ---
def update_status(msg):
    with open(STATUS_FILE, "w") as f:
        f.write(msg)

def get_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            return f.read()
    return "Aguardando início..."

# --- Email Extraction Helper ---
def find_emails(text):
    if not text: return []
    return re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)

def try_get_email_from_website(browser_context, url):
    if not url or "http" not in url: return ""
    try:
        page = browser_context.new_page()
        page.goto(url, timeout=10000, wait_until="domcontentloaded")
        content = page.content()
        emails = find_emails(content)
        page.close()
        if emails:
            # Pegar o primeiro e-mail que não seja óbvio placeholder
            for e in emails:
                if "domain" not in e and "example" not in e:
                    return e
        return ""
    except:
        return ""

# --- Playwright Scraper (Background Thread) ---
def scrape_maps(search_term, proxy_url, max_leads, extract_emails, stop_event):
    update_status("Iniciando navegador...")
    page = None
    browser = None
    lead_count = 0
    
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
            context.set_default_timeout(60000) # Aumentar timeout global para 60s
            page = context.new_page()
            
            update_status("Acessando Google Maps...")
            page.goto("https://www.google.com/maps", wait_until="networkidle", timeout=90000)
            time.sleep(random.uniform(3, 5))
            
            # Screenshot inicial para ver se há bloqueio/cookies
            page.screenshot(path=DEBUG_IMG_PATH)

            # Lógica reforçada para aceitar cookies/termos
            update_status("Verificando banners de cookies...")
            cookie_selectors = [
                "button:has-text('Rejeitar tudo')", 
                "button:has-text('Reject all')",
                "button:has-text('Aceitar tudo')",
                "button:has-text('Accept all')",
                "button[aria-label*='Aceitar']",
                "button[aria-label*='Accept']"
            ]
            for sel in cookie_selectors:
                try:
                    if page.locator(sel).is_visible():
                        page.click(sel, timeout=5000)
                        time.sleep(2)
                except: pass
            
            update_status(f"Buscando por: {search_term}")
            # Esperar o campo de busca estar disponível
            try:
                page.wait_for_selector("input#searchboxinput", timeout=15000)
                page.fill("input#searchboxinput", search_term)
                time.sleep(random.uniform(1, 2))
                page.keyboard.press("Enter")
            except:
                update_status("Campo de busca não encontrado. Google pode estar bloqueando ou mudou o layout.")
                page.screenshot(path=DEBUG_IMG_PATH)
                return
            
            update_status("Aguardando carregamento da lista...")
            page.wait_for_timeout(5000)
            page.screenshot(path=DEBUG_IMG_PATH)
            
            # Tentar múltiplos seletores para a lista de resultados
            list_selectors = ['div[role="feed"]', 'div[aria-label*="Resultados"]', 'div[aria-label*="Results"]']
            found_list = False
            for sel in list_selectors:
                try:
                    page.wait_for_selector(sel, timeout=15000)
                    found_list = True
                    break
                except: continue
            
            if not found_list:
                update_status("Lista de resultados não apareceu (Timeout). Verifique o Screenshot de Debug.")
                page.screenshot(path=DEBUG_IMG_PATH)
                return

            processed_names = set()
            scroll_attempts = 0
            
            while not stop_event.is_set() and lead_count < max_leads:
                update_status(f"Carregando resultados... ({lead_count}/{max_leads})")
                page.wait_for_timeout(random.uniform(2000, 3000))
                page.screenshot(path=DEBUG_IMG_PATH)

                links = page.locator('a[href*="/maps/place/"]').element_handles()
                new_links_found = False
                
                for link in links:
                    if stop_event.is_set() or lead_count >= max_leads:
                        break
                    
                    try:
                        name = link.get_attribute("aria-label")
                        if not name or name in processed_names:
                            continue
                        
                        update_status(f"Extraindo: {name} ({lead_count+1}/{max_leads})")
                        link.scroll_into_view_if_needed()
                        time.sleep(random.uniform(0.5, 1.5))
                        link.click()
                        new_links_found = True
                        
                        # Espera detalhes
                        page.wait_for_timeout(random.uniform(2000, 3000))
                        
                        phone = ""
                        website = ""
                        email = ""
                        
                        # Phone
                        try:
                            phone_locators = ['button[data-tooltip*="telefone"]', 'button[data-tooltip*="phone"]']
                            for sel in phone_locators:
                                el = page.locator(sel).first
                                if el.count() > 0:
                                    aria = el.get_attribute('aria-label')
                                    if aria:
                                        phone = aria.replace("Telefone:", "").replace("Phone:", "").strip()
                                        break
                        except: pass
                        
                        # Website
                        try:
                            web_locators = ['a[data-tooltip*="website"]', 'a[data-tooltip*="site"]']
                            for sel in web_locators:
                                el = page.locator(sel).first
                                if el.count() > 0:
                                    website = el.get_attribute("href")
                                    if website: break
                        except: pass
                        
                        # Email Extraction
                        if extract_emails:
                            # 1. Tenta achar na descrição visível do Maps
                            details_text = page.locator('div[role="main"]').inner_text()
                            emails_found = find_emails(details_text)
                            if emails_found:
                                email = emails_found[0]
                            
                            # 2. Se não achou e tem site, tenta visitar o site rapidamente
                            if not email and website:
                                update_status(f"Buscando e-mail no site de {name}...")
                                email = try_get_email_from_website(context, website)
                                update_status(f"Extraindo: {name} ({lead_count+1}/{max_leads})")

                        save_lead(name, phone, website, email)
                        processed_names.add(name)
                        lead_count += 1
                        time.sleep(random.uniform(1, 2))
                        
                    except Exception as item_ex:
                        print(f"Erro item: {item_ex}")
                
                # Scroll
                if not new_links_found:
                    scroll_attempts += 1
                else:
                    scroll_attempts = 0
                
                if scroll_attempts > 5:
                    break
                
                update_status("Rolando a página...")
                page.mouse.wheel(0, 3000)
                time.sleep(2)
            
            if stop_event.is_set():
                update_status(f"Cancelado. Capturados: {lead_count}")
            elif lead_count >= max_leads:
                update_status(f"Limite de {max_leads} atingido! ✅")
            else:
                update_status(f"Fim dos resultados. Capturados: {lead_count} ✅")
                
            browser.close()
            
    except Exception as e:
        update_status(f"Erro crítico: {str(e)}")
        if browser: browser.close()
    finally:
        st.session_state.is_running = False

# --- UI Streamlit ---
def main():
    st.set_page_config(page_title="Maps Lead Scraper PRO", page_icon="🗺️", layout="wide")
    
    if "is_running" not in st.session_state:
        st.session_state.is_running = False
    if "stop_event" not in st.session_state:
        st.session_state.stop_event = threading.Event()
        
    init_db()
    
    st.title("🗺️ Google Maps Lead Scraper PRO")
    
    with st.sidebar:
        st.header("Configurações")
        search_term = st.text_input("Busca:", placeholder="Ex: Dentistas em Curitiba")
        
        max_leads = st.number_input("Limite de Leads:", min_value=1, max_value=1000, value=50)
        extract_emails = st.checkbox("Extrair E-mails (Lento - Visita sites)", value=False)
        
        st.markdown("---")
        st.subheader("Rede (VPS)")
        proxy_url = st.text_input("Proxy (Opcional):", placeholder="http://user:pass@ip:port")
        
        st.markdown("---")
        if not st.session_state.is_running:
            if st.button("🚀 Iniciar Extração", type="primary", use_container_width=True):
                if search_term:
                    update_status("Iniciando...")
                    st.session_state.is_running = True
                    st.session_state.stop_event.clear()
                    t = threading.Thread(target=scrape_maps, args=(search_term, proxy_url, max_leads, extract_emails, st.session_state.stop_event))
                    add_script_run_ctx(t)
                    t.start()
                    st.rerun()
                else:
                    st.warning("Insira um termo.")
        else:
            if st.button("⏹️ Parar Extração", use_container_width=True):
                st.session_state.stop_event.set()
                st.session_state.is_running = False
                st.rerun()
                
        if st.button("📸 Screenshot de Debug"):
            if os.path.exists(DEBUG_IMG_PATH):
                st.image(DEBUG_IMG_PATH)

    col1, col2 = st.columns([3, 1])
    
    with col1:
        st.subheader("Resultados")
        df = get_leads_df()
        st.dataframe(df, use_container_width=True, height=450)
    
    with col2:
        st.info(f"**Status:**\n\n{get_status()}")
        
        if not df.empty:
            output_file = os.path.join(DATA_DIR, 'leads_export.xlsx')
            df.to_excel(output_file, index=False)
            with open(output_file, 'rb') as f:
                st.download_button("📥 Baixar Planilha (.xlsx)", data=f, file_name="leads_maps.xlsx", use_container_width=True)
                     
        if st.button("🔄 Atualizar Tabela"):
            st.rerun()

    if st.session_state.is_running:
        time.sleep(3)
        st.rerun()

if __name__ == "__main__":
    main()
