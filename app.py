
import requests
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask import Flask, request, jsonify, redirect, send_file
from backtest_service import run_backtest_for_month
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
    payment_id = db.Column(db.String(100), unique=True, nullable=False)

    phone = db.Column(db.String(50), nullable=False)

    amount_usd = db.Column(db.Float, nullable=False)
    amount_kes = db.Column(db.Float, nullable=False)

    pay_address = db.Column(db.String(255))
    pay_currency = db.Column(db.String(20))

    payment_status = db.Column(db.String(50), default="waiting")

    actually_paid = db.Column(db.Float, nullable=True)
    outcome_amount = db.Column(db.Float, nullable=True)
    outcome_currency = db.Column(db.String(20), nullable=True)

    raw_response = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)


# Create tables if they don't exist (Run this once)
with app.app_context():
    db.create_all()

# CONSTANTS
PAID_STATUSES = ["paid", "manual_accept", "confirmed", "completed", "success"]
ALERT_STATUSES = [
    "partially_paid",
    "finished",
    "failed",
    "refunded",
    "expired"
]
OXAPAY_KEY = os.getenv('OXAPAY_API_KEY')
PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', "https://api.daze-t.com")
FRONTEND_URL = os.getenv('FRONTEND_URL', "https://daze-t.com")
# FRONTEND_URL = 'http://localhost:3000'

NOWPAYMENTS_API_KEY = os.getenv('NOWPAYMENTS_API_KEY')


TOP_ASSETS = [
    {"value": "btc", "label": "BTC"},
    {"value": "eth", "label": "ETH"},
    {"value": "ltc", "label": "LTC"},

    # Memo / Tag coins (kept)
    {"value": "ada", "label": "ADA"},
    {"value": "algo", "label": "ALGO"},
    {"value": "bch", "label": "BCH"},
    {"value": "bnb", "label": "BNB"},
    {"value": "dash", "label": "DASH"},
    {"value": "doge", "label": "DOGE"},
    {"value": "trx", "label": "TRX"},
    {"value": "xlm", "label": "XLM"},
    {"value": "xmr", "label": "XMR"},
    {"value": "xrp", "label": "XRP"},
    {"value": "zec", "label": "ZEC"}

]


TOP_CURRENCY_PAIRS = [
    {"value": "AUD_JPY", "label": "AUDJPY"},
    {"value": "AUD_USD", "label": "AUDUSD"},
    {"value": "AUD_CHF", "label": "AUDCHF"},


    {"value": "USD_CAD", "label": "USDCAD"},
    {"value": "USD_CHF", "label": "USDCHF"},
    {"value": "USD_JPY", "label": "USDJPY"},
    {"value": "GBP_CAD", "label": "GBPCAD"},
    {"value": "GBP_JPY", "label": "GBPJPY"},
    {"value": "GBP_USD", "label": "GBPUSD"},
    {"value": "GBP_AUD", "label": "GBPAUD"},
    {"value": "EUR_CAD", "label": "EURCAD"},
    {"value": "NZD_JPY", "label": "NZDJPY"},


]


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
# GET AVAILABLE CRYPTO CURRENCIES
# =========================================

@app.route("/api/currencies", methods=["GET"])
def get_currencies():
    return TOP_ASSETS


@app.route("/api/currency-pairs", methods=["GET"])
def get_currency_pairs():
    return TOP_CURRENCY_PAIRS


@app.route("/api/create-trade", methods=["POST"])
def create_trade():
    try:
        data = request.get_json()
        usd = float(data.get("usd", 0))
        kes = float(data.get("kes", 0))
        phone = data.get("phone")
        pay_currency = data.get("currency")

        # Input Validation
        if kes > 100000:  # Adjusted limit, verify your needs
            return jsonify({"error": "Max limit exceeded"}), 400
        if usd < 0.1:
            return jsonify({"error": "Min amount 0.1 USD"}), 400
        if not phone:
            return jsonify({"error": "Phone number required"}), 400

        my_order_id = f"INV-{int(time.time() * 1000)}"

        # Create NOWPayment
        url = "https://api.nowpayments.io/v1/payment"
        headers = {"x-api-key": NOWPAYMENTS_API_KEY,
                   "Content-Type": "application/json"}

        payload = {
            "price_amount": usd,
            "price_currency": "usd",
            "pay_currency": pay_currency,
            "ipn_callback_url": f"{PUBLIC_BASE_URL}",
            "order_id": my_order_id,
            "order_description": f"Order for {phone}",
        }

        response = requests.post(
            url, json=payload, headers=headers, timeout=10)
        result = response.json()

        if result.get('payment_id'):

            # Save to MySQL
            new_invoice = Invoice(
                order_id=my_order_id,
                payment_id=result.get("payment_id"),

                phone=phone,
                amount_usd=result.get("price_amount"),
                amount_kes=kes,

                pay_address=result.get("pay_address"),
                pay_currency=result.get("pay_currency"),

                payment_status=result.get("payment_status"),

                raw_response=json.dumps(result)
            )

            db.session.add(new_invoice)
            db.session.commit()

            # GET USER TO A PAGE TO SHOW A BLINKING STATUS OF THE PAYMENT
            return jsonify({
                'payment_id': result.get('payment_id'),
                'payment_status': result.get('payment_status'),
                'pay_address': result.get('pay_address'),
                'pay_currency': result.get('pay_currency'),
                'price_amount': result.get('price_amount'),
                # frontend-friendly payment page
                "payment_url": f"{FRONTEND_URL}/pay/{result.get('payment_id')}"

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
            f"trackId={invoice.order_id}&"
            f"amount={invoice.amount_kes}&"
            f"phone={invoice.phone}"
        )
        return redirect(react_url)

    except Exception as e:
        app.logger.exception("Redirect Error")
        return redirect(FRONTEND_URL)


#  have a trigger from user, something like hit confirm after sending payment


def check_payment_status(payment_id):
    url = f"https://api.nowpayments.io/v1/payment/{payment_id}"
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json"
    }

    try:
        res = requests.get(url, headers=headers, timeout=10)
        data = res.json()

        if not data.get("payment_status"):
            logger.warning(f"No status returned for {payment_id}")
            return None

        invoice = Invoice.query.filter_by(payment_id=payment_id).first()
        if not invoice:
            logger.error(f"Invoice not found for payment_id={payment_id}")
            return None

        old_status = invoice.payment_status
        new_status = data.get("payment_status")

        # Update DB
        invoice.payment_status = new_status
        invoice.actually_paid = data.get("actually_paid")
        invoice.outcome_amount = data.get("outcome_amount")
        invoice.outcome_currency = data.get("outcome_currency")
        invoice.raw_response = json.dumps(data)

        db.session.commit()

        logger.info(
            f"[PAYMENT POLL] {payment_id}: {old_status} → {new_status}"
        )

        # Telegram alert ONLY on important statuses AND status change
        if new_status in ALERT_STATUSES and new_status != old_status:

            status_icons = {
                "finished": "🟢",
                "partially_paid": "🟠",
                "failed": "🔴",
                "expired": "🔴",
                "refunded": "🔴",
            }

            icon = status_icons.get(new_status, "ℹ️")

            msg = (
                f"💰 <b>PAYMENT ALERT {icon}!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>Receipt ID:</b> {payment_id}\n"
                f"<b>Status:</b> {new_status} USD\n"
                f"<b>Amount:</b> {invoice.amount_usd} USD\n"
                f"<b>Total Paid:</b> {invoice.amount_kes} KES\n"
                f"<b>Customer Phone:</b> {invoice.phone}\n"
                f"━━━━━━━━━━━━━━━━━━"
            )
            send_telegram_alert(msg)

        return new_status

    except Exception as e:
        logger.exception(f"Polling failed for {payment_id}")
        return None


@app.route("/api/check-payment", methods=["GET"])
def check_payment():
    payment_id = request.args.get("payment_id")
    if not payment_id:
        return jsonify({"error": "payment_id required"}), 400

    status = check_payment_status(payment_id)
    invoice = Invoice.query.filter_by(payment_id=payment_id).first()

    if not invoice:
        return jsonify({"error": "Invoice not found"}), 404

    # Parse the raw response to get the exact crypto pay_amount
    raw_data = json.loads(invoice.raw_response) if invoice.raw_response else {}

    return jsonify({
        "payment_id": invoice.payment_id,
        "status": invoice.payment_status,
        "pay_address": invoice.pay_address,
        # Exact crypto amount
        "pay_amount": raw_data.get("pay_amount", invoice.amount_usd),
        "pay_currency": invoice.pay_currency,
        "price_amount": invoice.amount_usd,  # The original USD price
        "amount_kes": invoice.amount_kes,
        "phone": invoice.phone
    })


# =========================================
# 5. OANDA BACKTEST ROUTE
# =========================================
@app.route("/api/run-backtest", methods=["POST"])
def api_run_backtest():
    try:
        data = request.get_json()
        instrument = data.get("instrument")
        year = int(data.get("year"))
        month = int(data.get("month"))

        if not instrument or not year or not month:
            return jsonify({"error": "Missing required parameters"}), 400

        app.logger.info(f"Running backtest for {instrument} - {month}/{year}")

        # Call the refactored Python module
        filepath = run_backtest_for_month(instrument, year, month)

        # Send the generated Excel file back to the user
        return send_file(
            filepath,
            as_attachment=True,
            download_name=f"{instrument}_backtest.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        app.logger.error(f"Backtest Error: {str(e)}")
        return jsonify({"error": "Failed to generate backtest report"}), 500


# =========================================
# . CONTACT FORM EMAIL
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
