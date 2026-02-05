
import requests
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask import Flask, request, jsonify, redirect
from datetime import datetime
from logging.handlers import RotatingFileHandler
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')

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
        url = "https://api.nowpayments.io/v1/invoice"
        headers = {"x-api-key": NOWPAYMENTS_API_KEY,
                   "Content-Type": "application/json"}

        payload = {

            "price_amount": usd,
            "price_currency": "usd",
            "order_id": my_order_id,
            "order_description": f"Order for {phone}",
            "ipn_callback_url": f"{PUBLIC_BASE_URL}/nowpayment/webhook",
            "success_url": f"{PUBLIC_BASE_URL}/api/success-redirect?orderId={my_order_id}",
            "cancel_url": f"{PUBLIC_BASE_URL}/api/success-redirect?orderId={my_order_id}",
        }

        response = requests.post(
            url, json=payload, headers=headers, timeout=10)
        result = response.json()

        if result.get('id'):

            # Save to MySQL
            new_invoice = Invoice(
                order_id=my_order_id,
                invoice_id=result.get('id'),
                phone=phone,
                amount_usd=usd,
                amount_kes=kes,
                pay_url=result.get('invoice_url'),
                raw_response=json.dumps(result)  # Save raw JSON as string
            )

            db.session.add(new_invoice)
            db.session.commit()

            return jsonify({
                "payment_url": result.get('invoice_url'),
                "track_id": result.get('id')
            })
        else:
            app.logger.error(f"NowPay Error: {result.get('message')}")
            return jsonify({"error": result.get("message", "Nowpay failed")}), 400

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
@app.route("/nowpayment/webhook", methods=["POST"])
def nowpayment_webhook():
    print('WEBHOOK CALLED')
    try:
        # 1️⃣ Read signature
        received_sig = request.headers.get("x-nowpayments-sig")
        if not received_sig:
            app.logger.warning("Missing x-nowpayments-sig")
            return "Unauthorized", 401

        # 2️⃣ Load JSON body
        payload = request.get_json()

        print('PAYLOAD ********', payload)
        if not payload:
            return "Invalid payload", 400

        # 3️⃣ Sort payload recursively
        def sort_object(obj):
            if isinstance(obj, dict):
                return {k: sort_object(obj[k]) for k in sorted(obj)}
            if isinstance(obj, list):
                return [sort_object(i) for i in obj]
            return obj

        sorted_payload = sort_object(payload)

        # 4️⃣ Create HMAC
        ipn_secret = os.getenv("NOWPAYMENTS_IPN_SECRET").strip()
        signed_payload = json.dumps(
            sorted_payload,
            separators=(',', ':')
        )

        calculated_sig = hmac.new(
            ipn_secret.encode(),
            signed_payload.encode(),
            hashlib.sha512
        ).hexdigest()

        # 5️⃣ Compare signatures
        if not hmac.compare_digest(calculated_sig, received_sig):
            app.logger.warning("Invalid NOWPayments signature")
            return "Unauthorized", 401

        # 6️⃣ Extract data
        payment_status = payload.get("payment_status", "").lower()
        invoice_id = payload.get("invoice_id")
        payment_id = payload.get("payment_id")

        app.logger.info(
            f"NOWPayments webhook: {payment_id} -> {payment_status}"
        )

        if not invoice_id:
            return "No invoice id", 400

        invoice = Invoice.query.filter_by(invoice_id=str(invoice_id)).first()
        if not invoice:
            return "Invoice not found", 404

        # 7️⃣ Idempotency
        if invoice.status == "finished":
            return "ok"

        invoice.status = payment_status
        db.session.commit()

        # 8️⃣ TELEGRAM ALERT ONLY WHEN FINISHED
        if payment_status == "finished":
            msg = (
                f"💰 <b>PAYMENT CONFIRMED</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>USD:</b> {invoice.amount_usd}\n"
                f"<b>KES:</b> {invoice.amount_kes}\n"
                f"<b>Phone:</b> {invoice.phone}\n"
                f"<b>Invoice ID:</b> {invoice.invoice_id}\n"
                f"<b>Payment ID:</b> {payment_id}\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_alert(msg)

        return "ok"

    except Exception:
        app.logger.exception("NOWPayments webhook error")
        db.session.rollback()
        return "error", 500


# =========================================
# 4. CONTACT FORM EMAIL
# =========================================
@app.route("/api/contact", methods=["POST"])
def contact_form():
    try:
        data = request.get_json()
        name = data.get("name")
        email = data.get("email")
        message = data.get("message")

        if not name or not email or not message:
            return jsonify({"error": "All fields are required"}), 400

        # Email Configuration (Use variables from your .env)
        sender_email = os.getenv("EMAIL_USER")  # e.g., "system@daze-t.com"
        sender_password = os.getenv("EMAIL_PASS")
        admin_email = "admin@daze-t.com"

        # Create Email
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = admin_email
        msg['Subject'] = f"New Contact Form: {name}"

        body = f"Name: {name}\nEmail: {email}\n\nMessage:\n{message}"
        msg.attach(MIMEText(body, 'plain'))

        # Send using SMTP (assuming Gmail/CPanel/Outlook)
        # For CPanel: use 'localhost' or your mail server domain on port 465 or 587
        with smtplib.SMTP_SSL("mail.daze-t.com", 465) as server:
            server.login(sender_email, sender_password)
            server.send_message(msg)

        # Optional: Send a Telegram Alert too!
        send_telegram_alert(
            f"✉️ <b>New Message Received</b>\nFrom: {name}\nEmail: {email}")

        return jsonify({"success": "Message sent!"}), 200

    except Exception as e:
        app.logger.exception("Contact Form Error")
        return jsonify({"error": "Failed to send message"}), 500


if __name__ == "__main__":
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
