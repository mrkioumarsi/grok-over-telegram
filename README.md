# Grok Over Telegram

Grok Over Telegram connects Grok Builder to a Telegram bot. You chat in Telegram. Grok runs on your own Ubuntu server.

Closing Telegram, or a dropped chat, does not stop the answer. The job keeps running on the server and the reply arrives when the bot can reach you again. Tap Stop when you want it to stop.

People you have not allowed get no reply. You, the admin, can use the whole server. Everyone else is locked inside a private workspace and cannot change the system, the kernel, or local services.

## Install

You need Ubuntu 22.04 or newer, root access, the Grok CLI installed on that server, and a Telegram bot token from BotFather. One line:

```bash
curl -fsSL https://raw.githubusercontent.com/mrkioumarsi/grok-over-telegram/main/install.sh | sudo bash
```

Then open the menu and sign in to Grok:

```bash
sudo grokwire
```

The installer asks for the bot token and your numeric Telegram id. That id is the admin. To skip the questions, set `BOT_TOKEN` and `ADMIN_ID` before you run the installer.

If an older `/opt/grokbot` install is on the same machine, the installer copies its settings and stops that service so only one bot polls Telegram.

## Menu

`sudo grokwire` opens the menu. From there you can:

- Start, stop, and restart the bot
- Show status and logs
- Check the install
- Sign in to Grok
- Set the bot token
- Set the admin Telegram id
- Stop the answer that is being written
- Refresh the sandbox firewall
- Uninstall, keep the workspaces, remove the program data, or delete `/var/www/workspaces`

## Use the bot

Open the bot in Telegram and type. The answer continues the open chat, and the buttons stay under it.

- **New chat** starts a fresh chat and keeps the old ones.
- **Chats** lists earlier chats so you can resume or delete one.
- **Status** shows your model, messages, and usage.
- **Stop** stops the answer that is being written. Closing the Telegram app does not.
- **Model** appears when you have more than one model.
- **Users** is the admin list. From there you allow a person, choose models, and set how many messages and how much usage they get. Zero means unlimited.

Send a file, photo, video, or voice note. Add a caption if you want Grok to work on it. With no caption, the file is only saved. Telegram lets the bot fetch files up to about 20 MB.

Find your id with `/id`. Useful admin commands: `/allow`, `/disable`, `/enable`, `/remove`, and `/cwd` for your own working folder.

## Workspaces and safety

Each person has a folder at `/var/www/workspaces/<telegram id>`. Do not point Nginx at that directory.

- Unknown Telegram users are ignored. The bot does not answer them.
- The admin runs Grok as root.
- Every other person runs as their own Unix user inside bubblewrap. They can write only in their workspace. System folders are read-only, root is hidden, and that user cannot open local services such as SSH or panels on the same machine.

## Uninstall

Open the menu with `sudo grokwire` and choose Uninstall. Workspaces stay unless you choose to delete them.

## License

MIT. See [LICENSE](LICENSE).

## Donate

https://mrkioumarsi.ir/donate.php?lang=en
