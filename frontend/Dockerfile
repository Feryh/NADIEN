FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

# Unprivileged variant: runs as uid 101 on port 8080, as Kubernetes "restricted" pods require.
FROM nginxinc/nginx-unprivileged:1.27-alpine
# Template: API_UPSTREAM / WORKER_UPSTREAM are substituted at start (only those, not nginx's own $vars).
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY security-headers.conf /etc/nginx/security-headers.conf
ENV API_UPSTREAM=backend:8000 \
    WORKER_UPSTREAM=backend:8000 \
    NGINX_ENVSUBST_FILTER="^(API|WORKER)_UPSTREAM$"
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
