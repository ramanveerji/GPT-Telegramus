"""
 Copyright (C) 2023 Fern Lane, GPT-Telegramus
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

import logging
import multiprocessing
from typing import List, Dict

import JSONReaderWriter

DEFAULT_USER_NAME = "Noname"


def get_key_or_none(dictionary: dict, key, default_value=None):
    """
    Safely gets value of key from dictionary
    :param dictionary:
    :param key:
    :param default_value: default value if key not found
    :return: key value or default_value if not found
    """
    if key is None:
        return default_value

    if key in dictionary:
        return default_value if dictionary[key] is None else dictionary[key]
    return default_value


class UsersHandler:
    def __init__(self, config: dict, messages: List[Dict]):
        self.config = config
        self.messages = messages

        self.lock = multiprocessing.Lock()

    def read_users(self) -> list:
        """
        Reads users data from database
        :return: users as list of dictionaries or [] if not found
        """
        with self.lock:
            users = JSONReaderWriter.load_json(self.config["files"]["users_database"])
            return [] if users is None else users

    def get_user_by_id(self, user_id: int) -> dict:
        """
        Returns user (or create new one) as dictionary from database using user_id
        :param user_id:
        :return: dictionary
        """
        users = self.read_users()
        for user in users:
            if user["user_id"] == user_id:
                return user

        # If we are here then user doesn't exist
        return self._create_user(user_id)

    def save_user(self, user_data: dict) -> None:
        """
        Saves user_data to database
        :param user_data:
        :return:
        """
        if user_data is None:
            return

        users = self.read_users()

        with self.lock:
            user_index = next(
                (
                    i
                    for i in range(len(users))
                    if users[i]["user_id"] == user_data["user_id"]
                ),
                -1,
            )
            # User exists
            if user_index >= 0:
                new_keys = user_data.keys()
                for new_key in new_keys:
                    users[user_index][new_key] = user_data[new_key]

            # New user
            else:
                users.append(user_data)

            # Save to database
            JSONReaderWriter.save_json(self.config["files"]["users_database"], users)

    def _create_user(self, user_id: int) -> dict:
        """
        Creates and saves new user
        :return:
        """
        logging.info("Creating new user with id: {0}".format(user_id))
        user = {
            "user_id": user_id,
            "user_name": DEFAULT_USER_NAME,
            "admin": user_id in self.config["telegram"]["admin_ids"],
            "banned": self.config["telegram"]["ban_by_default"],
            "ban_reason": self.messages[0]["ban_reason_default"].replace(
                "\\n", "\n"
            ),
            "module": self.config["modules"]["default_module"],
            "requests_total": 0,
            "reply_message_id_last": -1,
        }
        self.save_user(user)
        return user
