import threading

from flask import Flask, request
import os
import asyncio
import json
import subprocess
import tempfile
import re
import socket
from signal_message import SignalMessage
from ws import send_message
import logging
import logging.config
dir_path = os.path.dirname(os.path.realpath(__file__))

SIGNAL_CLI_PATH = "/signal-cli"
group_id_matcher = re.compile(r'^[0-9a-f ]+\n$')
logging.config.fileConfig(f'{dir_path}/logging.conf')

log_levels = {
    "NOTSET": logging.NOTSET,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

retrieved_log_level = log_levels.get(os.environ.get("SIGNAL_LOG_LEVEL", "INFO").upper(), logging.INFO)
print(f'SETTING LOG LEVEL TO {retrieved_log_level}')
logging.getLogger().setLevel(retrieved_log_level)


class SignalMessageSender:
    def __init__(self, executor=subprocess):
        self.executor = executor

    @staticmethod
    def __log_message(message_to_send, attachment, number_or_group):
        text = f'Sending "{message_to_send}" to {number_or_group}'
        if attachment:
            text += f'with attachment {attachment}'
        logging.info(text)

    def send_message_to_number(self, number, message_to_send, attachment):
        SignalMessageSender.__log_message(message_to_send=message_to_send, attachment=attachment, number_or_group=number)
        my_command = self.executor.Popen(
            f'dbus-send --system --type=method_call --print-reply --dest="org.asamk.Signal" /org/asamk/Signal org.asamk.Signal.sendMessage string:"{message_to_send}" array:string:"{attachment}" string:"{number}"',
            shell=True, stdout=self.executor.PIPE)
        my_command.wait()
        logging.debug(my_command)

    def send_message_to_group(self, group, message_to_send, attachment):
        SignalMessageSender.__log_message(message_to_send=message_to_send, attachment=attachment, number_or_group=group)
        group_to_byte = ','.join([f'0x{group[i:i + 2]}' for i in range(0, len(group), 2)])
        my_command = self.executor.Popen(
            f'dbus-send --system --type=method_call --print-reply --dest="org.asamk.Signal" /org/asamk/Signal org.asamk.Signal.sendGroupMessage string:"{message_to_send}" array:string:"{attachment}" array:byte:"{group_to_byte}"',
            shell=True, stdout=self.executor.PIPE)
        my_command.wait()
        logging.debug(my_command)

    def get_groups(self):
        logging.info(f'Retrieving groups')
        groups_command = self.executor.Popen(
            f'dbus-send --system --type=method_call --print-reply --dest="org.asamk.Signal" /org/asamk/Signal org.asamk.Signal.getGroupIds',
            shell=True, stdout=self.executor.PIPE)
        groups_command.wait()
        groups = {}
        for group_id_raw in groups_command.stdout.readlines():
            group_id_decoded = group_id_raw.decode('ascii')
            if group_id_matcher.match(group_id_decoded):
                group_byte = ','.join([f'0x{i}' for i in group_id_decoded.strip().split(' ')])
                group_hexa = ''.join(group_id_decoded.strip().split(' '))
                group_name_command = self.executor.Popen(
                    f'dbus-send --system --type=method_call --print-reply=literal --dest="org.asamk.Signal" /org/asamk/Signal org.asamk.Signal.getGroupName array:byte:{group_byte}',
                    shell=True, stdout=subprocess.PIPE)
                group_name_command.wait()
                group_name = group_name_command.stdout.readline()
                logging.info(f'Name: {group_name.decode("ascii").strip()}, id: {group_hexa}')
                groups[group_name.decode("ascii").strip()] = group_hexa
        return groups


def receive_signal_messages(signal_process: subprocess.Popen, signal_messages: SignalMessage,
                            signal_sender: SignalMessageSender):
    logging.info('Listening to incoming messages')
    for line in iter(signal_process.stdout.readline, ''):
        if signal_process.poll() is not None:
            logging.error('signal has been terminated, check the logs for more information')
            for error_line in iter(signal_process.stdout.readline, b''): # b'\n'-separated lines
                logging.info(error_line)
            logging.error('signal logs printed')
            # 4 is for stopping gunicorn
            os._exit(4)
        cleaned_line = line.decode('utf8').rstrip()
        logging.debug(f'receiving new line "{cleaned_line}"')
        signal_messages.new_line_received(cleaned_line)
        message_received = signal_messages.read_message()
        if message_received != {}:
            response = asyncio.run(send_message(message_received['message']))
            signal_sender.send_message_to_number(message_received['sender'], response, "")


class SignalApplication:

    def __init__(self, executor=subprocess):
        logging.info("Init")
        self.signal_messages = SignalMessage()
        self.signal_application = executor.Popen(SignalApplication.__signal_command(["daemon", "--system"]),
                                                 stdout=subprocess.PIPE)
        self.signal_sender = SignalMessageSender(executor)
        self.receive_thread = threading.Thread(target=receive_signal_messages,
                                               args=(self.signal_application, self.signal_messages, self.signal_sender),
                                               daemon=True)
        self.receive_thread.start()
        logging.info(f'Process started and listening on {socket.gethostname()}')

    def send_message_to_number(self, number, message_to_send, attachment):
        return self.signal_sender.send_message_to_number(number, message_to_send, attachment)

    def send_message_to_group(self, group, message_to_send, attachment):
        return self.signal_sender.send_message_to_group(group, message_to_send, attachment)

    def get_groups(self):
        return self.signal_sender.get_groups()

    @staticmethod
    def __signal_command(command):
        return [f'/{SIGNAL_CLI_PATH}/bin/signal-cli',
                "--config",
                os.environ["SIGNAL_CONFIG_PATH"],
                "-u",
                os.environ["PHONE_NUMBER"],
                *command]


def app(injected_signal=None):
    if injected_signal is not None:
        signal = injected_signal
    else:
        signal = SignalApplication()

    app = Flask(__name__)

    @app.route('/group', methods=['GET'])
    def groups():
        return signal.get_groups()

    @app.route('/message', methods=['POST'])
    def message():
        json_data = request.files['json']
        message_to_send = json.loads(json_data.read())
        message_content = message_to_send['content']
        attachment = ""
        if 'file' in request.files:
            file = request.files['file']
            f = tempfile.NamedTemporaryFile(suffix=os.path.basename(file.filename))
            f.write(file.read())
            f.flush()
            attachment = f.name
        if "number" in message_to_send:
            signal.send_message_to_number(number=message_to_send["number"], message_to_send=message_content,
                                          attachment=attachment)
        if "group" in message_to_send:
            signal.send_message_to_group(group=message_to_send["group"], message_to_send=message_content,
                                         attachment=attachment)
        if 'file' in request.files:
            f.close()
        return "ok"

    # COMPATIBILITY LAYER WITH OFFICIAL HOME ASSISTANT INTEGRATION

    @app.route('/v1/send', methods=['POST'])
    def official_integration_send_message():
        response = request.get_json()
        message_to_send = response['message']
        # number = response['number']
        recipients = response['recipients']
        attachment = ""
        if 'base64_attachment' in response:
            attachment = response['base64_attachment']
        for recipient in recipients:
            if recipient.startswith('+'):
                signal.send_message_to_number(number=recipient, message_to_send=message_to_send, attachment=attachment)
            else:
                signal.send_message_to_group(group=recipient, message_to_send=message_to_send, attachment=attachment)
        return "ok"

    return app
