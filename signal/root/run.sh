#!/bin/bash

CONFIG_PATH=/data/options.json
PHONE_NUMBER=$(jq --raw-output ".phone_number" ${CONFIG_PATH})
SIGNAL_CONFIG_PATH=$(jq --raw-output ".signal_config_path" ${CONFIG_PATH})
LOG_LEVEL=$(jq --raw-output ".log_level" ${CONFIG_PATH})

export PHONE_NUMBER
export SIGNAL_CONFIG_PATH
export LOG_LEVEL

dbus-uuidgen --ensure=/etc/machine-id
dbus-daemon --system --nopidfile
cd /app || exit
gunicorn -w 1 -b 0.0.0.0:5000 wsgi:app