#!/bin/bash

USER="Sherlok260"
TOKEN="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"
REPO="github.com/Sherlok260/asterisk-ari.git"

git stash && \
git pull https://$USER:$TOKEN@$REPO && \
sudo chmod -R 777 ./ast_ws_client.py && \
sh restart.sh