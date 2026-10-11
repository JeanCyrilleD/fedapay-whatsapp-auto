# Jarvis v2 — Bot WhatsApp conversationnel + livraison auto FedaPay

## Ce que fait le bot

**1. Livraison automatique après paiement** (`POST /webhooks/fedapay`)
- FedaPay `transaction.approved` → envoie le guide PDF sur WhatsApp
- Vérifie la signature webhook + le montant

**2. Bot conversationnel** (`/webhooks/whatsapp`)
- **Message de bienvenue** avec boutons : Teaser gratuit / Voir l'offre / Parler à Cyrille
- **Intents** : `teaser` → envoie le PDF teaser · `prix/acheter` → offre + lien FedaPay
  · `diagnostic` → pitch Diagnostic IA Express (15 000 FCFA) · `humain` → passe la main
  · détection de colère/méfiance → passe la main + alerte
- **IA (Spark 1.3)** : répond aux questions ouvertes sur le guide, en français, ton direct
- **Handoff humain** : le bot se met en pause pour l'utilisateur, Cyrille est notifié sur WhatsApp
- **Fichiers reçus** (images, docs, vocaux) : transmis à Cyrille

## Variables d'environnement (Render)

| Var | Rôle |
|-----|------|
| `FEDAPAY_WEBHOOK_SECRET` | Secret webhook FedaPay |
| `WHATSAPP_TOKEN` | Token Cloud API Meta |
| `WHATSAPP_PHONE_ID` | ID du numéro Cloud API |
| `WHATSAPP_VERIFY_TOKEN` | `jarvis_verify_2026` (pour la vérif webhook Meta) |
| `META_API_KEY` | Clé Meta Model API (cerveau IA) |
| `GUIDE_PDF_URL` | URL publique du guide complet |
| `TEASER_PDF_URL` | URL publique du teaser 3 pages |
| `FEDAPAY_LINK_FR` | Page de paiement FedaPay |
| `EXPECTED_AMOUNT` | `5000` |
| `OWNER_PHONE` | `22671160238` (alertes) |

## Webhooks à configurer

- **FedaPay** : `https://fedapay-whatsapp-auto.onrender.com/webhooks/fedapay` (event: `transaction.approved`)
- **Meta WhatsApp** : `https://fedapay-whatsapp-auto.onrender.com/webhooks/whatsapp` (verify token: `jarvis_verify_2026`)

## Test local des intents

```bash
python3 -c "import server; print(server.detect_intent('je veux le teaser'))"
```
