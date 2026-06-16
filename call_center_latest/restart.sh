sudo systemctl restart asterisk-ws.service
sudo journalctl -u asterisk-ws.service -f -n 200