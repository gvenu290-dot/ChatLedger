# ChatLedger: setup

Claude reads the receipt. This app stores it, flags problems with plain rules, and shares the ledger with the accountant. No AI key is used anywhere.

## Render settings
- Runtime: Python 3
- Build command:  pip install -r requirements.txt
- Start command:  uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"
- Health check path: /healthz

## Environment variables (all required, the app refuses to start without them)
- DATABASE_URL     the Neon connection string
- ADMIN_USER       your login name
- ADMIN_PASSWORD   your login password
- SESSION_SECRET   long random text
- MCP_SECRET       long random letters and digits only (no spaces or slashes)
- PYTHON_VERSION   3.12.8   (optional but recommended)

## After deploy
1. Open the app URL, log in, go to Settings.
2. Copy the "Connect Claude" link into Claude: Customize > Connectors > + > Add custom connector.
3. Send sample_receipt.txt (pasted or as a photo) and say: Add this to my ledger.

## Rules the app applies (no AI)
- Possible duplicate: same vendor, amount and date as another non-rejected expense.
- Missing GST number: tax is shown but no supplier GSTIN.
- Missing invoice number.
- Category over its monthly limit (set on the Settings page).
