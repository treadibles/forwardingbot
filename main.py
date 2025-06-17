Main
109
110
111
112
113
114
115
116
117
118
119
120
121
122
123
124
125
126
127
128
129
130
131
132
133
134
135
136
137
138
139
140
141
142
143
144
145
146
147
148
149
150
151
152
153
154
155
156
157
158
159
160
161
162
163
164
165
166
167
168
169
170
171
172
173
174
175
176
177
178
179
180
181
182
183
184
185
186
187
188
189
190
191
192
193
194
195
196
197
198
199
200
201
202
203
204
205
206
        return await update.message.reply_text("Usage: /increasepound <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Provide a valid number.")
    inc_pound[chat] = amt
    _config["inc_pound"] = inc_pound
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Pound increment for {chat} set to +{amt}")

async def increasecart(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) != 2:
        return await update.message.reply_text("Usage: /increasecart <chat> <amount>")
    chat, val = ctx.args
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered.")
    try:
        amt = float(val)
    except ValueError:
        return await update.message.reply_text("Provide a valid number.")
    inc_cart[chat] = amt
    _config["inc_cart"] = inc_cart
    with open(CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)
    await update.message.reply_text(f"✅ Cart increment for {chat} set to +{amt}")

async def forward_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Forward all historical messages from the source into the specified target channel,
    applying per-channel pound/cart increments.
    """
    # Validate arguments
    if len(ctx.args) != 1:
        return await update.message.reply_text("Usage: /forward <chat_id_or_username>")
    chat = ctx.args[0]
    if chat not in target_chats:
        return await update.message.reply_text("Channel not registered. Use /register first.")

    # Notify user
    notify = await update.message.reply_text("🔄 Forwarding history… this may take a while")
    count = 0

    # Ensure Telethon client is connected, reuse session
    if not tele_client.is_connected():
        try:
            await tele_client.connect()
        except Exception:
            try:
                await tele_client.start(bot_token=BOT_TOKEN)
            except FloodWaitError as e:
                return await notify.edit_text(f"❌ Telethon FloodWait: wait {e.seconds}s and try again.")
            except Exception as e:
                return await notify.edit_text(f"❌ Cannot initialize history session: {e}")

    # Fetch source channel entity
    try:
        src_entity = await tele_client.get_entity(int(SOURCE_CHAT))
    except Exception as e:
        return await notify.edit_text(f"❌ Failed to access source channel: {e}")

    # Iterate and forward messages
    async for orig in tele_client.iter_messages(src_entity, reverse=True):
        try:
            if orig.photo or orig.video or orig.document:
                sent = await ctx.bot.copy_message(
                    chat_id=chat,
                    from_chat_id=SOURCE_CHAT,
                    message_id=orig.id
                )
                if orig.caption:
                    new_cap = adjust_caption(orig.caption, chat)
                    if new_cap != orig.caption:
                        await ctx.bot.edit_message_caption(
                            chat_id=sent.chat_id,
                            message_id=sent.message_id,
                            caption=new_cap
                        )
            elif orig.text:
                new_txt = adjust_caption(orig.text, chat)
                await ctx.bot.send_message(chat_id=chat, text=new_txt)
            count += 1
        except Exception:
            continue

    await notify.edit_text(f"✅ History forwarded: {count} messages to {chat}.")"❌ Telethon FloodWait: wait {e.seconds}s and try again.")
        except Exception as e:
            return await notify.edit_text(f"❌ Error initializing history client: {e}")

    # Fetch source channel entity once
    try:
        src_entity = await tele_client.get_entity(int(SOURCE_CHAT))
    except Exception as e:
        return await notify.edit_text(f"❌ Failed to access source channel: {e}")


