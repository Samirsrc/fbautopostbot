import os
from flask import Flask, request
import threading
import asyncio
import requests
import re
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

# Utilisez les variables d'environnement pour vos tokens/secrets
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
PAGE_ID = os.environ.get("PAGE_ID")

app = Flask(__name__)
user_buffers = {}
validation_buffers = {}

def send_message_to_messenger(recipient_id, message):
    print(f"DEBUG send_message_to_messenger: {recipient_id} -> {message}")
    url = "https://graph.facebook.com/v17.0/me/messages"
    params = {"access_token": PAGE_ACCESS_TOKEN}
    data = {
        "recipient": {"id": recipient_id},
        "message": {"text": message}
    }
    try:
        r = requests.post(url, params=params, json=data, timeout=5)
        print("DEBUG Messenger API response:", r.status_code, r.text)
    except Exception as e:
        print("Erreur lors de l'envoi Messenger:", e)

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def is_date_valid(date_str):
    # Validation pour format سنة/شهر/يوم (exemple: 15/10/2025)
    return bool(re.match(r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/[0-9]{4}$", date_str))

def convert_date_to_ar_format(date_str):
    # Transforme jj/mm/aaaa -> aaaa/mm/jj pour l'affichage arabe
    m = re.match(r"^(0[1-9]|[12][0-9]|3[01])/(0[1-9]|1[0-2])/[0-9]{4}$", date_str)
    if not m:
        return date_str
    jj, mm, aaaa = date_str.split("/")
    return f"{aaaa}/{mm}/{jj}"

def get_user_name(sender_id):
    url = f"https://graph.facebook.com/{sender_id}"
    params = {"access_token": PAGE_ACCESS_TOKEN, "fields": "first_name,last_name"}
    try:
        r = requests.get(url, params=params, timeout=5)
        data = r.json()
        return f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    except Exception as e:
        print("Erreur fetch nom Messenger:", e)
        return f"ID {sender_id}"

def publish_on_facebook(message, image_urls=None):
    if not image_urls:
        url = f"https://graph.facebook.com/{PAGE_ID}/feed"
        resp = requests.post(url, params={
            "access_token": PAGE_ACCESS_TOKEN,
            "message": message
        })
        return resp.json()
    else:
        photo_ids = []
        for image_url in image_urls:
            upload_url = f"https://graph.facebook.com/{PAGE_ID}/photos"
            resp = requests.post(upload_url, params={
                "access_token": PAGE_ACCESS_TOKEN,
                "url": image_url,
                "published": False
            })
            res = resp.json()
            if "id" in res:
                photo_ids.append(res["id"])
        post_url = f"https://graph.facebook.com/{PAGE_ID}/feed"
        attached_media = [{"media_fbid": pid} for pid in photo_ids]
        resp = requests.post(
            post_url,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "message": message,
                "attached_media": attached_media
            }
        )
        return resp.json()

async def telegram_post_message_for_validation(bot, photo_urls, lieu, date, sender_name, sender_id):
    message = (
        f"Nouvelle demande de publication :\n"
        f"Nom de l'expéditeur : {sender_name}\n"
        f"ID Messenger : {sender_id}\n"
        f"Lieu : {lieu}\n"
        f"Date : {date}"
    )
    buttons = [
        [InlineKeyboardButton("📝 Modifier le lieu", callback_data="edit_lieu"),
         InlineKeyboardButton("📝 Modifier la date", callback_data="edit_date")],
        [InlineKeyboardButton("🗑️ Supprimer une photo", callback_data="delete_photo")],
        [InlineKeyboardButton("✅ Valider", callback_data="valider"),
         InlineKeyboardButton("❌ Refuser", callback_data="refuser")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    msg_ids = []
    if not photo_urls:
        msg = await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, reply_markup=reply_markup)
        msg_ids.append(msg.message_id)
    elif len(photo_urls) == 1:
        msg = await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo_urls[0], caption=message, reply_markup=reply_markup)
        msg_ids.append(msg.message_id)
    else:
        for i, chunk in enumerate(chunk_list(photo_urls, 10)):
            medias = []
            for idx, url in enumerate(chunk):
                if i == 0 and idx == 0:
                    medias.append(InputMediaPhoto(media=url, caption=message))
                else:
                    medias.append(InputMediaPhoto(media=url))
            msgs = await bot.send_media_group(chat_id=TELEGRAM_CHAT_ID, media=medias)
            msg_ids.extend([m.message_id for m in msgs])
        confirm_msg = await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="Veuillez valider ou modifier la publication ci-dessus.", reply_markup=reply_markup)
        msg_ids.append(confirm_msg.message_id)
    return msg_ids

async def async_send_to_telegram(photo_urls, lieu, date, sender_name, sender_id):
    bot = Bot(TELEGRAM_TOKEN)
    msg_ids = await telegram_post_message_for_validation(bot, photo_urls, lieu, date, sender_name, sender_id)
    for msg_id in msg_ids:
        validation_buffers[msg_id] = {
            "photos": photo_urls.copy(),
            "lieu": lieu,
            "date": date,
            "sender_name": sender_name,
            "sender_id": sender_id,
            "state": "awaiting",
        }

def send_to_telegram_for_validation(photo_urls, lieu, date, sender_name, sender_id):
    try:
        asyncio.get_running_loop()
        asyncio.create_task(
            async_send_to_telegram(photo_urls, lieu, date, sender_name, sender_id)
        )
    except RuntimeError:
        asyncio.run(
            async_send_to_telegram(photo_urls, lieu, date, sender_name, sender_id)
        )

async def start(update, context):
    await update.message.reply_text("Bot de validation prêt !")

async def validation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message_id = query.message.message_id if hasattr(query, "message") else None
    buf = validation_buffers.get(message_id)
    if not buf:
        await query.answer("Impossible de retrouver les infos du post.")
        return

    if buf.get("state") == "done":
        await query.answer("Déjà traité.")
        return

    if query.data == "edit_lieu":
        buf["state"] = "editing_lieu"
        await query.message.reply_text("Envoie le nouveau lieu en réponse à ce message.")
        await query.answer()
    elif query.data == "edit_date":
        buf["state"] = "editing_date"
        await query.message.reply_text("أرسل التاريخ بالصيغة: سنة/شهر/يوم (مثال: 15/10/2025) بالرد على هذه الرسالة.")
        await query.answer()
    elif query.data == "delete_photo":
        if not buf["photos"]:
            await query.answer("Aucune photo à supprimer.")
            return
        buttons = []
        for i, url in enumerate(buf["photos"]):
            buttons.append([InlineKeyboardButton(f"Supprimer photo {i+1}", callback_data=f"delete_photo_{i}")])
        buttons.append([InlineKeyboardButton("Annuler", callback_data="cancel_delete_photo")])
        markup = InlineKeyboardMarkup(buttons)
        await query.message.reply_text("Clique sur la photo à supprimer :", reply_markup=markup)
        await query.answer()
    elif query.data.startswith("delete_photo_"):
        idx = int(query.data.split("_")[-1])
        if 0 <= idx < len(buf["photos"]):
            del buf["photos"][idx]
            await query.message.reply_text("Photo supprimée.")
        else:
            await query.message.reply_text("Indice invalide.")
        bot = Bot(TELEGRAM_TOKEN)
        await telegram_post_message_for_validation(bot, buf["photos"], buf["lieu"], buf["date"], buf["sender_name"], buf["sender_id"])
        buf["state"] = "awaiting"
        await query.answer()
    elif query.data == "cancel_delete_photo":
        buf["state"] = "awaiting"
        await query.answer("Suppression annulée.")
    elif query.data == "valider":
        buf["state"] = "done"
        texte = (
            f"🗓️ التاريخ : {convert_date_to_ar_format(buf['date'])}\n"
            f"📍 المكان : {buf['lieu']}\n\n"
            "🌿 صور توثق النشاطات الدورية التي يقوم بها أعواننا للعناية بالمساحات الخضراء في ولاية وهران، وذلك في إطار الجهود المستمرة لتزيين وتحسين المحيط.\n\n"
            "#مؤسسة_وهران_خضراء\n"
            "#ولاية_وهران"
        )
        fb_result = publish_on_facebook(
            message=texte,
            image_urls=buf["photos"]
        )
        if getattr(query.message, "photo", None):
            await query.edit_message_caption(
                caption="✅ Publication validée et publiée sur Facebook !"
            )
        else:
            await query.edit_message_text(
                text="✅ Publication validée et publiée sur Facebook !"
            )
        print("Publication Facebook :", fb_result)
        validation_buffers.pop(message_id, None)
    elif query.data == "refuser":
        buf["state"] = "done"
        if getattr(query.message, "photo", None):
            await query.edit_message_caption(
                caption="❌ Publication refusée."
            )
        else:
            await query.edit_message_text(
                text="❌ Publication refusée."
            )
        validation_buffers.pop(message_id, None)
    else:
        await query.answer("Action non reconnue.")

async def edit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_to = update.message.reply_to_message
    if not reply_to:
        await update.message.reply_text("Merci de répondre au message de demande de modification.")
        return

    msg_id = reply_to.message_id
    buf = validation_buffers.get(msg_id)

    if not buf:
        # Recherche d'un buffer en mode édition (sécurité)
        for b in validation_buffers.values():
            if b.get("state") in ["editing_lieu", "editing_date"]:
                buf = b
                break
        if not buf:
            await update.message.reply_text("Impossible de trouver la publication à éditer.")
            return

    if buf.get("state") == "editing_lieu":
        buf["lieu"] = update.message.text.strip()
        buf["state"] = "awaiting"
        await update.message.reply_text("Lieu modifié.")
        bot = Bot(TELEGRAM_TOKEN)
        await telegram_post_message_for_validation(bot, buf["photos"], buf["lieu"], buf["date"], buf["sender_name"], buf["sender_id"])
    elif buf.get("state") == "editing_date":
        date_text = update.message.text.strip()
        if is_date_valid(date_text):
            buf["date"] = date_text
            buf["state"] = "awaiting"
            await update.message.reply_text("Date modifiée.")
            bot = Bot(TELEGRAM_TOKEN)
            await telegram_post_message_for_validation(bot, buf["photos"], buf["lieu"], buf["date"], buf["sender_name"], buf["sender_id"])
        else:
            await update.message.reply_text("صيغة التاريخ غير صحيحة. يرجى إرسال التاريخ بالصيغة: سنة/شهر/يوم (مثال: 15/10/2025).")
    else:
        await update.message.reply_text("Aucune modification en cours.")

def run_telegram_bot():
    app_telegram = Application.builder().token(TELEGRAM_TOKEN).build()
    app_telegram.add_handler(CommandHandler('start', start))
    app_telegram.add_handler(CallbackQueryHandler(validation_callback))
    app_telegram.add_handler(MessageHandler(filters.TEXT & filters.REPLY, edit_handler))
    app_telegram.run_polling()

threading.Thread(target=run_telegram_bot, daemon=True).start()

# ----------------------------- TRADUCTIONS EN ARABE pour Messenger -------------------------------
AR_MSGS = {
    "welcome": "مرحبًا، يرجى اتباع الخطوات لإرسال المنشور.",
    "ask_lieu": "من فضلك أرسل اسم المكان باللغة العربية (مثال: محور دوران دار البيضاء).",
    "lieu_ok": "شكرًا، تم استلام اسم المكان!",
    "ask_date": "من فضلك أرسل التاريخ بالصيغة: سنة/شهر/يوم (مثال: 15/10/2025).",
    "date_ok": "شكرًا، تم استلام التاريخ!",
    "date_invalid": "صيغة التاريخ غير صحيحة. يرجى إرسال التاريخ بالصيغة: سنة/شهر/يوم (مثال: 15/10/2025).",
    "ask_photo": "أرسل الصور أو اكتب 'fin' عند الانتهاء.",
    "photo_ok": "تم استلام الصور(ة).",
    "finish_ok": "تم إرسال المنشور، وسيتم نشره قريبًا.",
}

@app.post("/webhook")
def receive():
    data = request.get_json() or {}
    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event["sender"]["id"]
            message = event.get("message", {})
            mid = message.get("mid")

            if sender_id not in user_buffers:
                user_buffers[sender_id] = {
                    "step": 0, "lieu": None, "date": None, "photos": [],
                    "finished": False,
                    "error_sent_1": False, "error_sent_2": False, "error_sent_3": False,
                    "consigne_sent_1": False, "consigne_sent_2": False,
                    "processed_mids": set()
                }
            buffer = user_buffers[sender_id]

            if mid:
                if mid in buffer["processed_mids"]:
                    return {"ok": True}
                buffer["processed_mids"].add(mid)
                if len(buffer["processed_mids"]) > 30:
                    buffer["processed_mids"] = set(list(buffer["processed_mids"])[-15:])

            if buffer["step"] == 0:
                print("DEBUG step 0:", message)
                if "text" in message and message.get("text", "").strip().lower().startswith("samir"):
                    print("DEBUG samir detected, sending welcome!")
                    buffer["step"] = 1
                    send_message_to_messenger(
                        sender_id,
                        AR_MSGS["welcome"]
                    )
                return {"ok": True}

            if buffer["step"] == 1:
                if buffer["lieu"] is not None:
                    return {"ok": True}
                if "text" in message:
                    buffer["lieu"] = message["text"].strip()
                    buffer["step"] = 2
                    buffer["error_sent_2"] = False
                    buffer["consigne_sent_2"] = False
                    send_message_to_messenger(
                        sender_id,
                        AR_MSGS["lieu_ok"]
                    )
                elif not buffer.get("consigne_sent_1", False):
                    buffer["consigne_sent_1"] = True
                    send_message_to_messenger(
                        sender_id,
                        AR_MSGS["ask_lieu"]
                    )
                return {"ok": True}

            if buffer["step"] == 2:
                if buffer["date"] is not None:
                    return {"ok": True}
                if "text" in message:
                    date_str = message["text"].strip()
                    if is_date_valid(date_str):
                        buffer["date"] = date_str
                        buffer["step"] = 3
                        buffer["error_sent_3"] = False
                        buffer["error_sent_2"] = False
                        buffer["consigne_sent_2"] = False
                        send_message_to_messenger(
                            sender_id,
                            AR_MSGS["date_ok"]
                        )
                    else:
                        if not buffer.get("error_sent_2", False):
                            buffer["error_sent_2"] = True
                            send_message_to_messenger(
                                sender_id,
                                AR_MSGS["date_invalid"]
                            )
                elif not buffer.get("consigne_sent_2", False):
                    buffer["consigne_sent_2"] = True
                    send_message_to_messenger(
                        sender_id,
                        AR_MSGS["ask_date"]
                    )
                return {"ok": True}

            if buffer["step"] == 3 and not buffer.get("finished", False):
                attachments = message.get("attachments", [])
                images = [a["payload"]["url"] for a in attachments if a.get("type") == "image"]
                if images:
                    buffer["photos"].extend(images)
                    buffer["error_sent_3"] = False
                    send_message_to_messenger(sender_id, AR_MSGS["photo_ok"])
                elif "text" in message and message.get("text", "").strip().lower() == "fin":
                    buffer["finished"] = True
                    sender_name = get_user_name(sender_id)
                    send_message_to_messenger(sender_id, AR_MSGS["finish_ok"])
                    send_to_telegram_for_validation(
                        photo_urls=buffer["photos"],
                        lieu=buffer["lieu"],
                        date=buffer["date"],
                        sender_name=sender_name,
                        sender_id=sender_id
                    )
                    user_buffers[sender_id] = {
                        "step": 0, "lieu": None, "date": None, "photos": [],
                        "finished": False, "error_sent_1": False, "error_sent_2": False, "error_sent_3": False,
                        "consigne_sent_1": False, "consigne_sent_2": False,
                        "processed_mids": set()
                    }
                elif not images:
                    if not buffer.get("error_sent_3", False):
                        buffer["error_sent_3"] = True
                        send_message_to_messenger(
                            sender_id,
                            AR_MSGS["ask_photo"]
                        )
                return {"ok": True}

    return {"ok": True}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)