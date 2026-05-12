import logging
import asyncio
import aiosqlite
import aiohttp
import os
from datetime import datetime, timedelta
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from dotenv import load_dotenv

# Carrega as chaves do arquivo .env
load_dotenv()

# --- CONFIGURAÇÕES ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CLIENT_ID = os.getenv("SYNC_CLIENT_ID")
CLIENT_SECRET = os.getenv("SYNC_CLIENT_SECRET")
CHANNEL_ID = -1003954036870
ADMIN_ID = 8086722916
DB_NAME = "assinaturas.db"
WEBHOOK_PORT = 8080 

# Validação de segurança
if not TOKEN or not CLIENT_ID or not CLIENT_SECRET:
    print("❌ ERRO: Variáveis de ambiente não encontradas no arquivo .env!")
    exit(1)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Preços e Prazos (Vitalício R$ 79,90)
PLANS = {
    "15": {"price": 29.90, "stars": 650, "days": 15, "label": "👀 15 DIAS"},
    "30": {"price": 49.90, "stars": 1100, "days": 30, "label": "🔥 30 DIAS"},
    "vitalicio": {"price": 79.90, "stars": 1800, "days": 9999, "label": "👑 VITALÍCIO"}
}

# --- BANCO DE DADOS ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS membros 
            (user_id BIGINT PRIMARY KEY, plan TEXT, expire_date TIMESTAMP, status TEXT)''')
        await db.commit()

# --- INTEGRAÇÃO SYNCPAY ---
async def get_syncpay_pix(user_id, plan_key):
    base_url = "https://api.syncpayments.com.br/api/partner/v1"
    async with aiohttp.ClientSession() as session:
        try:
            # 1. Login
            auth_payload = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
            async with session.post(f"{base_url}/auth-token", json=auth_payload) as auth_resp:
                if auth_resp.status != 200: return None, None
                auth_data = await auth_resp.json()
                access_token = auth_data.get("access_token")

            # 2. Cash-in (PIX)
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            payload = {
                "amount": PLANS[plan_key]['price'],
                "paymentMethod": "PIX",
                "external_id": f"{user_id}:{plan_key}",
                "customer": {
                    "name": f"Cliente {user_id}",
                    "email": f"u{user_id}@t.me",
                    "document": "68516002934",
                    "phone": "47999999999"
                }
            }
            async with session.post(f"{base_url}/cash-in", json=payload, headers=headers) as resp:
                if resp.status in [200, 201]:
                    data = await resp.json()
                    res = data.get("data", data)
                    return res.get("pix_code") or res.get("qrcode"), res.get("qrcode_url")
                return None, None
        except: return None, None

# --- ATIVAÇÃO DE ACESSO ---
async def activate_user(user_id, plan_key):
    expire_at = datetime.now() + timedelta(days=PLANS[plan_key]['days'])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO membros VALUES (?, ?, ?, ?)", (user_id, plan_key, expire_at, 'active'))
        await db.commit()
    try:
        # Gera link de uso único
        link = await bot.create_chat_invite_link(chat_id=CHANNEL_ID, member_limit=1)
        await bot.send_message(user_id, f"🎉 **PAGAMENTO APROVADO!**\n\nSeu acesso foi liberado.\n🔗 Link: {link.invite_link}", parse_mode="Markdown")
    except Exception as e:
        # Fallback se as permissões de admin falharem
        await bot.send_message(user_id, "✅ Pago! Peça seu link ao suporte @Canabidioi (Erro: Permissões de Admin)")

# --- HANDLERS ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    buttons = [
        [{"text": "👑 VITALÍCIO - R$ 79,90", "callback_data": "sel_vitalicio"}],
        [{"text": "🔥 30 DIAS - R$ 49,90", "callback_data": "sel_30"}],
        [{"text": "👀 15 DIAS - R$ 29,90", "callback_data": "sel_15"}],
        [{"text": "📊 MINHA ASSINATURA", "callback_data": "check_status"}]
    ]
    if message.from_user.id == ADMIN_ID:
        buttons.append([{"text": "🛠️ TESTAR APROVAÇÃO (DONO)", "callback_data": "admin_test_approve"}])
        
    markup = {"inline_keyboard": buttons}
    await message.answer(f"🤫 *Psiu, {message.from_user.first_name}...*\nEscolha seu plano:", reply_markup=markup, parse_mode="Markdown")

@dp.callback_query(F.data == "admin_test_approve")
async def admin_test(callback: types.CallbackQuery):
    if callback.from_user.id == ADMIN_ID:
        await callback.message.answer("🧪 Simulando aprovação do Vitalício...")
        await activate_user(callback.from_user.id, "vitalicio")
    await callback.answer()

@dp.callback_query(F.data.startswith("sel_"))
async def process_sel(callback: types.CallbackQuery):
    plan = callback.data.split("_")[1]
    markup = {"inline_keyboard": [
        [{"text": "💠 Pagar via PIX", "callback_data": f"pix_{plan}"}],
        [{"text": "⭐ Pagar via Stars", "callback_data": f"stars_{plan}"}],
        [{"text": "⬅️ Voltar", "callback_data": "back_to_start"}]
    ]}
    await callback.message.edit_text(f"Plano: {PLANS[plan]['label']}\nComo deseja pagar?", reply_markup=markup)
    await callback.answer()

@dp.callback_query(F.data == "back_to_start")
async def back_start(callback: types.CallbackQuery):
    buttons = [[{"text": f"{v['label']} - R$ {v['price']}", "callback_data": f"sel_{k}"}] for k,v in PLANS.items()]
    if callback.from_user.id == ADMIN_ID:
        buttons.append([{"text": "🛠️ TESTAR APROVAÇÃO (DONO)", "callback_data": "admin_test_approve"}])
    await callback.message.edit_text("Escolha seu plano:", reply_markup={"inline_keyboard": buttons})
    await callback.answer()

@dp.callback_query(F.data.startswith("pix_"))
async def process_pix(callback: types.CallbackQuery):
    plan = callback.data.split("_")[1]
    wait = await callback.message.answer("⏳ Gerando PIX...")
    pix, qr = await get_syncpay_pix(callback.from_user.id, plan)
    if pix:
        await bot.send_photo(callback.message.chat.id, photo=qr or f"https://api.qrserver.com/v1/create-qr-code/?data={pix}", 
                             caption=f"💠 **PIX COPIA E COLA**\n\n`{pix}`", parse_mode="Markdown")
        await wait.delete()
    else:
        await wait.edit_text("❌ Erro ao gerar. Verifique o painel SyncPay.")
    await callback.answer()

@dp.callback_query(F.data.startswith("stars_"))
async def process_stars(callback: types.CallbackQuery):
    p = callback.data.split("_")[1]
    await bot.send_invoice(callback.message.chat.id, title=PLANS[p]['label'], description="VIP", payload=f"stars:{p}", currency="XTR", prices=[{"label": "VIP", "amount": PLANS[p]['stars']}] )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: types.PreCheckoutQuery):
    await bot.answer_pre_checkout_query(query.id, ok=True)

@dp.message(F.successful_payment)
async def stars_success(message: types.Message):
    await activate_user(message.from_user.id, message.successful_payment.invoice_payload.split(":")[1])

# --- WEBHOOK ---
async def handle_webhook(request):
    try:
        data = await request.json()
        if data.get("status") in ["paid", "completed", "approved"]:
            uid, pkey = data.get("external_id").split(":")
            await activate_user(int(uid), pkey)
        return web.Response(text="OK")
    except: return web.Response(text="Error", status=400)

async def main():
    await init_db()
    app = web.Application()
    app.router.add_post('/webhook', handle_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT).start()
    logging.info("Bot Online!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())