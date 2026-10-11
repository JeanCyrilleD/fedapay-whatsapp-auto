"""
FedaPay -> WhatsApp conversational bot v2 ("Jarvis").

Two webhooks:
  POST /webhooks/fedapay   - FedaPay transaction.approved -> deliver guide PDF
  GET  /webhooks/whatsapp  - Meta webhook verification
  POST /webhooks/whatsapp  - incoming WhatsApp messages -> conversational AI

Conversation engine:
  - keyword intents (teaser, prix/acheter, diagnostic, humain, bonjour...)
  - AI fallback via Meta Model API (Spark 1.3) for open questions
  - per-user session state (in-memory, 24h TTL)
  - human handoff on request or frustration signals

Deploy: Render.com (render.yaml). Python stdlib only, no dependencies.

Env vars:
  FEDAPAY_WEBHOOK_SECRET  - FedaPay dashboard > Webhooks
  WHATSAPP_TOKEN          - Meta Cloud API access token
  WHATSAPP_PHONE_ID       - Cloud API phone number ID
  WHATSAPP_VERIFY_TOKEN   - token you choose for webhook verification
  META_API_KEY            - Meta Model API key (for the AI brain)
  GUIDE_PDF_URL           - public URL of the full guide PDF
  TEASER_PDF_URL          - public URL of the 3-page teaser PDF
  FEDAPAY_LINK_FR         - FedaPay payment page (guide)
  EXPECTED_AMOUNT         - amount in XOF to validate (default 5000)
  OWNER_PHONE             - Ahadi's WhatsApp number (for handoff alerts)
"""
import hmac, hashlib, os, json, time, logging, re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")

# ---------------------------------------------------------------- config
FEDAPAY_SECRET   = os.environ.get("FEDAPAY_WEBHOOK_SECRET", "")
WA_TOKEN         = os.environ.get("WHATSAPP_TOKEN", "")
WA_PHONE_ID      = os.environ.get("WHATSAPP_PHONE_ID", "")
WA_VERIFY_TOKEN  = os.environ.get("WHATSAPP_VERIFY_TOKEN", "jarvis_verify_2026")
META_API_KEY     = os.environ.get("META_API_KEY", "")
GUIDE_PDF_URL    = os.environ.get("GUIDE_PDF_URL", "")
TEASER_PDF_URL   = os.environ.get("TEASER_PDF_URL", "")
FEDAPAY_LINK_FR  = os.environ.get("FEDAPAY_LINK_FR",
                                  "https://me.fedapay.com/I72O5AQz")
EXPECTED_AMOUNT  = int(os.environ.get("EXPECTED_AMOUNT", "5000"))
OWNER_PHONE      = os.environ.get("OWNER_PHONE", "22671160238")
PORT             = int(os.environ.get("PORT", "3000"))

WA_API = f"https://graph.facebook.com/v21.0/{WA_PHONE_ID}/messages"
META_API = "https://api.meta.ai/v1/chat/completions"

GUIDE_PRICE = "5 000 FCFA"
DIAG_PRICE = "15 000 FCFA"

# ---------------------------------------------------------------- sessions
# phone -> {state, history:[(role,text)], updated, handed_off, teaser_sent, ...}
SESSIONS = {}
SESSION_TTL = 24 * 3600

def get_session(phone):
    now = time.time()
    s = SESSIONS.get(phone)
    if not s or now - s.get("updated", 0) > SESSION_TTL:
        s = {"state": "new", "history": [], "updated": now,
             "handed_off": False, "teaser_sent": False,
             "offer_sent": False, "diag_pitched": False}
        SESSIONS[phone] = s
    s["updated"] = now
    # prune old sessions occasionally
    if len(SESSIONS) > 500:
        for p in [p for p, x in SESSIONS.items()
                  if now - x.get("updated", 0) > SESSION_TTL]:
            del SESSIONS[p]
    return s

# ---------------------------------------------------------------- WhatsApp send
def wa_post(payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = Request(WA_API, data=data, headers={
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    })
    return json.loads(urlopen(req, timeout=30).read())

def wa_send_text(to: str, text: str):
    return wa_post({"messaging_product": "whatsapp", "to": to,
                    "type": "text", "text": {"body": text}})

def wa_send_document(to: str, doc_url: str, filename: str, caption: str = ""):
    doc = {"link": doc_url, "filename": filename}
    if caption:
        doc["caption"] = caption
    return wa_post({"messaging_product": "whatsapp", "to": to,
                    "type": "document", "document": doc})

def wa_send_buttons(to: str, body: str, buttons):
    """buttons: list of (id, title) max 3"""
    btns = [{"type": "reply", "reply": {"id": b[0], "title": b[1][:20]}}
            for b in buttons[:3]]
    return wa_post({"messaging_product": "whatsapp", "to": to,
                    "type": "interactive",
                    "interactive": {"type": "button",
                                    "body": {"text": body},
                                    "action": {"buttons": btns}}})

def notify_owner(text: str):
    """Alert Ahadi about a handoff or important event."""
    if not OWNER_PHONE or not WA_TOKEN:
        log.warning("owner notify skipped (no phone/token): %s", text[:80])
        return
    try:
        wa_send_text(OWNER_PHONE, "🔔 Jarvis: " + text)
    except Exception as e:
        log.warning("owner notify failed: %s", e)

# ---------------------------------------------------------------- AI brain
SYSTEM_PROMPT = """Tu es l'assistant commercial de Cyrille Ramdi Dahani sur WhatsApp.
Tu parles français, tu tutoies, ton ton est chaleureux et direct, sans blabla.
Tu vends deux choses :

1. Le guide « IA DÉTOURNÉE » — 13 pages, français + anglais, 5 000 FCFA.
   Contenu : 5 usages contraires à l'éthique de l'IA décryptés, 5 détournements,
   4 checklists, 10 questions pour les réunions, 10 actions pour les organisations.
   Paiement via FedaPay (lien : https://me.fedapay.com/I72O5AQz), livraison du PDF
   automatique sur WhatsApp après paiement.

2. Le « Diagnostic IA Express » — 15 000 FCFA : 30 minutes d'appel WhatsApp avec
   Cyrille + recommandations écrites personnalisées + le guide complet offert en bonus.

Règles :
- Réponds à la question posée, en 2-4 phrases max. Pas de pavés.
- Si la personne hésite sur le prix, rappelle ce qu'elle reçoit concrètement.
- Si elle veut acheter le guide, donne le lien FedaPay et explique : paie, puis le PDF arrive ici automatiquement.
- Si elle veut le diagnostic, dis que Cyrille la recontactera pour fixer l'appel après paiement (même lien FedaPay pour l'instant, préciser le montant au moment de payer n'est pas possible — dis-lui d'envoyer « DIAGNOSTIC » après paiement).
- Ne promets jamais ce que tu ne peux pas faire. Si tu ne sais pas, dis-le et propose de faire suivre à Cyrille.
- Ne sois jamais insistant : une relance douce max, puis tu laisses la personne tranquille.
- Si la personne est frustrée, en colère, ou demande un humain, réponds : « Je fais suivre à Cyrille, il te répondra directement. » et ne dis rien de plus sur le sujet.
"""

def ai_reply(history, user_text: str) -> str:
    """Ask Spark 1.3 via Meta Model API. Falls back to a safe message."""
    if not META_API_KEY:
        log.warning("META_API_KEY missing, AI fallback disabled")
        return ("Merci pour ton message ! Je transmets à Cyrille, "
                "il te répondra directement. 👍")
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for role, text in history[-8:]:
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_text})
    data = json.dumps({
        "model": "muse-spark-1.3",
        "messages": messages,
        "max_tokens": 300,
        "temperature": 0.7,
    }).encode()
    req = Request(META_API, data=data, headers={
        "Authorization": f"Bearer {META_API_KEY}",
        "Content-Type": "application/json",
    })
    try:
        resp = json.loads(urlopen(req, timeout=45).read())
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning("AI call failed: %s", e)
        return ("Merci pour ton message ! Je transmets à Cyrille, "
                "il te répondra directement. 👍")

# ---------------------------------------------------------------- intents
WELCOME = (
    "Salut 👋 Bienvenue ! Je suis l'assistant de Cyrille.\n\n"
    "Je peux t'envoyer le teaser gratuit du guide « IA DÉTOURNÉE », "
    "ou te parler de l'offre complète. Dis-moi ce qui t'intéresse 👇"
)
WELCOME_BUTTONS = [("teaser", "📄 Teaser gratuit"),
                   ("offre", "💰 Voir l'offre"),
                   ("humain", "👤 Parler à Cyrille")]

TEASER_MSG = (
    "Voici le teaser gratuit du guide « IA DÉTOURNÉE » — "
    "les 3 premières pages 🎁\n\n"
    "Le guide complet (13 pages, FR/EN) coûte 5 000 FCFA. "
    "Tu veux que je te dise comment l'obtenir ?"
)

OFFRE_MSG = (
    "📦 Le guide « IA DÉTOURNÉE » — 5 000 FCFA :\n"
    "• 5 détournements d'IA décryptés\n"
    "• 4 checklists + 10 questions réunion\n"
    "• 10 actions pour ton organisation\n"
    "• Français + anglais, PDF\n\n"
    f"Pour commander, paie ici : {FEDAPAY_LINK_FR}\n"
    "Le PDF arrive automatiquement sur ce WhatsApp après paiement ✅"
)

DIAG_MSG = (
    "🎯 Le Diagnostic IA Express — 15 000 FCFA :\n"
    "• 30 min d'appel WhatsApp avec Cyrille\n"
    "• Recommandations écrites pour TON cas\n"
    "• Le guide complet offert en bonus\n\n"
    "Tu veux réserver ? Envoie « DIAGNOSTIC », "
    "Cyrille te recontactera pour fixer l'appel."
)

HUMAIN_MSG = ("Pas de souci, je fais suivre à Cyrille — "
              "il te répondra directement ici. 👍")

def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().lower())

def detect_intent(text: str) -> str | None:
    t = norm(text)
    if re.search(r"\bteaser\b|gratuit|extrait|apercu|aperçu|3 pages", t):
        return "teaser"
    if re.search(r"\bprix\b|combien|coût|cout|tarif|acheter|commander|"
                 r"\bpayer\b|paiement|guide complet|je veux le guide", t):
        return "offre"
    if re.search(r"\bdiagnostic\b|audit|formation|appel|consultation|"
                 r"\bexpert\b|accompagnement", t):
        return "diagnostic"
    if re.search(r"\bhumain\b|quelqu.?un|appeler|téléphone|telephone|"
                 r"\bcyrille\b|parler à|responsable|patron", t):
        return "humain"
    if re.search(r"^(bonjour|salut|hello|bonsoir|cc|coucou|yo|bjr)[!. ]*$", t):
        return "bonjour"
    if re.search(r"\bmerci\b", t):
        return "merci"
    if re.search(r"\barnaque\b|escroc|faux|menteur|voleur|arnaqueur", t):
        return "colere"
    return None

# ---------------------------------------------------------------- conversation
def handle_message(phone: str, text: str, msg_id: str = ""):
    s = get_session(phone)
    if s["handed_off"]:
        # Bot paused for this user; let Ahadi handle it.
        log.info("msg from %s... ignored (handed off)", phone[-4:])
        notify_owner(f"nouveau message de {phone} (bot en pause) : {text[:100]}")
        return

    intent = detect_intent(text)
    log.info("msg from %s... intent=%s state=%s",
             phone[-4:], intent, s["state"])

    if intent == "bonjour" or s["state"] == "new":
        wa_send_buttons(phone, WELCOME, WELCOME_BUTTONS)
        s["state"] = "welcomed"
        return

    if intent == "teaser":
        if TEASER_PDF_URL and WA_TOKEN:
            try:
                wa_send_document(phone, TEASER_PDF_URL,
                                 "Teaser_IA_DETOURNEE.pdf", TEASER_MSG)
            except Exception as e:
                log.warning("teaser send failed: %s", e)
                wa_send_text(phone, TEASER_MSG +
                             "\n\n(Petit souci technique avec le fichier — "
                             "Cyrille te l'enverra directement.)")
                notify_owner(f"échec envoi teaser à {phone}")
        else:
            wa_send_text(phone, TEASER_MSG)
        s["teaser_sent"] = True
        s["state"] = "teaser_sent"
        return

    if intent == "offre":
        wa_send_text(phone, OFFRE_MSG)
        s["offer_sent"] = True
        s["state"] = "offer_sent"
        return

    if intent == "diagnostic":
        wa_send_text(phone, DIAG_MSG)
        s["diag_pitched"] = True
        s["state"] = "diag_pitched"
        notify_owner(f"{phone} intéressé par le Diagnostic IA Express.")
        return

    if intent == "humain":
        wa_send_text(phone, HUMAIN_MSG)
        s["handed_off"] = True
        notify_owner(f"⚠️ {phone} demande à parler à un humain. "
                     f"Dernier message : {text[:150]}")
        return

    if intent == "merci":
        wa_send_text(phone, "Avec plaisir ! 😊 Autre chose ?")
        return

    if intent == "colere":
        wa_send_text(phone,
                     "Je comprends ta méfiance, c'est normal. "
                     "Je fais suivre à Cyrille, il te répondra directement "
                     "et tu pourras juger par toi-même. 👍")
        s["handed_off"] = True
        notify_owner(f"⚠️ {phone} méfiant/agacé : {text[:150]}")
        return

    # --- AI fallback for everything else
    s["history"].append(("user", text))
    reply = ai_reply(s["history"], text)
    s["history"].append(("assistant", reply))
    # Safety: if AI suggests human handoff, honor it
    if re.search(r"cyrille.*(répondra|recontactera|faire suivre)", norm(reply)):
        s["handed_off"] = True
        notify_owner(f"Jarvis a passé la main pour {phone}. "
                     f"Contexte : {text[:120]}")
    try:
        wa_send_text(phone, reply)
    except Exception as e:
        log.warning("reply send failed: %s", e)

def handle_button(phone: str, button_id: str):
    mapping = {"teaser": "teaser", "offre": "offre", "humain": "humain",
               "TEASER": "teaser", "OFFRE": "offre", "HUMAIN": "humain"}
    intent = mapping.get(button_id, "")
    if intent == "teaser":
        handle_message(phone, "teaser")
    elif intent == "offre":
        handle_message(phone, "je veux acheter le guide")
    elif intent == "humain":
        handle_message(phone, "je veux parler à un humain")
    else:
        handle_message(phone, button_id)

# ---------------------------------------------------------------- FedaPay
SUCCESS_MSG = (
    "Merci pour ton achat ! 🎉 Voici ton guide complet "
    "\"IA DÉTOURNÉE\" (13 pages, FR/EN). Bonne lecture !\n\n"
    "Une question sur le contenu ? Pose-la ici, je te réponds. 👇"
)

def verify_fedapay_signature(payload: bytes, header: str) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        sig = parts.get("s", "")
        expected = hmac.new(FEDAPAY_SECRET.encode(), payload,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False

def extract_customer_phone(event: dict) -> str | None:
    data = event.get("data", {})
    cust = data.get("customer", {}) or {}
    for key in ("phone", "phone_number", "msisdn"):
        if cust.get(key):
            return "".join(c for c in str(cust[key]) if c.isdigit())
    for key in ("phone", "customer_phone"):
        if data.get(key):
            return "".join(c for c in str(data[key]) if c.isdigit())
    return None

def handle_fedapay(payload: bytes, headers):
    try:
        event = json.loads(payload)
    except Exception:
        log.warning("bad json payload"); return
    sig = headers.get("X-FEDAPAY-SIGNATURE", "") or \
        headers.get("X-Fedapay-Signature", "")
    if FEDAPAY_SECRET and not verify_fedapay_signature(payload, sig):
        log.warning("invalid webhook signature"); return
    etype = event.get("type", "")
    data = event.get("data", {})
    log.info("webhook event: %s id=%s", etype, data.get("id"))
    if etype not in ("transaction.approved", "transaction.paid"):
        return
    if str(data.get("status", "")).lower() not in ("approved", "paid",
                                                  "successful"):
        return
    try:
        amount = int(float(data.get("amount", 0)))
    except Exception:
        amount = 0
    if amount != EXPECTED_AMOUNT:
        log.warning("amount mismatch: got %s expected %s",
                    amount, EXPECTED_AMOUNT)
        return
    phone = extract_customer_phone(event)
    if not phone:
        log.warning("no customer phone in payload; cannot deliver")
        notify_owner("⚠️ paiement FedaPay reçu mais pas de numéro client — "
                     "livraison manuelle requise.")
        return
    if phone.startswith("00226"):
        phone = phone[2:]
    if len(phone) == 8:
        phone = "226" + phone
    # Mark as customer so the bot knows
    s = get_session(phone)
    s["state"] = "customer"
    try:
        wa_send_document(phone, GUIDE_PDF_URL,
                         "Guide_IA_DETOURNEE.pdf", SUCCESS_MSG)
        log.info("guide sent to %s", phone[-4:])
        notify_owner(f"✅ vente confirmée : guide livré à {phone} "
                     f"({amount} FCFA).")
    except Exception as e:
        log.exception("WhatsApp send failed for %s", phone[-4:])
        notify_owner(f"⚠️ paiement {amount} FCFA de {phone} reçu mais "
                     f"livraison WhatsApp échouée : {e}")

# ---------------------------------------------------------------- WhatsApp webhook
def handle_whatsapp_post(payload: bytes):
    try:
        data = json.loads(payload)
    except Exception:
        log.warning("bad whatsapp json"); return
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") == "text":
                    phone = msg.get("from", "")
                    text = msg.get("text", {}).get("body", "")
                    handle_message(phone, text, msg.get("id", ""))
                elif msg.get("type") == "interactive":
                    ir = msg.get("interactive", {})
                    br = ir.get("button_reply", {})
                    if br.get("id"):
                        handle_button(msg.get("from", ""), br["id"])
                elif msg.get("type") in ("image", "document", "audio",
                                         "video", "sticker"):
                    phone = msg.get("from", "")
                    s = get_session(phone)
                    if not s["handed_off"]:
                        wa_send_text(phone,
                                     "Bien reçu ! 👍 Je fais suivre à Cyrille, "
                                     "il regardera ça directement.")
                        s["handed_off"] = True
                        notify_owner(f"📎 {phone} a envoyé un fichier "
                                     f"({msg.get('type')}) — à traiter.")
                # ignore statuses/delivery receipts

# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def _ok(self, body=b'{"received": true}'):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._ok()
            return
        if parsed.path == "/webhooks/whatsapp":
            q = parse_qs(parsed.query)
            mode = q.get("hub.mode", [""])[0]
            token = q.get("hub.verify_token", [""])[0]
            challenge = q.get("hub.challenge", [""])[0]
            if mode == "subscribe" and token == WA_VERIFY_TOKEN:
                log.info("whatsapp webhook verified")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(challenge.encode())
            else:
                log.warning("whatsapp verify failed")
                self.send_response(403); self.end_headers()
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        self._ok()  # ACK fast, process after
        if path == "/webhooks/fedapay":
            handle_fedapay(payload, self.headers)
        elif path == "/webhooks/whatsapp":
            handle_whatsapp_post(payload)
        else:
            log.warning("unknown POST path: %s", path)

    def log_message(self, *a):
        log.info(" ".join(str(x) for x in a))


if __name__ == "__main__":
    missing = [k for k, v in {
        "FEDAPAY_WEBHOOK_SECRET": FEDAPAY_SECRET,
        "WHATSAPP_TOKEN": WA_TOKEN,
        "WHATSAPP_PHONE_ID": WA_PHONE_ID,
        "META_API_KEY": META_API_KEY,
        "GUIDE_PDF_URL": GUIDE_PDF_URL,
        "TEASER_PDF_URL": TEASER_PDF_URL,
    }.items() if not v]
    if missing:
        log.warning("missing env vars: %s", ", ".join(missing))
    log.info("Jarvis v2 starting on port %s", PORT)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
