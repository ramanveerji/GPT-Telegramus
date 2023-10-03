"""
 Copyright (C) 2022 Fern Lane, GPT-Telegramus
 Licensed under the GNU Affero General Public License, Version 3.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
       https://www.gnu.org/licenses/agpl-3.0.en.html
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
 IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY CLAIM, DAMAGES OR
 OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
 ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
 OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import multiprocessing
import threading
import time
from typing import List, Dict

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters, CallbackQueryHandler

import LoggingHandler
import ProxyAutomation
import QueueHandler
import RequestResponseContainer
import UsersHandler
from main import __version__

# User commands
BOT_COMMAND_START = "start"
BOT_COMMAND_HELP = "help"
BOT_COMMAND_CHATGPT = "chatgpt"
BOT_COMMAND_EDGEGPT = "edgegpt"
BOT_COMMAND_DALLE = "dalle"
BOT_COMMAND_BARD = "bard"
BOT_COMMAND_BING_IMAGEGEN = "bingigen"
BOT_COMMAND_MODULE = "module"
BOT_COMMAND_STYLE = "style"
BOT_COMMAND_CLEAR = "clear"
BOT_COMMAND_LANG = "lang"
BOT_COMMAND_CHAT_ID = "chatid"

# Admin-only commands
BOT_COMMAND_ADMIN_QUEUE = "queue"
BOT_COMMAND_ADMIN_RESTART = "restart"
BOT_COMMAND_ADMIN_USERS = "users"
BOT_COMMAND_ADMIN_BAN = "ban"
BOT_COMMAND_ADMIN_UNBAN = "unban"
BOT_COMMAND_ADMIN_BROADCAST = "broadcast"

# List of markdown chars to escape with \\
MARKDOWN_ESCAPE = ["_", "*", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
MARKDOWN_ESCAPE_MINIMUM = ["_", "[", "]", "(", ")", "~", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]
MARKDOWN_MODE_ESCAPE_NONE = 0
MARKDOWN_MODE_ESCAPE_MINIMUM = 1
MARKDOWN_MODE_ESCAPE_ALL = 2
MARKDOWN_MODE_NO_MARKDOWN = 3

# After how many seconds restart bot polling if error occurs
RESTART_ON_ERROR_DELAY = 30

# How long to wait to clear conversation
CLEAR_CONVERSATION_TIMEOUT_S = 20


def build_menu(buttons, n_cols=1, header_buttons=None, footer_buttons=None):
    """
    Returns a list of inline buttons used to generate inlinekeyboard responses
    :param buttons: list of InlineKeyboardButton
    :param n_cols: Number of columns (number of list of buttons)
    :param header_buttons: First button value
    :param footer_buttons: Last button value
    :return: list of inline buttons
    """
    buttons = [button for button in buttons if button is not None]
    menu = [buttons[i: i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return menu


def async_helper(awaitable_) -> None:
    """
    Runs async function inside sync
    :param awaitable_:
    :return:
    """
    # Try to get current event loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    # Check it
    if loop and loop.is_running():
        loop.create_task(awaitable_)

    # We need new event loop
    else:
        asyncio.run(awaitable_)


async def send_message_async(config: dict, messages: List[Dict],
                             request_response: RequestResponseContainer.RequestResponseContainer,
                             end=False):
    """
    Sends new message or edits current one
    :param config:
    :param messages:
    :param request_response:
    :param end:
    :return:
    """
    try:
        # Get user language
        lang = UsersHandler.get_key_or_none(request_response.user, "lang", 0)

        # Fix empty message
        if end:
            if not request_response.response \
                    or (type(request_response.response) == list and len(request_response.response) == 0) \
                    or (type(request_response.response) == str and len(request_response.response.strip()) <= 0):
                request_response.response = messages[lang]["empty_message"]

        # Reset message parts if new response is smaller than previous one (EdgeGPT API bug)
        # TODO: Fix API code instead
        if len(request_response.response) < request_response.response_raw_len_last:
            request_response.response_part_positions = [0]
            request_response.response_part_counter = 0
        request_response.response_raw_len_last = len(request_response.response)

        # Split large response into parts (by index)
        if type(request_response.response) == str and len(request_response.response) > 0:
            while True:
                index_start = request_response.response_part_positions[-1]
                response_part_length = len(request_response.response[index_start:])
                if response_part_length > config["telegram"]["one_message_limit"]:
                    request_response.response_part_positions \
                        .append(index_start + config["telegram"]["one_message_limit"])
                else:
                    break

        # The last message
        if end:
            # Generate regenerate button
            button_regenerate = InlineKeyboardButton(messages[lang]["button_regenerate"],
                                                     callback_data="{0}_regenerate_{1}".format(
                                                         request_response.request_type,
                                                         request_response.reply_message_id))
            buttons = [button_regenerate]

            # Generate continue button (for ChatGPT only)
            if request_response.request_type == RequestResponseContainer.REQUEST_TYPE_CHATGPT:
                # Check if there is no error
                if not request_response.error:
                    button_continue = InlineKeyboardButton(messages[lang]["button_continue"],
                                                           callback_data="{0}_continue_{1}".format(
                                                               request_response.request_type,
                                                               request_response.reply_message_id))
                    buttons.append(button_continue)

            # Add clear button for all modules except DALL-E and Bing ImageGen
            if request_response.request_type not in [
                RequestResponseContainer.REQUEST_TYPE_DALLE,
                RequestResponseContainer.REQUEST_TYPE_BING_IMAGEGEN,
            ]:
                button_clear = InlineKeyboardButton(messages[lang]["button_clear"],
                                                    callback_data="{0}_clear_{1}".format(
                                                        request_response.request_type,
                                                        request_response.reply_message_id))
                buttons.append(button_clear)

            # Add change style button for EdgeGPT
            if request_response.request_type == RequestResponseContainer.REQUEST_TYPE_EDGEGPT:
                button_style = InlineKeyboardButton(messages[lang]["button_style_change"],
                                                    callback_data="{0}_style_{1}".format(
                                                        request_response.request_type,
                                                        request_response.reply_message_id))
                buttons.append(button_style)

            # Add change module button for all modules
            button_module = InlineKeyboardButton(messages[lang]["button_module"],
                                                 callback_data="-1_module_{0}".format(
                                                     request_response.reply_message_id))
            buttons.append(button_module)

            # Construct markup
            request_response.reply_markup = InlineKeyboardMarkup(build_menu(buttons, n_cols=2))

            # Send message as image
            if (
                request_response.request_type
                in [
                    RequestResponseContainer.REQUEST_TYPE_DALLE,
                    RequestResponseContainer.REQUEST_TYPE_BING_IMAGEGEN,
                ]
                and not request_response.error
            ):
                # Single photo
                if type(request_response.response) == str:
                    request_response.message_id = (await (telegram.Bot(config["telegram"]["api_key"]).sendPhoto(
                        chat_id=request_response.user["user_id"],
                        photo=request_response.response,
                        reply_to_message_id=request_response
                        .reply_message_id,
                        reply_markup=request_response.reply_markup))) \
                        .message_id

                else:
                    media_group = [InputMediaPhoto(media=url) for url in request_response.response]
                    # Send it
                    media_group_message_id = (await (telegram.Bot(config["telegram"]["api_key"]).sendMediaGroup(
                        chat_id=request_response.user["user_id"],
                        media=media_group,
                        reply_to_message_id=request_response.reply_message_id)))[0].message_id

                    # Send reply markup and get message ID
                    request_response.message_id = await send_reply(config["telegram"]["api_key"],
                                                                   request_response.user["user_id"],
                                                                   messages[lang]["media_group_response"]
                                                                   .format(request_response.request),
                                                                   media_group_message_id,
                                                                   markdown=False,
                                                                   reply_markup=request_response.reply_markup,
                                                                   edit_message_id=request_response.message_id)

            else:
                await _send_text_async_split(config, request_response, end)

        else:
            # Get current time
            time_current = time.time()

            # It's time to edit message, and we have any text to send, and we have new text
            if time_current - request_response.response_send_timestamp_last \
                    >= config["telegram"]["edit_message_every_seconds_num"] \
                    and len(request_response.response.strip()) > 0 \
                    and (request_response.response_len_last <= 0 or len(request_response.response.strip())
                         != request_response.response_len_last):

                # Generate stop button if it's the first message
                if request_response.message_id is None or request_response.message_id < 0:
                    button_stop = InlineKeyboardButton(messages[lang]["button_stop_generating"],
                                                       callback_data="{0}_stop_{1}".format(
                                                           request_response.request_type,
                                                           request_response.reply_message_id))
                    request_response.reply_markup = InlineKeyboardMarkup(build_menu([button_stop]))

                await _send_text_async_split(config, request_response, end)

                # Save new data
                request_response.response_len_last = len(request_response.response.strip())
                request_response.response_send_timestamp_last = time_current

    except Exception as e:
        logging.warning("Error sending message!", exc_info=e)

    # Save current timestamp to container
    request_response.response_timestamp = time.time()


async def _send_text_async_split(config: dict,
                                 request_response: RequestResponseContainer.RequestResponseContainer,
                                 end=False):
    """
    Sends text in multiple messages if needed (must be previously split)
    :param config:
    :param request_response:
    :param end:
    :return:
    """
    # Send all parts of message
    response_part_counter_init = request_response.response_part_counter
    while True:
        # Get current part of response
        response_part_index_start \
            = request_response.response_part_positions[request_response.response_part_counter]
        response_part_index_stop = len(request_response.response)
        if request_response.response_part_counter < len(request_response.response_part_positions) - 1:
            response_part_index_stop \
                = request_response.response_part_positions[request_response.response_part_counter + 1]
        response_part \
            = request_response.response[response_part_index_start:response_part_index_stop].strip()

        # Get message ID to reply to
        reply_to_id = request_response.reply_message_id
        if request_response.message_id >= 0 and request_response.response_part_counter > 0:
            reply_to_id = request_response.message_id

        edit_id = None
        # Edit last message if first loop enter
        if response_part_counter_init == request_response.response_part_counter:
            edit_id = request_response.message_id

        # Check if it is not empty
        if len(response_part) > 0:
            # Send with markup and exit from loop if it's the last part
            if response_part_index_stop == len(request_response.response):
                # Add cursor symbol?
                if not end and config["telegram"]["add_cursor_symbol"]:
                    response_part += config["telegram"]["cursor_symbol"]

                request_response.message_id = await send_reply(config["telegram"]["api_key"],
                                                               request_response.user["user_id"],
                                                               response_part,
                                                               reply_to_id,
                                                               markdown=True,
                                                               reply_markup=request_response.reply_markup,
                                                               edit_message_id=edit_id)
                break
            # Send as new message without markup and increment counter
            else:
                request_response.message_id = await send_reply(config["telegram"]["api_key"],
                                                               request_response.user["user_id"],
                                                               response_part,
                                                               reply_to_id,
                                                               markdown=True,
                                                               reply_markup=None,
                                                               edit_message_id=edit_id)
                request_response.response_part_counter += 1

        # Exit from loop if no response in current part
        else:
            break


async def send_reply(api_key: str, chat_id: int, message: str, reply_to_message_id: int | None,
                     markdown=False, reply_markup=None, edit_message_id=None):
    """
    Sends reply to chat
    :param api_key: Telegram bot API key
    :param chat_id: Chat id to send to
    :param message: Message to send
    :param reply_to_message_id: Message ID to reply on
    :param markdown: True to parse as markdown
    :param reply_markup: Buttons
    :param edit_message_id: Set message id to edit it instead of sending a new one
    :return: message_id if sent correctly, or None if not
    """
    # Send as markdown
    if not markdown:
        return await _send_parse(api_key, chat_id, message, reply_to_message_id,
                                 MARKDOWN_MODE_NO_MARKDOWN, reply_markup, edit_message_id)
    # MARKDOWN_MODE_ESCAPE_NONE
    message_id = await _send_parse(api_key, chat_id, message, reply_to_message_id,
                                   MARKDOWN_MODE_ESCAPE_NONE, reply_markup, edit_message_id)
    if message_id is None or message_id < 0:
        # MARKDOWN_MODE_ESCAPE_MINIMUM
        message_id = await _send_parse(api_key, chat_id, message, reply_to_message_id,
                                       MARKDOWN_MODE_ESCAPE_MINIMUM, reply_markup, edit_message_id)
        if message_id is None or message_id < 0:
            # MARKDOWN_MODE_ESCAPE_ALL
            message_id = await _send_parse(api_key, chat_id, message, reply_to_message_id,
                                           MARKDOWN_MODE_ESCAPE_ALL, reply_markup, edit_message_id)
            if message_id is None or message_id < 0:
                # MARKDOWN_MODE_NO_MARKDOWN
                message_id = await _send_parse(api_key, chat_id, message, reply_to_message_id,
                                               MARKDOWN_MODE_NO_MARKDOWN, reply_markup, edit_message_id)
                if message_id is None or message_id < 0:
                    raise Exception("Unable to send message in any markdown escape mode!")
                else:
                    return message_id
            else:
                return message_id
        else:
            return message_id
    else:
        return message_id


async def _send_parse(api_key: str, chat_id: int, message: str, reply_to_message_id: int | None, escape_mode: int,
                      reply_markup, edit_message_id):
    """
    Parses message and sends it as reply
    :param api_key:
    :param chat_id:
    :param message:
    :param reply_to_message_id:
    :param escape_mode:
    :param reply_markup:
    :param edit_message_id:
    :return: message_id if sent correctly, or None if not
    """
    try:
        # Escape some chars
        if escape_mode == MARKDOWN_MODE_ESCAPE_MINIMUM:
            for i in range(len(MARKDOWN_ESCAPE_MINIMUM)):
                escape_char = MARKDOWN_ESCAPE_MINIMUM[i]
                message = message.replace(escape_char, "\\" + escape_char)

        # Escape all chars
        elif escape_mode == MARKDOWN_MODE_ESCAPE_ALL:
            for i in range(len(MARKDOWN_ESCAPE)):
                escape_char = MARKDOWN_ESCAPE[i]
                message = message.replace(escape_char, "\\" + escape_char)

        # Create parse mode
        parse_mode = None if escape_mode == MARKDOWN_MODE_NO_MARKDOWN else "MarkdownV2"

        # Send as new message
        if edit_message_id is None or edit_message_id < 0:
            message_id = (await telegram.Bot(api_key).sendMessage(chat_id=chat_id,
                                                                  text=message,
                                                                  reply_to_message_id=reply_to_message_id,
                                                                  parse_mode=parse_mode,
                                                                  reply_markup=reply_markup,
                                                                  disable_web_page_preview=True)).message_id

        # Edit current message
        else:
            message_id = (await telegram.Bot(api_key).editMessageText(chat_id=chat_id,
                                                                      text=message,
                                                                      message_id=edit_message_id,
                                                                      parse_mode=parse_mode,
                                                                      reply_markup=reply_markup,
                                                                      disable_web_page_preview=True)).message_id

        # Seems OK
        return message_id

    except Exception as e:
        if escape_mode < MARKDOWN_MODE_NO_MARKDOWN:
            logging.warning("Error sending reply with escape_mode {0}: {1}\t You can ignore this message"
                            .format(escape_mode, str(e)))
        else:
            logging.error(
                f"Error sending reply with escape_mode {escape_mode}!",
                exc_info=e,
            )
        return None


async def _send_safe(chat_id: int, text: str, context: ContextTypes.DEFAULT_TYPE,
                     reply_to_message_id=None, reply_markup=None):
    """
    Sends message without raising any error
    :param chat_id:
    :param text:
    :param context:
    :param reply_to_message_id:
    :param reply_markup:
    :return:
    """
    try:
        await context.bot.send_message(chat_id=chat_id,
                                       text=text.replace("\\n", "\n").replace("\\t", "\t"),
                                       reply_to_message_id=reply_to_message_id,
                                       reply_markup=reply_markup,
                                       disable_web_page_preview=True)
    except Exception as e:
        logging.error("Error sending {0} to {1}!".format(text.replace("\\n", "\n").replace("\\t", "\t"), chat_id),
                      exc_info=e)


def clear_conversation_process(logging_queue: multiprocessing.Queue, str_or_exception_queue: multiprocessing.Queue,
                               request_type: int, config: dict, messages: List[Dict], proxy: str,
                               users_handler, user: dict, chatgpt_module, bard_module, edgegpt_module) -> None:
    """
    Clears conversation with user (must be called in new process)
    :param logging_queue:
    :param str_or_exception_queue:
    :param request_type:
    :param config:
    :param messages:
    :param proxy:
    :param users_handler:
    :param user:
    :param chatgpt_module:
    :param bard_module:
    :param edgegpt_module:
    :return:
    """
    # Setup logging for current process
    LoggingHandler.worker_configurer(logging_queue)

    try:
        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Clear ChatGPT
        if request_type == RequestResponseContainer.REQUEST_TYPE_CHATGPT:
            requested_module = messages[lang]["modules"][0]
            if chatgpt_module.processing_flag.value:
                raise Exception("The module is busy. Please try again later!")

            proxy_ = proxy if proxy and config["chatgpt"]["proxy"] == "auto" else None
            chatgpt_module.initialize(proxy_)
            chatgpt_module.clear_conversation_for_user(users_handler, user)
            chatgpt_module.exit()
        elif request_type == RequestResponseContainer.REQUEST_TYPE_EDGEGPT:
            requested_module = messages[lang]["modules"][2]
            if not edgegpt_module.processing_flag.value:
                edgegpt_module.clear_conversation_for_user(user)
            else:
                raise Exception("The module is busy. Please try again later!")

        elif request_type == RequestResponseContainer.REQUEST_TYPE_BARD:
            requested_module = messages[lang]["modules"][3]
            if not bard_module.processing_flag.value:
                bard_module.clear_conversation_for_user(user)
            else:
                raise Exception("The module is busy. Please try again later!")

        else:
            raise Exception(f"Wrong module type: {request_type}")

        # Return module name if everything is OK
        str_or_exception_queue.put(requested_module)

    except Exception as e:
        str_or_exception_queue.put(e)


class BotHandler:
    def __init__(self, config: dict, messages: List[Dict],
                 users_handler: UsersHandler.UsersHandler,
                 queue_handler: QueueHandler.QueueHandler,
                 proxy_automation: ProxyAutomation.ProxyAutomation,
                 logging_queue: multiprocessing.Queue,
                 chatgpt_module, bard_module, edgegpt_module):
        self.config = config
        self.messages = messages
        self.users_handler = users_handler
        self.queue_handler = queue_handler
        self.proxy_automation = proxy_automation
        self.logging_queue = logging_queue

        self.chatgpt_module = chatgpt_module
        self.bard_module = bard_module
        self.edgegpt_module = edgegpt_module

        self._application = None
        self._event_loop = None
        self._restart_requested_flag = False
        self._exit_flag = False
        self._response_loop_thread = None

    def start_bot(self):
        """
        Starts bot (blocking)
        :return:
        """
        # Start response_loop as thread
        # self._response_loop_thread = threading.Thread(target=self._response_loop)
        # self._response_loop_thread.start()
        # logging.info("response_loop thread: {0}".format(self._response_loop_thread.name))

        # Start telegram bot polling
        logging.info("Starting telegram bot")
        while True:
            try:
                # Build bot
                builder = ApplicationBuilder().token(self.config["telegram"]["api_key"])
                self._application = builder.build()

                # User commands
                self._application.add_handler(CommandHandler(BOT_COMMAND_START, self.bot_command_start))
                self._application.add_handler(CommandHandler(BOT_COMMAND_HELP, self.bot_command_help))
                self._application.add_handler(CommandHandler(BOT_COMMAND_CHATGPT, self.bot_command_chatgpt))
                self._application.add_handler(CommandHandler(BOT_COMMAND_EDGEGPT, self.bot_command_edgegpt))
                self._application.add_handler(CommandHandler(BOT_COMMAND_DALLE, self.bot_command_dalle))
                self._application.add_handler(CommandHandler(BOT_COMMAND_BARD, self.bot_command_bard))
                self._application.add_handler(CommandHandler(BOT_COMMAND_BING_IMAGEGEN, self.bot_command_bing_imagegen))
                self._application.add_handler(CommandHandler(BOT_COMMAND_MODULE, self.bot_command_module))
                self._application.add_handler(CommandHandler(BOT_COMMAND_STYLE, self.bot_command_style))
                self._application.add_handler(CommandHandler(BOT_COMMAND_CLEAR, self.bot_command_clear))
                self._application.add_handler(CommandHandler(BOT_COMMAND_LANG, self.bot_command_lang))
                self._application.add_handler(CommandHandler(BOT_COMMAND_CHAT_ID, self.bot_command_chatid))

                # Handle requests as messages
                if self.config["telegram"]["reply_to_messages"]:
                    self._application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.bot_message))

                # Admin commands
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_QUEUE, self.bot_command_queue))
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_RESTART, self.bot_command_restart))
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_USERS, self.bot_command_users))
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_BAN, self.bot_command_ban))
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_UNBAN, self.bot_command_unban))
                self._application.add_handler(CommandHandler(BOT_COMMAND_ADMIN_BROADCAST, self.bot_command_broadcast))

                # Unknown command -> send help
                self._application.add_handler(MessageHandler(filters.COMMAND, self.bot_command_help))

                # Add buttons handler
                self._application.add_handler(CallbackQueryHandler(self.query_callback))

                # Start bot
                self._event_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._event_loop)
                self._event_loop.run_until_complete(self._application.run_polling())

            # Exit requested
            except KeyboardInterrupt:
                logging.warning("KeyboardInterrupt @ bot_start")
                break

            # Bot error?
            except Exception as e:
                if "Event loop is closed" in str(e):
                    if not self._restart_requested_flag:
                        logging.warning("Stopping telegram bot")
                        break
                else:
                    logging.error("Telegram bot error!", exc_info=e)

            # Wait before restarting if needed
            if not self._restart_requested_flag:
                logging.info("Restarting bot polling after {0} seconds".format(RESTART_ON_ERROR_DELAY))
                try:
                    time.sleep(RESTART_ON_ERROR_DELAY)
                # Exit requested while waiting for restart
                except KeyboardInterrupt:
                    logging.warning("KeyboardInterrupt @ bot_start")
                    break

            # Restart bot
            logging.info("Restarting bot polling")
            self._restart_requested_flag = False

        # If we're here, exit requested
        logging.warning("Telegram bot stopped")

    async def query_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        reply_markup buttons callback
        :param update:
        :param context:
        :return:
        """
        try:
            telegram_chat_id = update.effective_chat.id
            data_ = update.callback_query.data
            if telegram_chat_id and data_:
                # Parse data from button
                data_splitted = data_.split("_")
                request_type = int(data_splitted[0])
                action = data_splitted[1]
                reply_message_id = int(data_splitted[2])

                # Get user
                user = self.users_handler.get_user_by_id(telegram_chat_id)

                # Exit if banned
                if user["banned"]:
                    return

                # Get user language
                lang = UsersHandler.get_key_or_none(user, "lang", 0)

                # Regenerate request
                if action == "clear":
                    await self.bot_command_clear_raw(request_type, user, context)

                elif action == "continue":
                    # Get last message ID
                    reply_message_id_last = UsersHandler.get_key_or_none(user, "reply_message_id_last")

                    # Check if it is last message
                    if reply_message_id_last and reply_message_id_last == reply_message_id:
                        # Ask
                        await self.bot_command_or_message_request_raw(request_type,
                                                                      self.config["chatgpt"]["continue_request_text"],
                                                                      user,
                                                                      reply_message_id_last,
                                                                      context)

                    # Message is not the last one
                    else:
                        await _send_safe(user["user_id"], self.messages[lang]["continue_error_not_last"], context)

                elif action == "lang":
                    await self.bot_command_lang_raw(request_type, user, context)

                elif action == "module":
                    await self.bot_command_module_raw(request_type, user, context)

                elif action == "regenerate":
                    # Get last message ID
                    reply_message_id_last = UsersHandler.get_key_or_none(user, "reply_message_id_last")

                    # Check if it is last message
                    if reply_message_id_last and reply_message_id_last == reply_message_id:
                        if request := UsersHandler.get_key_or_none(
                            user, "request_last"
                        ):
                            # Ask
                            await self.bot_command_or_message_request_raw(request_type,
                                                                          request,
                                                                          user,
                                                                          reply_message_id_last,
                                                                          context)

                        else:
                            await _send_safe(user["user_id"], self.messages[lang]["regenerate_error_empty"], context)

                    else:
                        await _send_safe(user["user_id"], self.messages[lang]["regenerate_error_not_last"], context)

                elif action == "stop":
                    # Get last message ID
                    reply_message_id_last = UsersHandler.get_key_or_none(user, "reply_message_id_last")

                    # Check if it is last message
                    if reply_message_id_last and reply_message_id_last == reply_message_id:
                        # Get queue as list
                        with self.queue_handler.lock:
                            queue_list = QueueHandler.queue_to_list(self.queue_handler.request_response_queue)

                        # Try to find out container
                        aborted = False
                        for container in queue_list:
                            if container.user["user_id"] == user["user_id"] \
                                        and container.reply_message_id == reply_message_id_last:
                                # Change state to aborted
                                logging.info(f"Requested container {container.id} abort")
                                container.processing_state = RequestResponseContainer.PROCESSING_STATE_CANCEL
                                QueueHandler.put_container_to_queue(self.queue_handler.request_response_queue,
                                                                    self.queue_handler.lock, container)
                                aborted = True
                                break

                        # Check if we aborted request
                        if not aborted:
                            await _send_safe(user["user_id"], self.messages[lang]["stop_error"], context)

                    else:
                        await _send_safe(user["user_id"], self.messages[lang]["stop_error_not_last"], context)

                elif action == "style":
                    await self.bot_command_style_raw(reply_message_id, user, context)

        except Exception as e:
            logging.error("Query callback error!", exc_info=e)

    async def bot_command_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /broadcast command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/broadcast command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check for admin rules
        if not user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["permissions_deny"], context)
            return

        # Check for message
        if not context.args or len(context.args) < 1:
            await _send_safe(user["user_id"], self.messages[lang]["broadcast_no_message"], context)
            return

        # Send initial message
        await _send_safe(user["user_id"], self.messages[lang]["broadcast_initiated"], context)

        # Get message
        broadcast_message = " ".join(context.args).strip()

        # Get list of users
        users = self.users_handler.read_users()

        # List of successful users
        broadcast_ok_users = []

        # Broadcast to non-banned users
        for broadcast_user in users:
            if not broadcast_user["banned"]:
                try:
                    # Try to send message and get message ID
                    message_id = (await telegram.Bot(self.config["telegram"]["api_key"]).sendMessage(
                        chat_id=broadcast_user["user_id"],
                        text=self.messages[lang]["broadcast"].replace("\\n", "\n").format(
                            broadcast_message))).message_id

                    # Check
                    if message_id is not None and message_id != 0:
                        logging.info("Message sent to: {0} ({1})".format(broadcast_user["user_name"],
                                                                         broadcast_user["user_id"]))
                        broadcast_ok_users.append(broadcast_user["user_name"])

                    # Wait some time
                    time.sleep(self.config["telegram"]["broadcast_delay_per_user_seconds"])
                except Exception as e:
                    logging.warning(
                        f'Error sending message to {broadcast_user["user_id"]}!',
                        exc_info=e,
                    )

        # Send final message
        await _send_safe(user["user_id"],
                         self.messages[lang]["broadcast_done"].format("\n".join(broadcast_ok_users)),
                         context)

    async def bot_command_ban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.bot_command_ban_unban(True, update, context)

    async def bot_command_unban(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.bot_command_ban_unban(False, update, context)

    async def bot_command_ban_unban(self, ban: bool, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /ban, /unban commands
        :param ban: True to ban, False to unban
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/{0} command from {1} ({2})".format("ban" if ban else "unban",
                                                          user["user_name"],
                                                          user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check for admin rules
        if not user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["permissions_deny"], context)
            return

        # Check user_id to ban
        if not context.args or len(context.args) < 1:
            await _send_safe(user["user_id"], self.messages[lang]["ban_no_user_id"], context)
            return
        try:
            ban_user_id = int(str(context.args[0]).strip())
        except Exception as e:
            await _send_safe(user["user_id"], str(e), context)
            return

        # Get ban reason
        reason = self.messages[lang]["ban_reason_default"].replace("\\n", "\n")
        if len(context.args) > 1:
            reason = " ".join(context.args[1:]).strip()

        # Get user to ban
        banned_user = self.users_handler.get_user_by_id(ban_user_id)

        # Ban / unban
        banned_user["banned"] = ban
        banned_user["ban_reason"] = reason

        # Save user
        self.users_handler.save_user(banned_user)

        # Send confirmation
        if ban:
            await _send_safe(user["user_id"],
                             self.messages[lang]["ban_message_admin"].format("{0} ({1})"
                                                                             .format(banned_user["user_name"],
                                                                                     banned_user["user_id"]), reason),
                             context)
        else:
            await _send_safe(user["user_id"],
                             self.messages[lang]["unban_message_admin"].format("{0} ({1})"
                                                                               .format(banned_user["user_name"],
                                                                                       banned_user["user_id"])),
                             context)

    async def bot_command_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /users command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/users command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check for admin rules
        if not user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["permissions_deny"], context)
            return

        # Get list of users
        users = self.users_handler.read_users()

        # Sort by number of requests
        users = sorted(users, key=lambda u: u["requests_total"], reverse=True)

        # Add them to message
        message = ""
        for user_info in users:
            # Banned?
            message += "B " if user_info["banned"] else "  "
            # Admin?
            message += "A " if user_info["admin"] else "  "
            # Language
            message += self.messages[UsersHandler.get_key_or_none(user_info, "lang", 0)]["language_icon"] + " "

            # Module
            message += self.messages[0]["module_icons"][UsersHandler.get_key_or_none(user_info, "module", 0)] + " "

            # User ID, name, total requests
            message += "{0} ({1}) - {2}\n".format(user_info["user_id"], user_info["user_name"],
                                                  user_info["requests_total"])

        # Parse as monospace
        message = self.messages[lang]["users_admin"].format(message).replace("\\t", "\t").replace("\\n", "\n")
        message = "```\n" + message + "\n```"

        # Send list of users as markdown
        await send_reply(self.config["telegram"]["api_key"],
                         user["user_id"],
                         message,
                         None,
                         markdown=True)

    async def bot_command_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /restart command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/restart command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check for admin rules
        if not user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["permissions_deny"], context)
            return

        # Send restarting message
        logging.info("Restarting")
        await _send_safe(user["user_id"], self.messages[lang]["restarting"], context)

        # Stop proxy automation
        logging.info("Stopping ProxyAutomation")
        self.proxy_automation.stop_automation_loop()

        # Make sure queue is empty
        if self.queue_handler.request_response_queue.qsize() > 0:
            logging.info("Waiting for all requests to finish")
            while self.queue_handler.request_response_queue.qsize() > 0:
                # Cancel all active containers
                with self.queue_handler.lock:
                    queue_list = QueueHandler.queue_to_list(self.queue_handler.request_response_queue)
                    for container in queue_list:
                        container.processing_state = RequestResponseContainer.PROCESSING_STATE_CANCEL

                # Check every 100ms
                time.sleep(0.1)

        # Start proxy automation
        logging.info("Starting back ProxyAutomation")
        self.proxy_automation.start_automation_loop()

        # Restart telegram bot
        self._restart_requested_flag = True
        self._event_loop.stop()
        try:
            self._event_loop.close()
        except:
            pass

        def send_message_after_restart():
            # Sleep while restarting
            while self._restart_requested_flag:
                time.sleep(1)

            # Done?
            logging.info("Restarting done")
            try:
                asyncio.run(telegram.Bot(self.config["telegram"]["api_key"])
                            .sendMessage(chat_id=user["user_id"],
                                         text=self.messages[lang]["restarting_done"].replace("\\n", "\n")))
            except Exception as e:
                logging.error("Error sending message!", exc_info=e)

        threading.Thread(target=send_message_after_restart).start()

    async def bot_command_queue(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /queue command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/queue command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check for admin rules
        if not user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["permissions_deny"], context)
            return

        # Get queue as list
        with self.queue_handler.lock:
            queue_list = QueueHandler.queue_to_list(self.queue_handler.request_response_queue)

        # Queue is empty
        if len(queue_list) == 0:
            await _send_safe(user["user_id"], self.messages[lang]["queue_empty"], context)

        else:
            message = ""
            for container_counter, container in enumerate(queue_list, start=1):
                text_to = RequestResponseContainer.REQUEST_NAMES[container.request_type]
                request_status = RequestResponseContainer.PROCESSING_STATE_NAMES[container.processing_state]
                message_ = "{0} ({1}). {2} ({3}) to {4} ({5}): {6}\n".format(container_counter,
                                                                             container.id,
                                                                             container.user["user_name"],
                                                                             container.user["user_id"],
                                                                             text_to,
                                                                             request_status,
                                                                             container.request)
                message += message_
            # Send queue content
            await _send_safe(user["user_id"], message, context)

    async def bot_command_chatid(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /chatid command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/chatid command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Send chat id and not exit if banned
        await _send_safe(user["user_id"], str(user["user_id"]), context)

    async def bot_command_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /clear command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/clear command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get requested module
        requested_module = -1
        if context.args and len(context.args) >= 1:
            try:
                requested_module = int(context.args[0].strip().lower())
            except Exception as e:
                logging.error("Error retrieving requested module!", exc_info=e)
                lang = UsersHandler.get_key_or_none(user, "lang", 0)
                await _send_safe(user["user_id"], self.messages[lang]["clear_error"].format(e), context)
                return

        # Clear
        await self.bot_command_clear_raw(requested_module, user, context)

    async def bot_command_clear_raw(self, request_type: int, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Clears conversation
        :param request_type:
        :param user:
        :param context:
        :return:
        """
        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Create buttons for module selection
        if request_type < 0:
            buttons = []
            if self.config["modules"]["chatgpt"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][0], callback_data="0_clear_0"))
            if self.config["modules"]["edgegpt"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][2], callback_data="2_clear_0"))
            if self.config["modules"]["bard"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][3], callback_data="3_clear_0"))

            # If at least one module is available
            if buttons:
                await _send_safe(user["user_id"], self.messages[lang]["clear_select_module"], context,
                                 reply_markup=InlineKeyboardMarkup(build_menu(buttons)))
            return

        # Clear conversation
        try:
            # Queue for result
            str_or_exception_queue = multiprocessing.Queue(maxsize=1)

            # Create process
            process = multiprocessing.Process(target=clear_conversation_process, args=(self.logging_queue,
                                                                                       str_or_exception_queue,
                                                                                       request_type,
                                                                                       self.config,
                                                                                       self.messages,
                                                                                       self.proxy_automation
                                                                                       .working_proxy,
                                                                                       self.users_handler,
                                                                                       user,
                                                                                       self.chatgpt_module,
                                                                                       self.bard_module,
                                                                                       self.edgegpt_module))

            # Start and join with timeout
            process.start()
            process.join(timeout=CLEAR_CONVERSATION_TIMEOUT_S)

            # Timeout
            if process.is_alive():
                process.terminate()
                process.join()
                raise Exception("Timed out")

            # Finished
            else:
                if str_or_exception_queue.qsize() > 0:
                    str_or_exception = str_or_exception_queue.get()

                    # Seems OK
                    if type(str_or_exception) == str:
                        await _send_safe(user["user_id"], self.messages[lang]["chat_cleared"].format(str_or_exception),
                                         context)

                    # Exception
                    else:
                        raise str_or_exception

        # Error deleting conversation
        except Exception as e:
            logging.error("Error clearing conversation!", exc_info=e)
            await _send_safe(user["user_id"], self.messages[lang]["clear_error"].format(e), context)
            return

    async def bot_command_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /style command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/style command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Get requested style
        style = -1
        if context.args and len(context.args) >= 1:
            try:
                style = int(context.args[0].strip().lower())
            except Exception as e:
                logging.error("Error retrieving requested style!", exc_info=e)
                lang = UsersHandler.get_key_or_none(user, "lang", 0)
                await _send_safe(user["user_id"], self.messages[lang]["style_change_error"].format(e), context)
                return

        # Clear
        await self.bot_command_style_raw(style, user, context)

    async def bot_command_style_raw(self, style: int, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Changes conversation style of EdgeGPT
        :param style:
        :param user:
        :param context:
        :return:
        """
        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Create buttons for style selection
        if style < 0 or style > 2:
            buttons = [InlineKeyboardButton(self.messages[lang]["style_precise"], callback_data="2_style_0"),
                       InlineKeyboardButton(self.messages[lang]["style_balanced"], callback_data="2_style_1"),
                       InlineKeyboardButton(self.messages[lang]["style_creative"], callback_data="2_style_2")]

            # Extract current style
            current_style = UsersHandler.get_key_or_none(user, "edgegpt_style")

            # Get default key instead
            if current_style is None:
                current_style = self.config["edgegpt"]["conversation_style_type_default"]

            # Get as string
            if current_style == 0:
                current_style_ = self.messages[lang]["style_precise"]
            elif current_style == 1:
                current_style_ = self.messages[lang]["style_balanced"]
            else:
                current_style_ = self.messages[lang]["style_creative"]

            await _send_safe(user["user_id"], self.messages[lang]["style_select"].format(current_style_), context,
                             reply_markup=InlineKeyboardMarkup(build_menu(buttons)))
            return

        # Change style
        try:
            # Change style of user
            user["edgegpt_style"] = style
            self.users_handler.save_user(user)

            # Send confirmation
            if style == 0:
                changed_style = self.messages[lang]["style_precise"]
            elif style == 1:
                changed_style = self.messages[lang]["style_balanced"]
            else:
                changed_style = self.messages[lang]["style_creative"]
            await _send_safe(user["user_id"], self.messages[lang]["style_changed"].format(changed_style), context)

        # Error changing style
        except Exception as e:
            logging.error("Error changing conversation style!", exc_info=e)
            await _send_safe(user["user_id"], self.messages[lang]["style_change_error"].format(e), context)
            return

    async def bot_command_module(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /module command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/module command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Request module selection
        await self.bot_command_module_raw(-1, user, context)

    async def bot_command_module_raw(self, request_type: int, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Suggest module selection to the user or changes user's module
        :param request_type: <0 for module selection
        :param user:
        :param context:
        :return:
        """
        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Change module
        if request_type >= 0:
            await self.bot_command_or_message_request_raw(request_type, "", user, -1, context)

        else:
            buttons = []
            if self.config["modules"]["chatgpt"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][0], callback_data="0_module_0"))
            if self.config["modules"]["dalle"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][1], callback_data="1_module_0"))
            if self.config["modules"]["edgegpt"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][2], callback_data="2_module_0"))
            if self.config["modules"]["bard"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][3], callback_data="3_module_0"))
            if self.config["modules"]["bing_imagegen"]:
                buttons.append(InlineKeyboardButton(self.messages[lang]["modules"][4], callback_data="4_module_0"))

            # Extract current module
            current_module = self.messages[lang]["modules"][user["module"]]

            # If at least one module is available
            if buttons:
                await _send_safe(user["user_id"], self.messages[lang]["module_select_module"].format(current_module),
                                 context,
                                 reply_markup=InlineKeyboardMarkup(build_menu(buttons)))
            return

    async def bot_command_lang(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        /lang command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/lang command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Request module selection
        await self.bot_command_lang_raw(-1, user, context)

    async def bot_command_lang_raw(self, lang_index: int, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Selects user language
        :param lang_index: <0 for language selection
        :param user:
        :param context:
        :return:
        """
        # Create buttons for language selection
        if lang_index < 0 or lang_index > len(self.messages):
            # Create language and buttons prompt
            buttons = []
            language_select_text = ""
            for i in range(len(self.messages)):
                buttons.append(
                    InlineKeyboardButton(
                        self.messages[i]["language_name"],
                        callback_data=f"{i}_lang_0",
                    )
                )
                language_select_text += self.messages[i]["language_select"] + "\n"

            await _send_safe(user["user_id"], language_select_text, context,
                             reply_markup=InlineKeyboardMarkup(build_menu(buttons)))
            return

        # Change language
        try:
            # Change language of user
            user["lang"] = lang_index
            self.users_handler.save_user(user)

            # Send confirmation
            await _send_safe(user["user_id"], self.messages[lang_index]["language_changed"], context)

            # Send start message if it is a new user
            user_started = UsersHandler.get_key_or_none(user, "started")
            if not user_started:
                await self.bot_command_start_raw(user, context)

        # Error changing lang
        except Exception as e:
            logging.error("Error selecting language!", exc_info=e)
            await _send_safe(user["user_id"], self.messages[0]["language_select_error"].format(e), context)

    async def bot_command_chatgpt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(RequestResponseContainer.REQUEST_TYPE_CHATGPT, update, context)

    async def bot_command_edgegpt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(RequestResponseContainer.REQUEST_TYPE_EDGEGPT, update, context)

    async def bot_command_dalle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(RequestResponseContainer.REQUEST_TYPE_DALLE, update, context)

    async def bot_command_bard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(RequestResponseContainer.REQUEST_TYPE_BARD, update, context)

    async def bot_command_bing_imagegen(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(RequestResponseContainer.REQUEST_TYPE_BING_IMAGEGEN, update, context)

    async def bot_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.bot_command_or_message_request(-1, update, context)

    async def bot_command_or_message_request(self, request_type: int,
                                             update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /chatgpt, /edgegpt, /dalle, /bard, /bingigen or message request
        :param request_type: -1 for message, or RequestResponseContainer.REQUEST_TYPE_...
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command or message
        if request_type == RequestResponseContainer.REQUEST_TYPE_CHATGPT:
            logging.info("/chatgpt command from {0} ({1})".format(user["user_name"], user["user_id"]))
        elif request_type == RequestResponseContainer.REQUEST_TYPE_EDGEGPT:
            logging.info("/edgegpt command from {0} ({1})".format(user["user_name"], user["user_id"]))
        elif request_type == RequestResponseContainer.REQUEST_TYPE_DALLE:
            logging.info("/dalle command from {0} ({1})".format(user["user_name"], user["user_id"]))
        elif request_type == RequestResponseContainer.REQUEST_TYPE_BARD:
            logging.info("/bard command from {0} ({1})".format(user["user_name"], user["user_id"]))
        elif request_type == RequestResponseContainer.REQUEST_TYPE_BING_IMAGEGEN:
            logging.info("/bingigen command from {0} ({1})".format(user["user_name"], user["user_id"]))
        else:
            logging.info("Text message from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Extract request
        if request_type >= 0:
            request_message = " ".join(context.args).strip() if context.args else ""
        else:
            request_message = update.message.text.strip()

        # Process request
        await self.bot_command_or_message_request_raw(request_type,
                                                      request_message,
                                                      user,
                                                      update.message.message_id,
                                                      context)

    async def bot_command_or_message_request_raw(self, request_type: int,
                                                 request_message: str,
                                                 user: dict,
                                                 reply_message_id: int,
                                                 context: ContextTypes.DEFAULT_TYPE):
        """
        Processes request to module
        :param request_type:
        :param request_message:
        :param user:
        :param reply_message_id:
        :param context:
        :return:
        """
        # Set default user module
        if request_type >= 0:
            user["module"] = request_type
            self.users_handler.save_user(user)

        else:
            # Automatically adjust message module
            request_type = user["module"]

        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Check request
        if not request_message or len(request_message) <= 0:
            # Module changed
            await _send_safe(user["user_id"],
                             self.messages[lang]["empty_request_module_changed"]
                             .format(self.messages[lang]["modules"][request_type]), context)
            return

        # Check queue size
        if self.queue_handler.request_response_queue.qsize() >= self.config["telegram"]["queue_max"]:
            await _send_safe(user["user_id"], self.messages[lang]["queue_overflow"], context)
            return

        # Create request timestamp (for data collecting)
        request_timestamp = ""
        if self.config["data_collecting"]["enabled"]:
            request_timestamp = datetime.datetime.now().strftime(self.config["data_collecting"]["timestamp_format"])

        # Create request
        request_response = RequestResponseContainer.RequestResponseContainer(user,
                                                                             reply_message_id=reply_message_id,
                                                                             request=request_message,
                                                                             request_type=request_type,
                                                                             request_timestamp=request_timestamp)

        # Add request to the queue
        logging.info("Adding new request with type {0} from {1} ({2}) to the queue".format(request_type,
                                                                                           user["user_name"],
                                                                                           user["user_id"]))
        QueueHandler.put_container_to_queue(self.queue_handler.request_response_queue,
                                            self.queue_handler.lock,
                                            request_response)

        # Send confirmation if queue size is more than 1
        with self.queue_handler.lock:
            queue_list = QueueHandler.queue_to_list(self.queue_handler.request_response_queue)
            if len(queue_list) > 1:
                await _send_safe(user["user_id"],
                                 self.messages[lang]["queue_accepted"].format(
                                     self.messages[lang]["modules"][request_type],
                                     len(queue_list),
                                     self.config["telegram"]["queue_max"]),
                                 context,
                                 reply_to_message_id=request_response.reply_message_id)

    async def bot_command_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /help command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/help command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned
        if user["banned"]:
            return

        # Send help message
        await self.bot_command_help_raw(user, context)

    async def bot_command_help_raw(self, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Sends help message to the user
        :param user:
        :param context:
        :return:
        """
        # Get user language
        lang = UsersHandler.get_key_or_none(user, "lang", 0)

        # Send default help message
        await _send_safe(user["user_id"], self.messages[lang]["help_message"], context)

        # Send admin help message
        if user["admin"]:
            await _send_safe(user["user_id"], self.messages[lang]["help_message_admin"], context)

    async def bot_command_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        /start command
        :param update:
        :param context:
        :return:
        """
        # Get user
        user = await self._user_check_get(update, context)

        # Log command
        logging.info("/start command from {0} ({1})".format(user["user_name"], user["user_id"]))

        # Exit if banned or user not selected the language
        if user["banned"] or UsersHandler.get_key_or_none(user, "lang") is None:
            return

        # Send start message
        await self.bot_command_start_raw(user, context)

    async def bot_command_start_raw(self, user: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Sends start message to teh user
        :param user:
        :param context:
        :return:
        """
        # Send start message
        lang = UsersHandler.get_key_or_none(user, "lang", 0)
        await _send_safe(user["user_id"], self.messages[lang]["start_message"].format(__version__), context)

        # Send help message
        await self.bot_command_help_raw(user, context)

        # Save that user received this message
        user["started"] = True
        self.users_handler.save_user(user)

    async def _user_check_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> dict:
        """
        Gets (or creates) user based on update.effective_chat.id and update.message.from_user.full_name
        and checks if they are banned or not
        :param update:
        :param context:
        :return: user as dictionary
        """
        # Get user (or create a new one)
        telegram_user_name = update.message.from_user.full_name if update.message is not None else None
        telegram_chat_id = update.effective_chat.id
        user = self.users_handler.get_user_by_id(telegram_chat_id)

        # Update user name
        if telegram_user_name is not None:
            user["user_name"] = str(telegram_user_name)
            self.users_handler.save_user(user)

        # Send banned info
        if user["banned"]:
            lang = UsersHandler.get_key_or_none(user, "lang", 0)
            await _send_safe(telegram_chat_id,
                             self.messages[lang]["ban_message_user"].format(user["ban_reason"]),
                             context)

        # Ask for user to select the language
        else:
            lang = UsersHandler.get_key_or_none(user, "lang")
            if lang is None or lang < 0:
                await self.bot_command_lang_raw(-1, user, context)

        return user
