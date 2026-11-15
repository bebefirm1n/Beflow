from beflow.core import TOKEN, bot, KEEP_ALIVE_AVAILABLE


def main():
    if not TOKEN:
        raise SystemExit("TOKEN_BOT manquant.")

    if KEEP_ALIVE_AVAILABLE:
        from keep_alive import keep_alive
        keep_alive()

    bot.run(TOKEN, reconnect=True)


if __name__ == "__main__":
    main()
