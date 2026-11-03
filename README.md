# Force Subscribe + One-Time Target Invite Bot

Telegram bot built with Python, python-telegram-bot, MongoDB and a Render **Web Service** webhook.

## Features
- Unlimited Force Subscribe channels and Target channels (subject to Telegram/API and database limits).
- Inline Join buttons; raw channel links are not printed in messages.
- All Force Subscribe channels are checked before target invites are issued.
- A separate one-use invite link (`member_limit=1`) is created per user and target channel.
- On a matching `chat_member` join update, the bot deletes its invite message, revokes the invite link, and marks it used.
- Owner-only admin management, ban/unban, broadcast and statistics.
- Admins can manage channels; only the Owner can manage admins and ban/unban users.
- MongoDB persistence.

## Important Telegram setup
1. Create a bot with @BotFather and copy its token.
2. Add the bot as an **administrator** in every Force Subscribe and Target channel.
3. In Target channels, grant **Invite Users** permission. The bot needs to receive `chat_member` updates.
4. For private Force Subscribe channels, use a valid channel invite URL when adding the channel. For public channels, use `https://t.me/channelusername`.
5. Use the numeric channel ID, usually beginning with `-100`.
6. The bot must be able to check membership in each Force Subscribe channel. If Telegram returns an error, check that the bot is an admin there.

## Add channels from inside the bot
Open `/admin` (Owner sees the full panel automatically).

- **Add Force Sub**: send `chat_id | title | join_url`
  - Example: `-1001234567890 | News Channel | https://t.me/newschannel`
  - Private channel example: use the channel's invite URL as `join_url`.
- **Add Target Channel**: send `chat_id | title`
  - Example: `-1001234567890 | Premium Channel`
  - The bot generates individual one-use invite links; do not paste a target invite link.
- Remove channels by sending their numeric chat ID.

## Admin / user controls
- Add Admin / Remove Admin: send the person's numeric Telegram user ID.
- Ban User / Unban User: send the numeric Telegram user ID.
- Broadcast: send the message after choosing Broadcast.
- `/cancel` cancels an input prompt.
- Only `OWNER_ID` can add/remove admins and ban/unban users.

## Deploy to Render as a Web Service
1. Push this folder to a GitHub repository.
2. In Render choose **New + → Web Service** and connect the repository.
3. Runtime: Python. Build command: `pip install -r requirements.txt`.
4. Start command: `python main.py`.
5. Add Environment Variables:
   - `BOT_TOKEN`: BotFather token
   - `OWNER_ID`: your numeric Telegram user ID
   - `MONGODB_URI`: MongoDB connection string
   - `DB_NAME`: `force_sub_bot` (optional)
   - `WEBHOOK_URL`: full public Render URL, e.g. `https://your-service.onrender.com` (no trailing slash)
   - `WEBHOOK_SECRET`: a long random string; Render blueprint can generate this automatically
6. Deploy. Check Render logs for `Webhook configured`.
7. Open your bot and send `/start`.

`PORT` is supplied by Render automatically; do not hardcode it in Render settings.

## Notes / limitations
- Telegram does not provide an absolute guarantee that a user will never share a link before it is used. `member_limit=1` limits successful joins to one account.
- The bot relies on Telegram delivering the target channel's `chat_member` update, including the invite link used. If this update is unavailable, the one-use link still reaches its one-member limit, but the bot may not be able to delete the bot message immediately.
- Telegram bots cannot delete arbitrary old messages in private user chats; they can delete their own messages within Telegram's applicable limits.
- For a production service, use a paid Render instance or another always-on host if available. Free instances may sleep and delay webhook processing.
- Never commit `.env`, bot tokens, MongoDB credentials, or private invite links to GitHub.
- This is a starter implementation. Test with a private test channel and test account before using it with real users.
