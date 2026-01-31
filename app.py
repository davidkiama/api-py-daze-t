
import requests
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask import Flask, request, jsonify, redirect
from datetime import datetime
from logging.handlers import RotatingFileHandler
import time
import logging
import json
import hashlib
import hmac
import os


# 1. SETUP LOGGING (Crucial for debugging on cPanel)
# This creates a file named 'app.log' in your folder. Check this file if you get Error 500.
logging.basicConfig(level=logging.INFO)
handler = RotatingFileHandler('app.log', maxBytes=100000, backupCount=3)
handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
))
logger = logging.getLogger(__name__)

# Load Env
load_dotenv()

app = Flask(__name__)

# Add file handler to app logger
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

# 2. CONFIGURATION
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'default_secret')

# 3. CORS SETUP
# Strictly allow your specific frontend domains
CORS(app, resources={r"/api/*": {"origins": [
    "https://daze-t.com",
    "https://www.daze-t.com",
    "http://localhost:3000"  # For local testing
]}})

# 4. DATABASE MODEL (Replaces Mongoose Schema)
db = SQLAlchemy(app)


class Invoice(db.Model):
    __tablename__ = 'invoices'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.String(100), unique=True, nullable=False)
    invoice_id = db.Column(db.String(100), nullable=False)  # OxaPay Track ID
    phone = db.Column(db.String(50), nullable=False)
    amount_usd = db.Column(db.Float, nullable=False)
    amount_kes = db.Column(db.Float, nullable=False)
    pay_url = db.Column(db.String(255))
    status = db.Column(db.String(50), default="pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

    # Store raw response as JSON text since MySQL doesn't natively do BSON like Mongo
    # (unless using JSON type, but Text is safer for older MariaDB versions)
    raw_response = db.Column(db.Text, nullable=True)


# Create tables if they don't exist (Run this once)
with app.app_context():
    db.create_all()

# CONSTANTS
PAID_STATUSES = ["paid", "manual_accept", "confirmed", "completed", "success"]
OXAPAY_KEY = os.getenv('OXAPAY_API_KEY')
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', "https://api.daze-t.com")
FRONTEND_URL = os.getenv('FRONTEND_URL', "https://daze-t.com")

# --- TELEGRAM HELPER ---


def send_telegram_alert(message):
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    # Split the comma-separated string into a list
    chat_ids_raw = os.getenv('TELEGRAM_CHAT_IDS', '')

    if not token or not chat_ids_raw:
        app.logger.warning("Telegram credentials missing.")
        return

    chat_ids = [cid.strip() for cid in chat_ids_raw.split(',') if cid.strip()]

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for chat_id in chat_ids:
        try:
            payload = {"chat_id": chat_id,
                       "text": message, "parse_mode": "HTML"}
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            app.logger.error(f"Telegram Alert Failed for {chat_id}: {str(e)}")

# =========================================
# 0. HEALTH CHECK
# =========================================


@app.route("/", methods=["GET"])
def health_check():
    return "✅ Python API is running and connected to MySQL!", 200

# =========================================
# 1. CREATE INVOICE
# =========================================


@app.route("/api/create-trade", methods=["POST"])
def create_trade():
    try:
        data = request.get_json()
        usd = float(data.get("usd", 0))
        kes = float(data.get("kes", 0))
        phone = data.get("phone")

        # Input Validation
        if kes > 100000:  # Adjusted limit, verify your needs
            return jsonify({"error": "Max limit exceeded"}), 400
        if usd < 0.1:
            return jsonify({"error": "Min amount 0.1 USD"}), 400
        if not phone:
            return jsonify({"error": "Phone number required"}), 400

        my_order_id = f"INV-{int(time.time() * 1000)}"

        # OxaPay Request
        url = "https://api.oxapay.com/v1/payment/invoice"
        headers = {"merchant_api_key": OXAPAY_KEY,
                   "Content-Type": "application/json"}

        payload = {
            "amount": usd,
            "currency": "USD",
            "lifetime": 30,
            "fee_paid_by_payer": 1,
            "under_paid_coverage": 2,
            "to_currency": "USDT",
            "auto_withdrawal": False,
            "mixed_payment": True,
            "callback_url": f"{PUBLIC_BASE_URL}/oxapay/webhook",
            "return_url": f"{PUBLIC_BASE_URL}/api/success-redirect?orderId={my_order_id}",
            "order_id": my_order_id,
            "thanks_message": "Thank you. Awaiting Blockchain confirmation...",
            "description": f"Order for {phone}",
            "sandbox": False,
        }

        response = requests.post(
            url, json=payload, headers=headers, timeout=10)
        result = response.json()

        if result.get("status") == 200:
            res_data = result.get("data", {})

            # Save to MySQL
            new_invoice = Invoice(
                order_id=my_order_id,
                invoice_id=res_data.get("track_id"),
                phone=phone,
                amount_usd=usd,
                amount_kes=kes,
                pay_url=res_data.get("payment_url"),
                raw_response=json.dumps(result)  # Save raw JSON as string
            )

            db.session.add(new_invoice)
            db.session.commit()

            return jsonify({
                "payment_url": res_data.get("payment_url"),
                "track_id": res_data.get("track_id")
            })
        else:
            app.logger.error(f"OxaPay Error: {result.get('message')}")
            return jsonify({"error": result.get("message", "OxaPay failed")}), 400

    except Exception as e:
        app.logger.exception("Create Trade Error")
        return jsonify({"error": "Internal Server Error"}), 500

# =========================================
# 2. SUCCESS REDIRECT
# =========================================


@app.route("/api/success-redirect", methods=["GET"])
def success_redirect():
    order_id = request.args.get("orderId")

    if not order_id:
        return redirect(FRONTEND_URL)

    try:
        # Find invoice in MySQL
        invoice = Invoice.query.filter_by(order_id=order_id).first()

        if not invoice:
            app.logger.error(f"Invoice not found for redirect: {order_id}")
            return redirect(FRONTEND_URL)

        # Redirect to Frontend Success Page
        react_url = (
            f"{FRONTEND_URL}/success?"
            f"trackId={invoice.invoice_id}&"
            f"amount={invoice.amount_kes}&"
            f"phone={invoice.phone}"
        )
        return redirect(react_url)

    except Exception as e:
        app.logger.exception("Redirect Error")
        return redirect(FRONTEND_URL)

# =========================================
# 3. WEBHOOK (With Security Checks)
# =========================================


@app.route("/oxapay/webhook", methods=["POST"])
def oxapay_webhook():
    hmac_header = request.headers.get("hmac")
    raw_body = request.get_data()

    if not raw_body:
        return "Raw body missing", 400

    # 1. HMAC Verification (Security against fake requests)
    try:
        calculated_hmac = hmac.new(
            key=OXAPAY_KEY.encode('utf-8'),
            msg=raw_body,
            digestmod=hashlib.sha512
        ).hexdigest()

        # Secure comparison to prevent timing attacks
        if not hmac.compare_digest(calculated_hmac, hmac_header or ""):
            app.logger.warning("Unauthorized Webhook Attempt (Bad HMAC)")
            return "Unauthorized", 401

    except Exception as e:
        app.logger.error(f"HMAC Error: {e}")
        return "Error", 500

    # 2. Process Data
    try:
        data = request.get_json()
        track_id = data.get("track_id")
        status = data.get("status", "").lower()

        invoice = Invoice.query.filter_by(invoice_id=track_id).first()

        if not invoice:
            return "Invoice not found", 404

        # Idempotency: If already marked paid, ignore to prevent duplicate alerts
        if invoice.status in PAID_STATUSES:
            return "ok"

        # Update Status
        invoice.status = status
        db.session.commit()

        app.logger.info(f"📩 Webhook: [{track_id}] -> {status}")

        if status in PAID_STATUSES:
            msg = (
                f"💰 <b>PAYMENT CONFIRMED!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>Amount:</b> {invoice.amount_usd} USD\n"
                f"<b>Total Paid:</b> {invoice.amount_kes} KES\n"
                f"<b>Customer Phone:</b> {invoice.phone}\n"
                f"<b>Invoice ID:</b> {track_id}\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_alert(msg)

        return "ok"

    except Exception as e:
        app.logger.exception("Webhook Processing Error")
        db.session.rollback()
        return "error", 500


if __name__ == "__main__":
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
