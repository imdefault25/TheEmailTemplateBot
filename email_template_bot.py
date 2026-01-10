import os
import json
import datetime
import contextlib
import html
from typing import Dict, Any, List

from jinja2 import Template
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

# =========================
#  CONFIG
# =========================
# MUST be set as an environment variable in Railway:
# TELEGRAM_BOT_TOKEN = 123456:AA....
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("ERROR: TELEGRAM_BOT_TOKEN environment variable not set")

TEMPLATES_FILE = "templates_store.json"
USER_SETTINGS_FILE = "user_settings.json"  # per-user saved names, counters, auth
ACCESS_PASSWORD = "2468"

# extra gate for the Ledger Live (Private) template
LEDGER_PRIVATE_CODE = "1083"
LEDGER_PRIVATE_KEY = "Ledger Live (Private)"

# Map internal template keys -> display labels
DISPLAY_LABELS = {
    "Binance": "Binance",
    "Newcastle AUS": "Newcastle 🇦🇺",
    "Ledger Live (Private)": "Ledger Live (Private)",
}
REVERSE_LABELS = {v: k for k, v in DISPLAY_LABELS.items()}

# =========================
#  PERSISTENCE (user settings)
# =========================
def load_user_settings() -> Dict[str, Any]:
    if not os.path.exists(USER_SETTINGS_FILE):
        return {}
    try:
        with open(USER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_user_settings(data: Dict[str, Any]) -> None:
    tmp = USER_SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, USER_SETTINGS_FILE)

user_settings = load_user_settings()

def get_entry(user_id: int) -> Dict[str, Any]:
    return user_settings.setdefault(str(user_id), {})

def is_authorized(user_id: int) -> bool:
    return bool(get_entry(user_id).get("authorized", False))

def set_authorized(user_id: int, value: bool) -> None:
    get_entry(user_id)["authorized"] = bool(value)
    save_user_settings(user_settings)

def is_ledger_unlocked(user_id: int) -> bool:
    return bool(get_entry(user_id).get("ledger_private_unlocked", False))

def set_ledger_unlocked(user_id: int, value: bool) -> None:
    get_entry(user_id)["ledger_private_unlocked"] = bool(value)
    save_user_settings(user_settings)

def get_rep_names(user_id: int) -> List[str]:
    return get_entry(user_id).get("rep_names", [])

def set_rep_names(user_id: int, names: List[str]) -> None:
    get_entry(user_id)["rep_names"] = names
    save_user_settings(user_settings)

def inc_generated(user_id: int) -> int:
    entry = get_entry(user_id)
    entry["generated_count"] = int(entry.get("generated_count", 0)) + 1
    save_user_settings(user_settings)
    return entry["generated_count"]

def get_generated(user_id: int) -> int:
    return int(get_entry(user_id).get("generated_count", 0))

# =========================
#  LOAD TEMPLATES
# =========================
if not os.path.exists(TEMPLATES_FILE):
    raise SystemExit(f"ERROR: '{TEMPLATES_FILE}' not found in the folder.")

with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
    templates = json.load(f)

# =========================
#  SESSIONS (per-chat letter building)
# =========================
sessions: Dict[int, Dict[str, Any]] = {}

def norm(label: str) -> str:
    return label.strip().replace(" ", "_")

def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)

# =========================
#  UI / TEXT HELPERS (HTML)
# =========================
def how_to_html() -> str:
    return (
        "<b>How to use</b>\n"
        "———————————————\n"
        "1️⃣ Select a template from <b>🗂️ Choose Template</b>\n"
        "2️⃣ Fill in the required fields\n"
        "3️⃣ Pick the Representative / Support specialist from your saved names or choose <b>Custom…</b>\n"
        "4️⃣ Review the summary and tap <b>Yes</b> to confirm (or <b>No</b> to edit)\n"
        "5️⃣ You’ll receive the completed <b>.html</b> file\n"
        "6️⃣ Use <b>⚙️ Settings</b> to add/remove your representative names anytime\n"
    )

def dashboard_html(first_name: str, user_id: int) -> str:
    count = get_generated(user_id)
    return (
        f"<b>Welcome, {esc(first_name)}!</b>\n\n"
        f"Templates generated: <b>{count}</b>\n\n"
        f"{how_to_html()}"
    )

def gate_html() -> str:
    return (
        "<b>This bot is locked.</b>\n"
        "Please enter the access password to continue.\n\n"
        "Type the password:"
    )

def ledger_gate_html() -> str:
    return (
        "<b>Locked template</b>\n"
        "This template requires an extra access code.\n\n"
        "Type the code:"
    )

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗂️ Choose Template", callback_data="menu:create")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu:settings")],
        [InlineKeyboardButton("📖 How to Use", callback_data="menu:help")],
    ])

def return_to_dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Return to Dashboard", callback_data="menu:home")]])

def templates_kb() -> InlineKeyboardMarkup:
    # split into rows of 2 so it doesn’t overflow
    keys = list(templates.keys())
    buttons = [InlineKeyboardButton(DISPLAY_LABELS.get(k, k), callback_data=f"tpl:{k}") for k in keys]
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i+2])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)

def yes_no_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes", callback_data="conf:yes"),
         InlineKeyboardButton("❌ No", callback_data="conf:no")]
    ])

def reps_kb(user_id: int, include_custom: bool = True) -> InlineKeyboardMarkup:
    names = get_rep_names(user_id)
    buttons = [InlineKeyboardButton(n, callback_data=f"rep:{n}") for n in names]
    rows: List[List[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i:i+2])
    if include_custom or not rows:
        rows.append([InlineKeyboardButton("✍️ Custom…", callback_data="rep:CUSTOM")])
    return InlineKeyboardMarkup(rows)

def settings_kb(user_id: int) -> InlineKeyboardMarkup:
    names = get_rep_names(user_id)
    rows: List[List[InlineKeyboardButton]] = []
    if names:
        for i, n in enumerate(names):
            rows.append([InlineKeyboardButton(f"❌ Remove: {n}", callback_data=f"settings:del:{i}")])
    rows.append([InlineKeyboardButton("➕ Add name", callback_data="settings:add")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)

def field_choices_kb(fields_order: List[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(lbl, callback_data=f"edit:{lbl}")] for lbl in fields_order]
    rows.append([InlineKeyboardButton("⬅️ Cancel", callback_data="edit:cancel")])
    return InlineKeyboardMarkup(rows)

# =========================
#  AUTH GUARD
# =========================
def ensure_auth_session(chat_id: int) -> None:
    s = sessions.setdefault(chat_id, {})
    if "mode" not in s:
        s["mode"] = None

async def require_auth_or_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if is_authorized(user_id):
        return True
    ensure_auth_session(chat_id)
    sessions[chat_id]["mode"] = "await_password"
    if update.callback_query:
        with contextlib.suppress(BadRequest):
            await update.callback_query.edit_message_text(
                gate_html(),
                disable_web_page_preview=True,
                parse_mode=ParseMode.HTML,
            )
    else:
        await update.message.reply_text(gate_html(), disable_web_page_preview=True, parse_mode=ParseMode.HTML)
    return False

# =========================
#  COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first = update.effective_user.first_name or "there"

    if not is_authorized(user_id):
        ensure_auth_session(update.effective_chat.id)
        sessions[update.effective_chat.id]["mode"] = "await_password"
        if update.message:
            await update.message.reply_text(gate_html(), disable_web_page_preview=True, parse_mode=ParseMode.HTML)
        else:
            with contextlib.suppress(BadRequest):
                await update.callback_query.edit_message_text(
                    gate_html(),
                    disable_web_page_preview=True,
                    parse_mode=ParseMode.HTML
                )
        return

    text = dashboard_html(first, user_id)
    if update.message:
        await update.message.reply_text(
            text,
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    else:
        with contextlib.suppress(BadRequest):
            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_menu_kb(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

# =========================
#  CALLBACKS (inline buttons)
# =========================
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    user_id = q.from_user.id
    data = q.data

    if not is_authorized(user_id):
        await require_auth_or_prompt(update, context)
        return

    if data in ("menu:home", "menu:back"):
        first = q.from_user.first_name or "there"
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                dashboard_html(first, user_id),
                reply_markup=main_menu_kb(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        return

    if data == "menu:create":
        with contextlib.suppress(BadRequest):
            await q.edit_message_text("Pick a template:", reply_markup=templates_kb())
        return

    if data == "menu:settings":
        first = q.from_user.first_name or ""
        head = f"Settings • Manage your representative names, {esc(first)}."
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                head + "\nTap a row to remove, or add a new one.",
                reply_markup=settings_kb(user_id),
                parse_mode=ParseMode.HTML,
            )
        return

    if data == "menu:help":
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                how_to_html(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:back")]]),
            )
        return

    if data == "settings:add":
        sessions[chat_id] = {"mode": "await_add_name"}
        with contextlib.suppress(BadRequest):
            await q.edit_message_text("Type the representative name to add (or /cancel):")
        return

    if data.startswith("settings:del:"):
        idx = int(data.split(":")[-1])
        names = get_rep_names(user_id)
        if 0 <= idx < len(names):
            names.pop(idx)
            set_rep_names(user_id, names)
        with contextlib.suppress(BadRequest):
            await q.edit_message_text(
                "Updated.\n\nSettings • Manage your representative names.",
                reply_markup=settings_kb(user_id)
            )
        return

    # Template selection
    if data.startswith("tpl:"):
        tpl_key = data.split(":", 1)[1]

        # extra gate for Ledger Live (Private)
        if tpl_key == LEDGER_PRIVATE_KEY and not is_ledger_unlocked(user_id):
            sessions[chat_id] = {
                "mode": "await_ledger_code",
                "pending_tpl_key": tpl_key,
            }
            with contextlib.suppress(BadRequest):
                await q.edit_message_text(ledger_gate_html(), parse_mode=ParseMode.HTML)
            return

        await start_template_session(chat_id, tpl_key, update, context, edit=True)
        return

    # Rep selection
    if data.startswith("rep:"):
        s = sessions.get(chat_id)
        if not s:
            return
        label = s["fields_order"][s["idx"]]
        tag = data.split(":", 1)[1]
        if tag == "CUSTOM":
            s["awaiting_custom_for"] = label
            with contextlib.suppress(BadRequest):
                await q.edit_message_text(f"Type the {esc(label)} name:", parse_mode=ParseMode.HTML)
            return
        s["values"][norm(label)] = tag
        s["idx"] += 1
        await ask_next(update, context, edit=True)
        return

    # Confirmation
    if data == "conf:yes":
        await render_and_send(update, context)
        sessions.pop(chat_id, None)
        return

    if data == "conf:no":
        s = sessions.get(chat_id)
        if not s:
            return
        s["stage"] = "edit_select"
        with contextlib.suppress(BadRequest):
            await q.edit_message_text("Select a field to edit:", reply_markup=field_choices_kb(s["fields_order"]))
        return

    if data.startswith("edit:"):
        choice = data.split(":", 1)[1]
        s = sessions.get(chat_id)
        if not s:
            return
        if choice == "cancel":
            await show_confirmation(update, context, edit=True)
            return
        s["idx"] = s["fields_order"].index(choice)
        s["stage"] = "collect"
        await ask_next(update, context, edit=True)
        return

# =========================
#  MESSAGE HANDLER (free text)
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    s = sessions.get(chat_id)

    # Global lock
    if not is_authorized(user_id):
        if not (s and s.get("mode") == "await_password"):
            ensure_auth_session(chat_id)
            sessions[chat_id]["mode"] = "await_password"
            await update.message.reply_text(gate_html(), disable_web_page_preview=True, parse_mode=ParseMode.HTML)
            return

        if text == ACCESS_PASSWORD:
            set_authorized(user_id, True)
            sessions.pop(chat_id, None)
            first = update.effective_user.first_name or "there"
            await update.message.reply_text(
                dashboard_html(first, user_id),
                reply_markup=main_menu_kb(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

        await update.message.reply_text("Incorrect password ❌. Try again:")
        return

    # Ledger private code gate
    if s and s.get("mode") == "await_ledger_code":
        if text == LEDGER_PRIVATE_CODE:
            set_ledger_unlocked(user_id, True)
            pending = s.get("pending_tpl_key", LEDGER_PRIVATE_KEY)
            # start the template immediately
            sessions.pop(chat_id, None)
            await update.message.reply_text("Unlocked ✅")
            await start_template_session(chat_id, pending, update, context, edit=False)
        else:
            await update.message.reply_text("Incorrect code ❌. Try again:")
        return

    # /cancel
    if text.lower() == "/cancel":
        with contextlib.suppress(KeyError):
            sessions.pop(chat_id)
        first = update.effective_user.first_name or "there"
        await update.message.reply_text(
            dashboard_html(first, user_id),
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        return

    # settings add-name mode
    if s and s.get("mode") == "await_add_name":
        name = text
        names = get_rep_names(user_id)
        if name and name not in names:
            names.append(name)
            set_rep_names(user_id, names)
        sessions.pop(chat_id, None)
        await update.message.reply_text(
            "Saved ✅\n\nSettings • Manage your representative names.",
            reply_markup=settings_kb(user_id)
        )
        return

    # template run
    if s and s.get("stage") in ("collect", "edit_select", "confirm"):
        labels = s["fields_order"]

        awaiting = s.get("awaiting_custom_for")
        if awaiting:
            s["values"][norm(awaiting)] = text
            s["idx"] += 1
            s["awaiting_custom_for"] = None
            await ask_next(update, context)
            return

        i = s["idx"]
        if i < len(labels):
            label = labels[i]
            s["values"][norm(label)] = text
            s["idx"] += 1
            await ask_next(update, context)
            return

    # default
    await start(update, context)

# =========================
#  HELPERS
# =========================
async def start_template_session(chat_id: int, tpl_key: str, update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool):
    tpl = templates.get(tpl_key)
    if not tpl:
        if update.message:
            await update.message.reply_text("Template not found.")
        else:
            with contextlib.suppress(BadRequest):
                await update.callback_query.edit_message_text("Template not found.")
        return

    sessions[chat_id] = {
        "tpl_key": tpl_key,
        "fields_order": tpl.get("fields_order", []),
        "values": {},
        "idx": 0,
        "stage": "collect",
    }
    await ask_next(update, context, edit=edit)

# =========================
#  STEPS
# =========================
async def ask_next(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    s = sessions.get(chat_id)
    if not s:
        return

    labels = s["fields_order"]
    while s["idx"] < len(labels):
        label = labels[s["idx"]]

        if label.lower() in ("representative", "support specialist"):
            kb = reps_kb(user_id, include_custom=True)
            txt = f"Choose {esc(label)}:"
            if edit and update.callback_query:
                with contextlib.suppress(BadRequest):
                    await update.callback_query.edit_message_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(txt, reply_markup=kb, parse_mode=ParseMode.HTML)
            return

        txt = f"Enter value for '{esc(label)}':"
        if edit and update.callback_query:
            with contextlib.suppress(BadRequest):
                await update.callback_query.edit_message_text(txt, parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(txt, parse_mode=ParseMode.HTML)
        return

    await show_confirmation(update, context, edit=edit)

async def show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False):
    s = sessions.get(update.effective_chat.id)
    if not s:
        return
    s["stage"] = "confirm"
    lines = [f"{esc(lbl)}: {esc(s['values'].get(norm(lbl), '(missing)'))}" for lbl in s["fields_order"]]
    text = "Please confirm the details:\n\n" + "\n".join(lines)
    kb = yes_no_kb()
    if edit and update.callback_query:
        with contextlib.suppress(BadRequest):
            await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

async def render_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user
    s = sessions.get(chat_id)
    if not s:
        return

    tpl = templates[s["tpl_key"]]
    ctx = dict(s["values"])

    auto = tpl.get("auto_fields") or {}
    for k, v in auto.items():
        if v == "DATE":
            ctx[k] = datetime.datetime.now().strftime("%d %B %Y")
        else:
            ctx.setdefault(k, v)

    html_out = Template(tpl["template"]).render(**ctx)
    filename = f"{s['tpl_key'].replace(' ', '_')}.html"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_out)

    with open(filename, "rb") as f:
        await context.bot.send_document(chat_id=chat_id, document=InputFile(f, filename=filename))

    total = inc_generated(user.id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Done ✅\nGenerated this session. Total generated: {total}",
        reply_markup=return_to_dashboard_kb(),
    )

# =========================
#  ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        import traceback
        print("Exception while handling update:", traceback.format_exc())
    except Exception:
        pass

# =========================
#  BOOT
# =========================
def main():
    request = HTTPXRequest(read_timeout=20.0, connect_timeout=20.0)
    app = ApplicationBuilder().token(BOT_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))
    app.add_error_handler(error_handler)

    print("Bot is running…")
    app.run_polling()

if __name__ == "__main__":
    main()
