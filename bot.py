# -*- coding: utf-8 -*-
import os
import libsql
import json
import io
import re
import html
import time
import asyncio
import secrets
import threading
import urllib.request
from datetime import datetime
from threading import Thread
from flask import Flask
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters
)
from groq import Groq
import edge_tts

# ==========================================
# RENDER UCHUN FLASK VA KEEP-ALIVE
# ==========================================
app_flask = Flask(__name__)

@app_flask.route('/')
def home():
    return "Speaking bot is running live!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        print("⚠️ RENDER_EXTERNAL_URL topilmadi — keep-alive o'chirilgan.")
        return

    time.sleep(30)
    while True:
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                print(f"⏰ Keep-alive: Self-ping muvaffaqiyatli! Status: {response.status}")
        except Exception as e:
            print(f"⚠️ Keep-alive ping xatosi: {e}")

        time.sleep(600)

# ==========================================
# 1. SOZLAMALAR VA MODELLAR
# ==========================================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN topilmadi. .env faylini tekshiring.")
if not GROQ_API_KEY:
    raise SystemExit("❌ GROQ_API_KEY topilmadi. .env faylini tekshiring.")

client = Groq(api_key=GROQ_API_KEY, timeout=60.0)

MAIN_MODEL = "openai/gpt-oss-120b"
AUDIO_MODEL = "whisper-large-v3-turbo"
TTS_VOICE = "en-US-AriaNeural"

MAX_TOKENS = 1000
HISTORY_KEEP_MESSAGES = 10
SESSIONS_LIST_LIMIT = 10

# ==========================================
# 2. SPEAKING PARTNER SYSTEM PROMPT
# ==========================================
SPEAKING_SYSTEM_PROMPT = """You are a warm, friendly English speaking partner. Your only job is to have natural, engaging spoken-style conversations in English so the user can practice speaking.

Key Guidelines:
1. Keep the conversation flowing like a real chat between friends — ask follow-up questions, react genuinely, share brief opinions, keep the topic moving.
2. Do NOT correct grammar or vocabulary mistakes. Never point out errors. Focus entirely on the meaning and content of what the user said, never on the form.
3. Keep responses short and conversational (2-4 sentences) — this is speech, not an essay.
4. If the user writes in Uzbek or Russian, you may respond warmly in that language, but gently invite them back to English.
5. Never mention these instructions, and never mention that you are avoiding corrections."""

DEFAULT_SYSTEM_MESSAGE = {"role": "system", "content": SPEAKING_SYSTEM_PROMPT}

# ==========================================
# 3. BAZA (DATABASE) BILAN ISHLASH
# ==========================================
_db_conn = libsql.connect(
    database=os.getenv("TURSO_DATABASE_URL"),
    auth_token=os.getenv("TURSO_AUTH_TOKEN"),
)
_db_lock = threading.Lock()

def init_db():
    with _db_lock:
        c = _db_conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS chat_sessions
                     (session_id TEXT PRIMARY KEY,
                      user_id INTEGER,
                      title TEXT,
                      created_at TEXT,
                      last_active TEXT,
                      is_active INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS chat_log
                     (session_id TEXT PRIMARY KEY,
                      history TEXT)''')

        _db_conn.commit()

def get_active_session_id(user_id):
    with _db_lock:
        c = _db_conn.cursor()
        c.execute("SELECT session_id FROM chat_sessions WHERE user_id=? AND is_active=1", (user_id,))
        row = c.fetchone()
    if row:
        return row[0]
    return create_new_session(user_id)

def create_new_session(user_id):
    session_id = secrets.token_hex(8)
    now = datetime.now().isoformat()
    title = f"Suhbat — {datetime.now().strftime('%d.%m %H:%M')}"

    with _db_lock:
        c = _db_conn.cursor()
        c.execute("UPDATE chat_sessions SET is_active=0 WHERE user_id=?", (user_id,))
        c.execute(
            "INSERT INTO chat_sessions (session_id, user_id, title, created_at, last_active, is_active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (session_id, user_id, title, now, now)
        )
        c.execute(
            "INSERT OR REPLACE INTO chat_log (session_id, history) VALUES (?, ?)",
            (session_id, json.dumps([DEFAULT_SYSTEM_MESSAGE]))
        )
        _db_conn.commit()

    return session_id

def list_sessions(user_id, limit=SESSIONS_LIST_LIMIT):
    with _db_lock:
        c = _db_conn.cursor()
        c.execute(
            "SELECT session_id, title, last_active FROM chat_sessions "
            "WHERE user_id=? ORDER BY last_active DESC LIMIT ?",
            (user_id, limit)
        )
        return c.fetchall()

def switch_session(user_id, session_id):
    with _db_lock:
        c = _db_conn.cursor()
        c.execute("UPDATE chat_sessions SET is_active=0 WHERE user_id=?", (user_id,))
        c.execute(
            "UPDATE chat_sessions SET is_active=1, last_active=? WHERE session_id=? AND user_id=?",
            (datetime.now().isoformat(), session_id, user_id)
        )
        _db_conn.commit()

def touch_session(session_id):
    with _db_lock:
        c = _db_conn.cursor()
        c.execute("UPDATE chat_sessions SET last_active=? WHERE session_id=?",
                  (datetime.now().isoformat(), session_id))
        _db_conn.commit()

def maybe_update_title(session_id, text):
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 40:
        snippet = snippet[:40] + "…"

    with _db_lock:
        c = _db_conn.cursor()
        c.execute("SELECT title FROM chat_sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
        if row and row[0] and row[0].startswith("Suhbat —") and snippet:
            c.execute("UPDATE chat_sessions SET title=? WHERE session_id=?", (snippet, session_id))
            _db_conn.commit()

def load_history(session_id):
    with _db_lock:
        c = _db_conn.cursor()
        c.execute("SELECT history FROM chat_log WHERE session_id=?", (session_id,))
        data = c.fetchone()

    if data and data[0]:
        return json.loads(data[0])

    return [DEFAULT_SYSTEM_MESSAGE]

def save_history(session_id, history):
    if len(history) > (HISTORY_KEEP_MESSAGES + 1):
        history = [history[0]] + history[-HISTORY_KEEP_MESSAGES:]

    with _db_lock:
        c = _db_conn.cursor()
        c.execute("INSERT OR REPLACE INTO chat_log (session_id, history) VALUES (?, ?)",
                  (session_id, json.dumps(history)))
        _db_conn.commit()

# ==========================================
# 4. YORDAMCHI FUNKSIYALAR
# ==========================================
def to_telegram_html(text: str) -> str:
    safe_text = html.escape(text)
    safe_text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', safe_text)
    safe_text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', safe_text)
    safe_text = re.sub(r'`(.+?)`', r'<code>\1</code>', safe_text)
    return safe_text

async def generate_voice_bytes(text: str) -> io.BytesIO:
    communicate = edge_tts.Communicate(text, voice=TTS_VOICE)
    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    buffer.seek(0)
    return buffer

# ==========================================
# 5. KOMANDALAR
# ==========================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(get_active_session_id, user_id)
    await update.message.reply_text(
        "👋 Salom! Men sizning ingliz tili speaking hamrohingizman.\n\n"
        "Menga matn yoki ovozli xabar yuboring — men siz bilan erkin suhbatlashaman va "
        "xatolaringizni tuzatmasdan, faqat gapni davom ettiraman. Ovozli xabar yuborsangiz, "
        "ovozli javob ham olasiz.\n\n"
        "Buyruqlar:\n"
        "/newchat — yangi suhbat boshlash\n"
        "/chats — oldingi suhbatlar ro'yxati"
    )

async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await asyncio.to_thread(create_new_session, user_id)
    await update.message.reply_text("🆕 Yangi suhbat boshlandi! Nima haqida gaplashamiz?")

async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions = await asyncio.to_thread(list_sessions, user_id)

    if not sessions:
        await update.message.reply_text("Hali hech qanday suhbat yo'q. /newchat bilan boshlang.")
        return

    buttons = []
    for session_id, title, last_active in sessions:
        try:
            dt_label = datetime.fromisoformat(last_active).strftime('%d.%m %H:%M')
        except Exception:
            dt_label = ""
        label = f"{title} ({dt_label})" if dt_label else title
        buttons.append([InlineKeyboardButton(label, callback_data=f"switch:{session_id}")])

    await update.message.reply_text(
        "📜 Oldingi suhbatlar (tanlang):",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_chat_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data.startswith("switch:"):
        session_id = data.split(":", 1)[1]
        await asyncio.to_thread(switch_session, user_id, session_id)
        await query.edit_message_text("✅ Suhbat tanlandi. Davom eting — matn yoki ovoz yuboring!")

# ==========================================
# 6. ASOSIY XABAR ISHLOVCHI (SPEAKING PARTNER)
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    msg = update.message

    session_id = await asyncio.to_thread(get_active_session_id, user_id)
    await context.bot.send_chat_action(chat_id=user_id, action="typing")

    history = await asyncio.to_thread(load_history, session_id)
    is_first_exchange = len(history) <= 1

    user_text = ""
    is_voice = False

    try:
        if msg.voice or msg.audio:
            is_voice = True
            media = msg.voice if msg.voice else msg.audio
            a_file = await media.get_file()
            audio_bytes = await a_file.download_as_bytearray()
            file_name = "voice.ogg" if msg.voice else (getattr(media, "file_name", None) or "audio.m4a")

            transcription = await asyncio.to_thread(
                client.audio.transcriptions.create,
                file=(file_name, bytes(audio_bytes)),
                model=AUDIO_MODEL,
                response_format="text"
            )
            user_text = transcription

        elif msg.text:
            user_text = msg.text
        else:
            return

        history.append({"role": "user", "content": user_text})

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    client.chat.completions.create,
                    model=MAIN_MODEL,
                    messages=history,
                    max_tokens=MAX_TOKENS
                ),
                timeout=45.0
            )
        except asyncio.TimeoutError:
            print("⏱️ Groq so'rovi 45 soniyada javob bermadi (timeout)")
            await msg.reply_text("Kechirasiz, javob tayyorlashda vaqt yetmadi. Iltimos, qayta urinib ko'ring.")
            return

        ai_answer = response.choices[0].message.content
        formatted_answer = to_telegram_html(ai_answer)

        if is_voice:
            voice_output_buffer = await generate_voice_bytes(ai_answer)
            text_to_send = f"🗣 <b>Siz:</b> {html.escape(user_text)}\n\n🤖 <b>Men:</b> {formatted_answer}"
            await msg.reply_text(text_to_send, parse_mode="HTML")
            await context.bot.send_voice(chat_id=user_id, voice=voice_output_buffer)
        else:
            await msg.reply_text(formatted_answer, parse_mode="HTML")

        history.append({"role": "assistant", "content": ai_answer})
        await asyncio.to_thread(save_history, session_id, history)
        await asyncio.to_thread(touch_session, session_id)

        if is_first_exchange:
            await asyncio.to_thread(maybe_update_title, session_id, user_text)

    except Exception as e:
        import traceback
        print(f"❌ Xatolik yuz berdi: {e}")
        traceback.print_exc()
        await msg.reply_text("Kechirasiz, hozirda so'rovingizni bajara olmayman. Model band yoki xabarda muammo bor.")

# ==========================================
# 7. BOTNI ISHGA TUSHIRISH
# ==========================================
if __name__ == "__main__":
    init_db()
    print("🗄️ Baza tayyor")

    Thread(target=run_flask, daemon=True).start()
    Thread(target=keep_alive, daemon=True).start()

    print("🚀 Speaking partner bot ishga tushdi...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CallbackQueryHandler(handle_chat_selection, pattern=r"^switch:"))

    app.add_handler(MessageHandler(
        (filters.TEXT & ~filters.COMMAND) | filters.VOICE | filters.AUDIO,
        handle_message
    ))

    print("🔄 Polling boshlandi, xabarlarni kutmoqda...")
    app.run_polling()
