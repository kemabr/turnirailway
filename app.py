# -*- coding: utf-8 -*-
import os
import random
import string
import sqlite3
import secrets
import re
import logging
import hashlib
from datetime import datetime
from functools import wraps
from html import escape as html_escape

import requests
from flask import Flask, render_template, request, jsonify, redirect, url_for, g, session, abort
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__, static_folder='static', static_url_path='/static')

# ENVIRONMENT VARIABLES
app.secret_key = os.environ.get('SECRET_KEY')
if not app.secret_key:
    app.secret_key = secrets.token_hex(32)
    logging.warning("SECRET_KEY bellenmedi, awto generasiya edildi!")

ADMIN_SIFRE_HASH = os.environ.get('ADMIN_SIFRE_HASH')
if not ADMIN_SIFRE_HASH:
    # Default: 'admin123' hash
    ADMIN_SIFRE_HASH = 'ef92b768b4298f4f9e2c4f7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0'
    logging.warning("ADMIN_SIFRE_HASH bellenmedi, default ulanylýar!")

CLOUDFLARE_WORKER_URL = os.environ.get('CLOUDFLARE_WORKER_URL', '')
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'turnuva.db')

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
                pubg_id TEXT NOT NULL,
                telefon TEXT NOT NULL,
                ulasim TEXT NOT NULL,
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
        db.execute("CREATE INDEX IF NOT EXISTS idx_katilimci_takim ON katilimcilar(takim_kodu)")
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
    token = secrets.token_urlsafe(32)
    session['csrf_token'] = token
    return token

def validate_csrf_token(token):
    return token and token == session.get('csrf_token')

def send_telegram_message(message):
    if not CLOUDFLARE_WORKER_URL:
        return False
    try:
        response = requests.post(f"{CLOUDFLARE_WORKER_URL}/send-message", json={'message': message}, timeout=10)
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

def validate_phone(phone):
    cleaned = phone.replace(' ', '').replace('-', '').replace('+', '')
    if len(cleaned) == 8 and cleaned.isdigit():
        return True, cleaned
    if len(cleaned) == 11 and cleaned.startswith('993') and cleaned[3:].isdigit() and len(cleaned[3:]) == 8:
        return True, cleaned[3:]
    if len(cleaned) == 12 and cleaned.startswith('993') and cleaned[3:].isdigit() and len(cleaned[3:]) == 9:
        return True, cleaned[3:]
    return False, None

def sanitize(text, max_len=100):
    if not text:
        return ''
    return html_escape(str(text).strip())[:max_len]

def check_password(password):
    """Paroly tekst bilen deňeşdirýär"""
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

@app.route('/kayit')
def kayit():
    if get_stats()['toplam'] >= get_stats()['yer_sany']:
        return redirect(url_for('index'))
    return render_template('kayit.html')

@app.route('/api/kayit-ol', methods=['POST'])
@limiter.limit("3 per minute")
def api_kayit_ol():
    data = request.get_json() or {}

    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ad = sanitize(data.get('ad', ''), 100)
    pubg_id = sanitize(data.get('pubg_id', ''), 50)
    telefon = str(data.get('telefon', '')).strip()
    ulasim = sanitize(data.get('ulasim', ''), 100)

    if not all([ad, pubg_id, telefon, ulasim]):
        return jsonify({'success': False, 'message': 'Ahli maglumatlary dolduryň!'})

    valid, telefon_clean = validate_phone(telefon)
    if not valid:
        return jsonify({'success': False, 'message': 'Telefon belgisi nadogry! Format: +993 XX XXX XXX ýa-da 8 san'})

    if not pubg_id.isdigit():
        return jsonify({'success': False, 'message': 'PUBG ID diňe san bolmaly!'})

    if len(ad) < 2:
        return jsonify({'success': False, 'message': 'Ad 2 harpdan uly bolmaly!'})

    db = get_db()
    try:
        db.execute('BEGIN IMMEDIATE')
        count = db.execute('SELECT COUNT(*) as s FROM katilimcilar').fetchone()['s']
        yer = int(get_ayar('turnir_yer_sany', '100'))
        if count >= yer:
            db.execute('ROLLBACK')
            return jsonify({'success': False, 'message': 'Ähli ýerler doldy!'})

        ref = generate_ref_code()
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute("INSERT INTO katilimcilar (referans_kodu, ad, pubg_id, telefon, ulasim, kayit_tarihi) VALUES (?, ?, ?, ?, ?, ?)",
                  (ref, ad, pubg_id, telefon_clean, ulasim, now))
        db.commit()
    except sqlite3.IntegrityError:
        db.execute('ROLLBACK')
        return jsonify({'success': False, 'message': 'Ýalňyşlyk! Gaýtadan synanyşyň.'})
    except Exception as e:
        db.execute('ROLLBACK')
        logger.error(f"Kayit hatasi: {e}")
        return jsonify({'success': False, 'message': 'Serwer ýalňyşlygy!'})

    msg = f"🎮 <b>TÄZE KATYLYJY!</b>\n\n👤 {ad}\n🆔 {pubg_id}\n📞 {telefon_clean}\n🔑 {ref}"
    send_telegram_message(msg)
    logger.info(f"Kayit: {ref} - {ad}")

    return jsonify({'success': True, 'referans_kodu': ref, 'message': 'Ustunlikli!'})

@app.route('/odeme/<ref_code>')
def odeme(ref_code):
    kat = get_db().execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref_code,)).fetchone()
    if not kat:
        return redirect(url_for('index'))
    return render_template('odeme.html', katilimci=kat)

@app.route('/api/odeme-yapildi', methods=['POST'])
@limiter.limit("5 per minute")
def api_odeme_yapildi():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    ref = data.get('referans_kodu', '')
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

@app.route('/profil/<ref_code>')
def profil(ref_code):
    db = get_db()
    kat = db.execute("""
        SELECT k.*, t.takim_adi, t.takim_kodu as t_kod
        FROM katilimcilar k
        LEFT JOIN takimlar t ON k.takim_kodu = t.takim_kodu
        WHERE k.referans_kodu = ?
    """, (ref_code,)).fetchone()
    if not kat:
        return redirect(url_for('index'))

    arkadaslar = []
    if kat['takim_kodu']:
        arkadaslar = db.execute("""
            SELECT ad, pubg_id, referans_kodu, admin_onay 
            FROM katilimcilar 
            WHERE takim_kodu = ? AND referans_kodu != ?
        """, (kat['takim_kodu'], ref_code)).fetchall()

    return render_template('profil.html', katilimci=kat, takim_arkadaslari=arkadaslar)

@app.route('/takim/<ref_code>')
def takim(ref_code):
    kat = get_db().execute('SELECT * FROM katilimcilar WHERE referans_kodu = ?', (ref_code,)).fetchone()
    if not kat:
        return redirect(url_for('index'))
    return render_template('takim.html', katilimci=kat)

@app.route('/api/takim-olustur', methods=['POST'])
@limiter.limit("3 per minute")
def api_takim_olustur():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    lider_ref = data.get('lider_ref', '')
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
@limiter.limit("3 per minute")
def api_takima_katil():
    data = request.get_json() or {}
    if not validate_csrf_token(data.get('csrf_token', '')):
        return jsonify({'success': False, 'message': 'CSRF token nadogry!'})

    uye_ref = data.get('uye_ref', '')
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

    db.execute("UPDATE katilimcilar SET takim_kodu = ? WHERE referans_kodu = ?", (takim_kodu, uye_ref))
    if not takim.get('uye1_referans'):
        db.execute('UPDATE takimlar SET uye1_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    elif not takim.get('uye2_referans'):
        db.execute('UPDATE takimlar SET uye2_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    elif not takim.get('uye3_referans'):
        db.execute('UPDATE takimlar SET uye3_referans = ? WHERE takim_kodu = ?', (uye_ref, takim_kodu))
    db.commit()

    msg = f"👥 <b>TOPARA TÄZE AGZA!</b>\n\nTopar: {takim['takim_adi']}\nKod: {takim_kodu}\n👤 {uye['ad']}"
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

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('admin_login'))

@app.route('/admin/panel')
@admin_required
def admin_panel():
    db = get_db()
    stats = db.execute("""
        SELECT COALESCE(COUNT(*), 0) as toplam,
               COALESCE(SUM(CASE WHEN odeme_durumu = 1 THEN 1 ELSE 0 END), 0) as odeme_yapan,
               COALESCE(SUM(CASE WHEN admin_onay = 1 THEN 1 ELSE 0 END), 0) as onaylanan
        FROM katilimcilar
    """).fetchone()

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

    # Eger lider bolsa, topary hem poz
    if kat['takim_lideri'] == 1 and kat['takim_kodu']:
        db.execute('DELETE FROM takimlar WHERE takim_kodu = ?', (kat['takim_kodu'],))
        db.execute('UPDATE katilimcilar SET takim_kodu = NULL, takim_lideri = 0 WHERE takim_kodu = ?', (kat['takim_kodu'],))

    db.execute('DELETE FROM katilimcilar WHERE referans_kodu = ?', (ref,))
    db.commit()

    logger.info(f"Pozuldy: {ref}")
    return jsonify({'success': True, 'message': 'Katylyjy pozuldy!'})

@app.route('/api/katilimci/<ref_code>')
def api_katilimci(ref_code):
    kat = db.execute("""
        SELECT k.*, t.takim_adi 
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

# ===================== START =====================

with app.app_context():
    init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
