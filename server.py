"""
FedaPay -> WhatsApp auto-delivery server.
Receives FedaPay webhook (transaction.approved), then sends the guide PDF
to the customer via WhatsApp Cloud API as a document message.

Deploy: Render.com / Railway / any Python host (free tier OK).
Env vars required:
  FEDAPAY_WEBHOOK_SECRET  - from FedaPay dashboard > Webhooks
  WHATSAPP_TOKEN          - Meta Cloud API access token
  WHATSAPP_PHONE_ID       - Cloud API phone number ID (+226 64 87 53 97)
  GUIDE_PDF_URL           - public URL of the guide PDF (or local path)
  EXPECTED_AMOUNT         - amount in XOF to validate (default 5000)
"""
import hmac, hashlib, os, json, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fedapay-wa")

FEDAPAY_SECRET = os.environ.get("FEDAPAY_WEBHOOK_SECRET", "")
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID = os.environ.get("WHATSAPP_PHONE_ID", "")
GUIDE_PDF_URL = os.environ.get("GUIDE_PDF_URL", "")
EXPECTED_AMOUNT = int(os.environ.get("EXPECTED_AMOUNT", "5000"))
PORT = int(os.environ.get("PORT", "3000"))

SUCCESS_MSG = (
    "Merci pour ton achat ! Voici ton guide complet "
    "\"L'IA DÉTOURNÉE\" (13 pages). Bonne lecture ! "
    "Une question ? Réponds directement ici."
)


def verify_fedapay_signature(payload: bytes, header: str) -> bool:
    """FedaPay signs with X-FEDAPAY-SIGNATURE: t=<ts>,s=<hmac-sha256>"""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        sig = parts.get("s", "")
        expected = hmac.new(
            FEDAPAY_SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def wa_send_text(to: str, text: str) -> dict:
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
    data = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }).encode()
    req = Request(url, data=data, headers={
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    })
    return json.loads(urlopen(req, timeout=30).read())


def wa_send_document(to: str, doc_url: str, filename: str, caption: str) -> dict:
    url = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
    data = json.dumps({
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"link": doc_url, "filename": filename, "caption": caption},
    }).encode()
    req = Request(url, data=data, headers={
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    })
    return json.loads(urlopen(req, timeout=30).read())


def extract_customer_phone(event: dict) -> str | None:
    """Try common FedaPay payload shapes for the payer's phone."""
    data = event.get("data", {})
    cust = data.get("customer", {}) or {}
    for key in ("phone", "phone_number", "msisdn"):
        if cust.get(key):
            return "".join(c for c in str(cust[key]) if c.isdigit())
    # fallback: custom field on transaction
    for key in ("phone", "customer_phone"):
        if data.get(key):
            return "".join(c for c in str(data[key]) if c.isdigit())
    return None


class Handler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"received": true}')

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            self._ok()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        if urlparse(self.path).path != "/webhooks/fedapay":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        # Always ACK fast; process after.
        self._ok()
        try:
            event = json.loads(payload)
        except Exception:
            log.warning("bad json payload"); return

        sig = self.headers.get("X-FEDAPAY-SIGNATURE", "") or \
              self.headers.get("X-Fedapay-Signature", "")
        if FEDAPAY_SECRET and not verify_fedapay_signature(payload, sig):
            log.warning("invalid webhook signature"); return

        etype = event.get("type", "")
        data = event.get("data", {})
        log.info("webhook event: %s id=%s", etype, data.get("id"))
        if etype not in ("transaction.approved", "transaction.paid"):
            return
        if str(data.get("status", "")).lower() not in ("approved", "paid", "successful"):
            return
        try:
            amount = int(float(data.get("amount", 0)))
        except Exception:
            amount = 0
        if amount != EXPECTED_AMOUNT:
            log.warning("amount mismatch: got %s expected %s", amount, EXPECTED_AMOUNT)
            return

        phone = extract_customer_phone(event)
        if not phone:
            log.warning("no customer phone in payload; cannot deliver")
            return
        # Normalize to international without '+'
        if phone.startswith("00226"): phone = phone[2:]
        if len(phone) == 8: phone = "226" + phone

        try:
            wa_send_document(phone, GUIDE_PDF_URL,
                             "Guide_IA_DETOURNEE.pdf", SUCCESS_MSG)
            log.info("guide sent to %s", phone)
        except Exception as e:
            log.exception("WhatsApp send failed for %s: %s", phone, e)
            try:
                wa_send_text(phone, SUCCESS_MSG + " (doc en cours d'envoi)")
            except Exception:
                pass

    def log_message(self, *a):
        log.info(" ".join(str(x) for x in a))


if __name__ == "__main__":
    missing = [k for k, v in {
        "FEDAPAY_WEBHOOK_SECRET": FEDAPAY_SECRET,
        "WHATSAPP_TOKEN": WA_TOKEN,
        "WHATSAPP_PHONE_ID": WA_PHONE_ID,
        "GUIDE_PDF_URL": GUIDE_PDF_URL,
    }.items() if not v]
    if missing:
        log.warning("missing env vars: %s", ", ".join(missing))
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
