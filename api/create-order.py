import os
import json
import hmac
import hashlib
import razorpay
from http.server import BaseHTTPRequestHandler


def get_razorpay_client():
    key_id = os.environ.get("RAZORPAY_KEY_ID", "")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return None, key_secret
    client = razorpay.Client(auth=(key_id, key_secret))
    return client, key_secret


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        client, _ = get_razorpay_client()
        if not client:
            self._json_response({"error": "Razorpay not configured."}, 500)
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            amount = int(data.get('amount', 0)) * 100  # to paise
            currency = data.get('currency', 'INR')

            order = client.order.create({
                "amount": amount,
                "currency": currency,
                "payment_capture": "1"
            })
            self._json_response(order, 200)

        except Exception as e:
            print(f"Razorpay Order Error: {e}")
            self._json_response({"error": str(e)}, 500)

    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
