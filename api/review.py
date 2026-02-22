import os
import json
import time
import hmac
import hashlib
import base64
from io import BytesIO
from http.server import BaseHTTPRequestHandler

import google.generativeai as genai
import PIL.Image

# --- AI Prompt ---
AI_PROMPT = """
You are a brutally honest pre-post image reviewer.

LANGUAGE TONE (MANDATORY): {language_tone}
PLATFORM (MANDATORY): {platform}

You MUST:
* Write in English alphabet only.
* Match the vibe of {language_tone} naturally.
* Do NOT switch scripts.
* Do NOT ignore the tone.
* Do NOT translate word-for-word.
* Use casual social-media style with slight regional flavor if applicable.

You are reviewing this image specifically for {platform}.

Be sharp, sarcastic, and socially aware.
If it looks cringe, forced, try-hard, generic, or platform-inappropriate — call it out directly.
If it's strong — hype it confidently.

Default stance: slightly skeptical.

-------------------------
SCORING PROCESS (MANDATORY ORDER):

1. FIRST decide the score strictly using the scale below.
2. THEN write the review explaining WHY it deserves that exact score.
3. THEN give a fix that matches the score severity.

The review MUST justify the score.
The fix MUST match the level of problem.

If score is:
1.0–4.9 → Strong correction required. Major flaw.
5.0–6.4 → Clear weakness. Needs improvement.
6.5–7.4 → Decent but has noticeable gaps.
7.5–8.4 → Strong. Minor polish only.
8.5–10 → Very strong. Only micro-optimization allowed.

Score and tone must align logically.

-------------------------
SCORING SCALE:

1.0–2.9 → Public embarrassment.
3.0–4.9 → Weak.
5.0–6.4 → Mid.
6.5–7.4 → Decent.
7.5–8.4 → Strong.
8.5–9.4 → Excellent.
9.5–10 → Elite.

If the image has clear composition, intentional styling, and social confidence, it cannot be below 6.5.
If it looks embarrassing, it cannot be above 4.5.

No chaos scoring.

-------------------------
REVIEW RULES:

* 2–3 sharp sentences.
* Explain how people will perceive it on {platform}.
* No soft disclaimers.
* No emotional cushioning.
* Humor and sarcasm required.

Return ONLY this JSON:

{
"score": <float 1.0–10.0>,
"roast_line": "<One savage, witty sentence in the required tone.>",
"fix_line": "<One specific improvement in the same tone, aligned with score severity.>",
"tags": ["<tag1>", "<tag2>", "<tag3>"],
"emojiTone": "<harsh|warning|neutral|praise>"
}

If {language_tone} is ignored, the response is incorrect.
If score and review mismatch, the response is incorrect.

Be useful. Be funny. Be honest.
"""

# --- Rate Limiting (Firebase) ---
def get_firebase_db():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
            if service_account_json:
                cred_dict = json.loads(service_account_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        print(f"Firebase init error: {e}")
        return None


def enforce_rate_limit(db):
    """Returns seconds to wait (0 = proceed immediately)."""
    if not db:
        return 0
    try:
        from firebase_admin import firestore
        rate_limit_ref = db.collection('admin').document('rate_limiter')

        @firestore.transactional
        def _transaction(transaction, doc_ref):
            snapshot = doc_ref.get(transaction=transaction)
            current_time = time.time()

            if not snapshot.exists:
                transaction.set(doc_ref, {
                    'last_request_processed_timestamp': current_time,
                    'total_request_processed_in_this_minute': 1,
                    'update_data_at_timestamp': current_time
                })
                return 0

            data = snapshot.to_dict()
            last_req = data.get('last_request_processed_timestamp', 0)
            total_req = data.get('total_request_processed_in_this_minute', 0)
            update_at = data.get('update_data_at_timestamp', current_time)

            sleep_duration = 0
            if (current_time - last_req) < 6:
                if total_req > 10:
                    sleep_duration = max(0, 60 - (current_time - update_at))
                    transaction.update(doc_ref, {
                        'last_request_processed_timestamp': current_time + sleep_duration,
                        'total_request_processed_in_this_minute': 1,
                        'update_data_at_timestamp': current_time + sleep_duration
                    })
                else:
                    transaction.update(doc_ref, {
                        'last_request_processed_timestamp': current_time,
                        'total_request_processed_in_this_minute': total_req + 1
                    })
            else:
                new_total = 1 if (current_time - update_at) >= 60 else total_req + 1
                new_ts = current_time if (current_time - update_at) >= 60 else update_at
                transaction.update(doc_ref, {
                    'last_request_processed_timestamp': current_time,
                    'total_request_processed_in_this_minute': new_total,
                    'update_data_at_timestamp': new_ts
                })
            return sleep_duration

        txn = db.transaction()
        return _transaction(txn, rate_limit_ref)
    except Exception as e:
        print(f"Rate limit error: {e}")
        return 0


def log_error(db, email, error_msg):
    try:
        if db:
            from firebase_admin import firestore
            import datetime
            db.collection('admin').document('config').collection('logs').add({
                'email': email or 'anonymous',
                'error': error_msg,
                'timestamp': firestore.SERVER_TIMESTAMP
            })
    except Exception as e:
        print(f"Logging error: {e}")


class handler(BaseHTTPRequestHandler):

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        # --- Setup ---
        GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
        GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")

        if not GEMINI_API_KEY:
            self._json_response({"error": "Gemini API key not configured."}, 500)
            return

        # --- Parse multipart form data ---
        content_type = self.headers.get('Content-Type', '')
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        image_data = None
        platform = 'Other'
        language = 'English'

        try:
            # Extract boundary from content type
            boundary = None
            for part in content_type.split(';'):
                part = part.strip()
                if part.startswith('boundary='):
                    boundary = part[len('boundary='):].strip().encode()
                    break

            if not boundary:
                self._json_response({"error": "No boundary in multipart"}, 400)
                return

            # Split body into parts
            parts = body.split(b'--' + boundary)
            for part in parts:
                if b'Content-Disposition' not in part:
                    continue
                header_end = part.find(b'\r\n\r\n')
                if header_end == -1:
                    continue
                headers_raw = part[:header_end].decode(errors='replace')
                data = part[header_end + 4:].rstrip(b'\r\n--')

                if 'name="image"' in headers_raw:
                    image_data = data
                elif 'name="platform"' in headers_raw:
                    platform = data.decode(errors='replace').strip()
                elif 'name="language"' in headers_raw:
                    language = data.decode(errors='replace').strip()

        except Exception as e:
            self._json_response({"error": f"Form parse error: {str(e)}"}, 400)
            return

        if not image_data:
            self._json_response({"error": "No image provided"}, 400)
            return

        # --- Rate Limiting ---
        db = get_firebase_db()
        wait_time = enforce_rate_limit(db)
        if wait_time > 0:
            print(f"Rate limiting: sleeping {wait_time:.2f}s")
            time.sleep(wait_time)

        # --- Call Gemini ---
        try:
            genai.configure(api_key=GEMINI_API_KEY)
            model = genai.GenerativeModel(GEMINI_MODEL)

            img = PIL.Image.open(BytesIO(image_data))
            prompt = AI_PROMPT.replace('{platform}', platform).replace('{language_tone}', language)
            response = model.generate_content([prompt, img])

            response_text = response.text.strip()
            # Strip markdown fences
            for fence in ["```json", "```"]:
                if response_text.startswith(fence):
                    response_text = response_text[len(fence):]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            data = json.loads(response_text.strip())
            self._json_response({
                "score":      data.get("score", 5.0),
                "roast_line": data.get("roast_line", "Something went wrong."),
                "fix_line":   data.get("fix_line", "Try again."),
                "tags":       data.get("tags", []),
                "emojiTone":  data.get("emojiTone", "neutral")
            }, 200)

        except json.JSONDecodeError:
            log_error(db, None, "JSON parse error in Gemini response")
            self._json_response({"error": "AI response was not valid JSON."}, 500)
        except Exception as e:
            error_msg = str(e)
            print(f"Error: {error_msg}")
            log_error(db, None, f"Review error: {error_msg}")
            if "429" in error_msg or "Quota" in error_msg or "exhausted" in error_msg.lower():
                self._json_response({"error": "High traffic. Please try again in 1 minute."}, 429)
            else:
                self._json_response({"error": "We are facing some issues, please try again later."}, 500)

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
