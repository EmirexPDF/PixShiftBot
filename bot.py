import os
import io
import logging
from aiohttp import web
from PIL import Image
from rembg import remove
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", "10000"))  # Render provides $PORT dynamically

# Temporary store for bulk processing or user settings (In-memory)
# Format: { user_id: { "files": [bytes, ...], "action": "..." } }
USER_DATA = {}

# --- CORE IMAGE PROCESSING FUNCTIONS ---

def convert_image(img_bytes, target_format) -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes))
    if img.mode in ('RGBA', 'LA') and target_format.upper() in ('JPEG', 'JPG'):
        img = img.convert('RGB')
    
    out_io = io.BytesIO()
    # Handle PDF specifically
    if target_format.upper() == 'PDF':
        img.save(out_io, format='PDF', save_all=True)
    else:
        img.save(out_io, format=target_format.upper())
    out_io.seek(0)
    return out_io

def compress_resize_image(img_bytes, resize_pct=50, quality=60) -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes))
    # Resize
    if resize_pct != 100:
        new_size = (int(img.width * (resize_pct / 100)), int(img.height * (resize_pct / 100)))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    out_io = io.BytesIO()
    # PDF/PNG don't use 'quality' parameters the same way as JPEG
    if img.mode in ('RGBA', 'LA'):
        img.save(out_io, format='PNG', optimize=True)
    else:
        img.save(out_io, format='JPEG', quality=quality)
    out_io.seek(0)
    return out_io

def remove_bg(img_bytes) -> io.BytesIO:
    input_data = img_bytes
    output_data = remove(input_data)
    out_io = io.BytesIO(output_data)
    out_io.seek(0)
    return out_io

def add_watermark_text(img_bytes, text="PixShiftBot") -> io.BytesIO:
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    txt_layer = Image.new("RGBA", img.size, (255, 255, 255, 0))
    
    # Simple watermark approach avoiding system font path issues on Render Linux
    from PIL import ImageDraw
    d = ImageDraw.Draw(txt_layer)
    # Place watermark text diagonally or bottom right
    d.text((10, img.height - 30), text, fill=(255, 255, 255, 128)) 
    
    watermarked = Image.alpha_composite(img, txt_layer)
    out_io = io.BytesIO()
    watermarked.convert("RGB").save(out_io, format="JPEG")
    out_io.seek(0)
    return out_io

# --- TELEGRAM BOT HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✨ Welcome to **PixShiftBot**! ✨\n\n"
        "Send me one or multiple images (as Photos or Documents) to begin. "
        "I can convert formats, compress, remove backgrounds, and add watermarks!"
    )

async def handle_document_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Extract file ID from Photo or Document
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("Please send an image file.")
        return

    # Download file into memory
    bot_file = await context.bot.get_file(file_id)
    img_buffer = io.BytesIO()
    await bot_file.download_to_memory(out=img_buffer)
    img_bytes = img_buffer.getvalue()

    # Save to user session data
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {"files": []}
    
    USER_DATA[user_id]["files"].append(img_bytes)
    count = len(USER_DATA[user_id]["files"])

    keyboard = [
        [InlineKeyboardButton("🔄 Convert Format", callback_data="menu_convert")],
        [InlineKeyboardButton("🗜️ Compress & Resize (50%)", callback_data="action_compress")],
        [InlineKeyboardButton("✂️ Remove Background", callback_data="action_rembg")],
        [InlineKeyboardButton("🏷️ Add Watermark", callback_data="action_watermark")],
        [InlineKeyboardButton("🧹 Clear Queue", callback_data="menu_clear")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"📥 Image received! Total in queue: **{count}**\nWhat would you like to do?",
        reply_markup=reply_markup
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in USER_DATA or not USER_DATA[user_id]["files"]:
        await query.edit_message_text("No active images found. Please upload a new image.")
        return

    data = query.data
    files = USER_DATA[user_id]["files"]

    if data == "menu_convert":
        keyboard = [
            [InlineKeyboardButton("➡️ JPG", callback_data="to_jpg"), InlineKeyboardButton("➡️ PNG", callback_data="to_png")],
            [InlineKeyboardButton("➡️ WEBP", callback_data="to_webp"), InlineKeyboardButton("➡️ PDF", callback_data="to_pdf")]
        ]
        await query.edit_message_text("Select your target format:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "menu_clear":
        USER_DATA[user_id]["files"] = []
        await query.edit_message_text("Queue cleared! Send me new images whenever you're ready.")
        return

    await query.edit_message_text("⏳ Processing your request... please wait.")

    try:
        # Determine format transitions
        target_format = None
        if data.startswith("to_"):
            target_format = data.split("_")[1].upper()

        # Process all queued assets (Bulk architecture native support)
        for out_idx, raw_bytes in enumerate(files):
            if target_format:
                processed = convert_image(raw_bytes, target_format)
                ext = target_format.lower()
            elif data == "action_compress":
                processed = compress_resize_image(raw_bytes)
                ext = "jpg"
            elif data == "action_rembg":
                processed = remove_bg(raw_bytes)
                ext = "png"
            elif data == "action_watermark":
                processed = add_watermark_text(raw_bytes)
                ext = "jpg"
            else:
                continue

            # Route file return payload dynamically
            filename = f"processed_{out_idx+1}.{ext}"
            processed.name = filename
            await query.message.reply_document(document=processed, filename=filename)

        await query.message.reply_text("✅ All operations completed successfully!")
    except Exception as e:
        logger.error(f"Error processing asset: {e}")
        await query.message.reply_text("❌ An error occurred during processing.")
    finally:
        # Clear database slice for session
        USER_DATA[user_id]["files"] = []

# --- RENDER ALIVE-KEEPER WEB SERVER ---

async def handle_health_check(request):
    return web.Response(text
