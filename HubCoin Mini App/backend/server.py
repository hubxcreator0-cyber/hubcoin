import os
import json
import logging
from datetime import date
from threading import Thread
from dotenv import load_dotenv

from flask import Flask, jsonify, request
from flask_cors import CORS, send_from_directory
import firebase_admin
from firebase_admin import credentials, firestore

import telegram
from telegram import Update, WebAppInfo, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. প্রাথমিক সেটআপ এবং কনফিগারেশন ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
load_dotenv()

# .env ফাইল থেকে ভেরিয়েবল লোড করা
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
FRONTEND_URL = os.getenv("FRONTEND_URL")
firebase_config_str = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON_STRING")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", 0)) # আপনার টেলিগ্রাম আইডি

# Firebase সেটআপ
try:
    firebase_config = json.loads(firebase_config_str)
    cred = credentials.Certificate(firebase_config)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logging.info("Firebase successfully initialized!")
except Exception as e:
    logging.error(f"Firebase initialization failed: {e}")
    db = None

# Flask অ্যাপ সেটআপ
# ফ্রন্টএন্ড ফাইলগুলো সার্ভ করার জন্য static_folder কনফিগার করা হয়েছে।
# এটি backend ফোল্ডারের এক ধাপ বাইরে থাকা ফাইলগুলোকে নির্দেশ করে।
app = Flask(__name__, static_folder='../')
# ⚠️ প্রোডাকশনে '*' এর বদলে আপনার FRONTEND_URL ব্যবহার করুন
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

# --- স্ট্যাটিক ফাইল সার্ভ করার জন্য রুট ---
@app.route('/')
def serve_index():
    """index.html ফাইলটি সার্ভ করে।"""
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static_files(path):
    """অন্যান্য স্ট্যাটিক ফাইল (css, js, images) সার্ভ করে।"""
    return send_from_directory(app.static_folder, path)

# --- 2. Helper Functions (সহকারী ফাংশন) ---
def get_user_ref(user_id):
    return db.collection('users').document(str(user_id))

def create_new_user(user_id, username, referrer_id=None):
    user_data = {
        'username': username,
        'balance': 0.0,
        'gems': 0,
        'unclaimedGems': 0,
        'refs': 0,
        'adWatch': 0,
        'todayIncome': 0.0,
        'gemsClaimedToday': 0,
        'lastGemClaimDate': str(date.today()),
        'totalWithdrawn': 0.0,
        'referredBy': referrer_id
    }
    get_user_ref(user_id).set(user_data)
    logging.info(f"New user created: {user_id}, Referred by: {referrer_id}")
    return user_data

# --- 3. API Endpoints (ফ্রন্টএন্ডের জন্য) ---
@app.route("/api/user", methods=['POST'])
def get_or_create_user():
    data = request.json
    user_id = data.get('user_id')
    username = data.get('username', 'N/A')
    # TODO: এখানে tg.initData ভ্যালিডেশন যোগ করতে হবে

    if not user_id: return jsonify({"error": "User ID missing"}), 400
    
    try:
        user_doc = get_user_ref(user_id).get()
        if user_doc.exists:
            return jsonify(user_doc.to_dict()), 200
        else:
            # যদি রেফারেল সহ আসে, start কমান্ড হ্যান্ডলার এটি তৈরি করবে
            # যদি সরাসরি আসে, এখানে তৈরি হবে
            new_user = create_new_user(user_id, username)
            return jsonify(new_user), 201
    except Exception as e:
        logging.error(f"API Error on /api/user: {e}")
        return jsonify({"error": "Server error"}), 500

@app.route("/api/claim-gems", methods=['POST'])
def claim_gems():
    user_id = request.json.get('user_id')
    if not user_id: return jsonify({"error": "User ID missing"}), 400

    try:
        user_ref = get_user_ref(user_id)
        @firestore.transactional
        def update_gems_transaction(transaction, doc_ref):
            today_str = str(date.today())
            snapshot = doc_ref.get(transaction=transaction)
            
            unclaimed = snapshot.get('unclaimedGems')
            if unclaimed < 2:
                return {"success": False, "message": "You need at least 2 gems."}

            last_claim_date = snapshot.get('lastGemClaimDate')
            claimed_today = snapshot.get('gemsClaimedToday')

            if last_claim_date != today_str:
                claimed_today = 0
                transaction.update(doc_ref, {'lastGemClaimDate': today_str})

            if claimed_today >= 6:
                return {"success": False, "message": "Daily gem claiming limit reached (6/day)."}

            transaction.update(doc_ref, {
                'gems': firestore.Increment(2),
                'unclaimedGems': firestore.Increment(-2),
                'gemsClaimedToday': firestore.Increment(2)
            })
            return {"success": True, "message": "2 Gems claimed!", 
                    "data": {"gems": snapshot.get('gems') + 2, "unclaimedGems": snapshot.get('unclaimedGems') - 2}}

        result = update_gems_transaction(db.transaction(), user_ref)
        return jsonify(result), 200
    except Exception as e:
        logging.error(f"API Error on /api/claim-gems: {e}")
        return jsonify({"error": "Could not claim gems"}), 500

@app.route('/api/withdrawal', methods=['POST'])
def request_withdrawal():
    data = request.json
    user_id = data.get('user_id')
    amount = float(data.get('amount'))
    method = data.get('method')
    
    if not all([user_id, amount, method, data.get('account')]):
        return jsonify({'error': 'Missing fields'}), 400

    # জেমের প্রয়োজনীয়তা হিসাব করা
    required_gems = 0
    if method in ['Bkash', 'Nagad']:
        if amount == 500: required_gems = 29
        elif amount == 1000: required_gems = 49
        elif amount == 1500: required_gems = 79
        else: required_gems = (amount / 500) * 50
    elif method == 'Binance':
        if amount == 5: required_gems = 58
        elif amount == 10: required_gems = 100
        elif amount == 15: required_gems = 150
        else: required_gems = amount * 10
    
    try:
        user_ref = get_user_ref(user_id)
        @firestore.transactional
        def withdrawal_transaction(transaction, doc_ref):
            snapshot = doc_ref.get(transaction=transaction)
            balance = snapshot.get('balance')
            gems = snapshot.get('gems')

            if balance < amount:
                return {"success": False, "error": "Insufficient balance."}
            if gems < required_gems:
                return {"success": False, "error": f"Insufficient gems. You need {int(required_gems)} gems."}

            # ব্যালেন্স এবং জেম কেটে নিন
            transaction.update(doc_ref, {
                'balance': firestore.Increment(-amount),
                'gems': firestore.Increment(-int(required_gems))
            })
            # নতুন উইথড্রয়াল অনুরোধ তৈরি করুন
            db.collection('withdrawals').add({
                'userId': user_id, 'amount': amount, 'method': method,
                'account': data.get('account'), 'status': 'pending',
                'timestamp': firestore.SERVER_TIMESTAMP
            })
            return {"success": True, "message": "Withdrawal request submitted!",
                    "data": {"balance": balance - amount, "gems": gems - int(required_gems)}}

        result = withdrawal_transaction(db.transaction(), user_ref)
        return jsonify(result), 200
    except Exception as e:
        logging.error(f"API Error on /api/withdrawal: {e}")
        return jsonify({'error': 'Server error during withdrawal'}), 500

@app.route("/api/leaderboard", methods=['GET'])
def get_leaderboard():
    try:
        doc = db.collection('leaderboard').document('top_players').get()
        return jsonify(doc.to_dict() if doc.exists else {"players": []}), 200
    except Exception as e:
        logging.error(f"API Error on /api/leaderboard: {e}")
        return jsonify({"error": "Could not fetch leaderboard"}), 500

# --- 4. Telegram Bot Command Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    user_id, username = str(user.id), user.username or user.first_name
    
    referrer_id = context.args[0] if context.args and context.args[0].isdigit() and context.args[0] != user_id else None

    if not get_user_ref(user_id).get().exists:
        create_new_user(user_id, username, referrer_id)
        if referrer_id:
            try:
                referrer_ref = get_user_ref(referrer_id)
                referrer_ref.update({
                    'balance': firestore.Increment(25.0),
                    'unclaimedGems': firestore.Increment(2),
                    'refs': firestore.Increment(1)
                })
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=f"🎉 Congratulations! {user.first_name} joined using your link. You received 25 TK and 2 Gems!"
                )
            except Exception as e:
                logging.error(f"Failed to reward referrer {referrer_id}: {e}")

    keyboard = [[InlineKeyboardButton("🚀 Open HubCoin Miner", web_app=WebAppInfo(url=FRONTEND_URL))]]
    await update.message.reply_html(
        rf"👋 Welcome, {user.mention_html()}! Click the button below to start earning.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def update_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("⛔ You are not authorized.")
        return

    await update.message.reply_text("⏳ Updating leaderboard...")
    try:
        query = db.collection('users').order_by('totalWithdrawn', direction=firestore.Query.DESCENDING).limit(20)
        top_users = [{'rank': i+1, 'username': doc.to_dict().get('username', 'N/A'),
                      'totalWithdrawn': doc.to_dict().get('totalWithdrawn', 0)}
                     for i, doc in enumerate(query.stream())]
        
        db.collection('leaderboard').document('top_players').set({
            'players': top_users,
            'lastUpdated': firestore.SERVER_TIMESTAMP
        })
        await update.message.reply_text(f"✅ Leaderboard updated with {len(top_users)} players!")
    except Exception as e:
        logging.error(f"Leaderboard update failed: {e}")
        await update.message.reply_text(f"❌ Failed to update leaderboard. Error: {e}")

# --- 5. অ্যাপ চালনা ---
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)

def main() -> None:
    # Render Gunicorn ব্যবহার করলে সরাসরি Flask অ্যাপটি চালাবে
    # লোকাল টেস্টিং এর জন্য একসাথে বট এবং Flask চালানো হচ্ছে
    
    # আলাদা থ্রেডে Flask অ্যাপ চালানো
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # টেলিগ্রাম বট চালু করা
    tg_app = Application.builder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("updateleaderboard", update_leaderboard_command))
    
    logging.info("Starting Telegram bot polling...")
    tg_app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()