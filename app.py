# -*- coding: utf-8 -*-
import os
import random
import string
import sqlite3
import secrets
import re
import logging
import hashlib
from datetime import datetime, timedelta
from functools import wraps
from html import escape as html_escape

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, g, session, abort, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# Get the directory where app.py is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Try to find templates folder - Railway might have different structure
TEMPLATE_DIRS = [
    os.path.join(BASE_DIR, 'templates'),
    os.path.join(BASE_DIR, 'app', 'templates'),
    '/app/templates',
    os.path.join(os.getcwd(), 'templates'),
]

STATIC_DIRS = [
    os.path.join(BASE_DIR, 'static'),
    os.path.join(BASE_DIR, 'app', 'static'),
    '/app/static',
    os.path.join(os.getcwd(), 'static'),
]

# Find existing template directory
TEMPLATE_DIR = None
for d in TEMPLATE_DIRS:
    if os.path.exists(d) and os.path.exists(os.path.join(d, 'index.html')):
        TEMPLATE_DIR = d
        break

# Find existing static directory  
STATIC_DIR = None
for d in STATIC_DIRS:
    if os.path.exists(d):
        STATIC_DIR = d
        break

# Fallback to default if not found
if not TEMPLATE_DIR:
    TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
if not STATIC_DIR:
    STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, 
            static_folder=STATIC_DIR, 
            static_url_path='/static', 
            template_folder=TEMPLATE_DIR)

# FIX 1: ProxyFix - Railway arkaly proxy HTTPS diýip bilsin
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Log for debugging (using print since logger not ready yet)
print(f"[STARTUP] BASE_DIR: {BASE_DIR}")
print(f"[STARTUP] TEMPLATE_DIR: {TEMPLATE_DIR}")
print(f"[STARTUP] TEMPLATE_DIR exists: {os.path.exists(TEMPLATE_DIR)}")
print(f"[STARTUP] STATIC_DIR: {STATIC_DIR}")
print(f"[STARTUP] STATIC_DIR exists: {os.path.exists(STATIC_DIR)}")
if os.path.exists(TEMPLATE_DIR):
    print(f"[STARTUP] Templates: {os.listdir(TEMPLATE_DIR)}")


# ENVIRONMENT VARIABLES
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    logging.warning("SECRET_KEY bellenmedi, awto generasiya edildi!")

# Admin parol - DÜZ TEKST galýar (ulanyjy islegi boýunça)
ADMIN_SIFRE_HASH = os.environ.get('ADMIN_SIFRE_HASH', '')
if not ADMIN_SIFRE_HASH:
    ADMIN_SIFRE_HASH = 'admin123'
    logging.warning("ADMIN_SIFRE_HASH bellenmedi, default ulanylýar!")

CLOUDFLARE_WORKER_URL = os.environ.get('CLOUDFLARE_WORKER_URL', '')

# Railway persistent storage path
DATABASE_DIR = os.environ.get('DATABASE_DIR', BASE_DIR)
DATABASE = os.path.join(DATABASE_DIR, 'turnuva.db')

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Rate Limiting
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"
)

# FIX 2: Session cookie - Railway üçin Lax we Secure
app.config.update(
    SESSION_COOKIE_SECURE=True,      # Diňe HTTPS
    SESSION_COOKIE_HTTPONLY=True,    # JavaScript okap bilmez
    SESSION_COOKIE_SAMESITE='Lax',   # CSRF goraýyş
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24)
)

# ===================== DATABASE =====================

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, check_same_thread=False)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS katilimcilar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referans_kodu TEXT UNIQUE NOT NULL,
                ad TEXT NOT NULL,
                telefon TEXT UNIQUE NOT NULL,
                parol_hash TEXT NOT NULL,
                pubg_id TEXT,
                payment_phone TEXT,
                tournament_id TEXT,
                ulasim TEXT,
                takim_kodu TEXT,
                takim_lideri INTEGER DEFAULT 0,
                odeme_durumu INTEGER DEFAULT 0,
                admin_onay INTEGER DEFAULT 0,
                kayit_tarihi TEXT NOT NULL,
                odeme_tarihi TEXT,
                onay_tarihi TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS takimlar (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                takim_kodu TEXT UNIQUE NOT NULL,
                takim_adi TEXT,
                lider_referans TEXT NOT NULL,
                uye1_referans TEXT,
                uye2_referans TEXT,
                uye3_referans TEXT,
                durum INTEGER DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS ayarlar (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        defaults = {
            'turnir_senesi': '25 Iýul 2026',
            'turnir_wagty': '20:00 (TM)',
            'turnir_karta': 'Erangel',
            'turnir_gatnasym': 'Squad (4 kişi)',
            'turnir_tolek': '5 Manat',
            'turnir_tolek_usuly': 'TMCell SMS',
            'turnir_yer_sany': '100',
            'bayrak_1': '300 Manat|+ 🏆 Kubok',
            'bayrak_2': '150 Manat',
            'bayrak_3': '50 Manat',
            'bayrak_jemi': '500 M'
        }
        for key, value in defaults.items():
            db.execute("INSERT OR IGNORE INTO ayarlar (key, value) VALUES (?, ?)", (key, value))
        db.execute("CREATE INDEX IF NOT EXISTS idx_katilimci_ref ON katilimcilar(referans_kodu)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_katilimci_telefon ON katilimcilar(telefon)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_katilimci_takim ON katilimcilar(takim_kodu)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_katilimci_pubg ON katilimcilar(pubg_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_takim_kod ON takimlar(takim_kodu)")
        db.commit()

# ===================== HELPERS =====================

def get_ayar(key, default=''):
    row = get_db().execute('SELECT value FROM ayarlar WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else default

def set_ayar(key, value):
    db = get_db()
    db.execute('INSERT OR REPLACE INTO ayarlar (key, value) VALUES (?, ?)', (key, value))
    db.commit()

def generate_ref_code():
    db = get_db()
    while True:
        code = 'PUBG-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not db.execute('SELECT 1 FROM katilimcilar WHERE referans_kodu = ?', (code,)).fetchone():
            return code

def generate_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token

def validate_csrf_token(token):
    return token and token == session.get('csrf_token')

def send_telegram_message(message):
    if not CLOUDFLARE_WORKER_URL:
        return False
    try:
        response = requests.post(
            f"{CLOUDFLARE_WORKER_URL}/send-message",
            json={'message': message},
            timeout=15
        )
        return response.status_code == 200
    except requests.RequestException:
        return False

def get_stats():
    stats = get_db().execute("""
        SELECT COALESCE(COUNT(*), 0) as toplam,
               COALESCE(SUM(CASE WHEN odeme_durumu = 1 THEN 1 ELSE 0 END), 0) as odeme_yapan,
               COALESCE(SUM(CASE WHEN admin_onay = 1 THEN 1 ELSE 0 END), 0) as onaylanan
        FROM katilimcilar
    """).fetchone()
    yer_sany = int(get_ayar('turnir_yer_sany', '100'))
    toplam = stats['toplam'] or 0
    return {
        'toplam': toplam,
        'odeme_yapan': stats['odeme_yapan'] or 0,
        'onaylanan': stats['onaylanan'] or 0,
        'yer_sany': yer_sany,
        'galan': max(0, yer_sany - toplam)
    }

def get_turnir_data():
    return {
        'senesi': get_ayar('turnir_senesi'),
        'wagty': get_ayar('turnir_wagty'),
        'karta': get_ayar('turnir_karta'),
        'gatnasym': get_ayar('turnir_gatnasym'),
        'tolek': get_ayar('turnir_tolek'),
        'tolek_usuly': get_ayar('turnir_tolek_usuly')
    }

def get_bayraklar():
    b1 = get_ayar('bayrak_1').split('|')
    b2 = get_ayar('bayrak_2').split('|')
    b3 = get_ayar('bayrak_3').split('|')
    return {
        'bir': {'mukdar': b1[0], 'bonus': b1[1] if len(b1) > 1 else ''},
        'iki': {'mukdar': b2[0], 'bonus': b2[1] if len(b2) > 1 else ''},
        'uc': {'mukdar': b3[0], 'bonus': b3[1] if len(b3) > 1 else ''},
        'jemi': get_ayar('bayrak_jemi')
    }

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('user_logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def validate_phone(phone):
    if not phone:
        return False, None
    cleaned = re.sub(r'[\s\-\+\(\)]', '', phone)
    if not cleaned.isdigit():
        return False, None
    # Diňe 8 sanly ýa-da 993+8 sanly kabul et
    if len(cleaned) == 8:
        return True, cleaned
    if len(cleaned) == 11 and cleaned.startswith('993'):
        return True, cleaned[3:]  # 993 aýyr, diňe 8 san galdyr
    return False, None

def sanitize(text, max_len=100):
    if not text:
        return ''
    return html_escape(str(text).strip())[:max_len]

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def check_password(password):
    # Ulanyjy diňe düz tekst paroly bilen barlamagy isleýär
    return password == ADMIN_SIFRE_HASH


# ===================== ERROR HANDLERS =====================

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': 'Sahypa tapylmady'}), 404
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f'500: {e}', exc_info=True)
    db = getattr(g, '_database', None)
    if db:
        try:
            db.rollback()
        except:
            pass
    if request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': 'Serwer ýalňyşlygy'}), 500
    return render_template('500.html'), 500

@app.errorhandler(429)
def rate_limit(e):
    return jsonify({'success': False, 'message': 'Gaty köp synanyşyk!'}), 429

# ===================== ROUTES =====================

@app.route('/')
def index():
    return render_template('index.html', stats=get_stats(), turnir=get_turnir_data(), bayraklar=get_bayraklar())

# ===================== LOGIN / REGISTER =====================

@app.route('/kayit')
def kayit():
    if get_stats()['toplam'] >= get_stats()['yer_sany']:
        return redirect(url_for('index'))
    return render_template('kayit.html')

@app.route('/login')
def login():
    return render_template('login.html')

@app.route('/api/kayit-ol', methods=['POST'])
@limiter.limit("3 per minute")
def api_kayit_ol():
    data = request.get_json() or {}

    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ad = sanitize(data.get('ad', ''), 100)
    telefon = str(data.get('telefon', '')).strip()
    parol = data.get('parol', '')
    parol_tekrar = data.get('parol_tekrar', '')

    if not all([ad, telefon, parol]):
        return jsonify({'success': False, 'message': 'Ahli maglumatlary dolduryň!'})

    if len(parol) < 6:
        return jsonify({'success': False, 'message': 'Parol 6 harpdan uly bolmaly!'})

    if parol != parol_tekrar:
        return jsonify({'success': False, 'message': 'Parollar deň däl!'})

    valid, telefon_clean = validate_phone(telefon)
    if not valid:
        return jsonify({'success': False, 'message': 'Telefon belgisi nadogry! Format: +993 XX XXX XXX ýa-da 8 san'})

    if len(ad) < 2:
        return jsonify({'success': False, 'message': 'Ad 2 harpdan uly bolmaly!'})

    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')

        # Telefon eýýäm barmy?
        existing = db.execute('SELECT 1 FROM katilimcilar WHERE telefon = ?', (telefon_clean,)).fetchone()
        if existing:
            db.execute('ROLLBACK')
            return jsonify({'success': False, 'message': 'Bu telefon belgisi bilen eýýäm hasap açylypdyr!'})

        # Yer sanyny barla
        count = db.execute('SELECT COUNT(*) as s FROM katilimcilar').fetchone()['s']
        yer = int(get_ayar('turnir_yer_sany', '100'))
        if count >= yer:
            db.execute('ROLLBACK')
            return jsonify({'success': False, 'message': 'Ähli ýerler doldy!'})

        ref = generate_ref_code()
        parol_hash = hash_password(parol)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute("INSERT INTO katilimcilar (referans_kodu, ad, telefon, parol_hash, kayit_tarihi) VALUES (?, ?, ?, ?, ?)",
                  (ref, ad, telefon_clean, parol_hash, now))
        db.commit()
    except sqlite3.IntegrityError:
        db.execute('ROLLBACK')
        return jsonify({'success': False, 'message': 'Bu telefon belgisi bilen eýýäm hasap açylypdyr!'})
    except Exception as e:
        db.execute('ROLLBACK')
        logger.error(f"Kayit hatasi: {e}")
        return jsonify({'success': False, 'message': 'Serwer ýalňyşlygy!'})

    msg = f"🎮 <b>TÄZE KATYLYJY!</b>\n\n👤 {ad}\n📞 {telefon_clean}\n🔑 {ref}"
    send_telegram_message(msg)
    logger.info(f"Kayit: {ref} - {ad}")

    # Auto login
    session['user_logged_in'] = True
    session['user_ref'] = ref
    session['user_telefon'] = telefon_clean
    session.permanent = True

    return jsonify({'success': True, 'referans_kodu': ref, 'message': 'Ustunlikli!'})

@app.route('/api/login', methods=['POST'])
@limiter.limit("5 per minute")
def api_login():
    data = request.get_json() or {}

    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    telefon = str(data.get('telefon', '')).strip()
    parol = data.get('parol', '')

    if not all([telefon, parol]):
        return jsonify({'success': False, 'message': 'Telefon we parol girizin!'})

    valid, telefon_clean = validate_phone(telefon)
    if not valid:
        return jsonify({'success': False, 'message': 'Telefon belgisi nadogry!'})

    parol_hash = hash_password(parol)
    db = get_db()
    kat = db.execute('SELECT * FROM katilimcilar WHERE telefon = ? AND parol_hash = ?', (telefon_clean, parol_hash)).fetchone()

    if not kat:
        return jsonify({'success': False, 'message': 'Telefon belgisi ýa-da parol nädogry!'})

    session['user_logged_in'] = True
    session['user_ref'] = kat['referans_kodu']
    session['user_telefon'] = telefon_clean
    session.permanent = True

    logger.info(f"Login: {kat['referans_kodu']} - {kat['ad']}")
    return jsonify({'success': True, 'referans_kodu': kat['referans_kodu'], 'message': 'Giriş üstünlikli!'})

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    session.pop('user_logged_in', None)
    session.pop('user_ref', None)
    session.pop('user_telefon', None)
    return redirect(url_for('index'))

# ===================== PROFILE / PAYMENT / TEAM =====================

@app.route('/profil')
@login_required
def profil():
    ref_code = session.get('user_ref')
    if not ref_code:
        return redirect(url_for('login'))

    db = get_db()
    kat = db.execute("""
        SELECT k.*, t.takim_adi, t.takim_kodu as t_kod
        FROM katilimcilar k
        LEFT JOIN takimlar t ON k.takim_kodu = t.takim_kodu
        WHERE k.referans_kodu = ?
    """, (ref_code,)).fetchone()
    if not kat:
        session.clear()
        return redirect(url_for('login'))

    arkadaslar = []
    if kat['takim_kodu']:
        arkadaslar = db.execute("""
            SELECT ad, referans_kodu, admin_onay 
            FROM katilimcilar 
            WHERE takim_kodu = ? AND referans_kodu != ?
        """, (kat['takim_kodu'], ref_code)).fetchall()

    return render_template('profil.html', katilimci=kat, takim_arkadaslari=arkadaslar)

@app.route('/odeme')
@login_required
def odeme():
    ref_code = session.get('user_ref')
    if not ref_code:
        return redirect(url_for('login'))

    kat = get_db().execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref_code,)).fetchone()
    if not kat:
        session.clear()
        return redirect(url_for('login'))
    return render_template('odeme.html', katilimci=kat)

@app.route('/api/odeme-yapildi', methods=['POST'])
@limiter.limit("5 per minute")
@login_required
def api_odeme_yapildi():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ref = session.get('user_ref', '')
    if not ref:
        return jsonify({'success': False, 'message': 'Giriş ediň!'})

    db = get_db()
    kat = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref,)).fetchone()
    if not kat:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute("UPDATE katilimcilar SET odeme_durumu = 1, odeme_tarihi = ? WHERE referans_kodu = ?", (now, ref))
    db.commit()

    msg = f"💰 <b>TÖLEG!</b>\n\n👤 {kat['ad']}\n🔑 {ref}\n📅 {now}"
    send_telegram_message(msg)
    logger.info(f"Odeme: {ref}")

    return jsonify({'success': True, 'message': 'Töleg bildirimi ugradyldy!'})

@app.route('/takim')
@login_required
def takim():
    ref_code = session.get('user_ref')
    if not ref_code:
        return redirect(url_for('login'))

    kat = get_db().execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref_code,)).fetchone()
    if not kat:
        session.clear()
        return redirect(url_for('login'))
    return render_template('takim.html', katilimci=kat)

@app.route('/api/takim-olustur', methods=['POST'])
@login_required
@limiter.limit("3 per minute")
def api_takim_olustur():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    lider_ref = session.get('user_ref', '')
    if not lider_ref:
        return jsonify({'success': False, 'message': 'Giriş ediň!'})

    takim_adi = sanitize(data.get('takim_adi', ''), 50)

    if len(takim_adi) < 2 or len(takim_adi) > 50:
        return jsonify({'success': False, 'message': 'Topar ady 2-50 harp aralygynda bolmaly!'})

    db = get_db()
    lider = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (lider_ref,)).fetchone()
    if not lider:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})
    if lider['takim_kodu']:
        return jsonify({'success': False, 'message': 'Siz eýýäm topar bolduňyz!'})

    kod = 'TEAM-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=5))
    db.execute("INSERT INTO takimlar (takim_kodu, takim_adi, lider_referans) VALUES (?, ?, ?)", (kod, takim_adi, lider_ref))
    db.execute("UPDATE katilimcilar SET takim_kodu = ?, takim_lideri = 1 WHERE referans_kodu = ?", (kod, lider_ref))
    db.commit()

    logger.info(f"Topar: {kod} - {takim_adi}")
    return jsonify({'success': True, 'takim_kodu': kod, 'message': 'Topar üstünlikli döredildi!'})

@app.route('/api/takima-katil', methods=['POST'])
@login_required
@limiter.limit("3 per minute")
def api_takima_katil():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    uye_ref = session.get('user_ref', '')
    if not uye_ref:
        return jsonify({'success': False, 'message': 'Giriş ediň!'})

    takim_kodu = str(data.get('takim_kodu', '')).strip().upper()

    if not re.match(r'^TEAM-[A-Z0-9]{5}$', takim_kodu):
        return jsonify({'success': False, 'message': 'Topar kody nädogry format!'})

    db = get_db()
    uye = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (uye_ref,)).fetchone()
    if not uye:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})
    if uye['takim_kodu']:
        return jsonify({'success': False, 'message': 'Siz eýýäm topar bolduňyz!'})

    takim = db.execute('SELECT * FROM takimlar WHERE takim_kodu = ?', (takim_kodu,)).fetchone()
    if not takim:
        return jsonify({'success': False, 'message': 'Topar kody nädogry!'})

    say = db.execute('SELECT COUNT(*) as s FROM katilimcilar WHERE takim_kodu = ?', (takim_kodu,)).fetchone()['s']
    if say >= 4:
        return jsonify({'success': False, 'message': 'Bu topar doly (4 kişi)!'})

    takim_dict = dict(takim)

    db.execute("UPDATE katilimcilar SET takim_kodu = ? WHERE referans_kodu = ?", (takim_kodu, uye_ref))

    if not takim_dict.get('uye1_referans'):
        db.execute('UPDATE takimlar SET uye1_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    elif not takim_dict.get('uye2_referans'):
        db.execute('UPDATE takimlar SET uye2_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    elif not takim_dict.get('uye3_referans'):
        db.execute('UPDATE takimlar SET uye3_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    db.commit()

    msg = f"👥 <b>TOPARA TÄZE AGZA!</b>\n\nTopar: {takim_dict.get('takim_adi', 'Topar')}\nKod: {takim_kodu}\n👤 {uye['ad']}"
    send_telegram_message(msg)
    logger.info(f"Katil: {takim_kodu} - {uye['ad']}")

    return jsonify({'success': True, 'message': f'Topara goşuldyňyz! ({say+1}/4)'})

# ===================== ADMIN (GIZLIN) =====================

@app.route('/admin')
def admin_login():
    return render_template('admin_login.html')

@app.route('/api/admin-login', methods=['POST'])
@limiter.limit("5 per minute")
def api_admin_login():
    data = request.get_json() or {}
    sifre = data.get('sifre', '')

    if not sifre or len(sifre) < 6:
        logger.warning(f"Nadogry login (gysga parol): {request.remote_addr}")
        return jsonify({'success': False, 'message': 'Parol 6 harpdan uly bolmaly!'})

    if not check_password(sifre):
        logger.warning(f"Nadogry login: {request.remote_addr}")
        return jsonify({'success': False, 'message': 'Parol nädogry!'})

    session['admin_logged_in'] = True
    session.permanent = True
    logger.info(f"Admin login: {request.remote_addr}")
    return jsonify({'success': True, 'message': 'Giriş üstünlikli!'})

@app.route('/admin/logout', methods=['GET', 'POST'])
@admin_required
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/panel')
@admin_required
def admin_panel():
    db = get_db()
    stats = get_stats()

    katilimcilar = db.execute("""
        SELECT k.*, t.takim_adi 
        FROM katilimcilar k
        LEFT JOIN takimlar t ON k.takim_kodu = t.takim_kodu
        ORDER BY k.kayit_tarihi DESC
    """).fetchall()

    takimlar = db.execute("""
        SELECT t.*, k.ad as lider_ady
        FROM takimlar t
        JOIN katilimcilar k ON t.lider_referans = k.referans_kodu
        ORDER BY t.id DESC
    """).fetchall()

    return render_template('admin_panel.html', stats=stats, katilimcilar=katilimcilar,
                          takimlar=takimlar, turnir=get_turnir_data(), bayraklar=get_bayraklar())

@app.route('/api/admin-ayarlari-kaydet', methods=['POST'])
@admin_required
def api_admin_ayarlari_kaydet():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    for key, value in data.items():
        if key != 'csrf_token' and value is not None:
            set_ayar(key, str(value))
    logger.info("Ayarlar üýtgedildi")
    return jsonify({'success': True, 'message': 'Ayarlar üstünlikli saklandy!'})

@app.route('/api/admin-onayla', methods=['POST'])
@admin_required
def api_admin_onayla():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ref = data.get('referans_kodu', '')
    db = get_db()
    kat = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref,)).fetchone()
    if not kat:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute("UPDATE katilimcilar SET admin_onay = 1, onay_tarihi = ? WHERE referans_kodu = ?", (now, ref))
    db.commit()

    msg = f"✅ <b>TASSYKLANDY!</b>\n\n👤 {kat['ad']}\n🔑 {ref}\n📅 {now}"
    send_telegram_message(msg)
    logger.info(f"Onay: {ref}")
    return jsonify({'success': True, 'message': 'Katylyjy tassyklandy!'})

@app.route('/api/admin-reddet', methods=['POST'])
@admin_required
def api_admin_reddet():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ref = data.get('referans_kodu', '')
    db = get_db()
    kat = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref,)).fetchone()
    if not kat:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})

    db.execute("UPDATE katilimcilar SET admin_onay = 2 WHERE referans_kodu = ?", (ref,))
    db.commit()

    msg = f"❌ <b>RET EDILDI!</b>\n\n👤 {kat['ad']}\n🔑 {ref}"
    send_telegram_message(msg)
    logger.info(f"Red: {ref}")
    return jsonify({'success': True, 'message': 'Katylyjy ret edildi!'})

@app.route('/api/admin-poz', methods=['POST'])
@admin_required
def api_admin_poz():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ref = data.get('referans_kodu', '')
    db = get_db()
    kat = db.execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref,)).fetchone()
    if not kat:
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady!'})

    # Lider pozulanda topary öçür
    if kat['takim_lideri'] == 1 and kat['takim_kodu']:
        db.execute('DELETE FROM takimlar WHERE takim_kodu = ?', (kat['takim_kodu'],))
        db.execute('UPDATE katilimcilar SET takim_kodu = NULL, takim_lideri = 0 WHERE takim_kodu = ?', (kat['takim_kodu'],))
    # Lider däl agza pozulanda, topar tablisasyndan arassala
    elif kat['takim_kodu'] and kat['takim_lideri'] == 0:
        team = db.execute('SELECT * FROM takimlar WHERE takim_kodu = ?', (kat['takim_kodu'],)).fetchone()
        if team:
            if team['uye1_referans'] == ref:
                db.execute('UPDATE takimlar SET uye1_referans = NULL WHERE takim_kodu = ?', (kat['takim_kodu'],))
            elif team['uye2_referans'] == ref:
                db.execute('UPDATE takimlar SET uye2_referans = NULL WHERE takim_kodu = ?', (kat['takim_kodu'],))
            elif team['uye3_referans'] == ref:
                db.execute('UPDATE takimlar SET uye3_referans = NULL WHERE takim_kodu = ?', (kat['takim_kodu'],))

    db.execute('DELETE FROM katilimcilar WHERE referans_kodu = ?', (ref,))
    db.commit()

    logger.info(f"Pozuldy: {ref}")
    return jsonify({'success': True, 'message': 'Katylyjy pozuldy!'})


@app.route('/api/katilimci/me')
@login_required
def api_katilimci_me():
    ref = session.get('user_ref')
    if not ref:
        return jsonify({'success': False, 'message': 'Giris edilmedi'}), 401
    db = get_db()
    kat = db.execute("""
        SELECT k.*, t.takim_adi 
        FROM katilimcilar k
        LEFT JOIN takimlar t ON k.takim_kodu = t.takim_kodu
        WHERE k.referans_kodu = ?
    """, (ref,)).fetchone()
    if not kat:
        session.clear()
        return jsonify({'success': False, 'message': 'Katylyjy tapylmady'}), 404
    return jsonify({'success': True, 'katilimci': dict(kat)})

@app.route('/api/katilimci/<ref_code>')
@login_required
def api_katilimci(ref_code):
    db = get_db()
    kat = db.execute("""
        SELECT k.referans_kodu, k.ad, k.telefon, k.takim_kodu, k.admin_onay, t.takim_adi 
        FROM katilimcilar k
        LEFT JOIN takimlar t ON k.takim_kodu = t.takim_kodu
        WHERE k.referans_kodu = ?
    """, (ref_code,)).fetchone()
    if not kat:
        return jsonify({'success': False})
    return jsonify({'success': True, 'katilimci': dict(kat)})

@app.route('/api/csrf-token')
def api_csrf_token():
    return jsonify({'success': True, 'csrf_token': generate_csrf_token()})

# ===================== NEW ROUTES =====================

@app.route('/turnir')
def turnir():
    return render_template('turnir.html')

@app.route('/turnir/goşul')
@app.route('/turnir/gosul')
@login_required
def turnir_gosul():
    return render_template('turnir_gosul.html')

@app.route('/magazyn')
def magazyn():
    return render_template('magazyn.html')

@app.route('/menyu')
def menyu():
    return render_template('menyu.html')

@app.route('/api/turnir-goşul', methods=['POST'])
@login_required
@limiter.limit("3 per minute")
def api_turnir_gosul():
    data = request.get_json() or {}

    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    pubg_id = sanitize(data.get('pubg_id', ''), 20)
    payment_phone = str(data.get('payment_phone', '')).strip()
    tournament_id = sanitize(data.get('tournament_id', ''), 50)

    # FIX: PUBG ID diňe san bolmaly
    if not pubg_id or len(pubg_id) < 8 or not pubg_id.isdigit():
        return jsonify({'success': False, 'message': 'PUBG ID diňe san bolmaly (minimum 8)!'})

    valid, phone_clean = validate_phone(payment_phone)
    if not valid:
        return jsonify({'success': False, 'message': 'Telefon belgisi nadogry!'})

    ref = session.get('user_ref', '')
    db = get_db()

    # Update user's PUBG ID and payment phone
    db.execute("""
        UPDATE katilimcilar 
        SET pubg_id = ?, payment_phone = ?, tournament_id = ?
        WHERE referans_kodu = ?
    """, (pubg_id, phone_clean, tournament_id, ref))
    db.commit()

    logger.info(f"Turnir goşul: {ref} -> {tournament_id}")
    return jsonify({'success': True, 'message': 'Turnira goşuldyňyz!'})

# ===================== START =====================

with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
