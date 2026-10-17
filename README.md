# SHUBA — Telegram bot + Mini App

Loyiha bitta asosiy `bot.py` faylida: Telegram bot, zamonaviy mini-do‘kon sahifasi, katalog, qidiruv, kategoriyalar, saralanganlar, savat, to‘lovsiz buyurtma va administrator paneli.

## 1. Render orqali ishga tushirish

1. ZIP faylni oching va ichidagi 4 ta faylni GitHub’dagi yangi repository’ga yuklang.
2. Render’da **New → Blueprint** orqali repository’ni tanlang (yoki **New → Web Service** tanlab `pip install -r requirements.txt` va `python bot.py` komandalarini kiriting).
3. `BOT_TOKEN` qiymatiga BotFather bergan tokenni kiriting. Tokenni GitHub kodiga yozmang.
4. Deploy tugagach, botga Telegram’da `/start` yuboring. Mini App URL’ni Render avtomatik oladi va bot menyu tugmasini sozlaydi.
5. Administratorni sozlash uchun botga `/myid` yuboring. Chiqqan raqamni Render → service → **Environment** bo‘limidagi `ADMIN_IDS` qiymatiga yozing (bir nechta ID bo‘lsa vergul bilan ajrating), so‘ng save/deploy qiling. Shunda buyurtmalar sizga Telegram orqali keladi va admin panel ochiladi.

`render.yaml` Render’ga build/start komandalarini oldindan beradi. `WEBAPP_URL` alohida yozish shart emas: Render’dagi `RENDER_EXTERNAL_URL` avtomatik ishlatiladi.

## 2. Lokal Windows’da ishga tushirish

Python 3.10+ o‘rnating. Terminalni loyiha papkasida oching:

```bat
py -m pip install -r requirements.txt
set BOT_TOKEN=BOTFATHER_TOKENINGIZ
set ADMIN_IDS=TELEGRAM_IDINGIZ
py bot.py
```

Lokal rejimda bot polling bilan ishlaydi. Telegram Mini App esa Telegram ichida ochilishi uchun HTTPS manzil kerak; Render deploy qilingandan so‘ng bu avtomatik sozlanadi.

## Mini App funksiyalari

- Mahsulot katalogi, kategoriyalar va qidiruv
- O‘zbekcha / ruscha interfeys
- Mahsulotni ko‘rish, saralanganlarga qo‘shish
- Savat va miqdorni o‘zgartirish
- Ism, telefon va manzil bilan to‘lovsiz buyurtma yuborish
- Buyurtmani administratorga Telegram orqali yuborish
- Mini App ichida administrator uchun tovar qo‘shish
- Bot buyruqlari: `/start`, `/products`, `/myid`, `/help`, `/addproduct`, `/deleteproduct`, `/orders`

Botda namunaviy katalog mavjud. Admin yangi tovarni Mini App panelidan yoki botda quyidagi formatda qo‘sha oladi:

```text
/addproduct Qora shuba | 3500000 | Klassik | https://example.com/photo.jpg | Qishki model
```

## Muhim eslatmalar

- `BOT_TOKEN` maxfiy ma’lumot. Uni hech qachon ochiq GitHub repository’ga joylamang.
- Hozirgi SQLite baza `shuba.db` faylida saqlanadi. Render bepul web-service fayl tizimi deploy/restart paytida doimiy saqlash kafolatini bermaydi; doimiy real savdo uchun Render Persistent Disk (mavjud tarifda) yoki tashqi PostgreSQL bazasini ulang va `DB_PATH` ni doimiy disk joyiga belgilang.
- Render bepul rejimida servis bo‘sh turganda uxlab qolishi mumkin, shu sababli Mini App birinchi ochilishida kechikish bo‘lishi mumkin.
- Namunaviy rasmlar tashqi Unsplash URL manzillaridan yuklanadi. O‘z mahsulotingiz uchun rasm URL manzillarini administrator panelidan kiriting.
- Bu boshlang‘ich loyiha buyurtma qabul qiladi; onlayn to‘lov, ombor hisobi, yetkazib berish integratsiyasi va maxsus mijozlar bazasi ulanmagan.
