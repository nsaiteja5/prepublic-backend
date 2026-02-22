import os
import json
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
        if not RAZORPAY_KEY_SECRET:
            self._json_response({"status": "failed", "error": "Razorpay secret not configured."}, 500)
            return

        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)

            razorpay_payment_id = data.get('razorpay_payment_id')
            razorpay_order_id   = data.get('razorpay_order_id')
            razorpay_signature  = data.get('razorpay_signature')

            msg = f"{razorpay_order_id}|{razorpay_payment_id}"
            generated_signature = hmac.new(
                RAZORPAY_KEY_SECRET.encode('utf-8'),
                msg.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            if generated_signature == razorpay_signature:
                self._json_response({"status": "verified"}, 200)
            else:
                self._json_response({"status": "failed", "error": "Invalid signature"}, 400)

        except Exception as e:
            print(f"Verify Payment Error: {e}")
            self._json_response({"status": "failed", "error": str(e)}, 500)

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
