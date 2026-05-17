import io
import logging
import os
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

WAITING_PDF, VIEWING_SPANS, EDITING_SPAN = range(3)
SPANS_PER_PAGE = 8

BASE14_MAP = {
    (False, False): "helv",
    (True, False): "hebo",
    (False, True): "heit",
    (True, True): "hebi",
}


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
        g = ((self.color >> 8) & 0xFF) / 255.0
        b = (self.color & 0xFF) / 255.0
        return (r, g, b)

    def font_display(self) -> str:
        name = self.font.split("+", 1)[-1] if "+" in self.font else self.font
        return name[:30]

    def flags_display(self) -> str:
        parts = []
        if self.flags & 16:
            parts.append("жирный")
        if self.flags & 2:
            parts.append("курсив")
        return ", ".join(parts) if parts else "обычный"


@dataclass
class UserSession:
    doc_path: str
    spans: list
    file_name: str
    current_page: int = 0
    selected_span_idx: int | None = None
    edits: dict = field(default_factory=dict)

    def total_pages(self) -> int:
        return max(1, (len(self.spans) + SPANS_PER_PAGE - 1) // SPANS_PER_PAGE)

    def page_spans(self) -> list:
        start = self.current_page * SPANS_PER_PAGE
        return self.spans[start: start + SPANS_PER_PAGE]

    def span_display_text(self, span: TextSpan) -> str:
        text = span.text.replace("\n", " ").strip()
        return text[:50] + "…" if len(text) > 50 else text


sessions: dict[int, UserSession] = {}


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
                        # PyMuPDF 1.24+: assemble text from chars if key missing
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


def resolve_fontname(span: TextSpan, page: fitz.Page) -> str:
    for name in [span.font, span.font.split("+", 1)[-1]]:
        try:
            rc = page.insert_textbox(fitz.Rect(0, 0, 1, 1), "", fontname=name)
            if rc >= 0:
                return name
        except Exception:
            pass
    bold = bool(span.flags & 16)
    italic = bool(span.flags & 2)
    return BASE14_MAP[(bold, italic)]


def apply_edits(src_path: str, spans: list, edits: dict) -> bytes:
    doc = fitz.open(src_path)
    edits_by_page = defaultdict(list)
    for idx, new_text in edits.items():
        edits_by_page[spans[idx].page_num].append((spans[idx], new_text))

    for page_num, page_edits in edits_by_page.items():
        page = doc[page_num]
        for span, _ in page_edits:
            page.add_redact_annot(fitz.Rect(span.bbox))
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        for span, new_text in page_edits:
            font_name = resolve_fontname(span, page)
            rc = page.insert_textbox(
                fitz.Rect(span.bbox),
                new_text,
                fontname=font_name,
                fontsize=span.size,
                color=span.color_rgb(),
                align=fitz.TEXT_ALIGN_LEFT,
            )
            if rc < 0:
                logger.warning("Text clipped on page %d, span %d", page_num, span.span_idx)

    buf = io.BytesIO()
    doc.save(buf, garbage=4, deflate=True)
    doc.close()
    return buf.getvalue()


def build_spans_keyboard(session: UserSession) -> InlineKeyboardMarkup:
    keyboard = []
    page_spans = session.page_spans()
    start_idx = session.current_page * SPANS_PER_PAGE

    row = []
    for i, span in enumerate(page_spans):
        global_idx = start_idx + i
        marker = "✏️" if global_idx not in session.edits else "✅"
        row.append(InlineKeyboardButton(
            f"{marker}{global_idx + 1}",
            callback_data=f"edit_{global_idx}",
        ))
        if len(row) == 4:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    nav_row = []
    if session.current_page > 0:
        nav_row.append(InlineKeyboardButton("◀ Назад", callback_data="page_prev"))
    if session.current_page < session.total_pages() - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶", callback_data="page_next"))
    if nav_row:
        keyboard.append(nav_row)

    edits_count = len(session.edits)
    save_label = f"💾 Скачать PDF" + (f" ({edits_count} изм.)" if edits_count else "")
    keyboard.append([InlineKeyboardButton(save_label, callback_data="save_pdf")])

    return InlineKeyboardMarkup(keyboard)


def build_spans_text(session: UserSession) -> str:
    page_spans = session.page_spans()
    start_idx = session.current_page * SPANS_PER_PAGE
    lines = [
        f"📄 *{session.file_name}*",
        f"Блоков: {len(session.spans)} | Страница {session.current_page + 1}/{session.total_pages()}",
        "",
    ]
    for i, span in enumerate(page_spans):
        global_idx = start_idx + i
        text = session.span_display_text(span)
        if global_idx in session.edits:
            new_text = session.edits[global_idx][:40]
            line = f"✅ *{global_idx + 1}.* ~~{text}~~ → {new_text}"
        else:
            line = f"✏️ *{global_idx + 1}.* {text}"
        lines.append(line)

    lines.append("")
    lines.append("Нажмите на номер блока чтобы редактировать.")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "👋 Привет! Я бот для редактирования текста в PDF.\n\n"
        "Отправьте мне PDF-файл, и я покажу все текстовые блоки.\n"
        "Вы сможете отредактировать нужные блоки, сохранив шрифты и форматирование.\n\n"
        "📎 Отправьте PDF для начала работы."
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
        logger.error("Failed to extract spans: %s", e)
        await msg.edit_text("❌ Не удалось прочитать PDF. Убедитесь, что файл не зашифрован.")
        return WAITING_PDF

    if not spans:
        await msg.edit_text("❌ В этом PDF не найдено редактируемых текстовых блоков.")
        return WAITING_PDF

    sessions[chat_id] = UserSession(
        doc_path=doc_path,
        spans=spans,
        file_name=doc.file_name or "document.pdf",
    )
    session = sessions[chat_id]

    await msg.edit_text(
        build_spans_text(session),
        reply_markup=build_spans_keyboard(session),
        parse_mode="Markdown",
    )
    return VIEWING_SPANS


async def page_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    if query.data == "page_prev" and session.current_page > 0:
        session.current_page -= 1
    elif query.data == "page_next" and session.current_page < session.total_pages() - 1:
        session.current_page += 1

    await query.edit_message_text(
        build_spans_text(session),
        reply_markup=build_spans_keyboard(session),
        parse_mode="Markdown",
    )
    return VIEWING_SPANS


async def select_span(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    span_idx = int(query.data.split("_")[1])
    span = session.spans[span_idx]
    session.selected_span_idx = span_idx

    current_text = session.edits.get(span_idx, span.text)

    text = (
        f"📝 *Блок #{span_idx + 1}* (стр. {span.page_num + 1})\n"
        f"Шрифт: `{span.font_display()}`, {span.size}pt, {span.flags_display()}\n\n"
        f"Текущий текст:\n`{current_text}`\n\n"
        f"Отправьте новый текст для этого блока:"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отмена", callback_data="cancel_edit"),
    ]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
    return EDITING_SPAN


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    await query.edit_message_text(
        build_spans_text(session),
        reply_markup=build_spans_keyboard(session),
        parse_mode="Markdown",
    )
    return VIEWING_SPANS


async def receive_new_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session or session.selected_span_idx is None:
        await update.message.reply_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    new_text = update.message.text
    span_idx = session.selected_span_idx
    session.edits[span_idx] = new_text
    session.selected_span_idx = None

    await update.message.reply_text(
        f"✅ Блок #{span_idx + 1} будет заменён.\n"
        f"Всего изменений: {len(session.edits)}",
    )
    await update.message.reply_text(
        build_spans_text(session),
        reply_markup=build_spans_keyboard(session),
        parse_mode="Markdown",
    )
    return VIEWING_SPANS


async def save_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    session = sessions.get(chat_id)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправьте PDF заново.")
        return WAITING_PDF

    if not session.edits:
        await query.answer("Нет изменений для сохранения.", show_alert=True)
        return VIEWING_SPANS

    await query.edit_message_text("⏳ Применяю изменения и формирую PDF...")

    try:
        pdf_bytes = apply_edits(session.doc_path, session.spans, session.edits)
    except Exception as e:
        logger.error("Failed to apply edits: %s", e)
        await query.edit_message_text("❌ Ошибка при сохранении PDF.")
        return VIEWING_SPANS

    out_name = "edited_" + session.file_name
    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(pdf_bytes),
        filename=out_name,
        caption=f"✅ PDF с {len(session.edits)} изменением(-ями) готов.",
    )

    session.edits.clear()
    await context.bot.send_message(
        chat_id=chat_id,
        text=build_spans_text(session),
        reply_markup=build_spans_keyboard(session),
        parse_mode="Markdown",
    )
    return VIEWING_SPANS


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    session = sessions.pop(chat_id, None)
    if session and os.path.exists(session.doc_path):
        import shutil
        shutil.rmtree(os.path.dirname(session.doc_path), ignore_errors=True)
    await update.message.reply_text(
        "Сессия завершена. Отправьте новый PDF чтобы начать снова."
    )
    return WAITING_PDF


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "8939898526:AAEkVBfxLt6fEBgW4Ub98OYo_tBN7F00DKY")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
        ],
        states={
            WAITING_PDF: [
                MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
            ],
            VIEWING_SPANS: [
                CallbackQueryHandler(page_nav, pattern="^page_(prev|next)$"),
                CallbackQueryHandler(select_span, pattern="^edit_\\d+$"),
                CallbackQueryHandler(save_pdf, pattern="^save_pdf$"),
                MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
            ],
            EDITING_SPAN: [
                CallbackQueryHandler(cancel_edit, pattern="^cancel_edit$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_new_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)

    logger.info("Бот запущен...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
