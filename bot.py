import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import requests
from flask import Flask, jsonify, request, Response
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# SHUBA shop: Telegram bot + Telegram Mini App + SQLite in one main file.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("shuba")

BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
BASE_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "shuba.db")
WEBHOOK_SECRET = hashlib.sha256((BOT_TOKEN or "shuba-demo").encode("utf-8")).hexdigest()[:40]

web = Flask(__name__)
ptb_app = None
bot_loop = None

SEED_PRODUCTS = [
    {
        "title": "Klassik qora shuba", "category": "Klassik", "price": 3450000,
        "image_url": "https://images.unsplash.com/photo-1539109136881-3be0616acf4b?auto=format&fit=crop&w=1000&q=85",
        "description": "Har kuni va alohida kunlar uchun nafis klassik model.", "featured": 1,
    },
    {
        "title": "Qumrang premium", "category": "Premium", "price": 4200000,
        "image_url": "https://images.unsplash.com/photo-1529139574466-a303027c1d8b?auto=format&fit=crop&w=1000&q=85",
        "description": "Yumshoq tus, zamonaviy bichim va premium ko‘rinish.", "featured": 1,
    },
    {
        "title": "Qisqa shuba", "category": "Qisqa", "price": 2890000,
        "image_url": "https://images.unsplash.com/photo-1543076447-215ad9ba6923?auto=format&fit=crop&w=1000&q=85",
        "description": "Qulay, yengil va kundalik obrazlarga mos model.", "featured": 0,
    },
    {
        "title": "Uzun siluet", "category": "Premium", "price": 4750000,
        "image_url": "https://images.unsplash.com/photo-1551232864-3beef8b01a2e?auto=format&fit=crop&w=1000&q=85",
        "description": "Uzun bichim va ko‘zga tashlanadigan nafis siluet.", "featured": 1,
    },
    {
        "title": "Sut rang eco-fur", "category": "Eco", "price": 2490000,
        "image_url": "https://images.unsplash.com/photo-1548624149-f323ca265a47?auto=format&fit=crop&w=1000&q=85",
        "description": "Och rangli, zamonaviy va oson uyg‘unlashtiriladigan model.", "featured": 0,
    },
    {
        "title": "Kamarli shuba", "category": "Klassik", "price": 3650000,
        "image_url": "https://images.unsplash.com/photo-1483985988355-763728e1935b?auto=format&fit=crop&w=1000&q=85",
        "description": "Belni ajratib ko‘rsatuvchi kamarli bichim.", "featured": 0,
    },
]


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'Boshqa',
                price INTEGER NOT NULL,
                image_url TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                featured INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                address TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                telegram_user_id TEXT NOT NULL DEFAULT '',
                items_json TEXT NOT NULL,
                total INTEGER NOT NULL
            )
        """)
        count = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            now = utc_now()
            conn.executemany(
                "INSERT INTO products (title,category,price,image_url,description,featured,created_at) VALUES (?,?,?,?,?,?,?)",
                [(p["title"], p["category"], p["price"], p["image_url"], p["description"], p["featured"], now) for p in SEED_PRODUCTS],
            )
        conn.commit()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def money(value):
    return f"{int(value):,}".replace(",", " ") + " so‘m"


def is_admin(user_id):
    return bool(user_id and int(user_id) in ADMIN_IDS)


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def verify_init_data(init_data):
    """Validate Telegram Mini App initData and return the signed user object, if valid."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        auth_date = int(parsed.get("auth_date", "0"))
        if auth_date <= 0 or abs(int(time.time()) - auth_date) > 86400:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        user = json.loads(parsed.get("user", "{}"))
        return user if isinstance(user, dict) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def request_user():
    raw = request.headers.get("X-Telegram-Init-Data", "")
    return verify_init_data(raw)


def admin_user_from_request():
    user = request_user()
    if user and is_admin(safe_int(user.get("id"))):
        return user
    return None


def fetch_products():
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY featured DESC, id DESC").fetchall()
        return [dict(row) for row in rows]


def add_product(title, price, category, image_url, description):
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO products (title,category,price,image_url,description,featured,created_at) VALUES (?,?,?,?,?,0,?)",
            (title.strip(), category.strip() or "Boshqa", int(price), image_url.strip(), description.strip(), utc_now()),
        )
        conn.commit()
        return cur.lastrowid


def telegram_send(chat_id, text):
    if not BOT_TOKEN:
        return False
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=8,
        )
        if not response.ok:
            log.warning("Telegram sendMessage xatosi: %s", response.text[:300])
        return response.ok
    except requests.RequestException as exc:
        log.warning("Admin xabari yuborilmadi: %s", exc)
        return False


MINI_APP_HTML = r'''<!doctype html>
<html lang="uz">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<meta name="theme-color" content="#211817"><title>SHUBA — butik</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--ink:#211817;--muted:#887b76;--paper:#f7f2ee;--card:#fffdfa;--line:#eadfd8;--wine:#8d3949;--gold:#c69b70;--green:#586b53;--radius:20px}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}button,input,textarea,select{font:inherit}button{cursor:pointer}.app{max-width:1100px;margin:auto;padding:18px 16px 100px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:22px}.brand{display:flex;align-items:center;gap:10px;font-weight:900;letter-spacing:2px;font-size:18px}.brand-mark{display:grid;place-items:center;width:42px;height:42px;border-radius:15px;background:var(--ink);color:#fff;font-size:23px;box-shadow:0 8px 20px #2118171c}.brand small{display:block;font-size:9px;letter-spacing:3px;color:var(--muted);font-weight:700;margin-top:3px}.lang{display:flex;background:#ece2dc;border-radius:99px;padding:4px}.lang button{border:0;background:transparent;padding:7px 10px;border-radius:99px;font-size:11px;font-weight:800;color:var(--muted)}.lang button.active{background:white;color:var(--ink);box-shadow:0 2px 8px #0000000c}.hero{position:relative;overflow:hidden;background:var(--ink);border-radius:28px;color:#fff;padding:25px 22px 24px;margin-bottom:23px;min-height:205px;display:flex;align-items:center}.hero:after{content:'✦';position:absolute;right:20px;top:-52px;font-size:240px;line-height:1;color:#ffffff0a;transform:rotate(13deg)}.hero-content{position:relative;z-index:1;max-width:620px}.eyebrow{display:inline-flex;align-items:center;gap:7px;text-transform:uppercase;font-size:10px;letter-spacing:2px;font-weight:800;color:#e4bd9e;margin-bottom:11px}.eyebrow:before{content:'';width:22px;height:1px;background:var(--gold)}h1{font-family:Georgia,serif;font-weight:500;font-size:clamp(30px,7vw,49px);line-height:1.02;margin:0 0 12px;letter-spacing:-1px}.hero p{margin:0;color:#dacdca;line-height:1.5;font-size:13px;max-width:420px}.hero-pill{display:inline-flex;margin-top:17px;padding:9px 13px;border:1px solid #ffffff30;border-radius:50px;font-size:11px;font-weight:700;color:#fff}.section-head{display:flex;justify-content:space-between;align-items:end;gap:10px;margin:23px 0 13px}.section-head h2{font-size:20px;margin:0;letter-spacing:-.5px}.sub{font-size:11px;color:var(--muted);margin-top:5px}.search-wrap{position:relative;margin:17px 0 14px}.search-wrap span{position:absolute;left:15px;top:50%;transform:translateY(-50%);font-size:16px;color:#9b8b84}.search{width:100%;border:1px solid var(--line);background:#fffdfa;border-radius:15px;padding:14px 15px 14px 43px;outline:none;color:var(--ink);font-size:13px}.search:focus{border-color:#c9a78d;box-shadow:0 0 0 3px #c9a78d25}.chips{display:flex;gap:8px;overflow:auto;padding:2px 0 5px;scrollbar-width:none}.chips::-webkit-scrollbar{display:none}.chip{white-space:nowrap;border:1px solid var(--line);border-radius:99px;padding:9px 14px;background:#fffdfa;color:#796b65;font-size:11px;font-weight:700}.chip.active{background:var(--ink);color:white;border-color:var(--ink)}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.product{min-width:0;background:var(--card);border:1px solid #eee4dd;border-radius:19px;overflow:hidden;box-shadow:0 8px 25px #4a26120a}.photo-wrap{position:relative;background:#e9ded6;aspect-ratio: .81;overflow:hidden}.product-photo{width:100%;height:100%;object-fit:cover;display:block;transition:transform .4s}.product:hover .product-photo{transform:scale(1.04)}.tag{position:absolute;left:9px;top:9px;background:#fffdf4eF;border-radius:99px;padding:6px 8px;font-size:9px;font-weight:800;letter-spacing:.4px}.heart{position:absolute;right:9px;top:9px;width:31px;height:31px;border:0;border-radius:50%;background:#fffdf4ed;color:#8e7d75;font-size:17px}.heart.on{color:var(--wine)}.product-info{padding:11px 11px 12px}.category{font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:#a18472;font-weight:800}.product-title{font-size:13px;font-weight:800;margin:5px 0 9px;line-height:1.3;min-height:32px}.product-foot{display:flex;align-items:center;justify-content:space-between;gap:7px}.price{font-size:12px;font-weight:900;letter-spacing:-.2px}.add{width:31px;height:31px;flex:0 0 31px;border:0;border-radius:11px;background:var(--ink);color:#fff;font-size:20px;line-height:1}.empty{padding:34px 12px;text-align:center;color:var(--muted);font-size:13px;background:#fffdfa;border:1px dashed var(--line);border-radius:18px;grid-column:1/-1}.bottom-nav{position:fixed;z-index:20;bottom:0;left:0;right:0;padding:10px 12px max(12px,env(safe-area-inset-bottom));background:#fffdfaf0;backdrop-filter:blur(18px);border-top:1px solid #e9ded7}.bottom-inner{max-width:650px;margin:auto;display:grid;grid-template-columns:repeat(4,1fr);gap:4px}.nav-btn{border:0;background:transparent;color:#9b8b84;font-size:10px;font-weight:700;padding:6px 2px;border-radius:13px;display:flex;flex-direction:column;align-items:center;gap:4px}.nav-btn .ico{font-size:19px;line-height:1.2}.nav-btn.active{color:var(--wine);background:#f7ece8}.counter{position:absolute;margin-left:18px;margin-top:-4px;border-radius:10px;background:var(--wine);color:white;font-size:9px;padding:2px 5px}.modal{display:none;position:fixed;inset:0;background:#2118178a;z-index:50;align-items:flex-end;justify-content:center;padding:12px}.modal.show{display:flex}.sheet{background:var(--paper);width:min(100%,620px);max-height:92dvh;overflow:auto;border-radius:25px 25px 18px 18px;padding:18px 16px 22px;box-shadow:0 20px 80px #0004;animation:rise .18s ease-out}@keyframes rise{from{transform:translateY(20px);opacity:.7}to{transform:translateY(0);opacity:1}}.sheet-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}.sheet-head h3{margin:0;font-size:19px}.close{border:0;border-radius:50%;width:32px;height:32px;background:#eadfd8;color:var(--ink);font-size:20px}.detail-photo{width:100%;height:min(43vh,360px);object-fit:cover;border-radius:18px;background:#e8dcd4}.detail-desc{color:var(--muted);font-size:13px;line-height:1.55}.detail-price{font-size:21px;font-weight:900;margin:12px 0}.primary{width:100%;border:0;border-radius:15px;background:var(--wine);color:#fff;padding:14px 16px;font-size:13px;font-weight:800;box-shadow:0 8px 20px #8d39492b}.secondary{border:1px solid var(--line);background:#fffdfa;color:var(--ink);border-radius:13px;padding:11px 13px;font-weight:700;font-size:12px}.field{margin:12px 0}.field label{display:block;margin-bottom:6px;color:#756760;font-size:11px;font-weight:800}.field input,.field textarea,.field select{width:100%;border:1px solid var(--line);border-radius:13px;background:#fffdfa;padding:12px;outline:none;font-size:13px;color:var(--ink)}.field textarea{min-height:70px;resize:vertical}.cart-line{display:grid;grid-template-columns:60px minmax(0,1fr) auto;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--line)}.cart-line img{width:60px;height:68px;object-fit:cover;border-radius:11px}.cart-line strong{font-size:12px;display:block;margin-bottom:5px}.cart-line small{font-size:11px;color:var(--muted)}.qty{display:flex;align-items:center;gap:9px;margin-top:8px}.qty button{width:24px;height:24px;border:1px solid var(--line);background:white;border-radius:8px}.cart-sum{display:flex;justify-content:space-between;font-weight:900;padding:16px 0;font-size:15px}.admin{display:none;margin-top:24px;border:1px solid #d8c3b3;background:#f0e5dc;padding:15px;border-radius:18px}.admin h3{margin:0 0 8px;font-size:16px}.admin p{color:var(--muted);font-size:11px}.admin-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.admin-grid .wide{grid-column:1/-1}.admin input,.admin textarea{width:100%;border:1px solid #e0cfc3;border-radius:10px;background:#fffaf6;padding:10px;font-size:12px}.admin button{margin-top:9px}.order-card{padding:12px;margin:10px 0;background:#fffdfa;border:1px solid var(--line);border-radius:13px;font-size:12px;line-height:1.5}.order-card b{font-size:13px}.toast{position:fixed;left:50%;bottom:90px;transform:translateX(-50%);background:var(--ink);color:white;padding:12px 16px;border-radius:14px;z-index:100;font-size:12px;box-shadow:0 10px 30px #0002;max-width:90%;text-align:center;display:none}.footer{margin-top:30px;text-align:center;color:#a3948c;font-size:10px;line-height:1.7}
@media(min-width:700px){.app{padding:25px 25px 110px}.hero{padding:36px 35px;min-height:245px}.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.product-info{padding:14px}.product-title{font-size:14px}.price{font-size:13px}.photo-wrap{aspect-ratio:.9}.hero p{font-size:14px}}
@media(min-width:1000px){.grid{grid-template-columns:repeat(4,minmax(0,1fr))}.app{padding-left:35px;padding-right:35px}.hero{min-height:270px}}
</style></head>
<body><main class="app">
<header class="topbar"><div class="brand"><div class="brand-mark">♧</div><div>SHUBA<small>FUR & STYLE</small></div></div><div class="lang"><button id="langUz" class="active" onclick="setLang('uz')">UZ</button><button id="langRu" onclick="setLang('ru')">RU</button></div></header>
<section class="hero"><div class="hero-content"><div class="eyebrow" id="eyebrow">YANGI MAVSUM · 2026</div><h1 id="heroTitle">Sovuq kunlarda<br>ham o‘z uslubingiz.</h1><p id="heroText">Nafis shubalar, zamonaviy siluetlar va sizga mos tanlov — barchasi bir butikda.</p><div class="hero-pill" id="heroPill">✦ Yangi kolleksiya bilan tanishing</div></div></section>
<div class="search-wrap"><span>⌕</span><input id="search" class="search" placeholder="Shuba qidirish..." oninput="renderProducts()"></div>
<div id="mainView"><div class="section-head"><div><h2 id="sectionTitle">Kolleksiya</h2><div id="sectionSub" class="sub">Siz uchun tanlangan modellar</div></div><span class="sub" id="productCount"></span></div><div id="chips" class="chips"></div><div id="grid" class="grid"></div></div>
<section class="admin" id="adminPanel"><h3 id="adminTitle">✦ Administrator paneli</h3><p id="adminHelp">Yangi shuba qo‘shish uchun ma’lumotlarni kiriting.</p><form id="productForm"><div class="admin-grid"><input name="title" required placeholder="Tovar nomi"><input name="price" required type="number" min="1" placeholder="Narxi (so‘m)"><input name="category" required placeholder="Kategoriya"><input name="image_url" required type="url" placeholder="Rasm URL manzili"><textarea class="wide" name="description" placeholder="Tavsif"></textarea></div><button class="primary" type="submit" id="productSave">Tovarni saqlash</button></form><div class="section-head"><h2 id="ordersTitle">So‘nggi buyurtmalar</h2><button class="secondary" onclick="loadOrders()" id="refreshOrders">Yangilash</button></div><div id="ordersList"></div></section>
<div class="footer" id="footerText">SHUBA — nafislik har bir detaldan boshlanadi.<br>Buyurtma to‘lovsiz, menejer bilan kelishiladi.</div>
</main>
<nav class="bottom-nav"><div class="bottom-inner"><button class="nav-btn active" data-view="catalog" onclick="switchView('catalog',this)"><span class="ico">⌂</span><span id="navCatalog">Katalog</span></button><button class="nav-btn" data-view="favorites" onclick="switchView('favorites',this)"><span class="ico">♡</span><span id="navFav">Saralangan</span></button><button class="nav-btn" data-view="cart" onclick="openCart()"><span class="ico">▢<span id="cartCount" class="counter" style="display:none">0</span></span><span id="navCart">Savat</span></button><button class="nav-btn" data-view="profile" onclick="switchView('profile',this)"><span class="ico">☻</span><span id="navProfile">Aloqa</span></button></div></nav>
<div class="modal" id="productModal" onclick="if(event.target===this)closeModal('productModal')"><div class="sheet"><div class="sheet-head"><h3 id="detailTitle">Tovar</h3><button class="close" onclick="closeModal('productModal')">×</button></div><div id="detailContent"></div></div></div>
<div class="modal" id="cartModal" onclick="if(event.target===this)closeModal('cartModal')"><div class="sheet"><div class="sheet-head"><h3 id="cartTitle">Sizning savatingiz</h3><button class="close" onclick="closeModal('cartModal')">×</button></div><div id="cartItems"></div><div class="cart-sum"><span id="totalLabel">Jami</span><span id="cartTotal">0 so‘m</span></div><button class="primary" id="checkoutBtn" onclick="openCheckout()">Buyurtmani rasmiylashtirish</button><p class="sub" id="payNote">To‘lov talab qilinmaydi — menejer buyurtmani tasdiqlaydi.</p></div></div>
<div class="modal" id="checkoutModal" onclick="if(event.target===this)closeModal('checkoutModal')"><div class="sheet"><div class="sheet-head"><h3 id="checkoutTitle">Yetkazib berish ma’lumotlari</h3><button class="close" onclick="closeModal('checkoutModal')">×</button></div><form id="checkoutForm"><div class="field"><label id="nameLabel">Ism-familiya</label><input name="name" required autocomplete="name" placeholder="Ismingiz"></div><div class="field"><label id="phoneLabel">Telefon raqam</label><input name="phone" required autocomplete="tel" placeholder="+998 90 123 45 67"></div><div class="field"><label id="addressLabel">Manzil</label><input name="address" required placeholder="Shahar, tuman, ko‘cha"></div><div class="field"><label id="noteLabel">Izoh (ixtiyoriy)</label><textarea name="note" placeholder="O‘lcham, rang yoki boshqa istak"></textarea></div><button class="primary" type="submit" id="submitOrder">Buyurtmani yuborish</button><p class="sub" id="checkoutNote">Buyurtma kelgach, siz bilan bog‘lanamiz.</p></form></div></div>
<div class="modal" id="contactModal" onclick="if(event.target===this)closeModal('contactModal')"><div class="sheet"><div class="sheet-head"><h3 id="contactTitle">Biz bilan bog‘laning</h3><button class="close" onclick="closeModal('contactModal')">×</button></div><p class="detail-desc" id="contactText">Savolingizni buyurtma izohida yozing — menejer buyurtmani tasdiqlash uchun siz bilan bog‘lanadi.</p><button class="primary" onclick="contactManager()" id="contactBtn">Buyurtma berish</button><p class="sub" id="contactTip">Buyurtma yuborish uchun katalogdan model tanlab, savatga qo‘shing.</p></div></div>
<div class="toast" id="toast"></div>
<script>
const tg=window.Telegram?.WebApp; if(tg){tg.ready();tg.expand();try{tg.setHeaderColor('#211817');tg.setBackgroundColor('#f7f2ee')}catch(e){}}
let lang='uz',products=[],category='all',view='catalog',activeProduct=null,cart=JSON.parse(localStorage.getItem('shuba_cart')||'{}'),favs=JSON.parse(localStorage.getItem('shuba_favs')||'[]'),isAdmin=false;
const tr={uz:{eyebrow:'YANGI MAVSUM · 2026',heroTitle:'Sovuq kunlarda<br>ham o‘z uslubingiz.',heroText:'Nafis shubalar, zamonaviy siluetlar va sizga mos tanlov — barchasi bir butikda.',heroPill:'✦ Yangi kolleksiya bilan tanishing',search:'Shuba qidirish...',collection:'Kolleksiya',sub:'Siz uchun tanlangan modellar',all:'Barchasi',favTitle:'Saralanganlar',favSub:'Yoqtirgan modellaringiz',emptyFav:'Hali saralangan tovar yo‘q.',emptyProducts:'Bu bo‘limda tovar topilmadi.',navCatalog:'Katalog',navFav:'Saralangan',navCart:'Savat',navProfile:'Aloqa',adminTitle:'✦ Administrator paneli',adminHelp:'Yangi shuba qo‘shish uchun ma’lumotlarni kiriting.',productSave:'Tovarni saqlash',ordersTitle:'So‘nggi buyurtmalar',refreshOrders:'Yangilash',footer:'SHUBA — nafislik har bir detaldan boshlanadi.<br>Buyurtma to‘lovsiz, menejer bilan kelishiladi.',view:'Batafsil ko‘rish',add:'Savatga qo‘shish',added:'Tovar savatga qo‘shildi',cartTitle:'Sizning savatingiz',emptyCart:'Savatingiz hozircha bo‘sh.',total:'Jami',checkout:'Buyurtmani rasmiylashtirish',payNote:'To‘lov talab qilinmaydi — menejer buyurtmani tasdiqlaydi.',checkoutTitle:'Yetkazib berish ma’lumotlari',name:'Ism-familiya',phone:'Telefon raqam',address:'Manzil',note:'Izoh (ixtiyoriy)',submit:'Buyurtmani yuborish',checkoutNote:'Buyurtma kelgach, siz bilan bog‘lanamiz.',sent:'Buyurtmangiz qabul qilindi! Menejer siz bilan bog‘lanadi.',sentNoAdmin:'Buyurtma saqlandi. Administrator bildirishnomasi sozlanmagan.',failed:'Xatolik yuz berdi. Qayta urinib ko‘ring.',contactTitle:'Biz bilan bog‘laning',contactText:'Savolingizni buyurtma izohida yozing — menejer buyurtmani tasdiqlash uchun siz bilan bog‘lanadi.',contactBtn:'Buyurtma berish',contactTip:'Buyurtma yuborish uchun katalogdan model tanlab, savatga qo‘shing.',noOrders:'Hozircha buyurtmalar yo‘q.',ordersLoadError:'Buyurtmalarni yuklab bo‘lmadi.',saved:'Tovar saqlandi.',remove:'O‘chirish',missing:'Savatga kamida bitta tovar qo‘shing.',currency:'so‘m',newTag:'TANLOV',success:'Tovar qo‘shildi.',invalid:'Barcha kerakli maydonlarni to‘ldiring.'},ru:{eyebrow:'НОВЫЙ СЕЗОН · 2026',heroTitle:'Стиль, который<br>согревает.',heroText:'Элегантные шубы, современные силуэты и модели для вашего образа — в одном бутике.',heroPill:'✦ Откройте новую коллекцию',search:'Поиск шубы...',collection:'Коллекция',sub:'Модели, выбранные для вас',all:'Все',favTitle:'Избранное',favSub:'Модели, которые вам понравились',emptyFav:'В избранном пока пусто.',emptyProducts:'В этой категории товаров нет.',navCatalog:'Каталог',navFav:'Избранное',navCart:'Корзина',navProfile:'Контакты',adminTitle:'✦ Панель администратора',adminHelp:'Заполните поля, чтобы добавить новую шубу.',productSave:'Сохранить товар',ordersTitle:'Последние заказы',refreshOrders:'Обновить',footer:'SHUBA — элегантность в каждой детали.<br>Оплата не требуется: заказ подтвердит менеджер.',view:'Подробнее',add:'В корзину',added:'Товар добавлен в корзину',cartTitle:'Ваша корзина',emptyCart:'Корзина пока пуста.',total:'Итого',checkout:'Оформить заказ',payNote:'Оплата не требуется — заказ подтвердит менеджер.',checkoutTitle:'Данные для доставки',name:'Имя и фамилия',phone:'Номер телефона',address:'Адрес',note:'Комментарий (необязательно)',submit:'Отправить заказ',checkoutNote:'После получения заказа мы свяжемся с вами.',sent:'Заказ принят! Менеджер свяжется с вами.',sentNoAdmin:'Заказ сохранён. Уведомления администратора не настроены.',failed:'Произошла ошибка. Попробуйте ещё раз.',contactTitle:'Свяжитесь с нами',contactText:'Оставьте вопрос в комментарии к заказу — менеджер свяжется с вами для подтверждения.',contactBtn:'Оформить заказ',contactTip:'Чтобы оформить заказ, выберите модель в каталоге и добавьте её в корзину.',noOrders:'Пока нет заказов.',ordersLoadError:'Не удалось загрузить заказы.',saved:'Товар сохранён.',remove:'Удалить',missing:'Добавьте в корзину хотя бы один товар.',currency:'сум',newTag:'ВЫБОР',success:'Товар добавлен.',invalid:'Заполните все обязательные поля.'}};
const T=()=>tr[lang];
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function cash(n){return new Intl.NumberFormat(lang==='ru'?'ru-RU':'uz-UZ').format(Number(n)||0)+' '+T().currency}
function saveState(){localStorage.setItem('shuba_cart',JSON.stringify(cart));localStorage.setItem('shuba_favs',JSON.stringify(favs));updateCount()}
function toast(msg){const el=document.getElementById('toast');el.textContent=msg;el.style.display='block';clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.style.display='none',2600)}
function closeModal(id){document.getElementById(id).classList.remove('show')}
function setLang(l){lang=l;document.getElementById('langUz').classList.toggle('active',l==='uz');document.getElementById('langRu').classList.toggle('active',l==='ru');document.documentElement.lang=l;document.getElementById('eyebrow').textContent=T().eyebrow;document.getElementById('heroTitle').innerHTML=T().heroTitle;document.getElementById('heroText').textContent=T().heroText;document.getElementById('heroPill').textContent=T().heroPill;document.getElementById('search').placeholder=T().search;document.getElementById('navCatalog').textContent=T().navCatalog;document.getElementById('navFav').textContent=T().navFav;document.getElementById('navCart').textContent=T().navCart;document.getElementById('navProfile').textContent=T().navProfile;document.getElementById('adminTitle').textContent=T().adminTitle;document.getElementById('adminHelp').textContent=T().adminHelp;document.getElementById('productSave').textContent=T().productSave;document.getElementById('ordersTitle').textContent=T().ordersTitle;document.getElementById('refreshOrders').textContent=T().refreshOrders;document.getElementById('footerText').innerHTML=T().footer;document.getElementById('cartTitle').textContent=T().cartTitle;document.getElementById('totalLabel').textContent=T().total;document.getElementById('checkoutBtn').textContent=T().checkout;document.getElementById('payNote').textContent=T().payNote;document.getElementById('checkoutTitle').textContent=T().checkoutTitle;document.getElementById('nameLabel').textContent=T().name;document.getElementById('phoneLabel').textContent=T().phone;document.getElementById('addressLabel').textContent=T().address;document.getElementById('noteLabel').textContent=T().note;document.getElementById('submitOrder').textContent=T().submit;document.getElementById('checkoutNote').textContent=T().checkoutNote;document.getElementById('contactTitle').textContent=T().contactTitle;document.getElementById('contactText').textContent=T().contactText;document.getElementById('contactBtn').textContent=T().contactBtn;document.getElementById('contactTip').textContent=T().contactTip;renderProducts();if(document.getElementById('cartModal').classList.contains('show'))renderCart()}
async function loadProducts(){try{const r=await fetch('/api/products');products=await r.json();renderProducts()}catch(e){toast(T().failed)}}
function categories(){let cats=[...new Set(products.map(p=>p.category).filter(Boolean))];let el=document.getElementById('chips');el.innerHTML=`<button class="chip ${category==='all'?'active':''}" onclick="pickCategory('all')">${T().all}</button>`+cats.map(c=>`<button class="chip ${category===c?'active':''}" onclick="pickCategory('${esc(c).replace(/'/g,"\\'")}')">${esc(c)}</button>`).join('')}
function pickCategory(c){category=c;categories();renderProducts()}
function renderProducts(){categories();let q=(document.getElementById('search').value||'').toLowerCase().trim();let list=products.filter(p=>(category==='all'||p.category===category)&&(view!=='favorites'||favs.includes(p.id))&&(`${p.title} ${p.category} ${p.description}`.toLowerCase().includes(q)));document.getElementById('sectionTitle').textContent=view==='favorites'?T().favTitle:T().collection;document.getElementById('sectionSub').textContent=view==='favorites'?T().favSub:T().sub;document.getElementById('productCount').textContent=list.length;const grid=document.getElementById('grid');if(!list.length){grid.innerHTML=`<div class="empty">${view==='favorites'?T().emptyFav:T().emptyProducts}</div>`;return}grid.innerHTML=list.map(p=>`<article class="product"><div class="photo-wrap" onclick="showProduct(${p.id})"><img class="product-photo" loading="lazy" src="${esc(p.image_url)}" alt="${esc(p.title)}" onerror="this.style.opacity='.2'"><span class="tag">${esc(p.category||T().newTag)}</span><button class="heart ${favs.includes(p.id)?'on':''}" aria-label="favorite" onclick="event.stopPropagation();toggleFav(${p.id})">${favs.includes(p.id)?'♥':'♡'}</button></div><div class="product-info"><div class="category">${esc(p.category)}</div><div class="product-title" onclick="showProduct(${p.id})">${esc(p.title)}</div><div class="product-foot"><div class="price">${cash(p.price)}</div><button class="add" title="${T().add}" onclick="addToCart(${p.id})">+</button></div></div></article>`).join('')}
function switchView(v,btn){if(v==='profile'){document.getElementById('contactModal').classList.add('show');document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b===btn));return}view=v;document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b===btn));renderProducts();window.scrollTo({top:0,behavior:'smooth'})}
function toggleFav(id){favs=favs.includes(id)?favs.filter(x=>x!==id):[...favs,id];saveState();renderProducts();}
function addToCart(id){cart[id]=(cart[id]||0)+1;saveState();toast(T().added);if(tg?.HapticFeedback)try{tg.HapticFeedback.impactOccurred('light')}catch(e){}}
function updateCount(){let n=Object.values(cart).reduce((a,b)=>a+b,0);let c=document.getElementById('cartCount');c.textContent=n;c.style.display=n?'inline-block':'none'}
function showProduct(id){activeProduct=products.find(p=>p.id===id);if(!activeProduct)return;let p=activeProduct;document.getElementById('detailTitle').textContent=p.title;document.getElementById('detailContent').innerHTML=`<img class="detail-photo" src="${esc(p.image_url)}" alt="${esc(p.title)}" onerror="this.style.opacity='.2'"><div class="category" style="margin-top:15px">${esc(p.category)}</div><p class="detail-desc">${esc(p.description||'')}</p><div class="detail-price">${cash(p.price)}</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:9px"><button class="secondary" onclick="toggleFav(${p.id});showProduct(${p.id})">${favs.includes(p.id)?'♥':'♡'} ${lang==='uz'?'Saralangan':'Избранное'}</button><button class="primary" style="padding:11px" onclick="addToCart(${p.id});closeModal('productModal')">${T().add}</button></div>`;document.getElementById('productModal').classList.add('show')}
function openCart(){document.querySelectorAll('.nav-btn').forEach(b=>b.classList.toggle('active',b.dataset.view==='cart'));renderCart();document.getElementById('cartModal').classList.add('show')}
function renderCart(){let items=products.filter(p=>cart[p.id]);let box=document.getElementById('cartItems');if(!items.length){box.innerHTML=`<div class="empty">${T().emptyCart}</div>`;document.getElementById('cartTotal').textContent=cash(0);document.getElementById('checkoutBtn').disabled=true;document.getElementById('checkoutBtn').style.opacity='.5';return}document.getElementById('checkoutBtn').disabled=false;document.getElementById('checkoutBtn').style.opacity='1';box.innerHTML=items.map(p=>`<div class="cart-line"><img src="${esc(p.image_url)}" alt="" onerror="this.style.opacity='.2'"><div><strong>${esc(p.title)}</strong><small>${cash(p.price)}</small><div class="qty"><button onclick="changeQty(${p.id},-1)">−</button><span>${cart[p.id]}</span><button onclick="changeQty(${p.id},1)">+</button></div></div><b style="font-size:11px">${cash(p.price*cart[p.id])}</b></div>`).join('');document.getElementById('cartTotal').textContent=cash(items.reduce((sum,p)=>sum+p.price*cart[p.id],0))}
function changeQty(id,d){cart[id]=(cart[id]||0)+d;if(cart[id]<=0)delete cart[id];saveState();renderCart()}
function openCheckout(){if(!Object.keys(cart).length){toast(T().missing);return}closeModal('cartModal');document.getElementById('checkoutModal').classList.add('show')}
async function apiHeaders(){return {'Content-Type':'application/json','X-Telegram-Init-Data':tg?.initData||''}}
document.getElementById('checkoutForm').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const body={name:f.get('name'),phone:f.get('phone'),address:f.get('address'),note:f.get('note'),items:Object.entries(cart).map(([id,qty])=>({id:Number(id),qty:Number(qty)})),initData:tg?.initData||''};let btn=document.getElementById('submitOrder');btn.disabled=true;btn.textContent='…';try{let r=await fetch('/api/order',{method:'POST',headers:await apiHeaders(),body:JSON.stringify(body)});let data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'order');cart={};saveState();e.currentTarget.reset();closeModal('checkoutModal');toast(data.notified_admin?T().sent:T().sentNoAdmin);if(tg?.HapticFeedback)try{tg.HapticFeedback.notificationOccurred('success')}catch(e){}}catch(err){toast(T().failed)}finally{btn.disabled=false;btn.textContent=T().submit}});
document.getElementById('productForm').addEventListener('submit',async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const body=Object.fromEntries(f.entries());body.price=Number(body.price);try{let r=await fetch('/api/products',{method:'POST',headers:await apiHeaders(),body:JSON.stringify(body)});let data=await r.json();if(!r.ok||!data.ok)throw new Error(data.error||'product');e.currentTarget.reset();await loadProducts();toast(T().success)}catch(err){toast(T().failed)}});
async function checkAdmin(){try{let r=await fetch('/api/admin/check',{headers:await apiHeaders()});let d=await r.json();isAdmin=!!d.admin;document.getElementById('adminPanel').style.display=isAdmin?'block':'none';if(isAdmin)loadOrders()}catch(e){}}
async function loadOrders(){if(!isAdmin)return;let box=document.getElementById('ordersList');try{const r=await fetch('/api/admin/orders',{headers:await apiHeaders()});const d=await r.json();if(!r.ok||!d.ok)throw 0;if(!d.orders.length){box.innerHTML=`<div class="empty">${T().noOrders}</div>`;return}box.innerHTML=d.orders.map(o=>`<div class="order-card"><b>#${o.id} · ${esc(o.customer_name)}</b><br>${esc(o.phone)} · ${esc(o.address)}<br>${esc(o.created_at)}<br><b>${cash(o.total)}</b><br>${o.items.map(i=>esc(i.title)+' × '+i.qty).join(', ')}</div>`).join('')}catch(e){box.innerHTML=`<div class="empty">${T().ordersLoadError}</div>`}}
function contactManager(){closeModal('contactModal');if(Object.keys(cart).length){openCart()}else{view='catalog';const btn=document.querySelector('.nav-btn[data-view="catalog"]');switchView('catalog',btn);toast(lang==='uz'?'Katalogdan model tanlab, savatga qo‘shing.':'Выберите модель в каталоге и добавьте её в корзину.')}}
loadProducts();checkAdmin();updateCount();
</script></body></html>'''


@web.get("/")
def mini_app():
    return Response(MINI_APP_HTML, mimetype="text/html")


@web.get("/health")
def health():
    return jsonify({"ok": True, "app": "SHUBA", "mini_app": bool(BASE_URL)})


@web.get("/api/products")
def api_products():
    return jsonify(fetch_products())


@web.post("/api/products")
def api_add_product():
    if not admin_user_from_request():
        return jsonify({"ok": False, "error": "Administrator ruxsati kerak."}), 403
    data = request.get_json(silent=True) or {}
    title = str(data.get("title", "")).strip()[:120]
    category = str(data.get("category", "Boshqa")).strip()[:60]
    description = str(data.get("description", "")).strip()[:1000]
    image_url = str(data.get("image_url", "")).strip()[:1000]
    price = safe_int(data.get("price"), 0)
    if not title or price <= 0 or not image_url.startswith(("https://", "http://")):
        return jsonify({"ok": False, "error": "Nomi, musbat narx va rasm URL manzilini tekshiring."}), 400
    new_id = add_product(title, price, category, image_url, description)
    return jsonify({"ok": True, "id": new_id})


@web.delete("/api/products/<int:product_id>")
def api_delete_product(product_id):
    if not admin_user_from_request():
        return jsonify({"ok": False, "error": "Administrator ruxsati kerak."}), 403
    with db_connect() as conn:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
    return jsonify({"ok": True})


@web.get("/api/admin/check")
def api_admin_check():
    user = admin_user_from_request()
    return jsonify({"admin": bool(user)})


@web.get("/api/admin/orders")
def api_admin_orders():
    if not admin_user_from_request():
        return jsonify({"ok": False, "error": "Administrator ruxsati kerak."}), 403
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 30").fetchall()
    orders = []
    for row in rows:
        item = dict(row)
        try:
            item["items"] = json.loads(item.pop("items_json"))
        except (ValueError, TypeError):
            item["items"] = []
            item.pop("items_json", None)
        orders.append(item)
    return jsonify({"ok": True, "orders": orders})


@web.post("/api/order")
def api_create_order():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:120]
    phone = str(data.get("phone", "")).strip()[:50]
    address = str(data.get("address", "")).strip()[:250]
    note = str(data.get("note", "")).strip()[:600]
    init_data = str(data.get("initData", "") or request.headers.get("X-Telegram-Init-Data", ""))
    tg_user = verify_init_data(init_data) if init_data else None
    tg_user_id = str(tg_user.get("id", "")) if tg_user else ""
    items = data.get("items")
    if not name or not phone or not address or not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "Ism, telefon, manzil va kamida bitta tovar kiriting."}), 400
    # Validate cart and calculate price on the server; do not trust browser totals.
    clean_items, total = [], 0
    with db_connect() as conn:
        for raw in items[:30]:
            if not isinstance(raw, dict):
                continue
            product_id = safe_int(raw.get("id"))
            qty = safe_int(raw.get("qty"))
            if product_id <= 0 or qty < 1 or qty > 20:
                continue
            product = conn.execute("SELECT id,title,price,category FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                continue
            subtotal = int(product["price"]) * qty
            clean_items.append({"id": product["id"], "title": product["title"], "category": product["category"], "price": product["price"], "qty": qty, "subtotal": subtotal})
            total += subtotal
        if not clean_items:
            return jsonify({"ok": False, "error": "Savatdagi tovarlar topilmadi."}), 400
        cur = conn.execute(
            "INSERT INTO orders (created_at,customer_name,phone,address,note,telegram_user_id,items_json,total) VALUES (?,?,?,?,?,?,?,?)",
            (utc_now(), name, phone, address, note, tg_user_id, json.dumps(clean_items, ensure_ascii=False), total),
        )
        order_id = cur.lastrowid
        conn.commit()
    lines = [f"🧥 SHUBA — YANGI BUYURTMA #{order_id}", f"👤 Ism: {name}", f"📞 Telefon: {phone}", f"📍 Manzil: {address}"]
    if tg_user_id:
        lines.append(f"Telegram ID: {tg_user_id}")
    if note:
        lines.append(f"💬 Izoh: {note}")
    lines += ["", "🛍 Tovarlar:"]
    lines.extend([f"• {item['title']} × {item['qty']} — {money(item['subtotal'])}" for item in clean_items])
    lines.append(f"\n💰 Jami: {money(total)}")
    notified = False
    if ADMIN_IDS:
        send_results = [telegram_send(admin_id, "\n".join(lines)) for admin_id in sorted(ADMIN_IDS)]
        notified = any(send_results)
    return jsonify({"ok": True, "order_id": order_id, "notified_admin": notified})


@web.post("/telegram/webhook")
def telegram_webhook():
    if not ptb_app or not bot_loop or not bot_loop.is_running():
        return jsonify({"ok": False}), 503
    if BASE_URL and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        return jsonify({"ok": False}), 403
    try:
        payload = request.get_json(force=True)
        update = Update.de_json(payload, ptb_app.bot)
        future = asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), bot_loop)
        future.result(timeout=12)
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Webhook update xatosi: %s", exc)
        # Return 200 on handler errors to avoid endlessly replaying malformed updates.
        return jsonify({"ok": True})


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = context.user_data.get("lang", "uz")
    if lang == "ru":
        text = "🧥 Добро пожаловать в SHUBA!\n\nОткройте каталог, выберите модель и отправьте заказ без оплаты. Менеджер свяжется с вами для подтверждения."
        open_label = "🛍 Открыть каталог"
        langs = "🇺🇿 O‘zbekcha"
    else:
        text = "🧥 SHUBA butikiga xush kelibsiz!\n\nKatalogdan model tanlang va to‘lovsiz buyurtma yuboring. Buyurtmani tasdiqlash uchun menejer siz bilan bog‘lanadi."
        open_label = "🛍 Katalogni ochish"
        langs = "🇷🇺 Русский"
    keyboard = []
    if BASE_URL:
        keyboard.append([InlineKeyboardButton(open_label, web_app=WebAppInfo(url=BASE_URL))])
    keyboard.append([InlineKeyboardButton(langs, callback_data="lang_ru" if lang == "uz" else "lang_uz")])
    keyboard.append([InlineKeyboardButton("📦 Tovarlar / Товары", callback_data="show_products")])
    await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Komandalar / Команды:\n/start — bosh menyu\n/products — katalogdagi tovarlar\n/myid — Telegram ID\n/addproduct — administrator: tovar qo‘shish\n/deleteproduct ID — administrator: tovarni o‘chirish\n/orders — administrator: so‘nggi buyurtmalar\n\nMini App pastdagi menyu tugmasidan ham ochiladi."
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.effective_message.reply_text(f"Telegram ID: {user.id}\nUni Render'dagi ADMIN_IDS qiymatiga kiriting.")


async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = fetch_products()[:15]
    if not items:
        await update.effective_message.reply_text("Hozircha tovarlar yo‘q.")
        return
    text = "🧥 SHUBA — katalog\n\n" + "\n".join(f"{i+1}. {p['title']} — {money(p['price'])} ({p['category']})" for i, p in enumerate(items))
    if BASE_URL:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛍 Mini App katalogi", web_app=WebAppInfo(url=BASE_URL))]])
        await update.effective_message.reply_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data in ("lang_uz", "lang_ru"):
        context.user_data["lang"] = "ru" if query.data == "lang_ru" else "uz"
        await start(update, context)
        return
    if query.data == "show_products":
        await products_command(update, context)


async def addproduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat administrator uchun. Avval /myid orqali ID'ingizni aniqlab, Render'da ADMIN_IDS ni sozlang.")
        return
    raw = update.effective_message.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|", 4)]
    if len(parts) < 5:
        await update.effective_message.reply_text(
            "Format:\n/addproduct Nomi | Narxi | Kategoriya | Rasm URL | Tavsif\n\nMasalan:\n/addproduct Qora shuba | 3500000 | Klassik | https://example.com/photo.jpg | Qishki model"
        )
        return
    title, price_text, category, image_url, description = parts
    price = safe_int(price_text)
    if not title or price <= 0 or not image_url.startswith(("https://", "http://")):
        await update.effective_message.reply_text("Nomi, musbat narx va to‘g‘ri rasm URL manzilini kiriting.")
        return
    product_id = add_product(title, price, category, image_url, description)
    await update.effective_message.reply_text(f"✅ Tovar qo‘shildi: {title}\nID: {product_id} · {money(price)}")


async def deleteproduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat administrator uchun.")
        return
    if not context.args:
        await update.effective_message.reply_text("Foydalanish: /deleteproduct 12")
        return
    product_id = safe_int(context.args[0])
    if product_id <= 0:
        await update.effective_message.reply_text("Tovar ID raqamini kiriting.")
        return
    with db_connect() as conn:
        cur = conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
    await update.effective_message.reply_text("✅ Tovar o‘chirildi." if cur.rowcount else "Bunday ID bilan tovar topilmadi.")


async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu buyruq faqat administrator uchun.")
        return
    with db_connect() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await update.effective_message.reply_text("Hozircha buyurtmalar yo‘q.")
        return
    for order in rows:
        try:
            items = json.loads(order["items_json"])
        except (ValueError, TypeError):
            items = []
        item_lines = "\n".join(f"• {x.get('title','Tovar')} × {x.get('qty',1)}" for x in items)
        txt = (
            f"📦 Buyurtma #{order['id']}\n🕒 {order['created_at']}\n👤 {order['customer_name']}\n"
            f"📞 {order['phone']}\n📍 {order['address']}\n{item_lines}\n💰 Jami: {money(order['total'])}"
        )
        await update.effective_message.reply_text(txt[:4000])


async def post_init_setup():
    # Kept for reference; the runtime setup below uses an explicit event loop in one thread.
    return None


def bot_thread_main():
    global ptb_app, bot_loop
    bot_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(bot_loop)
    ptb_app = Application.builder().token(BOT_TOKEN).build()
    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("help", help_command))
    ptb_app.add_handler(CommandHandler("myid", myid))
    ptb_app.add_handler(CommandHandler("products", products_command))
    ptb_app.add_handler(CommandHandler("addproduct", addproduct_command))
    ptb_app.add_handler(CommandHandler("deleteproduct", deleteproduct_command))
    ptb_app.add_handler(CommandHandler("orders", orders_command))
    ptb_app.add_handler(CallbackQueryHandler(callback_handler))

    try:
        bot_loop.run_until_complete(ptb_app.initialize())
        bot_loop.run_until_complete(ptb_app.start())
        bot_loop.run_until_complete(ptb_app.bot.set_my_commands([
            BotCommand("start", "Bosh menyu / Главная"),
            BotCommand("products", "Tovarlar katalogi / Каталог"),
            BotCommand("myid", "Telegram ID ni ko‘rish"),
            BotCommand("help", "Yordam / Помощь"),
        ]))
        if BASE_URL:
            bot_loop.run_until_complete(ptb_app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🧥 SHUBA", web_app=WebAppInfo(url=BASE_URL))
            ))
            bot_loop.run_until_complete(ptb_app.bot.set_webhook(
                url=f"{BASE_URL}/telegram/webhook",
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            ))
            log.info("Telegram webhook sozlandi: %s/telegram/webhook", BASE_URL)
        else:
            bot_loop.run_until_complete(ptb_app.updater.start_polling(drop_pending_updates=False))
            log.info("Telegram polling rejimida ishga tushdi. Mini App uchun HTTPS WEBAPP_URL kerak.")
        bot_loop.run_forever()
    except Exception:
        log.exception("Telegram bot ishga tushmadi")
    finally:
        try:
            if ptb_app and ptb_app.updater and ptb_app.updater.running:
                bot_loop.run_until_complete(ptb_app.updater.stop())
            if ptb_app and ptb_app.running:
                bot_loop.run_until_complete(ptb_app.stop())
            if ptb_app:
                bot_loop.run_until_complete(ptb_app.shutdown())
        except Exception:
            pass
        try:
            bot_loop.close()
        except Exception:
            pass


def main():
    init_db()
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN topilmadi. Render Environment bo‘limida BOT_TOKEN qo‘shing yoki lokalda muhit o‘zgaruvchisini belgilang."
        )
    thread = threading.Thread(target=bot_thread_main, name="telegram-bot", daemon=True)
    thread.start()
    log.info("SHUBA web server 0.0.0.0:%s da ishga tushmoqda", PORT)
    # One process intentionally serves both the Mini App and Telegram webhook.
    web.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
