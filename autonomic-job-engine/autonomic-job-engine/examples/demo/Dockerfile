# Fractal Lab demo UI: build with node, serve with nginx (which also proxies the API).
FROM node:20-bookworm-slim AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -y update && apt-get -y upgrade && apt-get -y clean && rm -rf /var/lib/apt/lists/*
WORKDIR /ui
COPY package.json ./
RUN npm install --no-audit --no-fund
COPY . .
ARG VITE_AJE_API_KEY=""
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-bookworm
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -y update && apt-get -y upgrade && apt-get -y clean && rm -rf /var/lib/apt/lists/*
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 8080
