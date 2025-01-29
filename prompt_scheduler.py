"""
title: Prompt Scheduler
author: Dan James
author_url: https://github.com/hello-dan-codes
funding_url: https://github.com/open-webui
version: 0.1
requirements: apscheduler
license: MIT

This function allows you to schedule prompts to be sent to a model at a specified time. Use cron syntax to set run frequency.

Install:
    - pip install apscheduler.
    - Enable function via the "Admin Panel".
    - Create new chat using "Prompt Scheduler" model and send the following command: !help
"""

import requests
import time
import uuid
from datetime import timedelta
from typing import List, Union, Generator, Iterator
from pydantic import BaseModel
from utils.misc import get_last_user_message

from open_webui.config import WEBUI_URL
from open_webui.models.models import Models
from open_webui.models.users import Users
from open_webui.models.chats import ChatForm, Chats
from open_webui.utils.auth import create_token

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger

VERSION = "0.1"
VERSION_REF = VERSION.replace(".", "")
VERSION_ID = f"prompt-scheduler-v{VERSION_REF}"

jobstores = {
    'default': SQLAlchemyJobStore(url=f"sqlite:///{VERSION_ID}.sqlite")
}
scheduler = AsyncIOScheduler()
scheduler.configure(jobstores=jobstores)
scheduler.start()


class Pipe:
    HELP_MESSAGE = """
 Commands:
     !add <cron> <model> <prompt>
         - Add a prompt to the scheduler. Use cron syntax to set run frequency.
         - Example: !add <0 0 12 * *> <gpt4> <Hello world>
         - For further information on cron syntax, visit: https://crontab.guru/

     !remove <ID>
         - Remove job by id from the scheduler.
         - Example: !remove <1234>

     !clear
         - Removes all jobs from the scheduler.

     !list
         - List all jobs in the scheduler.

     !help
         - Display this message.
"""

    class Valves(BaseModel):
        pass

    class UserJob():
        def __init__(self, job_id=None, user_id=None, model="", prompt=""):
            self.chat_id = None
            self.user_id = user_id
            self.job_id = job_id
            self.model = model
            self.prompt = prompt

        def run(self):
            timestamp = int(time.time())
            assistant_message = self.send_prompt(self.model, self.prompt)

            new_messages = [
                {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": self.prompt,
                    "timestamp": timestamp,
                    "models": [self.model]
                },
                {
                    "id": str(uuid.uuid4()),
                    "role": assistant_message['role'],
                    "content": assistant_message['content'],
                    "timestamp": timestamp,
                    "models": [self.model]
                }
            ]

            # Check if existing chat exists. If not, create new chat.
            if self.chat_id:
                chat = Chats.get_chat_by_id(self.chat_id)
                if chat is None:
                    self.chat_id = None

            # if chat_id is None, create new chat. else, update existing chat
            if self.chat_id is None:
                self.new_chat(self.model, new_messages)

                # get job scheduler by id. then remove job and readd it with updated chat_id
                job = scheduler.get_job(self.job_id)
                job_name = job.name
                job_trigger = job.trigger

                scheduler.remove_job(self.job_id)

                del job

                scheduler.add_job(self.run, job_trigger, id=self.job_id, name=job_name, max_instances=1)
            else:
                self.existing_chat(new_messages)

        def new_chat(self, model, new_messages):
            prompt = new_messages[0]['content']
            title = prompt[:50] + "..." if len(prompt) > 50 else prompt
            response = Chats.insert_new_chat(
                self.user_id,
                ChatForm(
                    **{
                        "chat": {
                            "title": title,  # TODO: title generation?
                            "messages": new_messages,
                            "models": [model]
                        }
                    }
                ),
            )
            self.chat_id = response.id

        def existing_chat(self, new_messages):
            chatmodel = Chats.get_chat_by_id(self.chat_id)

            messages = chatmodel.chat.get('messages', [])
            messages.append(new_messages[0])
            messages.append(new_messages[1])

            chatmodel.chat['messages'] = messages
            Chats.update_chat_by_id(self.chat_id, chatmodel.chat)

        def send_prompt(self, model, prompt):
            payload = {
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}]
            }

            model_info = Models.get_model_by_id(model)

            model_meta = model_info.meta

            metadata = {
                "user_id": self.user_id,
                "chat_id": self.chat_id,
                "tool_ids": model_meta.toolIds,
                "files": None,
                "features": None,  # TODO enable web_search if user has it enabled on model
            }

            payload['tool_ids'] = metadata['tool_ids']

            token = create_token(
                data={"id": self.user_id},
                expires_delta=timedelta(seconds=300),
            )

            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }

            response = requests.post(f"{WEBUI_URL}/api/chat/completions", headers=headers, json=payload)

            if response.status_code != 200:
                raise Exception(f"Error: {response.status_code}")

            output = response.json()

            return output['choices'][0]['message']

    def __init__(self):
        self.type = "manifold"
        self.id = VERSION_ID
        self.name = "Prompt Scheduler"
        self.user = Users.get_first_user()

    def pipes(self) -> List[dict]:
        return [
            {"id": self.id, "name": ""}
        ]

    def get_all_jobs(self, print_run_time=True):
        output = ""
        jobs = scheduler.get_jobs()
        for job in jobs:
            id = job.id
            name = job.name
            next_run_time = job.next_run_time
            output += f"\nID: {id}\nPrompt: {name}\n"

            if print_run_time:
                output += f"Next Run Time: {next_run_time}\n"

        return output

    def pipe(self, body: dict) -> Union[str, Generator, Iterator]:
        messages = body["messages"]

        try:
            return self.process_message(messages)
        except Exception as e:
            return f"Error: {e}"

    def process_message(self, messages: list) -> Union[str, Generator, Iterator]:
        user_message = get_last_user_message(messages)

        # If messages has no "assistant" content, return help commands at start of chat.
        if len(messages) <= 1:
            return self.HELP_MESSAGE

        if "!add" in user_message:
            cron, model, prompt = self.input_validation("add", user_message)

            # set job_name to be the first 100 characters of the prompt
            job_name = prompt[:100] + "... (Truncated)" if len(prompt) > 100 else prompt

            job_id = str(uuid.uuid4())
            user_job = self.UserJob(job_id, self.user.id, model, prompt)

            job = scheduler.add_job(user_job.run, CronTrigger.from_crontab(cron), id=job_id, name=job_name, max_instances=1)
            return f"ID: {job.id}\nPrompt: {job.name}\n\nJob added."
        elif "!remove" in user_message:
            job_id = self.input_validation("remove", user_message)
            exists = scheduler.get_job(job_id)
            if not exists:
                return "Job not found."

            scheduler.remove_job(job_id)
            return "Job removed."
        elif "!clear" in user_message:
            message = self.get_all_jobs(print_run_time=False)
            message += "\n\nJobs have been removed."
            scheduler.remove_all_jobs()
            return message
        elif "!list" in user_message:
            return self.get_all_jobs() or "No jobs found."
        elif "!help" in user_message:
            return self.HELP_MESSAGE

        return "Invalid syntax. \n\n" + self.HELP_MESSAGE

    def input_validation(self, input_type, user_message):
        """ Validate user input for the chosen command. User input should match the command syntax. """
        if input_type == "add":
            try:
                _, cron, model, prompt = user_message.split("<", 3)
                cron = cron.strip().replace(">", "")
                model = model.strip().replace(">", "")
                prompt = prompt.strip().replace(">", "")
            except ValueError:
                raise ValueError("Invalid syntax. \n\n" + self.HELP_MESSAGE)

            # Validate model
            model_info = Models.get_model_by_id(model)
            if not model_info:
                all_models = Models.get_all_models()
                list_models = ""
                for item in all_models:
                    if item.is_active:
                        list_models += f"{item.id}\n"

                raise ValueError(f"Model not found.\nAvailable models:\n{list_models}")

            # Validate cron syntax
            if len(cron.split(" ")) != 5:
                raise ValueError("Invalid syntax. \n\n" + self.HELP_MESSAGE)

            # validate cron values
            for value in cron.split(" "):
                if value == "*":
                    continue
                if not value.isnumeric() or (int(value) < 0 or int(value) > 59):
                    raise ValueError("Invalid syntax. \n\n" + self.HELP_MESSAGE)

            return cron, model, prompt
        elif input_type == "remove":
            try:
                _, job_id = user_message.split("<", 1)
                job_id = job_id.strip().replace(">", "")
            except ValueError:
                raise ValueError("Invalid syntax. \n\n" + self.HELP_MESSAGE)

            return job_id