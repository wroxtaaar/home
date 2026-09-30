# Exact fix-seedr-direct-browser-stream application, packaged as one Render service.
# The application source itself is unchanged; this Dockerfile only combines the
# existing React frontend and FastAPI backend into one container.

FROM node:22-alpine AS frontend-build

WORKDIR /frontend
COPY frontend/package.json ./package.json
RUN npm install

COPY frontend/ ./

ENV VITE_API_URL=https://home-a9e7.onrender.com
RUN npm run build

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 nginx \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./
COPY --from=frontend-build /frontend/dist ./frontend-dist

RUN rm -f /etc/nginx/sites-enabled/default
RUN cat > /etc/nginx/sites-available/torrent-studio <<'NGINX'
server {
    listen 10000;
    server_name _;
    root /app/frontend-dist;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:10001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_request_buffering off;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
NGINX

RUN ln -s /etc/nginx/sites-available/torrent-studio /etc/nginx/sites-enabled/torrent-studio \
    && nginx -t

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn main:app --host 127.0.0.1 --port 10001 & exec nginx -g 'daemon off;'"]
