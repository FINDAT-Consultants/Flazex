
# ProATR Flask Application

This repository contains a production‑ready structure for the ProATR reconciliation platform.

## Local quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python run.py
```

## Gunicorn + Nginx (EC2)

1. Copy the folder to **/home/ec2-user**.
2. Create a virtualenv and install dependencies:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```
3. Copy **deploy/proatr.service** to */etc/systemd/system* and enable:
   ```bash
   sudo cp deploy/proatr.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable proatr
   sudo systemctl start proatr
   ```
4. Copy **deploy/nginx.conf** to */etc/nginx/conf.d/proatr.conf* and restart Nginx.

## Docker

```bash
docker build -t proatr .
docker run -p 8000:8000 proatr
```
