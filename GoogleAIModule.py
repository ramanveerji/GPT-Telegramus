import time
import uuid
import json
import os
import multiprocessing
import ctypes
import asyncio
import logging
import requests
from typing import List, Dict
import google.generativeai as genai

from google.generativeai.client import (  # pylint: disable=no-name-in-module
    _ClientManager,
)

import BotHandler
import UsersHandler
from RequestResponseContainer import RequestResponseContainer


class GoogleAIModule:
    def __init__(
        self,
        config: Dict,
        config_key: str,
        messages: List[Dict],
        users_handler: UsersHandler.UsersHandler,
    ) -> None:
        self.config = config
        self.config_key = config_key
        self.messages = messages
        self.users_handler = users_handler

        # All variables here must be multiprocessing
        self.cancel_requested = multiprocessing.Value(ctypes.c_bool, False)
        self.processing_flag = multiprocessing.Value(ctypes.c_bool, False)
        self._last_request_time = multiprocessing.Value(ctypes.c_double, 0.0)

        self._enabled = False
        self._model = None

    def initialize(self) -> None:
        """
        Initializes Google AI module using the generative language API: https://ai.google.dev/api
        This method must be called from another process
        :return:
        """
        # Internal variables for current process
        self._enabled = False
        self._model = None

        self.processing_flag.value = False
        self.cancel_requested.value = False

        try:
            # Log
            logging.info("Initializing Google AI module with proxy")

            # Set enabled status
            self._enabled = self.config["modules"][self.config_key]
            if not self._enabled:
                logging.warning("Google AI module disabled in config file!")
                raise Exception("Google AI module disabled in config file!")

            # Set up the model
            generation_config = {
                "temperature": 0.9,
                "top_p": 1,
                "top_k": 1,
                "max_output_tokens": 2048,
            }
            safety_settings = []
            self._model = genai.GenerativeModel(
                model_name="gemini-pro",
                generation_config=generation_config,
                safety_settings=safety_settings,
            )
            self._vision_model = genai.GenerativeModel(
                model_name="gemini-pro-vision",
                generation_config=generation_config,
                safety_settings=safety_settings,
            )

            client_manager = _ClientManager()
            client_manager.configure(api_key=self.config[self.config_key]["api_key"])
            self._model._client = client_manager.get_default_client(
                "generative"
            )  # pylint: disable=protected-access
            self._vision_model._client = client_manager.get_default_client(
                "generative"
            )  # pylint: disable=protected-access
            logging.info("Google AI module initialized")

        # Error
        except Exception as e:
            self._enabled = False
            raise e

    def process_request(self, request_response: RequestResponseContainer) -> None:
        """
        Processes request to Google AI
        :param request_response: RequestResponseContainer object
        :return:
        """
        lang = UsersHandler.get_key_or_none(request_response.user, "lang", 0)
        conversations_dir = self.config["files"]["conversations_dir"]
        conversation_id = UsersHandler.get_key_or_none(
            request_response.user, f"{self.config_key}_conversation_id"
        )

        # Check if we are initialized
        if not self._enabled:
            logging.error("Google AI module not initialized!")
            request_response.response = (
                self.messages[lang]["response_error"]
                .replace("\\n", "\n")
                .format("Google AI module not initialized!")
            )
            request_response.error = True
            self.processing_flag.value = False
            return

        try:
            # Set flag that we are currently processing request
            self.processing_flag.value = True

            # Cool down
            if (
                time.time() - self._last_request_time.value
                <= self.config[self.config_key]["cooldown_seconds"]
            ):
                logging.warning(
                    "Too frequent requests. Waiting {0} seconds...".format(
                        int(
                            self.config[self.config_key]["cooldown_seconds"]
                            - (time.time() - self._last_request_time.value)
                        )
                    )
                )
                time.sleep(
                    self._last_request_time.value
                    + self.config[self.config_key]["cooldown_seconds"]
                    - time.time()
                )
            self._last_request_time.value = time.time()

            # Try to load conversation
            has_image, conversation = _load_conversation(
                conversations_dir, conversation_id
            ) or [False, []]
            # Generate new random conversation ID
            if conversation_id is None:
                conversation_id = str(uuid.uuid4())

            request = [request_response.request]
            # Try to download image
            if request_response.image_url:
                has_image = True
                logging.info("Downloading user image")
                image = requests.get(request_response.image_url, timeout=120)
                content_type = image.headers.get("content-type")
                if content_type is not None:
                    request = [{"mime_type": content_type, "data": image.content}]

            conversation.append({"role": "user", "parts": request})

            logging.info("Asking Gemini...")
            response = (
                self._vision_model if has_image else self._model
            ).generate_content(conversation, stream=True)
            for chunk in response:
                if self.cancel_requested.value:
                    break
                request_response.response += chunk.text
                BotHandler.async_helper(
                    BotHandler.send_message_async(
                        self.config, self.messages, request_response, end=False
                    )
                )

            if not self.cancel_requested.value:
                conversation.append({"role": "model", "parts": response.text})

                if not _save_conversation(
                    conversations_dir, conversation_id, [has_image, conversation]
                ):
                    conversation_id = None
                request_response.user[
                    f"{self.config_key}_conversation_id"
                ] = conversation_id
                self.users_handler.save_user(request_response.user)
            else:
                logging.info("Gemini module canceled")

        # Error
        except Exception as e:
            self._enabled = False
            raise e

        self.processing_flag.value = False
        # Finish message
        BotHandler.async_helper(
            BotHandler.send_message_async(
                self.config, self.messages, request_response, end=True
            )
        )

    def clear_conversation_for_user(self, user: dict) -> None:
        """
        Clears conversation (chat history) for selected user
        :param user_handler:
        :param user:
        :return: True if cleared successfully
        """
        if not self._enabled:
            return
        conversation_id = UsersHandler.get_key_or_none(
            user, f"{self.config_key}_conversation_id"
        )
        if conversation_id is None:
            return

        # Delete from API
        _delete_conversation(self.config["files"]["conversations_dir"], conversation_id)

        # Delete from user
        user[f"{self.config_key}_conversation_id"] = None
        self.user_handler.save_user(user)


def _load_conversation(conversations_dir, conversation_id):
    """
    Loads conversation
    :param conversations_dir:
    :param conversation_id:
    :return: Content of conversation, None if error
    """
    logging.info("Loading conversation {0}".format(conversation_id))
    try:
        if conversation_id is None:
            logging.info("conversation_id is None. Skipping loading")
            return None

        # API type 3
        conversation_file = os.path.join(conversations_dir, conversation_id + ".json")
        if os.path.exists(conversation_file):
            # Load from json file
            with open(conversation_file, "r", encoding="utf-8") as json_file:
                return json.load(json_file)
        else:
            logging.warning("File {0} not exists!".format(conversation_file))

    except Exception as e:
        logging.warning(
            "Error loading conversation {0}".format(conversation_id), exc_info=e
        )

    return None


def _save_conversation(conversations_dir, conversation_id, conversation) -> bool:
    """
    Saves conversation
    :param conversations_dir:
    :param conversation_id:
    :param conversation:
    :return: True if no error
    """
    print(conversation)
    logging.info("Saving conversation {0}".format(conversation_id))
    try:
        if conversation_id is None:
            logging.info("conversation_id is None. Skipping saving")
            return False

        # Save as json file
        conversation_file = os.path.join(conversations_dir, conversation_id + ".json")
        with open(conversation_file, "w", encoding="utf-8") as json_file:
            json.dump(conversation, json_file, indent=4)

    except Exception as e:
        logging.error(
            "Error saving conversation {0}".format(conversation_id), exc_info=e
        )
        return False

    return True


def _delete_conversation(conversations_dir, conversation_id) -> bool:
    """
    Deletes conversation
    :param conversation_id:
    :return:
    """
    logging.info("Deleting conversation " + conversation_id)
    # Delete conversation file if exists
    try:
        conversation_file = os.path.join(conversations_dir, conversation_id + ".json")
        if os.path.exists(conversation_file):
            logging.info("Deleting {0} file".format(conversation_file))
            os.remove(conversation_file)
        return True

    except Exception as e:
        logging.error(
            "Error removing conversation file for conversation {0}".format(
                conversation_id
            ),
            exc_info=e,
        )

    return False
