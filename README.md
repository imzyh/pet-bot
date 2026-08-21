# Pet Reminder Telegram Bot

بوت تذكير للحيوانات الأليفة باستخدام:
- Python
- python-telegram-bot 22.x
- PostgreSQL
- aiohttp health endpoint
- Render Web Service
- UptimeRobot

## Environment Variables

BOT_TOKEN=توكن بوت تيليجرام
DATABASE_URL=رابط PostgreSQL
TIMEZONE=Asia/Riyadh

## تشغيل محلي

python -m venv venv

Windows:
venv\Scripts\activate

pip install -r requirements.txt

ثم ضع المتغيرات البيئية وشغّل:

python bot.py

## Render

استخدم Web Service وليس Static Site.

Build Command:
pip install -r requirements.txt

Start Command:
python bot.py

Environment Variables:
BOT_TOKEN
DATABASE_URL
TIMEZONE=Asia/Riyadh

Render يمرر PORT تلقائياً، والكود يستمع على 0.0.0.0.

## UptimeRobot

بعد نشر Render سيكون لديك رابط مثل:
https://YOUR-SERVICE.onrender.com

أنشئ HTTP(s) monitor على:
https://YOUR-SERVICE.onrender.com/health

يمكن استخدامه لفحص الخدمة وإرسال طلبات دورية. هذا ليس ضماناً رسمياً بأن Render لن يعيد تشغيل الخدمة.

## PostgreSQL

ضع رابط PostgreSQL الخارجي في DATABASE_URL.
لا تضع كلمة المرور أو الرابط داخل الكود.

## أوامر البوت

/start
/add_pet
/add_reminder
/pets
/reminders
/delete_reminder ID
/cancel

التنبيه يعاد كل 10 دقائق حتى الضغط على:
✅ تأكيد (تم الإنجاز)

حالة التنبيه محفوظة في PostgreSQL، لذلك يمكن استعادتها بعد Restart.
