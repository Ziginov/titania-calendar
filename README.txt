Titania cloud bundle

Files:
- titania_cloud.py
- titania_bootstrap.py
- .github/workflows/titania.yml

One-time setup:
1. Put titania_cloud.py into the root of your titania-calendar repo.
2. Put titania.yml into .github/workflows/titania.yml.
3. Run locally in the repo folder:
      python titania_bootstrap.py
   Paste current Titania refresh_token.
4. Upload/commit titania_refresh_token.enc.
5. In GitHub repo -> Settings -> Secrets and variables -> Actions, create:
   TITANIA_TOKEN_KEY
   TELEGRAM_BOT_TOKEN
   TELEGRAM_CHAT_ID
6. Commit/push all files.
7. Open Actions -> Titania calendar update -> Run workflow for first test.

Schedule:
- Daily at 20:00 Europe/Helsinki.
- Manual Run workflow is available from phone.

Security:
- Never commit TITANIA_TOKEN_KEY, Telegram bot token, or raw refresh_token.
- titania_refresh_token.enc is encrypted and safe to commit without its key.
