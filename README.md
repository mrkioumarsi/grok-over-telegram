# GrokWire

GrokWire is a Telegram bot for Grok on Ubuntu. You chat from Telegram. Grok runs on your own server.

People you have not allowed get no reply. You, the admin, can use the whole server. Everyone else is locked inside a private workspace and cannot change the system, the kernel, or local services.

## Install

Ubuntu 22.04 or newer, with the Grok CLI already installed:

```bash
git clone https://github.com/mrkioumarsi/grokwire.git
cd grokwire
sudo bash install.sh
sudo grokwire login
sudo grokwire doctor
```

The installer asks for the Telegram bot token from BotFather and your numeric Telegram id. That id is the admin. To skip the questions, set `BOT_TOKEN` and `ADMIN_ID` before you run `install.sh`.

If an older `/opt/grokbot` install is on the same machine, the installer copies its settings and stops that service so only one bot polls Telegram.

## Commands

| Command | What it does |
| --- | --- |
| `sudo grokwire start` | Start the bot and enable it on boot |
| `sudo grokwire stop` | Stop the bot |
| `sudo grokwire restart` | Restart the bot |
| `sudo grokwire status` | Show the service |
| `sudo grokwire logs` | Show recent logs |
| `sudo grokwire doctor` | Check Ubuntu, Grok, the sandbox, and the firewall |
| `sudo grokwire login` | Sign in to Grok with a device code |
| `sudo grokwire grok-stop` | Stop the answer that is being written |
| `sudo grokwire firewall` | Refresh the sandbox network rules |
| `sudo grokwire uninstall` | Remove the service and keep workspaces |
| `sudo grokwire uninstall --purge` | Also remove program data and sandbox homes |
| `sudo grokwire uninstall --purge-workspaces` | Also delete `/var/www/workspaces` |

## Use the bot

Open the bot in Telegram and type. The answer continues the open chat, and the buttons stay under it.

- **New chat** starts a fresh chat and keeps the old ones.
- **Chats** lists earlier chats so you can resume or delete one.
- **Status** shows your model, messages, and usage.
- **Stop** stops the answer that is being written.
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

```bash
sudo grokwire uninstall
```

## Support

BNB Smart Chain, BEP20:

0xAec9F6Ad80940a50e35F453d3354CA8379d0CFaD
