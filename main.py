import os
import psycopg2
import requests
import stripe
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# =========================
# Load environment variables
# =========================
load_dotenv()

app = Flask(__name__)

# =========================
# Stripe Configuration
# =========================
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# =========================
# Database Setup (PostgreSQL - Railway)
# =========================
def get_db_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn
    except Exception as e:
        print("❌ Database connection failed:", e)
        return None


def init_db():
    """Initialize database tables if not exist"""
    conn = get_db_connection()
    if conn is None:
        return "❌ Database connection failed."

    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS clients (
                id SERIAL PRIMARY KEY,
                name TEXT,
                whatsapp_token TEXT,
                whatsapp_phone_id TEXT,
                stripe_customer_id TEXT,
                subscription_status TEXT DEFAULT 'inactive'
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS auto_replies (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                keyword TEXT,
                reply_message TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id SERIAL PRIMARY KEY,
                client_id INTEGER REFERENCES clients(id),
                user_phone TEXT,
                message TEXT,
                reply TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        print("✅ Database initialized successfully!")
        return "✅ Database initialized successfully!"
    except Exception as e:
        print("❌ Error initializing DB:", e)
        return f"❌ Error initializing DB: {e}"
    finally:
        cur.close()
        conn.close()


# Jalankan init otomatis saat start
init_db()

# =========================
# WhatsApp Message Sender
# =========================
def send_whatsapp_message(client_id, to, message):
    """Send message to WhatsApp user based on client data"""
    conn = get_db_connection()
    if not conn:
        return

    cur = conn.cursor()
    cur.execute("""
        SELECT whatsapp_token, whatsapp_phone_id 
        FROM clients WHERE id = %s
    """, (client_id,))
    client = cur.fetchone()
    cur.close()
    conn.close()

    if not client:
        print(f"❌ Client ID {client_id} not found.")
        return

    token, phone_id = client
    url = f"https://graph.facebook.com/v17.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "text": {"body": message}
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"📤 WhatsApp Response ({client_id}):", response.text)
    except Exception as e:
        print("❌ Error sending WhatsApp message:", e)


# =========================
# WhatsApp Webhook Verification
# =========================
@app.route("/webhook", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        VERIFY_TOKEN = "versabotid_token"  # harus sama dengan di Meta Developer
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("✅ Webhook verified successfully!")
            return challenge, 200
        else:
            print("❌ Webhook verification failed.")
            return "Verification failed", 403

    elif request.method == "POST":
        data = request.get_json()
        print("📩 Incoming webhook data:", data)

        try:
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    print("🪶 Event received:", change.get("field"))
        except Exception as e:
            print("⚠️ Error parsing webhook data:", e)

        return jsonify({"status": "received"}), 200


# =========================
# Client-specific Webhook (auto reply)
# =========================
@app.route("/<int:client_id>/webhook", methods=["POST"])
def client_webhook(client_id):
    data = request.get_json()
    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "DB connection failed"}), 500
        cur = conn.cursor()

        # Cari auto-reply
        cur.execute("""
            SELECT reply_message 
            FROM auto_replies
            WHERE client_id = %s AND LOWER(keyword) = LOWER(%s)
        """, (client_id, text))
        row = cur.fetchone()
        reply = row[0] if row else "Halo 👋, terima kasih sudah menghubungi kami."

        # Simpan ke log
        cur.execute("""
            INSERT INTO conversations (client_id, user_phone, message, reply)
            VALUES (%s, %s, %s, %s)
        """, (client_id, from_number, text, reply))
        conn.commit()

        send_whatsapp_message(client_id, from_number, reply)

        cur.close()
        conn.close()
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("❌ Error handling webhook:", e)
        return jsonify({"error": str(e)}), 400


# =========================
# Stripe Webhook
# =========================
@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    event_type = event.get("type")
    data = event["data"]["object"]
    customer_id = data.get("customer")

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB connection failed"}), 500
    cur = conn.cursor()

    if event_type == "checkout.session.completed":
        cur.execute("""
            UPDATE clients 
            SET subscription_status = 'active' 
            WHERE stripe_customer_id = %s
        """, (customer_id,))
        print(f"✅ Subscription activated for {customer_id}")

    elif event_type == "invoice.payment_failed":
        cur.execute("""
            UPDATE clients 
            SET subscription_status = 'inactive' 
            WHERE stripe_customer_id = %s
        """, (customer_id,))
        print(f"❌ Payment failed for {customer_id}")

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})


# =========================
# Routes for Testing
# =========================
@app.route("/")
def home():
    return "🚀 Chatbot SaaS is running successfully on Railway!"


@app.route("/init-db")
def init_database():
    result = init_db()
    return result


# =========================
# Run App (Local / Railway)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
