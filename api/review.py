import os
import json
import time
import hmac
import hashlib
import sys
import traceback
from io import BytesIO
from http.server import BaseHTTPRequestHandler
from email.parser import BytesParser
from email.policy import default

from google import genai
from google.genai import types
import PIL.Image

# --- Latest Prompt Logic ---
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
SCORING PROCESS (MANDATORY ORDER) :

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

# --- Firebase Admin ---
def get_db():
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        if not firebase_admin._apps:
            service_account_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
            if service_account_json:
                cred_dict = json.loads(service_account_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
            else:
                return None
        return firestore.client()
    except Exception as e:
        print(f"Firebase Init Error: {e}", file=sys.stderr)
        return None

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        self._json_response({"status": "Online", "service": "PrePublic AI Review"}, 200)

    def do_POST(self):
        try:
            db = get_db()
            
            # 1. Maintenance Check
            if db:
                try:
                    config = db.collection('admin').document('config').get()
                    if config.exists and config.to_dict().get('isMaintenance', False):
                        self._json_response({"error": "System is currently under maintenance."}, 503)
                        return
                except: pass

            # 2. Rate Limiting Logic
            wait_time = self._enforce_limit(db)
            if wait_time > 0:
                time.sleep(wait_time)

            # 3. Parse Multipart Form
            ctype = self.headers.get('Content-Type', '')
            clength = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(clength)
            
            # Extract boundary
            msg_raw = f"Content-Type: {ctype}\r\n\r\n".encode() + body
            msg = BytesParser(policy=default).parsebytes(msg_raw)
            
            p_img, p_plat, p_lang = None, "Other", "English"
            if msg.is_multipart():
                for part in msg.iter_parts():
                    name = part.get_param('name', header='content-disposition')
                    if name == 'image': p_img = part.get_payload(decode=True)
                    elif name == 'platform': p_plat = part.get_payload(decode=True).decode(errors='replace').strip()
                    elif name == 'language': p_lang = part.get_payload(decode=True).decode(errors='replace').strip()
            
            if not p_img:
                self._json_response({"error": "No image found in request."}, 400)
                return

            # 4. Gemini Execution
            api_key = os.environ.get("GEMINI_API_KEY", "")
            model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

            # Load generation params from environment
            try:
                temperature = float(os.environ.get("GEMINI_TEMPERATURE", "1.0"))
            except (ValueError, TypeError):
                temperature = 1.0

            try:
                top_k = int(os.environ.get("GEMINI_TOP_K", "70"))
            except (ValueError, TypeError):
                top_k = 70

            try:
                top_p = float(os.environ.get("GEMINI_TOP_P", "0.8"))
            except (ValueError, TypeError):
                top_p = 0.8

            client = genai.Client(api_key=api_key)
            img = PIL.Image.open(BytesIO(p_img))
            prompt = AI_PROMPT.replace('{platform}', p_plat).replace('{language_tone}', p_lang)

            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, img],
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    top_k=top_k,
                    top_p=top_p
                )
            )
            text = response.text.strip()
            
            # Clean JSON
            for fence in ["```json", "```"]:
                if text.startswith(fence): text = text[len(fence):]
            if text.endswith("```"): text = text[:-3]
            
            res_data = json.loads(text.strip())
            self._json_response(res_data, 200)

        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            err_str = str(e)
            if "429" in err_str or "Quota" in err_str:
                self._json_response({"error": "Global AI capacity reached. Try again in 1 minute."}, 429)
            else:
                self._json_response({"error": "Internal processor error. Please try again."}, 500)

    def _enforce_limit(self, db):
        if not db: return 0
        try:
            from firebase_admin import firestore
            ref = db.collection('admin').document('rate_limiter')
            @firestore.transactional
            def _txn(transaction, doc_ref):
                snap = doc_ref.get(transaction=transaction)
                now = time.time()
                if not snap.exists:
                    transaction.set(doc_ref, {'last_request_processed_timestamp': now, 'total_request_processed_in_this_minute': 1, 'update_data_at_timestamp': now})
                    return 0
                d = snap.to_dict()
                last, total, update = d.get('last_request_processed_timestamp', 0), d.get('total_request_processed_in_this_minute', 0), d.get('update_data_at_timestamp', now)
                
                sleep = 0
                if (now - last) < 6 and total > 10:
                    sleep = max(0, 60 - (now - update))
                    transaction.update(doc_ref, {'last_request_processed_timestamp': now + sleep, 'total_request_processed_in_this_minute': 1, 'update_data_at_timestamp': now + sleep})
                else:
                    new_total = 1 if (now - update) >= 60 else total + 1
                    new_up = now if (now - update) >= 60 else update
                    transaction.update(doc_ref, {'last_request_processed_timestamp': now, 'total_request_processed_in_this_minute': new_total, 'update_data_at_timestamp': new_up})
                return sleep
            return _txn(db.transaction(), ref)
        except: return 0

    def _send_cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _json_response(self, data, status):
        body = json.dumps(data).encode()
        self.send_response(status)
        self._send_cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
