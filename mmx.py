import asyncio
import logging
import base64
import secrets
import re
import os
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters
)
import requests

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# CONFIG - Get from environment variables
TOKEN = os.environ.get('TOKEN', '8352761247:AAGPKrMSv-fxnEjAkf3JyUcLwyLDYlrTtAU')
THIRD_PERSON_ID = int(os.environ.get('THIRD_PERSON_ID', 8385149606))
PAYMENT_API_KEY = os.environ.get('PAYMENT_API_KEY', '48533c4e008372cfb0aab1155226e0')
CREATE_ORDER_URL = "https://www.mypancard.in/paytm/create"
STATUS_CHECK_URL = "https://www.mypancard.in/paytm/status.php"
VOUCH_CHANNEL_LINK = "https://t.me/middlemam/32?comment=658"
PORT = int(os.environ.get('PORT', 8443))

# STATES
DEAL_AMOUNT, CUSTOM_AMOUNT, DEAL_DETAILS, TERMS_CONDITIONS = range(4)

# DBs
active_deals = {}
pending_payments = {}
user_data_store = {}

# Bot Info
BOT_INFO = """
🤖 **Middleman Bot - Successfully Added!**

✅ **Group automatically activated for secure deals!**

**📋 How it works:**
1️⃣ /deal - Start a new deal (Anyone can start)
2️⃣ Select amount using buttons (₹100, ₹200, ₹500, ₹1000, Custom)
3️⃣ Enter item details, terms & conditions
4️⃣ Both parties verify with buttons
5️⃣ Payment QR code generated (5 min timer)
6️⃣ After payment success, buyer clicks "Release Fund" button
7️⃣ Seller sends UPI/QR code for payment
8️⃣ Admin processes payment to seller automatically

**🎯 Available Commands:**
/deal - Start new deal
/info - Bot information
/cancel - Cancel active deal

**Ready to use! Type /deal to start your first secure deal! 🚀**
"""

# -------- AUTO ACTIVATION ON BOT ADD --------
async def handle_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Automatically show info when bot is added to ANY group"""
    try:
        new_members = update.message.new_chat_members
        for member in new_members:
            if member.is_bot and member.id == context.bot.id:
                chat_id = update.effective_chat.id
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=BOT_INFO,
                    parse_mode='Markdown'
                )
                logger.info(f"Bot added to group {chat_id} - Auto info sent")
                break
    except Exception as e:
        logger.error(f"Error in handle_new_chat_member: {e}")

# -------- PAYMENT API --------
async def create_payment_order(user_id, amount, chat_id):
    order_id = f"mm_{secrets.token_hex(12)}"
    user_details = {
        'name': f"User_{user_id}",
        'mobile': '9999999999',
        'email': f"user{user_id}@telegram.bot"
    }

    payment_data = {
        'api_key': PAYMENT_API_KEY,
        'txn_amount': str(int(amount)),
        'redirectUrl': 'https://t.me/your_bot',
        'order_id': order_id,
        'txn_note': 'Middleman Service Payment',
        'txn_note2': f'User: {user_id}',
        'txn_note3': 'Telegram Bot Deal',
        'customer_name': user_details['name'],
        'customer_mobile': user_details['mobile'],
        'customer_email': user_details['email']
    }

    try:
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/plain, */*',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
            'Origin': 'https://www.mypancard.in',
            'Referer': 'https://www.mypancard.in/'
        }

        response = requests.post(
            CREATE_ORDER_URL,
            json=payment_data,
            headers=headers,
            timeout=30,
            verify=True
        )

        logger.info(f"Payment API Response: {response.status_code}")
        if response.status_code == 200:
            try:
                result = response.json()
                if result.get('status') == True and 'results' in result:
                    return order_id, result['results']
                else:
                    error_msg = result.get('message', 'Payment API error')
                    return None, f"Payment error: {error_msg}"
            except Exception:
                return None, "Invalid response from payment gateway"
        elif response.status_code == 406:
            return None, "Payment gateway temporarily unavailable. Please try again in few minutes."
        else:
            return None, f"Payment gateway error: HTTP {response.status_code}"

    except Exception as e:
        logger.error(f"Payment error: {e}")
        return None, "Network error creating payment order. Please check connection and try again."

async def check_payment_status(order_id):
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, */*',
            'Referer': 'https://www.mypancard.in/'
        }

        response = requests.get(
            f"{STATUS_CHECK_URL}?order_id={order_id}",
            headers=headers,
            timeout=15
        )

        if response.status_code == 200:
            result = response.json()
            if result.get('status') and result.get('data', {}).get('status') == 'TXN_SUCCESS':
                return True, result['data']
        return False, None
    except Exception as e:
        logger.error(f"Payment status check error: {e}")
        return False, None

async def schedule_payment_check(context, order_id, chat_id, message_id, deal_id):
    # Check payment for 5 minutes
    for i in range(60):
        await asyncio.sleep(5)
        if order_id not in pending_payments:
            return

        is_paid, payment_data = await check_payment_status(order_id)
        if is_paid:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
            except:
                pass

            if deal_id in active_deals:
                active_deals[deal_id]['payment_status'] = 'completed'
                deal = active_deals[deal_id]

                # Get buyer and seller usernames
                try:
                    buyer_member = await context.bot.get_chat_member(chat_id, deal['buyer_id'])
                    seller_member = await context.bot.get_chat_member(chat_id, deal['seller_id'])
                    buyer_name = f"@{buyer_member.user.username}" if buyer_member.user.username else f"`{deal['buyer_id']}`"
                    seller_name = f"@{seller_member.user.username}" if seller_member.user.username else f"`{deal['seller_id']}`"
                except:
                    buyer_name = f"`{deal['buyer_id']}`"
                    seller_name = f"`{deal['seller_id']}`"

                # "Release Fund" button for buyer
                keyboard = [
                    [InlineKeyboardButton("💳 Release Fund (Product Received)", callback_data=f"release_{deal_id}")]
                ]

                reply_markup = InlineKeyboardMarkup(keyboard)

                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ **Payment Received Successfully!**\n\n"
                         f"💰 Amount: ₹{deal['amount']}\n"
                         f"👤 Buyer: {buyer_name}\n"
                         f"👤 Seller: {seller_name}\n\n"
                         f"🔏 **Privacy Tip:** Buyer and Seller can now proceed in DM for privacy.\n\n"
                         f"After receiving product/service, Buyer click the button below:",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )

            if order_id in pending_payments:
                del pending_payments[order_id]
            return

    # Payment timeout
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

    await context.bot.send_message(
        chat_id=chat_id, text="⏰ Payment session expired. Please start a new deal if needed."
    )

    if order_id in pending_payments:
        del pending_payments[order_id]
    if deal_id in active_deals:
        del active_deals[deal_id]

# -------- DEAL CREATION --------
async def start_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check if user already has active deal
    for deal_id, deal in active_deals.items():
        if user_id in [deal.get('buyer_id'), deal.get('seller_id'), deal.get('initiator_id')]:
            await update.message.reply_text("❌ You already have an active deal. Complete it first or use /cancel")
            return ConversationHandler.END

    deal_id = f"deal_{secrets.token_hex(8)}"
    active_deals[deal_id] = {
        'deal_id': deal_id,
        'initiator_id': user_id,
        'chat_id': chat_id,
        'amount': None,
        'item': None,
        'terms': None,
        'buyer_id': None,
        'seller_id': None,
        'payment_status': 'pending',
        'release_status': 'pending',
        'created_at': datetime.now().isoformat()
    }

    user_data_store[user_id] = {'deal_id': deal_id}

    # Amount selection buttons
    keyboard = [
        [
            InlineKeyboardButton("₹100", callback_data=f"amount_100_{deal_id}"),
            InlineKeyboardButton("₹200", callback_data=f"amount_200_{deal_id}"),
        ],
        [
            InlineKeyboardButton("₹500", callback_data=f"amount_500_{deal_id}"),
            InlineKeyboardButton("₹1000", callback_data=f"amount_1000_{deal_id}")
        ],
        [
            InlineKeyboardButton("💰 Custom Amount", callback_data=f"amount_custom_{deal_id}")
        ]
    ]

    await update.message.reply_text(
        "🤝 **Middleman Deal Started**\n\nSelect deal amount:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DEAL_AMOUNT

async def handle_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    amount_type = parts[1]
    deal_id = "_".join(parts[2:])

    if deal_id not in active_deals:
        await query.edit_message_text("❌ Deal expired or not found.")
        return ConversationHandler.END

    user_id = query.from_user.id
    if active_deals[deal_id]['initiator_id'] != user_id:
        await query.answer("❌ Only deal starter can select amount.", show_alert=True)
        return DEAL_AMOUNT

    if amount_type == "custom":
        await query.edit_message_text("💰 Custom Amount\nEnter amount in ₹:")
        return CUSTOM_AMOUNT
    else:
        amount = float(amount_type)
        active_deals[deal_id]['amount'] = amount

        await query.edit_message_text(
            f"✅ Amount selected: ₹{amount}\n\n💼 **Item Details**\nWhat is being sold/purchased?",
            parse_mode="Markdown"
        )
        return DEAL_DETAILS

async def custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    amount_text = update.message.text.strip()

    try:
        amount = float(amount_text)
        if amount < 1:
            await update.message.reply_text("Please enter amount greater than 0.")
            return CUSTOM_AMOUNT
    except ValueError:
        await update.message.reply_text("Please enter valid amount.")
        return CUSTOM_AMOUNT

    deal_id = user_data_store[user_id]['deal_id']
    active_deals[deal_id]['amount'] = amount

    await update.message.reply_text(
        f"✅ Custom Amount set: ₹{amount}\n\n💼 **Item Details**\nWhat is being sold/purchased?",
        parse_mode="Markdown"
    )
    return DEAL_DETAILS

async def deal_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    item = update.message.text.strip()

    if len(item) < 5:
        await update.message.reply_text("Please provide more details about the item (min 5 characters).")
        return DEAL_DETAILS

    deal_id = user_data_store[user_id]['deal_id']
    active_deals[deal_id]['item'] = item

    await update.message.reply_text(
        "📝 **Terms & Conditions**\nPlease describe deal terms & conditions:",
        parse_mode="Markdown"
    )
    return TERMS_CONDITIONS

async def terms_conditions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    terms = update.message.text.strip()

    if len(terms) < 10:
        await update.message.reply_text("Please provide more details about terms (min 10 characters).")
        return TERMS_CONDITIONS

    deal_id = user_data_store[user_id]['deal_id']
    active_deals[deal_id]['terms'] = terms

    # Show verification buttons for Buyer and Seller
    keyboard = [
        [InlineKeyboardButton("✅ I'm Buyer", callback_data=f"verify_buyer_{deal_id}")],
        [InlineKeyboardButton("✅ I'm Seller", callback_data=f"verify_seller_{deal_id}")]
    ]

    await update.message.reply_text(
        "👥 **Verification Required**\n\n"
        "Both Buyer and Seller need to verify themselves by clicking their respective buttons.\n\n"
        "*Click your role below:*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

# -------- VERIFICATION CALLBACK --------
async def handle_verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    role, deal_id = parts[1], "_".join(parts[2:])
    user_id = query.from_user.id

    if deal_id not in active_deals:
        await query.answer("❌ Deal not found.", show_alert=True)
        return

    deal = active_deals[deal_id]

    if role == "buyer":
        if deal['buyer_id'] is None:
            active_deals[deal_id]['buyer_id'] = user_id
            await query.answer("✅ Verified as Buyer!")
        else:
            await query.answer("❌ Buyer already verified.", show_alert=True)
            return

    elif role == "seller":
        if deal['seller_id'] is None:
            active_deals[deal_id]['seller_id'] = user_id
            await query.answer("✅ Verified as Seller!")
        else:
            await query.answer("❌ Seller already verified.", show_alert=True)
            return

    # Check if both parties are verified
    if deal['buyer_id'] is not None and deal['seller_id'] is not None:
        # Get usernames for display
        try:
            buyer_member = await context.bot.get_chat_member(deal['chat_id'], deal['buyer_id'])
            seller_member = await context.bot.get_chat_member(deal['chat_id'], deal['seller_id'])
            
            buyer_name = f"@{buyer_member.user.username}" if buyer_member.user.username else f"`{deal['buyer_id']}`"
            seller_name = f"@{seller_member.user.username}" if seller_member.user.username else f"`{deal['seller_id']}`"
        except:
            buyer_name = f"`{deal['buyer_id']}`"
            seller_name = f"`{deal['seller_id']}`"

        confirmation_text = (
            "🤝 **Deal Summary**\n\n"
            f"💰 **Amount:** ₹{deal['amount']}\n"
            f"📦 **Item:** {deal['item']}\n"
            f"👤 **Buyer:** {buyer_name}\n"
            f"👤 **Seller:** {seller_name}\n"
            f"📝 **Terms:** {deal['terms']}\n\n"
            "Is everything correct?"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=f"confirm_yes_{deal_id}"),
                InlineKeyboardButton("❌ No", callback_data=f"confirm_no_{deal_id}")
            ]
        ]

        await query.edit_message_text(
            confirmation_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        # Update buttons to show verification status
        buyer_verified = deal['buyer_id'] is not None
        seller_verified = deal['seller_id'] is not None
        
        keyboard = []
        if not buyer_verified:
            keyboard.append([InlineKeyboardButton("✅ I'm Buyer", callback_data=f"verify_buyer_{deal_id}")])
        else:
            keyboard.append([InlineKeyboardButton("☑️ Buyer Verified", callback_data="verified")])
            
        if not seller_verified:
            keyboard.append([InlineKeyboardButton("✅ I'm Seller", callback_data=f"verify_seller_{deal_id}")])
        else:
            keyboard.append([InlineKeyboardButton("☑️ Seller Verified", callback_data="verified")])

        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))

# -------- CONFIRMATION CALLBACK --------
async def handle_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action, deal_id = parts[1], "_".join(parts[2:])

    if deal_id not in active_deals:
        await query.edit_message_text("❌ Deal expired or not found.")
        return

    if action == "no":
        del active_deals[deal_id]
        await query.edit_message_text("❌ Deal cancelled. Start over with /deal.")
        return

    # Proceed to payment
    await query.edit_message_text("✅ Both parties verified! Starting payment process...")
    await proceed_to_payment(context, deal_id)

async def proceed_to_payment(context, deal_id):
    if deal_id not in active_deals:
        return

    deal = active_deals[deal_id]
    await asyncio.sleep(2)

    order_id, result = await create_payment_order(deal['buyer_id'], deal['amount'], deal['chat_id'])

    if not order_id:
        await context.bot.send_message(
            chat_id=deal['chat_id'],
            text=f"❌ Payment order creation failed: {result}\n\nPlease try again later or contact support."
        )
        return

    pending_payments[order_id] = {
        'user_id': deal['buyer_id'],
        'amount': deal['amount'],
        'chat_id': deal['chat_id'],
        'deal_id': deal_id,
        'timestamp': datetime.now(),
        'status': 'pending'
    }

    qr_image_base64 = result.get('qr_image', '')
    if qr_image_base64:
        try:
            qr_image_data = base64.b64decode(qr_image_base64)
            sent_message = await context.bot.send_photo(
                chat_id=deal['chat_id'],
                photo=qr_image_data,
                caption=f"💳 **Payment Required**\n\n"
                        f"💰 Amount: ₹{deal['amount']}\n\n"
                        f"⏰ **Time Limit: 5 minutes**\n\n"
                        f"Scan QR to pay. Order ID: `{order_id}`",
                parse_mode='Markdown'
            )

            pending_payments[order_id]['message_id'] = sent_message.message_id

            asyncio.create_task(schedule_payment_check(
                context, order_id, deal['chat_id'], sent_message.message_id, deal_id
            ))

        except Exception as e:
            logger.error(f"QR code error: {e}")
            await context.bot.send_message(
                chat_id=deal['chat_id'],
                text="❌ Error generating payment QR code. Please contact support."
            )

# -------- RELEASE FUND CALLBACK --------
async def handle_release_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    deal_id = "_".join(query.data.split("_")[1:])
    user_id = query.from_user.id

    if deal_id not in active_deals:
        await query.answer("❌ Deal not found.", show_alert=True)
        return

    deal = active_deals[deal_id]

    # Check if user is the buyer
    if deal['buyer_id'] != user_id:
        await query.answer("❌ Only buyer can release fund.", show_alert=True)
        return

    if deal['payment_status'] != 'completed':
        await query.answer("❌ Payment is still pending.", show_alert=True)
        return

    active_deals[deal_id]['release_status'] = 'released'

    await query.edit_message_text(
        f"✅ **Fund Released by Buyer!**\n\n"
        f"👤 Seller, please send your UPI ID or QR code to receive payment.\n\n"
        f"**Simply send UPI ID or QR code image - no need to reply to any message!**",
        parse_mode='Markdown'
    )

    # Set up seller payment info collection
    if deal['seller_id']:
        user_data_store[deal['seller_id']] = {
            'deal_id': deal_id, 
            'awaiting_payment_info': True, 
            'chat_id': deal['chat_id']
        }

# -------- SELLER PAYMENT INFO --------
def is_valid_upi_id(text):
    upi_pattern = r'^[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+$'
    if not re.match(upi_pattern, text):
        return False
    providers = ['paytm', 'phonepe', 'googlepay', 'gpay', 'ybl', 'okaxis',
                'okhdfcbank', 'okicici', 'oksbi', 'allbank', 'ibl', 'axl']
    return any(provider in text.lower() for provider in providers)

async def handle_seller_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Check if this user is awaiting payment info and in correct chat
    if (user_id not in user_data_store or
        not user_data_store[user_id].get('awaiting_payment_info') or
        user_data_store[user_id].get('chat_id') != chat_id):
        return False

    deal_id = user_data_store[user_id]['deal_id']
    if deal_id not in active_deals:
        return False

    deal = active_deals[deal_id]

    if update.message.text:
        upi_text = update.message.text.strip()
        if is_valid_upi_id(upi_text):
            payment_info = f"UPI ID: {upi_text}"
            try:
                # Send to third person (admin)
                await context.bot.send_message(
                    chat_id=THIRD_PERSON_ID,
                    text=f"💰 **Payment Request**\n\n"
                         f"Amount: ₹{deal['amount']}\n"
                         f"Seller ID: {deal['seller_id']}\n"
                         f"Payment Info: {payment_info}\n\n"
                         f"Please make payment and type /paymentdone.",
                    parse_mode='Markdown'
                )

                await update.message.reply_text("✅ Your UPI ID sent to admin. Please wait for payment.")
                del user_data_store[user_id]
                active_deals[deal_id]['seller_payment_info'] = payment_info
                return True
            except Exception:
                await update.message.reply_text("❌ Could not notify admin. Please contact manually.")
        else:
            await update.message.reply_text("❌ Invalid UPI ID. Please send proper UPI ID (e.g., name@paytm)")

    elif update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        try:
            # Send QR to third person (admin)
            await context.bot.send_photo(
                chat_id=THIRD_PERSON_ID,
                photo=file_id,
                caption=f"💰 **Payment Request**\n\n"
                        f"Amount: ₹{deal['amount']}\nSeller ID: {deal['seller_id']}\n\n"
                        f"Please scan QR code and type /paymentdone.",
                parse_mode='Markdown'
            )

            await update.message.reply_text("✅ Your QR code sent to admin. Please wait for payment.")
            del user_data_store[user_id]
            active_deals[deal_id]['seller_payment_info'] = f"QR Code: {file_id}"
            return True
        except Exception:
            await update.message.reply_text("❌ Could not send QR to admin. Please contact manually.")

    return False

# -------- PAYMENT CONFIRMATION (ADMIN ONLY) --------
async def payment_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != THIRD_PERSON_ID:
        await update.message.reply_text("❌ Only admin can confirm payment to seller.")
        return

    deal_id = None
    for did, deal in active_deals.items():
        if deal['release_status'] == 'released' and deal.get('seller_payment_info'):
            deal_id = did
            break

    if not deal_id:
        await update.message.reply_text("❌ No deal waiting for payment confirmation.")
        return

    deal = active_deals[deal_id]

    # Get usernames for vouch messages
    try:
        buyer_member = await context.bot.get_chat_member(deal['chat_id'], deal['buyer_id'])
        seller_member = await context.bot.get_chat_member(deal['chat_id'], deal['seller_id'])
        
        buyer_mention = f"@{buyer_member.user.username}" if buyer_member.user.username else f"{deal['buyer_id']}"
        seller_mention = f"@{seller_member.user.username}" if seller_member.user.username else f"{deal['seller_id']}"
    except:
        buyer_mention = f"{deal['buyer_id']}"
        seller_mention = f"{deal['seller_id']}"

    # MESSAGE 1: Main completion message
    message1 = (
        f"✅ Payment to Seller Completed by Admin!\n\n"
        f"🎉 Deal Successfully Completed!\n\n"
        f"💰 Amount: {deal['amount']}\n"
        f"👤 Buyer: {buyer_mention}\n"
        f"👤 Seller: {seller_mention}\n"
        f"🏆 Item: {deal['item']}\n"
        f"⬆️ T&C: {deal['terms']}\n\n"
        f"Thank you for using my Middleman service! 🤝\n\n"
        f"Please leave me a vouch here: {VOUCH_CHANNEL_LINK}\n\n"
        f"Format: Vouch @hunny MM'd. $xxxx"
    )

    # MESSAGE 2: Buyer vouch
    message2 = f"Vouch {buyer_mention} For Using My Middleman Service 💞"

    # MESSAGE 3: Seller vouch  
    message3 = f"Vouch {seller_mention} For Using My Middleman Service 💞"

    # Send all 3 messages
    await context.bot.send_message(
        chat_id=deal['chat_id'],
        text=message1,
        parse_mode='Markdown'
    )
    
    await asyncio.sleep(1)
    
    await context.bot.send_message(
        chat_id=deal['chat_id'],
        text=message2,
        parse_mode='Markdown'
    )
    
    await asyncio.sleep(1)
    
    await context.bot.send_message(
        chat_id=deal['chat_id'],
        text=message3,
        parse_mode='Markdown'
    )

    # Clean up completed deal
    del active_deals[deal_id]

# -------- OTHER COMMANDS --------
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    deal_id = None
    for did, deal in active_deals.items():
        if user_id in [deal.get('initiator_id'), deal.get('buyer_id'), deal.get('seller_id')] and deal['chat_id'] == chat_id:
            deal_id = did
            break

    if deal_id:
        del active_deals[deal_id]
        if user_id in user_data_store:
            del user_data_store[user_id]
        await update.message.reply_text("✅ Deal cancelled successfully.")
    else:
        await update.message.reply_text("❌ No active deal found.")

    return ConversationHandler.END

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(BOT_INFO, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == THIRD_PERSON_ID:
        await update.message.reply_text(
            "👋 **Welcome Admin!**\n\n"
            "You handle seller payments using /paymentdone command.\n\n" + BOT_INFO,
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(
            "👋 **Welcome to Middleman Bot!**\n\n" + BOT_INFO,
            parse_mode='Markdown'
        )

# -------- MESSAGE ROUTERS --------
async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await handle_seller_payment_info(update, context):
        return

async def photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_seller_payment_info(update, context)

# -------- CALLBACK QUERY HANDLER --------
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    if data.startswith("amount_"):
        return await handle_amount_callback(update, context)
    elif data.startswith("confirm_"):
        return await handle_confirmation_callback(update, context)
    elif data.startswith("verify_"):
        return await handle_verification_callback(update, context)
    elif data.startswith("release_"):
        return await handle_release_callback(update, context)
    elif data == "verified":
        await query.answer("✅ Verified!")
    else:
        await query.answer()

# -------- ERROR HANDLER --------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    try:
        if hasattr(update, "message") and update.message:
            await update.message.reply_text("❌ Something went wrong. Please try again.")
        elif hasattr(update, "callback_query") and update.callback_query:
            await update.callback_query.answer("❌ Error occurred. Please try again.", show_alert=True)
    except Exception:
        pass

# -------- WEBHOOK SETUP FOR RENDER --------
async def set_webhook(application):
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}"
    await application.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to: {webhook_url}")

# -------- MAIN FUNCTION --------
def main():
    print("🚀 Starting Universal Middleman Bot...")
    
    # Create application
    application = Application.builder().token(TOKEN).build()

    # Add error handler
    application.add_error_handler(error_handler)

    # Conversation handler for deal creation
    deal_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('deal', start_deal)],
        states={
            DEAL_AMOUNT: [CallbackQueryHandler(handle_amount_callback, pattern="^amount_")],
            CUSTOM_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_amount)],
            DEAL_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_details)],
            TERMS_CONDITIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, terms_conditions)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True,
    )

    # Register all handlers
    application.add_handler(deal_conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('info', info))
    application.add_handler(CommandHandler('paymentdone', payment_done))
    application.add_handler(CommandHandler('cancel', cancel))

    # Auto-info when bot added to group
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_member))

    # Callback and message handlers
    application.add_handler(CallbackQueryHandler(callback_query_handler))
    application.add_handler(MessageHandler(filters.PHOTO, photo_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    # Check if running on Render
    if os.environ.get('RENDER'):
        print("🌐 Running on Render - Using Webhook")
        # Start the webhook server
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TOKEN}",
            drop_pending_updates=True
        )
    else:
        print("🖥️  Running locally - Using Polling")
        # Start polling for local development
        application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()