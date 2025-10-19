    if txt == "Galleries":
        with SessionLocal() as db:
            u = db.execute(select(VIPUser).where(VIPUser.chat_id == chat_id)).scalar_one_or_none()
            if not u or not is_active(u):
                tg_send(chat_id, "🔒 Your muse sleeps until you renew VIP 💋")
            else:
                today = now_mx().date()
                if u.last_sent_at and u.last_sent_at.date() == today:
                    tg_send(chat_id, "✨ You already received today’s muse. Come back tomorrow 🌙")
                else:
                    link = pick_new_gallery(db, chat_id)
                    if not link:
                        tg_send(chat_id, "⚠️ No new galleries yet. Please wait 🔮")
                    else:
                        tg_send(chat_id, f"🎁 <b>Your muse today</b>:\n{esc(link)} 💋", preview=False)
                        record_delivery(db, chat_id, link)
                        u.last_sent_at = now_mx().replace(tzinfo=None)
                        db.commit()
        return jsonify(ok=True)
