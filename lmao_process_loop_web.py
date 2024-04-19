"""
Copyright (C) 2023-2024 Fern Lane

This file is part of the GPT-Telegramus distribution
(see <https://github.com/F33RNI/GPT-Telegramus>)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as
published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program. If not, see <http://www.gnu.org/licenses/>.
"""

import base64
import json
import logging
import multiprocessing
import queue
import random
import string
import threading
import time
from typing import Dict

import requests
from lmao.module_wrapper import (
    STATUS_NOT_INITIALIZED,
    STATUS_INITIALIZING,
    STATUS_IDLE,
    STATUS_BUSY,
    STATUS_FAILED,
)

import logging_handler
import messages
import users_handler
from bot_sender import send_message_async
from async_helper import async_helper

# Timeout of all requests
REQUEST_TIMEOUT = 30

# Minimum allowed seconds between requests to prevent overloading API
REQUEST_COOLDOWN = 1.5

# How long to wait for module to initialize
INIT_TIMEOUT = 30

# How long to wait for module to become available
IDLE_TIMEOUT = 60

# Delay between status checks
CHECK_STATUS_INTERVAL = 10

# lmao process loop delay during idle
LMAO_LOOP_DELAY = 0.5


def _request_wrapper(
    api_url: str,
    endpoint: str,
    payload: Dict,
    cooldown_timer: Dict,
    request_lock: multiprocessing.Lock,
    proxy: str or None = None,
    token: str or None = None,
    stream: bool = False,
) -> Dict or requests.Response:
    """Wrapper for POST requests to LMAO API

    Args:
        api_url (str): API base url
        endpoint (str): ex. "status" or "init"
        payload (Dict): request payload as JSON
        cooldown_timer (multiprocessing.Value): double value to prevent "Too Many Requests"
        request_lock (multiprocessing.Lock): lock to prevent multiple process from sending multiple requests at ones
        proxy (str or None, optional): connect with proxy. Defaults to None
        token (str or None, optional): token-based authorization. Defaults to None
        stream (boor, optional). True for "ask" endpoint. Defaults to False

    Raises:
        Exception: _description_
        Exception: _description_

    Returns:
        Dict or requests.Response: parsed JSON response or response object if stream is True
    """
    # Add proxies if needed
    proxies = None
    if proxy:
        proxies = {
            "http": proxy,
            "https": proxy,
        }

    # Add token-based authorization if needed
    if token:
        payload["token"] = token

    # Set lock
    request_lock.acquire()

    try:

        # Wait if needed
        with cooldown_timer.get_lock():
            cooldown_timer_ = cooldown_timer.value
        while time.time() - cooldown_timer_ < REQUEST_COOLDOWN:
            time.sleep(0.1)

        with cooldown_timer.get_lock():
            cooldown_timer.value = time.time()

        # Send request
        response_ = requests.post(
            f"{api_url}/{endpoint}", json=payload, proxies=proxies, timeout=REQUEST_TIMEOUT, stream=stream
        )

        # Check status code
        if response_.status_code != 200:
            try:
                error = response_.json().get("error")
            except:
                error = None
            if error is not None:
                raise Exception(str(error))
            else:
                raise Exception(f"Status code: {response_.status_code}")

        # Return response itself if in stream mode
        if stream:
            return response_

        # Return parsed json otherwise
        return response_.json()

    # Release lock
    finally:
        if not stream:
            request_lock.release()


def _check_status(
    status_dict: Dict,
    api_url: str,
    cooldown_timer: Dict,
    request_lock: multiprocessing.Lock,
    proxy: str or None = None,
    token: str or None = None,
) -> None:
    """Sends POST request to api_url/status and writes status codes into status_dict

    Args:
        status_dict (Dict): each module status will be set there as "module_name": status as integer
        api_url (str): API base url
        cooldown_timer (multiprocessing.Value): double value to prevent "Too Many Requests"
        request_lock (multiprocessing.Lock): lock to prevent multiple process from sending multiple requests at ones
        proxy (str or None, optional): connect with proxy. Defaults to None
        token (str or None, optional): token-based authorization. Defaults to None
    """
    if api_url.endswith("/"):
        api_url = api_url[:-1]
    try:
        modules = _request_wrapper(api_url, "status", {}, cooldown_timer, request_lock, proxy, token)
        for module in modules:
            module_name = module.get("module")
            module_status = module.get("status_code")
            if module_name and module_status is not None and isinstance(module_status, int):
                status_dict[module_name] = module_status
    except Exception as e:
        status_dict["exception"] = e
        logging.error("Error checking status", exc_info=e)


def _status_checking_loop(
    status_dict: Dict,
    lmao_module_name: str,
    status_value: multiprocessing.Value,
    api_url: str,
    cooldown_timer: Dict,
    request_lock: multiprocessing.Lock,
    proxy: str or None = None,
    token: str or None = None,
) -> None:
    """Calls _check_status() every CHECK_STATUS_INTERVAL seconds and sets status_value to status_dict[lmao_module_name]
    set status_dict["exit"] = True to stop this

    Args:
        status_dict (Dict): each module status will be set there as "module_name": status as integer
        lmao_module_name (str): name of module to get status of (ex. "chatgpt")
        status_value (multiprocessing.Value): multiprocessing status for lmao_module_name
        api_url (str): API base url
        cooldown_timer (multiprocessing.Value): double value to prevent "Too Many Requests"
        request_lock (multiprocessing.Lock): lock to prevent multiple process from sending multiple requests at ones
        proxy (str or None, optional): connect with proxy. Defaults to None
        token (str or None, optional): token-based authorization. Defaults to None
    """
    logging.info("Status checker thread started")
    while not status_dict.get("exit"):
        _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
        status_code = status_dict.get(lmao_module_name)
        with status_value.get_lock():
            if status_code is not None:
                status_value.value = status_code
            else:
                status_value.value = STATUS_NOT_INITIALIZED
        time.sleep(CHECK_STATUS_INTERVAL)
    logging.info("Status checker thread finished")


def lmao_process_loop_web(
    name: str,
    name_lmao: str,
    config: Dict,
    messages_: messages.Messages,
    users_handler_: users_handler.UsersHandler,
    logging_queue: multiprocessing.Queue,
    lmao_process_running: multiprocessing.Value,
    lmao_stop_stream_value: multiprocessing.Value,
    lmao_module_status: multiprocessing.Value,
    lmao_delete_conversation_request_queue: multiprocessing.Queue,
    lmao_delete_conversation_response_queue: multiprocessing.Queue,
    lmao_request_queue: multiprocessing.Queue,
    lmao_response_queue: multiprocessing.Queue,
    lmao_exceptions_queue: multiprocessing.Queue,
    cooldown_timer: multiprocessing.Value,
    request_lock: multiprocessing.Lock,
) -> None:
    """Handler for LMAO web API
    (see <https://github.com/F33RNI/LlM-Api-Open> for more info)
    """
    # Setup logging for current process
    logging_handler.worker_configurer(logging_queue)
    logging.info("_lmao_process_loop_web started")

    # Parse some config
    api_url = config.get("modules").get("lmao_web_api_url")
    proxy = config.get("modules").get("lmao_web_proxy")
    token = config.get("modules").get("lmao_token_manage")

    status_dict = {}

    # Initialize module
    logging.info(f"Initializing {name}")
    try:
        # Claim status
        with lmao_module_status.get_lock():
            lmao_module_status.value = STATUS_INITIALIZING

        # Read actual status
        _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
        exception_ = status_dict.get("exception")
        if exception_ is not None:
            raise exception_
        status_code = status_dict.get(name_lmao)

        # Initialize module
        if status_code is None or status_code == STATUS_NOT_INITIALIZED:
            # Send initialization request
            logging.info(f"Requesting {name_lmao} module initialization")
            _request_wrapper(api_url, "init", {"module": name_lmao}, cooldown_timer, request_lock, proxy, token)

            # Wait
            logging.info(f"Waiting for {name_lmao} module to initialize")
            time_started = time.time()
            while True:
                if time.time() - time_started > INIT_TIMEOUT:
                    raise Exception(f"Timeout waiting for {name_lmao} module to initialize")
                _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
                exception_ = status_dict.get("exception")
                if exception_ is not None:
                    raise exception_
                status_code = status_dict.get(name_lmao)
                if status_code == STATUS_FAILED:
                    raise Exception(f"Unable to initialize module {name_lmao}. See LMAO logs for more info")
                elif status_code == STATUS_IDLE or status_code == STATUS_BUSY:
                    break
                time.sleep(1)
            logging.info(f"{name} initialized")
            with lmao_module_status.get_lock():
                lmao_module_status.value = status_code
    except Exception as e:
        logging.error(f"{name} initialization error", exc_info=e)
        with lmao_module_status.get_lock():
            lmao_module_status.value = STATUS_FAILED
        with lmao_process_running.get_lock():
            lmao_process_running.value = False
        return

    # Start status checker as thread
    logging.info("Starting status checker thread")
    status_checking_thread = threading.Thread(
        target=_status_checking_loop,
        args=(status_dict, name_lmao, lmao_module_status, api_url, cooldown_timer, request_lock, proxy, token),
    )
    status_checking_thread.start()

    # Main loop container
    request_response = None

    def _lmao_stop_stream_loop() -> None:
        """Background thread that handles stream stop signal"""
        logging.info("_lmao_stop_stream_loop started")
        while True:
            # Exit from loop
            with lmao_process_running.get_lock():
                if not lmao_process_running.value:
                    logging.warning("Exit from _lmao_stop_stream_loop requested")
                    break

            # Wait a bit to prevent overloading
            # We need to wait at the beginning to enable delay even after exception and continue statement
            time.sleep(LMAO_LOOP_DELAY)

            # Get stop request
            lmao_stop_stream = False
            with lmao_stop_stream_value.get_lock():
                if lmao_stop_stream_value.value:
                    lmao_stop_stream = True
                    lmao_stop_stream_value.value = False

            if not lmao_stop_stream:
                continue

            # Send stop request
            try:
                logging.info(f"Requesting {name_lmao} module stop response")
                _request_wrapper(api_url, "stop", {"module": name_lmao}, cooldown_timer, request_lock, proxy, token)

            # Catch process interrupts just in case
            except (SystemExit, KeyboardInterrupt):
                logging.warning("Exit from _lmao_stop_stream_loop requested")
                break

            # Stop loop error
            except Exception as e:
                logging.error("_lmao_stop_stream_loop error", exc_info=e)

            # Read module's status
            finally:
                try:
                    _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
                    exception_ = status_dict.get("exception")
                    if exception_ is not None:
                        raise exception_
                    status_code = status_dict.get(name_lmao, STATUS_NOT_INITIALIZED)
                    with lmao_module_status.get_lock():
                        lmao_module_status.value = status_code
                except Exception as e:
                    logging.warning(f"Unable to read {name_lmao} module status: {e}")

        # Done
        logging.info("_lmao_stop_stream_loop finished")

    # Start stream stop signal handler
    stop_handler_thread = threading.Thread(target=_lmao_stop_stream_loop)
    stop_handler_thread.start()

    # Main loop
    while True:
        # Exit from loop
        with lmao_process_running.get_lock():
            lmao_process_running_value = lmao_process_running.value
        if not lmao_process_running_value:
            logging.warning(f"Exit from {name} loop requested")
            break

        try:
            # Wait a bit to prevent overloading
            # We need to wait at the beginning to enable delay even after exception
            # But inside try-except to catch interrupts
            time.sleep(LMAO_LOOP_DELAY)

            # Non-blocking get of request-response container
            request_response = None
            try:
                request_response = lmao_request_queue.get(block=False)
            except queue.Empty:
                pass

            # New request
            if request_response:
                logging.info(f"Received new request to {name}")
                with lmao_module_status.get_lock():
                    lmao_module_status.value = STATUS_BUSY

                # Wait for module to become available
                logging.info(f"Waiting for IDLE status from {name_lmao} module")
                time_started = time.time()
                while True:
                    if time.time() - time_started > IDLE_TIMEOUT:
                        raise Exception(f"Timeout waiting for IDLE status from {name_lmao} module")
                    status_code = status_dict.get(name_lmao)
                    if status_code == STATUS_FAILED:
                        raise Exception(f"Error waiting for IDLE status from {name_lmao}. See LMAO logs for more info")
                    elif status_code == STATUS_IDLE:
                        break
                    time.sleep(1)
                logging.info(f"Received IDLE status from {name} module")
                with lmao_module_status.get_lock():
                    lmao_module_status.value = status_code

                # Extract request
                prompt_text = request_response.request_text
                prompt_image = request_response.request_image

                # Check prompt
                if not prompt_text:
                    raise Exception("No text request")
                else:
                    # Extract conversation ID
                    conversation_id = users_handler_.get_key(request_response.user_id, name + "_conversation_id")

                    module_request = {"prompt": prompt_text, "convert_to_markdown": True}

                    # Extract style (for lmao_ms_copilot only)
                    if name == "lmao_ms_copilot":
                        style = users_handler_.get_key(request_response.user_id, "ms_copilot_style", "balanced")
                        module_request["style"] = style

                    # Add image and conversation ID
                    if prompt_image is not None:
                        module_request["image"] = base64.b64encode(prompt_image).decode("utf-8")
                    if conversation_id:
                        module_request["conversation_id"] = conversation_id

                    # Reset suggestions
                    users_handler_.set_key(request_response.user_id, "suggestions", [])

                    # Ask and read stream
                    try:
                        for line in _request_wrapper(
                            api_url,
                            "ask",
                            {name_lmao: module_request},
                            cooldown_timer,
                            request_lock,
                            proxy,
                            token,
                            stream=True,
                        ).iter_lines():
                            if not line:
                                continue
                            try:
                                response = json.loads(line.decode("utf-8"))
                            except Exception as e:
                                logging.warning(f"Unable to parse response line as JSON: {e}")
                                continue

                            finished = response.get("finished")
                            conversation_id = response.get("conversation_id")
                            request_response.response_text = response.get("response")

                            images = response.get("images")
                            if images is not None:
                                request_response.response_images = images[:]

                            # Format and add attributions
                            attributions = response.get("attributions")
                            if attributions is not None and len(attributions) != 0:
                                response_link_format = messages_.get_message(
                                    "response_link_format", user_id=request_response.user_id
                                )
                                request_response.response_text += "\n"
                                for i, attribution in enumerate(attributions):
                                    request_response.response_text += response_link_format.format(
                                        source_name=str(i + 1), link=attribution.get("url", "")
                                    )

                            # Suggestions must be stored as tuples with unique ID for reply-markup
                            if finished:
                                suggestions = response.get("suggestions")
                                if suggestions is not None:
                                    request_response.response_suggestions = []
                                    for suggestion in suggestions:
                                        if not suggestion or len(suggestion) < 1:
                                            continue
                                        id_ = "".join(
                                            random.choices(
                                                string.ascii_uppercase + string.ascii_lowercase + string.digits, k=8
                                            )
                                        )
                                        request_response.response_suggestions.append((id_, suggestion))
                                    users_handler_.set_key(
                                        request_response.user_id,
                                        "suggestions",
                                        request_response.response_suggestions,
                                    )

                            # Check if exit was requested
                            with lmao_process_running.get_lock():
                                lmao_process_running_value = lmao_process_running.value
                            if not lmao_process_running_value:
                                finished = True

                            # Send response to the user
                            async_helper(
                                send_message_async(config.get("telegram"), messages_, request_response, end=finished)
                            )

                            # Exit from stream reader
                            if not lmao_process_running_value:
                                break

                        # Save conversation ID
                        users_handler_.set_key(request_response.user_id, name + "_conversation_id", conversation_id)

                        # Return container
                        lmao_response_queue.put(request_response)

                    # Release lock after stream stop
                    finally:
                        request_lock.release()

            # Non-blocking get of user_id to clear conversation for
            delete_conversation_user_id = None
            try:
                delete_conversation_user_id = lmao_delete_conversation_request_queue.get(block=False)
            except queue.Empty:
                pass

            # Get and delete conversation
            if delete_conversation_user_id is not None:
                with lmao_module_status.get_lock():
                    lmao_module_status.value = STATUS_BUSY

                # Wait for module to become available
                logging.info(f"Waiting for IDLE status from {name_lmao} module")
                time_started = time.time()
                while True:
                    if time.time() - time_started > IDLE_TIMEOUT:
                        raise Exception(f"Timeout waiting for IDLE status from {name_lmao} module")
                    status_code = status_dict.get(name_lmao)
                    if status_code == STATUS_FAILED:
                        raise Exception(f"Error waiting for IDLE status from {name_lmao}. See LMAO logs for more info")
                    elif status_code == STATUS_IDLE:
                        break
                    time.sleep(1)
                logging.info(f"Received IDLE status from {name} module")
                with lmao_module_status.get_lock():
                    lmao_module_status.value = status_code

                conversation_id = users_handler_.get_key(delete_conversation_user_id, name + "_conversation_id")

                try:
                    if conversation_id:
                        payload = {name_lmao: {"conversation_id": conversation_id}}
                        _request_wrapper(api_url, "delete", payload, cooldown_timer, request_lock, proxy, token)
                        users_handler_.set_key(delete_conversation_user_id, name + "_conversation_id", None)
                    lmao_delete_conversation_response_queue.put(delete_conversation_user_id)
                except Exception as e:
                    logging.error(f"Error deleting conversation for {name}", exc_info=e)
                    lmao_delete_conversation_response_queue.put(e)
                finally:
                    try:
                        _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
                        exception_ = status_dict.get("exception")
                        if exception_ is not None:
                            raise exception_
                        status_code = status_dict.get(name_lmao, STATUS_NOT_INITIALIZED)
                        with lmao_module_status.get_lock():
                            lmao_module_status.value = status_code
                    except Exception as e:
                        lmao_delete_conversation_response_queue.put(e)

        # Catch process interrupts just in case
        except (SystemExit, KeyboardInterrupt):
            logging.warning(f"Exit from {name} loop requested")
            break

        # Main loop error
        except Exception as e:
            logging.error(f"{name} error", exc_info=e)
            lmao_exceptions_queue.put(e)

    # Read module status
    try:
        _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
        exception_ = status_dict.get("exception")
        if exception_ is not None:
            raise exception_
        status_code = status_dict.get(name_lmao, STATUS_NOT_INITIALIZED)
        with lmao_module_status.get_lock():
            lmao_module_status.value = status_code
    except Exception as e:
        logging.warning(f"Unable to read {name_lmao} module status: {e}")

    # Wait for stop handler to finish
    if stop_handler_thread and stop_handler_thread.is_alive():
        logging.info("Waiting for _lmao_stop_stream_loop")
        try:
            stop_handler_thread.join()
        except Exception as e:
            logging.warning(f"Error joining _lmao_stop_stream_loop: {e}")

    # Try to close module
    logging.info(f"Trying to close {name}")
    try:
        # Request module close
        _request_wrapper(api_url, "delete", {"module": name_lmao}, cooldown_timer, request_lock, proxy, token)

        # Wait
        logging.info(f"Waiting for {name_lmao} module to close")
        time_started = time.time()
        while True:
            if time.time() - time_started > INIT_TIMEOUT:
                raise Exception(f"Timeout waiting for {name_lmao} module to close")
            _check_status(status_dict, api_url, cooldown_timer, request_lock, proxy, token)
            exception_ = status_dict.get("exception")
            if exception_ is not None:
                raise exception_
            status_code = status_dict.get(name_lmao)
            if status_code == STATUS_FAILED:
                raise Exception(f"Unable to close module {name_lmao}. See LMAO logs for more info")
            elif status_code == STATUS_IDLE or status_code == STATUS_BUSY:
                break
            time.sleep(1)
        logging.info(f"{name} initialized")
        with lmao_module_status.get_lock():
            lmao_module_status.value = status_code
    except Exception as e:
        logging.error(f"Error closing {name}", exc_info=e)

    # Stop status checker
    if status_checking_thread is not None and status_checking_thread.is_alive:
        logging.info("Stopping status checking thread")
        status_dict["exit"] = True
        logging.info("Trying to join status checking thread")
        try:
            status_checking_thread.join()
        except Exception as e:
            logging.warning(f"Unable to join status checker thread: {e}")

    # Done
    with lmao_process_running.get_lock():
        lmao_process_running.value = False
    logging.info("_lmao_process_loop_web finished")
