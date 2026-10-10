# FedaPay → WhatsApp : livraison auto du guide

## Ce que fait le serveur
1. Reçoit le webhook FedaPay quand un paiement est approuvé (`transaction.approved`)
2. Vérifie la signature + le montant (5 000 FCFA)
3. Envoie le guide PDF au client via WhatsApp Cloud API (message document)

## Déploiement (Render.com, gratuit)
1. Crée un compte sur render.com → New → Web Service → connecte ce dossier
   (ou dépose `server.py` seul, commande de démarrage : `python server.py`)
2. Variables d'environnement à renseigner :
   - `FEDAPAY_WEBHOOK_SECRET` — dashboard FedaPay → Webhooks → créer → copier le secret
   - `WHATSAPP_TOKEN` — developers.facebook.com → ton app → token d'accès
   - `WHATSAPP_PHONE_ID` — ID du numéro +226 64 87 53 97 (WhatsApp Manager)
   - `GUIDE_PDF_URL` — URL publique du PDF (ex: lien Muse/Drive)
   - `EXPECTED_AMOUNT` — `5000` (défaut)
3. Note l'URL publique du service, ex : `https://ton-service.onrender.com`

## Config FedaPay
Dashboard FedaPay → Webhooks → Add → URL :
`https://ton-service.onrender.com/webhooks/fedapay`
Événements : cocher `transaction.approved` (et `transaction.paid` si proposé).

## Important : le numéro du client
Le webhook FedaPay doit contenir le téléphone du payeur pour savoir où envoyer
le guide. Deux options :
- **Option 1** : sur ta page FedaPay, ajoute un champ « numéro WhatsApp » obligatoire
  au formulaire de paiement → le numéro arrive dans `customer.phone`.
- **Option 2** : le client t'écrit d'abord sur WhatsApp (cas actuel via la pub),
  tu fais correspondre le paiement au numéro manuellement en attendant mieux.

## Pour Orange Money direct (sans FedaPay)
Pas de webhook possible. Circuit semi-auto : le client envoie la capture du reçu
sur WhatsApp → tu vérifies → tu envoies le PDF (10 secondes).

## Test
1. `curl https://ton-service.onrender.com/health` → `{"received": true}`
2. Paiement test FedaPay en sandbox → vérifie les logs Render.
