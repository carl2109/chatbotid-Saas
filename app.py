import os
import psycopg2
from flask import Flask, request, jsonify
import requests
import stripe
from dotenv import load_dotenv

# Load environment variables (.env atau Render Dashboard)
load_dotenv()

# Flask app
app = Flask(__name__)

# Stripe config
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")  
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

# =========================
# Database setup (PostgreSQL di Railway)
# =========================
def get_db_connection():
    try:
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        return conn
    except Exception as e:
        print("❌ Database connection failed:", e)
        return None


def init_db():
    conn = get_db_connection()
    if conn is None:
        return "Database connection failed."

    cur = conn.cursor()

    # Table clients → data UMKM/tenant
    cur.execute('''
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            name TEXT,
            whatsapp_token TEXT,
            whatsapp_phone_id TEXT,
            stripe_customer_id TEXT,
            subscription_status TEXT DEFAULT 'inactive'
        )
    ''')

    # Table auto_replies → setting jawaban per tenant
    cur.execute('''
        CREATE TABLE IF NOT EXISTS auto_replies (
            id SERIAL PRIMARY KEY,
            client_id INTEGER REFERENCES clients(id),
            keyword TEXT,
            reply_message TEXT
        )
    ''')

    conn.commit()
    cur.close()
    conn.close()

    return "✅ Database initialized successfully!"


init_db()

# ==========================
# Kirim pesan WhatsApp (per tenant)
# ==========================
def send_whatsapp_message(client_id, to, message):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT whatsapp_token, whatsapp_phone_id FROM clients WHERE id = %s", (client_id,))
    client = cur.fetchone()
    cur.close()
    conn.close()

    if not client:
        print("❌ Client not found:", client_id)
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
    response = requests.post(url, headers=headers, json=payload)
    print(f"[{client_id}] WA Response:", response.text)

# ==========================
# Webhook WhatsApp per client
# ==========================
@app.route("/<int:client_id>/webhook", methods=["POST"])
def webhook(client_id):
    data = request.json
    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]
        from_number = message["from"]
        text = message.get("text", {}).get("body", "")

        # Cari auto-reply dari DB
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT reply FROM auto_replies
            WHERE client_id = %s AND LOWER(keyword) = LOWER(%s)
        """, (client_id, text))
        row = cur.fetchone()

        if row:
            reply = row[0]
        else:
            reply = "Halo 👋, terima kasih sudah menghubungi kami."

        # Simpan log percakapan
        cur.execute("""
            INSERT INTO conversations (client_id, user_phone, message, reply)
            VALUES (%s, %s, %s, %s)
        """, (client_id, from_number, text, reply))
        conn.commit()
        cur.close()
        conn.close()

        # Kirim balasan
        send_whatsapp_message(client_id, from_number, reply)

    except Exception as e:
        print("Error:", e)

    return jsonify({"status": "ok"})

# ==========================
# Stripe Webhook
# ==========================
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

    # Event handler
    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        customer_id = session.get("customer")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE clients SET subscription_status = %s WHERE stripe_customer_id = %s",
            ('active', customer_id)
        )
        conn.commit()
        cur.close()
        conn.close()

        print(f"✅ Subscription active for customer {customer_id}")

    elif event["type"] == "invoice.payment_failed":
        session = event["data"]["object"]
        customer_id = session.get("customer")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "UPDATE clients SET subscription_status = %s WHERE stripe_customer_id = %s",
            ('inactive', customer_id)
        )
        conn.commit()
        cur.close()
        conn.close()

        print(f"❌ Payment failed, subscription inactive for {customer_id}")

    return jsonify({"status": "ok"})

@app.route("/init-db")
def init_database():
    result = init_db()
    return result

@app.route("/")
def home():
    return "🚀 Chatbot SaaS is running successfully on Railway!"

# ==========================
# Run app
# ==========================
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port) 
