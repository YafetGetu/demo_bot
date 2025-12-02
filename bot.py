import os
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters
from pymongo import MongoClient
from datetime import datetime
from flask import Flask
import threading

app = Flask('')

@app.route('/')
def home():
    return "Bot is running"

def run():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run).start()


# Load .env file
load_dotenv()

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")
BOT_USERNAME = os.getenv("BOT_USERNAME")

# Connect to MongoDB
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
counters_collection = db["counters"]
comments_collection = db["comments"]
channel_posts_collection = db["channel_posts"]

# Helper: get or create user in DB
def get_or_create_user(user_id):
    user = users_collection.find_one({"telegram_id": user_id})
    if not user:
        user = {
            "telegram_id": user_id,
            "nickname": "Anonymous",
            "profile_emoji": "👤",
            "aura": 0,
            "confessions": [],
            "comments": [],
            "liked_comments": [],
            "disliked_comments": []
        }
        users_collection.insert_one(user)
    return user

# Update user data in DB
def update_user(user_id, data: dict):
    users_collection.update_one({"telegram_id": user_id}, {"$set": data})

# Update user's aura
def update_user_aura(user_id, delta):
    users_collection.update_one(
        {"telegram_id": user_id},
        {"$inc": {"aura": delta}}
    )
    return users_collection.find_one({"telegram_id": user_id})["aura"]

# Generate global incremental confession ID
def get_next_confession_id():
    counter = counters_collection.find_one_and_update(
        {"_id": "confession_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    return counter["seq"]

# Generate global incremental comment ID
def get_next_comment_id():
    counter = counters_collection.find_one_and_update(
        {"_id": "comment_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    return counter["seq"]

# Get confession from DB by ID
def get_confession_by_id(confession_id):
    user = users_collection.find_one({"confessions.confession_id": confession_id})
    if user:
        for confession in user["confessions"]:
            if confession["confession_id"] == confession_id:
                return confession
    return None

# Add confession to DB
def add_confession(user_id, text, status="pending"):
    confession_id = get_next_confession_id()
    confession = {
        "confession_id": confession_id,
        "text": text,
        "status": status,
        "user_id": user_id,
        "timestamp": datetime.now(),
        "comments": []
    }
    users_collection.update_one(
        {"telegram_id": user_id},
        {"$push": {"confessions": confession}}
    )
    return confession_id

# Add comment to confession
def add_comment_to_confession(confession_id, user_id, text):
    comment_id = get_next_comment_id()
    comment = {
        "comment_id": comment_id,
        "confession_id": confession_id,
        "user_id": user_id,
        "text": text,
        "timestamp": datetime.now(),
        "likes": 0,
        "dislikes": 0,
        "reply_count": 0
    }
    
    # Add to comments collection
    comments_collection.insert_one(comment)
    
    # Also add to user's comments
    users_collection.update_one(
        {"telegram_id": user_id},
        {"$push": {"comments": comment}}
    )
    
    return comment_id

# Add reply to comment
def add_reply_to_comment(parent_comment_id, user_id, text):
    # Get the parent comment to get confession_id
    parent_comment = comments_collection.find_one({"comment_id": parent_comment_id})
    if not parent_comment:
        return None, None
    
    confession_id = parent_comment["confession_id"]
    
    # Add reply as a regular comment
    reply_id = get_next_comment_id()
    reply = {
        "comment_id": reply_id,
        "confession_id": confession_id,
        "user_id": user_id,
        "text": text,
        "timestamp": datetime.now(),
        "likes": 0,
        "dislikes": 0,
        "reply_count": 0,
        "is_reply": True,
        "parent_comment_id": parent_comment_id
    }
    
    # Add to comments collection
    comments_collection.insert_one(reply)
    
    # Also add to user's comments
    users_collection.update_one(
        {"telegram_id": user_id},
        {"$push": {"comments": reply}}
    )
    
    # Update parent comment's reply count
    comments_collection.update_one(
        {"comment_id": parent_comment_id},
        {"$inc": {"reply_count": 1}}
    )
    
    return reply_id, confession_id

# Handle like/dislike on comment
def handle_comment_reaction(comment_id, user_id, reaction_type):
    comment = comments_collection.find_one({"comment_id": comment_id})
    if not comment:
        return None, 0, 0
    
    comment_owner_id = comment["user_id"]
    user = get_or_create_user(user_id)
    
    # Check if user already liked/disliked
    liked_comments = user.get("liked_comments", [])
    disliked_comments = user.get("disliked_comments", [])
    
    likes = comment.get("likes", 0)
    dislikes = comment.get("dislikes", 0)
    aura_change = 0
    
    if reaction_type == "like":
        if comment_id in liked_comments:
            # User already liked, remove like
            likes -= 1
            comments_collection.update_one(
                {"comment_id": comment_id},
                {"$inc": {"likes": -1}}
            )
            users_collection.update_one(
                {"telegram_id": user_id},
                {"$pull": {"liked_comments": comment_id}}
            )
            # Decrease aura from comment owner
            new_aura = update_user_aura(comment_owner_id, -1)
            aura_change = -1
            result = "like_removed"
        else:
            # Add like
            likes += 1
            comments_collection.update_one(
                {"comment_id": comment_id},
                {"$inc": {"likes": 1}}
            )
            users_collection.update_one(
                {"telegram_id": user_id},
                {"$push": {"liked_comments": comment_id}}
            )
            # Increase aura for comment owner
            new_aura = update_user_aura(comment_owner_id, 1)
            aura_change = 1
            result = "liked"
            
            # If user previously disliked, remove dislike
            if comment_id in disliked_comments:
                dislikes -= 1
                comments_collection.update_one(
                    {"comment_id": comment_id},
                    {"$inc": {"dislikes": -1}}
                )
                users_collection.update_one(
                    {"telegram_id": user_id},
                    {"$pull": {"disliked_comments": comment_id}}
                )
                # Add back aura that was decreased
                update_user_aura(comment_owner_id, 1)
                aura_change += 1
    
    elif reaction_type == "dislike":
        if comment_id in disliked_comments:
            # User already disliked, remove dislike
            dislikes -= 1
            comments_collection.update_one(
                {"comment_id": comment_id},
                {"$inc": {"dislikes": -1}}
            )
            users_collection.update_one(
                {"telegram_id": user_id},
                {"$pull": {"disliked_comments": comment_id}}
            )
            # Add back aura that was decreased
            new_aura = update_user_aura(comment_owner_id, 1)
            aura_change = 1
            result = "dislike_removed"
        else:
            # Add dislike
            dislikes += 1
            comments_collection.update_one(
                {"comment_id": comment_id},
                {"$inc": {"dislikes": 1}}
            )
            users_collection.update_one(
                {"telegram_id": user_id},
                {"$push": {"disliked_comments": comment_id}}
            )
            # Decrease aura for comment owner
            new_aura = update_user_aura(comment_owner_id, -1)
            aura_change = -1
            result = "disliked"
            
            # If user previously liked, remove like
            if comment_id in liked_comments:
                likes -= 1
                comments_collection.update_one(
                    {"comment_id": comment_id},
                    {"$inc": {"likes": -1}}
                )
                users_collection.update_one(
                    {"telegram_id": user_id},
                    {"$pull": {"liked_comments": comment_id}}
                )
                # Remove aura that was added
                update_user_aura(comment_owner_id, -1)
                aura_change -= 1
    
    return result, likes, dislikes

# Get comments for confession (OLDEST FIRST - new at bottom)
def get_comments_for_confession(confession_id):
    # Get all comments for this confession, sorted by timestamp (OLDEST FIRST for display)
    all_comments = list(comments_collection.find(
        {"confession_id": confession_id}
    ).sort("timestamp", 1))  # 1 for ascending (oldest first) - NEW AT BOTTOM
    
    # Separate regular comments and replies
    regular_comments = []
    replies_by_parent = {}
    
    for comment in all_comments:
        if comment.get("is_reply"):
            parent_id = comment.get("parent_comment_id")
            if parent_id:
                if parent_id not in replies_by_parent:
                    replies_by_parent[parent_id] = []
                replies_by_parent[parent_id].append(comment)
        else:
            regular_comments.append(comment)
    
    total_comments = len(all_comments)
    
    return regular_comments, replies_by_parent, total_comments

# Get single comment with user info
def get_comment_with_user_info(comment_id, current_user_id=None):
    comment = comments_collection.find_one({"comment_id": comment_id})
    if not comment:
        return None
    
    user = get_or_create_user(comment["user_id"])
    comment_owner = {
        "nickname": user.get("nickname", "Anonymous"),
        "profile_emoji": user.get("profile_emoji", "👤"),
        "aura": user.get("aura", 0)
    }
    
    # Check if current user has liked/disliked this comment
    user_liked = False
    user_disliked = False
    
    if current_user_id:
        current_user = get_or_create_user(current_user_id)
        liked_comments = current_user.get("liked_comments", [])
        disliked_comments = current_user.get("disliked_comments", [])
        user_liked = comment_id in liked_comments
        user_disliked = comment_id in disliked_comments
    
    return {
        **comment,
        "user_info": comment_owner,
        "user_liked": user_liked,
        "user_disliked": user_disliked
    }

# Store channel post info
def store_channel_post(confession_id, message_id):
    channel_posts_collection.update_one(
        {"confession_id": confession_id},
        {"$set": {"message_id": message_id, "timestamp": datetime.now()}},
        upsert=True
    )

# Update channel post button
async def update_channel_post_button(confession_id, context):
    post_info = channel_posts_collection.find_one({"confession_id": confession_id})
    if post_info and 'message_id' in post_info:
        comments_count = comments_collection.count_documents({"confession_id": confession_id})
        
        bot_url = f"https://t.me/{BOT_USERNAME}?start=confession_{confession_id}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💬 View / Add Comments ({comments_count})",
                url=bot_url
            )
        ]])
        
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=post_info['message_id'],
                reply_markup=keyboard
            )
        except Exception as e:
            print(f"Error updating channel post: {e}")

# Send confession to admin for approval
async def send_to_admin(confession_id, user_text, user_id, context):
    buttons = [
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{confession_id}")],
        [InlineKeyboardButton("❌ Reject", callback_data=f"reject_{confession_id}")]
    ]
    await context.bot.send_message(
        chat_id=int(ADMIN_CHAT_ID),
        text=f"New confession received (ID: #{confession_id}):\n\n{user_text}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# Format comment display
def format_comment_display(comment_data, is_reply=False, parent_comment_info=None):
    comment = comment_data
    user_info = comment.get("user_info", {})
    
    # Format timestamp
    timestamp = comment.get("timestamp", datetime.now())
    if isinstance(timestamp, str):
        from dateutil import parser
        timestamp = parser.parse(timestamp)
    
    time_str = timestamp.strftime("%H:%M")
    
    # Build display text
    if is_reply and parent_comment_info:
        # For replies, show who they're replying to at the beginning
        parent_user_info = parent_comment_info.get("user_info", {})
        display_text = f"↪️ Reply to {parent_user_info.get('profile_emoji', '👤')} {parent_user_info.get('nickname', 'Anonymous')}\n"
        display_text += f"{user_info.get('profile_emoji', '👤')} {user_info.get('nickname', 'Anonymous')} ⚡︎ {user_info.get('aura', 0)}\n"
        display_text += f"💬 {comment.get('text', '')}\n\n"
        display_text += f"🕐 {time_str}"
    else:
        # For regular comments
        display_text = f"{user_info.get('profile_emoji', '👤')} {user_info.get('nickname', 'Anonymous')} ⚡︎ {user_info.get('aura', 0)}\n"
        display_text += f"💬 {comment.get('text', '')}\n\n"
        display_text += f"🕐 {time_str}"
    
    return display_text

# Start command handler with deep linking support
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_or_create_user(user_id)

    # Check for deep link parameter
    args = context.args
    
    if args and args[0].startswith("confession_"):
        try:
            confession_id = int(args[0].replace("confession_", ""))
            confession = get_confession_by_id(confession_id)
            
            if confession:
                comments_count = comments_collection.count_documents({"confession_id": confession_id})
                
                confession_text = confession.get('text', '')
                message_text = f"📄 Confession #{confession_id}\n\n{confession_text}\n\n💬 Comments: {comments_count}"
                
                buttons = [
                    [InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment_{confession_id}")],
                    [InlineKeyboardButton("📝 View Comments", callback_data=f"view_comments_{confession_id}")],
                    [InlineKeyboardButton("🏠 Main Menu", callback_data="back_to_main")]
                ]
                
                await update.message.reply_text(message_text, reply_markup=InlineKeyboardMarkup(buttons))
                return
        except ValueError:
            pass

    # Save session info
    context.user_data['nickname'] = user.get('nickname', 'Anonymous')
    context.user_data['profile_emoji'] = user.get('profile_emoji', '👤')
    context.user_data['aura'] = user.get('aura', 0)
    context.user_data['confessions'] = user.get('confessions', [])

    keyboard = [
        [InlineKeyboardButton("Confess", callback_data="confess")],
        [InlineKeyboardButton("Profile", callback_data="profile")],
        [InlineKeyboardButton("Rules", callback_data="rules")]
    ]
    await update.message.reply_text(
        "Welcome to Confession Bot! Choose an option:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Send individual comment as separate message
async def send_single_comment(comment_data, confession_id, query, is_reply=False, parent_comment_info=None):
    display_text = format_comment_display(comment_data, is_reply, parent_comment_info)
    
    # Get like/dislike counts
    likes = comment_data.get('likes', 0)
    dislikes = comment_data.get('dislikes', 0)
    reply_count = comment_data.get('reply_count', 0)
    
    # Create buttons with counts ON THE BUTTONS
    comment_buttons = [
        InlineKeyboardButton(f"👍 {likes}", callback_data=f"like_comment_{comment_data['comment_id']}"),
        InlineKeyboardButton(f"👎 {dislikes}", callback_data=f"dislike_comment_{comment_data['comment_id']}"),
        InlineKeyboardButton(f"💬 Reply ({reply_count})", callback_data=f"reply_comment_{comment_data['comment_id']}")
    ]
    
    # Action buttons below the comment
    action_buttons = [
        [InlineKeyboardButton("💬 Add New Comment", callback_data=f"add_comment_{confession_id}")],
        [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")],
        [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
    ]
    
    # Combine all buttons
    all_buttons = [comment_buttons]
    all_buttons.extend(action_buttons)
    
    # Send as new message
    await query.message.reply_text(
        display_text,
        reply_markup=InlineKeyboardMarkup(all_buttons)
    )

# Main button handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Confess
    if query.data == "confess":
        confess_keyboard = [
            [InlineKeyboardButton("❌ Cancel Confession", callback_data="cancel_confess")]
        ]
        await query.edit_message_text(
            "Please send the text of your confession.\nYou will be able to review, edit, or enhance it next.",
            reply_markup=InlineKeyboardMarkup(confess_keyboard)
        )

    elif query.data == "cancel_confess":
        main_keyboard = [
            [InlineKeyboardButton("Confess", callback_data="confess")],
            [InlineKeyboardButton("Profile", callback_data="profile")],
            [InlineKeyboardButton("Rules", callback_data="rules")]
        ]
        await query.edit_message_text(
            "Confession canceled.\n\nWelcome back to the main menu:",
            reply_markup=InlineKeyboardMarkup(main_keyboard)
        )

    # Profile menu
    elif query.data == "profile":
        user = get_or_create_user(user_id)
        context.user_data['nickname'] = user.get('nickname', 'Anonymous')
        context.user_data['profile_emoji'] = user.get('profile_emoji', '👤')
        context.user_data['aura'] = user.get('aura', 0)
        context.user_data['confessions'] = user.get('confessions', [])

        profile_keyboard = [
            [InlineKeyboardButton("Edit Profile", callback_data="edit_profile")],
            [InlineKeyboardButton("My Confessions", callback_data="my_confessions")],
            [InlineKeyboardButton("My Comments", callback_data="my_comments")],
            [InlineKeyboardButton("⬅ Back", callback_data="back_to_main")]
        ]
        profile_text = f"{context.user_data['profile_emoji']} {context.user_data['nickname']}\n\n⚡️ Aura: {context.user_data['aura']}"
        await query.edit_message_text(profile_text, reply_markup=InlineKeyboardMarkup(profile_keyboard))

    elif query.data == "edit_profile":
        edit_profile_keyboard = [
            [InlineKeyboardButton("Change Profile Emoji", callback_data="change_emoji")],
            [InlineKeyboardButton("Change Nickname", callback_data="change_nickname")],
            [InlineKeyboardButton("⬅ Back to Profile", callback_data="profile")]
        ]
        profile_text = (
            "🎨 Profile Customization\n\n"
            f"Profile Emoji: {context.user_data.get('profile_emoji', 'None')}\n"
            f"Nickname: {context.user_data.get('nickname', 'Default (Anonymous)')}\n"
            f"⚡️ Aura: {context.user_data.get('aura', 0)}"
        )
        await query.edit_message_text(profile_text, reply_markup=InlineKeyboardMarkup(edit_profile_keyboard))

    elif query.data == "change_emoji":
        emoji_list = ["💀","🔱","🔥","💰","😎","👻","👹","👩‍🦰","👨‍🦱","🥷","☦️","☪️","🧚‍♀️","💅","🐶","🦅","🐰","🐐",
                      "🔞","⚽️","🍆","🥜","🍑","❄️","🌚","🥀","💫","☀️","🌝","🦍"]
        emoji_keyboard = [[InlineKeyboardButton(e, callback_data=f"set_emoji_{e}") for e in emoji_list[i:i+5]] for i in range(0, len(emoji_list), 5)]
        emoji_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="edit_profile")])
        await query.edit_message_text("Choose your profile emoji:", reply_markup=InlineKeyboardMarkup(emoji_keyboard))

    elif query.data.startswith("set_emoji_"):
        selected_emoji = query.data.replace("set_emoji_", "")
        context.user_data['profile_emoji'] = selected_emoji
        update_user(user_id, {"profile_emoji": selected_emoji})
        back_button = [[InlineKeyboardButton("⬅ Back to Profile", callback_data="edit_profile")]]
        await query.edit_message_text(f"Profile emoji set to: {selected_emoji}", reply_markup=InlineKeyboardMarkup(back_button))

    elif query.data == "change_nickname":
        context.user_data['editing_nickname'] = True
        nickname_keyboard = [[InlineKeyboardButton("⬅ Back", callback_data="edit_profile")]]
        await query.edit_message_text("Please send your new nickname (max 30 characters).", reply_markup=InlineKeyboardMarkup(nickname_keyboard))

    elif query.data == "back_to_main":
        main_keyboard = [
            [InlineKeyboardButton("Confess", callback_data="confess")],
            [InlineKeyboardButton("Profile", callback_data="profile")],
            [InlineKeyboardButton("Rules", callback_data="rules")]
        ]
        await query.edit_message_text("Welcome to Confession Bot! Choose an option:", reply_markup=InlineKeyboardMarkup(main_keyboard))

    elif query.data == "rules":
        rules_keyboard = [[InlineKeyboardButton("⬅ Back", callback_data="back_to_main")]]
        rules_text = "Rules:\n1. No personal attacks.\n2. No illegal content.\n3. Be respectful.\n4. Stay anonymous."
        await query.edit_message_text(rules_text, reply_markup=InlineKeyboardMarkup(rules_keyboard))

    elif query.data == "my_comments":
        user = get_or_create_user(user_id)
        user_comments = user.get('comments', [])
        
        if not user_comments:
            message_text = "You haven't commented on any confessions yet."
            buttons = [[InlineKeyboardButton("⬅ Back to Profile", callback_data="profile")]]
        else:
            message_text = "📝 Your Comments:\n\n"
            for comment in user_comments[-10:]:
                confession_id = comment.get('confession_id', 'N/A')
                text_preview = comment.get('text', '')[:50] + '...' if len(comment.get('text', '')) > 50 else comment.get('text', '')
                message_text += f"On Confession #{confession_id}:\n\"{text_preview}\"\n\n"
            buttons = [
                [InlineKeyboardButton("⬅ Back to Profile", callback_data="profile")],
                [InlineKeyboardButton("Browse Confessions", callback_data="browse_confessions")]
            ]
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(buttons))

    elif query.data == "my_confessions":
        user_confessions = context.user_data.get('confessions', [])
        if not user_confessions:
            message_text = "You haven't confessed yet."
            buttons = [[InlineKeyboardButton("Submit New Confession", callback_data="confess")]]
        else:
            message_text = "📜 Your Confessions (Page 1/1)\n\n"
            buttons = []
            for idx, conf in enumerate(user_confessions, 1):
                status_icon = "✅ Approved" if conf.get('status') == 'approved' else "⏳ Pending"
                text_preview = conf.get('text', '')[:50] + '...' if len(conf.get('text', '')) > 50 else conf.get('text', '')
                message_text += f"ID: #{conf['confession_id']} ({status_icon})\n\"{text_preview}\"\n\n"
                if conf.get('status') == 'pending':
                    buttons.append([InlineKeyboardButton(f"Request Deletion for #{conf['confession_id']}", callback_data=f"delete_confess_{conf['confession_id']}")])
            buttons.append([InlineKeyboardButton("Submit New Confession", callback_data="confess")])
        await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(buttons))

    # Confession category & final submit logic
    elif query.data == "submit_confess":
        context.user_data['selected_categories'] = set()
        categories = ["family","sexual assult","addition","friendship","relation ship","couples","truama","mental",
                      "sexual","crush","rape","harassment","school","collage","university","highschool","others"]
        category_keyboard = [[InlineKeyboardButton(cat, callback_data=f"category_{cat}") for cat in categories[i:i+3]] for i in range(0, len(categories), 3)]
        category_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="review_confess")])
        await query.edit_message_text("Choose at least 3 categories:", reply_markup=InlineKeyboardMarkup(category_keyboard))

    elif query.data.startswith("category_"):
        selected_cat = query.data.replace("category_", "")
        if 'selected_categories' not in context.user_data:
            context.user_data['selected_categories'] = set()
        if selected_cat in context.user_data['selected_categories']:
            context.user_data['selected_categories'].remove(selected_cat)
        else:
            context.user_data['selected_categories'].add(selected_cat)

        categories = ["family","sexual assult","addition","friendship","relation ship","couples","truama","mental",
                      "sexual","crush","rape","harassment","school","collage","university","highschool","others"]
        category_keyboard = []
        for i in range(0, len(categories), 3):
            row = []
            for cat in categories[i:i+3]:
                display = f"✅ {cat}" if cat in context.user_data['selected_categories'] else cat
                row.append(InlineKeyboardButton(display, callback_data=f"category_{cat}"))
            category_keyboard.append(row)
        if len(context.user_data['selected_categories']) >= 3:
            category_keyboard.append([InlineKeyboardButton("✅ Submit Confession", callback_data="final_submit")])
        category_keyboard.append([InlineKeyboardButton("⬅ Back", callback_data="review_confess")])
        await query.edit_message_text(f"Selected categories: {', '.join(context.user_data['selected_categories'])}", reply_markup=InlineKeyboardMarkup(category_keyboard))

    elif query.data == "final_submit":
        if len(context.user_data.get('selected_categories', [])) < 3:
            await query.answer("Please select at least 3 hashtags.", show_alert=True)
            return

        confession_text = context.user_data.get('confession', '')
        hashtags = ' '.join([f"#{cat.replace(' ', '')}" for cat in context.user_data['selected_categories']])
        final_text = f"{confession_text}\n\n{hashtags}"

        # Save confession to DB
        confession_id = add_confession(user_id, final_text)

        # Send to admin group for approval
        await send_to_admin(confession_id, final_text, user_id, context)

        await query.edit_message_text("Your confession has been sent to admins for approval.")
        context.user_data.pop('confession', None)
        context.user_data.pop('selected_categories', None)
        context.user_data['confessions'] = get_or_create_user(user_id).get('confessions', [])

    # View confession from channel post (via deep link)
    elif query.data.startswith("view_confession_"):
        confession_id = int(query.data.replace("view_confession_", ""))
        confession = get_confession_by_id(confession_id)
        
        if confession:
            comments_count = comments_collection.count_documents({"confession_id": confession_id})
            
            confession_text = confession.get('text', '')
            message_text = f"📄 Confession #{confession_id}\n\n{confession_text}\n\n💬 Comments: {comments_count}"
            
            buttons = [
                [InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment_{confession_id}")],
                [InlineKeyboardButton("📝 View Comments", callback_data=f"view_comments_{confession_id}")],
                [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
            ]
            
            await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            await query.edit_message_text("Confession not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]]))

    # Add comment to confession
    elif query.data.startswith("add_comment_"):
        confession_id = int(query.data.replace("add_comment_", ""))
        context.user_data['commenting_on'] = confession_id
        context.user_data['commenting'] = True
        context.user_data['is_reply'] = False  # Regular comment, not a reply
        
        buttons = [[InlineKeyboardButton("❌ Cancel", callback_data=f"view_confession_{confession_id}")]]
        await query.edit_message_text("Please send your comment:", reply_markup=InlineKeyboardMarkup(buttons))

    # View comments for confession - EACH COMMENT AS SEPARATE MESSAGE (OLDEST FIRST, NEW AT BOTTOM)
    elif query.data.startswith("view_comments_"):
        confession_id = int(query.data.replace("view_comments_", ""))
        
        # Get all comments and replies for this confession (OLDEST FIRST)
        regular_comments, replies_by_parent, total_comments = get_comments_for_confession(confession_id)
        
        if not regular_comments and not replies_by_parent:
            # Show message that there are no comments
            await query.edit_message_text(
                f"📝 Comments for Confession #{confession_id}\n\nNo comments yet. Be the first to comment!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment_{confession_id}")],
                    [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")],
                    [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
                ])
            )
        else:
            # Send a header message
            await query.edit_message_text(
                f"📝 Showing comments for Confession #{confession_id} (Oldest first, newest at bottom):",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Add Comment", callback_data=f"add_comment_{confession_id}")],
                    [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")],
                    [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
                ])
            )
            
            # Send each comment as a separate message (oldest first, newest at bottom)
            for comment in regular_comments:
                comment_data = get_comment_with_user_info(comment['comment_id'], user_id)
                if comment_data:
                    # Send the main comment
                    await send_single_comment(comment_data, confession_id, query)
                    
                    # Send replies to this comment if any
                    parent_comment_id = comment['comment_id']
                    if parent_comment_id in replies_by_parent:
                        for reply in replies_by_parent[parent_comment_id]:
                            reply_data = get_comment_with_user_info(reply['comment_id'], user_id)
                            if reply_data:
                                # Get parent comment info for display
                                parent_info = get_comment_with_user_info(parent_comment_id, user_id)
                                await send_single_comment(reply_data, confession_id, query, is_reply=True, parent_comment_info=parent_info)

    # Handle like on comment
    elif query.data.startswith("like_comment_"):
        comment_id = int(query.data.replace("like_comment_", ""))
        result, new_likes, new_dislikes = handle_comment_reaction(comment_id, user_id, "like")
        
        # Find confession_id from the comment
        comment = comments_collection.find_one({"comment_id": comment_id})
        if comment:
            confession_id = comment["confession_id"]
            is_reply = comment.get("is_reply", False)
            parent_comment_id = comment.get("parent_comment_id")
            
            # Update the specific comment message
            comment_data = get_comment_with_user_info(comment_id, user_id)
            if comment_data:
                # Get parent comment info if this is a reply
                parent_comment_info = None
                if is_reply and parent_comment_id:
                    parent_comment_info = get_comment_with_user_info(parent_comment_id, user_id)
                
                # Edit the message with updated like count
                display_text = format_comment_display(comment_data, is_reply, parent_comment_info)
                
                # Create buttons with updated counts
                reply_count = comment_data.get('reply_count', 0)
                comment_buttons = [
                    InlineKeyboardButton(f"👍 {new_likes}", callback_data=f"like_comment_{comment_id}"),
                    InlineKeyboardButton(f"👎 {new_dislikes}", callback_data=f"dislike_comment_{comment_id}"),
                    InlineKeyboardButton(f"💬 Reply ({reply_count})", callback_data=f"reply_comment_{comment_id}")
                ]
                
                # Action buttons below the comment
                action_buttons = [
                    [InlineKeyboardButton("💬 Add New Comment", callback_data=f"add_comment_{confession_id}")],
                    [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")],
                    [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
                ]
                
                # Combine all buttons
                all_buttons = [comment_buttons]
                all_buttons.extend(action_buttons)
                
                # Edit the message
                await query.edit_message_text(
                    display_text,
                    reply_markup=InlineKeyboardMarkup(all_buttons)
                )

    # Handle dislike on comment
    elif query.data.startswith("dislike_comment_"):
        comment_id = int(query.data.replace("dislike_comment_", ""))
        result, new_likes, new_dislikes = handle_comment_reaction(comment_id, user_id, "dislike")
        
        # Find confession_id from the comment
        comment = comments_collection.find_one({"comment_id": comment_id})
        if comment:
            confession_id = comment["confession_id"]
            is_reply = comment.get("is_reply", False)
            parent_comment_id = comment.get("parent_comment_id")
            
            # Update the specific comment message
            comment_data = get_comment_with_user_info(comment_id, user_id)
            if comment_data:
                # Get parent comment info if this is a reply
                parent_comment_info = None
                if is_reply and parent_comment_id:
                    parent_comment_info = get_comment_with_user_info(parent_comment_id, user_id)
                
                # Edit the message with updated dislike count
                display_text = format_comment_display(comment_data, is_reply, parent_comment_info)
                
                # Create buttons with updated counts
                reply_count = comment_data.get('reply_count', 0)
                comment_buttons = [
                    InlineKeyboardButton(f"👍 {new_likes}", callback_data=f"like_comment_{comment_id}"),
                    InlineKeyboardButton(f"👎 {new_dislikes}", callback_data=f"dislike_comment_{comment_id}"),
                    InlineKeyboardButton(f"💬 Reply ({reply_count})", callback_data=f"reply_comment_{comment_id}")
                ]
                
                # Action buttons below the comment
                action_buttons = [
                    [InlineKeyboardButton("💬 Add New Comment", callback_data=f"add_comment_{confession_id}")],
                    [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")],
                    [InlineKeyboardButton("⬅ Back to Main", callback_data="back_to_main")]
                ]
                
                # Combine all buttons
                all_buttons = [comment_buttons]
                all_buttons.extend(action_buttons)
                
                # Edit the message
                await query.edit_message_text(
                    display_text,
                    reply_markup=InlineKeyboardMarkup(all_buttons)
                )
                
    # Reply to comment
    # Reply to comment
    elif query.data.startswith("reply_comment_"):
         comment_id = int(query.data.replace("reply_comment_", ""))
     
    # Get comment info for context
         comment_data = get_comment_with_user_info(comment_id, user_id)
    
         if comment_data:
        # Set reply context
               context.user_data['replying_to'] = comment_id
               context.user_data['replying'] = True
               context.user_data['commenting'] = True  # Important: This triggers the text handler
               context.user_data['is_reply'] = True
        
        # Show the comment being replied to
               display_text = format_comment_display(comment_data)
               confession_id = comment_data['confession_id']
        
        # Store confession_id for reference
               context.user_data['commenting_on'] = confession_id
          
               message_text = f"📝 Replying to:\n\n{display_text}\n\nPlease write your reply:"
               buttons = [[InlineKeyboardButton("❌ Cancel", callback_data=f"view_comments_{confession_id}")]]
               await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(buttons))
         else:
          await query.edit_message_text("Comment not found.")
     
     

    # Edit confession (from review)
    elif query.data == "edit_confess":
        context.user_data['editing'] = True
        await query.edit_message_text("Please send the edited text of your confession.")

    # Delete confession request
    elif query.data.startswith("delete_confess_"):
        confession_id = int(query.data.replace("delete_confess_", ""))
        await query.answer(f"Deletion request for confession #{confession_id} sent to admins.", show_alert=True)
        await query.edit_message_text("Deletion request sent to administrators.")

    # Admin approval/rejection
    elif query.data.startswith("approve_") or query.data.startswith("reject_"):
        confession_id = int(query.data.split("_")[1])
        status = "approved" if query.data.startswith("approve_") else "rejected"

        users_collection.update_one(
            {"confessions.confession_id": confession_id},
            {"$set": {"confessions.$.status": status}}
        )

        if status == "approved":
            user = users_collection.find_one({"confessions.confession_id": confession_id})
            confession = next((c for c in user["confessions"] if c["confession_id"] == confession_id), None)
            if confession:
                # Create URL button that opens the bot with deep link
                bot_url = f"https://t.me/{BOT_USERNAME}?start=confession_{confession_id}"
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"💬 View / Add Comments (0)", 
                        url=bot_url
                    )
                ]])
                
                # Post to channel and store message ID
                sent_message = await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=f"Confession #{confession_id}\n\n{confession['text']}",
                    reply_markup=keyboard
                )
                
                # Store channel post info
                store_channel_post(confession_id, sent_message.message_id)
                
            await query.edit_message_text(f"Confession #{confession_id} approved ✅")
        else:
            await query.edit_message_text(f"Confession #{confession_id} rejected ❌")

# Handle text messages (confessions, comments, replies, or nickname)
# Handle text messages (confessions, comments, replies, or nickname)
async def confession_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    user_id = update.effective_user.id

    if context.user_data.get('editing'):
        context.user_data['confession'] = user_text
        context.user_data['editing'] = False
        review_keyboard = [
            [InlineKeyboardButton("✅ Submit", callback_data="submit_confess")],
            [InlineKeyboardButton("✏ Edit", callback_data="edit_confess")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_confess")]
        ]
        await update.message.reply_text(f"Edited confession for review:\n\n{user_text}", reply_markup=InlineKeyboardMarkup(review_keyboard))

    elif context.user_data.get('editing_nickname'):
        context.user_data['nickname'] = user_text[:30]
        context.user_data['editing_nickname'] = False
        update_user(user_id, {"nickname": context.user_data['nickname']})
        back_button = [[InlineKeyboardButton("⬅ Back to Profile", callback_data="edit_profile")]]
        await update.message.reply_text(f"Nickname updated to: {context.user_data['nickname']}", reply_markup=InlineKeyboardMarkup(back_button))

    elif context.user_data.get('commenting'):
        # Check if this is a REPLY to a comment
        if context.user_data.get('replying') and context.user_data.get('is_reply'):
            # This is a REPLY to a comment
            parent_comment_id = context.user_data.get('replying_to')
            
            if parent_comment_id:
                # Add the reply to the comment
                reply_id, confession_id = add_reply_to_comment(parent_comment_id, user_id, user_text)
                
                if reply_id:
                    # Update the channel post button with new comment count
                    await update_channel_post_button(confession_id, context)
                    
                    # Show success message
                    await update.message.reply_text(
                        f"✅ Your reply has been added!",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📝 View Comments", callback_data=f"view_comments_{confession_id}")],
                            [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")]
                        ])
                    )
                else:
                    await update.message.reply_text("Error: Could not add reply.")
            else:
                await update.message.reply_text("Error: Could not find comment to reply to.")
            
            # Clear replying state
            context.user_data.pop('replying', None)
            context.user_data.pop('replying_to', None)
            context.user_data.pop('is_reply', None)
        
        else:
            # This is a REGULAR COMMENT on a confession
            confession_id = context.user_data.get('commenting_on')
            
            if confession_id:
                # Add regular comment
                comment_id = add_comment_to_confession(confession_id, user_id, user_text)
                
                # Update the channel post button with new comment count
                await update_channel_post_button(confession_id, context)
                
                # Show success message
                await update.message.reply_text(
                    f"✅ Your comment has been added to Confession #{confession_id}!",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📝 View Comments", callback_data=f"view_comments_{confession_id}")],
                        [InlineKeyboardButton("📄 View Confession", callback_data=f"view_confession_{confession_id}")]
                    ])
                )
            else:
                await update.message.reply_text("Error: Could not find confession to comment on.")
        
        # Clear commenting state
        context.user_data.pop('commenting', None)
        context.user_data.pop('commenting_on', None)

    else:
        # If none of the above, treat as a new confession
        context.user_data['confession'] = user_text
        review_keyboard = [
            [InlineKeyboardButton("✅ Submit", callback_data="submit_confess")],
            [InlineKeyboardButton("✏ Edit", callback_data="edit_confess")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_confess")]
        ]
        await update.message.reply_text(f"Here is your confession for review:\n\n{user_text}", reply_markup=InlineKeyboardMarkup(review_keyboard))
# Main function
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, confession_text))
    app.run_polling()

if __name__ == "__main__":
    main()