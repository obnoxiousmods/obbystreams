# Installation

These steps assume Python 3, Starlette, Uvicorn, PyYAML, httpx, psutil, nginx, and ArangoDB are available.

## 1. Install files

Build the frontend assets before copying files:

```bash
npm ci
npm run build
```

```bash
sudo mkdir -p /opt/obbystreams /etc/obbystreams
sudo cp -a app.py static tools pyproject.toml uv.lock /opt/obbystreams/
cd /opt/obbystreams
sudo chown -R joey:nobody /opt/obbystreams
sudo -u joey /home/joey/.local/bin/uv sync --no-dev --frozen
sudo cp bin/obbystreams /usr/bin/obbystreams
sudo chmod 755 /usr/bin/obbystreams
sudo cp examples/obbystreams.example.yaml /etc/obbystreams/obbystreams.yaml
sudo chmod 640 /etc/obbystreams/obbystreams.yaml
```

Edit `/etc/obbystreams/obbystreams.yaml` and set real dashboard and ArangoDB passwords.

## 2. Bootstrap ArangoDB

```bash
python3 /opt/obbystreams/tools/bootstrap_arango.py \
  --root-password 'root-password' \
  --app-password 'same-password-as-yaml'
```

## 3. Install systemd service

```bash
sudo cp systemd/obbystreams.service /etc/systemd/system/obbystreams.service
sudo systemctl daemon-reload
sudo systemctl enable --now obbystreams.service
sudo systemctl status obbystreams.service --no-pager
```

## 4. Install nginx vhost

```bash
sudo cp nginx/s.obby.ca /etc/nginx/sites-available/s.obby.ca
sudo ln -s /etc/nginx/sites-available/s.obby.ca /etc/nginx/sites-enabled/s.obby.ca
sudo nginx -t
sudo systemctl reload nginx
```

Open `https://s.obby.ca`.
