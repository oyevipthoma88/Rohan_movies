# 🎬 Rohan Movies Bot

[![Deploy on Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/oyevipthoma88/Rohan_movies)

Telegram movies bot — force-join verify, fuzzy movie search from your channel, free/premium limits, UPI payments with owner approval, referral rewards aur full owner panel.

## Features

**Users**
- Start → maximum **5 must-join channels/URLs** + **Verify** button (verify hone par hi panel milega)
- 🎬 **Get Movie** — naam type karo (galat spelling chalega) ya 🎤 voice note bhejo; DM aur group dono me plain movie name se direct search hoti hai
- Saare matching results quality/size ke saath buttons me; tap karte hi file channel se aa jati hai
- 📘 Tutorial, 🕘 Last History, 🎥 Watch Online, 💎 Premium, 🎟 Redeem Coupon, 🎁 Refer & Earn
- Free plan: pehle din **3 search**, uske baad **roz 1**
- Movie na mile to owner request log hoti hai aur catalog me available movies ki suggestions milti hain; users ko sirf MAIN_DATA me indexed content deliver hota hai

**Premium**
- Plans: 1 Day ₹25 · 7 Days ₹149 · 1 Month ₹449 · 1 Year ₹2999 · Lifetime ₹4999 (owner panel se editable)
- Plan select → QR/UPI → screenshot → owner + log channel me details → **Approve/Reject** button
- Coupon codes bhi support hain

**Referral**
- `https://t.me/<bot>?start=ref_<userid>`
- 3 referral → 1 hour · 5 → 5 hours · 10 → 1 day premium

**Owner Panel** (`/ownerpanel` ya Start ka button; sirf owner ko)
- 🖼 Start pic set (GitHub raw link ya photo)
- 💳 QR set + UPI id
- 💰 Plan price edit · 🎁 Gift premium (userid + days)
- 🎟 Coupon code banao · 💎 Premium users list
- 📡 Comma-separated 100+ source movie channels configure karke `/scanall` ya custom range se MAIN_DATA me newest-to-oldest scan; new source upload par owner DMs aur LOG_CHANNEL me **Add to MAIN_DATA / Decline** approval request aati hai
- 🗑 Owner Panel → **Auto Delete** se delivered user messages ko 1 minute–24 hours me automatically delete kar sakte ho; fresh installs aur legacy `0` default installs me default **7 minutes** hai, aur `0` owner manually set karke disable kar sakta hai. Delivery warning aur deletion ke baad available movie suggestions bhi bheji jaati hain
- 📢 Broadcast · 👮 Manage admin · 🔗 Verify URLs add/remove (maximum 5) · 📘 Tutorial set · 🎥 Watch Online bot set · 📊 Stats
- Admin users ko owner panel nahi milta; unka sole action `/addvideo` ya **Add Video** button se forwarded video catalog me add karna hai

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` aur values bharo (`BOT_TOKEN`, `OWNER_IDS`, `MAIN_DATA`, `MOVIE_CHANNELS`, `MONGO_URI`); optional `LOG_CHANNEL` set karo
3. Bot ko **MAIN_DATA channel**, **log channel** aur **verify channels** me required permissions do; source chat IDs owner panel se configure honge
4. `python src/main.py`
5. Owner Panel → **Source Channels** me source chat ID(s) comma-separated set karo. New video upload hone par owner DM aur `LOG_CHANNEL` me approval request aayegi. **Add to MAIN_DATA** dabane par source post canonical `MAIN_DATA` me copy/index hogi; **Decline** se skip hogi. Historical content ke liye `/scanall` ya `/scan start_id end_id` chalao. Owner Panel → **Watch Online Bot** se player username set karo; `ONLINE_PLAYER_BOT` fallback ke roop me supported hai. Owner Panel → **Verify URLs** me `add @channel`, `del @channel`, ya `set @one,@two` use karke maximum 5 public channels manage karo. Owner Panel → **Set Tutorial** se clean Hinglish tutorial edit karo.

## Notes
- Telegram flood limits ko respect karte hue scan me bounded parallelism, retry/backoff, aur per-batch durable database commits use hote hain; literally unlimited speed/range Telegram API allow nahi karta.
- `MAIN_DATA` ek canonical storage/catalog channel hai. Bot ko wahan post, copy, pin aur delete permission do. Movie index ab MongoDB me persist hota hai, isliye restart par existing catalog restore hota hai aur historical scan nahi hota. `LOG_CHANNEL` configured ho to latest state snapshot mirror hokar pin hota hai; har movie ki duplicate log copy nahi banti.
- Voice search ke liye `OPENAI_API_KEY` (whisper) set karo; na ho to bot text maangega.
- **Watch Online** button sirf configured player bot ka usage flow dikhata hai: player bot ko group me admin do, voice chat start karo, movie forward/tag karke `vplay` likho. Actual VC playback player bot ki apni implementation par depend karta hai.
- User delivery caption strict compact format me hoti hai: Movie, Quality, Duration, Join, Visit, aur Enjoy lines only. Video/audio metadata se duration detect hoti hai; unknown hone par `Unknown` dikhaya jata hai.
- Files hamesha `copy_message` se bhejti hain, isliye Telegram ki full speed milti hai (koi re-upload nahi). Missing movies ke liye koi external movie-download fallback nahi hai; bot sirf MAIN_DATA catalog se delivery karta hai.

## Deploy
Koi bhi VPS / Railway / Render worker:
```
python src/main.py
```

## Heroku Deploy

1. Upar wale **Deploy on Heroku** button pe click karo
2. App name do, phir `BOT_TOKEN`, `OWNER_IDS`, `MAIN_DATA`, `MOVIE_CHANNELS`, `MONGO_URI` bharo; optional `LOG_CHANNEL` set karo (baki optional)
3. Deploy hone ke baad **Resources** tab me jaake `worker` dyno ON karo; MAIN_DATA aur LOG_CHANNEL dono me bot ko post/copy/pin/delete permissions do
4. Note: Heroku ka filesystem temporary hai — dyno restart par local `bot.db` reset ho sakta hai. Bot MAIN_DATA me compressed pinned state snapshot rakhta hai; isliye MAIN_DATA permissions aur pinned snapshot ko delete na karna zaroori hai.
