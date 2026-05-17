import io
import logging
import os
import re
import shutil
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field

import fitz
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

(WAITING_PDF,
 STEP_DATE, STEP_TIME, STEP_AMOUNT,
 STEP_SENDER, STEP_CARD, STEP_RECIPIENT,
 STEP_BANK, STEP_RECEIPT, CONFIRM) = range(10)

_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_REGULAR = os.path.join(_DIR, "DejaVuSans.ttf")
FONT_BOLD    = os.path.join(_DIR, "DejaVuSans-Bold.ttf")


@dataclass
class TextSpan:
    page_num: int
    span_idx: int
    bbox: tuple
    text: str
    font: str
    size: float
    color: int
    flags: int

    def color_rgb(self) -> tuple:
        r = ((self.color >> 16) & 0xFF) / 255.0
        g = ((self.color >>  8) & 0xFF) / 255.0
        b = ( self.color        & 0xFF) / 255.0
        return (r, g, b)


@dataclass
class ReceiptFields:
    datetime_idx:  int | None = None
    itogo_idx:     int | None = None
    summa_idx:     int | None = None
    sender_idx:    int | None = None
    card_idx:      int | None = None
    recipient_idx: int | None = None
    bank_idx:      int | None = None
    receipt_idx:   int | None = None


@dataclass
class ReceiptValues:
    date:        str = ""
    time:        str = ""
    amount:      str = ""
    sender:      str = ""
    card:        str = ""
    recipient:   str = ""
    bank:        str = ""
    receipt_num: str = ""


@dataclass
class UserSession:
    doc_path:  str
    spans:     list
    file_name: str
    fields:    ReceiptFields
    values:    ReceiptValues = field(default_factory=ReceiptValues)


sessions: dict[int, UserSession] = {}


# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_spans(doc_path: str) -> list:
    doc = fitz.open(doc_path)
    spans = []
    idx = 0
    for page_num in range(len(doc)):
        page = doc[page_num]
        textdict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in textdict["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    text = span.get("text", "")
                    if not text:
                        text = "".join(c.get("c", "") for c in span.get("chars", []))
                    if text.strip():
                        spans.append(TextSpan(
                            page_num=page_num,
                            span_idx=idx,
                            bbox=tuple(span["bbox"]),
                            text=text,
                            font=span["font"],
                            size=round(span["size"], 1),
                            color=span["color"],
                            flags=span["flags"],
                        ))
                        idx += 1
    doc.close()
    return spans


def find_right_of_label(spans: list, label_text: str) -> "TextSpan | None":
    label = next((s for s in spans if s.text.strip() == label_text), None)
    if not label:
        return None
    label_mid_y = (label.bbox[1] + label.bbox[3]) / 2
    candidates = [
        s for s in spans
        if s.span_idx != label.span_idx
        and s.bbox[0] > label.bbox[2]
        and abs((s.bbox[1] + s.bbox[3]) / 2 - label_mid_y) < 8
    ]
    return candidates[0] if candidates else None


def detect_receipt_fields(spans: list) -> ReceiptFields:
    f = ReceiptFields()
    for s in spans:
        text = s.text.strip()
        if re.match(r'\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2}', text):
            f.datetime_idx = s.span_idx
        if re.match(r'Квитанция\s+№', text):
            f.receipt_idx = s.span_idx

    for label, attr in [
        ("Итого",            "itogo_idx"),
        ("Сумма",            "summa_idx"),
        ("Отправитель",      "sender_idx"),
        ("Карта получателя", "card_idx"),
        ("Получатель",       "recipient_idx"),
        ("Банк получателя",  "bank_idx"),
    ]:
        span = find_right_of_label(spans, label)
        if span:
            setattr(f, attr, span.span_idx)

    return f


# ── Formatting helpers ────────────────────────────────────────────────────────

def format_card(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 16:
        return f"{digits[:6]}{'*' * 6}{digits[-4:]}"
    return raw


def format_amount(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits:
        return f"{int(digits):,}".replace(",", " ")
    return raw


# ── Build edits dict ──────────────────────────────────────────────────────────

def build_edits(session: UserSession) -> dict:
    v, f, spans = session.values, session.fields, session.spans
    edits: dict[int, str] = {}

    if f.datetime_idx is not None and (v.date or v.time):
        orig = spans[f.datetime_idx].text
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})(\s+)(\d{2}:\d{2}:\d{2})", orig)
        if m:
            new_date = v.date or m.group(1)
            new_time = v.time or m.group(3)
            edits[f.datetime_idx] = f"{new_date}  {new_time}"

    if v.amount:
        amt = format_amount(v.amount)
        if f.itogo_idx is not None:
            edits[f.itogo_idx] = amt
        if f.summa_idx is not None:
            edits[f.summa_idx] = amt

    if v.sender      and f.sender_idx    is not None: edits[f.sender_idx]    = v.sender
    if v.card        and f.card_idx      is not None: edits[f.card_idx]      = format_card(v.card)
    if v.recipient   and f.recipient_idx is not None: edits[f.recipient_idx] = v.recipient
    if v.bank        and f.bank_idx      is not None: edits[f.bank_idx]      = v.bank
    if v.receipt_num and f.receipt_idx   is not None:
        edits[f.receipt_idx] = f"Квитанция  № {v.receipt_num}"

    return edits


# ── PDF save ──────────────────────────────────────────────────────────────────

def apply_edits(src_path: str, spans: list, edits: dict) -> bytes:
    doc = fitz.open(src_path)
    edits_by_page: dict[int, list] = defaultdict(list)
    for idx, new_text in edits.items():
        edits_by_page[spans[idx].page_num].append((spans[idx], new_text))

    for page_num, page_edits in edits_by_page.items():
        page = doc[page_num]
        for span, _ in page_edits:
            page.add_redact_annot(fitz.Rect(span.bbox))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for span, new_text in page_edits:
            font_file = FONT_BOLD if (span.flags & 16) else FONT_REGULAR
            page.insert_text(
                fitz.Point(span.bbox[0], span.bbox[3]),
                new_text,
                fontfile=font_file,
                fontsize=span.size,
                color=span.color_rgb(),
            )

    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()


# ── Wizard messages & keyboards ───────────────────────────────────────────────

def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⏭ Пропустить", callback_data="skip"),
    ]])


def _orig_date(session: UserSession) -> tuple[str, str]:
    if session.fields.datetime_idx is None:
        return "—", "—"
    orig = session.spans[session.fields.datetime_idx].text
    m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})", orig)
    return (m.group(1), m.group(2)) if m else ("—", "—")


def ask_date(session: UserSession) -> str:
    date, _ = _orig_date(session)
    return f"📅 *Дата*\nТекущая: `{date}`\n\nВведите новую дату (ДД.ММ.ГГГГ):"


def ask_time(session: UserSession) -> str:
    _, time = _orig_date(session)
    return f"🕐 *Время*\nТекущее: `{time}`\n\nВведите новое время (ЧЧ:ММ:СС):"


def ask_amount(session: UserSession) -> str:
    f = session.fields
    cur = session.spans[f.summa_idx].text.strip() if f.summa_idx is not None else "—"
    return (
        f"💰 *Сумма*\nТекущая: `{cur}`\n\n"
        "Введите новую сумму (например: `5000`):\n"
        "_Значение «Итого» обновится автоматически_"
    )


def ask_sender(session: UserSession) -> str:
    f = session.fields
    cur = session.spans[f.sender_idx].text.strip() if f.sender_idx is not None else "—"
    return f"👤 *Отправитель*\nТекущий: `{cur}`\n\nВведите имя отправителя:"


def ask_card(session: UserSession) -> str:
    f = session.fields
    cur = session.spans[f.card_idx].text.strip() if f.card_idx is not None else "—"
    return (
        f"💳 *Карта получателя*\nТекущая: `{cur}`\n\n"
        "Введите полный номер карты (16 цифр):\n"
        "_Будет отформатировано: XXXXXX\\*\\*\\*\\*\\*\\*XXXX_"
    )


def ask_recipient(session: UserSession) -> str:
    f = session.fields
    cur = session.spans[f.recipient_idx].text.strip() if f.recipient_idx is not None else "—"
    return f"👤 *Получатель*\nТекущий: `{cur}`\n\nВведите имя получателя (например: `Иван И.`):"


def ask_bank(session: UserSession) -> str:
    f = session.fields
    cur = session.spans[f.bank_idx].text.strip() if f.bank_idx is not None else "—"
    return f"🏦 *Банк получателя*\nТекущий: `{cur}`\n\nВведите название банка:"


def ask_receipt(session: UserSession) -> str:
    f = session.fields
    if f.receipt_idx is not None:
        m = re.search(r"№\s*(.+)", session.spans[f.receipt_idx].text)
        cur = m.group(1).strip() if m else "—"
    else:
        cur = "—"
    return f"🧾 *Номер квитанции*\nТекущий: `{cur}`\n\nВведите новый номер:"


def confirm_text(session: UserSession) -> str:
    v, f, spans = session.values, session.fields, session.spans
    lines = ["📋 *Проверьте изменения перед сохранением:*\n"]

    if f.datetime_idx is not None:
        orig = spans[f.datetime_idx].text
        m = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})", orig)
        if m:
            lines.append(f"📅 Дата:  `{v.date or m.group(1)}`")
            lines.append(f"🕐 Время: `{v.time or m.group(2)}`")

    if v.amount:
        lines.append(f"💰 Сумма / Итого: `{format_amount(v.amount)}`")
    if v.sender:
        lines.append(f"👤 Отправитель: `{v.sender}`")
    if v.card:
        lines.append(f"💳 Карта: `{format_card(v.card)}`")
    if v.recipient:
        lines.append(f"👤 Получатель: `{v.recipient}`")
    if v.bank:
        lines.append(f"🏦 Банк: `{v.bank}`")
    if v.receipt_num:
        lines.append(f"🧾 Квитанция №: `{v.receipt_num}`")

    if len(lines) == 1:
        lines.append("_Нет изменений_")

    return "\n".join(lines)


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Сохранить PDF", callback_data="confirm_save"),
        InlineKeyboardButton("🔄 Заново",        callback_data="confirm_restart"),
    ]])


# ── Handlers ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Привет! Бот для редактирования квитанций PDF.\n\n"
        "Отправьте PDF-файл и я проведу по всем полям.\n"
        "📎 Отправьте PDF для начала."
    )
    return WAITING_PDF


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc or doc.mime_type != "application/pdf":
        await update.message.reply_text("Пожалуйста, отправьте файл в формате PDF.")
        return WAITING_PDF

    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⏳ Обрабатываю PDF...")

    tmp_dir = tempfile.mkdtemp()
    doc_path = os.path.join(tmp_dir, doc.file_name or "document.pdf")
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(doc_path)

    try:
        spans = extract_spans(doc_path)
    except Exception as e:
        logger.error("extract_spans: %s", e)
        await msg.edit_text("❌ Не удалось прочитать PDF.")
        return WAITING_PDF

    fields = detect_receipt_fields(spans)
    sessions[chat_id] = UserSession(
        doc_path=doc_path, spans=spans,
        file_name=doc.file_name or "document.pdf",
        fields=fields,
    )
    session = sessions[chat_id]

    await msg.edit_text("✅ Файл загружен. Отвечайте на вопросы или нажимайте «Пропустить».")
    await update.message.reply_text(
        ask_date(session), parse_mode="Markdown", reply_markup=skip_kb()
    )
    return STEP_DATE


# Generic helper: save value → ask next question
async def _next(update, session, value_setter, next_ask_fn, next_state):
    if update.callback_query:
        await update.callback_query.answer()
        reply = update.callback_query.message.reply_text
    else:
        value_setter(update.message.text.strip())
        reply = update.message.reply_text

    await reply(next_ask_fn(session), parse_mode="Markdown", reply_markup=skip_kb())
    return next_state


async def step_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "date", t),
        ask_time, STEP_TIME,
    )


async def step_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "time", t),
        ask_amount, STEP_AMOUNT,
    )


async def step_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "amount", t),
        ask_sender, STEP_SENDER,
    )


async def step_sender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "sender", t),
        ask_card, STEP_CARD,
    )


async def step_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "card", t),
        ask_recipient, STEP_RECIPIENT,
    )


async def step_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "recipient", t),
        ask_bank, STEP_BANK,
    )


async def step_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF
    return await _next(
        update, session,
        lambda t: setattr(session.values, "bank", t),
        ask_receipt, STEP_RECEIPT,
    )


async def step_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        return WAITING_PDF

    if update.callback_query:
        await update.callback_query.answer()
        reply = update.callback_query.message.reply_text
    else:
        session.values.receipt_num = update.message.text.strip()
        reply = update.message.reply_text

    await reply(confirm_text(session), parse_mode="Markdown", reply_markup=confirm_kb())
    return CONFIRM


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    await query.edit_message_text("⏳ Применяю изменения...")
    edits = build_edits(session)

    if not edits:
        await query.edit_message_text("Нет изменений. Отправьте PDF заново.")
        return WAITING_PDF

    try:
        pdf_bytes = apply_edits(session.doc_path, session.spans, edits)
    except Exception as e:
        logger.error("apply_edits: %s", e)
        await query.edit_message_text("❌ Ошибка при сохранении PDF.")
        return WAITING_PDF

    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(pdf_bytes),
        filename="edited_" + session.file_name,
        caption=f"✅ Готово! Изменено полей: {len(edits)}.",
    )
    shutil.rmtree(os.path.dirname(session.doc_path), ignore_errors=True)
    sessions.pop(chat_id, None)
    await context.bot.send_message(chat_id=chat_id, text="Отправьте новый PDF для редактирования.")
    return WAITING_PDF


async def confirm_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела.")
        return WAITING_PDF

    session.values = ReceiptValues()
    await query.edit_message_text("🔄 Начинаем сначала.")
    await query.message.reply_text(ask_date(session), parse_mode="Markdown", reply_markup=skip_kb())
    return STEP_DATE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.pop(chat_id, None)
    if session:
        shutil.rmtree(os.path.dirname(session.doc_path), ignore_errors=True)
    await update.message.reply_text("Сессия завершена. Отправьте новый PDF чтобы начать снова.")
    return WAITING_PDF


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "8939898526:AAEkVBfxLt6fEBgW4Ub98OYo_tBN7F00DKY")
    app = Application.builder().token(token).build()

    ANY_TEXT = filters.TEXT & ~filters.COMMAND
    SKIP_CB  = CallbackQueryHandler(lambda u, c: None, pattern="^skip$")  # placeholder

    def text_or_skip(handler):
        return [
            MessageHandler(ANY_TEXT, handler),
            CallbackQueryHandler(handler, pattern="^skip$"),
        ]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
        ],
        states={
            WAITING_PDF:    [MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf)],
            STEP_DATE:      text_or_skip(step_date),
            STEP_TIME:      text_or_skip(step_time),
            STEP_AMOUNT:    text_or_skip(step_amount),
            STEP_SENDER:    text_or_skip(step_sender),
            STEP_CARD:      text_or_skip(step_card),
            STEP_RECIPIENT: text_or_skip(step_recipient),
            STEP_BANK:      text_or_skip(step_bank),
            STEP_RECEIPT:   text_or_skip(step_receipt),
            CONFIRM: [
                CallbackQueryHandler(confirm_save,    pattern="^confirm_save$"),
                CallbackQueryHandler(confirm_restart, pattern="^confirm_restart$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
